# 如何新增一个节点

## 1. 五步流程

```mermaid
flowchart LR
    A["1. 写节点函数"] --> B["2. 用 safe_node 包裹"]
    B --> C["3. 在 graph 中注册并连线"]
    C --> D["4. 补单测"]
    D --> E["5. 同步文档"]
```

## 2. 写节点函数

```python
# src/vc/graph/nodes/your_node.py
from typing import Any, Dict

from ...decorators import safe_node
from ...errors import ErrorCode, make_error


@safe_node(
    "your_node",                       # 节点名，必须与图中注册名一致
    timeout=2.0,                       # 超时预算
    retries=1,                         # 可重试错误的重试次数（默认取 CONFIG.max_retry）
    provider=None,                     # 若依赖外部 provider（llm/vectorstore/bm25），填名字以启用熔断
    fallback_patch={"your_output": []},# 失败时返回的"安全最小状态"
)
def your_node(state: Dict[str, Any]) -> Dict[str, Any]:
    data = state.get("fused") or []
    if not data:
        return {"your_output": []}
    ...
    return {"your_output": result}
```

## 3. 硬性约定

| 约定 | 原因 |
| --- | --- |
| 签名必须是 `(state) -> dict` | 与 LangGraph 节点契约一致 |
| 返回**增量 patch**，不 mutate 传入的 state | 避免并发分支（Send）互相污染 |
| 读取字段一律用 `state.get(k) or 默认值` | 上游可能降级为空 |
| 不写裸 `try/except`，交给 `safe_node` | 保证错误形状统一、trace 不丢 |
| 主动判定失败时用 `make_error(ErrorCode.X, "your_node", msg, fallback=...)` | 错误码收敛 |
| 主字段失败时置空，**不写答案类字段** | 错误不污染主字段 |
| 纯计算、无副作用 | 便于单测与重试 |

## 4. 在图中注册

```python
# src/vc/graph/query_graph.py
from .nodes.your_node import your_node

g.add_node("your_node", your_node)
g.add_edge("compress", "your_node")          # 或 add_conditional_edges
```

若需要条件分支，新增路由函数并**同步更新 `docs/design/nodes.md` 的条件边表**。

## 5. 补单测

在 `tests/test_graph.py` 中至少覆盖：

```python
def test_your_node_normal_path():
    assert your_node({"fused": [...]})["your_output"]

def test_your_node_failure_is_captured():
    out = your_node({})            # 空输入不应抛异常
    assert out["your_output"] == []
```

## 6. 同步文档（不做完不算完成）

- `docs/design/nodes.md`：在对应表格补一行（设计思路 / 功能 / 读 / 写 / 失败模式）；
- `docs/design/architecture.md`：若改变图结构，同步 Mermaid 与 ASCII 图；
- `CHANGELOG.md`：追加条目；
- 若引入新的外部依赖或架构取舍 → 新增 ADR（`docs/adrs/ADR-XXXX-xxx.md`）。
