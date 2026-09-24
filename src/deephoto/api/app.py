"""FastAPI 应用工厂:组装配置、服务、路由与后台 worker。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from ..agent.knowledge import KnowledgeService
from ..agent.qa import QAService
from ..config import Settings, load_settings
from ..db import connect, init_db
from ..indexing.service import IndexService
from ..llm import build_chat_model, build_embeddings
from ..parsing.pymupdf_parser import PyMuPDFParser
from ..pipeline.ingest import IngestService
from ..security import ensure_bootstrap_user
from ..storage import ObjectStore
from .routes_documents import router as documents_router
from .routes_qa import router as qa_router
from .routes_settings import router as settings_router
from .worker import IngestWorker

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).parent.parent / "web"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    init_db(settings.db_path)
    conn = connect(settings.db_path)
    ensure_bootstrap_user(conn, settings.bootstrap_tenant, settings.bootstrap_user,
                          settings.bootstrap_token)

    store = ObjectStore(settings.object_dir)
    parser = PyMuPDFParser()
    embeddings = build_embeddings(settings)
    index_service = IndexService(
        embeddings=embeddings,
        embedding_version=settings.embedding_model if embeddings else None,
    )
    ingest_service = IngestService(
        settings, store, parser, index_service,
        chat_model_factory=lambda: build_chat_model(settings),
    )
    knowledge = KnowledgeService(settings, store, index_service)
    qa_service = QAService(settings, knowledge)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker = IngestWorker(ingest_service, settings.db_path)
        worker.start()
        logger.info("deephoto started; embeddings=%s", "on" if embeddings else "off")
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

    app.include_router(documents_router)
    app.include_router(qa_router)
    app.include_router(settings_router)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(_WEB_DIR / "index.html")

    return app
