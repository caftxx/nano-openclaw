"""Todo store — agent-managed in-session task list.

模型可读写的会话内任务列表，关键能力：
- 单一工具 `todo`：传 `todos` 参数 = 写，省略 = 读
- 状态枚举 `pending / in_progress / completed / cancelled`
- merge 模式：按 id 增量更新 + 追加，否则全替换
- compact 后通过 `format_for_injection` 把活跃项重注入到 history

设计参考 hermes-agent/tools/todo_tool.py，去掉 hermes 私有 registry 调用，
保留行为 / schema 描述。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}


@dataclass
class TodoStore:
    """One instance per session/conversation.

    Items 是有序列表（位置 = 优先级）。每个 item 形如：
    ``{"id": str, "content": str, "status": str}``。
    """

    _items: list[dict[str, str]] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    def write(
        self,
        todos: list[dict[str, Any]],
        merge: bool = False,
    ) -> list[dict[str, str]]:
        """Write todos. 返回写入后的完整列表。

        - ``merge=False``: 整体替换为传入项（dedup by id，每 id 保留最后一次）
        - ``merge=True``: 按 id 更新已存在项；新项追加到末尾
        """
        if not isinstance(todos, list):
            raise ValueError("todos must be a list")

        if not merge:
            self._items = [self._validate(t) for t in self._dedupe_by_id(todos)]
            return self.read()

        # Merge mode
        existing = {item["id"]: item for item in self._items}
        for raw in self._dedupe_by_id(todos):
            item_id = str(raw.get("id", "")).strip()
            if not item_id:
                continue  # 无 id 没法 merge

            if item_id in existing:
                # 只更新模型实际提供的字段
                if "content" in raw and raw["content"]:
                    existing[item_id]["content"] = str(raw["content"]).strip()
                if "status" in raw and raw["status"]:
                    status = str(raw["status"]).strip().lower()
                    if status in VALID_STATUSES:
                        existing[item_id]["status"] = status
            else:
                validated = self._validate(raw)
                existing[validated["id"]] = validated
                self._items.append(validated)

        # 重建保持原顺序
        seen: set[str] = set()
        rebuilt: list[dict[str, str]] = []
        for item in self._items:
            current = existing.get(item["id"], item)
            if current["id"] not in seen:
                rebuilt.append(current)
                seen.add(current["id"])
        self._items = rebuilt
        return self.read()

    def read(self) -> list[dict[str, str]]:
        """返回当前列表的副本（避免调用方误改内部 state）。"""
        return [item.copy() for item in self._items]

    def has_items(self) -> bool:
        return bool(self._items)

    # ------------------------------------------------------------------
    # Compact-time injection
    # ------------------------------------------------------------------
    def format_for_injection(self) -> Optional[str]:
        """渲染活跃任务给压缩后的 history 用。

        只输出 ``pending`` / ``in_progress`` —— ``completed`` / ``cancelled``
        留在压缩里会让模型重复完成工作，弊大于利。

        无活跃项返回 ``None``，由 caller 决定是否注入。
        """
        if not self._items:
            return None

        markers = {
            "completed": "[x]",
            "in_progress": "[>]",
            "pending": "[ ]",
            "cancelled": "[~]",
        }

        active = [
            item
            for item in self._items
            if item["status"] in ("pending", "in_progress")
        ]
        if not active:
            return None

        lines = ["[Your active task list was preserved across context compression]"]
        for item in active:
            marker = markers.get(item["status"], "[?]")
            lines.append(
                f"- {marker} {item['id']}. {item['content']} ({item['status']})"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def to_json(self) -> list[dict[str, str]]:
        """序列化为 JSON 兼容形态（用于 sessions.json）。"""
        return self.read()

    @classmethod
    def from_json(cls, data: Any) -> "TodoStore":
        """从持久化数据还原 store。data 应是 ``to_json`` 输出，
        但也容忍 ``None`` / 非 list / 单项不完整。"""
        store = cls()
        if not isinstance(data, list):
            return store
        validated = [store._validate(item) for item in data if isinstance(item, dict)]
        # dedup by id 保持最后一次
        seen_ids: dict[str, int] = {}
        for idx, item in enumerate(validated):
            seen_ids[item["id"]] = idx
        store._items = [validated[i] for i in sorted(seen_ids.values())]
        return store

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _validate(item: dict[str, Any]) -> dict[str, str]:
        """Normalize & validate a single item; 非法 status 回退到 pending。"""
        item_id = str(item.get("id", "")).strip() or "?"
        content = str(item.get("content", "")).strip() or "(no description)"
        status = str(item.get("status", "pending")).strip().lower()
        if status not in VALID_STATUSES:
            status = "pending"
        return {"id": item_id, "content": content, "status": status}

    @staticmethod
    def _dedupe_by_id(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """折叠重复 id —— 保留最后一次出现，位置取最后一次的位置。"""
        last_index: dict[str, int] = {}
        for i, item in enumerate(todos):
            item_id = str(item.get("id", "")).strip() or "?"
            last_index[item_id] = i
        return [todos[i] for i in sorted(last_index.values())]
