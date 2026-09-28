"""入库观测的阶段与结果常量、空观察器与时间工具(开发任务 md文档/deephoto_ingestion_progress_plan.md)。

观测只记录与展示,不改变业务状态;所有回调都有空实现(NoOpObserver),
未接入观测存储的调用方(旧测试、程序化调用)无需任何改动。
"""

from __future__ import annotations

import time

# ---- 顶层阶段(顺序即展示顺序;标识与展示名分离)----

STAGE_QUEUED = "queued"
STAGE_DEDUP = "dedup_lookup"
STAGE_REUSE = "reuse"
STAGE_PARSING = "parsing"
STAGE_FIGURES = "persist_figures"
STAGE_CHUNKS = "chunks_and_links"
STAGE_DESCRIBING = "describing"
STAGE_INDEXING = "indexing"
STAGE_FINALIZING = "finalizing"

TOP_STAGES = (STAGE_QUEUED, STAGE_DEDUP, STAGE_REUSE, STAGE_PARSING, STAGE_FIGURES,
              STAGE_CHUNKS, STAGE_DESCRIBING, STAGE_INDEXING, STAGE_FINALIZING)

STAGE_NAMES = {
    STAGE_QUEUED: "排队中",
    STAGE_DEDUP: "检查已有处理结果",
    STAGE_REUSE: "复用已有结果",
    STAGE_PARSING: "云端解析",
    STAGE_FIGURES: "保存图片",
    STAGE_CHUNKS: "整理正文与图文关系",
    STAGE_DESCRIBING: "生成图片描述",
    STAGE_INDEXING: "准备检索",
    STAGE_FINALIZING: "完成保存",
    # MinerU 子阶段(parsing 的子项)
    "mineru_split": "读取与拆分",
    "mineru_merge": "合并解析结果",
    "index_prepare": "整理检索内容",
}

# ---- 结果口径 ----

RESULT_RUNNING = "running"        # 进行中
RESULT_SUCCEEDED = "succeeded"    # 成功
RESULT_SKIPPED = "skipped"        # 跳过(如未配置模型/向量)
RESULT_PARTIAL = "partial"        # 部分降级(如个别向量批次失败)
RESULT_FAILED = "failed"          # 失败
RESULT_INTERRUPTED = "interrupted"  # 服务重启导致观测中断(真实耗时未知)

# 单项结果(图片/批次)
ITEM_OK = "ok"
ITEM_PARSE_FAILED = "parse_failed"  # 调用完成但描述格式解析失败(不能算成功)
ITEM_ERROR = "error"                # 调用异常
ITEM_DEGRADED = "degraded"          # 降级(向量批次失败转关键词)


def utc_iso(ts: float | None = None) -> str:
    """UTC ISO 时间戳(持久化口径)。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def iso_to_ms(value: str | None) -> int | None:
    """ISO 时间戳 -> epoch 毫秒;无法解析返回 None。"""
    if not value:
        return None
    try:
        return int(time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")) * 1000)
    except (ValueError, OverflowError):
        return None


def elapsed_ms(start_iso: str | None, end_iso: str | None) -> int | None:
    """两个 ISO 时间戳的毫秒差;负数防护为 0,缺参返回 None。"""
    start, end = iso_to_ms(start_iso), iso_to_ms(end_iso)
    if start is None or end is None:
        return None
    return max(0, end - start)


class NoOpObserver:
    """空观察器:与 RunObserver 同接口,全部方法无操作。"""

    def stage_start(self, stage: str, *, parent: str | None = None,
                    total: int | None = None, detail: dict | None = None) -> None: ...

    def stage_end(self, stage: str, result: str = RESULT_SUCCEEDED, *,
                  counts: dict | None = None, detail: dict | None = None,
                  duration_ms: int | None = None) -> None: ...

    def item_start(self, kind: str, seq: int, *, label: str | None = None,
                   page: int | None = None, figure: str | None = None) -> None: ...

    def item_update(self, kind: str, seq: int, *, label: str | None = None,
                    detail: dict | None = None) -> None: ...

    def item_end(self, kind: str, seq: int, result: str, *,
                 count: int | None = None, error_kind: str | None = None,
                 detail: dict | None = None) -> None: ...

    def warn(self, message: str) -> None: ...

    def finish(self, result: str) -> None: ...
