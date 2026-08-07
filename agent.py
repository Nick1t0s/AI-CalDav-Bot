"""Агент с function calling: фраза пользователя → ответ + план действий.

Агент может читать календарь (list_events) и планировать изменения
(propose_create / propose_delete / propose_update). Изменения НЕ выполняются
в цикле: они накапливаются в план, который скрипт показывает кнопкой
подтверждения (см. handlers.py) и исполняет после подтверждения.

История сообщений хранится на каждый chat_id (in-memory, с TTL).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from icalendar import vRecur

import config
import caldav_service
from caldav_service import collapse_events
from confirmation import PlanAction
from formatting import format_catalog

logger = logging.getLogger(__name__)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_events",
            "description": (
                "Найти события в календаре. Даты опциональны (без них — следующие 90 дней). "
                "query — подстрока названия/места/описания. Возвращает пронумерованный список "
                "с токенами [eN], которые нужны для propose_delete / propose_update."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date_from": {"type": "string", "description": "YYYY-MM-DD, начало периода"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD, конец периода"},
                    "query": {"type": "string", "description": "подстрока для фильтрации"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_create",
            "description": "Запланировать создание события. Исполнится после подтверждения пользователем.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "название события"},
                    "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM:SS в часовом поясе пользователя"},
                    "duration": {"type": "integer", "description": "длительность в минутах, по умолчанию 60"},
                    "location": {"type": "string"},
                    "description": {"type": "string"},
                    "rrule": {"type": "string", "description": "iCal RRULE: FREQ=WEEKLY;BYDAY=MO и т.п."},
                },
                "required": ["summary", "start"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_delete",
            "description": "Запланировать удаление события по токену ref из list_events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "токен [eN] из list_events"},
                    "scope": {
                        "type": "string",
                        "enum": ["instance", "all"],
                        "description": "instance — только одно вхождение, all — вся серия (для одиночного события неважно)",
                    },
                },
                "required": ["ref", "scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_update",
            "description": "Запланировать изменение события по токену ref из list_events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "токен [eN] из list_events"},
                    "scope": {
                        "type": "string",
                        "enum": ["instance", "all"],
                        "description": "instance — только одно вхождение (для серии), all — вся серия",
                    },
                    "changes": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "start": {"type": "string", "description": "новое время начала YYYY-MM-DDTHH:MM:SS"},
                            "shift_minutes": {"type": "integer", "description": "сдвиг начала: +позже, -раньше"},
                            "duration": {"type": "integer", "description": "новая длительность в минутах"},
                            "location": {"type": "string"},
                        },
                    },
                },
                "required": ["ref", "changes"],
            },
        },
    },
]


SYSTEM_TEMPLATE = """Ты — ассистент, который управляет календарём пользователя через инструменты. \
Ты НЕ выполняешь изменения сам: любое создание/изменение/удаление ты только планируешь через \
propose_* инструменты. Подтверждает пользователь кнопкой, а исполняет скрипт.

Дата сегодня: {date} ({date_dmy}). Сейчас: {time}. Часовой пояс: {tz}.

