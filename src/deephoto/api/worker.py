"""后台入库 worker:轮询 queued 文档并执行确定性入库任务(开发文档§3.1.3)。

单实例进程内线程;状态全部落库,进程重启后 queued 文档会被重新拾取,
重复执行以稳定键幂等。多副本部署时应换成真正的队列。
"""

from __future__ import annotations

import logging
import threading

from .. import repo
from ..db import connect

logger = logging.getLogger(__name__)


class IngestWorker(threading.Thread):
    def __init__(self, ingest_service, db_path, poll_seconds: float = 2.0):
        super().__init__(daemon=True, name="deephoto-ingest-worker")
        self.ingest_service = ingest_service
        self.db_path = db_path
        self.poll_seconds = poll_seconds
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                doc = repo.next_queued_document(connect(self.db_path))
                if doc is not None:
                    logger.info("ingesting document %s (%s)", doc["id"], doc["filename"])
                    self.ingest_service.ingest(doc["id"])
                else:
                    self._stop_event.wait(self.poll_seconds)
            except Exception:
                logger.exception("ingest worker iteration failed")
                self._stop_event.wait(self.poll_seconds)
