"""元数据访问层:集中所有 SQL,业务层不直接写 SQL。

所有查询按 tenant_id 过滤;文档归属校验在各入口完成。
"""

from __future__ import annotations

import json
import time
from sqlite3 import Connection

from .security import new_id


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---- tenant settings ----

def get_ocr_settings(conn: Connection, tenant_id: str,
                     defaults: dict[str, object] | None = None) -> dict[str, object]:
    """读取租户 OCR 设置;尚未保存时返回环境变量默认值。"""
    result = {
        "provider": "third_party",
        "model": "",
        "base_url": None,
        "api_key": None,
        "timeout_seconds": 60.0,
    }
    if defaults:
        result.update(defaults)
    row = conn.execute(
        "SELECT provider, model, base_url, api_key, timeout_seconds FROM ocr_settings"
        " WHERE tenant_id = ?", (tenant_id,),
    ).fetchone()
    if row:
        result.update(dict(row))
    result["timeout_seconds"] = float(result["timeout_seconds"] or 60.0)
    return result


def save_ocr_settings(conn: Connection, tenant_id: str, *, provider: str, model: str,
                      base_url: str | None, api_key: str | None,
                      timeout_seconds: float) -> None:
    conn.execute(
        "INSERT INTO ocr_settings (tenant_id, provider, model, base_url, api_key,"
        " timeout_seconds, updated_at) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(tenant_id) DO UPDATE SET provider = excluded.provider,"
        " model = excluded.model, base_url = excluded.base_url, api_key = excluded.api_key,"
        " timeout_seconds = excluded.timeout_seconds, updated_at = excluded.updated_at",
        (tenant_id, provider, model, base_url, api_key, float(timeout_seconds), _now()),
    )
    conn.commit()


# ---- documents ----

def insert_document(conn: Connection, *, tenant_id: str, owner_id: str, filename: str,
                    pdf_object_key: str, sha256: str, ingestion_version: str) -> str:
    doc_id = new_id("doc")
    conn.execute(
        "INSERT INTO documents (id, tenant_id, owner_id, filename, pdf_object_key, sha256,"
        " status, ingestion_version, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (doc_id, tenant_id, owner_id, filename, pdf_object_key, sha256,
         "queued", ingestion_version, _now()),
    )
    conn.commit()
    return doc_id


