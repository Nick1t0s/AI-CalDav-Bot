"""Рендер событий на русском (HTML для Telegram)."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from html import escape as esc
from typing import Optional

from icalendar import vRecur

import config

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
_ORDINALS = {
    1: "первый", 2: "второй", 3: "третий", 4: "четвёртый", 5: "пятый",
    6: "шестой", 7: "седьмой", 8: "восьмой", 9: "девятый", 10: "десятый",
    -1: "последний", -2: "предпоследний", -3: "третий с конца",
}
_WDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


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
        if getattr(ev, "series_count", 1) > 1:
            span = _series_span(ev)
            line += f" · 🔁 {esc(describe_rrule(ev.rrule))}{span}"
        else:
            line += " 🔁"
    return line


def _series_span(ev) -> str:
    """Диапазон вхождений серии в периоде → « (Пн 10 авг — Пн 2 ноя)»."""
    if not ev.series_first or not ev.series_last:
        return ""
    d1 = ev.series_first.astimezone(config.TZ).date()
    d2 = ev.series_last.astimezone(config.TZ).date()
    if d1 == d2:
        return ""
    return f" ({fmt_date(d1)} — {fmt_date(d2)})"


def _parse_byday(items) -> tuple[list[int], dict[int, int]]:
    """BYDAY → (дни недели, {день: порядковый номер})."""
    days: list[int] = []
    ordinals: dict[int, int] = {}
    for item in items:
        s = item.to_ical().decode() if hasattr(item, "to_ical") else str(item)
        m = re.fullmatch(r"(-?\d+)?(MO|TU|WE|TH|FR|SA|SU)", s)
        if not m:
            continue
        n, code = m.groups()
        if code not in _WDAY:
            continue
        if n:
            ordinals[_WDAY[code]] = int(n)
        else:
            days.append(_WDAY[code])
    return days, ordinals


def describe_rrule(rrule: Optional[str]) -> str:
    """RRULE → русское описание повтора («каждый Пн», «до 2 ноя»)."""
    if not rrule:
        return "повторяется"
    try:
        rr = vRecur.from_ical(rrule)
    except Exception:
        return f"повторяется ({rrule})"

    def _first(key):
        value = rr.get(key)
        if isinstance(value, list):
            return value[0] if value else None
        return value

    freq = (_first("FREQ") or "WEEKLY").upper()
    parts: list[str] = []

    def _join_days(days: list[int]) -> str:
        names = [WEEKDAYS[d] for d in sorted(days)]
        if len(names) == 1:
            return names[0]
        return " и ".join(names)

    if freq == "DAILY":
        parts.append("каждый день")
    elif freq == "WEEKLY":
        days, ordinals = _parse_byday(rr.get("BYDAY", []))
        if days:
            parts.append(f"каждый {_join_days(days)}")
        else:
            parts.append("каждую неделю")
    elif freq == "MONTHLY":
        days, ordinals = _parse_byday(rr.get("BYDAY", []))
        if ordinals:
            for d in sorted(ordinals):
                name = _ORDINALS.get(ordinals[d], f"{ordinals[d]}-й")
                parts.append(f"каждый {name} {WEEKDAYS[d]} месяца")
        elif days:
            parts.append(f"каждый {_join_days(days)} месяца")
        else:
            bymonthday = rr.get("BYMONTHDAY") or []
            if bymonthday:
                parts.append("каждое " + " и ".join(str(d) for d in bymonthday) + " числа месяца")
            else:
                parts.append("каждый месяц")
    elif freq == "YEARLY":
        parts.append("каждый год")
    else:
        parts.append("повторяется")

    until = _first("UNTIL")
    if until is not None:
        if isinstance(until, datetime):
            until = until.date()
        parts.append(f"до {fmt_date(until)}")
    count = _first("COUNT")
    if count:
        parts.append(f"{count} раз")
    return ", ".join(parts)


def describe_event(ev) -> str:
    """Плоское описание события для LLM-каталога (без HTML/эмодзи)."""
    s = ev.start.astimezone(config.TZ)
    if ev.all_day:
        when = f"{fmt_date(s.date())}, весь день"
    else:
        e = ev.end.astimezone(config.TZ)
        when = f"{fmt_date(s.date())}, {s:%H:%M}–{e:%H:%M}"
    parts = [f"{ev.summary}", when]
    if ev.is_recurring:
        parts.append(describe_rrule(ev.rrule))
    if ev.location:
        parts.append(ev.location)
    if ev.description:
        parts.append(ev.description)
    return " · ".join(parts)


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
    if payload.get("rrule"):
        lines.append(f"🔁 Повтор: {describe_rrule(payload['rrule'])}")
    if payload.get("location"):
        lines.append(f"📍 Место: {esc(payload['location'])}")
    if payload.get("description"):
        lines.append(f"📝 Описание: {esc(payload['description'])}")
    lines.append("")
    lines.append("Создать?")
    return "\n".join(lines)
