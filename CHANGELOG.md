# Changelog

本项目遵循"文档与代码同 PR 更新"（见 `docs/adrs/ADR-0005-文档与代码同-PR-更新.md`）：
任何 PR 都必须在本文件追加条目。

## [0.5.0] - 2026-09-27

知识星图的**可玩性**：点亮不再只看邻接表，而是「探索前沿」——未点亮节点必须与已点亮
节点相邻才可点；新增会话边（用户可自己连两个指标）；亮度改为绝对数量分档。同时把后端
新能力在 Streamlit 前端**全部接通**：此前星图后端能发信号，前端一个都不认，跑起来
用户看到的仍然是 0.4 的星图。

### 新增

- **探索前沿 `clickable`**（`ui.py :: compute_clickable`）：未点亮节点 x 可点亮 ⟺
  ∃ 已点亮 u 使 `dist_G(x,u) ≤ 1`。此前任何节点都能点、点了才被告知"没解锁"，
  用户会以为是自己点错了，而不是机制在起作用。
- **图上距离 `dist_G`**：规则边取 `next` 的**无向对称闭包**（A.next 含 B 不代表 B.next
  含 A；不做闭包会让距离随方向变化，同一对节点因点击先后得到不同结果，像星图在随机变）。
  会话边**最多 1 条且不串联**：可自由串联时连几条边就能把 37 节点图的直径压到 2~3，
  `clickable ≈ 全部`，「探索前沿」自我瓦解——而用户恰恰有动机这么做（能连边何必一步步走）。
- **冷启动种子点 `SEED_NODES`**：unlocked 为空时若按"距离已点亮 ≤1"算会一个都不亮，
  用户面对全灰的图不知道能干什么。种子点是任何 A 股定期报告必有的 5 个科目。
- **亮度分档 v0.5**（`node_brightness_tier`）：v0.4 按比例切档有两个致命缺陷——
  ① **跳变**：点亮 A 导致无关节点 B 掉档，而用户对 B 什么都没做；② **虚假差异**：
  衰退按 wall clock，一周后所有值都趋近 0，但**排名永远存在**，必然有 25% 被标成
  "最近常看"。根因是相对分档没有绝对锚点，改为**绝对数量锚定 + 绝对门限前置**：
  `max(brightness) < 0.15` 时不分档全 dim；分档只按 `last_seen` 排序（不按 brightness
  排名），总数增长时只有真正的"最近 3 / 最久 3"会变，其余永不跳档。
- **状态新增 `node_last_seen` / `session_edges`**（`state.py`）：heat 是累加量（只增），
  表达不了"最近一次是什么时候"，分档必须有独立的时间维度。会话边按无向键去重，上限 5
  （20 会让图密到失去前沿）。
- **会话边 `graph/nodes/link.py`（新建）**：模式一口述两端 / 模式二从候选选，
  **必须显式确认**——连边会改变整张图的拓扑（进而改变别人的可点亮集合），一次误点就
  默默改图的话，用户根本无从发现、也无从撤销；提交时用 `candidates_token` 重新判定
  （候选可能很久以前下发的，拓扑已变）。禁止连到不可用节点：连了也点不亮，等能力就绪
  后批量生效，用户早忘了自己连过，突然看到它亮了还挂着一条边，无从解释。
- **`GET /starmap`**（`api.py` + `query_graph.starmap_state`）：只读会话快照，不跑图、
  不检索、不生成。存在的理由是首屏——否则要"先问一句才看得到星图"，而星图本该是入口。
- **Streamlit 前端接通全部新字段**：不可点亮节点禁用并灰显（＋可点亮 / 🔒 未解锁 /
  ●◐○ 三档亮度 / ◉ 当前）；锁定卡片给 `next_hop` 与 `alternatives` 两个出口；
  连边三段交互（候选 → 确认 → 完成）；reset 拆两档；会话边可见（n/5）。

### 修复

- **`link_commit` 根本没接进图**：`route_after_rewrite` 会返回 `"link_commit"`，
  但 `add_conditional_edges` 的 ends 映射漏了这个键——LangGraph 是 `ends[返回值]`
  直接查表，漏键要到运行到那条分支才 KeyError，静态检查和 import 都不报错。
  连边功能此前 100% 崩。
- **冷启动连边完全失效**：`compute_clickable` 在 unlocked 为空时直接返回种子点，
  **忽略会话边**——用户连了 `revenue ↔ eps`，eps 看得见却点不动、还被告知"还没解锁"。
  现在边的端点一并纳入可点亮。
- **冷启动锁定文案没有出口**：没有已点亮节点做锚点时 `next_hop` 算不出来，文案退化成
  "「净资产收益率」还没解锁。"，说了等于没说。改为退到 `alternatives` 的第一个。
- **首屏全灰**：`star={}` 时 `clickable` 为空 → 所有节点渲染成禁用态。改为首屏调
  `starmap_state()` 读只读快照。
- **Streamlit 列嵌套两层**：两档 reset 按钮在 `head[1]` 里又开 `st.columns(2)`，
  抛 `Columns can only be placed inside other columns up to one level of nesting`。
  改为单级三列。

### 变更

- **reset 拆两档**：`reset_starmap`（只清进度，保留新节点与连线）/
  `reset_starmap_all`（全清，需二次确认）。原子清除，绝不出现"清了进度却留着边"的
  半清状态——那会让星图自相矛盾：边的语义是"用户连的"，用户却看不到自己连过什么。

### 验证

- 回归 `128 passed, 1 skipped`。
- 端到端（走不触发 LLM 的两条路径）：冷启动点 `roe` → `locked=True`、
  `alternatives=['revenue','net_profit']`、文案「还没解锁，先看看「营业收入」」；
  连边三段 `link_candidates` → `link_confirm_card` → `link_done`，建边后 `eps`
  进入 `clickable`（修复前为 False）。
