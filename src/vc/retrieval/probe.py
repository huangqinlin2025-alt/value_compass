"""新星图节点的可召回性探针。

为什么需要它
------------
星图字典 `STAR_NODES` 只收"任意 A 股定期报告必有"的科目（当前 27 个）。用户关心的
内容远不止这些——某家公司特有的指标、某个细分口径，字典里都没有。

两个极端都要避开：

- **完全封闭**：只能点字典里的 27 个，遇到报告里写得很清楚、字典里却没收的科目，
  用户就是找不到入口；
- **完全放开**：模型想造什么节点就造什么。会造出「市盈率」这类定期报告里不披露的
  科目——用户点进去是空卡片，而空卡片在链路里的表现是"转兜底"，
  从 trace 上完全看不出是节点造错了（每个节点都 ok=true）。

所以：**允许模型提议，但提议必须过探针**。

判据：关键词整体子串命中，不是 token 覆盖率
------------------------------------------
第一版用 `coverage_score`（query 与文本的 token 集合交集 / query 大小），实测是废的：
「量子计算」「区块链」这种财报里绝不可能出现的词也拿到 0.5+，全部通过。

原因是中文短词被切成单字 + bigram 后太碎——query「量子计算」的 token 里有「计算」
这个 bigram，而财报里「计算方法」「计算依据」到处都是，单字「量」「子」更是必然出现，
于是覆盖率虚高。token 覆盖率衡量的是"字面相似"，不是"这个词是否存在"。

这里改用**关键词作为完整词是否出现在文本里**（子串匹配）：

- 「存货周转率」在茅台年报里作为整体出现 -> 命中；
- 「量子计算」不会出现 -> 不命中，拒绝入图。

同时仍要求 BM25 在当前作用域（公司 / 期间）下真的召得到片段：命中了词但不在
当前报告期里，点进去照样是空卡片。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..ingestion.bm25_index import get_bm25
from ..text_utils import coverage_score, strip_page_prefix, token_set
from .filters import apply_filters_to_chunks

# 命中率门槛：关键词里至少一半要作为完整词出现在召回片段中。
# 取 0.5 而不是 1.0，是因为模型常给同义并列（["存货周转率", "存货周转天数"]），
# 只命中其中一个也算这个科目确实存在；但低于一半说明它基本是在造词。
PROBE_MIN_HIT_RATIO = 0.5
# 候选池要按 bm25_recall 的口径放大：过滤条件（公司 / 期间）是先召回后过滤的，
# 池子只取 top30 时目标公司的 chunk 一条都进不来（公司名常在页眉里、已被清洗，
# 字面分最高的往往是别家公司的同款科目），过滤后要么为空、要么只剩不相干的片段。
# 实测「毛利率」在茅台年报里明明有 200+ 处命中，池子 30 时 hit=0.00 被误判为造词。
PROBE_POOL_K = 60
PROBE_POOL_K_FILTERED = 300


def _hit_ratio(keywords: Sequence[str], text: str) -> float:
    """关键词作为**完整词**出现在文本中的比例。

    长度 < 2 的词不参与：单字在中文财报里几乎必然出现，算进去只会把判据稀释成噪声。
    """
    ks = [str(k).strip() for k in (keywords or []) if len(str(k).strip()) >= 2]
    if not ks:
        return 0.0
    hit = sum(1 for k in ks if k in text)
    return hit / float(len(ks))


def probe_node(
    keywords: Sequence[str],
    company: str = "",
    filters: Optional[Dict[str, Any]] = None,
    min_hit_ratio: float = None,
) -> Tuple[bool, float, float]:
    """验证关键词在当前作用域下能否召回可回答的内容。

    参数
    ----
    keywords: 候选节点的检索关键词（模型给的）
    company: 公司名，用于拼检索词
    filters: 当前作用域的硬过滤条件（stock_code / report_period(s)），与正式召回一致

    返回
    ----
    (是否通过, 最佳命中率, 最佳字面覆盖率)

    两段式：BM25 先按作用域召回，再在召回结果里查词。若过滤后为空直接判失败——
    **当期没披露就是不能点**，放进星图只会得到一张转兜底的卡片。
    """
    idx = get_bm25()
    if not len(idx):
        return False, 0.0, 0.0

    # 检索词**不拼公司名**：公司名在正文里往往是高频噪声（茅台报告里"贵州茅台"
    # 出现在每一条关联方名称中），拼进去会让 BM25 被公司名主导——实测「存货」
    # 「销售费用」这类真词因此一条都排不进候选池，被误判成造词。
    # 作用域改由 filters 硬过滤保证（先召回后过滤，与 bm25_recall 同口径）。
    q = " ".join(dict.fromkeys(
        [str(k).strip() for k in (keywords or []) if str(k).strip()]))
    if not q:
        return False, 0.0, 0.0

    hits = idx.search(q, top_k=PROBE_POOL_K_FILTERED if filters else PROBE_POOL_K)
    if not hits:
        return False, 0.0, 0.0

    chunks: List[Dict[str, Any]] = [dict(idx.chunks[i]) for i, _ in hits]
    if filters:
        chunks = apply_filters_to_chunks(chunks, filters)
    if not chunks:
        return False, 0.0, 0.0

    q_terms = token_set(q)
    best_hit = 0.0
    best_cov = 0.0
    for c in chunks:
        text = strip_page_prefix(c.get("text") or "")
        if not text:
            continue
        # 只查科目词，不查公司名：公司名常在页眉里、入库时已被清洗掉，
        # 拿它做子串判据会把"茅台"没出现在正文里的正常片段误判为不命中。
        best_hit = max(best_hit, _hit_ratio(keywords, text))
        best_cov = max(best_cov, coverage_score(q_terms, text))

    thr = PROBE_MIN_HIT_RATIO if min_hit_ratio is None else float(min_hit_ratio)
    return best_hit >= thr, round(best_hit, 4), round(best_cov, 4)
