"""CRUD для CalDAV (Яндекс Календарь) через caldav + icalendar.

Повторяющиеся события раскрываются клиентски (recurring_ical_events).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from uuid import uuid4

import caldav
import icalendar
from icalendar import vRecur
from icalendar.prop import vDDDLists
import recurring_ical_events

from app import config

logger = logging.getLogger(__name__)
UTC = timezone.utc


class CalDAVError(Exception):
    """Ошибка работы с CalDAV."""


@dataclass
class EventData:
    """Описание события (или одного вхождения повторяющегося события)."""

    url: str  # URL ресурса-владельца (для повторной загрузки мастера)
    uid: str
    summary: str
    location: str
    description: str
    start: datetime
    end: datetime
    all_day: bool
    is_recurring: bool
    instance_start: Optional[datetime] = None
    rrule: Optional[str] = None  # сырая RRULE мастер-события (для серий)
    exdates: list = field(default_factory=list)  # исключённые даты мастера (локальные date)
    rdates: list = field(default_factory=list)  # внеплановые вхождения (локальные datetime)
    alarms: list = field(default_factory=list)  # напоминания: минуты до начала
    categories: list = field(default_factory=list)
    status: str = ""  # CONFIRMED | TENTATIVE | CANCELLED
    transp: str = ""  # OPAQUE (занят) | TRANSPARENT (свободен)
    priority: Optional[int] = None  # 1..9
    link: str = ""  # свойство URL (веб-ссылка)

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _parse_dt(value: str) -> datetime:
    """ISO-строка → aware datetime в часовом поясе пользователя."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)
    return dt


def _norm(dt: datetime) -> datetime:
    return _ensure_aware(dt).astimezone(config.TZ)


def _get_vevent(cal: icalendar.Calendar) -> icalendar.Event:
    for comp in cal.walk("VEVENT"):
        return comp
    raise CalDAVError("VEVENT не найден")


def _as_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time.min, tzinfo=config.TZ)


def _is_all_day(value) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _to_all_day_duration(minutes: int) -> timedelta:
    """Длительность all-day события в днях из минут (мин. 1 день)."""
    return timedelta(days=max(1, round(minutes / 1440)))


def _replace_prop(vevent: icalendar.Event, key: str, value) -> None:
    """Замена свойства с корректным типом (напрямую нельзы — ломает vCal)."""
    if key in vevent:
        del vevent[key]
    vevent.add(key, value)


# ---------- напоминания (VALARM) ----------


