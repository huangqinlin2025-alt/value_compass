# 节点规格表

统一约定：

- 节点签名 `(state) -> dict`，返回**增量 patch**，禁止直接 mutate 大对象；
- 所有节点被 `@safe_node(name, timeout, retries, provider, fallback_patch)` 包裹；
- 节点永不向外抛异常，失败时写入 `errors` + `degraded`，返回"安全的最小可用 patch"；
- 节点名与代码函数名一一对应，本表是节点规格的唯一真源。

## 一、入库子图（`src/vc/graph/nodes/ingest_nodes.py`）

| 节点 | 设计思路 | 功能 | 读 State | 写 State | 失败模式与处理 |
| --- | --- | --- | --- | --- | --- |
| `purge_stale` | 文档下线不能留幽灵 chunk | 对比 manifest 与磁盘，删除已消失文档的向量与快照 | — | `purge_stats` | 删除失败 → 标记 stale，下次入库重试 |
| `load_pdf` | 复用 `test_loader.py` 已验证的 PyPDFLoader | 按页加载 + 文件指纹（size/mtime/sha256） | `doc_path` | `pages`, `doc_id`, `doc_meta` | 文件缺失/加密/损坏 → `E_LOAD`（fatal），**终止入库不写 manifest** |
| `clean_normalize` | 页眉页脚噪声会同时污染 BM25 词频与向量语义 | 跨页统计短行频率，出现率 ≥30% 视为页眉页脚剔除；去页码行、水印、连续重复行；符号字体归一（私用区勾选框 → `☑`、项目符号 → `·`） | `pages` | `pages_clean` | 清洗异常则保留原文继续（警告级） |
| `enrich_metadata` | 元数据是第三路召回与溯源的基础 | 读同名 sidecar（`*.meta.json`）取 company/short_name/stock_code/report_period/industry，**缺失才回落**首页正则；记录 embedding provider 与 dim | `pages_clean`, `doc_path` | `doc_meta` | sidecar 缺失 → 回落正则并打 `degraded: meta_no_sidecar`，不阻断 |
| `table_aware_split` | 表格被切碎 = 数字失去行列语义 | 表格区整块成 chunk（超长按行切并重复表头）；正文 800/120 滑窗；<120 字残块并入相邻块；每块加 `[P页码｜章节路径]` 前缀 | `pages_clean`, `doc_meta` | `chunks`, `doc_version` | 切分异常 → `E_SPLIT`，回退按页切分 |
| `dedup_hash` | 为增量提供最小比对粒度 | 计算 `page_hashes`、`file_sha256` | `pages_clean`, `doc_meta` | `page_hashes`, `doc_hash` | hash 失败 → 视为全量更新 |
| `plan_diff` | 避免每次全量重嵌 | 决策树：新文档/provider 变化/维度变化 → `full`；sha256 未变 → `skip`；少量页变 → `page` | `doc_meta`, `page_hashes` | `diff_plan`, `manifest` | 读 manifest 失败 → 按空清单处理（等价全量） |
| `skip_node` | 未变更直接短路 | 只写 trace | `diff_plan` | `skipped`, `upsert_stats` | — |
| `embed_upsert` | 昂贵的 embedding 只做在变更页上 | `page` 模式按 `$in` 删变更页（失败降级为整文档替换）；批量编码（BGE：`batch_size=32`、L2 归一化、MPS/CPU 自动选设备）；写向量库 + chunk 快照 | `chunks`, `diff_plan` | `upsert_stats`, `doc_version` | 权重下载失败 → HF 转 ModelScope 兜底；Embedding 超时 → 重试 2 次 → 回落 Hashing；写入失败 → `E_STORE_UPSERT` 保留旧索引 |
| `build_bm25` | BM25 与向量库必须同源 | 读取所有活跃文档的 chunk 快照重建倒排并落盘 | — | `bm25_stats` | 落盘失败 → 内存可用但重启失效，告警 |
| `write_manifest` | 原子切换避免"写一半"的脏索引 | 先写 `.tmp` → `store.count()` 校验 → `os.replace` 原子替换 | `upsert_stats` | `manifest`, `doc_version` | 校验不通过 → 抛错，旧版本继续服务 |

