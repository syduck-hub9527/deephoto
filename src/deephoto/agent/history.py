"""已完成轮次只延续用户问题与最终文本。运行中/待审批的工具消息不裁剪。"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content
                       if isinstance(block, dict) and block.get("type") == "text")
    return ""


def compact_messages(messages: list) -> list:
    """按 HumanMessage 分轮;只裁剪以无 tool_calls 的非空 AI 文本结束的轮次。"""
    result = []
    index = 0
    while index < len(messages):
        if not isinstance(messages[index], HumanMessage):
            # 非标准前导状态不猜测其语义,保留 System/summary 等消息。
            result.append(messages[index])
            index += 1
            continue
        end = index + 1
        while end < len(messages) and not isinstance(messages[end], HumanMessage):
            end += 1
        segment = messages[index:end]
        final = segment[-1]
        if (len(segment) > 1 and isinstance(final, AIMessage)
                and not final.tool_calls and not final.invalid_tool_calls and _text(final.content).strip()):
            # 最终回答本来就只输出文本。清掉 reasoning 等附带载荷,保留消息 ID/usage。
            final = final.model_copy(update={"content": _text(final.content),
                                             "additional_kwargs": {}, "response_metadata": {}})
            result.extend([segment[0], final])
        else:
            # 包含尚待执行/审批的 AI tool_calls 或部分工具结果时,全部保留。
            result.extend(segment)
        index = end
    return result


class CompletedRoundMiddleware(AgentMiddleware):
    """before_agent 处理升级前历史;after_agent 处理当前完成轮次。只在 03 开启时挂载。"""

    @staticmethod
    def _update(state):
        messages = state.get("messages", [])
        compacted = compact_messages(messages)
        if compacted == messages:
            return None
        update = {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *compacted]}
        if "_summarization_event" in state:
            # 固定版本摘要 event 按旧 messages 下标切片,删消息后不能沿用 cutoff。
            update["_summarization_event"] = None
        return update

    def before_agent(self, state, runtime):
        return self._update(state)

    def after_agent(self, state, runtime):
        return self._update(state)

    async def abefore_agent(self, state, runtime):
        return self._update(state)

    async def aafter_agent(self, state, runtime):
        return self._update(state)
