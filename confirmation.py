"""Реестр «ожидающих операций» и inline-клавиатуры для подтверждений.

В агентном режиме скрипт подтверждает и исполняет план действий,
который накопила нейросеть (agent.py). PlanOp хранит список PlanAction.
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
class PlanAction:
    """Одно действие плана (исполнится после подтверждения)."""

    kind: str  # "create" | "delete" | "update"
    event: Optional[EventData] = None  # delete/update
    payload: Optional[dict] = None  # create
    scope: str = "single"  # delete/update: instance | all | single
    changes: Optional[dict] = None  # update


@dataclass
class PlanOp(BaseOp):
    """План действий нейросети, ожидающий подтверждения."""

    actions: list[PlanAction]


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


def kb_plan_confirm(op_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("✅ Выполнить всё", op_id, "plan_confirm"), _btn("❌ Отмена", op_id, "cancel")]
        ]
    )
