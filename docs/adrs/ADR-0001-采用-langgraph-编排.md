# ADR-0001：采用 LangGraph 作为编排框架

- 状态：accepted
- 日期：2026-09-18
- 相关：`docs/design/architecture.md`

## 背景

财报问答不是"一次检索 + 一次生成"，而是一条带**条件分流、并行扇出、失败回边**的链路：
意图不同走不同分支；三路召回要并行且互不拖累；数值校验失败要能回退重来。
用线性链式代码（if/else + 顺序调用）表达这些控制流，很快会变成难以维护的状态机泥团。

## 备选方案

| 方案 | 优点 | 缺点 |
| --- | --- | --- |
| 手写顺序流程 + if/else | 无新依赖，直白 | 分支多了不可读；并行与失败回边要自己造；状态流转隐式 |
| LangChain LCEL | 生态成熟 | 本质是 DAG，表达"条件回边/重试循环"很别扭 |
| **LangGraph** | 原生 State + Node + Conditional Edge + Send 并行；状态显式可序列化；便于可视化与测试 | 引入新依赖；需要约束节点签名 |
| 自研状态机 | 完全可控 | 重复造轮子，trace/checkpoint 都要自己实现 |

## 决策

采用 **LangGraph**，并附加三条工程约束：

1. 单一 `GraphState`（TypedDict），节点只返回增量 patch；
2. 所有节点统一用 `@safe_node` 包裹，异常在节点内收敛；
3. 横切字段 `errors / degraded / trace` 用 `add_list` reducer，保证只增不减。

## 后果

- 正面：控制流显式；`Send` 天然实现三路并行与隔离；回边（gate 失败重试）表达自然；
  状态是可序列化的 dict，便于 API 返回与前端展示。
- 负面：团队需要理解 State/Reducer/Conditional Edge 概念；节点拆分粒度需要规范约束
  （见 `docs/dev/how-to-add-node.md`）。
- 约束：Python 3.9 环境下注解必须用 `typing.List/Optional` 与 `typing_extensions.TypedDict`。