def get_document(conn: Connection, doc_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


def get_owned_document(conn: Connection, document_id: str, tenant_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM documents WHERE id = ? AND tenant_id = ?", (document_id, tenant_id)).fetchone()
    return dict(row) if row else None


def list_documents(conn: Connection, tenant_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, filename, status, error, page_count, created_at FROM documents"
        " WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_document_status(conn: Connection, doc_id: str, status: str,
                           error: str | None = None, page_count: int | None = None) -> None:
    if page_count is None:
        conn.execute("UPDATE documents SET status = ?, error = ? WHERE id = ?", (status, error, doc_id))
    else:
        conn.execute("UPDATE documents SET status = ?, error = ?, page_count = ? WHERE id = ?",
                     (status, error, page_count, doc_id))
    conn.commit()


def find_ready_document_by_hash(conn: Connection, tenant_id: str, sha256: str, ingestion_version: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM documents WHERE tenant_id = ? AND sha256 = ? AND ingestion_version = ?"
        " AND status = 'ready' ORDER BY created_at ASC LIMIT 1",
        (tenant_id, sha256, ingestion_version),
    ).fetchone()
    return dict(row) if row else None


def next_queued_document(conn: Connection) -> dict | None:
    row = conn.execute("SELECT * FROM documents WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1").fetchone()
    return dict(row) if row else None


# ---- text chunks ----

def insert_chunk(conn: Connection, *, chunk_id: str | None = None, tenant_id: str, document_id: str,
                 ingestion_version: str, section: str | None, text: str,
                 page_start: int, page_end: int, paragraph_ids: list[str],
                 referenced_image_ids: list[str], nearby_image_ids: list[str]) -> str:
    cid = chunk_id or new_id("chk")
    conn.execute(
        "INSERT INTO text_chunks (id, tenant_id, document_id, ingestion_version, section, text,"
        " page_start, page_end, paragraph_ids, referenced_image_ids, nearby_image_ids)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (cid, tenant_id, document_id, ingestion_version, section, text, page_start, page_end,
         json.dumps(paragraph_ids), json.dumps(referenced_image_ids), json.dumps(nearby_image_ids)),
    )
    return cid


def chunks_for_document(conn: Connection, document_id: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM text_chunks WHERE document_id = ?", (document_id,)).fetchall()
    return [_chunk_dict(r) for r in rows]


def get_chunks(conn: Connection, chunk_ids: list[str]) -> list[dict]:
    if not chunk_ids:
        return []
    marks = ",".join("?" for _ in chunk_ids)
    rows = conn.execute(f"SELECT * FROM text_chunks WHERE id IN ({marks})", chunk_ids).fetchall()
    return [_chunk_dict(r) for r in rows]


def _chunk_dict(row) -> dict:
    d = dict(row)
    for key in ("paragraph_ids", "referenced_image_ids", "nearby_image_ids"):
        d[key] = json.loads(d[key])
    return d


# ---- image assets / occurrences ----

def get_or_create_asset(conn: Connection, *, tenant_id: str, sha256: str, object_key: str,
                        width: int, height: int, mime_type: str) -> str:
    row = conn.execute("SELECT id FROM image_assets WHERE tenant_id = ? AND sha256 = ?",
                       (tenant_id, sha256)).fetchone()
    if row:
        return row["id"]
    asset_id = new_id("ast")
    conn.execute(
        "INSERT INTO image_assets (id, tenant_id, sha256, original_object_key, width, height, mime_type, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (asset_id, tenant_id, sha256, object_key, width, height, mime_type, _now()),
    )
    return asset_id


def get_asset(conn: Connection, asset_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM image_assets WHERE id = ?", (asset_id,)).fetchone()
    return dict(row) if row else None


def insert_occurrence(conn: Connection, *, occ_id: str | None = None, tenant_id: str, document_id: str,
                      ingestion_version: str, image_asset_id: str, page_number: int,
                      bbox: list[float] | None, figure_number: str | None, caption: str | None,
                      extraction_method: str, needs_review: bool) -> str:
    oid = occ_id or new_id("occ")
    conn.execute(
        "INSERT INTO image_occurrences (id, tenant_id, document_id, ingestion_version, image_asset_id,"
        " page_number, bbox, figure_number, caption, extraction_method, needs_review)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (oid, tenant_id, document_id, ingestion_version, image_asset_id, page_number,
         json.dumps(bbox) if bbox else None, figure_number, caption, extraction_method, int(needs_review)),
    )
    return oid


def occurrences_for_document(conn: Connection, document_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM image_occurrences WHERE document_id = ? ORDER BY page_number", (document_id,),
    ).fetchall()
    return [_occ_dict(r) for r in rows]


def get_occurrence(conn: Connection, occ_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM image_occurrences WHERE id = ?", (occ_id,)).fetchone()
    return _occ_dict(row) if row else None


def update_occurrence_description(conn: Connection, occ_id: str, *, visible_summary: str,
                                  visible_labels: list[str], caption: str, context_summary: str,
                                  uncertain_details: list[str], description_model: str) -> None:
    conn.execute(
        "UPDATE image_occurrences SET description = ?, visible_labels = ?,"
        " caption = COALESCE(NULLIF(caption, ''), ?), context_summary = ?,"
        " uncertain_details = ?, description_model = ? WHERE id = ?",
        (visible_summary, json.dumps(visible_labels, ensure_ascii=False), caption,
         context_summary, json.dumps(uncertain_details, ensure_ascii=False), description_model, occ_id),
    )


def _occ_dict(row) -> dict:
    d = dict(row)
    d["bbox"] = json.loads(d["bbox"]) if d["bbox"] else None
    d["visible_labels"] = json.loads(d["visible_labels"])
    d["uncertain_details"] = json.loads(d["uncertain_details"])
    d["needs_review"] = bool(d["needs_review"])
    return d


# ---- links ----

def insert_link(conn: Connection, *, tenant_id: str, document_id: str, chunk_id: str,
                image_occurrence_id: str, relation: str, confidence: float) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO chunk_image_links"
        " (id, tenant_id, document_id, chunk_id, image_occurrence_id, relation, confidence)"
        " VALUES (?,?,?,?,?,?,?)",
        (new_id("lnk"), tenant_id, document_id, chunk_id, image_occurrence_id, relation, confidence),
    )


def links_for_chunks(conn: Connection, chunk_ids: list[str], min_confidence: float = 0.0) -> list[dict]:
    if not chunk_ids:
        return []
    marks = ",".join("?" for _ in chunk_ids)
    rows = conn.execute(
        f"SELECT * FROM chunk_image_links WHERE chunk_id IN ({marks}) AND confidence >= ?",
        (*chunk_ids, min_confidence),
    ).fetchall()
    return [dict(r) for r in rows]


def links_for_images(conn: Connection, occ_ids: list[str], min_confidence: float = 0.0) -> list[dict]:
    if not occ_ids:
        return []
    marks = ",".join("?" for _ in occ_ids)
    rows = conn.execute(
        f"SELECT * FROM chunk_image_links WHERE image_occurrence_id IN ({marks}) AND confidence >= ?",
        (*occ_ids, min_confidence),
    ).fetchall()
    return [dict(r) for r in rows]


# ---- index items / embeddings ----

def upsert_index_item(conn: Connection, *, item_id: str, tenant_id: str, document_id: str,
                      ingestion_version: str, source_type: str, source_id: str,
                      embedding_version: str | None, searchable_text: str, index_status: str) -> None:
    conn.execute(
        "INSERT INTO index_items (id, tenant_id, document_id, ingestion_version, source_type, source_id,"
        " embedding_version, searchable_text, index_status) VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET searchable_text = excluded.searchable_text,"
        " embedding_version = excluded.embedding_version, index_status = excluded.index_status",
        (item_id, tenant_id, document_id, ingestion_version, source_type, source_id,
         embedding_version, searchable_text, index_status),
    )


def index_items_for_documents(conn: Connection, document_ids: list[str]) -> list[dict]:
    if not document_ids:
        return []
    marks = ",".join("?" for _ in document_ids)
    rows = conn.execute(f"SELECT * FROM index_items WHERE document_id IN ({marks})", document_ids).fetchall()
    return [dict(r) for r in rows]


def put_embedding(conn: Connection, item_id: str, vector_bytes: bytes) -> None:
    conn.execute(
        "INSERT INTO embeddings (item_id, vector) VALUES (?,?)"
        " ON CONFLICT(item_id) DO UPDATE SET vector = excluded.vector",
        (item_id, vector_bytes),
    )


def get_embeddings(conn: Connection, item_ids: list[str]) -> dict[str, bytes]:
    if not item_ids:
        return {}
    marks = ",".join("?" for _ in item_ids)
    rows = conn.execute(f"SELECT item_id, vector FROM embeddings WHERE item_id IN ({marks})", item_ids).fetchall()
    return {r["item_id"]: r["vector"] for r in rows}


def delete_stale_index_items(conn: Connection, document_id: str, keep_ids: list[str]) -> None:
    """删除该文档不在 keep_ids 中的索引项及其向量(重复入库一致性)。"""
    existing = [r["id"] for r in conn.execute(
        "SELECT id FROM index_items WHERE document_id = ?", (document_id,)).fetchall()]
    for item_id in existing:
        if item_id not in keep_ids:
            conn.execute("DELETE FROM embeddings WHERE item_id = ?", (item_id,))
            conn.execute("DELETE FROM index_items WHERE id = ?", (item_id,))


# ---- 去重复用与删除 ----

def clone_document_data(conn: Connection, *, src_document_id: str, dst: dict) -> tuple[dict[str, str], dict[str, str]]:
    """把 src 文档的块/图/关系克隆到 dst(去重复用,文档§3.1.2)。

    图片资产按内容哈希共享;块、出现位置、关系生成新 id。
    返回 (chunk_id_map, occ_id_map),供索引层克隆索引项与向量。
    """
    dst_id, tenant, version = dst["id"], dst["tenant_id"], dst["ingestion_version"]
    occ_id_map: dict[str, str] = {}
    for occ in occurrences_for_document(conn, src_document_id):
        new_occ = new_id("occ")
        occ_id_map[occ["id"]] = new_occ
        conn.execute(
            "INSERT INTO image_occurrences (id, tenant_id, document_id, ingestion_version, image_asset_id,"
            " page_number, bbox, figure_number, caption, description, visible_labels, context_summary,"
            " uncertain_details, description_model, extraction_method, needs_review)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_occ, tenant, dst_id, version, occ["image_asset_id"], occ["page_number"],
             json.dumps(occ["bbox"]) if occ["bbox"] else None, occ["figure_number"], occ["caption"],
             occ["description"], json.dumps(occ["visible_labels"], ensure_ascii=False),
             occ["context_summary"], json.dumps(occ["uncertain_details"], ensure_ascii=False),
             occ["description_model"], occ["extraction_method"], int(occ["needs_review"])),
        )
    chunk_id_map: dict[str, str] = {}
    for chunk in chunks_for_document(conn, src_document_id):
        new_chunk = new_id("chk")
        chunk_id_map[chunk["id"]] = new_chunk
        insert_chunk(
            conn, chunk_id=new_chunk, tenant_id=tenant, document_id=dst_id, ingestion_version=version,
            section=chunk["section"], text=chunk["text"],
            page_start=chunk["page_start"], page_end=chunk["page_end"],
            paragraph_ids=chunk["paragraph_ids"],
            referenced_image_ids=[occ_id_map[i] for i in chunk["referenced_image_ids"] if i in occ_id_map],
            nearby_image_ids=[occ_id_map[i] for i in chunk["nearby_image_ids"] if i in occ_id_map],
        )
    src_links = conn.execute("SELECT * FROM chunk_image_links WHERE document_id = ?", (src_document_id,)).fetchall()
    for link in src_links:
        if link["chunk_id"] in chunk_id_map and link["image_occurrence_id"] in occ_id_map:
            insert_link(conn, tenant_id=tenant, document_id=dst_id,
                        chunk_id=chunk_id_map[link["chunk_id"]],
                        image_occurrence_id=occ_id_map[link["image_occurrence_id"]],
                        relation=link["relation"], confidence=link["confidence"])
    return chunk_id_map, occ_id_map


def delete_document(conn: Connection, document_id: str, tenant_id: str) -> dict | None:
    """删除文档及其元数据与索引;返回仍被引用的对象键之外的待清理信息。"""
    doc = get_owned_document(conn, document_id, tenant_id)
    if doc is None:
        return None
    occs = occurrences_for_document(conn, document_id)
    asset_ids = {o["image_asset_id"] for o in occs}
    item_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM index_items WHERE document_id = ?", (document_id,)).fetchall()]
    for item_id in item_ids:
        conn.execute("DELETE FROM embeddings WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM index_items WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM chunk_image_links WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM image_occurrences WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM text_chunks WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    # 仅删除不再被任何出现位置引用的图片资产
    orphan_asset_keys: list[str] = []
    for asset_id in asset_ids:
        still_used = conn.execute(
            "SELECT 1 FROM image_occurrences WHERE image_asset_id = ? LIMIT 1", (asset_id,)).fetchone()
        if not still_used:
            asset = get_asset(conn, asset_id)
            conn.execute("DELETE FROM image_assets WHERE id = ?", (asset_id,))
            if asset:
                orphan_asset_keys.append(asset["original_object_key"])
    pdf_still_used = conn.execute(
        "SELECT 1 FROM documents WHERE pdf_object_key = ? LIMIT 1", (doc["pdf_object_key"],)).fetchone()
    conn.commit()
    object_keys = list(orphan_asset_keys)
    if not pdf_still_used:
        object_keys.append(doc["pdf_object_key"])
    return {"object_keys": object_keys}
