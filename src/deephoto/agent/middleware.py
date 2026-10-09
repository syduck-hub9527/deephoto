"""04:请求级工具观测与工具可见性。轨迹不是证据,引用仍由工具/VFS 的 tracker 校验。"""

from __future__ import annotations

from threading import Lock
from time import monotonic
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.types import Command


class ToolTrace:
    """多子智能体共享的有界轨迹;只存元数据,不存参数、正文、图像或异常文本。"""

    def __init__(self, capacity: int = 256):
        if capacity < 1:
            raise ValueError("capacity 必须 >= 1")
        self.request_id = uuid4().hex
        self._capacity = capacity
        self._lock = Lock()
        self._started = 0
        self._completed = 0
        self._events: list[dict] = []

    def begin(self) -> int:
        with self._lock:
            self._started += 1
            return self._started

    def finish(self, seq: int, *, agent: str, tool: str, call_id: str,
               started: float, outcome: str, error_type: str | None = None) -> None:
        with self._lock:
            self._completed += 1
            if seq <= self._capacity:
                self._events.append({
                    "seq": seq, "agent": agent, "tool": tool[:100], "call_id": call_id[:100],
                    "elapsed_ms": round((monotonic() - started) * 1000, 2),
                    "outcome": outcome, "error_type": error_type,
                })

    def snapshot(self) -> dict:
        with self._lock:
            events = [dict(e) for e in sorted(self._events, key=lambda e: e["seq"])]
            return {"request_id": self.request_id, "started": self._started,
                    "completed": self._completed, "dropped": max(0, self._started - self._capacity),
                    "events": events}


def _outcome(result) -> str:
    # Command 是合法的工具返回值(例如文件转存),不能包装/扁平化它。
    if isinstance(result, ToolMessage):
        return "tool_error" if result.status == "error" else "returned"
    if isinstance(result, Command) and isinstance(result.update, dict):
        if any(isinstance(m, ToolMessage) and m.status == "error"
               for m in result.update.get("messages", [])):
            return "tool_error"
    return "returned"


class ToolTraceMiddleware(AgentMiddleware):
    def __init__(self, trace: ToolTrace, agent: str):
        self.trace = trace
        self.agent = agent

    def _finish(self, request, seq, started, outcome, error_type=None):
        self.trace.finish(seq, agent=self.agent, tool=request.tool_call["name"],
                          call_id=request.tool_call.get("id") or "", started=started,
                          outcome=outcome, error_type=error_type)

    def wrap_tool_call(self, request, handler):
        seq, started = self.trace.begin(), monotonic()
        try:
            result = handler(request)
        except Exception as exc:
            self._finish(request, seq, started, "exception", type(exc).__name__)
            raise
        self._finish(request, seq, started, _outcome(result))
        return result

    async def awrap_tool_call(self, request, handler):
        seq, started = self.trace.begin(), monotonic()
        try:
            result = await handler(request)
        except Exception as exc:
            self._finish(request, seq, started, "exception", type(exc).__name__)
            raise
        self._finish(request, seq, started, _outcome(result))
        return result


def _tool_name(tool) -> str | None:
    if isinstance(tool, dict):
        name = tool.get("name") or tool.get("function", {}).get("name")
    else:
        name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


class HideToolsMiddleware(AgentMiddleware):
    """保持 02 行为:仅从模型请求中去掉工具,不承担执行边界/权限校验。"""

    def __init__(self, names):
        self.hidden = frozenset(names)

    def _request(self, request):
        return request.override(tools=[t for t in request.tools if _tool_name(t) not in self.hidden])

    def wrap_model_call(self, request, handler):
        return handler(self._request(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._request(request))