- 星图界面用 `AppTest` 冒烟（真跑脚本 + 模拟点击，不是看 HTML）：首屏 34 个按钮无异常，
  5 个种子点渲染为 `＋`、其余 22 个为 `🔒`、两个 reset 按钮在位；点击「营业收入」后
  节点变 `◉`、可点亮从 12 → 15（前沿推进）。
- 踩坑两条：`apply_pulse` 用 `if not t_recommend` 判断会把合法时间戳 `0.0` 当成
  "未提供"（改 `is None`）；连边轮不下发 `tiers`，前端按**键**合并而非整体覆盖，
  否则建边后所有已点亮节点瞬间失档（档位变空 → 全掉回默认，像系统故障）。

## [0.4.4] - 2026-09-24

星图**字典扩容 + 期间对比**：节点从 9 个扩到 27 个财报科目，节点支持两期对比；
`explanation` 放宽到 350 字。扩容过程中暴露并修掉三个"节点级全绿、结果却是错的"缺陷。

### 新增

- **星图字典 9 → 27**（`ui.py :: STAR_NODES`）：补上利润表（营业利润、每股收益、期间费用、
  研发投入、非经常性损益）、现金流（资本开支）、资产负债（总资产、商誉、应收账款、存货、
  货币资金）、结构治理（分红方案、股东情况、员工情况）、定性（行业格局、核心竞争力、
  审计意见、关联交易）。入字典标准是**任意 A 股定期报告必有的科目**——只部分公司披露的
  不放，否则点了就是空卡片。
- **节点热度 `node_heat`**（`state.py :: merge_heat`）：直接点击 +1.0，被别节点推荐 +0.5。
  与 `unlocked_nodes` 分工：点亮只由直接点击产生（否则会出现"没读过却已亮"的节点，
  点进去没内容），热度只驱动亮度。热度**不随公司/期间切换重置**，仅 `reset_starmap` 清空。
- **期间对比**：`ui_filters` 支持 `report_periods`（两期，走硬过滤）与 `compare`
  （要对比但不指定期时，自动给"最新 + 去年同期"，见 `knowledge.py :: default_periods`）。
  用户可选任意两期，不局限于相邻两年。
- **引用按期间分组** `citations_by_period`：对比时给出每期各自的页码，
  配合 `scope.company` 渲染成「公司 - 期间 - 页码」。
- **受控扩充：模型可提议新节点**（`retrieval/probe.py`）：字典只收"财报必有"的科目，
  覆盖不了所有报告内容；但放开让模型造词会造出「市盈率」这类不披露的科目——
  点了是空卡片，且在链路里表现为"转兜底"，trace 上每个节点都 `ok=true`，
  排查时看不出是节点造错了。现在模型通过 `new_nodes` 提议（id + 中文名 + 1-3 个
  关键词），系统先跑 BM25 探针验证可召回性，通过才写进会话级 `session_nodes`，
  之后与字典节点同构（可点亮 / 可加热度 / 用它自己的 keywords 检索）。
  **不写进全局字典**——那需要人工确认，且一次抖动无法回滚。
- **Streamlit 知识星图**（`apps/streamlit_app.py` + `ui.STAR_GROUPS`）：此前界面只有
  "问答 + 证据 + trace"，星图后端能力（`ui_payload` / `unlock_next` / `citations_by_period` /
  `new_nodes`）在前端完全没有出口。现在左栏按分组渲染 27 个字典节点
  （○ 未点亮 / ⭐ 推荐 / ● 已点亮 / ◉ 当前），右栏给出节点卡片与按期间分组的页码，
  会话新节点单独成组「本会话新发现」，侧栏可先锁定公司/期间（否则每点一个节点都会被
  反问"你想看哪家公司"）。字典里只有注释分组、不可机读，因此新增 `STAR_GROUPS`
  并由单测断言它覆盖全部字典 id（漏加只会导致前端少渲染、不报错）。

### 修复

- **`_period_cn` 的年度期根本没生效**：尾缀用 `p[-2:]` 取，对 `"2025A"` 得到 `"5A"`，
  匹配不上任何字典项，于是 `"2025A"` 原样进检索词，中文年份表述全部丢失
  （对 `"2025H1"` 才碰巧正确）。改成 `p[4:]`。这正是 0.4.3 新增的"期间默认最新一期"
  每次都踩的坑——那次改动之后，所有星图点击的检索词里都没有中文年份。
- **BM25 对同一 query 全量算两遍**：`bm25_recall` 与 `metadata_recall` 是并行两路，
  却各自对 56755 个 chunk 做一次完整打分（实测 1.0–1.6s/次）；两路抢 GIL 实际串行累加，
  双双超过 3.0s 阈值被判 `E_TIMEOUT` → 召回塌成一路 → `E_LOW_SCORE` 转兜底。
  加分数缓存（`BM25Index._scores`，上限 8 条约 14MB），阈值 3.0 → 5.0。
- **检索词硬拼「明细 构成」稀释相关性**：原先所有 `prefer_table` 节点都拼这两个词，
  但总资产 / 资产负债率 / 商誉这类单一数值科目根本没有构成表，硬拼只拉低 IDF 覆盖率
  （实测总资产 0.28 → 0.26，直接被闸门拦掉）。改为只有 `TABLE` 意图才拼。

### 变更

- **`explanation` 上限 100 → 350 字**（`schema.py` 硬截断 + 提示词 + `UI_EXPLANATION_MAX`）：
  两期对比要放得下"两期金额 + 同比变化 + 引用"（约 150–200 字），
  100 字会把第二期的数字直接切掉，等于白算。单期仍建议 80–120 字。

