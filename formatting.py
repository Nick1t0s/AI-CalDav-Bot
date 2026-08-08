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


def _parse_byday(items) -> tuple[list[int], dict[int, int]]:
    """BYDAY → (дни недели, {день: порядковый номер})."""
    days: list[int] = []
    ordinals: dict[int, int] = {}
    if items is None:
        items = []
    elif not isinstance(items, list):
        items = [items]
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
            bymonthday = rr.get("BYMONTHDAY")
            if bymonthday is not None and not isinstance(bymonthday, list):
                bymonthday = [bymonthday]
            bymonthday = bymonthday or []
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


def format_catalog_compact(events, start=None, end=None, oneoff_limit: Optional[int] = None) -> tuple[str, dict]:
    """Компактный каталог для LLM: серии одной строкой, одиночные — по дням.

    Серия рендерится один раз: «[eN] Название · 16:00–17:00 · каждый Пн · кроме …».
    Возвращает (текст, {ref: event|list[event]}): серия → список вхождений периода,
    одиночное → EventData. Токены [eN] уникальны в пределах всего периода.
    """
    period_start = start.astimezone(config.TZ).date() if start is not None else None
    period_end = end.astimezone(config.TZ).date() if end is not None else None

    series: dict[str, list] = {}
    oneoffs: list = []
    for ev in events:
        if ev.is_recurring:
            series.setdefault(ev.url, []).append(ev)
        else:
            oneoffs.append(ev)
    for insts in series.values():
        insts.sort(key=lambda e: (e.start, e.summary))

    refs: dict[str, object] = {}
    lines: list[str] = []
    idx = 1

    if series:
        lines.append("Серии:")
        for url in sorted(series, key=lambda u: series[u][0].start):
            insts = series[url]
            rep = insts[0]
            ref = f"e{idx}"
            idx += 1
            refs[ref] = insts
            when = "весь день" if rep.all_day else f"{rep.start:%H:%M}–{rep.end:%H:%M}"
            line = f"  [{ref}] {rep.summary} · {when}"
            rec = describe_rrule(rep.rrule) if rep.rrule else ""
            if rec:
                line += f" · {rec}"
            excluded = sorted(rep.exdates)
            if period_start is not None and period_end is not None:
                excluded = [d for d in excluded if period_start <= d < period_end]
            if excluded:
                line += " · кроме " + ", ".join(fmt_date(d) for d in excluded)
            lines.append(line)
        lines.append("")

    if oneoffs:
        lines.append("Одиночные события:")
        groups: dict[date, list] = {}
        for ev in oneoffs:
            day = ev.start.astimezone(config.TZ).date()
            groups.setdefault(day, []).append(ev)
        total = len(oneoffs)
        shown = 0
        for day in sorted(groups):
            day_events = sorted(groups[day], key=lambda e: (e.start, e.summary))
            if oneoff_limit is not None and shown >= oneoff_limit:
                break
            lines.append(f"{fmt_date(day)}:")
            for ev in day_events:
                if oneoff_limit is not None and shown >= oneoff_limit:
                    break
                ref = f"e{idx}"
                idx += 1
                refs[ref] = ev
                lines.append(f"  [{ref}] {describe_event(ev)}")
                shown += 1
        if oneoff_limit is not None and shown < total:
            lines.append(f"  (показаны первые {shown} одиночных из {total})")

    return "\n".join(lines).strip(), refs


def format_ask(question: str) -> str:
    """Текст вопроса ask_user (HTML для Telegram)."""
    return f"❓ {esc(question)}"


def format_done(message: str, items: list[str]) -> str:
    """Финальный ответ done: сообщение + аккуратный список пунктов (HTML)."""
    out = [esc(message)]
    if items:
        out.extend(["", "<b>📋 Детали</b>", ""])
        for item in items:
            lowered = item.strip().lower()
            if lowered.startswith("серии"):
                out.append(f"🔁 <b>{esc(item)}</b>")
            elif lowered.startswith("одиночные"):
                out.append(f"📅 <b>{esc(item)}</b>")
            else:
                out.append(f"• {esc(item)}")
    return "\n".join(out)


def format_plan(actions) -> str:
    """Человекочитаемый список запланированных действий (для кнопки подтверждения)."""
    if not actions:
        return ""
    lines = ["<b>📋 План действий</b>", ""]
    for i, a in enumerate(actions, 1):
        lines.append(f"{i}. {_plan_action_line(a)}")
    lines.append("")
    lines.append("Выполнить?")
    return "\n".join(lines)


def _plan_action_line(a) -> str:
    if a.kind == "create":
        p = a.payload
        start = p["start"].astimezone(config.TZ)
        end = start + p["duration"]
        line = f"✨ Создать <b>«{esc(p['summary'])}»</b> — {fmt_date(start.date())}, {start:%H:%M}–{end:%H:%M}"
        if p.get("rrule"):
            line += f" · 🔁 {describe_rrule(p['rrule'])}"
        if p.get("location"):
            line += f" · 📍 {esc(p['location'])}"
        return line

    ev = a.event
    if a.kind == "delete":
        if a.scope == "all" and ev.is_recurring:
            return f"🗑 Удалить <b>все повторения</b> «{esc(ev.summary)}»"
        when = "весь день" if ev.all_day else f"{ev.start:%H:%M}"
        return f"🗑 Удалить «{esc(ev.summary)}» ({fmt_dtime(ev.start)}, {when})"

    if a.kind == "update":
        changes = a.changes or {}
        bits: list[str] = []
        if changes.get("summary") is not None:
            bits.append(f"название → «{esc(changes['summary'])}»")
        if changes.get("start") or changes.get("shift_minutes"):
            ns = _new_start(ev, changes)
            bits.append(f"начало → {ns:%H:%M}")
        if changes.get("duration"):
            bits.append(f"длительность → {int(changes['duration'])} мин")
        if changes.get("location") is not None:
            loc = esc(changes["location"]) if changes["location"] else "—"
            bits.append(f"место → {loc}")
        target = "все вхождения" if a.scope == "all" else "это вхождение" if a.scope == "instance" else "событие"
        return f"📝 Изменить «{esc(ev.summary)}» ({target}): {', '.join(bits)}"

    return "❓ Неизвестное действие"


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
