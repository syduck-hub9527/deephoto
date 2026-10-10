"""离线假模型:契约/委派测试用,不依赖真实 API(md文档 00 §4.1 B3、01-subagents §5.4)。

ScriptedFakeChatModel 按脚本逐项出招:
- "任意文本" 或 {"text": "..."}        -> 产生最终回答(流式时切成多块);
- {"tool": "<name>", "args": {...}}   -> 产生一轮工具调用;
- {"tools": [{"name": ..., "args": ...}, ...]} -> 同一条 AI 消息里并发多个工具调用(06 HITL 批次/并行中断用)。

脚本耗尽后的行为由 repeat_last 决定:False 返回兜底文本让图收敛(默认);
True 重复最后一项,用于"子智能体死循环被调用上限截断"这类测试。

模型发起的每次请求(消息列表)与每次 bind_tools 见到的工具名都被记录,
供契约断言使用。设 model_name/ls_provider 后,deepagents 的 HarnessProfile
按 "<ls_provider>:<model_name>" 匹配(如 openai:fake-k3),用于验证
01-subagents 的工具排除与 general-purpose 关闭。

出招游标有锁保护,并行子智能体(06 U1)可以并发消费脚本;同一并发批次内
哪一方先拿到下一项不确定,脚本应写成与顺序无关的形式。
"""

from __future__ import annotations

import json
from threading import Lock
from typing import Any, Iterator, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import BaseTool
from pydantic import PrivateAttr

# 流式时每块的字符数:多块才能验证 qa.py 的 token 拼接路径
STREAM_CHUNK_SIZE = 12


class ScriptedFakeChatModel(BaseChatModel):
    """按脚本出招的假对话模型。不联网;游标有锁,可承受并行子智能体并发出招。"""

    script: list[Any]
    """逐项消费的出招脚本:str / {"text": ...} / {"tool": name, "args": {...}}。"""

    model_name: str | None = None
    """设置后作为 HarnessProfile 匹配的 identifier(get_model_identifier 读 model_name)。"""

    ls_provider: str | None = None
    """设置后覆盖 _get_ls_params 的 ls_provider(如 "openai"),用于匹配 HarnessProfile。"""

    repeat_last: bool = False
    """脚本耗尽后重复最后一项(死循环测试);False 则返回兜底文本收尾。"""

    _cursor: int = PrivateAttr(default=0)
    _requests: list[list[BaseMessage]] = PrivateAttr(default_factory=list)
    _bound_tool_names: list[list[str]] = PrivateAttr(default_factory=list)
    _call_seq: int = PrivateAttr(default=0)
    _script_lock: Lock = PrivateAttr(default_factory=Lock)

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def _get_ls_params(self, stop: list[str] | None = None, **kwargs: Any) -> dict:
        params = super()._get_ls_params(stop=stop, **kwargs)
        if self.ls_provider:
            params["ls_provider"] = self.ls_provider
        if self.model_name:
            params["ls_model_name"] = self.model_name
        return params

    # ---- 观测点 ----

    @property
    def requests(self) -> list[list[BaseMessage]]:
        """历次模型请求的消息列表(系统提示在 messages[0])。"""
        return self._requests

    @property
    def bound_tool_names(self) -> list[list[str]]:
        """历次 bind_tools 收到的工具名(最后一次即当前模型可见集合)。"""
        return self._bound_tool_names

    @property
    def calls_made(self) -> int:
        """模型被调用的次数(脚本游标;repeat_last 下越过末尾也继续计)。"""
        return self._cursor

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedFakeChatModel":
        names = []
        for t in tools:
            if isinstance(t, BaseTool):
                names.append(t.name)
            elif isinstance(t, dict):      # OpenAI 风格的 dict 工具
                names.append(str(t.get("function", {}).get("name") or t.get("name")))
            else:
                names.append(str(getattr(t, "name", t)))
        self._bound_tool_names.append(names)
        return self  # 假模型无需真绑定;返回自身保持 langchain 的链式用法

    # ---- 出招 ----

    def _next_message(self) -> AIMessage:
        with self._script_lock:
            if self._cursor < len(self.script):
                step = self.script[self._cursor]
            elif self.repeat_last and self.script:
                step = self.script[-1]
            else:
                step = {"text": "（假模型脚本已耗尽,兜底收尾）"}
            self._cursor += 1
            if isinstance(step, str):
                step = {"text": step}
            tool_items = []
            if "tool" in step:
                tool_items = [{"name": step["tool"], "args": step.get("args", {})}]
            elif "tools" in step:
                tool_items = [{"name": t["name"], "args": t.get("args", {})} for t in step["tools"]]
            if tool_items:
                calls = []
                for item in tool_items:
                    self._call_seq += 1
                    calls.append({"name": item["name"], "args": item["args"], "id": f"call_{self._call_seq}"})
                return AIMessage(content="", tool_calls=calls)
            return AIMessage(content=str(step["text"]))

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._requests.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self._next_message())])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        self._requests.append(list(messages))
        msg = self._next_message()
        if msg.tool_calls:
            # 工具调用轮:每个调用一个 tool_call_chunks 块流出(args 为 JSON 字符串)
            for index, call in enumerate(msg.tool_calls):
                chunk = AIMessageChunk(
                    content="",
                    tool_call_chunks=[{
                        "name": call["name"],
                        "args": json.dumps(call["args"], ensure_ascii=False),
                        "id": call["id"],
                        "index": index,
                    }],
                )
                yield ChatGenerationChunk(message=chunk)
            return
        text = str(msg.content)
        for i in range(0, max(len(text), 1), STREAM_CHUNK_SIZE):
            yield ChatGenerationChunk(message=AIMessageChunk(content=text[i:i + STREAM_CHUNK_SIZE]))
