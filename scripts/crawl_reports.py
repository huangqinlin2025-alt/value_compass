#!/usr/bin/env python3
"""财报语料爬虫：从巨潮资讯网（沪深两市法定披露平台）抓取年报 / 半年报 PDF。

    # 按种子清单抓取 2023-2025 年报 + 半年报
    python3 scripts/crawl_reports.py

    # 只抓年报、限制 5 份、间隔 2 秒（冒烟用）
    python3 scripts/crawl_reports.py --types annual --limit 5 --sleep 2

    # 忽略断点状态强制重抓
    python3 scripts/crawl_reports.py --force

产物（默认落在 data/reports/）：
    {code}_{period}.pdf          例：600519_2024A.pdf / 600519_2025H1.pdf
    {code}_{period}.meta.json    同名 sidecar：简称 / 代码 / 报告期 / 行业 / 公告标题 / 源 URL
    _crawl_state.json            已抓 URL → {path, sha256, status}，断点续传与去重依据

设计约定：
1. 只认文本型 PDF：下载后用 pypdf 抽样抽字，抽不出正文（扫描件）直接丢弃并记 skipped_textless。
2. 文件名由「6 位代码 + 期间」生成，不拼接公司名 / 公告标题，天然防重复落盘、也防路径穿越；
   manifest 的 path 因此稳定，purge_stale 不会误判文档下线。
3. sidecar 是 meta_recall 的权威元数据源，PDF 首页正则只是兜底（见 src/vc/ingestion/sidecar.py）。
4. 只访问白名单域名，限速 + 退避重试，中断可续跑。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# 安全白名单：禁止爬虫被引导到内联网段或任意第三方域名
ALLOWED_HOSTS = ("www.cninfo.com.cn", "static.cninfo.com.cn")

QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
STATIC_PREFIX = "http://static.cninfo.com.cn/"
# 全 A 股代码 → orgId 映射表（6255 家，约 530KB，只需下载一次后缓存）
STOCK_LIST_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"

# 报告类别（巨潮全量公告分类码）
CATEGORY = {
    "annual": "category_ndbg_szsh",   # 年度报告
    "semi": "category_bndbg_szsh",    # 半年度报告
    "q1": "category_yjdbg_szsh",      # 第一季度报告
    "q3": "category_sjdbg_szsh",      # 第三季度报告
}
PERIOD_SUFFIX = {"annual": "A", "semi": "H1", "q1": "Q1", "q3": "Q3"}
PERIOD_CN = {"annual": "年年度报告", "semi": "年半年度报告", "q1": "年第一季度报告", "q3": "年第三季度报告"}
PERIOD_TYPE = {"annual": "年报", "semi": "半年报", "q1": "一季报", "q3": "三季报"}

# 同一份报告在巨潮会带摘要 / 英文版 / 更正版多条，全部剔除，只留正文全文版
EXCLUDE_TOKENS = (
    "摘要", "英文", "取消", "作废", "更正", "补充", "问询", "二次",
    "差异", "说明", "申请文件", "审阅", "审核", "预案", "通知", "提示",
    "更新前",  # 半年报常有（更新前）/（更新后）两版，只保留更新后
    # 注意：不排除「修订」——修订版通常是最晚披露的最终版，由 pick_best 按时间择优
)

_YEAR_RE = re.compile(r"(20\d{2})\s*年")
_TAG_RE = re.compile(r"<[^>]+>")
_SAFE_CODE_RE = re.compile(r"^\d{6}$")


# --------------------------------------------------------------------------- 抓取请求


def _assert_host(url: str) -> None:
    host = urllib.parse.urlparse(url).hostname or ""
    if host.lower() not in ALLOWED_HOSTS:
        raise ValueError("拒绝访问非白名单域名: %s" % host)


def _http(url: str, data: bytes = None, headers: Dict[str, str] = None,
          timeout: float = 30.0, retries: int = 3, sleep: float = 1.0) -> bytes:
    """带退避重试的 HTTP 请求；只允许白名单域名。"""
    _assert_host(url)
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": UA,
            "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
            **(headers or {}),
        },
    )
    last: Optional[Exception] = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # 网络抖动 / 限流，退避重试
            last = exc
            if i + 1 < retries:
                time.sleep(sleep * (2 ** i))
    raise RuntimeError("请求失败 %s: %s" % (url, last))


def load_org_map(out_dir: Path, sleep: float = 1.0) -> Dict[str, str]:
    """代码 → orgId。巨潮查询必须传 orgId，且 orgId **无法**由代码推导：

    真实值形如 gssh0600519 / gssz0000858 / 9900002221 / gshk0001211 / GD165627，
    同一套规则里混了四种写法，唯一可靠来源是官方全量股票表。
    """
    cache = out_dir / "_cninfo_stocks.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    raw = _http(STOCK_LIST_URL, sleep=sleep)
    try:
        stock_list = json.loads(raw.decode("utf-8", "ignore")).get("stockList") or []
    except Exception:
        return {}
    mapping = {str(s.get("code")): str(s.get("orgId") or "") for s in stock_list}
    cache.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return mapping


def guess_org_id(code: str, exchange: str) -> str:
    """兜底：老式沪市/深市 orgId 为 gssh0{code} / gssz0{code}。"""
    return ("gssh0" if exchange.lower().startswith("sh") else "gssz0") + code


def query_announcements(code: str, org_id: str, category: str, se_date: str,
                        page_size: int = 30, sleep: float = 1.0) -> List[Dict[str, Any]]:
    """巨潮公告查询。

    坑 1：该接口只认 **表单编码** body，用 JSON body 会被静默忽略、返回全站 53 万条。
    坑 2：stock 参数必须是 `{code},{orgId}`，orgId 写错会**静默返回 0 条**（不是报错）。
    """
    column = "sse" if code.startswith(("6", "9")) else "szse"
    body = urllib.parse.urlencode({
        "pageNum": 1,
        "pageSize": page_size,
        "column": column,
        "tabName": "fulltext",
        "plate": "",
        "stock": "%s,%s" % (code, org_id),
        "searchkey": "",
        "secid": "",
        "category": category,
        "trade": "",
        "seDate": se_date,
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }).encode("utf-8")
    raw = _http(
        QUERY_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "X-Requested-With": "XMLHttpRequest"},
        sleep=sleep,
    )
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return []
    items = data.get("announcements") or []
    time.sleep(sleep)
    return items


def download_pdf(url: str, dest: Path, timeout: float = 120.0, sleep: float = 1.0) -> Path:
    """流式下载并原子 rename（先写 .part，避免中断留下半个 PDF）。"""
    _assert_host(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    data = _http(url, headers={"Referer": "http://www.cninfo.com.cn/"}, timeout=timeout, sleep=sleep)
    if not data.startswith(b"%PDF-"):
        raise ValueError("响应不是 PDF: %s" % url)
    tmp.write_bytes(data)
    tmp.replace(dest)
    time.sleep(sleep)
    return dest


# --------------------------------------------------------------------------- 校验与筛选


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_text_pdf(path: Path, min_chars: int = 100, sample: int = 6) -> Tuple[bool, str]:
    """文本型 PDF 判定：抽样若干页，平均每页可抽字符数 >= min_chars。

    扫描件每页抽字通常 < 20，抽不出正文的 PDF 入库后只有页码噪声，直接丢弃。
    """
    try:
        from pypdf import PdfReader
    except Exception:
        return True, "pypdf 不可用，跳过文本型校验"
    try:
        reader = PdfReader(str(path))
        n = len(reader.pages)
        if n < 5:
            return False, "页数过少(%d)" % n
        idxs = sorted({0, min(1, n - 1), n // 2, min(n // 2 + 1, n - 1), max(n - 2, 0), n - 1})
        idxs = [i for i in idxs if 0 <= i < n][:sample]
        total = 0
        for i in idxs:
            total += len((reader.pages[i].extract_text() or "").strip())
        avg = total / float(max(1, len(idxs)))
        if avg < min_chars:
            return False, "疑似扫描件：抽样 %d 页平均每页 %d 字 < %d" % (len(idxs), int(avg), min_chars)
        return True, "抽样 %d 页平均每页 %d 字，%d 页" % (len(idxs), int(avg), n)
    except Exception as exc:
        return False, "PDF 解析失败: %s" % exc


def clean_title(title: str) -> str:
    return _TAG_RE.sub("", title or "").strip()


def wanted_report(title: str, rtype: str, years: List[int]) -> Optional[int]:
    """判定公告标题是否为目标报告，返回年份；否则 None。"""
    t = clean_title(title)
    if rtype == "annual":
        # 年报标题形如「2024年年度报告」；含「半年度/中期」的是半年报，必须排除
        if "年度报告" not in t or "半年度" in t or "中期" in t:
            return None
    elif rtype == "semi":
        # AH 股公司（如中国平安）把半年报叫「中期报告」
        if "半年度" not in t and "中期" not in t:
            return None
    elif rtype == "q1":
        if "第一季度" not in t:
            return None
    elif rtype == "q3":
        if "第三季度" not in t:
            return None
    if any(tok in t for tok in EXCLUDE_TOKENS):
        return None
    m = _YEAR_RE.search(t)
    if not m:
        return None
    year = int(m.group(1))
    return year if year in years else None


def pick_best(cands: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """同一 (公司, 年份, 类型) 命中多条时取最新披露的一条，标题最短者优先。"""
    if not cands:
        return None
    return sorted(cands, key=lambda a: (-int(a.get("announcementTime") or 0), len(clean_title(a.get("announcementTitle", "")))))[0]


def ts_to_date(ms: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ms) / 1000.0))
    except Exception:
        return ""


# --------------------------------------------------------------------------- sidecar


def sidecar_path(pdf: Path) -> Path:
    return pdf.with_suffix(".meta.json")


def write_sidecar(pdf: Path, seed: Dict[str, Any], rtype: str, year: int,
                  ann: Dict[str, Any], source_url: str) -> Path:
    suffix = PERIOD_SUFFIX[rtype]
    meta = {
        "company": seed.get("company", ""),
        "short_name": seed.get("short_name", ""),
        "stock_code": seed.get("stock_code", ""),
        "exchange": seed.get("exchange", ""),
        "industry": seed.get("industry", ""),
        "sw_level1": seed.get("sw_level1", ""),
        "sw_level2": seed.get("sw_level2", ""),
        "report_period": "%s%s" % (year, suffix),
        "report_period_cn": "%s%s" % (year, PERIOD_CN[rtype]),
        "report_type": PERIOD_TYPE[rtype],
        "announcement_title": clean_title(ann.get("announcementTitle", "")),
        "publish_date": ts_to_date(ann.get("announcementTime")),
        "source_url": source_url,
        "crawled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "file_sha256": sha256_file(pdf),
    }
    p = sidecar_path(pdf)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


# --------------------------------------------------------------------------- 主流程


def load_seeds(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    for c in data.get("companies", []):
        if not _SAFE_CODE_RE.match(str(c.get("stock_code", ""))):
            raise ValueError("非法证券代码（必须 6 位数字）: %r" % c.get("stock_code"))
    return data


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "updated_at": 0.0, "items": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "updated_at": 0.0, "items": {}}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    state["updated_at"] = time.time()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def crawl_one(seed: Dict[str, Any], rtype: str, year: int, ann: Dict[str, Any],
              out_dir: Path, state: Dict[str, Any], args: argparse.Namespace,
              stats: Dict[str, int]) -> Optional[Path]:
    code = str(seed["stock_code"])
    adjunct = (ann.get("adjunctUrl") or "").lstrip("/")
    if not adjunct:
        stats["skipped_no_url"] += 1
        return None
    url = STATIC_PREFIX + adjunct
    dest = out_dir / ("%s_%s%s.pdf" % (code, year, PERIOD_SUFFIX[rtype]))

    item = state["items"].get(url)
    if item and not args.force and dest.exists() and item.get("status") == "ok":
        stats["skipped_state"] += 1
        return dest

    try:
        download_pdf(url, dest, sleep=args.sleep)
    except Exception as exc:
        print("  ✘ 下载失败 %s %s%s | %s" % (code, year, PERIOD_SUFFIX[rtype], exc))
        state["items"][url] = {"path": str(dest), "status": "download_failed",
                               "reason": str(exc)[:200], "ts": time.time()}
        stats["failed_download"] += 1
        return None

    ok, why = is_text_pdf(dest, min_chars=args.min_chars)
    if not ok:
        print("  ✘ 非文本型 %s %s%s | %s" % (code, year, PERIOD_SUFFIX[rtype], why))
        dest.unlink(missing_ok=True)
        state["items"][url] = {"path": str(dest), "status": "textless", "reason": why, "ts": time.time()}
        stats["skipped_textless"] += 1
        return None

    write_sidecar(dest, seed, rtype, year, ann, url)
    state["items"][url] = {"path": str(dest), "status": "ok",
                           "sha256": sha256_file(dest), "ts": time.time()}
    stats["downloaded"] += 1
    print("  ✔ %s %s %s%s | %s | %.1fMB" % (
        code, seed.get("short_name", ""), year, PERIOD_SUFFIX[rtype],
        clean_title(ann.get("announcementTitle", ""))[:32], dest.stat().st_size / 1048576.0))
    return dest


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser(description="抓取上市公司定期报告（年报 / 半年报）")
    ap.add_argument("--seeds", default=str(root / "data" / "reports" / "_seeds.json"), help="种子清单 JSON")
    ap.add_argument("--out", default=str(root / "data" / "reports"), help="PDF 与 sidecar 输出目录")
    ap.add_argument("--years", default="", help="报告年份，逗号分隔；默认取种子清单的 years")
    ap.add_argument("--types", default="annual,semi", help="报告类型：annual,semi,q1,q3")
    ap.add_argument("--limit", type=int, default=0, help="最多下载多少份（0=不限）")
    ap.add_argument("--sleep", type=float, default=1.0, help="每次请求间隔秒（限速）")
    ap.add_argument("--min-chars", type=int, default=100, help="文本型 PDF 每页最少抽字数")
    ap.add_argument("--dry-run", action="store_true", help="只列出命中公告，不下载")
    ap.add_argument("--force", action="store_true", help="忽略断点状态，强制重抓")
    args = ap.parse_args()

    seeds = load_seeds(Path(args.seeds))
    years = [int(y) for y in args.years.split(",") if y.strip()] or [int(y) for y in seeds.get("years", [])]
    types = [t for t in args.types.split(",") if t.strip() in CATEGORY]
    if not types:
        print("报告类型非法：%s（可选 annual,semi,q1,q3）" % args.types)
        return 1
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(out_dir / "_crawl_state.json")
    org_map = load_org_map(out_dir, sleep=args.sleep)

    # 披露时间窗：年报次年 4 月披露、半年报当年 8 月披露，窗口取 {min}-01-01 ~ {max+1}-06-30 全包
    se_date = "%d-01-01~%d-06-30" % (min(years), max(years) + 1)

    stats: Dict[str, int] = {"downloaded": 0, "skipped_state": 0, "skipped_textless": 0,
                             "failed_download": 0, "skipped_no_url": 0, "missed": 0}
    t0 = time.time()
    print("抓取范围：%d 家 × %s × %s 年 | 时间窗 %s | 输出 %s%s" % (
        len(seeds.get("companies", [])), "/".join(types), "/".join(map(str, years)),
        se_date, out_dir, "（dry-run）" if args.dry_run else ""))

    for seed in seeds.get("companies", []):
        code = str(seed["stock_code"])
        org_id = org_map.get(code) or guess_org_id(code, seed.get("exchange", "sh"))
        print("\n[%s %s · %s | orgId=%s]" % (code, seed.get("short_name", ""),
                                             seed.get("industry", ""), org_id))
        for rtype in types:
            try:
                anns = query_announcements(code, org_id, CATEGORY[rtype], se_date, sleep=args.sleep)
            except Exception as exc:
                print("  ✘ 查询失败 %s %s | %s" % (code, rtype, exc))
                continue
            bucket: Dict[int, List[Dict[str, Any]]] = {}
            for a in anns:
                y = wanted_report(a.get("announcementTitle", ""), rtype, years)
                if y:
                    bucket.setdefault(y, []).append(a)
            for year in sorted(bucket):
                if args.limit and stats["downloaded"] >= args.limit:
                    print("  · 达到 --limit=%d，停止" % args.limit)
                    save_state(out_dir / "_crawl_state.json", state)
                    print("\n耗时 %.1fs | %s" % (time.time() - t0, json.dumps(stats, ensure_ascii=False)))
                    return 0
                best = pick_best(bucket[year])
                if best is None:
                    continue
                if args.dry_run:
                    print("  · %s%s | %s" % (year, PERIOD_SUFFIX[rtype],
                                             clean_title(best.get("announcementTitle", ""))))
                    continue
                crawl_one(seed, rtype, year, best, out_dir, state, args, stats)
                save_state(out_dir / "_crawl_state.json", state)
            for year in years:
                if year not in bucket:
                    stats["missed"] += 1
                    print("  · 未命中 %s%s" % (year, PERIOD_SUFFIX[rtype]))

    save_state(out_dir / "_crawl_state.json", state)
    print("\n耗时 %.1fs | %s" % (time.time() - t0, json.dumps(stats, ensure_ascii=False)))
    print("产物目录：%s" % out_dir)
    return 1 if stats["failed_download"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