Правила:
1. Чтобы найти или посмотреть события, вызывай list_events. НЕ выдумывай события, даты и факты о \
календаре — сначала всегда запрашивай его через list_events.
2. «ближайшее/следующее/предстоящее» вхождение — вызови list_events без дат (по умолчанию следующие \
90 дней) и выбери событие с ближайшей датой. Не угадывай дату сам.
3. Если list_events вернул «Ничего не найдено» — так и ответь («Удалять нечего», «Событий нет»), \
без выдумок.
4. Если подходит несколько событий — выбери одно наиболее подходящее (ближайшее по дате, точное \
совпадение названия). Если без уточнения выбрать нельзя — задай вопрос обычным текстом и дождись ответа.
5. Одноразовое событие и повторяющаяся серия с одинаковым названием — это РАЗНЫЕ события. \
«Все повторения / всю серию» — только про серию (scope="all"), не трогая одноразовые события. \
«Удали X» без уточнения — одно действие: ближайшее вхождение (либо серию, если одноразового нет).
6. НЕ планируй удаление/изменение нескольких событий за один ход, если пользователь явно не просил \
несколько («удали оба», «перенеси обе тренировки» и т.п.).
7. Повторяющиеся события: одно вхождение — scope="instance", вся серия — scope="all". Если из \
запроса непонятно и событие повторяется — спроси пользователя либо выбери instance.
8. Все изменения — только через propose_create / propose_delete / propose_update. Никогда не пиши \
«сделано», «удалено», «создано» — до подтверждения это лишь план.
9. Мульти-действия: за один ответ можно запланировать несколько изменений (несколько propose_delete, \
затем propose_create и т.п.) — они попадут в общий план.
10. Создание: summary и start обязательны (start — YYYY-MM-DDTHH:MM:SS в часовом поясе пользователя), \
duration в минутах (по умолчанию 60). Повтор: «каждый понедельник» → FREQ=WEEKLY;BYDAY=MO, \
«по будням» → FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR, «каждый день» → FREQ=DAILY. Для недельного повтора \
день недели в start обязан совпадать с BYDAY: если дата не указана, возьми ближайший от сегодня \
подходящий день (например, для BYDAY=TU при сегодняшней пятнице — следующий вторник).
11. Изменение: правки клади в changes: summary / start / shift_minutes / duration / location. \
При переносе (start / shift_minutes) сохраняй прежнюю длительность события — duration меняй, \
только если пользователь явно просил («сделай 30 минут»).
12. Отвечай кратко по-русски, без markdown и эмодзи. Не упоминай в тексте пользователю токены ref \
и технические детали инструментов.
"""


class AgentError(Exception):
    """Ошибка работы агента."""


@dataclass
class AgentResult:
    text: str
    plan: list = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(config.TZ)


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)
    return dt.astimezone(config.TZ)


def _day_start(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day, tzinfo=config.TZ)


def build_system_prompt() -> str:
    now = _now()
    return SYSTEM_TEMPLATE.format(
        date=f"{now:%Y-%m-%d}",
        date_dmy=f"{now:%d.%m.%Y}",
        time=f"{now:%H:%M}",
        tz=now.tzinfo,
    )


def _normalize_rrule(value: str) -> Optional[str]:
    value = value.strip()
    if value.upper().startswith("RRULE:"):
        value = value[len("RRULE:"):]
    value = re.sub(r"\s+", "", value).upper()
    if not value:
        return None
    try:
        vRecur.from_ical(value)
    except Exception:
        return None
    return value


def _resolve_period(args: dict) -> tuple[datetime, datetime]:
    now = _now()
    date_from = args.get("date_from")
    date_to = args.get("date_to")
    if date_from:
        start = _day_start(_parse_dt(date_from))
        end = _day_start(_parse_dt(date_to)) + timedelta(days=1) if date_to else start + timedelta(days=1)
    elif date_to:
        start = _day_start(_parse_dt(date_to))
        end = start + timedelta(days=1)
    else:
        start = _day_start(now)
        end = start + timedelta(days=config.LIST_DEFAULT_DAYS)
    return start, end


class CalendarAgent:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[int, dict] = {}
        self._client = None
        self._plan: list[PlanAction] = []

    # ---------- сессии ----------

    def _prune(self) -> None:
        now = time.time()
        ttl = config.AGENT_SESSION_TTL_MIN * 60
        for chat_id in list(self._sessions):
            if now - self._sessions[chat_id]["ts"] > ttl:
                del self._sessions[chat_id]

    def _session(self, chat_id: int) -> dict:
        now = time.time()
        sess = self._sessions.get(chat_id)
        if sess is None or now - sess["ts"] > config.AGENT_SESSION_TTL_MIN * 60:
            sess = {"messages": [], "refs": {}, "ts": now}
            self._sessions[chat_id] = sess
        sess["ts"] = now
        return sess

    def _append(self, chat_id: int, msg: dict) -> None:
        sess = self._session(chat_id)
        sess["messages"].append(msg)
        limit = config.AGENT_HISTORY_LIMIT
        if len(sess["messages"]) > limit:
            sess["messages"] = sess["messages"][-limit:]

    def append_assistant_text(self, chat_id: int, text: str) -> None:
        with self._lock:
            self._append(chat_id, {"role": "assistant", "content": text})

    # ---------- LLM ----------

    def _ask(self, messages: list[dict]):
        if not config.OPENAI_API_KEY:
            raise AgentError("не настроен OPENAI_API_KEY")
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=config.OPENAI_API_KEY,
                base_url=config.OPENAI_BASE_URL or None,
                timeout=config.REQUESTS_TIMEOUT_SECONDS,
            )
        try:
            return self._client.chat.completions.create(
                model=config.OPENAI_MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0,
            )
        except Exception as exc:
            raise AgentError(f"ошибка LLM: {exc}") from exc

    @staticmethod
    def _to_assistant_msg(choice) -> dict:
        return {
            "role": "assistant",
            "content": choice.content or None,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in (choice.tool_calls or [])
            ],
        }

    # ---------- цикл ----------

    def run(self, chat_id: int, user_text: str) -> AgentResult:
        with self._lock:
            self._prune()
            self._append(chat_id, {"role": "user", "content": user_text})
            self._plan = []
            logger.debug("AGENT chat=%s шаг=start user=%r", chat_id, user_text)
            for step in range(config.AGENT_MAX_STEPS):
                logger.debug("AGENT chat=%s шаг=%d вызов LLM (история=%d сообщений)", chat_id, step + 1, len(self._session(chat_id)["messages"]))
                messages = [{"role": "system", "content": build_system_prompt()}] + self._session(chat_id)["messages"]
                choice = self._ask(messages).choices[0].message
                tool_calls = getattr(choice, "tool_calls", None)
                if not tool_calls:
                    final = (choice.content or "").strip()
                    logger.debug("AGENT chat=%s шаг=%d финальный ответ: %r", chat_id, step + 1, final)
                    logger.debug("AGENT chat=%s шаг=%d план=%d действий", chat_id, step + 1, len(self._plan))
                    self._append(chat_id, {"role": "assistant", "content": final})
                    return AgentResult(text=final, plan=list(self._plan))
                logger.debug("AGENT chat=%s шаг=%d tool_calls=%d", chat_id, step + 1, len(tool_calls))
                self._append(chat_id, self._to_assistant_msg(choice))
                for tc in tool_calls:
                    logger.debug("AGENT chat=%s шаг=%d tool=%s args=%s", chat_id, step + 1, tc.function.name, tc.function.arguments)
                    result = self._call_tool(chat_id, tc.function.name, tc.function.arguments)
                    logger.debug("AGENT chat=%s шаг=%d tool=%s result=%r", chat_id, step + 1, tc.function.name, result[:500])
                    self._append(chat_id, {"role": "tool", "tool_call_id": tc.id, "content": result})
            logger.warning("AGENT chat=%s превышен лимит шагов %d", chat_id, config.AGENT_MAX_STEPS)
            self._append(chat_id, {"role": "assistant", "content": "(максимум шагов достигнут)"})
            return AgentResult(
                text="Не удалось обработать запрос за отведённое число шагов.",
                plan=list(self._plan),
            )

    # ---------- инструменты ----------

    def _call_tool(self, chat_id: int, name: str, arguments: str) -> str:
        try:
            args = json.loads(arguments) if arguments else {}
            if not isinstance(args, dict):
                return "Ошибка: аргументы инструмента должны быть объектом"
        except json.JSONDecodeError:
            return "Ошибка: некорректные аргументы инструмента"
        try:
            if name == "list_events":
                return self._tool_list_events(chat_id, args)
            if name == "propose_create":
                return self._tool_propose_create(args)
            if name == "propose_delete":
                return self._tool_propose_delete(chat_id, args)
            if name == "propose_update":
                return self._tool_propose_update(chat_id, args)
            return f"Неизвестный инструмент: {name}"
        except Exception as exc:
            logger.exception("Инструмент %s упал", name)
            return f"Ошибка при выполнении {name}: {exc}"

    def _tool_list_events(self, chat_id: int, args: dict) -> str:
        start, end = _resolve_period(args)
        query = (args.get("query") or "").strip()
        try:
            events = collapse_events(caldav_service.list_events(start, end))
        except caldav_service.CalDAVError as exc:
            return f"Ошибка CalDAV: {exc}"
        if query:
            q = query.lower()
            events = [
                ev
                for ev in events
                if q in ev.summary.lower()
                or q in ev.location.lower()
                or q in ev.description.lower()
            ]
        if not events:
            return "Ничего не найдено в выбранном периоде."
        catalog, refs = format_catalog(events[: config.AGENT_CATALOG_LIMIT])
        self._session(chat_id)["refs"] = refs
        if len(events) > config.AGENT_CATALOG_LIMIT:
            catalog += f"\n(показаны первые {config.AGENT_CATALOG_LIMIT} из {len(events)})"
        return catalog

    def _tool_propose_create(self, args: dict) -> str:
        summary = (args.get("summary") or "").strip()
        start_iso = args.get("start")
        if not summary or not start_iso:
            return "Ошибка: для создания нужны summary и start (YYYY-MM-DDTHH:MM:SS)"
        try:
            start = _parse_dt(start_iso)
        except ValueError:
            return "Ошибка: некорректный start. Используй формат YYYY-MM-DDTHH:MM:SS"
        duration = max(1, int(args.get("duration") or 60))
        rrule = None
        if args.get("rrule"):
            rrule = _normalize_rrule(args["rrule"])
            if rrule is None:
                return f"Ошибка: некорректный rrule: {args['rrule']}"
        payload = {
            "summary": summary,
            "start": start,
            "duration": timedelta(minutes=duration),
            "location": (args.get("location") or "").strip() or None,
            "description": (args.get("description") or "").strip() or None,
            "rrule": rrule,
        }
        self._plan.append(PlanAction(kind="create", payload=payload))
        return f"Действие {len(self._plan)} запланировано: создать «{summary}». Дождись подтверждения."

    def _tool_propose_delete(self, chat_id: int, args: dict) -> str:
        ref = (args.get("ref") or "").strip()
        ev = self._session(chat_id)["refs"].get(ref)
        if ev is None:
            return f"Ошибка: неизвестный ref '{ref}'. Вызови list_events заново и используй свежие токены."
        scope = args.get("scope") or "instance"
        if scope not in ("instance", "all"):
            scope = "instance"
        self._plan.append(PlanAction(kind="delete", event=ev, scope=scope))
        return (
            f"Действие {len(self._plan)} запланировано: удалить «{ev.summary}»"
            f" (scope={scope}). Дождись подтверждения."
        )

    def _tool_propose_update(self, chat_id: int, args: dict) -> str:
        ref = (args.get("ref") or "").strip()
        ev = self._session(chat_id)["refs"].get(ref)
        if ev is None:
            return f"Ошибка: неизвестный ref '{ref}'. Вызови list_events заново и используй свежие токены."
        changes = args.get("changes") or {}
        if not isinstance(changes, dict) or not changes:
            return "Ошибка: нужно заполнить changes."
        for key in ("summary", "start", "location"):
            if key in changes and isinstance(changes[key], str):
                changes[key] = changes[key].strip() or None
        if not any(changes.get(k) for k in ("summary", "start", "shift_minutes", "duration", "location")):
            return "Ошибка: не указано, что именно изменить."
        scope = args.get("scope") or ("instance" if ev.is_recurring else "single")
        if scope not in ("instance", "all", "single"):
            scope = "instance"
        self._plan.append(PlanAction(kind="update", event=ev, scope=scope, changes=changes))
        return f"Действие {len(self._plan)} запланировано: изменить «{ev.summary}». Дождись подтверждения."


# ---------- модульный фасад ----------

_agent = CalendarAgent()


def run_agent(chat_id: int, user_text: str) -> AgentResult:
    return _agent.run(chat_id, user_text)


def append_assistant_text(chat_id: int, text: str) -> None:
    _agent.append_assistant_text(chat_id, text)
