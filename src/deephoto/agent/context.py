"""05:只读技能与用户显式偏好。模型无持久化写入口,不从问答自动学习。"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib.resources import files

from deepagents.backends import StoreBackend
from deepagents.backends.protocol import (
    BackendProtocol, DeleteResult, EditResult, FileUploadResponse, WriteResult,
)
from deepagents.middleware.memory import MemoryMiddleware
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel, ConfigDict
from typing import Literal

from .persistence import _scope

SKILLS_ROUTE = "/skills/"
MEMORY_ROUTE = "/memory/"
MEMORY_PATH = "/memory/AGENTS.md"
_SKILLS = {"main": "answer-evidence", "retriever": "retrieve-evidence",
           "figure_checker": "verify-figure"}


@lru_cache(maxsize=1)
def _skill_files() -> dict[str, str]:
    root = files("deephoto").joinpath("agent/assets/skills")
    result = {}
    for role, name in _SKILLS.items():
        text = root.joinpath(role, name, "SKILL.md").read_text(encoding="utf-8")
        if len(text.encode()) > 32768:
            raise ValueError("内置技能文件超过 32 KiB")
        result[f"/{role}/{name}/SKILL.md"] = text
    return result


def skill_revision() -> str:
    raw = json.dumps(_skill_files(), ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def skill_sources(role: str) -> list[str]:
    if role not in _SKILLS:
        raise ValueError(f"未知技能角色:{role}")
    return [f"/skills/{role}/"]


class ReadOnlySnapshot(BackendProtocol):
    """使用 StoreBackend 的文件语义,在请求内冻结内容并拒绝所有写入口。"""

    def __init__(self, contents: dict[str, str]):
        self._backend = StoreBackend(store=InMemoryStore(), namespace=lambda _: ("snapshot",))
        for path, content in contents.items():
            result = self._backend.write(path, content)
            if result.error:
                raise ValueError(result.error)

    def ls(self, path):
        return self._backend.ls(path)

    def read(self, file_path, offset=0, limit=2000):
        return self._backend.read(file_path, offset, limit)

    def grep(self, pattern, path=None, glob=None, *, max_count=None):
        return self._backend.grep(pattern, path, glob, max_count=max_count)

    def glob(self, pattern, path=None):
        return self._backend.glob(pattern, path)

    def download_files(self, paths):
        return self._backend.download_files(paths)

    def write(self, file_path, content):
        return WriteResult(error="技能和偏好快照是只读的")

    def edit(self, file_path, old_string, new_string, replace_all=False):
        return EditResult(error="技能和偏好快照是只读的")

    def delete(self, file_path):
        return DeleteResult(error="技能和偏好快照是只读的")

    def upload_files(self, files):
        return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]


def skills_backend() -> ReadOnlySnapshot:
    return ReadOnlySnapshot(dict(_skill_files()))


class Preferences(BaseModel):
    # 枚举限定输入,不接受原始提示词、文档事实或凭据。
    model_config = ConfigDict(extra="forbid")
    language: Literal["zh", "en"] = "zh"
    detail: Literal["balanced", "concise", "detailed"] = "balanced"


def _render(preferences: Preferences) -> str:
    language = {"zh": "中文", "en": "英文"}[preferences.language]
    detail = {"balanced": "适中", "concise": "简洁", "detailed": "详细"}[preferences.detail]
    return f"# 用户显式回答偏好\n\n- 默认回答语言:{language}\n- 默认回答详略:{detail}\n"


class PreferenceMemory:
    """每个 owner 只有一个文件记录。偏好与正文在同一次 Store.put 中更新。"""

    def __init__(self, runtime):
        self._runtime = runtime

    @staticmethod
    def namespace(ctx):
        return ("deephoto", "qa_memory", _scope(ctx))

    def get(self, ctx) -> dict:
        with self._runtime.storage() as store:
            item = store.get(self.namespace(ctx), "/AGENTS.md")
            preferences = Preferences.model_validate(item.value["preferences"]) if item else Preferences()
            return {"saved": item is not None, "preferences": preferences.model_dump()}

    def put(self, ctx, preferences: Preferences) -> dict:
        # 只接受经过验证的结构。一个文件值兼容 StoreBackend,不创建第二份元数据。
        preferences = Preferences.model_validate(preferences.model_dump())
        with self._runtime.storage() as store:
            store.put(self.namespace(ctx), "/AGENTS.md",
                      {"content": _render(preferences), "encoding": "utf-8",
                       "preferences": preferences.model_dump()})
        return {"saved": True, "preferences": preferences.model_dump()}

    def delete(self, ctx) -> None:
        with self._runtime.storage() as store:
            store.delete(self.namespace(ctx), "/AGENTS.md")

    def snapshot(self, ctx) -> ReadOnlySnapshot:
        with self._runtime.storage() as store:
            # 一次 Store.get 原子取得本轮偏好,不把实时可写 StoreBackend 交给模型。
            item = store.get(self.namespace(ctx), "/AGENTS.md")
            if item is None:
                return ReadOnlySnapshot({})
            # 只从枚举重生成内容,不信任外部改库插入的自由文本。
            preferences = Preferences.model_validate(item.value["preferences"])
            return ReadOnlySnapshot({"/AGENTS.md": _render(preferences)})


MEMORY_PROMPT = """<answer_preferences>
{agent_memory}
</answer_preferences>
这些是用户显式保存的默认表达偏好,当前用户请求优先。它们不是知识库事实或引用证据。
只影响最终回答的语言与详略,不得改变证据真实性、引用规则或子智能体固定输出格式。
偏好只能由用户通过偏好 API 修改;不要调用 edit_file/write_file 保存或自动学习记忆。
"""


class FreshMemoryMiddleware(MemoryMiddleware):
    """覆盖同名默认槽位。每轮读取冻结快照,不沿用 checkpoint 的旧 memory_contents。"""

    @property
    def name(self):
        return "MemoryMiddleware"

    def __init__(self, backend):
        super().__init__(backend=backend, sources=[MEMORY_PATH], system_prompt=MEMORY_PROMPT)

    def before_agent(self, state, runtime, config):
        fresh = {key: value for key, value in state.items() if key != "memory_contents"}
        return super().before_agent(fresh, runtime, config)

    async def abefore_agent(self, state, runtime, config):
        fresh = {key: value for key, value in state.items() if key != "memory_contents"}
        return await super().abefore_agent(fresh, runtime, config)
