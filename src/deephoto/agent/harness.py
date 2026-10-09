"""HarnessProfile 注册与"对模型隐藏工具"的统一入口(开发文档 01 §3.4 + 02 §4.3)。

为什么单独成模块:deepagents 的 excluded_tools 在**重复注册时取并集**,而且注册是进程内全局的,
一旦排除了 read_file/grep 就无法再放开。01(委派模式)与 02(知识库文件系统)对同一个
"openai:<chat_model>" key 的需求不同,所以必须由**一处**根据两个开关算出最终排除集合并只注册一次。
"""

from __future__ import annotations

from typing import Any

# 内置文件工具:读类 / 写类
READ_TOOLS = frozenset({"ls", "read_file", "glob", "grep"})
WRITE_TOOLS = frozenset({"write_file", "edit_file", "delete", "execute"})
ALL_FS_TOOLS = READ_TOOLS | WRITE_TOOLS

# model key -> (delegating, kb_vfs);用于发现"同进程内用不同配置重复注册"这类无法撤销的冲突
_REGISTERED: dict[str, tuple[bool, bool]] = {}


def excluded_tools(*, delegating: bool, kb_vfs: bool) -> frozenset[str]:
    if kb_vfs:
        return WRITE_TOOLS                     # 读类工具留给 /kb/;写类对模型隐藏(后端本身也拒写)
    return ALL_FS_TOOLS if delegating else frozenset()


def register_harness(chat_model: str, *, delegating: bool, kb_vfs: bool) -> None:
    """按 openai:<chat_model> 注册 profile。

    - 单智能体且未启用 kb:不注册(与升级前完全一致);
    - 委派模式:关闭 general-purpose 子智能体;
    - 同一进程里对同一 key 换配置重复注册会抛 RuntimeError(并集无法撤销,需要重启进程)。
    """
    key = f"openai:{chat_model}"
    flags = (delegating, kb_vfs)
    if key in _REGISTERED:
        if _REGISTERED[key] != flags:
            raise RuntimeError(
                f"{key} 已按 delegating/kb_vfs={_REGISTERED[key]} 注册;excluded_tools 只增不减,"
                f"不能在同一进程里改成 {flags},请重启进程。")
        return
    excluded = excluded_tools(delegating=delegating, kb_vfs=kb_vfs)
    if not excluded and not delegating:
        _REGISTERED[key] = flags
        return

    from deepagents import HarnessProfile, register_harness_profile
    from deepagents.profiles import GeneralPurposeSubagentProfile

    kwargs: dict[str, Any] = {"excluded_tools": excluded}
    if delegating:
        kwargs["general_purpose_subagent"] = GeneralPurposeSubagentProfile(enabled=False)
    register_harness_profile(key, HarnessProfile(**kwargs))
    _REGISTERED[key] = flags


def hide_tools_middleware(names: frozenset[str] | set[str]):
    """只对**某一个**智能体隐藏工具(profile 是按模型全局生效的,做不到"主智能体看不到、子智能体看得到")。

    注意:这只是从模型请求里摘掉工具定义;工具仍注册在执行节点上。模型看不到就不会调用。
    """
    from langchain.agents.middleware import AgentMiddleware

    hidden = frozenset(names)

    def _tool_name(t: Any) -> str | None:
        n = t.get("name") if isinstance(t, dict) else getattr(t, "name", None)
        return n if isinstance(n, str) else None

    class HideToolsMiddleware(AgentMiddleware):
        def wrap_model_call(self, request, handler):
            return handler(request.override(
                tools=[t for t in request.tools if _tool_name(t) not in hidden]))

        async def awrap_model_call(self, request, handler):
            return await handler(request.override(
                tools=[t for t in request.tools if _tool_name(t) not in hidden]))

    return HideToolsMiddleware()
