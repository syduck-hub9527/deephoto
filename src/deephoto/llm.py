"""模型工厂:集中构造 Kimi K3 对话模型与可选嵌入模型。

- Kimi K3 走 OpenAI 兼容接口,原生多模态,图片只接受 base64 数据
  (不支持公网 URL),支持工具调用。
- 两条路由的模型 ID 不可混用:
  Kimi Code 会员  https://api.kimi.com/coding/v1   -> model="k3"(本项目默认)
  直连 Moonshot API https://api.moonshot.cn/v1     -> model="kimi-k3"
- K3 注意点:思考常开;多轮/工具循环需回传完整 assistant 消息
  (含 reasoning content)。若所用 langchain-openai 版本丢弃该字段,
  需在此集中更换适配器——只改本文件。
- 嵌入走任意 OpenAI 兼容端点(如阿里云百炼 DashScope
  https://dashscope.aliyuncs.com/compatible-mode/v1),未配置则退化为纯关键词检索。
"""

from __future__ import annotations

from .config import Settings


def build_chat_model(settings: Settings):
    """构造 Kimi K3 对话模型实例(懒导入,避免无依赖环境下影响纯逻辑模块)。"""
    if not settings.moonshot_api_key:
        raise RuntimeError("未配置 DEEPHOTO_MOONSHOT_API_KEY,无法调用 Kimi K3")
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    return ChatOpenAI(
        model=settings.chat_model,
        api_key=SecretStr(settings.moonshot_api_key),
        base_url=settings.moonshot_base_url,
        temperature=settings.chat_temperature,
        timeout=180,
        max_retries=2,
    )


def build_embeddings(settings: Settings):
    """可选嵌入模型(OpenAI 兼容端点)。未配置返回 None,检索退化为纯关键词。"""
    if not settings.embeddings_enabled:
        return None
    from langchain_openai import OpenAIEmbeddings
    from pydantic import SecretStr

    return OpenAIEmbeddings(
        model=settings.embedding_model, # type: ignore
        api_key=SecretStr(settings.embedding_api_key or "not-needed"),
        base_url=settings.embedding_base_url,
        # 默认 True 会先 tiktoken 切成 token ID 数组再请求;
        # 百炼等兼容端点只接受字符串,必须关掉
        check_embedding_ctx_length=False,
    )
