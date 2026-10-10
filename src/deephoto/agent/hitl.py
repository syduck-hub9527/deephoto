"""06:inspect_image 人工审批闸门(开发文档 06-human-in-the-loop)。

每轮前 N 次 inspect_image 自动放行,超出的调用经 HumanInTheLoopMiddleware 暂停,
等待人工 approve / reject(不开放 edit/respond:改掉 image_occurrence_id 会变成
"批准 A、实际看 B")。

框架事实依据(tests/test_deepagents_contract.py 的 HitlDeepContract 逐条钉住):
- U2:恢复时 after_model 整体重跑,when 谓词对同一 tool_call_id 再次求值
  -> 决定必须按 tool_call_id 记忆,重放时结论不变、计数不翻倍;
- F11:子智能体内的谓词看不到父级消息 -> 计数只能放在请求级 gate 对象里,
  不能从 state 推导;
- F6:同一条 AI 消息的多个受闸门调用合并为一次中断,决定顺序 = action_requests 顺序;
- F7:决定数不匹配会毁掉挂起的线程 -> resume 前必须在图外完成全部校验(5.7)。
"""

from __future__ import annotations

from threading import Lock

from langchain.agents.middleware.types import ToolCallRequest

from .. import repo
from ..db import connect

# 决定取值:"auto" 预算内自动放行;"ask" 暂停等待人工
_AUTO, _ASK = "auto", "ask"

MAX_REJECT_MESSAGE = 200          # reject 可选留言的长度上限(5.7)
DECISION_TYPES = ("approve", "reject")


class ImageApprovalGate:
    """每个请求(一轮问答)一个实例。决定按 tool_call_id 记录,恢复重放时复用(U2)。

    计数的是**决定**,不是执行结果:同一条 AI 消息里的多个调用按顺序各自决定,
    不会因为都以 0 计数而同时放行(F6)。锁用于并行子智能体:并行的 task 调用
    可能让 when 并发执行(U1)。
    """

    def __init__(self, auto_budget: int, decided: dict[str, str] | None = None):
        self._budget = auto_budget
        self._decided: dict[str, str] = dict(decided or {})
        self._lock = Lock()

    def when(self, req: ToolCallRequest) -> bool:
        """True = 暂停等待人工;False = 自动放行。"""
        call_id = req.tool_call["id"]
        with self._lock:
            if call_id not in self._decided:
                used = sum(1 for v in self._decided.values() if v == _AUTO)
                self._decided[call_id] = _AUTO if used < self._budget else _ASK
            return self._decided[call_id] == _ASK

    def snapshot(self) -> dict[str, str]:
        """随挂起元数据持久化;恢复时用同一个 decided 重建,保证重放结论一致。"""
        with self._lock:
            return dict(self._decided)


def describe_image_request(db_path, ctx, tool_call, _state=None, _runtime=None) -> str:
    """人可读的审批描述,如"查看原图 occ_x(图 3,第 5 页)";不含图片字节。

    回调在图节点线程中执行(U3),遵守 qa.py 文件头的线程纪律:在使用点经 connect()
    自取连接(thread-local 缓存,**不得关闭**,与 qa.py 的工具同一用法)。
    描述只是展示,不是权限控制;元数据不存在或归属其他租户时退化为通用文案,
    实际的执行权限仍由 inspect_image 工具与后端校验把关。
    """
    occ_id = str(tool_call["args"].get("image_occurrence_id", "") or "")
    occ = repo.get_occurrence(connect(db_path), occ_id) if occ_id else None
    if not occ or occ["tenant_id"] != ctx.tenant_id:
        return f"查看原图 {occ_id or '(未知图片)'}"
    parts = []
    if occ.get("figure_number"):
        parts.append(f"图 {occ['figure_number']}")
    if occ.get("page_number"):
        parts.append(f"第 {occ['page_number']} 页")
    caption = (occ.get("caption") or "").strip()
    if caption:
        parts.append(caption[:50])
    suffix = f"({', '.join(parts)})" if parts else ""
    return f"查看原图 {occ_id}{suffix}"


def approval_items(interrupts) -> list[dict]:
    """把 __interrupt__ 展开为审批项列表;approval_id 按出现顺序编号(ap_1、ap_2…)。

    index 是该调用在其所属 interrupt 的 action_requests 中的位置(F6 批次);
    resume 时按 interrupt_id 分组、组内按 index 排序还原决定列表。
    """
    items = []
    for interrupt in interrupts:
        for index, action in enumerate(interrupt.value.get("action_requests", [])):
            items.append({
                "approval_id": f"ap_{len(items) + 1}",
                "interrupt_id": interrupt.id,
                "index": index,
                "tool": action.get("name"),
                "args": dict(action.get("args") or {}),
                "description": action.get("description", ""),
            })
    return items


class ApprovalError(ValueError):
    """审批请求校验失败(HTTP 400)。任何一条不通过都不得触碰图(F7)。"""


def validate_decisions(pending: list[dict], decisions) -> list[dict]:
    """图外校验并规范化决定(5.7)。通过时返回按 pending 顺序排列、附带 interrupt_id/index
    的决定列表;任何一条不通过抛 ApprovalError。

    - approval_id 集合与待审批集合完全相等:不缺、不重、不多;
    - type 只能是 approve 或 reject;message 仅 reject 可带,长度 <= 200。
    """
    if not isinstance(decisions, list):
        raise ApprovalError("decisions 必须是数组")
    by_id = {}
    for i, raw in enumerate(decisions):
        if not isinstance(raw, dict):
            raise ApprovalError(f"decisions[{i}] 必须是对象")
        approval_id = raw.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id:
            raise ApprovalError(f"decisions[{i}] 缺少 approval_id")
        if approval_id in by_id:
            raise ApprovalError(f"审批项重复: {approval_id}")
        dtype = raw.get("type")
        if dtype not in DECISION_TYPES:
            raise ApprovalError(f"decisions[{i}] 的 type 只能是 approve/reject")
        message = raw.get("message")
        if message is not None:
            if dtype != "reject":
                raise ApprovalError("只有 reject 可以带 message")
            if not isinstance(message, str) or len(message) > MAX_REJECT_MESSAGE:
                raise ApprovalError(f"message 必须是不超过 {MAX_REJECT_MESSAGE} 字的字符串")
        entry = {"type": dtype}
        if message:
            entry["message"] = message
        by_id[approval_id] = entry
    extra = set(by_id) - {p["approval_id"] for p in pending}
    if extra:
        raise ApprovalError(f"审批项不存在或已处理: {sorted(extra)}")
    missing = [p["approval_id"] for p in pending if p["approval_id"] not in by_id]
    if missing:
        raise ApprovalError(f"缺少审批决定: {missing}")
    normalized = []
    for p in pending:      # 保持待审批顺序:组内 index 升序,多中断按出现顺序(U1)
        normalized.append({**by_id[p["approval_id"]],
                           "interrupt_id": p["interrupt_id"], "index": p["index"]})
    return normalized


def build_resume_value(normalized: list[dict]):
    """构造 Command(resume=...) 的负载:单中断用 {"decisions": [...]}(F4),
    多中断用 {interrupt_id: {"decisions": [...]}} 按 id 映射(U1)。"""
    if not normalized:
        raise ApprovalError("没有待审批的调用")
    grouped: dict[str, list[dict]] = {}
    for item in normalized:
        grouped.setdefault(item["interrupt_id"], []).append(
            {k: v for k, v in item.items() if k in ("type", "message")})
    payloads = {iid: {"decisions": decisions} for iid, decisions in grouped.items()}
    if len(payloads) == 1:
        return next(iter(payloads.values()))
    return payloads