def _get_alarms(vevent: icalendar.Event) -> list:
    """Минуты до начала из VALARM (только относительные, до события)."""
    out: list = []
    for sub in vevent.subcomponents:
        if getattr(sub, "name", "") != "VALARM":
            continue
        trig = sub.get("TRIGGER")
        if trig is None:
            continue
        try:
            td = trig.dt
        except Exception:
            continue
        if isinstance(td, timedelta) and td < timedelta(0):
            out.append(int(-td.total_seconds() // 60))
    return sorted(set(out))


def _set_alarms(vevent: icalendar.Event, minutes: list) -> None:
    """Заменить VALARM на указанные минуты до начала (пустой список — без напоминаний)."""
    subs = vevent.subcomponents
    subs[:] = [c for c in subs if getattr(c, "name", "") != "VALARM"]
    for m in sorted({int(x) for x in minutes}):
        if m <= 0:
            continue
        alarm = icalendar.Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", "Напоминание")
        alarm.add("trigger", timedelta(minutes=-m))
        vevent.add_component(alarm)


# ---------- правка RRULE (серия) ----------

RRULE_FREQS = {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}
WEEKDAY_CODES = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}
_WEEKDAY_CODE = {0: "MO", 1: "TU", 2: "WE", 3: "TH", 4: "FR", 5: "SA", 6: "SU"}


def _until_dt(value: str) -> datetime:
    """UNTIL: 'YYYY-MM-DD' → конец дня в TZ пользователя; 'YYYY-MM-DDTHH:MM:SS' → как есть."""
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise CalDAVError(f"некорректный until: {value!r}") from None
    if dt.tzinfo is None:
        dt = datetime.combine(dt.date(), time(23, 59, 59), tzinfo=config.TZ)
    return _ensure_aware(dt).astimezone(UTC)


def _patch_rrule(vevent: icalendar.Event, changes: dict) -> None:
    """Применить частичные правки RRULE (until/count/freq/interval/byday/rrule)."""
    existing = vevent.get("RRULE")
    parts: dict = {}
    if existing is not None:
        for k in existing.keys():
            v = existing[k]
            parts[k] = list(v) if isinstance(v, list) else [v]

    full = changes.get("rrule")
    if full:
        raw = full.strip()
        if raw.upper().startswith("RRULE:"):
            raw = raw[len("RRULE:"):]
        try:
            rr = vRecur.from_ical(raw)
        except Exception:
            raise CalDAVError("некорректный rrule") from None
        parts = {}
        for k in rr.keys():
            v = rr[k]
            parts[k] = list(v) if isinstance(v, list) else [v]

    if changes.get("freq"):
        freq = str(changes["freq"]).strip().upper()
        if freq not in RRULE_FREQS:
            raise CalDAVError(f"некорректный freq: {freq}")
        parts["FREQ"] = [freq]

    if changes.get("interval") is not None:
        try:
            interval = int(changes["interval"])
        except (TypeError, ValueError):
            raise CalDAVError("некорректный interval") from None
        if interval < 1:
            raise CalDAVError("interval должен быть ≥ 1")
        parts["INTERVAL"] = [interval]

    if changes.get("byday") is not None:
        days = [str(d).strip().upper() for d in changes["byday"]]
        if not days or any(d not in WEEKDAY_CODES for d in days):
            raise CalDAVError("некорректный byday: нужны коды MO..SU")
        parts["BYDAY"] = days

    if "until" in changes:
        parts.pop("COUNT", None)
        if changes.get("until"):
            parts["UNTIL"] = [_until_dt(changes["until"])]
        else:
            parts.pop("UNTIL", None)

    if "count" in changes and changes.get("count") is not None:
        try:
            count = int(changes["count"])
        except (TypeError, ValueError):
            raise CalDAVError("некорректный count") from None
        if count < 1:
            raise CalDAVError("count должен быть ≥ 1")
        parts.pop("UNTIL", None)
        parts["COUNT"] = [count]

    if "FREQ" not in parts:
        if existing is None:
            if changes.get("byday") or changes.get("interval"):
                parts["FREQ"] = ["WEEKLY"]
            else:
                raise CalDAVError("нельзя задать until/count без правила повтора (нужен rrule или freq)")
        # иначе сохраняем FREQ из существующего правила

    if not parts:
        if "RRULE" in vevent:
            del vevent["RRULE"]
        return

    rr = vRecur()
    for k, v in parts.items():
        rr[k] = v
    _replace_prop(vevent, "RRULE", rr)


def _byday_codes(existing) -> list:
    """Простые коды дней недели из BYDAY (без порядковых номеров вида 2TU)."""
    if existing is None:
        return []
    items = existing if isinstance(existing, list) else [existing]
    out: list = []
    for item in items:
        s = item.to_ical().decode() if hasattr(item, "to_ical") else str(item)
        s = s.strip().upper()
        if s in WEEKDAY_CODES:
            out.append(s)
    return out


def _align_weekly_byday(vevent: icalendar.Event, changes: dict, new_start: datetime) -> Optional[dict]:
    """Перенос недельной серии: если DTSTART меняет день недели — синхронизировать BYDAY.

    Возвращает копию changes с добавленным byday (если нужна правка) либо None.
    """
    if not new_start or "byday" in changes or "rrule" in changes:
        return None
    existing = vevent.get("RRULE")
    if existing is None:
        return None
    freq = existing.get("FREQ")
    if isinstance(freq, list):
        freq = freq[0] if freq else None
    if freq is None or str(freq).strip().upper() != "WEEKLY":
        return None
    codes = _byday_codes(existing.get("BYDAY"))
    if not codes:
        return None
    target = _WEEKDAY_CODE[new_start.weekday()]
    if target in codes:
        return None
    changes = dict(changes)
    changes["byday"] = [target]
    return changes


def _drop_prop(vevent: icalendar.Event, key: str) -> None:
    if key in vevent:
        del vevent[key]


def _parse_categories(items) -> list:
    out: list = []
    for item in items or []:
        s = str(item).strip()
        if s:
            out.append(s)
    return out


class CalDAVClient:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        try:
            self.client = caldav.DAVClient(
                url=config.CALDAV_URL,
                username=config.CALDAV_USERNAME,
                password=config.CALDAV_PASSWORD,
            )
        except Exception as exc:
            raise CalDAVError(f"Не удалось создать CalDAV-клиент: {exc}") from exc
        self.principal = self._find_principal()
        self.calendar = self._find_calendar()

    # ---------- discovery ----------

    def _find_principal(self):
        if config.CALDAV_PRINCIPAL_PATH:
            url = config.CALDAV_PRINCIPAL_PATH
            if not url.startswith("http"):
                url = config.CALDAV_URL.rstrip("/") + "/" + url.lstrip("/")
            return caldav.Principal(client=self.client, url=url)
        try:
            return self.client.principal()
        except Exception as exc:
            raise CalDAVError(f"Не удалось получить principal: {exc}") from exc

    def _find_calendar(self):
        if config.CALENDAR_PATH:
            return caldav.Calendar(client=self.client, url=config.CALENDAR_PATH)
        try:
            calendars = self.principal.calendars()
        except Exception as exc:
            raise CalDAVError(f"Не удалось получить список календарей: {exc}") from exc
        if not calendars:
            raise CalDAVError("У пользователя нет календарей")
        if config.CALENDAR_ID:
            for cal in calendars:
                if cal.name == config.CALENDAR_ID:
                    return cal
            raise CalDAVError(f"Календарь '{config.CALENDAR_ID}' не найден")
        for cal in calendars:
            name = (cal.name or "").lower()
            if "trash" in name or "корзин" in name:
                continue
            return cal
        return calendars[0]

    # ---------- чтение ----------

    def list_events(self, start: datetime, end: datetime) -> list[EventData]:
        """Все события/вхождения в [start, end). start/end — aware."""
        with self._lock:
            try:
                masters = self.calendar.events()
            except Exception as exc:
                raise CalDAVError(f"Не удалось получить события: {exc}") from exc
            results: list[EventData] = []
            for master in masters:
                try:
                    results.extend(self._expand(master, start, end))
                except Exception as exc:
                    logger.warning("Пропускаю событие %s: %s", getattr(master, "url", "?"), exc)
            results.sort(key=lambda e: (e.start, e.summary))
            return results

    def _expand(self, master, start: datetime, end: datetime) -> list[EventData]:
        cal = master.icalendar_instance
        master_vevent = _get_vevent(cal)
        rrule_obj = master_vevent.get("RRULE")
        is_recurring = rrule_obj is not None
        rrule = rrule_obj.to_ical().decode() if rrule_obj is not None else None
        exdates = self._exdates_of(master_vevent)
        out: list[EventData] = []
        try:
            occurrences = recurring_ical_events.of(cal).between(start, end)
        except Exception:
            return out
        for vevent in occurrences:
            out.append(self._to_event_data(vevent, master, is_recurring, rrule, exdates))
        return out

    @staticmethod
    def _exdates_of(vevent: icalendar.Event) -> list:
        """Локальные даты EXDATE мастер-события (дедуплицированные)."""
        existing = vevent.get("EXDATE")
        if existing is None:
            return []
        values: list[date] = []
        for prop in existing if isinstance(existing, list) else [existing]:
            for d in prop.dts:
                dt = d.dt
                if _is_all_day(dt):
                    values.append(dt)
                else:
                    values.append(_ensure_aware(dt).astimezone(config.TZ).date())
        return list(dict.fromkeys(values))

    def _to_event_data(
        self, vevent, master, is_recurring: bool, rrule: Optional[str] = None, exdates: Optional[list] = None
    ) -> EventData:
        raw_start = vevent.decoded("DTSTART")
        try:
            raw_end = vevent.decoded("DTEND")
        except Exception:
            duration = vevent.decoded("DURATION") if vevent.get("DURATION") is not None else timedelta(hours=1)
            raw_end = raw_start + duration
        all_day = _is_all_day(raw_start)
        if all_day:
            start = datetime.combine(raw_start, time.min, tzinfo=config.TZ)
            end = datetime.combine(raw_end, time.min, tzinfo=config.TZ)
        else:
            start = _norm(raw_start)
            end = _norm(raw_end)
        rdates_raw = vevent.get("RDATE")
        rdates: list = []
        if rdates_raw is not None:
            for prop in rdates_raw if isinstance(rdates_raw, list) else [rdates_raw]:
                for d in prop.dts:
                    dt = d.dt
                    if _is_all_day(dt):
                        rdates.append(datetime.combine(dt, time.min, tzinfo=config.TZ))
                    else:
                        rdates.append(_norm(dt))
        categories_raw = vevent.get("CATEGORIES")
        categories: list = []
        if categories_raw is not None:
            for prop in categories_raw if isinstance(categories_raw, list) else [categories_raw]:
                categories.extend(_parse_categories(getattr(prop, "cats", None)))
        priority_raw = vevent.get("PRIORITY")
        return EventData(
            url=master.canonical_url,
            uid=str(vevent.get("UID") or ""),
            summary=str(vevent.get("SUMMARY") or "(без названия)"),
            location=str(vevent.get("LOCATION") or ""),
            description=str(vevent.get("DESCRIPTION") or ""),
            start=start,
            end=end,
            all_day=all_day,
            is_recurring=is_recurring,
            instance_start=start if is_recurring else None,
            rrule=rrule,
            exdates=exdates or [],
            rdates=rdates,
            alarms=_get_alarms(vevent),
            categories=categories,
            status=str(vevent.get("STATUS") or ""),
            transp=str(vevent.get("TRANSP") or ""),
            priority=int(priority_raw) if priority_raw is not None else None,
            link=str(vevent.get("URL") or ""),
        )

    # ---------- создание ----------

    def create_event(
        self,
        summary: str,
        start: datetime,
        duration: timedelta,
        location: Optional[str] = None,
        description: Optional[str] = None,
        rrule: Optional[str] = None,
        all_day: bool = False,
        alarms: Optional[list] = None,
        categories: Optional[list] = None,
        status: Optional[str] = None,
        transp: Optional[str] = None,
        priority: Optional[int] = None,
        link: Optional[str] = None,
    ) -> EventData:
        cal = icalendar.Calendar()
        cal.add("prodid", "-//AI CalDAV Bot//RU")
        cal.add("version", "2.0")
        event = icalendar.Event()
        event.add("uid", str(uuid4()))
        event.add("dtstamp", datetime.now(UTC))
        if all_day:
            start_date = _ensure_aware(start).astimezone(config.TZ).date()
            days = _to_all_day_duration(int(duration.total_seconds() // 60))
            event.add("dtstart", start_date)
            event.add("dtend", start_date + timedelta(days=days.days))
        else:
            start_utc = _ensure_aware(start).astimezone(UTC)
            end_utc = start_utc + duration
            event.add("dtstart", start_utc)
            event.add("dtend", end_utc)
        event.add("summary", summary)
        if rrule:
            event.add("rrule", vRecur.from_ical(rrule))
        if location:
            event.add("location", location)
        if description:
            event.add("description", description)
        self._apply_props(event, alarms=alarms, categories=categories, status=status, transp=transp, priority=priority, link=link)
        cal.add_component(event)
        try:
            with self._lock:
                created = self.calendar.save_event(cal.to_ical().decode())
        except Exception as exc:
            raise CalDAVError(f"Не удалось создать событие: {exc}") from exc
        if created is not None:
            try:
                return self._to_event_data(_get_vevent(created.icalendar_instance), created, bool(rrule), rrule)
            except Exception:
                pass
        start_dt = _ensure_aware(start).astimezone(config.TZ)
        if all_day:
            days = _to_all_day_duration(int(duration.total_seconds() // 60)).days
            fb_start = datetime.combine(start_dt.date(), time.min, tzinfo=config.TZ)
            fb_end = datetime.combine(start_dt.date() + timedelta(days=days), time.min, tzinfo=config.TZ)
        else:
            start_utc = start_dt.astimezone(UTC)
            fb_start = _norm(start_utc)
            fb_end = _norm(start_utc + duration)
        return EventData(
            url="",
            uid=str(event.get("UID") or ""),
            summary=summary,
            location=location or "",
            description=description or "",
            start=fb_start,
            end=fb_end,
            all_day=all_day,
            is_recurring=bool(rrule),
            rrule=rrule,
            alarms=_get_alarms(event),
            categories=categories or [],
            status=status or "",
            transp=transp or "",
            priority=priority,
            link=link or "",
        )

    @staticmethod
    def _apply_props(
        vevent: icalendar.Event,
        alarms: Optional[list] = None,
        categories: Optional[list] = None,
        status: Optional[str] = None,
        transp: Optional[str] = None,
        priority: Optional[int] = None,
        link: Optional[str] = None,
    ) -> None:
        if alarms is not None:
            _set_alarms(vevent, alarms)
        if categories:
            vevent.add("categories", _parse_categories(categories))
        if status:
            vevent.add("status", str(status).upper())
        if transp:
            vevent.add("transp", str(transp).upper())
        if priority is not None:
            vevent.add("priority", int(priority))
        if link:
            vevent.add("url", str(link))

    # ---------- удаление ----------

    def delete_event(self, ev: EventData) -> None:
        """Удалить весь объект (для одиночного события или всей серии)."""
        try:
            with self._lock:
                target = self.calendar.event_by_url(ev.url)
                target.delete()
        except CalDAVError:
            raise
        except Exception as exc:
            raise CalDAVError(f"Не удалось удалить событие: {exc}") from exc

    def exclude_occurrence(self, ev: EventData) -> None:
        """Добавить EXDATE в мастер-событие (удалить одно вхождение серии)."""
        if ev.instance_start is None:
            raise CalDAVError("Не указано вхождение для исключения")
        try:
            with self._lock:
                target = self.calendar.event_by_url(ev.url)
                cal = target.icalendar_instance
                vevent = _get_vevent(cal)
                raw_start = vevent.decoded("DTSTART")
                if _is_all_day(raw_start):
                    exdate_value = ev.instance_start.date()
                else:
                    exdate_value = ev.instance_start.astimezone(UTC)
                existing = vevent.get("EXDATE")
                values: list = []
                if existing is not None:
                    for prop in existing if isinstance(existing, list) else [existing]:
                        values.extend(p.dt for p in prop.dts)
                values.append(exdate_value)
                values = list(dict.fromkeys(values))
                if existing is not None:
                    del vevent["EXDATE"]
                vevent["EXDATE"] = vDDDLists(values)
                target.data = cal.to_ical()
                target.save()
        except CalDAVError:
            raise
        except Exception as exc:
            raise CalDAVError(f"Не удалось исключить вхождение: {exc}") from exc

    # ---------- изменение ----------

    def update_event(self, ev: EventData, changes: dict) -> None:
        """Обновить мастер-событие (все вхождения серии или одиночное событие)."""
        try:
            with self._lock:
                target = self.calendar.event_by_url(ev.url)
                cal = target.icalendar_instance
                vevent = _get_vevent(cal)
                raw_start = vevent.decoded("DTSTART")
                all_day = _is_all_day(raw_start)
                old_start = _as_dt(raw_start) if all_day else _norm(raw_start)
                old_duration = self._duration_of(vevent, raw_start, all_day)

                new_start = old_start
                if changes.get("start"):
                    new_start = _parse_dt(changes["start"])
                new_duration = old_duration

                # Разрешение all_day/длительности.
                #
                # Авто-конвертация: у all-day мастера без явного флага all_day
                # длительность в минутах, не кратная суткам, означает «снять
                # весь день» (timed-событие на N минут). Кратная 1440 — остаётся
                # «весь день» на N//1440 дня.
                explicit_all_day = "all_day" in changes
                if explicit_all_day and bool(changes["all_day"]) and not all_day:
                    new_all_day = True
                    if changes.get("duration") is not None:
                        new_duration = _to_all_day_duration(int(changes["duration"]))
                    else:
                        new_duration = timedelta(days=1)
                elif explicit_all_day and not bool(changes["all_day"]):
                    new_all_day = False
                    if changes.get("duration"):
                        new_duration = timedelta(minutes=int(changes["duration"]))
                    elif all_day:
                        new_duration = timedelta(minutes=old_duration.days * 1440)
                elif all_day and changes.get("duration") is not None:
                    minutes = int(changes["duration"])
                    if minutes % 1440 == 0:
                        new_all_day = True
                        new_duration = _to_all_day_duration(minutes)
                    else:
                        new_all_day = False
                        new_duration = timedelta(minutes=minutes)
                else:
                    new_all_day = all_day
                    if all_day and changes.get("duration") is not None:
                        new_duration = _to_all_day_duration(int(changes["duration"]))
                    elif changes.get("duration"):
                        new_duration = timedelta(minutes=int(changes["duration"]))

                aligned = _align_weekly_byday(vevent, changes, new_start)
                if aligned is not None:
                    changes = aligned

                new_summary = changes.get("summary")
                new_location = changes.get("location")
                new_description = changes.get("description")

                if new_summary:
                    _replace_prop(vevent, "SUMMARY", new_summary)
                if new_location is not None:
                    if new_location:
                        _replace_prop(vevent, "LOCATION", new_location)
                    else:
                        _drop_prop(vevent, "LOCATION")
                if new_description is not None:
                    if new_description:
                        _replace_prop(vevent, "DESCRIPTION", new_description)
                    else:
                        _drop_prop(vevent, "DESCRIPTION")

                if changes.get("alarms") is not None:
                    _set_alarms(vevent, changes["alarms"])
                if changes.get("categories") is not None:
                    cats = _parse_categories(changes["categories"])
                    if cats:
                        _replace_prop(vevent, "CATEGORIES", cats)
                    else:
                        _drop_prop(vevent, "CATEGORIES")
                if changes.get("status") is not None:
                    if changes["status"]:
                        _replace_prop(vevent, "STATUS", str(changes["status"]).upper())
                    else:
                        _drop_prop(vevent, "STATUS")
                if changes.get("transp") is not None:
                    if changes["transp"]:
                        _replace_prop(vevent, "TRANSP", str(changes["transp"]).upper())
                    else:
                        _drop_prop(vevent, "TRANSP")
                if changes.get("priority") is not None:
                    if changes["priority"]:
                        _replace_prop(vevent, "PRIORITY", int(changes["priority"]))
                    else:
                        _drop_prop(vevent, "PRIORITY")
                if changes.get("link") is not None:
                    if changes["link"]:
                        _replace_prop(vevent, "URL", str(changes["link"]))
                    else:
                        _drop_prop(vevent, "URL")

                if any(k in changes for k in ("rrule", "until", "count", "freq", "interval", "byday")):
                    _patch_rrule(vevent, changes)

                self._set_start(vevent, new_start, new_duration, new_all_day)

                target.data = cal.to_ical()
                target.save()
        except CalDAVError:
            raise
        except Exception as exc:
            raise CalDAVError(f"Не удалось изменить событие: {exc}") from exc

    @staticmethod
    def _duration_of(vevent, raw_start, all_day: bool) -> timedelta:
        if vevent.get("DTEND") is not None:
            raw_end = vevent.decoded("DTEND")
            if all_day:
                return timedelta(days=(raw_end - raw_start).days)
            return _ensure_aware(raw_end) - _ensure_aware(raw_start)
        if vevent.get("DURATION") is not None:
            return vevent.decoded("DURATION")
        return timedelta(hours=1)

    @staticmethod
    def _set_start(vevent, new_start: datetime, new_duration: timedelta, all_day: bool) -> None:
        if "DURATION" in vevent:
            del vevent["DURATION"]
        if all_day:
            start_date = new_start.date()
            _replace_prop(vevent, "DTSTART", start_date)
            _replace_prop(vevent, "DTEND", start_date + new_duration)
            return
        new_start = _ensure_aware(new_start).astimezone(UTC)
        _replace_prop(vevent, "DTSTART", new_start)
        _replace_prop(vevent, "DTEND", new_start + new_duration)


# ---------- модульный фасад ----------

_client: Optional[CalDAVClient] = None
_client_lock = threading.Lock()


def get_client() -> CalDAVClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = CalDAVClient()
    return _client


def list_events(start: datetime, end: datetime) -> list[EventData]:
    return get_client().list_events(start, end)


def create_event(
    summary: str,
    start: datetime,
    duration: timedelta,
    location: Optional[str] = None,
    description: Optional[str] = None,
    rrule: Optional[str] = None,
    all_day: bool = False,
    alarms: Optional[list] = None,
    categories: Optional[list] = None,
    status: Optional[str] = None,
    transp: Optional[str] = None,
    priority: Optional[int] = None,
    link: Optional[str] = None,
) -> EventData:
    return get_client().create_event(
        summary,
        start,
        duration,
        location,
        description,
        rrule,
        all_day,
        alarms,
        categories,
        status,
        transp,
        priority,
        link,
    )


def delete_event(ev: EventData) -> None:
    get_client().delete_event(ev)


def exclude_occurrence(ev: EventData) -> None:
    get_client().exclude_occurrence(ev)


def update_event(ev: EventData, changes: dict) -> None:
    get_client().update_event(ev, changes)
