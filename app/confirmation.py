"""Реестр «ожидающих операций» и inline-клавиатуры для подтверждений.

В агентном режиме скрипт подтверждает и исполняет план действий,
который накопила нейросеть (agent.py). PlanOp хранит список PlanAction.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.caldav_service import EventData

CALLBACK_PREFIX = "op:"


@dataclass
class BaseOp:
    user_id: int


@dataclass
class PlanAction:
    """Одно действие плана (исполнится после подтверждения)."""

    kind: str  # "create" | "delete" | "exclude" | "update"
    event: Optional[EventData] = None  # delete/exclude/update
    payload: Optional[dict] = None  # create
    changes: Optional[dict] = None  # update: правки для целого события/серии


@dataclass
class PlanOp(BaseOp):
    """План действий нейросети, ожидающий подтверждения."""

    actions: list[PlanAction]


PENDING: dict[str, BaseOp] = {}


def register(op: BaseOp) -> str:
    op_id = uuid.uuid4().hex[:12]
    PENDING[op_id] = op
    return op_id


def get(op_id: str) -> Optional[BaseOp]:
    return PENDING.get(op_id)


def consume(op_id: str) -> Optional[BaseOp]:
    return PENDING.pop(op_id, None)


# ---------- клавиатуры ----------


def _btn(text: str, op_id: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f"{CALLBACK_PREFIX}{op_id}:{action}")


def kb_plan_confirm(op_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("✅ Выполнить всё", op_id, "plan_confirm"), _btn("❌ Отмена", op_id, "cancel")]
        ]
    )