## 二、问答主图（`src/vc/graph/nodes/`）

| 节点 | 设计思路 | 功能 | 读 State | 写 State | 失败模式与处理 |
| --- | --- | --- | --- | --- | --- |
| `query_rewrite` | 追问缺主语 + 术语口语化 | 指代消解（承接上一轮实体）+ 金融同义扩展 + 抽取 filters；**`ui_action=click_star/clarify_followup` 时绕过 `query_raw`**，直接用 `ui_filters` 构造检索词；**公司名解析**（见 2.2）：`company` 归一不出 `stock_code` 时用 `known_meta()` 反查，库外标的注入 `OOS_STOCK_CODE` 让三路硬过滤必然落空 → 转兜底；**作用域解析**（见 2.3）：公司必填（显式 > 会话继承 > 库内唯一 > 回「请先选择公司」卡片），期间默认最新一期且只做软偏好 | `query_raw`, `history`, `ui_action`, `ui_filters`, `session_company` | `query_rewritten`, `query_terms`, `filters`, `star_node_id`, `ui_filters`（解析后写回，供 router 复用）, `session_company` | 空 `query_raw` 且非 UI 事件 → `E_INTERNAL`；每轮返回 `RESET_LIST` 重置 errors/trace 回合窗口 |
| `intent_router` | 不同意图决定召回权重与是否直答；**合规红线用规则最可解释，语义绕弯才问 LLM** | 规则打分 → 低置信/多意图并列时调 `generate_json(IntentResult)`；红线（OOS/CHITCHAT）不被 LLM 推翻；**`click_star`/`clarify_followup` 前置短路**：节点 id 即意图（`STAR_NODES` 映射），不调 LLM，`ui_filters` 清洗后作为绝对过滤（`filters_strict=True`） | `query_rewritten`, `history`, `ui_action`, `ui_filters` | `intent`, `intent_confidence`, `intent_scores`, `route_cfg`, `filters`, `filters_strict`, `star_node_id` | LLM 失败/脏输出 → 回落规则并记 `degraded=["router:llm_fallback_rule"]`；provider 不支持 JSON（Mock）→ 直接走规则；top1-top2 差 <0.15 → 用 GENERAL 配置；未知星图节点 → `degraded=["ui:unknown_star_node"]` 并按 node_id 当检索词继续 |
| `bm25_recall` | 保数字/会计科目精确命中 | BM25 top 20~24（有过滤条件时先放大候选池），再做**软**元数据过滤 | `query_rewritten`, `filters`, `route_cfg` | `recall_bm25` | 索引缺失 → `E_BM25` + 空列表，其它两路继续 |
| `vector_recall` | 保同义泛化 | embedding 查询 + Chroma `where` 过滤 | `query_rewritten`, `filters`, `route_cfg` | `recall_vector` | 超时 3s / 库不可用 → `E_STORE_QUERY`，降级为 BM25+meta |
| `metadata_recall` | 保范围正确（公司/期间/行业） | 无过滤条件直接返回空；否则 **过滤优先**（`BM25Index.search_where` 先按元数据圈定范围再按 BM25 排序），再按覆盖度排序 | `filters` | `recall_meta` | 无匹配 → 空列表（**不是错误**） |
| `rrf_fusion` | 三路分数不可比，用排名融合 | 加权 RRF + 表格 boost + 相关性闸门（IDF 加权） | 三路召回, `query_terms` | `fused`, `fusion_stats` | 空/低分/低相关 → 记 `E_EMPTY_RECALL`/`E_LOW_SCORE` → 路由到兜底 |
| `rerank` | 融合后精排提升 top5 质量；真模型可选、启发式零依赖兜底 | `RERANK_PROVIDER=bge` → `CrossEncoderReranker`（bge-reranker-base 逐对打分，写 `rerank_score`）；默认启发式：0.45 覆盖 + 0.25 数值命中 + 0.15 表格 + 0.15 页码邻近 + RRF 分；`prefer_table` 时表格提前；**期间聚焦**（见 2.3）：主导期片段稳定提前 | `fused`, `route_cfg`, `ui_filters` | `reranked` | 真模型加载失败 → 工厂回落启发式并记 `degraded=["rerank:fallback_heuristic"]`；节点失败/超时 → 用 `fused` 顺序，记 `degraded=["rerank"]` |
| `context_compress` | 控 token 预算 | 按分数降序填至 4000 token，按 chunk_id 去重；**同期聚焦**（见 2.3）：主导期片段 ≥2 条时只保留这一期 | `reranked`, `ui_filters` | `context` | 超预算 → 跳过该块；极端情况保底取前 3 |
| `generator` | 适配器隔离模型差异；**输出必须是可校验的结构** | 走 `generate_json(AnswerResult)`（answer + citations + used_chunk_ids），按 context 页码白名单剥离编造引用；不支持 JSON 的 provider 回落抽取式；**UI 事件分支**改走 `generate_json(UiAnswerResult)`，产出 `ui_payload`（explanation≤100 字 / citations / unlock_next）；模型漏标引用时用 context 页码补引，`[P12|章节路径]` 归一为 `[P12]`（**见 2.1**） | `context`, `intent`, `history`, `ui_action`, `star_node_id`, `unlocked_nodes` | `draft_answer`, `answer`, `generation_used`, `ui_payload` | JSON 解析/校验失败 → 修复重试 1 次 → 回落抽取式并记 `degraded=["generator:json_fallback_text"]`；UI 分支失败 → 退化 `ui_payload`（`refused=true`），图不断裂 |
| `faithfulness_gate` | 金融问答的防幻觉底线；**两层判定取严**：确定性比对 + LLM 语义判定 | ①数值字面量必须逐字命中 context、引用页码必须真实存在；②`generate_json(FaithfulnessResult)` 抓"数字对但结论越界" | `draft_answer`, `context` | `gate_pass`, `gate_reason` | LLM 不可用 → 只记 `degraded=["gate:llm_skipped"]`，判定权回到底线；任一层不通过 → `retry_shrink` 收紧后重试 1 次 → 仍失败转兜底 |
| `retry_shrink` | 给自愈一次机会 | `retry_count+1`，context 砍半 | `context`, `retry_count` | `context`, `retry_count` | — |
| `citation_validate` | 引用必须可溯源 | 从 `answer` 抽取引用页码（**口径见 2.1**：`[P12]` 角标 +「来源：P12」变体），再按 context 页码白名单判定；剥离不可溯源引用 | `answer`, `context` | `citations`, `valid` | 引用编造 → 剥离 + `E_CITATION_MISS`；含数字却抽不出任何引用 → `valid=false`，由 `output_guard` 转摘录兜底 |
| `unlock_commit` | 星图进度是**状态**而非一次输出，必须落 Checkpointer | 只写本次实际点亮的节点（白名单内）；`unlock_next` 仅作推荐留在 `ui_payload`，不冒充已点亮 | `star_node_id`, `unlocked_nodes`, `ui_payload`, `ui_action` | `unlocked_nodes`, `ui_payload` | `reset_starmap` → 返回 `RESET_LIST` 清空；未知节点 → 不写 |
| `output_guard` | 合规与格式收口 | 追加免责声明；**无来源却含数字一律转摘录兜底**；UI 事件下把降级结果同步回 `ui_payload`（卡片与正文必须同口径），免责声明以独立 `disclaimer` 字段下发，不占 explanation 的 100 字预算 | `answer`, `citations`, `valid`, `ui_payload` | `final_answer`, `ui_payload` | 无答案 → 统一"未在报告中找到"；UI 分支无源数字 → 卡片 `explanation` 一并降级并置 `refused=true` |
| `fallback_node` | 五类场景各有话术，绝不静默失败 | 见 `fallback.md`；UI 事件下额外产出退化 `ui_payload`（前端不空屏） | `errors`, `context`, `ui_action`, `star_node_id` | `final_answer`, `fallback_reason`, `ui_payload` | — |
| `refuse_node` | 合规红线硬拦截 | 拒答买卖/荐股/目标价 | — | `final_answer` | — |
| `direct_answer` | 闲聊不进检索 | 自我介绍 + 示例问题 | — | `final_answer` | — |
| `clarify_node` | 追问优于瞎猜 | 澄清问句 + 3 条候选问法 + **结构化按钮**（`ui_payload.options` 带 `node_id`/`ui_filters`，点一下即补齐条件） | `intent_confidence`, `ui_filters` | `final_answer`, `suggestions`, `ui_payload` | — |
| `error_node` | 错误只汇总不覆盖答案 | 汇总 errors/degraded/trace；仅在确实无答案时产出安全回复 | `errors`, `final_answer` | `error_summary`, `final_answer` | — |

