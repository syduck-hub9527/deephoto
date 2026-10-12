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


def build_description_model(settings: Settings, *, max_tokens: int | None = None):
    """图片描述专用模型(OpenAI 兼容,如百炼 qwen3.8-omni-flash;与问答/嵌入配置解耦)。

    未启用(DEEPHOTO_DESCRIPTION_ENABLED 非 true)返回 None——这是明确的"已关闭"
    状态,与初始化失败严格区分:启用后配置缺项在 load_settings 启动校验时已报出;
    其余初始化异常直接上抛,由入库失败路径记录真实失败,不伪装成未配置。
    max_tokens:默认沿用 settings.description_max_tokens;parse_failed 重试按轮
    升级输出上限时显式传入(DEV_describe_parse_retry §4.4)。
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
        # 不用 max_tokens= 字段:langchain-openai 会把它改写成 max_completion_tokens
        # (已在 0.3.0 / 0.3.35 / 1.6.6 实测),第三方兼容端点未必接受。
        # 经 extra_body 直接发经典的顶层 max_tokens,各版本行为一致。
        extra_body={"max_tokens": (max_tokens if max_tokens is not None
                                   else settings.description_max_tokens)},
        # 官方 HTTP 指南推荐流式;invoke 由 LangChain 聚合为完整响应后再解析一次 JSON,
        # 流式中断的半段 JSON 不会被当成成功
        streaming=True,
    )
    if settings.description_reasoning_effort:
        # qwen-omni 系列:Chat Completions 请求体顶层 reasoning_effort="none" 关闭思考;
        # 不混用其他 Qwen 型号的 enable_thinking / thinking_budget
        kwargs["reasoning_effort"] = settings.description_reasoning_effort
    return ChatOpenAI(**kwargs)


class OrderedEmbeddings:
    """薄嵌入客户端(embedding-bge-m3-migration §5.1):按批调用 OpenAI 兼容 /embeddings。

    用 openai 原生客户端而不是 langchain OpenAIEmbeddings(§5.1 备选路径):langchain 1.7.0
    的 embed_documents 直接按 response["data"] 的返回顺序与文本配对,不按 index 排序,也没有
    可截获原始响应的扩展点(已在锁定版本源码确认,embeddings/base.py 的
    `embeddings.extend(r["embedding"] for r in response["data"])`);SiliconFlow 未承诺 data
    有序(S6),乱序会把向量静默错配到文本(R2)。这里每批按 data[].index 归位后再拼接。
    接口与 IndexService 现有调用方一致(embed_documents / embed_query);
    check_embedding_ctx_length 随 langchain 一起退役(不再经过它的 tiktoken 预切分)。
    """

    def __init__(self, *, model: str, api_key: str, base_url: str, batch_size: int,
                 max_retries: int, timeout: float):
        from openai import OpenAI

        self._model = model
        self._batch_size = batch_size
        self._client = OpenAI(api_key=api_key, base_url=base_url,
                              max_retries=max_retries, timeout=timeout)

    @property
    def client(self):
        """测试捕获请求参数用(patch.object(emb.client, "create"))。"""
        return self._client.embeddings

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start:start + self._batch_size]
            response = self._client.embeddings.create(
                model=self._model, input=batch,
                encoding_format="float")      # D2;bge-m3 不传 dimensions(D4/S3)
            data = response.model_dump()["data"]
            data.sort(key=lambda item: item["index"])         # S6:按 index 归位
            vectors.extend(item["embedding"] for item in data)
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def build_embeddings(settings: Settings):
    """可选嵌入模型(OpenAI 兼容端点)。未配置返回 None,检索退化为纯关键词。"""
    if not settings.embeddings_enabled:
        return None
    return OrderedEmbeddings(
        model=settings.embedding_model,
        api_key=settings.embedding_api_key or "not-needed",   # 自建端点可能不校验 key
        base_url=settings.embedding_base_url,
        batch_size=settings.embedding_batch_size,       # ≤ 32,SiliconFlow 数组上限(D3/S2)
        max_retries=settings.embedding_max_retries,     # D9:429/503 由 SDK 层重试
        timeout=settings.embedding_timeout_seconds,     # D9
    )
