"""v0 极简骨架 —— 架构演进的起点（保留用于教学与回归）。

它是整份架构图的"种子"：router -> (retriever|guardrail) -> generator。
后续所有能力（三路召回、融合重排、数值校验、容错兜底）都是在这 4 个节点上
逐步生长出来的，见 docs/design/architecture.md。

运行：python3 src/vc/graph/skeleton_graph.py
"""
from typing import TypedDict

from langgraph.graph import END, START, StateGraph


# 1. 定义全局状态 (State) - 这是 Agent 流转时的"记忆黑板"
class AgentState(TypedDict):
    user_query: str
    intent: str
    context: str
    final_answer: str


# 2. 定义节点 (Nodes) - 核心动作，目前用假逻辑模拟
def router_node(state: AgentState):
    print("🧭 [节点] Router: 正在分析意图...")
    query = state["user_query"]
    # 极简模拟模型 Function Calling：带有"买"或"推荐"字眼就判为越界
    if "买" in query or "推荐" in query:
        return {"intent": "out_of_bounds"}
    else:
        return {"intent": "financial_qa"}


def retrieval_node(state: AgentState):
    print("🔍 [节点] Retrieval: 正在沙盒中检索财报片段...")
    # 极简模拟向量检索
    return {"context": "【假装检索到的财报段落：2023年公司净利润增长20%】"}


def generator_node(state: AgentState):
    print("✍️ [节点] Generator: 正在生成强制溯源的答案...")
    # 极简模拟大模型回答
    return {"final_answer": "基于财报，公司净利润增长了20%。[引用：财报第X页]"}


def guardrail_node(state: AgentState):
    print("🛡️ [节点] Guardrail: 触发合规拦截！")
    return {"final_answer": "抱歉，作为价值投资助手，我不提供个股买卖推荐。我们可以聊聊如何看懂财报。"}


# 3. 定义条件路由逻辑 (Conditional Edge)
def route_intent(state: AgentState):
    if state["intent"] == "out_of_bounds":
        return "to_guardrail"
    return "to_retrieval"


# 4. 编排图网络 (Graph 连线)
workflow = StateGraph(AgentState)

# 注册节点
workflow.add_node("router", router_node)
workflow.add_node("retriever", retrieval_node)
workflow.add_node("generator", generator_node)
workflow.add_node("guardrail", guardrail_node)

# 编排边：起点 -> 路由
workflow.add_edge(START, "router")
# 编排边：路由 -> 条件分流
workflow.add_conditional_edges(
    "router",
    route_intent,
    {
        "to_guardrail": "guardrail",
        "to_retrieval": "retriever",
    },
)
# 编排边：检索 -> 生成 -> 终点
workflow.add_edge("retriever", "generator")
workflow.add_edge("generator", END)
# 编排边：合规拦截 -> 终点
workflow.add_edge("guardrail", END)

# 编译图
app = workflow.compile()


# ================= 5. 验证测试 =================
if __name__ == "__main__":
    print("\n=== 测试 1：正常提问（应该走检索和生成节点） ===")
    result1 = app.invoke({"user_query": "公司的净利润是多少？"})
    print(f"👉 最终输出: {result1['final_answer']}")

    print("\n=== 测试 2：越界提问（应该直接被 Guardrail 拦截） ===")
    result2 = app.invoke({"user_query": "这只股票明天能买吗？"})
    print(f"👉 最终输出: {result2['final_answer']}\n")