### 2.1 引用抽取口径（`citation_validate`）

抽取由 `providers/llm.py :: extract_citations` **单点负责**；判定链条是
「先抽取 → 再过 context 白名单」，两段规则互不替代。改这里即改全局闸门。

**一、抽取：认两种写法**

| 写法 | 示例 | 计为引用 |
| --- | --- | --- |
| 角标（契约要求） | `……营业收入 18.2 亿元 [P10]` | ✅ |
| 来源串（放宽兼容） | `（来源:P10、P12、P15）` / `来源：P10` | ✅ |
| 裸页码 | `见 P10` / `第 10 页` / `P2P` | ❌ |

来源串必须匹配 `(来源|出处|参见) + 冒号(半角/全角) + 页码列表` 才算数，
**没有来源类前缀的裸 `P数字` 一律不认**。前缀是唯一的防噪声闸门：财报正文里
`P2P` / `PEG` / `P/E` 中的 `P数字` 一旦被误抽，会先落入 `bad`（不在白名单）
再触发"剥离引用"，等于把合法答案误判成编造引用。

**二、判定：白名单仍是硬边界，严格性未下降**

抽到的页码仍需 `∈ context 页码集合`：

- 命中 → 产出 `citations[{page, doc_id, section_path, snippet}]`，`valid=true`；
- 未命中 → 记 `bad`，剥离正文中的 `[Pxx]`，`valid=false` + `E_CITATION_MISS`（`fallback=strip_citation`）；
- 一个都没抽到、且正文含数字 → `valid=false` + `E_CITATION_MISS`（`fallback=fallback`），由 `output_guard` 转摘录兜底。

