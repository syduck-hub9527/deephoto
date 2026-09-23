"""索引项稳定键(开发文档§3.5)。

同一租户+文档+解析版本+类型+来源 id 定义唯一索引项;
重复入库/去重克隆时据此幂等更新,不产生重复向量。
"""

from __future__ import annotations

import hashlib


def stable_item_id(tenant_id: str, document_id: str, ingestion_version: str,
                   source_type: str, source_id: str) -> str:
    raw = f"{tenant_id}|{document_id}|{ingestion_version}|{source_type}|{source_id}"
    return "idx_" + hashlib.sha1(raw.encode()).hexdigest()[:24]