### 验证

- 27 个节点逐个 `click_star` 跑闸门：**27/27 通过**（修改前 12/27）。
  典型提升：`revenue` 0.524 → 0.686、`total_assets` 0.263 → 0.352、`goodwill` 0.231 → 0.312。
- 受控扩充端到端（注入会提议新节点的假 LLM）：「存货周转率」通过（命中率 1.0）、
  「市盈率」被拒；跨轮点击新节点时中文名正确、检索词用的是它的 keywords 而非 id、
  召回 6 条无兜底、点亮与热度均正常。
- 回归 `106 passed, 1 skipped`。
- 星图界面用 Streamlit 自带的 `AppTest` 冒烟（真跑脚本 + 模拟点击，不是看 HTML）：
  初始渲染 33 个按钮 / 5 个分组无异常；点击「营业收入」-> `unlocked=['revenue']`、
  卡片 scope=五粮液 2023A、重置后清空；注入提议新节点的假 LLM 后，「存货周转率」
  出现在「本会话新发现」组而「市盈率」未出现（探针已拦）。
- 踩坑两条：`render_evidence` 自带 expander 不可再嵌套（运行时才抛错）；
  不加 `st.rerun()` 时"已点亮"标记滞后一帧，看起来像点了没反应。
- 回归 `107 passed, 1 skipped`。

### 已知未修

- 对比模式下 `context_compress` 按期间交替取片段，若某一期本身没披露该科目，
  该期桶为空 → 卡片只能给出单期数据（模型应说明另一期未披露，待真实 LLM 验证）。

## [0.4.3] - 2026-09-24

星图卡片的**作用域**修复：真实 LLM 下取回的卡片文案全是"XX 是指……"的教科书定义，
对标的公司一无所知；追溯发现根因是星图点击缺少公司维度。按 ADR-0005 同批次更新文档。

### 修复

- **星图点击强制公司维度**（`rewrite.py :: _resolve_scope`，规则见 `nodes.md` 2.3）。
  前端只传 `node_id` 时三路召回没有任何硬过滤，向量路 `where=None` 按语义召回各家公司
  同一科目的片段——实测点「风险因素」一次混进隆基绿能 / 药明康德 / 中国平安 / 伊利 4 家。
  优先级：显式 `company` > 会话 `session_company` > 库内唯一一家 > **拒答**。
  多家公司且会话未锁定时注入 `OOS_STOCK_CODE` 把三路打空，回「请先选择公司」卡片并列出
  可选项——宁可拒答，也不拿某家的资料冒充答案。
- **会话作用域跨轮继承**：state 新增 `session_company`，自然语言回合抽到 `stock_code`
  时写入，后续星图点击继承（先问"茅台营收"再点节点，主体仍是茅台）。
  库外哨兵**不写**，否则一次库外点击会把会话锁死在 `__not_in_corpus__`。
- **期间只做软偏好，不硬过滤**：`prefer_period` 只进检索词。实测硬锁定期会把召回打空
  （茅台 2025A 的"毛利率"只召回 6 条且 IDF 相关性不过线 → 转兜底）。
  用户显式指定的 `report_period` 仍是硬过滤（库外期间照旧转兜底）。
- **两级期间聚焦**（`rerank` 稳定排序提前 + `context_compress` 同期片段 ≥2 条时只留这一期）：
  不聚焦时模型把这一期的金额安到另一期的口径上，数值闸门判 `E_NUM_MISMATCH`
  → 整张卡片转兜底（实测 `business_mix` 即如此）。
- **卡片必须落地到具体主体**（`prompts/ui_generator.py` 铁律 4）：
  禁止只写概念定义，必须给出该主体的金额 / 比率 / 构成项，并把「讲解主体」点名进提示词。
- **引用带文档坐标**：`ui_payload` 新增 `scope`（`stock_code` + `company` + `report_period`），
  由 `ui.scope_of_chunks` 从实际喂给模型的 context 反推。页码只在「公司 + 期间」下才唯一
  （同一家有 6 期报告，`P54` 每期都存在），前端据此渲染「贵州茅台 2025年年度 P54」。

### 文档（ADR-0005）

- `nodes.md` 新增 **2.3 星图作用域：公司必填与期间聚焦**（问题 / 规则表 / 四条边界）；
  `query_rewrite`、`rerank`、`context_compress` 三行同步指向。
- `rest_api.md` 补「知识星图（click_star）」小节：`company` 必填说明、
  `ui_payload` 与 `scope` 示例；`fallback_reason` 增加 `need_company`。

### 验证

- `98 passed, 1 skipped`（新增 `tests/test_starmap_scope.py` 4 例，mock 环境即可跑：
  作用域解析、硬过滤、兜底话术都不经过 LLM）。
- 真实 LLM 复验（茅台 600519）：`revenue` 由"营业收入是指……"变为
  「贵州茅台2025年度营业收入为人民币16,883,810.25万元 [P54]」；
  `risk` 由通用定义变为「宏观经济风险、安全风险、舆情风险和环境保护风险」。
  context 已全部收敛到单一期（此前 6 期混合）。
- `test_starmap_cases.py` 的 Case 全部补 `company`（不锁定就拿不到真实卡片），
  并新增 `C21-未选公司(裸点)` 覆盖降级路径。

### 已知问题

- `business_mix` 仍转兜底（数值未通过原文一致性校验）：单期聚焦后模型给出的构成数值
  在原文中无法逐字命中，属**闸门正常拦截**（宁可不说，也不错说），
  不是本次改动引入的回归——此前它"成功"只是因为输出通用定义、不含数值。

## [0.4.2] - 2026-09-24

