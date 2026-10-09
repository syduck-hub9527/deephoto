"""委派模式:主智能体只做协调与撰写,检索/看图交给子智能体(开发文档 01-subagents)。

设计要点(均经 deepagents 0.7.19 离线实测):
- 子智能体是 isolated 模式:只收到 task 的 description,看不到对话历史与父智能体消息;
- 子智能体返回给主智能体的只有它**最后一条非空 AI 文本**,其内部的工具结果(含图片块)不会回传;
- 工具是同一批闭包,tracker 副作用在子智能体里照样记录,_assemble 的校验无需改动;
- 父级 recursion_limit 不会传给子智能体(子智能体自带 9999),必须用 ModelCallLimitMiddleware 另行限制;
- 默认会注入 general-purpose 子智能体与 ls/read_file/write_file/... 内置文件工具,
  这里通过 HarnessProfile 关掉(profile 按 "provider:model" 匹配,进程内全局)。
"""

from __future__ import annotations

from typing import Callable, Sequence

from .qa import CITATION_AND_FORMAT_RULES, KB_USAGE_RULES

_FS_TOOLS = frozenset({"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute"})

RETRIEVER_NAME = "retriever"
FIGURE_CHECKER_NAME = "figure_checker"

RETRIEVER_DESCRIPTION = (
    "文档检索员:在用户有权访问的知识库里检索并读取全文,返回带 [chunk:ID] 的要点、原文摘录和候选图片 ID。"
    "description 必须自包含:写完整问题、需要的事实点、用户指定的 document_id(如有)。"
    "它看不到对话历史,也不会回答最终问题。"
)

FIGURE_CHECKER_DESCRIPTION = (
    "图像核验员:查看原图并核对图内标签、箭头、数值、颜色与空间关系。"
    "description 必须写清要核对的 image_occurrence_id(来自 retriever 的【候选图片】)和具体要核对的问题。"
)

RETRIEVER_PROMPT = (
    "你是文档检索员,只负责为上级收集证据,不回答最终问题。\n"
    "1. 先用 search_knowledge 检索(任务里给了 document_id 就传入,否则检索全部可访问文档);"
    "一次没找全可以换关键词再检索。\n"
    "2. 命中块 truncated 为 true 且与问题相关时,必须用 read_chunk 读全文;"
    "答案可能跨块或跨页时用 neighbors=1。不要只凭截断的开头下结论。\n"
    "3. 只能使用工具返回过的 ID;不得编造页码、图号、chunk_id 或 image_occurrence_id。\n"
    "4. 最终回复只能使用下面的固定格式(中文,总长不超过 1500 字),不要写其他内容:\n"
    "【要点】\n- 与问题相关的事实,每条末尾带 [chunk:chunk_id]\n"
    "【原文摘录】\n- [chunk:chunk_id] 位置:关键句原文(每条不超过 150 字)\n"
    "【候选图片】\n- [image:image_occurrence_id] 图号/图注/位置:与问题的关系(没有写“无”)\n"
    "【缺口】\n- 没找到或不确定的内容(没有写“无”)"
)

FIGURE_CHECKER_PROMPT = (
    "你是图像核验员。上级会给出要核对的 image_occurrence_id 和具体问题。\n"
    "1. 对任务点名的图片调用 inspect_image 查看完整原图(最多 3 张,按任务里的顺序)。\n"
    "2. 只陈述图中能直接看到的标签、箭头、数值、颜色、空间关系;看不清就写“无法确认”,不要推测。\n"
    "3. 只能使用任务里给出的或工具返回过的 image_occurrence_id,不得编造。\n"
    "4. 最终回复只能使用下面的固定格式(中文,总长不超过 800 字):\n"
    "【核验结果】\n- [image:image_occurrence_id] 图中观察:…;与问题的关系:…\n"
    "【无法确认】\n- …(没有写“无”)"
)

# 规则 1-3 换成委派规则;规则 4-8(引用、排版、公式)与单智能体模式共用同一常量
DELEGATING_RULES = (
    "你是文档知识库问答助手。你不能直接检索文档,必须通过 task 工具委派给子智能体,"
    "再基于它们返回的证据撰写最终回答。回答当前用户的问题时遵守:\n"
    f"1. 回答与文档内容有关的问题时,先调用 task(subagent_type=\"{RETRIEVER_NAME}\"):"
    "description 要自包含(子智能体看不到对话历史),写清完整问题、需要的事实点、用户指定的 document_id(如有)。"
    "互不相关的子问题可在同一轮并行委派多个。\n"
    "2. 当问题涉及图中的标签、箭头、数值、颜色或空间关系时,把检索员返回的【候选图片】中相关的 "
    f"image_occurrence_id 连同要核对的具体问题,委派给 task(subagent_type=\"{FIGURE_CHECKER_NAME}\");"
    "不要只凭图片的文字描述下结论。\n"
    "3. 子智能体返回的内容是你唯一的证据来源。结果为空、写明无法确认,或是“Model call limits exceeded”"
    "之类的超限提示时,如实告诉用户没有检索到可靠依据,不要编造。最终回答由你自己撰写,"
    "不要把子智能体的固定格式原样贴给用户。\n"
)

