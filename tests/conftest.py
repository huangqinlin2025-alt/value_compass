"""测试基线：默认把 LLM 固定为离线 Mock。

为什么需要：.env 里配了真实 Key（LLM_PROVIDER=openai_compat）之后，
pytest 会继承它去真实调用远端模型——既烧额度，又让断言随模型输出漂移。
这里只做 setdefault，显式设置 LLM_PROVIDER 仍能覆盖（用于真机联调）。
"""
import os

os.environ.setdefault("LLM_PROVIDER", "mock")