修复 [0.4.1] 记录的两个语义层问题（均为 20 Case 跑批暴露、节点级单测无法发现），
按 ADR-0005 同批次更新文档。

### 修复

- **库外标的不再冒充答案**（合规）。`company="特斯拉"` / `"苹果公司"` 无法归一出
  `stock_code` → `filters` 为空 → 向量路 `where=None`，必然按语义召回库内其它公司的片段，
  卡片拿"营业收入的通用定义"冒充成特斯拉的答案且 `refused=false`。
  现由 `rewrite.py :: _resolve_company` 做两级处理：库内标的用 `known_meta()` 反查补
  `stock_code`（顺带修好中文名「贵州茅台」此前也无法硬过滤、会跨公司串味的问题）；
  库外标的注入 `OOS_STOCK_CODE`（`__not_in_corpus__`）让三路硬过滤必然落空 → 转兜底。
  两个坑：①`clean_ui_filters` 对 `stock_code` 的 `_norm_code` 校验必须**显式放行**哨兵，
  否则哨兵被当非法值丢弃、退回无过滤（第一次修复即因此失效）；
  ②解析结果必须**写回 `state["ui_filters"]`**，否则 `intent_router._star_patch`
  从 state 读到未解析版本会覆盖掉 `stock_code`。
- **库内标的不再被"无引用"误杀**（惜答）。模型给了数值却漏标页码时（实测 `glm-4-flash`
  偶发），用真实喂给它的 context 页码补引，并让来源串**计入 100 字预算**
  （否则超限时 clip 掉的正是来源串，等于白补）。
- **`[P12|章节路径]` 归一为 `[P12]`**：模型常把 context 的片段前缀整段抄进 explanation，
  既让卡片显示 `|审计意见 >` 这类脏后缀，又因抽取正则只认 `[P12]` 而漏掉引用。
  只处理闭合的 `]`——不闭合的多半已被截断，宁可留着也不误删正文。

### 文档（ADR-0005）

- `nodes.md` 2.1 增「五、上游归一」；新增 **2.2 库外标的的过滤与拒答**（问题 / 规则表 / 三条边界）；
  `query_rewrite`、`generator` 两行同步指向。

### 验证

- 跑批 20/20 通过（真实模型，224s）：`C16(特斯拉)` / `C17(苹果)` / `C20(0700)` 由
  `refused=false` 转为 `refused=true` + 兜底话术；`A10(茅台)` / `B15(平安)` 由
  `valid=false` 的兜底文案恢复为带引用的真实数据（`17,089,915.23 万元`）。
- `94 passed, 1 skipped`，无回归。
- `B11(宁德)` / `B13(招行)` 仍 `refused=true`，但兜底原因分别是
  「数值未通过原文一致性校验」与「检索置信度偏低」——属闸门**正常拦截**，不是缺陷。

## [0.4.1] - 2026-09-24

首次接入真实 LLM（`LLM_PROVIDER=openai_compat`，智谱 `glm-4-flash` / OpenAI 兼容协议）
后暴露的缺陷修复与配置链路补齐。按 ADR-0005 同批次更新文档。

### 修复

- **放宽 `extract_citations` 的引用抽取口径**（核心质量控制节点的行为变更，规则见
  `docs/design/nodes.md` 2.1）：除 `[P12]` 角标外兼容「来源：P10、P12」写法，
  但要求带 `来源/出处/参见` + 冒号前缀，裸 `P数字` 不认（防 `P2P` / `PEG` 噪声误抽）。
  真实模型不遵守角标契约（实测输出 `（来源:P10、P12、P15）`），而 `sanitize_answer`
  自身补引生成的也是该格式——只认角标会把两者都判成"答案含数字但无任何可溯源引用"，
  星图卡片被 `output_guard` 整段降级为"未能从报告中获得可溯源的数值"，
  且 trace 上每个节点都 `ok=true`、节点级单测无法发现。
  **白名单判定与数值闸门严格性未下降**：抽到的页码仍须 `∈ context`，编造页码照旧剥离。
- **`.env` 此前根本不生效**：`config.py` 注释声明"通过 python-dotenv 加载"，但代码里
  从未调用 `load_dotenv`。现补 `_load_dotenv()`（`override=False`：命令行注入优先于 `.env`）；
  `python-dotenv` 缺失时静默跳过，仍可用真实环境变量。
- **`LLM_JSON_MODE` 静默失效**：`.env.example` 写 `json_object | off`，而 `config.py`
  按布尔解析（`_b`），填 `json_object` 会被判为 **False**——等于关掉结构化输出却不报错。
  `.env` 已改为 `1`。

### 新增

- `.gitignore`（此前仓库没有）：`.env` / `__pycache__` / `.DS_Store` / `index/models/`。
  首次写入 API Key 前必须存在，否则 Key 会被提交。
- `tests/conftest.py`：默认 `LLM_PROVIDER=mock`（`setdefault`，显式设置仍可覆盖）。
  配了真实 Key 后 pytest 会继承它去真实调用远端模型——既烧额度又让断言随模型输出漂移。
- `tests/test_starmap_cases.py`：20 个典型 Case 的契约跑批（10 星图点击 / 5 跨文档对比 /
  5 信息缺失诱导），只做两条代码级硬校验——`UiAnswerResult` 能否被 Pydantic 加载、
  `unlock_next` 是否 100% 落在 `STAR_NODES`。不做语义质量评估（那是 `eval/` 的职责）。

### 文档（ADR-0005）

- `docs/design/nodes.md`：新增「2.1 引用抽取口径」——抽取规则表、白名单判定、
  放宽动因、三条边界不变量；`citation_validate` 行同步指向该小节。
- `docs/design/error_handling.md`：`E_CITATION_MISS` 补充"答案含数字却抽不出任何引用"
  这一触发条件与转兜底路径。