DELEGATING_SYSTEM_PROMPT = DELEGATING_RULES + CITATION_AND_FORMAT_RULES


def register_harness(chat_model: str, *, kb_vfs: bool = False) -> None:
    """委派模式的 profile 注册;实现在 harness.py(01 与 02 共用同一个排除集合计算)。"""
    from .harness import register_harness as _register

    _register(chat_model, delegating=True, kb_vfs=kb_vfs)


# 02:retriever 额外获得只读 /kb/ 文件系统时,把使用说明插在"固定格式"要求之前(RETRIEVER_PROMPT 本身不变)
_head, _sep, _tail = RETRIEVER_PROMPT.partition("4. 最终回复")
RETRIEVER_KB_PROMPT = _head + KB_USAGE_RULES + _sep + _tail


def _call_limit(n: int):
    from langchain.agents.middleware import ModelCallLimitMiddleware

    # 超限时子智能体以一条说明文本结束,task 工具把它当结果交回主智能体(提示词里已约定如何处理)
    return ModelCallLimitMiddleware(run_limit=n, exit_behavior="end")


def build_subagent_specs(tools: Sequence[Callable], *, retriever_max_calls: int = 8,
                         checker_max_calls: int = 4, kb_vfs: bool = False, trace=None) -> list[dict]:
    from .harness import READ_TOOLS, hide_tools_middleware

    by_name = {getattr(t, "__name__", getattr(t, "name", "")): t for t in tools}
    missing = {"search_knowledge", "read_chunk", "inspect_image"} - set(by_name)
    if missing:
        raise ValueError(f"缺少子智能体所需工具: {sorted(missing)}")

    def trace_middleware(role):
        if trace is None:
            return []
        from .middleware import ToolTraceMiddleware
        return [ToolTraceMiddleware(trace, role)]

    return [
        {
            "name": RETRIEVER_NAME,
            "description": RETRIEVER_DESCRIPTION,
            # kb_vfs 开启时 retriever 保留内置的 ls/read_file/glob/grep(写类由 profile 排除)
            "system_prompt": RETRIEVER_KB_PROMPT if kb_vfs else RETRIEVER_PROMPT,
            "tools": [by_name["search_knowledge"], by_name["read_chunk"]],
            "middleware": [_call_limit(retriever_max_calls)] + trace_middleware(RETRIEVER_NAME),
        },
        {
            "name": FIGURE_CHECKER_NAME,
            "description": FIGURE_CHECKER_DESCRIPTION,
            "system_prompt": FIGURE_CHECKER_PROMPT,
            "tools": [by_name["inspect_image"]],
            # 读类文件工具在 kb_vfs 下不再被 profile 排除,figure_checker 不需要它们
            "middleware": [_call_limit(checker_max_calls)]
            + trace_middleware(FIGURE_CHECKER_NAME)
            + ([hide_tools_middleware(READ_TOOLS)] if kb_vfs else []),
        },
    ]


def build_delegating_agent(model, tools: Sequence[Callable], *, retriever_max_calls: int = 8,
                           checker_max_calls: int = 4, backend=None, middleware=(), trace=None,
                           checkpointer=None, store=None):
    """backend 非 None 即启用 02 的知识库文件系统:retriever 可用 /kb/,主智能体仍只有 task。"""
    from deepagents import create_deep_agent

    from .harness import READ_TOOLS, hide_tools_middleware

    kb_vfs = backend is not None
    extra: dict = {}
    if checkpointer is not None:
        extra["checkpointer"] = checkpointer
    if store is not None:
        extra["store"] = store
    main_middleware = list(middleware)
    if kb_vfs:
        extra["backend"] = backend
        main_middleware.append(hide_tools_middleware(READ_TOOLS))
    if main_middleware:
        extra["middleware"] = main_middleware
    return create_deep_agent(
        model=model,
        tools=[],    # 业务工具全部下放给子智能体;主智能体只有 task
        system_prompt=DELEGATING_SYSTEM_PROMPT,
        subagents=build_subagent_specs(
            tools, retriever_max_calls=retriever_max_calls, checker_max_calls=checker_max_calls,
            kb_vfs=kb_vfs, trace=trace),
        **extra,
    )