即：**放宽的是"认不认得出"，不是"认出之后算不算数"**。

**三、为什么放宽（真实模型暴露的缺陷）**

`glm-4-flash` 等小模型不遵守 `prompts/ui_generator.py` 的 `[P页码]` 契约，
实测把角标写成 `（来源:P10、P12、P15）`。只认角标时，"模型明明给了可溯源页码"
却被判成"答案含数字但无任何可溯源引用"，星图卡片被 `output_guard` 整段改写为
"未能从报告中获得可溯源的数值"（`degraded` 为空，从 trace 看每个节点都 `ok=true`
——纯靠节点级单测无法发现）。

更关键的是 `generate.py :: sanitize_answer` / `_ui_generate` **自身补引时生成的
就是"来源：P10、P12"格式**：只认角标会让代码产出的引用被自己的校验判为无效。
所以这不是"向模型让步"，而是修掉一处口径自相矛盾。

**四、边界与不变量**

1. 数值一致性由 `faithfulness_gate` 独立负责（逐字比对 + LLM 语义），与本次放宽无关；
2. `output_guard` 的"无来源数字一律转兜底"规则不变；
3. 放宽不产生"从无到有的引用"：只把模型已写出但格式不合规的页码捞回来，
   捞回后仍须过白名单——编造页码（`[P99]` 类）照样被剥离，幻觉探针结果不受影响。

