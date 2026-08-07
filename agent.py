"""Агент с function calling: фраза пользователя → ответ + план действий.

Парадигма «всё есть инструмент»: агент работает только через инструменты.
Четыре инструмента:
  - get_period — чтение календаря (исполняется сразу);
  - reg_list   — регистрация списка изменений в план сессии;
  - ask_user   — вопрос пользователю (пауза: вопрос уходит в чат с кнопками
                 вариантов, ответ подаётся модели как результат инструмента);
  - done       — завершение хода (фиксирует план для подтверждения кнопкой).

Изменения НЕ выполняются в цикле: они копятся в session["plan"], а после done
показываются кнопкой подтверждения (см. handlers.py) и исполняются скриптом.

История сообщений, план и ожидающие вопросы хранятся на каждый chat_id
(in-memory, с TTL).
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
from formatting import format_catalog_grouped
from asks import AskQ, register_ask as register_ask_op

logger = logging.getLogger(__name__)


def build_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "get_period",
                "description": (
                    "Все события за период, сгруппированы по дням. Без дат — следующие "
                    f"{config.LIST_DEFAULT_DAYS} дней. Возвращает пронумерованный список "
                    "с токенами [eN], которые нужны для reg_list."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date_from": {"type": "string", "description": "YYYY-MM-DD, начало периода (включительно)"},
                        "date_to": {"type": "string", "description": "YYYY-MM-DD, конец периода (включительно)"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "reg_list",
                "description": (
                    "Зарегистрировать список изменений в план одним вызовом. Каждый элемент actions — "
                    "объект с op='add' | 'delete' | 'update' и полями конкретного действия. "
                    "Исполнится после подтверждения пользователем."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "actions": {
                            "type": "array",
                            "description": (
                                "add: summary + start (YYYY-MM-DDTHH:MM:SS), duration в минутах (по умолчанию 60), "
                                "location, description, rrule. delete: ref (токен [eN]) + scope (instance|all). "
                                "update: ref + scope (instance|all) + changes {summary, start, shift_minutes, "
                                "duration, location}."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "op": {"type": "string", "enum": ["add", "delete", "update"]},
                                    "summary": {"type": "string"},
                                    "start": {"type": "string"},
                                    "duration": {"type": "integer"},
                                    "location": {"type": "string"},
                                    "description": {"type": "string"},
                                    "rrule": {"type": "string"},
                                    "ref": {"type": "string"},
                                    "scope": {"type": "string", "enum": ["instance", "all"]},
                                    "changes": {
                                        "type": "object",
                                        "properties": {
                                            "summary": {"type": "string"},
                                            "start": {"type": "string"},
                                            "shift_minutes": {"type": "integer"},
                                            "duration": {"type": "integer"},
                                            "location": {"type": "string"},
                                        },
                                    },
                                },
                                "required": ["op"],
                            },
                        },
                    },
                    "required": ["actions"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ask_user",
                "description": (
                    "Задать вопрос пользователю. question — текст вопроса, options — 1–4 варианта "
                    "ответа (кнопками). Диалог приостановится до ответа пользователя."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "текст вопроса"},
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 4,
                            "description": "варианты ответа (кнопки)",
                        },
                    },
                    "required": ["question", "options"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "done",
                "description": (
                    "Завершить ход: message — финальный ответ пользователю, зарегистрированный "
                    "через reg_list план будет показан на подтверждение. Каждый ход должен "
                    "заканчиваться вызовом done либо ask_user."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "финальный ответ пользователю"},
                    },
                    "required": ["message"],
                },
            },
        },
    ]


TOOLS = build_tools()


SYSTEM_TEMPLATE = """Ты — ассистент, который управляет календарём пользователя через инструменты. \
Каждый ход ты обязан закончить вызовом done (финальный ответ) или ask_user (уточняющий вопрос) — \
обычный текст без инструментов недопустим. Изменения ты НЕ выполняешь сам: любое \
создание/изменение/удаление ты регистрируешь через reg_list, а подтверждает пользователь \
кнопкой, исполняет скрипт.

