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

import config

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
    series_count: int = 1  # сколько вхождений серии в запрошенном периоде
    series_first: Optional[datetime] = None
    series_last: Optional[datetime] = None
    exdates: list = field(default_factory=list)  # исключённые даты мастера (локальные date)

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


def _replace_prop(vevent: icalendar.Event, key: str, value) -> None:
    """Замена свойства с корректным типом (напрямую нельзы — ломает vCal)."""
    if key in vevent:
        del vevent[key]
    vevent.add(key, value)


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
    ) -> EventData:
        start_utc = _ensure_aware(start).astimezone(UTC)
        end_utc = start_utc + duration
        cal = icalendar.Calendar()
        cal.add("prodid", "-//AI CalDAV Bot//RU")
        cal.add("version", "2.0")
        event = icalendar.Event()
        event.add("uid", str(uuid4()))
        event.add("dtstamp", datetime.now(UTC))
        event.add("dtstart", start_utc)
        event.add("dtend", end_utc)
        event.add("summary", summary)
        if rrule:
            event.add("rrule", vRecur.from_ical(rrule))
        if location:
            event.add("location", location)
        if description:
            event.add("description", description)
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
        return EventData(
            url="",
            uid=str(event.get("UID") or ""),
            summary=summary,
            location=location or "",
            description=description or "",
            start=_norm(start_utc),
            end=_norm(end_utc),
            all_day=False,
            is_recurring=bool(rrule),
            rrule=rrule,
        )

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
                elif changes.get("shift_minutes"):
                    new_start = old_start + timedelta(minutes=int(changes["shift_minutes"]))
                new_duration = (
                    timedelta(minutes=int(changes["duration"]))
                    if changes.get("duration")
                    else old_duration
                )

                new_summary = changes.get("summary")
                new_location = changes.get("location")

                if new_summary:
                    _replace_prop(vevent, "SUMMARY", new_summary)
                if new_location is not None:
                    if new_location:
                        _replace_prop(vevent, "LOCATION", new_location)
                    elif "LOCATION" in vevent:
                        del vevent["LOCATION"]

                self._set_start(vevent, new_start, new_duration, all_day)

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
) -> EventData:
    return get_client().create_event(summary, start, duration, location, description, rrule)


def delete_event(ev: EventData) -> None:
    get_client().delete_event(ev)


def exclude_occurrence(ev: EventData) -> None:
    get_client().exclude_occurrence(ev)


def update_event(ev: EventData, changes: dict) -> None:
    get_client().update_event(ev, changes)
