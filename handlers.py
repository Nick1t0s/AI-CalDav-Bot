"""Обработчики сообщений и callback-кнопок.

Агентная архитектура: LLM (agent.py) сама ищет события и планирует изменения,
а скрипт подтверждает план кнопкой и исполняет его через caldav_service.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from html import escape as esc

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

import config
from agent import AgentError, answer_ask, append_assistant_text, resume_agent, run_agent
from asks import cleanup_expired as cleanup_asks, consume_ask, get_ask, kb_ask
from caldav_service import (
    CalDAVError,
    create_event,
    delete_event,
    exclude_occurrence,
    update_event,
)
from confirmation import (
    PlanAction,
    PlanOp,
    cleanup_expired,
    consume,
    get,
    register,
    kb_plan_confirm,
)
from formatting import (
    _new_start,
    describe_rrule,
    format_ask,
    format_done,
    format_plan,
    fmt_dtime,
)

logger = logging.getLogger(__name__)
router = Router()

INTRO = (
    "Привет! Я умею работать с твоим Яндекс-календарём.\n\n"
    "Примеры запросов:\n"
    "• «что у меня завтра»\n"
    "• «что на следующей неделе»\n"
    "• «отмени завтрашнее занятие»\n"
    "• «перенеси занятие завтра на 20:00»\n"
    "• «создай встречу с Аней завтра в 14:00 на час»"
)

# chat_id -> op_id незакрытого плана (новое сообщение отменяет его)
_PENDING_PLANS: dict[int, str] = {}


def _cancel_pending_plan(user_id: int) -> None:
    op_id = _PENDING_PLANS.pop(user_id, None)
    if op_id:
        consume(op_id)


# ---------- helpers ----------


async def _safe_edit(message: Message, text: str, reply_markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await message.answer(text, reply_markup=reply_markup)


async def _check_allowed(message: Message) -> bool:
    if not config.ALLOWED_USER_IDS:
        await message.answer("⛔ Доступ запрещён: не настроен ALLOWED_USER_IDS.")
        return False
    if message.from_user.id not in config.ALLOWED_USER_IDS:
        await message.answer("⛔ Доступ запрещён.")
        return False
    return True


# ---------- команды ----------


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not await _check_allowed(message):
        return
    await message.answer(INTRO)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not await _check_allowed(message):
        return
    await message.answer(INTRO)


# ---------- текст ----------


@router.message(F.text)
async def on_message(message: Message) -> None:
    if not await _check_allowed(message):
        return
    cleanup_expired()
    cleanup_asks()
    text = message.text.strip()
    if not text or text.startswith("/"):
        return
    await message.bot.send_chat_action(message.chat.id, action=ChatAction.TYPING)
    _cancel_pending_plan(message.from_user.id)
    try:
        result = await asyncio.to_thread(run_agent, message.from_user.id, text)
    except AgentError as exc:
        await message.answer(f"😕 Ошибка агента: {esc(str(exc))}")
        return
    await _handle_result(message, result)


async def _handle_result(message: Message, result, user_id: int = None) -> None:
    """Показ результата агента: вопросы (ask) / ошибка / финальный ответ + план."""
    if user_id is None:
        user_id = message.from_user.id if message.from_user else 0
    if result.kind == "error":
        await message.answer(f"😕 {esc(result.text)}")
        return
    if result.kind == "ask":
        for q in result.questions:
            await message.answer(format_ask(q["question"]), reply_markup=kb_ask(q["ask_id"], q["options"]))
        return
    if result.plan:
        op = PlanOp(user_id=user_id, actions=result.plan)
        op_id = register(op)
        _PENDING_PLANS[user_id] = op_id
        content = format_done(result.text, result.items)
        content += "\n\n" + format_plan(result.plan)
        await message.answer(content, reply_markup=kb_plan_confirm(op_id))
    else:
        await message.answer(format_done(result.text or "🤷 Не понял, что сделать.", result.items))


# ---------- callback ----------


@router.callback_query(F.data.startswith("ask:"))
async def on_ask_callback(cb: CallbackQuery) -> None:
    cleanup_asks()
    parts = cb.data.split(":")
    if len(parts) < 3:
        await cb.answer()
        return
    ask_id, idx = parts[1], parts[2]
    ask = get_ask(ask_id)
    if ask is None:
        await cb.answer("Вопрос устарел, попробуйте ещё раз.", show_alert=True)
        return
    if cb.from_user.id != ask.user_id:
        await cb.answer("Это не ваш вопрос.", show_alert=True)
        return
    try:
        option = ask.options[int(idx)]
    except (ValueError, IndexError):
        await cb.answer()
        return
    consume_ask(ask_id)
    all_done = await asyncio.to_thread(answer_ask, cb.from_user.id, ask_id, option)
    await _safe_edit(cb.message, f"{format_ask(ask.question)}\n✓ {esc(option)}")
    if not all_done:
        await cb.answer("Осталось ответить на другие вопросы.")
        return
    try:
        result = await asyncio.to_thread(resume_agent, cb.from_user.id)
    except AgentError as exc:
        await _safe_edit(cb.message, f"😕 Ошибка агента: {esc(str(exc))}")
        await cb.answer()
        return
    await _handle_result(cb.message, result, user_id=cb.from_user.id)
    await cb.answer()


@router.callback_query(F.data.startswith("op:"))
async def on_callback(cb: CallbackQuery) -> None:
    cleanup_expired()
    parts = cb.data.split(":")
    if len(parts) < 3:
        await cb.answer()
        return
    op_id, action = parts[1], parts[2]
    op = get(op_id)
    if op is None:
        await cb.answer("Операция устарела, попробуйте ещё раз.", show_alert=True)
        return
    if cb.from_user.id != op.user_id:
        await cb.answer("Это не ваша операция.", show_alert=True)
        return

    if action == "cancel":
        consume(op_id)
        _PENDING_PLANS.pop(cb.from_user.id, None)
        await _safe_edit(cb.message, "❌ Отменено.")
        await cb.answer()
        return

    if action == "plan_confirm":
        op = consume(op_id)
        _PENDING_PLANS.pop(cb.from_user.id, None)
        if not isinstance(op, PlanOp):
            await cb.answer()
            return
        results = await asyncio.to_thread(_perform_plan, op)
        text = "\n".join(results)
        append_assistant_text(cb.from_user.id, text)
        await _safe_edit(cb.message, text)
        await cb.answer()
        return

    await cb.answer()


# ---------- исполнение плана (скрипт, не ИИ) ----------


def _perform_plan(op: PlanOp) -> list[str]:
    out: list[str] = []
    for action in op.actions:
        try:
            out.append(_perform_action(action))
        except CalDAVError as exc:
            out.append(f"❌ Ошибка: {esc(str(exc))}")
    return out


def _perform_action(action: PlanAction) -> str:
    if action.kind == "create":
        return _run_create_action(action.payload)
    if action.kind == "delete":
        return _run_delete_action(action)
    if action.kind == "update":
        return _run_update_action(action)
    return "❌ Неизвестное действие"


def _run_create_action(payload: dict) -> str:
    created = create_event(**payload)
    when = "весь день" if created.all_day else f"{created.start:%H:%M}"
    text = f"✅ Создано: «{esc(created.summary)}» ({fmt_dtime(created.start)}, {when})"
    if created.is_recurring:
        text += f" 🔁 {describe_rrule(created.rrule)}"
    return text


def _run_delete_action(action: PlanAction) -> str:
    ev = action.event
    if action.scope == "all" or not ev.is_recurring:
        delete_event(ev)
        return f"✅ Удалено: «{esc(ev.summary)}»"
    exclude_occurrence(ev)
    when = "весь день" if ev.all_day else f"{ev.start:%H:%M}"
    return f"✅ Удалено вхождение: «{esc(ev.summary)}» ({fmt_dtime(ev.start)}, {when})"


def _run_update_action(action: PlanAction) -> str:
    ev = action.event
    changes = action.changes or {}
    if ev.is_recurring and action.scope == "instance":
        exclude_occurrence(ev)
        payload = _build_updated_payload(ev, changes)
        created = create_event(**payload)
        return f"✅ Изменено: «{esc(payload['summary'])}» (вхождение вынесено в отдельное событие)"
    update_event(ev, changes)
    summary = changes.get("summary") or ev.summary
    return f"✅ Изменено: «{esc(summary)}»"


def _build_updated_payload(ev, changes: dict) -> dict:
    start = _new_start(ev, changes)
    duration = timedelta(minutes=int(changes["duration"])) if changes.get("duration") else ev.duration
    summary = changes.get("summary") or ev.summary
    location = changes.get("location") if changes.get("location") is not None else ev.location
    return {
        "summary": summary,
        "start": start,
        "duration": duration,
        "location": location or None,
        "description": ev.description or None,
        "rrule": None,
    }