**五、上游归一：`[P12|章节路径]` → `[P12]`**

`<资料>` 的片段前缀形如 `[P12|审计意见]`，模型常把它整段抄进输出。
不归一的后果有两个：卡片上显示 `|审计意见 >` 这类脏后缀；抽取正则只认 `[P12]`，
整段前缀一个引用都抽不到，又落回"含数字却无引用"的误杀。
归一由 `generate.py :: _ui_generate` 在上游完成，只处理**闭合**的 `]`——
不闭合的多半已被 100 字截断，宁可留着，也不要为了清理而误删正文。

### 2.2 库外标的的过滤与拒答（`query_rewrite`）

**问题**：`clean_ui_filters` 只认 4-6 位数字，于是 `company="特斯拉"` / `"苹果公司"`
归一不出 `stock_code` → `filters` 为空 → **向量路的 `where` 为 None**，
必然按语义召回库内其它公司的片段。召回不空 → 不转兜底 →
卡片拿"营业收入的通用定义"冒充成特斯拉的答案，且 `refused=false`。

同一成因也伤库内标的：`company="贵州茅台"`（中文名）同样取不到代码，
三路没有硬过滤条件就会召回别家公司的报表原文（跨公司串味）。

**规则**（`rewrite.py :: _resolve_company`）

| 输入 | 解析结果 | 效果 |
| --- | --- | --- |
| `company="600519"` / `"贵州茅台"` | 反查 `known_meta()` 命中 → 补 `stock_code` | 三路硬过滤生效，锁定目标公司的文档 |
| `company="特斯拉"` / `"苹果公司"` | 未命中 → 注入 `OOS_STOCK_CODE`（`__not_in_corpus__`） | 三路必然落空 → `E_EMPTY_RECALL` → 转兜底（`refused=true`） |
| `company="0700"`（港股，库内无） | 取到 4 位码但库中无匹配 | 同上，转兜底 |

**边界**

1. 哨兵值不是真代码，只用于"让过滤必然落空"，因此 `clean_ui_filters` 对 `stock_code`
   的 `_norm_code` 校验**必须显式放行**它——否则被当非法值丢弃后，
   库外标的又退回无过滤状态（第一次修复就是在这里失效的）；
2. 解析结果必须**写回 `state["ui_filters"]`**：`intent_router._star_patch` 从 state 读同一份，
   不写回的话 router 会用未解析版本覆盖掉 `stock_code`；
3. 仅作用于 UI 事件（`click_star` / `clarify_followup`）；自然语言回合走
   `extract_filters` 的既有逻辑，不受影响。

### 2.3 星图作用域：公司必填与期间聚焦（`query_rewrite` / `rerank` / `context_compress`）

**问题**：前端点星图只传 `{"node_id": "risk"}` 时，三路召回没有任何硬过滤，
向量路 `where=None` 按语义召回各家公司同一科目的片段（实测一次点击混进
隆基绿能 / 药明康德 / 中国平安 / 伊利 4 家）。后果有两个：

1. 卡片只能归纳出"风险因素是指……"这类通用定义，对该公司一无所知；
2. `citations` 里的 `P2` 是**歧义 ID**——它同时属于好几家公司、好几期报告。

**规则**（`rewrite.py :: _resolve_scope`）

| 维度 | 取值优先级 | 是否硬过滤 |
| --- | --- | --- |
| 公司 | 显式 `company` > 会话 `session_company` > 库内唯一一家 > **拒答** | 是（`stock_code`） |
| 期间 | 未指定 → 该公司最新一期 | **否**（`prefer_period`，只进检索词） |

公司一律不"猜"：多家公司且会话未锁定时注入 `OOS_STOCK_CODE`，三路打空 → 转兜底，
卡片话术指向下一步动作（列出可选公司）。宁可拒答，也不能把 A 公司的资料
当成 B 公司的答案——这与 2.2 的库外标的是同一条底线。

