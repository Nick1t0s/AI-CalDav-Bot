"""Реестр «ожидающих операций» и inline-клавиатуры для подтверждений.

Подтверждение выполняет скрипт (а не ИИ): каждая операция регистрируется,
а по клику на кнопку выполняется соответствующий CalDAV-запрос.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from caldav_service import EventData

CALLBACK_PREFIX = "op:"
OP_TTL_SECONDS = 15 * 60


@dataclass
class BaseOp:
    user_id: int


@dataclass
class DeleteOp(BaseOp):
    """Первый шаг удаления: выбор «только вхождение / все серии»."""

    event: EventData


@dataclass
class DeleteConfirmOp(BaseOp):
    """Финальное подтверждение удаления (деструктив всегда двухшаговый)."""

    event: EventData
    delete_all: bool


@dataclass
class CandidateOp(BaseOp):
    """Выбор одного из нескольких найденных событий."""

    kind: str  # "delete" | "update"
    events: list[EventData]
    changes: Optional[dict] = None
    apply_to: Optional[str] = None


@dataclass
class UpdateScopeOp(BaseOp):
    """Выбор области правки повторяющегося события."""

    event: EventData
    changes: dict


@dataclass
class UpdateOp(BaseOp):
    """Превью правки + подтверждение."""

    event: EventData
    changes: dict
    scope: str  # "instance" | "all" | "single"


@dataclass
class CreateOp(BaseOp):
    """Превью создания + подтверждение."""

    payload: dict


PENDING: dict[str, BaseOp] = {}
_CREATED_AT: dict[str, float] = {}


def register(op: BaseOp) -> str:
    op_id = uuid.uuid4().hex[:12]
    PENDING[op_id] = op
    _CREATED_AT[op_id] = time.time()
    return op_id


def get(op_id: str) -> Optional[BaseOp]:
    return PENDING.get(op_id)


def consume(op_id: str) -> Optional[BaseOp]:
    _CREATED_AT.pop(op_id, None)
    return PENDING.pop(op_id, None)


def cleanup_expired() -> None:
    now = time.time()
    for op_id, op in list(PENDING.items()):
        if now - _CREATED_AT.get(op_id, 0) > OP_TTL_SECONDS:
            PENDING.pop(op_id, None)
            _CREATED_AT.pop(op_id, None)


# ---------- клавиатуры ----------


def _btn(text: str, op_id: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f"{CALLBACK_PREFIX}{op_id}:{action}")


def kb_delete_options(op_id: str, is_recurring: bool) -> InlineKeyboardMarkup:
    rows = []
    if is_recurring:
        rows.append([_btn("🗑 Только это вхождение", op_id, "instance")])
        rows.append([_btn("🗑 Все серии", op_id, "all")])
    else:
        rows.append([_btn("🗑 Удалить", op_id, "instance")])
    rows.append([_btn("❌ Отмена", op_id, "cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_delete_confirm(op_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("✅ Точно удалить", op_id, "confirm"), _btn("❌ Отмена", op_id, "cancel")]
        ]
    )


def kb_scope(op_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("📝 Только это вхождение", op_id, "scope:0"), _btn("📝 Все серии", op_id, "scope:1")],
            [_btn("❌ Отмена", op_id, "cancel")],
        ]
    )


def kb_update_confirm(op_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("✅ Применить", op_id, "apply"), _btn("❌ Отмена", op_id, "cancel")]
        ]
    )


def kb_create_confirm(op_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("✅ Создать", op_id, "create"), _btn("❌ Отмена", op_id, "cancel")]
        ]
    )


def kb_pick(op_id: str, events: list[EventData]) -> InlineKeyboardMarkup:
    rows = []
    for i, ev in enumerate(events):
        label = ev.summary
        if not ev.all_day:
            label += f" · {ev.start:%H:%M}"
        if ev.is_recurring:
            label += " 🔁"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:60], callback_data=f"{CALLBACK_PREFIX}{op_id}:pick:{i}"
                )
            ]
        )
    rows.append([_btn("❌ Отмена", op_id, "cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
