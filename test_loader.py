# 首次运行前需在终端执行：pip install langchain-community pypdf
from langchain_community.document_loaders import PyPDFLoader

# 1. 指向沙盒里的唯一靶点
loader = PyPDFLoader("./data/sample_report.pdf")
pages = loader.load()

# 2. 打印基础信息，证明代码成功“摸”到了数据
print(f"🟢 成功加载！这份财报总共有 {len(pages)} 页。")
print("--------------------------------------------------")
print("第一页前 200 个字符预览：\n", pages[0].page_content[:200])
