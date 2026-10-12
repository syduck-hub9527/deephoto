"""索引服务:正文块与图片描述的统一索引与融合检索(开发文档§3.5)。

- 稳定键幂等:tenant+document+version+source_type+source_id;
- 关键词:BM25(必选);向量:可选 OpenAI 兼容嵌入,未配置时 keyword_only;
- 融合:两者都有时各归一化后等权相加;
- 向量库只存检索内容与 ID,原图永远走对象存储(§3.5 末)。
"""

from __future__ import annotations

import logging
from sqlite3 import Connection

import numpy as np

from .. import repo
from ..parsing.captions import display_label
from .bm25 import BM25Index, normalize
from .keys import stable_item_id

logger = logging.getLogger(__name__)

EMBED_BATCH = 20
_IMAGE_TEXT_LIMIT = 600


class IndexService:
    def __init__(self, embeddings=None, embedding_version: str | None = None, *,
                 batch_size: int = EMBED_BATCH):
        """embeddings: 带 embed_documents/embed_query 的对象(如 OrderedEmbeddings),可为 None。
        batch_size: 每批嵌入条数(≤ 32,BGE-M3 迁移 D3;默认保持 EMBED_BATCH)。"""
        self.embeddings = embeddings
        self.embedding_version = embedding_version
        self.batch_size = batch_size

    # ---- 写入 ----

    def upsert_document(self, conn: Connection, document_id: str, observer=None) -> None:
        doc = repo.get_document(conn, document_id)
        if doc is None:
            return
        from ..pipeline.progress import NoOpObserver
        observer = observer or NoOpObserver()
        observer.stage_start("index_prepare", parent="indexing")
        tenant_id, version = doc["tenant_id"], doc["ingestion_version"]
        items: list[dict] = []   # {item_id, source_type, source_id, searchable_text}
        n_chunks = n_images = 0
        for chunk in repo.chunks_for_document(conn, document_id):
            text = (f"[{chunk['section']}]\n" if chunk["section"] else "") + chunk["text"]
            items.append({
                "item_id": stable_item_id(tenant_id, document_id, version, "chunk", chunk["id"]),
                "source_type": "chunk", "source_id": chunk["id"], "searchable_text": text,
            })
            n_chunks += 1
        for occ in repo.occurrences_for_document(conn, document_id):
            text = _image_searchable_text(occ)
            if not text.strip():
                continue
            items.append({
                "item_id": stable_item_id(tenant_id, document_id, version, "image", occ["id"]),
                "source_type": "image", "source_id": occ["id"], "searchable_text": text,
            })
            n_images += 1
        observer.stage_end("index_prepare", counts={
            "chunks": n_chunks, "images": n_images, "vectorizable": len(items) if self.embeddings else 0})

        keep_ids = [item["item_id"] for item in items]
        for item in items:
            repo.upsert_index_item(
                conn, item_id=item["item_id"], tenant_id=tenant_id, document_id=document_id,
                ingestion_version=version, source_type=item["source_type"], source_id=item["source_id"],
                embedding_version=self.embedding_version if self.embeddings else None,
                searchable_text=item["searchable_text"],
                index_status="ready" if self.embeddings else "keyword_only",
            )
        repo.delete_stale_index_items(conn, document_id, keep_ids)

        if self.embeddings and items:
            self._embed_items(conn, items, observer)
        else:
            observer.stage_start("embed_batches", parent="indexing")
            reason = "未启用语义向量,使用关键词检索" if not self.embeddings else "没有待生成的向量"
            observer.stage_end("embed_batches", result="skipped", detail={"reason": reason})
        conn.commit()

    def _embed_items(self, conn: Connection, items: list[dict], observer) -> None:
        from ..pipeline import progress as pg
        batch_size = self.batch_size
        total_batches = (len(items) + batch_size - 1) // batch_size
        observer.stage_start("embed_batches", parent="indexing", total=total_batches)
        degraded = 0
        for batch_no, start in enumerate(range(0, len(items), batch_size), start=1):
            batch = items[start:start + batch_size]
            observer.item_start("embed_batch", batch_no, label=f"第 {batch_no}/{total_batches} 批")
            try:
                vectors = self.embeddings.embed_documents([i["searchable_text"] for i in batch])
            except Exception as exc:
                # 嵌入失败降级为纯关键词,索引仍然可用
                degraded += 1
                logger.warning("embedding batch failed, keyword only: %s", exc)
                for item in batch:
                    conn.execute("UPDATE index_items SET index_status = 'keyword_only' WHERE id = ?",
                                 (item["item_id"],))
                observer.item_end("embed_batch", batch_no, pg.ITEM_DEGRADED,
                                  count=len(batch), error_kind=type(exc).__name__)
                continue
            for item, vector in zip(batch, vectors):
                repo.put_embedding(conn, item["item_id"],
                                   np.asarray(vector, dtype=np.float32).tobytes())
            observer.item_end("embed_batch", batch_no, pg.ITEM_OK, count=len(batch))
        if degraded:
            observer.warn(f"一批或多批向量生成失败({degraded}/{total_batches}),"
                          "对应内容仅支持关键词检索")
            observer.stage_end("embed_batches", result=pg.RESULT_PARTIAL)
        else:
            observer.stage_end("embed_batches")

    def clone_document(self, conn: Connection, *, src_document_id: str, dst: dict,
                       chunk_id_map: dict[str, str], occ_id_map: dict[str, str]) -> None:
        """去重克隆时按新 id 复制索引项与向量,避免重复嵌入调用。"""
        src_items = [i for i in repo.index_items_for_documents(conn, [src_document_id])]
        vectors = repo.get_embeddings(conn, [i["id"] for i in src_items])
        for item in src_items:
            if item["source_type"] == "chunk":
                new_source = chunk_id_map.get(item["source_id"])
            else:
                new_source = occ_id_map.get(item["source_id"])
            if not new_source:
                continue
            new_item_id = stable_item_id(dst["tenant_id"], dst["id"], dst["ingestion_version"],
                                         item["source_type"], new_source)
            # 只有与当前版本一致的向量才可复制(D6)
            same_version = (item["embedding_version"] is not None
                            and item["embedding_version"] == self.embedding_version)
            repo.upsert_index_item(
                conn, item_id=new_item_id, tenant_id=dst["tenant_id"], document_id=dst["id"],
                ingestion_version=dst["ingestion_version"], source_type=item["source_type"],
                source_id=new_source, embedding_version=item["embedding_version"],
                searchable_text=item["searchable_text"],
                # 版本守卫(D6):向量只复制版本一致的项;不一致的项置 keyword_only 等待重建,
                # 保留原版本号便于追溯来源模型
                index_status=item["index_status"] if same_version else "keyword_only",
            )
            if same_version and item["id"] in vectors:
                repo.put_embedding(conn, new_item_id, vectors[item["id"]])

    # ---- 检索 ----

    def search(self, conn: Connection, *, document_ids: list[str], query: str,
               k_chunks: int = 8, k_images: int = 6) -> dict[str, list[dict]]:
        """返回 {"chunks": [...], "images": [...]},元素 {source_id, item_id, score}。"""
        items = repo.index_items_for_documents(conn, document_ids)
        if not items:
            return {"chunks": [], "images": []}

        bm25 = BM25Index()
        bm25.build({item["id"]: item["searchable_text"] for item in items})
        kw_scores = normalize(bm25.scores(query))

        vec_scores: dict[str, float] = {}
        if self.embeddings:
            vec_scores = self._vector_scores(conn, query, [i["id"] for i in items])

        by_id = {item["id"]: item for item in items}
        fused: dict[str, float] = {}
        for item_id in by_id:
            kw = kw_scores.get(item_id, 0.0)
            if vec_scores:
                fused[item_id] = 0.5 * kw + 0.5 * vec_scores.get(item_id, 0.0)
            else:
                fused[item_id] = kw

        ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
        chunks, images = [], []
        for item_id, score in ranked:
            if score <= 0:
                break
            item = by_id[item_id]
            entry = {"item_id": item_id, "source_id": item["source_id"], "score": round(score, 4),
                     "document_id": item["document_id"]}
            if item["source_type"] == "chunk" and len(chunks) < k_chunks:
                chunks.append(entry)
            elif item["source_type"] == "image" and len(images) < k_images:
                images.append(entry)
            if len(chunks) >= k_chunks and len(images) >= k_images:
                break
        return {"chunks": chunks, "images": images}

    def _vector_scores(self, conn: Connection, query: str, item_ids: list[str]) -> dict[str, float]:
        """向量分(带版本与维度守卫,D6):只用与当前版本一致的向量;任何维度错配或
        底层异常都降级返回 {}(走关键词),问答不得因此失败(§1 问题 1、§5.3)。"""
        try:
            query_vec = np.asarray(self.embeddings.embed_query(query), dtype=np.float32)
        except Exception as exc:
            logger.warning("query embedding failed, keyword only: %s", exc)
            return {}
        stored = repo.get_embeddings_for_version(conn, item_ids, self.embedding_version)
        if not stored:
            return {}
        # 维度守卫:字节长度不符的向量是别的模型/迁移残留,过滤并计数告警(每条查询只报一次)
        dim = query_vec.shape[0]
        usable = {item_id: blob for item_id, blob in stored.items() if len(blob) == dim * 4}
        wrong_dim = len(stored) - len(usable)
        if wrong_dim:
            logger.warning("skipped %d stored vectors with dimension mismatch (query dim=%d)",
                           wrong_dim, dim)
        if not usable:
            if wrong_dim == len(stored):
                logger.warning("query vector dimension %d matches no stored vectors, keyword only",
                               dim)
            return {}
        try:
            ids = list(usable)
            matrix = np.stack([np.frombuffer(usable[i], dtype=np.float32) for i in ids])
            q_norm = query_vec / (np.linalg.norm(query_vec) or 1.0)
            m_norm = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-12)
            sims = m_norm @ q_norm
            return normalize({item_id: float(max(sims[i], 0.0)) for i, item_id in enumerate(ids)})
        except Exception:
            logger.exception("vector scoring failed, keyword only")
            return {}


def _image_searchable_text(occ: dict) -> str:
    parts: list[str] = []
    if occ.get("figure_number"):
        parts.append(display_label(occ["figure_number"]))
    if occ.get("caption"):
        parts.append(occ["caption"])
    if occ.get("visible_labels"):
        parts.append(" ".join(occ["visible_labels"]))
    if occ.get("description"):
        parts.append(occ["description"])
    if occ.get("context_summary"):
        parts.append(occ["context_summary"])
    return "\n".join(parts)[:_IMAGE_TEXT_LIMIT]