### 验证

- 真实模型端到端：普通问答 `营业收入为 4,052,850.98 万元 [P49]`；
  星图点击 `valid=true` + `citations=['P10','P12','P15']`（修复前 `valid=false` + 兜底文案）。
- `94 passed`（新增 conftest 后复跑，无回归）。
- 星图契约跑批 20/20 通过（真实 `glm-4-flash`，264s）：结构完整性与词典合规两条底线全数成立，
  `C19-字典外节点(pe_ratio)` 被正确拦为 `degraded=ui:unknown_star_node` 且 `unlock_next` 为空
  （幽灵节点防线有效）。

### 已知问题（跑批暴露，未修，不在本轮断言范围内）

`test_starmap_cases.py` 按约定只校验契约底线，但跑批输出暴露两个**语义层**问题，
留待 P1 评测量化后统一处理（两条都靠节点级单测发现不了）：

1. **库内标的被闸门误杀（惜答）**：`A10-revenue@茅台(600519)` 召回正常
   （context 6 段、trace 全 `ok=true`、无 `degraded`），却 `valid=false` → 卡片被
   `output_guard` 改写成"未能从报告中获得可溯源的数值"。同类信号见于
   `B11(宁德)` / `B13(招行)` / `B15(平安)`，均 `refused=true`。
   根因：模型在这类 Prompt 下未输出任何可识别引用（既无 `[Pxx]` 也无「来源：Pxx」），
   命中 `E_CITATION_MISS` 的"答案含数字但无引用"分支。放宽抽取口径只覆盖了最常见的一种变体。
2. **库外标的未拒答**：`C16(特斯拉)` `valid=true` / `refused=false`，
   卡片文本是"营业收入"的**通用概念定义**，并非特斯拉数据。
   根因：`company="特斯拉"` 中无 4-6 位数字，`clean_ui_filters` 归一不出 `stock_code`
   → **不产生硬过滤**，向量路按语义召回库内其它公司的片段（P10/P12/P15）→ 召回不空 → 不转兜底
   → 闸门只判"数字能否溯源"，通用定义不含数字故直接放行。

两者同源：闸门判定的是"数字能否溯源"，而不是"召回片段是否属于目标标的"。
候选方向（待决策）：把目标标的一致性纳入闸门；或让无法归一出 `stock_code` 的 `company`
走"软过滤 + 强制 `refused`"路径，避免把通用定义冒充成某家公司的答案。

## [0.4.0] - 2026-09-21

在线问答主图从"纯文本 RAG"改造为**事件驱动 + 富交互（知识星图与渐进式披露）**架构，
底层三路召回与容错护栏（gate / validate / guard）保持不变。

### 新增

- **UI 事件契约层** `src/vc/ui.py`：前端事件与图之间的唯一翻译层（纯函数、无 IO）。
  `clean_ui_filters` 白名单清洗（脏 key 一律丢弃、公司字段归一成 `stock_code`）；
  `STAR_NODES` 维护 9 个星图节点的 label / keywords / intent / 推荐后继，改 UI 不改图。
- **状态总线扩充**：`ui_action` / `ui_filters` / `star_node_id` / `filters_strict` /
  `ui_payload` / `unlocked_nodes`（`merge_unique` 去重累加，靠 Checkpointer 跨轮持久化）。
- **意图路由短路**：`ui_action ∈ {click_star, clarify_followup}` 时 `intent_router` 直接产出确定性
  意图与检索词（节点 id 即意图），**不触碰 LLM**；`ui_filters` 注入为绝对过滤（`filters_strict=True`，
  召回为空也不回退）。`query_rewrite` 同步短路，因此空 `query_raw` 也能激活 Send 并行召回
  ——这是 clarify 卡片点按钮后的第二轮 invoke 路径。
- **结构化 UI 指令**：新增 `schema.py::UiAnswerResult` 与 `prompts/ui_generator.py`，
  generator 在 UI 分支强制输出 `{explanation, citations, unlock_next}`：
  explanation ≤100 字（Schema 期硬截断，不整条丢弃）、citations 归一为 `["P12"]`、
  unlock_next 限 2 且必须落在星图白名单（防"点不动的幽灵节点"）。
- **多轮记忆**：`compile(checkpointer=...)`，`MemorySaver` 默认、`VC_CHECKPOINT=sqlite` 走 `SqliteSaver`；
  `ask(..., thread_id=...)` 按会话隔离，`unlocked_nodes` 跨 invoke 累加，不同 thread 互不影响。
  新增 `unlock_commit` 节点（挂在 `citation_validate` 之后：引用不通过不点亮）。
- **回合窗口**：Checkpointer 会让 `errors`/`trace` 跨轮无限累积，`query_rewrite` 每轮返回
  `RESET_LIST` 哨兵，由 reducer 过滤实现"本轮重新计数"（累加语义不变，窗口收敛到当前轮次）。
- **清理**：新增 `tests/test_ui_graph.py`（14 例）；临时冒烟脚本已删除。

### 变更

- `output_guard`：UI 事件下把合规改写同步回 `ui_payload`——卡片渲染的是 `explanation` 而非
  `final_answer`，此前正文降级（无源数字转摘录）时卡片仍展示未过闸门的原文；现同步降级并置 `refused=true`，
  引用统一用校验通过的页码覆盖，免责声明以独立 `disclaimer` 字段下发（不占 100 字预算）。
- `clarify_node`：除文本追问外产出 `ui_payload.options` 结构化按钮（`node_id` + 继承的公司/期间约束）。
- `fallback_node`：UI 事件下额外产出退化 `ui_payload`，前端不空屏。
- `bm25_recall`：`filters_strict=True` 时不再"过滤后为空则退回不过滤"。
- `/ask`：请求新增 `ui_action` / `ui_filters` / `thread_id`（`question` 放宽为可空），
  响应新增 `ui_payload` 与 `unlocked_nodes`。

