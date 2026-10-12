"""嵌入启动探测与 IndexService 装配(embedding-bge-m3-migration §5.2/§5.6)。

启动时做一次真实 embed_query 调用,确认模型可达并实测维度;探测失败不阻塞启动,
降级为纯关键词检索并记录错误(错误信息经脱敏,不含 API key)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..sanitize import error_summary

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingProbe:
    ok: bool
    dim: int | None
    version: str | None      # f"{model}#{dim}",dim 来自实测(D5),不写死
    error: str | None


def probe_embeddings(embeddings, settings) -> EmbeddingProbe:
    """调用一次 embed_query 探测;任何异常(网络/401/404/超时)都返回 ok=False,不抛出。

    设定了 EMBEDDING_DIM 且与实测不符时 ok=False,错误写明期望与实际值(§5.2)。
    """
    try:
        vector = embeddings.embed_query("探测")
        dim = len(vector)
    except Exception as exc:
        return EmbeddingProbe(ok=False, dim=None, version=None,
                              error=error_summary(exc, [settings.embedding_api_key]))
    expected = settings.embedding_dim
    if expected is not None and dim != expected:
        return EmbeddingProbe(ok=False, dim=dim, version=None,
                              error=f"维度不符:EMBEDDING_DIM 期望 {expected},实测 {dim}")
    return EmbeddingProbe(ok=True, dim=dim,
                          version=f"{settings.embedding_model}#{dim}", error=None)


def build_index_service(settings):
    """app.py 与 tests/baseline/run_snapshot.py 共用的装配:构造客户端 + 启动探测(§5.6)。

    探测失败:embeddings=None、embedding_version=None,降级为纯关键词检索(§5.2)。
    """
    from ..llm import build_embeddings
    from .service import IndexService

    embeddings = build_embeddings(settings)
    if embeddings is None:
        logger.info("embeddings: status=disabled(未配置 DEEPHOTO_EMBEDDING_BASE_URL/MODEL)")
        return IndexService()
    probe = probe_embeddings(embeddings, settings)
    if not probe.ok:
        logger.error("embeddings: status=disabled(%s),降级为纯关键词检索", probe.error)
        return IndexService()
    logger.info("embeddings: model=%s dim=%s version=%s status=ok",
                settings.embedding_model, probe.dim, probe.version)
    return IndexService(embeddings=embeddings, embedding_version=probe.version,
                        batch_size=settings.embedding_batch_size)