**为什么期间不做硬过滤**：实测硬锁定期会把召回打空（茅台 2025A 的"毛利率"
只召回 6 条，且 IDF 相关性不过线 → 转兜底）。硬过滤公司已消除最严重的跨公司串味，
期间改由两级"聚焦"处理：

1. `rerank`：主导期片段**稳定排序**提前——只改顺序不改集合，召回不足时其它期照样补位；
2. `context_compress`：主导期片段 ≥2 条时**只保留这一期**（门槛 2 条是安全垫，
   同期资料不足时宁可保留混合，也不让上下文变得太薄）。

不聚焦的后果：模型把这一期的金额安到另一期的口径上，数值闸门判 `E_NUM_MISMATCH`
→ 整张卡片转兜底（实测 `business_mix` 即如此）。

**边界**

1. 用户**显式**指定的 `report_period` 仍是硬过滤（库外期间 `1999A` 必须转兜底）；
   只有系统补的默认期是软偏好，二者用不同字段区分：`report_period` vs `prefer_period`；
2. `prefer_period` 必须在 `_UI_KEYS` 白名单内，否则 `intent_router._star_patch`
   清洗后会丢掉它，检索词里就没有期间了；
3. 会话作用域只在**解析出真实公司**时写入，库外哨兵不写——否则一次库外点击
   会把会话锁死在 `__not_in_corpus__` 上，后续点击全部转兜底；
4. 卡片回传 `ui_payload.scope`（`stock_code` + `company` + `report_period`），
   由 `ui.scope_of_chunks` 从**实际喂给模型的 context** 反推，不读 `filters`
   （filters 可能是空的，也可能带哨兵，都会说谎）。

### 2.4 星图字典、节点热度与期间对比（`ui.py` / `rewrite.py` / `context_compress`）

**一、字典是"打底"而非"封闭"**

`STAR_NODES` 从 9 个扩到 27 个。入字典的标准是**任意 A 股定期报告必有的科目**：
只部分公司披露的（客户集中度、股权激励等）不放，否则用户点了就是空卡片。

字典之外仍允许模型造新节点，但必须先过**可召回性探针**（见 2.5）。

**二、keywords 不是越多越好（实测踩过的坑）**

`star_query` 把 keywords 拼进检索词，而相关性闸门看的是 **IDF 覆盖率**——
多一个语料里不出现的同义词，就多一份覆盖率损失：

| 改动 | top1 相关性 | 结果 |
| --- | --- | --- |
| 毛利率加「综合毛利率」 | 0.648 → 0.256 | 跌破 0.28 阈值，转兜底 |
| 去掉无关的「明细 构成」 | 总资产 0.263 → 0.352 | 通过 |

两条规则：

1. keywords 只放**财报原文必现**的表述，控制在 2 个以内；
2. 「明细 构成」只有 `TABLE` 意图才拼进检索词。总资产 / 资产负债率 / 商誉这类
   单一数值科目没有构成表，硬拼纯属稀释。表格加权交给 rerank 的 `prefer_table`，
   不靠检索词硬凑。

**三、期间对比**

| 入参 | 语义 |
| --- | --- |
| `report_periods: ["2024A", "2025A"]` | 显式指定两期，走**硬过滤**（`chunk_matches` 多期 OR、Chroma `$or`） |
| `compare: true` | 要对比但不指定期 → `default_periods()` 补"最新 + 去年同期" |
| 都不传 | 单期，`prefer_period` 软偏好（0.4.3 的行为） |

三条约束：

1. **同口径**：`default_periods` 只配"同尾缀的前一年"（2025A ↔ 2024A）。
   拿 2025A 对 2024H1 会把全年数和半年数并排，模型算出的"同比 -50%"是假的，
   而数值闸门查不出——那两个数在原文里确实存在。
