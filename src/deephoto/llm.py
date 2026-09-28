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


def build_description_model(settings: Settings):
    """图片描述专用模型(OpenAI 兼容,如百炼 qwen3.8-omni-flash;与问答/嵌入配置解耦)。

    未启用(DEEPHOTO_DESCRIPTION_ENABLED 非 true)返回 None——这是明确的"已关闭"
    状态,与初始化失败严格区分:启用后配置缺项在 load_settings 启动校验时已报出;
    其余初始化异常直接上抛,由入库失败路径记录真实失败,不伪装成未配置。
    """
    if not settings.description_enabled:
        return None
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    kwargs = dict(
        model=settings.description_model,
        api_key=SecretStr(settings.description_api_key or ""),
        base_url=settings.description_base_url,
        timeout=settings.description_timeout_seconds,
        max_retries=settings.description_max_retries,
        max_tokens=settings.description_max_tokens,
        # 官方 HTTP 指南推荐流式;invoke 由 LangChain 聚合为完整响应后再解析一次 JSON,
        # 流式中断的半段 JSON 不会被当成成功
        streaming=True,
    )
    if settings.description_reasoning_effort:
        # qwen-omni 系列:Chat Completions 请求体顶层 reasoning_effort="none" 关闭思考;
        # 不混用其他 Qwen 型号的 enable_thinking / thinking_budget
        kwargs["reasoning_effort"] = settings.description_reasoning_effort
    return ChatOpenAI(**kwargs)


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