Дата сегодня: {date} ({date_dmy}). Сейчас: {time}. Часовой пояс: {tz}.

Правила:
1. Чтобы найти или посмотреть события, вызывай get_period. НЕ выдумывай события, даты и факты о \
календаре — сначала всегда запрашивай его через get_period.
2. «ближайшее/следующее/предстоящее» вхождение — вызови get_period без дат (по умолчанию следующие \
{list_days} дней) и выбери событие с ближайшей датой. Не угадывай дату сам.
3. Если get_period вернул «Ничего не найдено» — в done ответь «Удалять нечего», «Событий нет» и т.п., \
без выдумок.
4. Если подходит несколько событий — выбери одно наиболее подходящее (ближайшее по дате, точное \
совпадение названия). Если без уточнения выбрать нельзя — задай вопрос через ask_user с вариантами.
5. Одноразовое событие и повторяющаяся серия с одинаковым названием — это РАЗНЫЕ события. \
«Все повторения / всю серию» — только про серию (scope="all"), не трогая одноразовые события. \
«Удали X» без уточнения — одно действие: ближайшее вхождение (либо серию, если одноразового нет).
6. НЕ планируй удаление/изменение нескольких событий за один ход, если пользователь явно не просил \
несколько («удали оба», «перенеси обе тренировки» и т.п.). Все изменения одного хода — ОДНИМ \
вызовом reg_list со списком actions.
7. Повторяющиеся события: одно вхождение — scope="instance", вся серия — scope="all". Если из \
запроса непонятно и событие повторяется — спроси пользователя через ask_user либо выбери instance.
8. Все изменения — только через reg_list. Никогда не пиши «сделано», «удалено», «создано» — до \
подтверждения это лишь план.
9. Создание (op="add"): summary и start обязательны (start — YYYY-MM-DDTHH:MM:SS в часовом поясе \
пользователя), duration в минутах (по умолчанию 60). Повтор: «каждый понедельник» → \
FREQ=WEEKLY;BYDAY=MO, «по будням» → FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR, «каждый день» → FREQ=DAILY. \
Для недельного повтора день недели в start обязан совпадать с BYDAY: если дата не указана, возьми \
ближайший от сегодня подходящий день (например, для BYDAY=TU при сегодняшней пятнице — следующий \
вторник).
10. Изменение (op="update"): правки клади в changes: summary / start / shift_minutes / duration / \
location. При переносе (start / shift_minutes) сохраняй прежнюю длительность события — duration \
меняй, только если пользователь явно просил («сделай 30 минут»).
11. ask_user: question — текст вопроса, options — 1–4 варианта ответа. Вызывай его отдельно, когда \
нужно решение пользователя; после ответа продолжи и заверши done.
12. Отвечай кратко по-русски, без markdown и эмодзи. Не упоминай в тексте пользователю токены ref \
и технические детали инструментов.
"""


class AgentError(Exception):
    """Ошибка работы агента."""


@dataclass
class AgentResult:
    kind: str = "done"  # "done" | "ask" | "error"
    text: str = ""
    plan: list = field(default_factory=list)
    questions: list = field(default_factory=list)  # [{"ask_id", "question", "options"}]


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
        list_days=config.LIST_DEFAULT_DAYS,
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
            sess = {"messages": [], "refs": {}, "plan": [], "pending_asks": [], "ts": now}
            self._sessions[chat_id] = sess
        sess["ts"] = now
        return sess

    def _append(self, chat_id: int, msg: dict) -> None:
        sess = self._session(chat_id)
        sess["messages"].append(msg)
        limit = config.AGENT_HISTORY_LIMIT
        if len(sess["messages"]) > limit:
            sess["messages"] = sess["messages"][-limit:]

    def _append_tool_response(self, chat_id: int, tool_call_id: str, content: str) -> None:
        self._append(chat_id, {"role": "tool", "tool_call_id": tool_call_id, "content": content})

    def append_assistant_text(self, chat_id: int, text: str) -> None:
        with self._lock:
            self._append(chat_id, {"role": "assistant", "content": text})

    def has_pending_asks(self, chat_id: int) -> bool:
        with self._lock:
            sess = self._sessions.get(chat_id)
            if not sess:
                return False
            return any(not q["answered"] for q in sess["pending_asks"])

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

    # ---------- ask_user ----------

    def _register_ask(self, chat_id: int, tool_call_id: str, arguments: str) -> None:
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            args = {}
        question = (args.get("question") or "").strip() or "Пожалуйста, уточните."
        options = [str(o).strip() for o in (args.get("options") or []) if str(o).strip()]
        if not options:
            logger.warning("AGENT chat=%s ask_user без options", chat_id)
        ask_id = register_ask_op(AskQ(user_id=chat_id, tool_call_id=tool_call_id, question=question, options=options))
        sess = self._session(chat_id)
        sess["pending_asks"].append(
            {
                "ask_id": ask_id,
                "tool_call_id": tool_call_id,
                "question": question,
                "options": options,
                "posted": False,
                "answered": False,
            }
        )

    def answer_ask(self, chat_id: int, ask_id: str, content: str) -> bool:
        """Ответ на вопрос кнопкой/текстом. True, если отвечены ВСЕ вопросы раунда."""
        with self._lock:
            sess = self._session(chat_id)
            found = False
            for q in sess["pending_asks"]:
                if q["ask_id"] == ask_id:
                    self._append_tool_response(chat_id, q["tool_call_id"], content)
                    q["answered"] = True
                    found = True
                    logger.debug("AGENT chat=%s ответ на вопрос %s: %r", chat_id, ask_id, content)
                    break
            if not found:
                return True
            return all(q["answered"] for q in sess["pending_asks"])

    # ---------- цикл ----------

    def _loop(self, chat_id: int) -> AgentResult:
        for step in range(config.AGENT_MAX_STEPS):
            logger.debug(
                "AGENT chat=%s шаг=%d вызов LLM (история=%d сообщений)",
                chat_id, step + 1, len(self._session(chat_id)["messages"]),
            )
            messages = [{"role": "system", "content": build_system_prompt()}] + self._session(chat_id)["messages"]
            choice = self._ask(messages).choices[0].message
            tool_calls = getattr(choice, "tool_calls", None)
            if not tool_calls:
                final = (choice.content or "").strip()
                logger.debug("AGENT chat=%s шаг=%d текст без инструментов: %r", chat_id, step + 1, final)
                self._append(chat_id, {"role": "assistant", "content": final or "—"})
                return AgentResult(kind="error", text="Не удалось завершить ход — агент не вызвал done. Попробуйте ещё раз.")

            self._append(chat_id, self._to_assistant_msg(choice))
            logger.debug("AGENT chat=%s шаг=%d tool_calls=%d", chat_id, step + 1, len(tool_calls))
            asked = False
            done_msg = None
            for tc in tool_calls:
                name = tc.function.name
                logger.debug("AGENT chat=%s шаг=%d tool=%s args=%s", chat_id, step + 1, name, tc.function.arguments)
                if name == "ask_user":
                    asked = True
                    self._register_ask(chat_id, tc.id, tc.function.arguments)
                    continue
                if name == "done":
                    try:
                        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except json.JSONDecodeError:
                        args = {}
                    done_msg = (args.get("message") or "Готово.").strip()
                    self._append_tool_response(chat_id, tc.id, "Принято.")
                    continue
                result = self._call_tool(chat_id, name, tc.function.arguments)
                logger.debug("AGENT chat=%s шаг=%d tool=%s result=%r", chat_id, step + 1, name, result[:500])
                self._append_tool_response(chat_id, tc.id, result)

            if asked:
                sess = self._session(chat_id)
                questions = [q for q in sess["pending_asks"] if not q["posted"]]
                for q in questions:
                    q["posted"] = True
                logger.debug("AGENT chat=%s шаг=%d пауза: вопросов=%d", chat_id, step + 1, len(questions))
                return AgentResult(
                    kind="ask",
                    questions=[{"ask_id": q["ask_id"], "question": q["question"], "options": q["options"]} for q in questions],
                )

            if done_msg is not None:
                sess = self._session(chat_id)
                plan = list(sess["plan"])
                sess["plan"] = []
                self._append(chat_id, {"role": "assistant", "content": done_msg})
                logger.debug("AGENT chat=%s шаг=%d done: план=%d действий", chat_id, step + 1, len(plan))
                return AgentResult(kind="done", text=done_msg, plan=plan)

        logger.warning("AGENT chat=%s превышен лимит шагов %d", chat_id, config.AGENT_MAX_STEPS)
        return AgentResult(
            kind="error",
            text="Не удалось обработать запрос за отведённое число шагов.",
        )

    def run(self, chat_id: int, user_text: str) -> AgentResult:
        with self._lock:
            self._prune()
            sess = self._session(chat_id)
            unanswered = [q for q in sess["pending_asks"] if not q["answered"]]
            if unanswered:
                # Текст пользователя — ответ на первый неотвеченный вопрос.
                q = unanswered[0]
                self._append_tool_response(chat_id, q["tool_call_id"], user_text)
                q["answered"] = True
                logger.debug("AGENT chat=%s текстовый ответ на %s", chat_id, q["ask_id"])
                if any(not x["answered"] for x in sess["pending_asks"]):
                    return AgentResult(kind="ask")  # ждём остальные вопросы раунда
                sess["pending_asks"] = []
                return self._loop(chat_id)
            self._append(chat_id, {"role": "user", "content": user_text})
            logger.debug("AGENT chat=%s шаг=start user=%r", chat_id, user_text)
            return self._loop(chat_id)

    def resume(self, chat_id: int) -> AgentResult:
        """Продолжить цикл после ответов на все вопросы раунда."""
        with self._lock:
            self._prune()
            sess = self._session(chat_id)
            if any(not q["answered"] for q in sess["pending_asks"]):
                return AgentResult(kind="ask")
            sess["pending_asks"] = []
            logger.debug("AGENT chat=%s возобновление цикла", chat_id)
            return self._loop(chat_id)

    # ---------- инструменты ----------

    def _call_tool(self, chat_id: int, name: str, arguments: str) -> str:
        try:
            args = json.loads(arguments) if arguments else {}
            if not isinstance(args, dict):
                return "Ошибка: аргументы инструмента должны быть объектом"
        except json.JSONDecodeError:
            return "Ошибка: некорректные аргументы инструмента"
        try:
            if name == "get_period":
                return self._tool_get_period(chat_id, args)
            if name == "reg_list":
                return self._tool_reg_list(chat_id, args)
            return f"Неизвестный инструмент: {name}"
        except Exception as exc:
            logger.exception("Инструмент %s упал", name)
            return f"Ошибка при выполнении {name}: {exc}"

    def _tool_get_period(self, chat_id: int, args: dict) -> str:
        start, end = _resolve_period(args)
        try:
            events = collapse_events(caldav_service.list_events(start, end))
        except caldav_service.CalDAVError as exc:
            return f"Ошибка CalDAV: {exc}"
        if not events:
            return "Ничего не найдено в выбранном периоде."
        catalog, refs = format_catalog_grouped(events[: config.AGENT_CATALOG_LIMIT])
        self._session(chat_id)["refs"] = refs
        if len(events) > config.AGENT_CATALOG_LIMIT:
            catalog += f"\n(показаны первые {config.AGENT_CATALOG_LIMIT} из {len(events)})"
        return catalog

    # --- построение действий для reg_list ---

    def _build_add(self, args: dict) -> PlanAction:
        summary = (args.get("summary") or "").strip()
        start_iso = args.get("start")
        if not summary or not start_iso:
            raise ValueError("для add нужны summary и start (YYYY-MM-DDTHH:MM:SS)")
        try:
            start = _parse_dt(start_iso)
        except ValueError:
            raise ValueError("некорректный start. Используй формат YYYY-MM-DDTHH:MM:SS") from None
        try:
            duration = max(1, int(args.get("duration") or 60))
        except (TypeError, ValueError):
            raise ValueError("некорректный duration (минуты)") from None
        rrule = None
        if args.get("rrule"):
            rrule = _normalize_rrule(args["rrule"])
            if rrule is None:
                raise ValueError(f"некорректный rrule: {args['rrule']}")
        payload = {
            "summary": summary,
            "start": start,
            "duration": timedelta(minutes=duration),
            "location": (args.get("location") or "").strip() or None,
            "description": (args.get("description") or "").strip() or None,
            "rrule": rrule,
        }
        return PlanAction(kind="create", payload=payload)

    def _build_delete(self, chat_id: int, args: dict) -> PlanAction:
        ref = (args.get("ref") or "").strip()
        ev = self._session(chat_id)["refs"].get(ref)
        if ev is None:
            raise ValueError(f"неизвестный ref '{ref}'. Вызови get_period заново и используй свежие токены.")
        scope = args.get("scope") or "instance"
        if scope not in ("instance", "all"):
            raise ValueError("scope должен быть instance или all")
        return PlanAction(kind="delete", event=ev, scope=scope)

    def _build_update(self, chat_id: int, args: dict) -> PlanAction:
        ref = (args.get("ref") or "").strip()
        ev = self._session(chat_id)["refs"].get(ref)
        if ev is None:
            raise ValueError(f"неизвестный ref '{ref}'. Вызови get_period заново и используй свежие токены.")
        changes = args.get("changes") or {}
        if not isinstance(changes, dict) or not changes:
            raise ValueError("нужно заполнить changes.")
        for key in ("summary", "start", "location"):
            if key in changes and isinstance(changes[key], str):
                changes[key] = changes[key].strip() or None
        if not any(changes.get(k) for k in ("summary", "start", "shift_minutes", "duration", "location")):
            raise ValueError("не указано, что именно изменить.")
        scope = args.get("scope") or ("instance" if ev.is_recurring else "single")
        if scope not in ("instance", "all", "single"):
            scope = "instance"
        return PlanAction(kind="update", event=ev, scope=scope, changes=changes)

    def _tool_reg_list(self, chat_id: int, args: dict) -> str:
        actions = args.get("actions")
        if not isinstance(actions, list) or not actions:
            return "Ошибка: reg_list требует actions — непустой список действий."
        plan = self._session(chat_id)["plan"]
        lines: list[str] = []
        for i, act in enumerate(actions, 1):
            if not isinstance(act, dict):
                lines.append(f"{i}. ❌ действие не объект")
                continue
            op = act.get("op")
            try:
                if op == "add":
                    action = self._build_add(act)
                elif op == "delete":
                    action = self._build_delete(chat_id, act)
                elif op == "update":
                    action = self._build_update(chat_id, act)
                else:
                    lines.append(f"{i}. ❌ неизвестный op: {op!r}")
                    continue
            except ValueError as exc:
                lines.append(f"{i}. ❌ {exc}")
                continue
            plan.append(action)
            lines.append(f"{i}. ✅ {_action_label(action)}")
        return "План зарегистрирован:\n" + "\n".join(lines) if lines else "План пуст."


def _action_label(action: PlanAction) -> str:
    if action.kind == "create":
        return f"создать «{action.payload['summary']}»"
    ev = action.event
    if action.kind == "delete":
        return f"удалить «{ev.summary}» (scope={action.scope})"
    if action.kind == "update":
        return f"изменить «{ev.summary}» (scope={action.scope})"
    return "неизвестное действие"


# ---------- модульный фасад ----------

_agent = CalendarAgent()


def run_agent(chat_id: int, user_text: str) -> AgentResult:
    return _agent.run(chat_id, user_text)


def resume_agent(chat_id: int) -> AgentResult:
    return _agent.resume(chat_id)


def answer_ask(chat_id: int, ask_id: str, content: str) -> bool:
    return _agent.answer_ask(chat_id, ask_id, content)


def append_assistant_text(chat_id: int, text: str) -> None:
    _agent.append_assistant_text(chat_id, text)
