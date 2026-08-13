"""Реестр «ожидающих вопросов» (ask_user) и reply-клавиатуры вариантов.

Вопрос задаётся моделью через инструмент ask_user (agent.py): цикл ставится на
паузу, вопрос уходит в чат с reply-клавиатурой вариантов (KeyboardButton).
Ответ (кнопка или произвольный текст) подаётся модели как результат
инструмента, после чего цикл возобновляется.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

ASK_TTL_SECONDS = 15 * 60
ASK_CANCEL_LABEL = "❌ Отмена"

_BUTTON_LABEL_MAX = 40


@dataclass
class AskQ:
    """Ожидающий вопрос агента."""

    user_id: int
    tool_call_id: str  # id tool_call, которому вернётся ответ
    question: str
    options: list = field(default_factory=list)


PENDING: dict[str, AskQ] = {}
_CREATED_AT: dict[str, float] = {}


def register_ask(q: AskQ) -> str:
    ask_id = uuid.uuid4().hex[:12]
    PENDING[ask_id] = q
    _CREATED_AT[ask_id] = time.time()
    return ask_id


def consume_ask(ask_id: str) -> Optional[AskQ]:
    _CREATED_AT.pop(ask_id, None)
    return PENDING.pop(ask_id, None)


def cleanup_expired() -> None:
    now = time.time()
    for ask_id in list(PENDING):
        if now - _CREATED_AT.get(ask_id, 0) > ASK_TTL_SECONDS:
            PENDING.pop(ask_id, None)
            _CREATED_AT.pop(ask_id, None)


# ---------- клавиатуры ----------


def kb_ask(options: list[str], cancel_label: str = ASK_CANCEL_LABEL) -> Optional[ReplyKeyboardMarkup]:
    """Reply-клавиатура вариантов ответа (пользователь может и написать свой)."""
    if not options:
        return None
    buttons = [KeyboardButton(text=o[:_BUTTON_LABEL_MAX]) for o in options]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([KeyboardButton(text=cancel_label[:_BUTTON_LABEL_MAX])])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Ответьте или выберите вариант",
    )
