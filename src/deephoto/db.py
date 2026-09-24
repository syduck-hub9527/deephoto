"""SQLite 元数据存储:schema 定义与连接助手。

对应开发文档§4 的数据结构;字段名按 sqlite 习惯微调。
所有记录都带 tenant_id / document_id / ingestion_version(适用处),
索引写入以稳定键幂等(见 indexing/service.py)。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    token       TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

-- 每个租户一份可由设置页面修改的 OCR 配置。密钥只在服务端使用,
-- API 返回时仅暴露是否已配置,不会把原值发给浏览器。
CREATE TABLE IF NOT EXISTS ocr_settings (
    tenant_id       TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    base_url        TEXT,
    api_key         TEXT,
    timeout_seconds REAL NOT NULL DEFAULT 60,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    owner_id          TEXT NOT NULL,
    filename          TEXT NOT NULL,
    pdf_object_key    TEXT NOT NULL,
    sha256            TEXT NOT NULL,
    status            TEXT NOT NULL,          -- queued|parsing|describing|indexing|ready|failed
    error             TEXT,
    ingestion_version TEXT NOT NULL,
    page_count        INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_tenant ON documents(tenant_id, status);
-- 去重复用:同租户+同内容+同解析版本
CREATE INDEX IF NOT EXISTS idx_documents_dedup ON documents(tenant_id, sha256, ingestion_version);

CREATE TABLE IF NOT EXISTS text_chunks (
    id                    TEXT PRIMARY KEY,
    tenant_id             TEXT NOT NULL,
    document_id           TEXT NOT NULL,
    ingestion_version     TEXT NOT NULL,
    section               TEXT,
    text                  TEXT NOT NULL,
    page_start            INTEGER NOT NULL,
    page_end              INTEGER NOT NULL,
    paragraph_ids         TEXT NOT NULL DEFAULT '[]',  -- JSON 数组
    referenced_image_ids  TEXT NOT NULL DEFAULT '[]',  -- JSON 数组:ImageOccurrence id
    nearby_image_ids      TEXT NOT NULL DEFAULT '[]'   -- JSON 数组
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON text_chunks(document_id);

CREATE TABLE IF NOT EXISTS image_assets (
    id                   TEXT PRIMARY KEY,
    tenant_id            TEXT NOT NULL,
    sha256               TEXT NOT NULL,
    original_object_key  TEXT NOT NULL,
    width                INTEGER NOT NULL,
    height               INTEGER NOT NULL,
    mime_type            TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    UNIQUE(tenant_id, sha256)   -- 图片资产按内容哈希去重
);

CREATE TABLE IF NOT EXISTS image_occurrences (
    id                 TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    document_id        TEXT NOT NULL,
    ingestion_version  TEXT NOT NULL,
    image_asset_id     TEXT NOT NULL REFERENCES image_assets(id),
    page_number        INTEGER NOT NULL,
    bbox               TEXT,               -- JSON [x0,y0,x1,y1],坐标系见 bbox_coord
    bbox_coord         TEXT NOT NULL DEFAULT 'pdf_points',
    figure_number      TEXT,               -- 规范化后的图号,如 "3"
    caption            TEXT,
    description        TEXT,               -- K3 生成的 visible_summary
    visible_labels     TEXT NOT NULL DEFAULT '[]',  -- JSON 数组
    context_summary    TEXT,
    uncertain_details  TEXT NOT NULL DEFAULT '[]',  -- JSON 数组
    description_model  TEXT,               -- 生成描述的模型与提示词版本
    extraction_method  TEXT NOT NULL,      -- embedded_bitmap|vector_render|page_fallback
    needs_review       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_occ_doc ON image_occurrences(document_id);
CREATE INDEX IF NOT EXISTS idx_occ_fig ON image_occurrences(document_id, figure_number);

CREATE TABLE IF NOT EXISTS chunk_image_links (
    id                  TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    document_id         TEXT NOT NULL,
    chunk_id            TEXT NOT NULL REFERENCES text_chunks(id),
    image_occurrence_id TEXT NOT NULL REFERENCES image_occurrences(id),
    relation            TEXT NOT NULL,     -- references|caption_of|nearby
    confidence          REAL NOT NULL,
    UNIQUE(chunk_id, image_occurrence_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_links_chunk ON chunk_image_links(chunk_id);
CREATE INDEX IF NOT EXISTS idx_links_image ON chunk_image_links(image_occurrence_id);

CREATE TABLE IF NOT EXISTS index_items (
    -- id 为稳定键:sha1(tenant|document|version|source_type|source_id),重复入库幂等
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    document_id       TEXT NOT NULL,
    ingestion_version TEXT NOT NULL,
    source_type       TEXT NOT NULL,       -- chunk|image
    source_id         TEXT NOT NULL,
    embedding_version TEXT,
    searchable_text   TEXT NOT NULL,
    index_status      TEXT NOT NULL        -- ready|keyword_only
);
CREATE INDEX IF NOT EXISTS idx_index_doc ON index_items(document_id, source_type);

CREATE TABLE IF NOT EXISTS embeddings (
    item_id  TEXT PRIMARY KEY REFERENCES index_items(id),
    vector   BLOB NOT NULL                 -- float32 小端字节序
);
"""

_local = threading.local()


def connect(db_path: Path) -> sqlite3.Connection:
    """当前线程的连接(thread-local 缓存;WAL 模式允许读写并发)。

    sqlite 连接有线程亲和性:只能在创建它的线程内使用。
    调用方必须保证不在线程间传递连接对象(如 FastAPI 依赖注入)。
    """
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != str(db_path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
        _local.path = str(db_path)
    return conn


def init_db(db_path: Path) -> None:
    connect(db_path).executescript(SCHEMA)
