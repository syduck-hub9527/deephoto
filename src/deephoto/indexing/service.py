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
from .bm25 import BM25Index, normalize
from .keys import stable_item_id

logger = logging.getLogger(__name__)

EMBED_BATCH = 32
_IMAGE_TEXT_LIMIT = 600


class IndexService:
    def __init__(self, embeddings=None, embedding_version: str | None = None):
        """embeddings: 带 embed_documents/embed_query 的对象(如 OpenAIEmbeddings),可为 None。"""
        self.embeddings = embeddings
        self.embedding_version = embedding_version

    # ---- 写入 ----

    def upsert_document(self, conn: Connection, document_id: str) -> None:
        doc = repo.get_document(conn, document_id)
        if doc is None:
            return
        tenant_id, version = doc["tenant_id"], doc["ingestion_version"]
        items: list[dict] = []   # {item_id, source_type, source_id, searchable_text}
        for chunk in repo.chunks_for_document(conn, document_id):
            text = (f"[{chunk['section']}]\n" if chunk["section"] else "") + chunk["text"]
            items.append({
                "item_id": stable_item_id(tenant_id, document_id, version, "chunk", chunk["id"]),
                "source_type": "chunk", "source_id": chunk["id"], "searchable_text": text,
            })
        for occ in repo.occurrences_for_document(conn, document_id):
            text = _image_searchable_text(occ)
            if not text.strip():
                continue
            items.append({
                "item_id": stable_item_id(tenant_id, document_id, version, "image", occ["id"]),
                "source_type": "image", "source_id": occ["id"], "searchable_text": text,
            })

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
            self._embed_items(conn, items)
        conn.commit()

    def _embed_items(self, conn: Connection, items: list[dict]) -> None:
        for start in range(0, len(items), EMBED_BATCH):
            batch = items[start:start + EMBED_BATCH]
            try:
                vectors = self.embeddings.embed_documents([i["searchable_text"] for i in batch])
            except Exception as exc:
                # 嵌入失败降级为纯关键词,索引仍然可用
                logger.warning("embedding batch failed, keyword only: %s", exc)
                for item in batch:
                    conn.execute("UPDATE index_items SET index_status = 'keyword_only' WHERE id = ?",
                                 (item["item_id"],))
                continue
            for item, vector in zip(batch, vectors):
                repo.put_embedding(conn, item["item_id"],
                                   np.asarray(vector, dtype=np.float32).tobytes())

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
            repo.upsert_index_item(
                conn, item_id=new_item_id, tenant_id=dst["tenant_id"], document_id=dst["id"],
                ingestion_version=dst["ingestion_version"], source_type=item["source_type"],
                source_id=new_source, embedding_version=item["embedding_version"],
                searchable_text=item["searchable_text"], index_status=item["index_status"],
            )
            if item["id"] in vectors:
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
        try:
            query_vec = np.asarray(self.embeddings.embed_query(query), dtype=np.float32)
        except Exception as exc:
            logger.warning("query embedding failed, keyword only: %s", exc)
            return {}
        stored = repo.get_embeddings(conn, item_ids)
        if not stored:
            return {}
        ids = list(stored)
        matrix = np.stack([np.frombuffer(stored[i], dtype=np.float32) for i in ids])
        q_norm = query_vec / (np.linalg.norm(query_vec) or 1.0)
        m_norm = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-12)
        sims = m_norm @ q_norm
        return normalize({item_id: float(max(sims[i], 0.0)) for i, item_id in enumerate(ids)})


def _image_searchable_text(occ: dict) -> str:
    parts: list[str] = []
    if occ.get("figure_number"):
        parts.append(f"图 {occ['figure_number']}")
    if occ.get("caption"):
        parts.append(occ["caption"])
    if occ.get("visible_labels"):
        parts.append(" ".join(occ["visible_labels"]))
    if occ.get("description"):
        parts.append(occ["description"])
    if occ.get("context_summary"):
        parts.append(occ["context_summary"])
    return "\n".join(parts)[:_IMAGE_TEXT_LIMIT]
