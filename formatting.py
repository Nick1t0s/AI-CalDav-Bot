"""Рендер событий на русском (HTML для Telegram)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape as esc

import config

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _now() -> datetime:
    return datetime.now(config.TZ)


def fmt_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS_GEN[d.month - 1]}"


def fmt_dtime(dt: datetime) -> str:
    dt = dt.astimezone(config.TZ)
    today = _now().date()
    if dt.date() == today:
        return "сегодня"
    if dt.date() == today + timedelta(days=1):
        return "завтра"
    return fmt_date(dt.date())


def format_period(start: datetime, end: datetime) -> str:
    """Период [start, end) → человекочитаемая дата/диапазон."""
    d1 = start.astimezone(config.TZ).date()
    d2 = (end - timedelta(days=1)).astimezone(config.TZ).date()
    if d1 == d2:
        return fmt_date(d1)
    return f"{fmt_date(d1)} — {fmt_date(d2)}"


def event_line(ev) -> str:
    s = ev.start.astimezone(config.TZ)
    if ev.all_day:
        line = f"📌 {esc(ev.summary)} <i>(весь день)</i>"
    else:
        e = ev.end.astimezone(config.TZ)
        line = f"🕐 {s:%H:%M}–{e:%H:%M} {esc(ev.summary)}"
    if ev.location:
        line += f" · <b>{esc(ev.location)}</b>"
    if ev.is_recurring:
        line += " 🔁"
    return line


def format_event_list(events, start: datetime, end: datetime) -> str:
    groups: dict[date, list] = {}
    for ev in events:
        day = ev.start.astimezone(config.TZ).date()
        groups.setdefault(day, []).append(ev)
    out: list[str] = []
    for day in sorted(groups):
        out.append(f"<b>📅 {fmt_date(day)}</b>")
        for ev in groups[day]:
            out.append(event_line(ev))
        out.append("")
    return "\n".join(out).rstrip()


def format_delete_question(ev) -> str:
    s = ev.start.astimezone(config.TZ)
    when = "весь день" if ev.all_day else f"{s:%H:%M}"
    return f"🗑 Удалить <b>«{esc(ev.summary)}»</b> ({fmt_dtime(s)}, {when})?"


def format_delete_confirm(ev, delete_all: bool) -> str:
    if delete_all:
        return f"⚠️ Точно удалить <b>все повторения</b> «{esc(ev.summary)}»?"
    when = "весь день" if ev.all_day else f"{ev.start:%H:%M}"
    return f"⚠️ Точно удалить <b>«{esc(ev.summary)}»</b> ({fmt_dtime(ev.start)}, {when})?"


def _new_start(ev, changes: dict) -> datetime:
    start_iso = changes.get("start")
    if start_iso:
        dt = datetime.fromisoformat(start_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=config.TZ)
        return dt.astimezone(config.TZ)
    shift = changes.get("shift_minutes")
    if shift:
        return ev.start + timedelta(minutes=int(shift))
    return ev.start


def format_update_preview(ev, changes: dict, scope: str) -> str:
    lines = ["<b>📝 Изменение события</b>", ""]
    if ev.is_recurring:
        target = "все вхождения" if scope == "all" else "только это вхождение"
        lines.append(f"Применяется к: <b>{target}</b>")
        lines.append("")
    lines.append(f"• Текущее: <b>«{esc(ev.summary)}»</b>")
    if ev.all_day:
        lines.append(f"  {fmt_dtime(ev.start)} (весь день)")
    else:
        lines.append(f"  {fmt_dtime(ev.start)} в {ev.start:%H:%M}–{ev.end:%H:%M}")
    if ev.location:
        lines.append(f"  📍 {esc(ev.location)}")
    lines.append("")
    lines.append("🆕 Новые значения:")
    has_change = False
    if changes.get("summary") is not None:
        lines.append(f"• Название → <b>«{esc(changes['summary'])}»</b>")
        has_change = True
    if changes.get("start") or changes.get("shift_minutes"):
        new_start = _new_start(ev, changes)
        lines.append(f"• Начало → <b>{new_start:%H:%M}</b>")
        has_change = True
    if changes.get("duration"):
        new_start = _new_start(ev, changes)
        new_end = new_start + timedelta(minutes=int(changes["duration"]))
        lines.append(f"• Окончание → <b>{new_end:%H:%M}</b>")
        has_change = True
    loc = changes.get("location")
    if loc is not None:
        lines.append(f"• Место → <b>{esc(loc) if loc else '—'}</b>")
        has_change = True
    if not has_change:
        return "\n".join(["<b>📝 Изменение события</b>", "", "Не указаны конкретные изменения."])
    lines.append("")
    lines.append("Применить?")
    return "\n".join(lines)


def format_new_event_preview(payload: dict) -> str:
    start = payload["start"].astimezone(config.TZ)
    end = start + payload["duration"]
    lines = ["<b>✨ Новое событие</b>", ""]
    lines.append(f"📌 Название: <b>«{esc(payload['summary'])}»</b>")
    lines.append(f"📅 Дата: {fmt_date(start.date())}")
    lines.append(f"🕐 Время: {start:%H:%M}–{end:%H:%M}")
    if payload.get("location"):
        lines.append(f"📍 Место: {esc(payload['location'])}")
    if payload.get("description"):
        lines.append(f"📝 Описание: {esc(payload['description'])}")
    lines.append("")
    lines.append("Создать?")
    return "\n".join(lines)