2. **检索词不带期间**：对比时两个期间都进 query 会稀释 IDF，且会把召回偏向其中一期
   （另一期只剩零星片段，卡片缺一半数据）。期间定位完全交给硬过滤。
3. **`context_compress` 按期间交替取**：沿用"只留主导期"会让对比退化成单期——
   只见一期数字却要输出同比，模型只能编，而闸门照样查不出。

**四、引用的三坐标：公司 - 期间 - 页码**

页码只在「公司 + 期间」下才唯一。单期时 `scope` 给 `report_period`；
对比时给 `report_periods`，并另给 `citations_by_period`（每期各自的页码）。

为什么不是"给每个页码打期间标签"：报表页码结构逐年固定，「营业收入」在 2024A 和
2025A 里往往都在 P54，反查期间时两期都命中、判不明。按期间分组天然无歧义。

**五、点亮 vs 亮度**

| 字段 | 语义 | 产生方式 |
| --- | --- | --- |
| `unlocked_nodes` | 内容确实读过 | 仅直接点击 |
| `node_heat` | 与节点的关联强度（驱动亮度） | 直接点击 +1.0，被推荐 +0.5 |

分开的理由：若让"被推荐够多次"也点亮，会出现没读过却已亮的节点，点进去没内容。
热度**不随公司/期间切换重置**——它衡量的是用户兴趣的累积，与当前作用域无关。

### 2.5 受控扩充：模型可以造节点，但必须先过探针（`retrieval/probe.py`）

**问题**：27 个科目覆盖不了所有报告里的内容；但放开让模型造词也不行——
会造出「市盈率」这类定期报告不披露的科目，用户点进去是空卡片。
更麻烦的是空卡片在链路里的表现是"转兜底"，trace 上每个节点都 `ok=true`，
排查时看不出是节点造错了，只会以为"这家公司没披露"。

**规则**：模型提议 → 探针验证 → 通过才写进会话星图。

| 环节 | 落点 |
| --- | --- |
| 提议 | `UiAnswerResult.new_nodes`（id + 中文 label + 1-3 个关键词），每轮限 2 个 |
| 验证 | `probe_node`：BM25 召回 → 作用域过滤 → 关键词**整体命中率** ≥ 0.5 |
| 放行 | 写入 `state.session_nodes`（`merge_nodes` reducer，跨轮保留） |
| 消费 | `node_meta` / `star_query` / `clean_unlock_next` / `unlock_commit` 都认它 |

**判据为什么是子串命中而不是 token 覆盖率**（踩过的坑）

| 判据 | 「量子计算」 | 「毛利率」 | 结论 |
| --- | --- | --- | --- |
| `coverage_score`（token 集合交集） | 0.67 → 通过 | 0.62 | 失效：中文短词被切成单字/bigram，「计算」在财报里到处都是 |
| 关键词整体子串命中 | 0.00 → 拒绝 | 1.00 | 直接回答"报告里有没有这个词" |

另外两个坑：

1. **探针的检索词不能拼公司名**。公司名在正文里是高频噪声（茅台报告里「贵州茅台」
   出现在每一条关联方名称中），拼进去后 BM25 被公司名主导——实测「存货」「销售费用」
   这类真词一条都排不进候选池，被误判成造词。作用域改由 `filters` 硬过滤保证。
2. **候选池要按 `bm25_recall` 的口径放大**（有过滤时 300 而非 60）。先召回后过滤，
   池子小了目标公司的 chunk 根本进不来——「毛利率」在茅台年报里有 200+ 处命中，
   池子 30 时命中率却是 0.00。

**边界**

1. 只活在会话里，**不写进 `STAR_NODES`**：全局字典是人工确认过的"财报必有科目"，
   模型一次抖动就永久污染，且无法回滚；
2. 探针自身异常时**不放行**：宁可不扩充，也不放幽灵节点；
3. 没给关键词的提议直接丢弃——无法验证等同于不能放行；
4. 每轮最多 2 个，且通过的节点才补进 `unlock_next`（受 `limit=2` 约束）。

