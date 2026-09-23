"""问答服务:接入 create_deep_agent,后端校验引用并组装图片(开发文档§5)。

要点:
- tenant/user 来自可信请求上下文,不作为模型参数;
- 模型只能以 [chunk:ID] / [image:ID] 格式引用工具返回的 ID;
- 最终引用与图片列表由后端验证组装,不依赖模型自由生成(§5.1.7);
- 支持流式(SSE):token 实时推送,结束后推送校验后的引用与图片。

线程纪律:sqlite 连接有线程亲和性。请求级 conn 只在请求线程内使用;
工具在 LangGraph 执行器线程中运行,体内用 connect(db_path) 自取连接;
流式生成器被 Starlette 分次调度,任何数据库访问都在使用点现取连接。
"""

from __future__ import annotations

import json
import logging
import re
from sqlite3 import Connection
from typing import Iterator

from .. import repo
from ..config import Settings
from ..db import connect
from ..llm import build_chat_model
from ..parsing.captions import asks_for_images
from ..security import AuthContext
from .knowledge import KnowledgeService

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是文档知识库问答助手。回答当前用户的问题时遵守:\n"
    "1. 先用 search_knowledge 检索文档(用户指定了文档就传该 document_id,否则检索全部可访问文档)。\n"
    "2. 当问题涉及图中的标签、箭头、数值、颜色或空间关系时,必须用 inspect_image 查看完整原图后再作答;"
    "不要只凭图片的文字描述下结论。\n"
    "3. 引用证据必须使用固定格式:正文 [chunk:chunk_id],图片 [image:image_occurrence_id];"
    "只能引用工具返回过的 ID,不得编造页码、图号或图片。\n"
    "4. 回答中注明出处页码(证据里的 page_start / page 字段)。\n"
    "5. 检索不到可靠依据时明确说明不知道,不要编造内容。"
)

_CITE_CHUNK_RE = re.compile(r"\[chunk:([A-Za-z0-9_]+)\]")
_CITE_IMAGE_RE = re.compile(r"\[image:([A-Za-z0-9_]+)\]")