### 修复

端到端路试（整图 invoke，`tests/test_e2e_starmap.py`）暴露的缺陷，节点级单测全部无法发现：

- **`bm25_recall` 严格过滤未真的置空**：`filters_strict=True` 且过滤后为空时沿用"退回不过滤"，
  导致点「腾讯(0700)毛利率」返回五粮液的报表原文。现严格模式下过滤为空即返回空并转兜底。
- **自然语言回合未清 UI 残留**：`ui_payload` / `star_node_id` / `filters_strict` 都是覆盖写字段，
  Checkpointer 恢复上一轮值后造成三种串味——卡片显示"上轮节点 + 本轮引用"、
  自然语言提问误点亮上轮点过的星图节点、严格过滤泄漏到自然语言回合。
  现由 `query_rewrite`（每轮第一个节点）统一重置。
- **`text_utils.clip` 超长一个字符**：截断后补省略号使实际长度 = limit+1，
  星图卡片 100 字上限被突破。现省略号计入额度，保证返回值 ≤ limit。
- **`output_guard` 污染 reset 卡片**：reset 不进检索，却被上一轮残留的 `citations` 覆盖，
  出现"重置卡片挂着旧页码"。现 reset 不参与引用同步。
- **空 `query_raw` 分支漏带 `RESET_LIST`**：`reset_starmap` 会带着上一轮检索的 trace/errors 返回。

### 测试

- 新增 `tests/test_e2e_starmap.py`：整图 invoke 路试（非节点级），覆盖主链路 13 个节点、
  跨轮 `unlocked_nodes` 累加、UI 残留清理、reset、库外标的转兜底不点亮。
  依赖已构建的索引，无语料环境自动 skip。

### 兼容性

- 自然语言链路零改动：`ask(question)` 行为与 0.3.1 一致（默认 `ui_action=natural_query`）。
- 未注入 thread_id 时统一用 `"default"`，单用户场景无需适配；
  多用户接入**必须**传 thread_id，否则星图进度串台。

## [0.3.1] - 2026-09-21

清洗层补「符号字体归一」，并按 ADR-0005 同批次更新文档后做了一次全量重建。

### 新增

- **符号字体归一** `cleaner.normalize_icons`：财报用 Wingdings 类图标字体画勾选框与项目符号，
  PDF 抽取后落在 Unicode 私用区。全语料实测命中 6 个码位：
  `U+F052`(2590) / `U+F0FE`(17) / `U+F0A3`(10) 为已勾选方框，`U+F0B7`(334) / `U+F06E`(36) /
  `U+F06C`(28) 为项目符号，分别归一为 `☑` 与 `·`；未识别的私用区字符换成空格后由后续空白压缩收掉。
  私用区字符进不了汉字 unigram/bigram，却会占 BM25 词频，属于纯噪声。
  归一在 `normalize_text` **之前**执行，并在学页眉页脚之前统一施加到 pages 上——
  否则"学到的 boiler 行 vs 清洗后的行"会因符号字体对不上而漏删页眉。
- **单测** `tests/test_ingest.py::test_clean_normalizes_icon_fonts`：勾选框/项目符号归一 + 私用区零残留。

### 重建与校验

- `scripts/ingest_corpus.py --dir data/reports --force` + `scripts/ingest.py --force`（示例文档）：
  60 份财报 + 示例文档全部 `full` 重建，chunk 总数 56755。
- 落盘校验七项全 ✔（manifest/chroma/bm25/快照 四个 chunk 数 + provider/dim + bm25 文件）。
- 索引扫描：私用区字符残留 **0**；含 `☑` 的 chunk 1028、含 `·` 的 174（语义保留）。
- 金标 22 题复测：命中率 100%、幻觉率 0%、引用率 81.8%。

### 已知问题（未修，非本次引入）

- 冷启动后的前 1–3 个查询，`bm25_recall` / `metadata_recall` 偶发超过 `timeout_bm25=3s`
  （实测 BM25 单次搜索仅 0.23–0.56s，超时来自进程刚起来时 BGE 推理与三路并行召回抢 GIL）。
  若失败次数在 TTL 内达到 `circuit_threshold`，熔断会打开并影响后续若干查询
  （观测到一次命中率 90.9%，两次复跑均为 100%）。后续可考虑：预热里加一次探针搜索，
  或让熔断只在**非超时**错误上计数。

## [0.3.0] - 2026-09-20

按 ADR-0005，本条目与代码、文档同批次更新。语料从单文档扩到 61 份 A 股定期报告。

### 新增

- **财报爬虫** `scripts/crawl_reports.py`：巨潮资讯网（沪深法定披露平台）按
  「股票 orgId + 年报/半年报类别 + 时间段」精确查询，标题过滤（摘要/英文版/更新前…）、
  文本型 PDF 校验（pypdf 抽样抽字）、限速重试、断点续传；产物 `data/reports/{code}_{period}.pdf`。
  orgId **无法由代码推导**（实测混有 `gssh0600519` / `9900002221` / `gshk0001211` / `GD165627`），
  改为下载官方全量股票表解析并缓存。
- **网页侧元数据 sidecar** `src/vc/ingestion/sidecar.py`：每份 PDF 配同名 `.meta.json`
  （简称/代码/报告期/行业/公告标题/发布日期/源 URL），字段白名单 + 去 `<em>` 高亮标签；
  `enrich_metadata` 改为 **sidecar 优先、PDF 首页正则兜底**，缺失打 `degraded:meta_no_sidecar`。