### 2.6 前端渲染（`apps/streamlit_app.py`）

字典里的分组只有注释、不可机读，所以另设 `ui.STAR_GROUPS` 显式给出渲染顺序，
并由单测断言"分组覆盖全部字典 id"——漏加只会导致前端少渲染一个节点，不报错，
只能靠断言兜住。

节点状态用文案标记 + `type=primary` 表达（Streamlit 无法给单个 button 加 class）：
`○ 未点亮` / `⭐ 推荐（unlock_next）` / `● 已点亮` / `◉ 当前`。

会话新节点单独成组「本会话新发现」，**不与字典节点混排**：前者是模型当场提议、
只在本会话有效的，后者是人工确认过的，视觉上要能区分。

两个 Streamlit 特有的坑：

1. `render_evidence` 自带 expander，**不能**再包进另一个 expander（运行时才抛错）；
2. 同一 run 里按钮先渲染、点击后处理，所以"已点亮"标记会滞后一帧——
   `click_node` 末尾必须 `st.rerun()`，否则用户看到的是"点了没反应"。

## 三、条件边（路由函数）

| 路由函数 | 位置 | 分支 |
| --- | --- | --- |
| `route_after_load` | `ingest_graph.py` | 有 pages → `clean_normalize`；否则 → END（加载失败） |
| `route_after_plan` | `ingest_graph.py` | `skip` → `skip_node`；否则 → `embed_upsert` |
| `route_after_rewrite` | `query_graph.py` | `reset_starmap` → `unlock_commit`（纯 UI 动作，跳过改写/路由/召回）；否则 → `intent_router` |
| `route_after_router` | `query_graph.py` | `OOS`→refuse；`CHITCHAT`→chitchat；`UNCLEAR`→clarify；其余 → `Send` 三路并行 |
| `route_after_fusion` | `query_graph.py` | 空 / `relevant=False` → fallback；否则 → rerank |
| `route_after_gate` | `query_graph.py` | 通过 → validate；未通过且 retry<1 → retry_shrink；否则 → fallback |
| `route_after_guard` | `query_graph.py` | 无 final_answer → error_node；否则 → END |

## 四、LLM 契约层（`schema.py` + `prompts/`）

三个"大脑节点"的输出不再是自由文本，而是受 Pydantic Schema 约束的 JSON：

| 节点 | Schema | 关键字段 | 落库/下游消费 |
| --- | --- | --- | --- |
| `intent_router` | `IntentResult` | `intent`(9 类枚举) / `confidence` / `reason` / `filters` / `refused` | `route_cfg` 决定三路权重与 top_k |
| `faithfulness_gate` | `FaithfulnessResult` | `passed` / `mismatched_numbers` / `bad_citations` / `unsupported_claims` | 不通过 → `retry_shrink` |
| `generator` | `AnswerResult` | `answer`(内嵌 `[Pxx]`) / `citations[{page,quote,chunk_id}]` / `refused` / `used_chunk_ids` | `citation_validate` 二次校验 |
| `generator`（UI 分支） | `UiAnswerResult` | `explanation`(≤100 字，校验期硬截断) / `citations:["P12"]` / `unlock_next`(去重限 2) / `refused` | `ui_payload` 直出前端；`explanation` 同时写入 `answer`，照旧过 `gate`/`validate` |

调用链（`providers/llm.py :: generate_json`）：
`json_object 模式` → 抠 JSON（容忍围栏/前后废话）→ `model_validate_json`
→ 失败则带错误信息**修复重试 1 次**（`LLM_JSON_REPAIR`）→ 仍失败抛 `E_LLM_BADJSON`
→ 节点回落确定性逻辑。无 Key 时用 `LLM_PROVIDER=fake` 的 `FakeLLM` 注入脚本化响应做契约测试。

## 五、新增节点的规范

见 `docs/dev/how-to-add-node.md`：必须同步更新本表与 `architecture.md` 的图。