class QAService:
    def __init__(self, settings: Settings, knowledge: KnowledgeService):
        self.settings = settings
        self.knowledge = knowledge
        self._chat_model = None

    def _model(self):
        if self._chat_model is None:
            self._chat_model = build_chat_model(self.settings)
        return self._chat_model

    # ---- 非流式(保留给调试/程序化调用)----

    def answer(self, conn: Connection, ctx: AuthContext, question: str,
               document_id: str | None = None, history: list[dict] | None = None) -> dict:
        if document_id and not self.knowledge.allowed_document_ids(conn, ctx, document_id):
            return {"error": "文档不存在、无权限或尚未处理完成", "answer": None, "citations": [], "images": []}
        tools, tracker = self._make_tools(ctx)
        agent = self._build_agent(tools)
        messages = _build_messages(question, document_id, history)
        result = agent.invoke({"messages": messages})
        answer_text = _final_answer_text(result.get("messages", []))
        extra_chunks, extra_images = _history_cited_ids(history)
        return self._assemble(conn, ctx, answer_text, question, tracker, extra_chunks, extra_images)

    # ---- 流式(SSE)----

    def answer_stream(self, ctx: AuthContext, question: str,
                      document_id: str | None = None, history: list[dict] | None = None) -> Iterator[dict]:
        """产出事件:{type: thinking|token|done|error}。done 携带校验后的 citations/images。"""
        try:
            yield from self._stream_impl(ctx, question, document_id, history)
        except Exception as exc:
            logger.exception("qa stream failed")
            yield {"type": "error", "detail": f"{type(exc).__name__}: {exc}"}

    def _stream_impl(self, ctx: AuthContext, question: str,
                     document_id: str | None, history: list[dict] | None) -> Iterator[dict]:
        db_path = self.settings.db_path
        if document_id and not self.knowledge.allowed_document_ids(connect(db_path), ctx, document_id):
            yield {"type": "error", "detail": "文档不存在、无权限或尚未处理完成"}
            return

        tools, tracker = self._make_tools(ctx)
        agent = self._build_agent(tools)
        messages = _build_messages(question, document_id, history)

        answer_parts: list[str] = []
        stream_broken: str | None = None
        seen_chunks = 0
        try:
            for chunk, _metadata in agent.stream({"messages": messages}, stream_mode="messages"):
                seen_chunks += 1
                # 只流式 AI 增量:ToolMessage(Chunk) 的 content 是工具返回的 JSON/图片块,必须排除。
                # 注意 langchain-core 中 AIMessageChunk.type == "AIMessageChunk"(非 "ai")
                if getattr(chunk, "type", None) not in ("ai", "AIMessageChunk"):
                    continue
                # 工具调用轮次的 AI 消息没有正文,跳过
                if getattr(chunk, "tool_call_chunks", None):
                    continue
                reasoning = _extract_reasoning(getattr(chunk, "additional_kwargs", None) or {})
                if reasoning:
                    yield {"type": "thinking", "text": reasoning}
                text = _extract_text(chunk.content)
                if text:
                    answer_parts.append(text)
                    yield {"type": "token", "text": text}
        except Exception as exc:
            # 流中断:保留已流出的部分答案,继续走校验组装,让前端拿到完整 done 事件
            logger.exception("model stream interrupted")
            stream_broken = f"{type(exc).__name__}: {exc}"

        answer_text = "".join(answer_parts)
        if not answer_text.strip():
            detail = f"模型未产生回答(收到 {seen_chunks} 个流块,无正文 token)"
            if stream_broken:
                detail += f";流中断: {stream_broken}"
            yield {"type": "error", "detail": detail}
            return
        if stream_broken:
            yield {"type": "warning", "detail": f"回答可能不完整(流中断:{stream_broken})"}

        answer_text = "".join(answer_parts)
        extra_chunks, extra_images = _history_cited_ids(history)
        result = self._assemble(connect(db_path), ctx, answer_text, question, tracker,
                                extra_chunks, extra_images)
        result["type"] = "done"
        yield result

    # ---- 公共部分 ----

    def _build_agent(self, tools):
        from deepagents import create_deep_agent

        return create_deep_agent(model=self._model(), tools=tools, system_prompt=SYSTEM_PROMPT)

    def _make_tools(self, ctx: AuthContext):
        """构造绑定请求上下文的工具;tracker 由工具副作用记录实际提供过的证据 ID。"""
        knowledge = self.knowledge
        db_path = self.settings.db_path
        tracker = {"chunks": set(), "images": set()}

        def search_knowledge(query: str, document_id: str = "") -> str:
            """检索当前用户有权访问的文档知识库。输入问题或关键词(可含“图 3”等图号),
            可选传入目标 document_id。返回 JSON:正文块与候选图片的 ID、页码、图注、分数。"""
            result = knowledge.search(connect(db_path), ctx, query, document_id or None)
            for c in result.get("chunks", []):
                tracker["chunks"].add(c["chunk_id"])
            for i in result.get("images", []):
                tracker["images"].add(i["image_occurrence_id"])
            return json.dumps(result, ensure_ascii=False)

        def inspect_image(image_occurrence_id: str) -> list[dict]:
            """查看 search_knowledge 命中的图片的完整原图,核对图内标签、箭头、数值与空间关系。
            传入证据中的 image_occurrence_id。"""
            blocks = knowledge.image_content_blocks(connect(db_path), ctx, image_occurrence_id)
            if any(b.get("type") == "image" for b in blocks):
                tracker["images"].add(image_occurrence_id)
            return blocks

        return [search_knowledge, inspect_image], tracker

    def _assemble(self, conn: Connection, ctx: AuthContext, answer_text: str,
                  question: str, tracker: dict,
                  extra_chunk_ids: set[str], extra_image_ids: set[str]) -> dict:
        """后端校验:仅接受本轮工具提供(或前几轮已引用过)且属于当前租户的 ID。

        extra_*_ids 为多轮历史中 assistant 引用过的 ID;
        最终存在性与租户归属由 build_image_entries / get_chunks 把关。
        """
        allowed_chunks = tracker["chunks"] | extra_chunk_ids
        allowed_images = tracker["images"] | extra_image_ids

        cited_chunks = [c for c in dict.fromkeys(_CITE_CHUNK_RE.findall(answer_text)) if c in allowed_chunks]
        cited_images = [i for i in dict.fromkeys(_CITE_IMAGE_RE.findall(answer_text)) if i in allowed_images]
        dropped = [i for i in _CITE_IMAGE_RE.findall(answer_text) if i not in allowed_images]
        if dropped:
            logger.warning("模型引用了未由工具提供的图片 ID,已丢弃: %s", dropped)

        citations = []
        for chunk in repo.get_chunks(conn, cited_chunks):
            if chunk["tenant_id"] == ctx.tenant_id:
                citations.append({"document_id": chunk["document_id"], "chunk_id": chunk["id"],
                                  "page": chunk["page_start"]})

        # 用户明确要求看图而模型未引用时,附带本轮证据中的图片候选
        if not cited_images and asks_for_images(question):
            cited_images = list(tracker["images"])[:3]

        images = self.knowledge.build_image_entries(conn, ctx, cited_images)
        return {"answer": answer_text, "citations": citations, "images": images}


def _build_messages(question: str, document_id: str | None, history: list[dict] | None) -> list[dict]:
    messages: list[dict] = []
    for turn in (history or [])[-20:]:
        role, content = turn.get("role"), str(turn.get("content") or "")[:4000]
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    user_content = question
    if document_id:
        user_content = f"(目标文档 ID:{document_id})\n{question}"
    messages.append({"role": "user", "content": user_content})
    return messages


def _history_cited_ids(history: list[dict] | None) -> tuple[set[str], set[str]]:
    chunks: set[str] = set()
    images: set[str] = set()
    for turn in (history or []):
        if turn.get("role") == "assistant":
            content = str(turn.get("content") or "")
            chunks.update(_CITE_CHUNK_RE.findall(content))
            images.update(_CITE_IMAGE_RE.findall(content))
    return chunks, images


def _final_answer_text(messages: list) -> str:
    for message in reversed(messages):
        if getattr(message, "type", None) == "ai" or message.__class__.__name__ == "AIMessage":
            return _extract_text(message.content)
    return ""


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _extract_reasoning(additional_kwargs: dict) -> str:
    """K3 思考常开;OpenAI 兼容端点的推理增量一般在 reasoning_content 字段。"""
    value = additional_kwargs.get("reasoning_content")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            block.get("text", "") for block in value
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""