- **行业过滤维度**：`industry / short_name / source_url` 贯穿
  `Chunk → splitter → _META_KEYS → filters → manifest`，`known_meta()` 暴露全部文档的公司与行业。
- **批量入库** `scripts/ingest_corpus.py`：逐文档跑入库子图并传 `skip_bm25=True`，
  末尾 `rebuild_bm25()` 统一重建（避免 60 份串行 O(n²) 快照 IO）；提供 `--rebuild-bm25-only` 补救入口。
- **冷启动预热** `src/vc/retrieval/warmup.py`：`ask()` 入口调一次 `warmup_index()`，
  把 5.7 万条倒排（≈7s）与 embedding/向量库初始化（≈3s）挪出节点计时。
- **过滤优先检索** `BM25Index.search_where`：元数据路先圈定范围再排序。
- **单测** `tests/test_corpus.py`（16 项）：sidecar 合并优先级、期间/行业/公司抽取、
  Chroma `$and`、过滤优先、标题过滤与域名白名单。

### 修改

- `timeout_bm25` 1.0→3.0、`timeout_vector` 3.0→5.0（按热态实测重标，见 `docs/dev/tuning.md` 第七节）。
- `report_title()` 多文档下不再输出数字（"11 家公司 / 61 份报告"会拼进兜底话术，
  被数值闸门判成假阳性幻觉，实测把 22 题幻觉率从 0% 拉到 9.1%）。

### 修复

- **Chroma 多条件 `where` 抛错**：隐式多键 AND 不被接受，改为显式 `{"$and": [...]}`。
- **期间过滤静默失效**：正则 `(20\d{2})…年报|年度报告` 的 `|` 优先级导致「2024年年度报告」
  拼出 `NoneA`，库里永不匹配；改为强制捕获年份，没年份不过滤。
- **跨公司串味**：BM25 路不做元数据过滤时，「宁德时代净利润」会被隆基绿能原文顶掉；
  现扩大候选池并做软过滤（过滤为空则退回不过滤）。

### 数据

- `data/reports/`：10 家 A 股（白酒/银行/保险/新能源/光伏/电子/食品/汽车/医药）×
  2023-2025 年报 + 半年报 = **60 份 PDF + 60 份 sidecar**（297MB，行业经 NeoData 申万分类核对）。
- 入库后语料：61 份文档 / 56755 chunk，落盘四件套（manifest / chroma / bm25 / 快照）校验一致，
  二次批量入库 60/60 全 skip（幂等）。
- 金标 22 题复测：命中率 100%（↑）、幻觉率 0.0%（持平）、P95 1.397s（↑，见 README 基线）。

## [0.2.0] - 2026-09-20

按 ADR-0005，本条目与代码、文档同批次更新。

### 新增

- **真实本地向量**：`BGEEmbedding` 实体化 —— 批量编码（batch 32）、L2 归一化、设备自动选择
  （mps > cpu）、`dim` 从模型读取（512）、HF 优先 / ModelScope 兜底、`VC_MODEL_CACHE` 可指定缓存目录。
  `EMBEDDING_PROVIDER=auto`（默认）：装了 `sentence-transformers` 就自动用 BGE，装不上回落 Hashing。
- **真实重排**：`CrossEncoderReranker`（`BAAI/bge-reranker-base`，仅对 top-20 打分），
  `RERANK_PROVIDER=heuristic|bge|none`；加载/超时失败自动回落启发式并写 `degraded:rerank:fallback_heuristic`。
- **LLM 契约层**（`src/vc/schema.py` + `src/vc/prompts/`）：
  `IntentResult` / `FaithfulnessResult` / `AnswerResult` 三份 Pydantic v2 契约；
  `generate_json()` 走 `response_format=json_object` → 校验 → 修复重试一次 → 仍失败抛 `E_LLM_BADJSON`；
  router / gate / generator 三份系统 Prompt 与 few-shot。
- **`FakeLLM`**（`LLM_PROVIDER=fake`）：按节点 key 返回脚本化响应（合法 JSON / 缺字段 / 枚举非法 /
  markdown 围栏 / 拒答 / 超时），**没有 API Key 也能覆盖契约全分支**（`tests/test_llm_contract.py` 15 项）。
- **落盘校验** `src/vc/ingestion/verify.py`：`verify_index()` 七项检查
  （manifest / chroma / bm25 / 快照 的 chunk 数与 provider/dim 一致），`scripts/ingest.py` 入库后打印校验表。
- **评测层** `src/vc/eval/`：统一指标口径的 `runner`（hit/cite/gate/fabricate/fallback/honest/page/num + P95）、
  CFQA 适配（MIT，仅 JSON，no-corpus 模式评"是否诚实"）、幻觉注入探针（编造数字 / 伪造页码）。
- **参数扫描** `scripts/sweep.py` + `scripts/fetch_cfqa.py`：
  检索期网格（不重建索引）与切分期网格（`--restore-best` 用最优重建），结论写入 `docs/dev/tuning.md`。
- **UI 升级**（Streamlit）：暗色金融终端风格；答案内 `[Pxx]` 渲染为**金色可点击角标**（展开原文片段）；
  **trace 时间线**（节点耗时横条 + 成功/降级/失败着色 + 错误码）；证据抽屉展示 `routes / rank_by_route / rerank_score`；
  侧栏落盘校验状态与 Top-K / 阈值 / 重排档位控制台；降级提示。
- **API 升级**：新增 `GET /config`（providers / retrieval / contracts / chunking / index_verify，排障先看它）；
  `/stats` 增加 `rerank_provider` 与 `index_verify`；`/ask` 增加 `gate_pass` / `retry_count` / `route_cfg` / `generation_used`。
