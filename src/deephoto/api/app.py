"""FastAPI 应用工厂:组装配置、服务、路由与后台 worker。"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from ..agent.knowledge import KnowledgeService
from ..agent.qa import QAService
from ..config import Settings, load_settings
from ..db import init_db
from ..indexing.service import IndexService
from ..llm import build_description_model, build_embeddings
from ..parsing.pymupdf_parser import PyMuPDFParser
from ..pipeline.ingest import IngestService
from ..progress_store import ProgressStore
from ..storage import ObjectStore
from .routes_documents import router as documents_router
from .routes_qa import router as qa_router
from .worker import IngestWorker

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).parent.parent / "web"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    # 项目 logger 的 INFO 需要真正可见(用户主要看服务日志),不重复添加 handler
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db(settings.db_path)

    # 入库观测:独立轻量库;本次进程实例标识用于启动时识别旧实例的"假活跃"运行
    instance_id = f"inst-{int(time.time())}-{id(object()):x}"
    progress_store = ProgressStore(settings.progress_db_path, instance_id=instance_id)
    progress_store.mark_interrupted()

    store = ObjectStore(settings.object_dir)
    parser = PyMuPDFParser()
    embeddings = build_embeddings(settings)
    index_service = IndexService(
        embeddings=embeddings,
        embedding_version=settings.embedding_model if embeddings else None,
    )
    ingest_service = IngestService(
        settings, store, parser, index_service,
        # 图片描述专用工厂(与问答模型解耦);问答服务继续走 build_chat_model,互不共享缓存
        chat_model_factory=lambda: build_description_model(settings),
        progress_store=progress_store,
    )
    knowledge = KnowledgeService(store, index_service)
    qa_service = QAService(settings, knowledge)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker = IngestWorker(ingest_service, settings.db_path, progress_store=progress_store)
        worker.start()
        logger.info("deephoto started; embeddings=%s", "on" if embeddings else "off")
        logger.info("image_description enabled=%s model=%s reasoning_effort=%s timeout=%s retries=%s",
                    settings.description_enabled,
                    settings.description_model if settings.description_enabled else "-",
                    settings.description_reasoning_effort if settings.description_enabled else "-",
                    settings.description_timeout_seconds, settings.description_max_retries)
        yield
        worker.stop()
        worker.join(timeout=5)

    app = FastAPI(title="deephoto", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.parser = parser
    app.state.index_service = index_service
    app.state.ingest_service = ingest_service
    app.state.qa_service = qa_service
    app.state.progress_store = progress_store

    app.include_router(documents_router)
    app.include_router(qa_router)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(_WEB_DIR / "index.html")

    @app.get("/segments.js", include_in_schema=False)
    def segments_js():
        return FileResponse(_WEB_DIR / "segments.js", media_type="text/javascript")

    return app
