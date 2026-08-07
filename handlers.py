"""Обработчики сообщений и callback-кнопок.

Скрипт (а не ИИ) выполняет все действия с календарём и подтверждения.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from html import escape as esc
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

import config
from caldav_service import (
    CalDAVError,
    EventData,
    collapse_events,
    create_event,
    delete_event,
    exclude_occurrence,
    list_events,
    update_event,
)
from confirmation import (
    CandidateOp,
    CreateOp,
    DeleteConfirmOp,
    DeleteOp,
    UpdateOp,
    UpdateScopeOp,
    cleanup_expired,
    consume,
    get,
    kb_create_confirm,
    kb_delete_confirm,
    kb_delete_options,
    kb_pick,
    kb_scope,
    kb_update_confirm,
    register,
)
from formatting import (
    describe_event,
    format_delete_confirm,
    format_delete_question,
    format_event_list,
    format_new_event_preview,
    format_period,
    format_update_preview,
    fmt_dtime,
    _new_start,
)
from intent_parser import IntentParseError, match_events, parse_intent

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


# ---------- helpers ----------


def _now() -> datetime:
    return datetime.now(config.TZ)


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)
    return dt.astimezone(config.TZ)


def _day_start(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day, tzinfo=config.TZ)


def _period_range(intent: dict, default_days: Optional[int] = None) -> Optional[tuple[datetime, datetime]]:
    today = _now().date()
    date_from = intent.get("date_from")
    date_to = intent.get("date_to")
    if not date_from and not date_to:
        if default_days is not None:
            start = _day_start(datetime.combine(today, datetime.min.time()))
            end = _day_start(datetime.combine(today, datetime.min.time())) + timedelta(days=default_days)
            return start, end
        date_from = today.isoformat()
    date_to = date_to or date_from
    try:
        start = _day_start(datetime.fromisoformat(date_from))
        end = _day_start(datetime.fromisoformat(date_to)) + timedelta(days=1)
    except ValueError:
        return None
    return start, end


def _match(ev: EventData, query: str) -> bool:
    if not query:
        return True
    q = query.lower()
    return q in ev.summary.lower() or q in ev.description.lower() or q in ev.location.lower()


async def _match_query(events: list[EventData], query: str) -> list[EventData]:
    """Свернуть серии и выбрать подходящие события.

    Основной механизм — LLM: она видит свёрнутый каталог (серия = одна строка
    с описанием повтора) и возвращает номера строк. При сбое LLM — локальный
    фильтр по подстроке.
    """
    collapsed = collapse_events(events)
    if not query:
        return collapsed
    catalog = "\n".join(f"{i}. {describe_event(ev)}" for i, ev in enumerate(collapsed, 1))
    try:
        indices = await asyncio.to_thread(match_events, query, catalog)
    except IntentParseError:
        logger.warning("LLM-матчинг не удался, использую локальный фильтр", exc_info=True)
        return [ev for ev in collapsed if _match(ev, query)]
    return [collapsed[i - 1] for i in indices]


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


async def _find_candidates(message: Message, intent: dict) -> Optional[list[EventData]]:
    rng = _period_range(intent)
    if rng is None:
        await message.answer("Не удалось определить дату в запросе.")
        return None
    start, end = rng
    query = (intent.get("query") or "").strip()
    if not query:
        await message.answer(
            "Уточни, какое событие (название). Например: «отмени завтрашнее занятие»."
        )
        return None
    try:
        events = await asyncio.to_thread(list_events, start, end)
    except CalDAVError as exc:
        await message.answer(f"❌ Ошибка CalDAV: {esc(str(exc))}")
        return None
    return await _match_query(events, query)


def _build_payload(intent: dict) -> Optional[dict]:
    summary = intent.get("summary")
    start_iso = intent.get("start")
    if not summary or not start_iso:
        return None
    duration_min = intent.get("duration") or 60
    try:
        start = _parse_dt(start_iso)
    except ValueError:
        return None
    return {
        "summary": summary,
        "start": start,
        "duration": timedelta(minutes=int(duration_min)),
        "location": intent.get("location") or None,
        "description": intent.get("description") or None,
    }


def _update_payload(ev: EventData, changes: dict) -> dict:
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
    }


# ---------- commands ----------


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


# ---------- text ----------


@router.message(F.text)
async def on_message(message: Message) -> None:
    if not await _check_allowed(message):
        return
    cleanup_expired()
    text = message.text.strip()
    if not text or text.startswith("/"):
        return
    try:
        intent = await asyncio.to_thread(parse_intent, text)
    except IntentParseError as exc:
        await message.answer(f"😕 Не удалось понять запрос: {esc(str(exc))}")
        return
    kind = intent.get("intent")
    logger.info("intent=%s message=%r", kind, text)
    if kind == "none":
        await message.answer(
            "🤷 Не понял, что сделать. Попробуй, например:\n"
            "• «что у меня завтра»\n"
            "• «когда вебинар по поступлению»\n"
            "• «отмени завтрашнее занятие»"
        )
    elif kind == "list":
        await _list_flow(message, intent)
    elif kind == "create":
        await _create_flow(message, intent)
    elif kind == "delete":
        await _delete_flow(message, intent)
    elif kind == "update":
        await _update_flow(message, intent)
    else:
        await message.answer("🤷 Не понял, что сделать.")


async def _list_flow(message: Message, intent: dict) -> None:
    query = (intent.get("query") or "").strip()
    explicit_dates = bool(intent.get("date_from") or intent.get("date_to"))
    rng = _period_range(intent, default_days=None if explicit_dates else config.LIST_DEFAULT_DAYS)
    if rng is None:
        await message.answer("Не удалось определить дату в запросе.")
        return
    start, end = rng
    try:
        events = await asyncio.to_thread(list_events, start, end)
    except CalDAVError as exc:
        await message.answer(f"❌ Ошибка CalDAV: {esc(str(exc))}")
        return
    events = await _match_query(events, query)
    if not events:
        if query:
            await message.answer(
                f"🔍 Не нашёл «{esc(query)}» в ближайшие {config.LIST_DEFAULT_DAYS} дней."
            )
        else:
            await message.answer(f"📭 На {format_period(start, end)}: событий нет.")
        return
    await message.answer(format_event_list(events, start, end))


async def _create_flow(message: Message, intent: dict) -> None:
    payload = _build_payload(intent)
    if payload is None:
        await message.answer(
            "Для создания нужно название и время. Пример: «создай встречу с Аней завтра в 14:00 на час»."
        )
        return
    op = CreateOp(user_id=message.from_user.id, payload=payload)
    op_id = register(op)
    await message.answer(format_new_event_preview(payload), reply_markup=kb_create_confirm(op_id))


async def _delete_flow(message: Message, intent: dict) -> None:
    candidates = await _find_candidates(message, intent)
    if candidates is None:
        return
    if not candidates:
        await message.answer("🔍 Не нашёл подходящих событий.")
        return
    if len(candidates) == 1:
        await _offer_delete(message, candidates[0], message.from_user.id)
        return
    op = CandidateOp(user_id=message.from_user.id, kind="delete", events=candidates)
    op_id = register(op)
    await message.answer("Нашёл несколько событий. Какое удалить?", reply_markup=kb_pick(op_id, candidates))


async def _update_flow(message: Message, intent: dict) -> None:
    candidates = await _find_candidates(message, intent)
    if candidates is None:
        return
    if not candidates:
        await message.answer("🔍 Не нашёл событие для изменения.")
        return
    changes = intent.get("changes") or {}
    if not any(changes.get(k) for k in ("summary", "start", "shift_minutes", "duration", "location")):
        await message.answer("Не понял, что именно изменить.")
        return
    apply_to = intent.get("apply_to")
    if len(candidates) == 1:
        await _offer_update(message, candidates[0], changes, apply_to, message.from_user.id)
        return
    op = CandidateOp(
        user_id=message.from_user.id, kind="update", events=candidates,
        changes=changes, apply_to=apply_to,
    )
    op_id = register(op)
    await message.answer("Нашёл несколько событий. Какое изменить?", reply_markup=kb_pick(op_id, candidates))


async def _offer_delete(msg: Message, ev: EventData, user_id: int) -> None:
    op = DeleteOp(user_id=user_id, event=ev)
    op_id = register(op)
    await _safe_edit(msg, format_delete_question(ev), kb_delete_options(op_id, ev.is_recurring))


async def _offer_update(
    msg: Message, ev: EventData, changes: dict, apply_to: Optional[str], user_id: int
) -> None:
    if ev.is_recurring and not apply_to:
        op = UpdateScopeOp(user_id=user_id, event=ev, changes=changes)
        op_id = register(op)
        await _safe_edit(msg, "Это повторяющееся событие. Что изменить?", kb_scope(op_id))
        return
    scope = "all" if ev.is_recurring and apply_to == "all" else "instance" if ev.is_recurring else "single"
    op = UpdateOp(user_id=user_id, event=ev, changes=changes, scope=scope)
    op_id = register(op)
    await _safe_edit(msg, format_update_preview(ev, changes, scope), kb_update_confirm(op_id))


# ---------- callback ----------


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
        await _safe_edit(cb.message, "❌ Отменено.")
        await cb.answer()
        return

    if action in ("instance", "all"):
        op = get(op_id)
        if not isinstance(op, DeleteOp):
            await cb.answer()
            return
        confirm = DeleteConfirmOp(user_id=op.user_id, event=op.event, delete_all=(action == "all"))
        new_id = register(confirm)
        await _safe_edit(
            cb.message, format_delete_confirm(op.event, action == "all"), kb_delete_confirm(new_id)
        )
        await cb.answer()
        return

    if action == "confirm":
        op = consume(op_id)
        if not isinstance(op, DeleteConfirmOp):
            await cb.answer()
            return
        await _run_delete(cb, op)
        return

    if action == "pick":
        op = consume(op_id)
        if not isinstance(op, CandidateOp) or len(parts) < 4:
            await cb.answer()
            return
        idx = int(parts[3])
        if idx >= len(op.events):
            await cb.answer("Событие не найдено.")
            return
        ev = op.events[idx]
        if op.kind == "delete":
            await _offer_delete(cb.message, ev, op.user_id)
        else:
            await _offer_update(cb.message, ev, op.changes or {}, op.apply_to, op.user_id)
        await cb.answer()
        return

    if action == "scope":
        op = consume(op_id)
        if not isinstance(op, UpdateScopeOp) or len(parts) < 4:
            await cb.answer()
            return
        scope = "instance" if parts[3] == "0" else "all"
        upd = UpdateOp(user_id=op.user_id, event=op.event, changes=op.changes, scope=scope)
        new_id = register(upd)
        await _safe_edit(
            cb.message, format_update_preview(op.event, op.changes, scope), kb_update_confirm(new_id)
        )
        await cb.answer()
        return

    if action == "apply":
        op = consume(op_id)
        if not isinstance(op, UpdateOp):
            await cb.answer()
            return
        await _run_update(cb, op)
        return

    if action == "create":
        op = consume(op_id)
        if not isinstance(op, CreateOp):
            await cb.answer()
            return
        await _run_create(cb, op)
        return

    await cb.answer()


# ---------- исполнение (скрипт, не ИИ) ----------


async def _run_delete(cb: CallbackQuery, op: DeleteConfirmOp) -> None:
    try:
        await asyncio.to_thread(_perform_delete, op)
        if op.delete_all or not op.event.is_recurring:
            text = f"✅ Удалено: «{esc(op.event.summary)}»"
        else:
            text = f"✅ Удалено вхождение: «{esc(op.event.summary)}» ({fmt_dtime(op.event.start)})"
    except CalDAVError as exc:
        text = f"❌ Ошибка: {esc(str(exc))}"
    await _safe_edit(cb.message, text)
    await cb.answer()


def _perform_delete(op: DeleteConfirmOp) -> None:
    if op.delete_all or not op.event.is_recurring:
        delete_event(op.event)
    else:
        exclude_occurrence(op.event)


async def _run_update(cb: CallbackQuery, op: UpdateOp) -> None:
    try:
        await asyncio.to_thread(_perform_update, op)
        summary = op.changes.get("summary") or op.event.summary
        text = f"✅ Изменено: «{esc(summary)}»"
    except CalDAVError as exc:
        text = f"❌ Ошибка: {esc(str(exc))}"
    await _safe_edit(cb.message, text)
    await cb.answer()


def _perform_update(op: UpdateOp) -> None:
    if op.event.is_recurring and op.scope == "instance":
        exclude_occurrence(op.event)
        payload = _update_payload(op.event, op.changes)
        create_event(
            summary=payload["summary"],
            start=payload["start"],
            duration=payload["duration"],
            location=payload["location"],
            description=payload["description"],
        )
    else:
        update_event(op.event, op.changes)


async def _run_create(cb: CallbackQuery, op: CreateOp) -> None:
    payload = op.payload
    try:
        created = await asyncio.to_thread(
            create_event,
            summary=payload["summary"],
            start=payload["start"],
            duration=payload["duration"],
            location=payload.get("location"),
            description=payload.get("description"),
        )
        when = "весь день" if created.all_day else f"{created.start:%H:%M}"
        text = f"✅ Создано: «{esc(created.summary)}» ({fmt_dtime(created.start)}, {when})"
    except CalDAVError as exc:
        text = f"❌ Ошибка: {esc(str(exc))}"
    await _safe_edit(cb.message, text)
    await cb.answer()