- **文档**：新增 `docs/dev/tuning.md`（调参记录与阈值权衡表）；
  `nodes.md` 增"四、LLM 契约层"；`retrieval.md` 增重排三档表与新基线；
  `routing.md` / `resilience.md` / `error_handling.md` / `architecture.md` / `setup.md` / `testing.md` /
  `api/rest_api.md` / `README.md` 同步更新。

### 变更

- Chroma 集合名与 bm25 文件带 `__provider_dim` 后缀（如 `__bge_512`），
  规避 provider/dim 变化后的维度不兼容；换模型由 `plan_diff` 识别为 `full` 全量重建。
- `intent_router`：规则优先，仅在低置信 / 边界（margin<0.15）时调 LLM；
  OOS / CHITCHAT 合规红线**不可被 LLM 推翻**；LLM 失败回落规则并写 `router:llm_fallback_rule`。
- `faithfulness_gate`：正则数值逐字比对 + 页码校验保留为**底线**，叠加 LLM 判定
  （抓"数字对但结论越界"），**取严不取宽**；LLM 不可用写 `gate:llm_skipped`，不通过 → `retry_shrink`。
- `generator`：走 `AnswerResult` 结构化输出并按 context 页码白名单剥离编造引用；
  失败回落抽取式并写 `generator:json_fallback_text`。
- 融合可解释性：`fusion_stats` 增加 `route_hits` / `multi_route_hits`，片段携带 `rank_by_route` / `rerank_score`。
- 默认参数定稿（见 `docs/dev/tuning.md`）：`chunk_size=800 / overlap=120`、`top_k_final=5`、
  `min_top1_relevance=0.28`（阈值再往上抬会更"惜答"，但会误杀合法问题）。
- 评测口径修正：兜底会清空 `context`，幻觉判定改为覆盖 `context or reranked or fused`；
  剔除摘录截断产生的半截数字与问题自带的年份，消除两类假阳性。

### 修复

- 切分期扫描后索引用最优参数（800/120）重建，避免残留次优 chunk。
- 移除一处导致金标回归的 `UNCLEAR` 低置信降级逻辑（现只按 margin 收敛到 GENERAL）。
- `scripts/fetch_cfqa.py` 输出到项目根 `data/cfqa`（此前误落到 `src/data`）。
- `_llm_judge` 不再吞掉 provider 构造异常，确保失败时正确写 `degraded`。

### 已知问题

- 答案仍由 MockLLM 抽取式生成；接真 LLM 后引用率 / 闸门通过率会变，需重跑三套评测并更新基线。
- CFQA 目前只用 no-corpus 模式（评"诚实"），`with-corpus` 模式的准确率未测。
- 金标仅 22 题，Top-K 维度分辨力不足（多组同分），只能排除明显更差的档位。
- `CrossEncoderReranker` 首次加载需下载 `bge-reranker-base`；未安装时自动回落启发式。
- Python 3.9 环境下 `streamlit` 需锁 `<1.40`；`torch==2.4.1` / `sentence-transformers==2.7.0` 为验证过的版本组合。

## [0.1.0] - 2026-09-18

### 新增

- **编排框架**：基于 LangGraph 的双图架构 —— 离线入库子图 + 在线问答主图（`src/vc/graph/`）。
- **v0 骨架**：保留 `src/vc/graph/skeleton_graph.py`（router/retriever/generator/guardrail）作为演进起点。
- **入库链路**：`purge_stale → load_pdf → clean_normalize → enrich_metadata → table_aware_split
  → dedup_hash → plan_diff → embed_upsert → build_bm25 → write_manifest`。
- **表格感知切分**：表格整块保留、正文 800/120 滑窗、残块合并、章节路径前缀。
- **知识更新**：manifest 索引 + `file_sha256`/`page_hash` 增量决策（skip/page/full）+ 原子替换 + 快照回滚。
- **三路召回**：BM25 / 向量（Chroma，降级 PickleStore）/ 元数据过滤，Send 并行 + 加权 RRF 融合。
- **意图路由**：8 类金融意图（METRIC/TABLE/COMPARE/CALC/QUALITATIVE/SUMMARY/CHITCHAT/OOS）+ 置信度降级。
- **防幻觉闸门**：`faithfulness_gate` 校验答案数值与引用页码必须可溯源，失败可收紧重试 1 次。
- **容错隔离**：`safe_node` 统一超时/重试/熔断/降级标记；错误只累加到 `errors`，不污染主字段。
- **兜底策略**：无命中 / 低分 / 意图不明 / 生成失败 / 数值不通过 五类场景话术。
- **可插拔 Provider**：`HashingEmbedding`/`BGEEmbedding`/`OpenAICompatEmbedding`、
  `MockLLM`/`OpenAICompatLLM`、`HeuristicReranker`，默认零依赖可跑。
- **交付形态**：CLI（`scripts/ingest.py`、`query.py`、`eval.py`）、FastAPI（`apps/api.py`）、
  Streamlit（`apps/streamlit_app.py`）。
- **相关性闸门**：融合后追加 IDF 加权相关性判定，拦住"知识库里根本没有相关词"与"覆盖率过低"两类硬答。
- **测试与评测**：28 条 pytest 单测 + 22 题金标集（命中率 95.5% / 引用率 90.9% / 数值一致性 90.9% / 兜底率 4.5%）。
- **文档资产**：架构图、节点规格、7 项方案细则、5 篇 ADR、开发/运维/接口文档。

### 已知问题

- 默认 Hashing Embedding 语义表达弱，定性类问题召回质量有限；换 BGE 后需重建索引并重测基线。
- 页级增量目前仍对整份文档重新编码，真正的"只编码变更页"优化尚未实现。
- Python 3.9 环境下 `streamlit` 需锁 `<1.40`。
