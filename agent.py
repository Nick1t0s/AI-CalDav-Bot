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
from caldav_service import EventData, RRULE_FREQS, WEEKDAY_CODES
from confirmation import PlanAction
from formatting import format_catalog_compact
from asks import AskQ, consume_ask as consume_ask_op, register_ask as register_ask_op

logger = logging.getLogger(__name__)


def build_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "get_period",
                "description": (
                    "Все события за период: серии свёрнуты одной строкой (каждый Пн, время, "
                    "кроме …), одиночные — по дням. date_from и date_to ОБЯЗАТЕЛЬНЫ — сам выбирай "
                    "период под запрос. Возвращает пронумерованный список с токенами [eN], которые нужны для reg_list."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date_from": {"type": "string", "description": "YYYY-MM-DD, начало периода (включительно)"},
                        "date_to": {"type": "string", "description": "YYYY-MM-DD, конец периода (включительно)"},
                    },
                    "required": ["date_from", "date_to"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "reg_list",
                "description": (
                    "Зарегистрировать список изменений в план одним вызовом. Каждый элемент actions — "
                    "объект с op='add' | 'delete' | 'exclude' | 'update'. Исполнится после подтверждения пользователем."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "actions": {
                            "type": "array",
                            "description": (
                                "add: summary + start (YYYY-MM-DDTHH:MM:SS), duration в минутах (по умолчанию 60), "
                                "location, description, link (URL), all_day (событие «весь день», duration в днях: 1440=1 день), "
                                "alarms (напоминания: минуты до начала, по умолчанию [60, 15, 5]), categories, status, transp, priority, rrule. "
                                "delete: ref (токен eN события/серии целиком, со скобками [eN] тоже принимается). "
                                "exclude: ref (токен ПОВТОРЯЮЩЕЙСЯ серии) + date (YYYY-MM-DD) — внести одно вхождение "
                                "в список исключений серии (удалить именно это вхождение, не трогая остальные). "
                                "update: ref (токен eN события/серии целиком) + changes — правки ЦЕЛОГО объекта "
                                "(одиночное событие или вся серия, UID сохраняется); см. описание changes. "
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "op": {"type": "string", "enum": ["add", "delete", "exclude", "update"]},
                                    "summary": {"type": "string"},
                                    "start": {"type": "string"},
                                    "duration": {"type": "integer"},
                                    "location": {"type": "string"},
                                    "description": {"type": "string"},
                                    "link": {"type": "string"},
                                    "all_day": {"type": "boolean"},
                                    "alarms": {"type": "array", "items": {"type": "integer"}},
                                    "categories": {"type": "array", "items": {"type": "string"}},
                                    "status": {"type": "string", "enum": ["CONFIRMED", "TENTATIVE", "CANCELLED"]},
                                    "transp": {"type": "string", "enum": ["OPAQUE", "TRANSPARENT"]},
                                    "priority": {"type": "integer"},
                                    "rrule": {"type": "string"},
                                    "ref": {"type": "string"},
                                    "date": {"type": "string", "description": "YYYY-MM-DD, дата вхождения серии для exclude"},
                                    "changes": {
                                        "type": "object",
                                        "description": (
                                            "для op='update': правки ЦЕЛОГО события/серии (UID сохраняется). "
                                            "summary — новое название, start — новая дата YYYY-MM-DDTHH:MM:SS, "
                                            "duration — минуты (для all_day — дни: 1440=1 день), "
                                            "all_day — bool «весь день», location/description/link — новые значения "
                                            "(пустая строка очищает), alarms — напоминания в минутах до начала, "
                                            "categories — список, status/transp/priority — свойства, "
                                            "rrule — полное правило повтора, либо freq/interval/byday/until/count — "
                                            "частичные правки повтора. Одно вхождение серии НЕ обновляется через update "
                                            "(для него используй exclude+add). При переносе недельной серии на другой "
                                            "день недели BYDAY обновится автоматически."
                                        ),
                                        "properties": {
                                            "summary": {"type": "string"},
                                            "start": {"type": "string"},
                                            "duration": {"type": "integer"},
                                            "all_day": {"type": "boolean"},
                                            "location": {"type": "string"},
                                            "description": {"type": "string"},
                                            "link": {"type": "string"},
                                            "alarms": {"type": "array", "items": {"type": "integer"}},
                                            "categories": {"type": "array", "items": {"type": "string"}},
                                            "status": {"type": "string", "enum": ["CONFIRMED", "TENTATIVE", "CANCELLED"]},
                                            "transp": {"type": "string", "enum": ["OPAQUE", "TRANSPARENT"]},
                                            "priority": {"type": "integer"},
                                            "rrule": {"type": "string"},
                                            "freq": {"type": "string", "enum": ["DAILY", "WEEKLY", "MONTHLY", "YEARLY"]},
                                            "interval": {"type": "integer"},
                                            "byday": {"type": "array", "items": {"type": "string"}},
                                            "until": {"type": "string"},
                                            "count": {"type": "integer"},
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
                    "Задать ОДИН вопрос пользователю. question — текст вопроса, options — 1–4 варианта "
                    "ответа (кнопки). Пользователь может выбрать вариант или написать свой ответ текстом. "
                    "Спрашивай по одному вопросу за раз: задай вопрос, дождись ответа, затем продолжай."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "текст вопроса"},
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "варианты ответа (кнопки), от 1 до 4",
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
                    "Завершить ход: message — краткий финальный ответ пользователю, items — "
                    "необязательный список коротких пунктов (например, найденные события), "
                    "которые будут показаны аккуратным списком под ответом. Зарегистрированный "
                    "через reg_list план будет показан на подтверждение. Каждый ход должен "
                    "заканчиваться вызовом done либо ask_user."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "краткий финальный ответ"},
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "короткие пункты списка (необязательно)",
                        },
                    },
                    "required": ["message"],
                },
            },
        },
    ]


TOOLS = build_tools()


SYSTEM_TEMPLATE = """Ты — ассистент, который управляет календарём пользователя через инструменты. \
Ты работаешь «ходами»: один ход — обработка одного сообщения/ответа пользователя. Внутри хода ты \
вызываешь инструменты (get_period, reg_list, ask_user), накапливая план изменений. Каждый ход ОБЯЗАН \
завершиться ровно одним из двух инструментов: done (финальный ответ, ход закончен) либо ask_user \
(уточняющий вопрос — ход ставится на паузу и продолжится после ответа пользователя). Обычный текст \
без инструментов недопустим. За один вызов можно вернуть несколько инструментов (например, \
get_period + ask_user) — они выполнятся в указанном порядке, но вопросы задавай только после сбора \
нужных данных и только по одному за раз.

Дата сегодня: {date} ({date_dmy}). Сейчас: {time}. Часовой пояс: {tz}.

Правила:
1. Чтобы найти или посмотреть события, вызывай get_period. date_from и date_to ОБЯЗАТЕЛЬНЫ — \
всегда задавай конкретный период (YYYY-MM-DD), который покрывает запрос. НЕ выдумывай события, \
даты и факты о календаре — сначала всегда запрашивай его через get_period.
2. «ближайшее/следующее/предстоящее» — вызови get_period на разумный период вперёд \
(например, от сегодня до +30 дней) и выбери событие с ближайшей датой. Не угадывай дату сам.
3. Если get_period вернул «Ничего не найдено» — в done ответь «Удалять нечего», «Событий нет» и т.п., \
без выдумок.
4. Выбор события: если запрос однозначно определяет одно событие (полное название или его \
уникальная часть совпала только с одним) — бери его без вопроса. Спрашивай через ask_user только \
когда несколько событий одинаково подходят и пользователь не назвал событие точно; в options \
перечисляй совпавшие события кратко (название и дата). Пример: «юайти алгоритмы» — однозначно \
«Юайти алгоритмы», не спрашивай; просто «юайти» — подходят оба, задай вопрос.
5. Одноразовое событие и повторяющаяся серия с одинаковым названием — это РАЗНЫЕ события \
(серии показаны в блоке «Серии» одной строкой, одиночные — в «Одиночные события»). \
delete и update всегда работают с ЦЕЛЫМ объектом: delete удаляет весь объект (одиночное событие \
или серию целиком), update изменяет весь объект (сохраняя его UID). ОДНО вхождение серии можно \
только исключить/изменить через exclude + add: exclude (внести дату в список исключений серии) \
и add (создать новое событие с нужными правками).
6. НЕ планируй удаление/изменение нескольких событий за один ход, если пользователь явно не просил \
несколько («удали оба», «перенеси обе тренировки» и т.п.). Все изменения одного хода — ОДНИМ \
вызовом reg_list со списком actions. План накапливается и переживает паузу на ask_user: уже \
зарегистрированные действия повторно НЕ регистрируй (в истории уже есть ответ «План зарегистрирован») \
— после ответа пользователя просто заверши ход вызовом done.
7. Повторяющиеся события: серия показана в каталоге ОДНИМ токеном [eN] с описанием «каждый Пн, \
время, кроме …». Для exclude нужна конкретная дата вхождения (YYYY-MM-DD): возьми её из запроса \
пользователя или вычисли по правилу («следующий понедельник» и т.п.), с учётом исключений «кроме …». \
Если пользователь не сказал явно «одно/это вхождение» или «всю серию/все» — обязательно спроси через \
ask_user (question: «Удалить одно вхождение или всю серию?», options: «Это вхождение», «Всю серию»). \
Для «Это вхождение» уточни дату: если пользователь её не назвал — ask_user с вариантами \
датами вхождений (для серии это даты «каждый Пн», кроме исключений). После ответов зарегистрируй \
план: для одного вхождения — exclude с датой, для всей серии — delete.
8. Все изменения — только через reg_list. Никогда не пиши «сделано», «удалено», «создано» — до \
подтверждения это лишь план.
9. Создание (op="add"): summary и start обязательны (start — YYYY-MM-DDTHH:MM:SS в часовом поясе \
пользователя), duration в минутах (по умолчанию 60). Повтор: «каждый понедельник» → \
FREQ=WEEKLY;BYDAY=MO, «по будням» → FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR, «каждый день» → FREQ=DAILY. \
Для недельного повтора день недели в start обязан совпадать с BYDAY: если дата не указана, возьми \
ближайший от сегодня подходящий день (например, для BYDAY=TU при сегодняшней пятнице — следующий \
вторник). Событие «весь день»: all_day: true и duration в днях (1440 = 1 день). Дополнительно можно \
передать: location, description, link (URL), categories (список), status (CONFIRMED/TENTATIVE/CANCELLED), \
transp (OPAQUE — занят / TRANSPARENT — свободен), priority (1–9).
9а. Напоминания (alarms — минуты до начала): для создаваемых событий по умолчанию указывай \
alarms: [60, 15, 5] (за час, 15 и 5 минут), если пользователь не попросил другой набор \
интервалов или не попросил убрать напоминания (тогда alarms: []).
10. Удаление (op="delete"): ref — токен события/серии целиком из каталога get_period. Удаление \
всегда ЦЕЛОЕ: одиночное событие или вся серия. Одно вхождение серии — op="exclude" с ref серии и \
date вхождения.
10а. Обновление (op="update"): ref — токен события/серии целиком + changes — любые из полей \
summary, start, duration, all_day, location, description, link, alarms, categories, status, \
transp, priority, rrule (либо freq/interval/byday/until/count). Обновляется ТОЛЬКО целый объект \
(одиночное событие или вся серия, UID сохраняется); одно вхождение серии — только exclude + add. \
При переносе недельной серии на другой день недели правило повтора (BYDAY) обновится автоматически.
11. ask_user: question — текст вопроса, options — 1–4 варианта ответа кнопками; пользователь может \
и написать свой ответ. Уточняй ВСЕГДА, когда в запросе есть неопределённость (какое событие, какая \
дата, одно вхождение или вся серия, какой период). Как можно чаще предлагай варианты ответа \
(options), но не переспрашивай явное указание пользователя («Вы уверены?» и т.п.) — прямое указание \
уже является решением: сразу зарегистрируй план через reg_list и заверши ход done. Задавай по одному \
вопросу за раз: задал вопрос → получил ответ → продолжай (возможно, следующим вопросом).
12. Отвечай кратко по-русски, без markdown и эмодзи. Не упоминай в тексте пользователю токены ref \
и технические детали инструментов. В done клади суть в message, а перечисление (например, события с \
датой и временем) — отдельными короткими пунктами в items.
"""


class AgentError(Exception):
    """Ошибка работы агента."""


@dataclass
class AgentResult:
    kind: str = "done"  # "done" | "ask" | "error"
    text: str = ""
    items: list = field(default_factory=list)  # пункты списка под ответом done
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


def _trim_history(msgs: list[dict], limit: Optional[int] = None) -> None:
    """Обрезает историю спереди, не разрывая пару assistant(tool_calls) → tool.

    Срез [-limit:] мог оставить tool-ответ без родительского сообщения с
    tool_calls — провайдер отвечает 400 «tool must be a response to tool_calls».
    Поэтому первым сохранённым сообщением не может быть tool: отступаем за него.
    """
    if limit is None:
        limit = config.AGENT_HISTORY_LIMIT
    overflow = len(msgs) - limit
    if overflow <= 0:
        return
    cut = overflow
    while cut < len(msgs) and msgs[cut].get("role") == "tool":
        cut += 1
    del msgs[:cut]


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


def _norm_alarms(items) -> list:
    if not isinstance(items, list):
        raise ValueError("некорректный alarms: нужен список минут")
    out: list = []
    for item in items:
        try:
            m = int(item)
        except (TypeError, ValueError):
            raise ValueError("некорректный alarms: нужны целые минуты") from None
        if m > 0:
            out.append(m)
    return sorted(set(out))


def _norm_categories(items) -> list:
    if not isinstance(items, list):
        raise ValueError("некорректный categories: нужен список")
    return [str(c).strip() for c in items if str(c).strip()]


def _norm_priority(value) -> int:
    try:
        p = int(value)
    except (TypeError, ValueError):
        raise ValueError("некорректный priority") from None
    if not 1 <= p <= 9:
        raise ValueError("priority должен быть от 1 до 9")
    return p


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
        self._chat_locks: dict[int, threading.RLock] = {}
        self._sessions: dict[int, dict] = {}
        self._client = None

    def _chat_lock(self, chat_id: int) -> threading.RLock:
        with self._lock:
            return self._chat_locks.setdefault(chat_id, threading.RLock())

    # ---------- сессии ----------

    def _prune(self) -> None:
        now = time.time()
        ttl = config.AGENT_SESSION_TTL_MIN * 60
        with self._lock:
            for chat_id in list(self._sessions):
                if now - self._sessions[chat_id]["ts"] > ttl:
                    del self._sessions[chat_id]

    def _session(self, chat_id: int) -> dict:
        now = time.time()
        with self._lock:
            sess = self._sessions.get(chat_id)
            if sess is None or now - sess["ts"] > config.AGENT_SESSION_TTL_MIN * 60:
                sess = {"messages": [], "refs": {}, "plan": [], "pending_asks": [], "ts": now}
                self._sessions[chat_id] = sess
            sess["ts"] = now
            return sess

    def _append(self, chat_id: int, msg: dict) -> None:
        sess = self._session(chat_id)
        sess["messages"].append(msg)
        _trim_history(sess["messages"])

    def _append_tool_response(self, chat_id: int, tool_call_id: str, content: str) -> None:
        self._append(chat_id, {"role": "tool", "tool_call_id": tool_call_id, "content": content})

    def append_assistant_text(self, chat_id: int, text: str) -> None:
        with self._chat_lock(chat_id):
            self._append(chat_id, {"role": "assistant", "content": text})

    def has_pending_asks(self, chat_id: int) -> bool:
        with self._chat_lock(chat_id):
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
            logger.debug("AGENT → LLM messages:\n%s", json.dumps(messages, ensure_ascii=False, indent=2))
            request_kwargs: dict = {
                "model": config.OPENAI_MODEL,
                "messages": messages,
                "tools": TOOLS,
                "tool_choice": "auto",
                "temperature": 0,
            }
            if config.OPENAI_THINKING == "disabled":
                request_kwargs["extra_body"] = {
                    "reasoning": {"enabled": False},
                    "thinking": {"type": "disabled"},
                }
            response = self._client.chat.completions.create(**request_kwargs)
            if logger.isEnabledFor(logging.DEBUG):
                for choice in response.choices:
                    logger.debug(
                        "AGENT ← LLM: content=%r reasoning=%r tool_calls=%s",
                        choice.message.content,
                        getattr(choice.message, "reasoning_content", None),
                        [tc.function.name for tc in (choice.message.tool_calls or [])],
                    )
            return response
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
        with self._chat_lock(chat_id):
            sess = self._session(chat_id)
            found = False
            for q in sess["pending_asks"]:
                if q["ask_id"] == ask_id:
                    if q["answered"]:
                        return all(q["answered"] for q in sess["pending_asks"])
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
                self._session(chat_id)["plan"] = []
                return AgentResult(kind="error", text="Не удалось завершить ход — агент не вызвал done. Попробуйте ещё раз.")

            self._append(chat_id, self._to_assistant_msg(choice))
            logger.debug("AGENT chat=%s шаг=%d tool_calls=%d", chat_id, step + 1, len(tool_calls))
            ask_pending = None
            done_msg = None
            done_items: list[str] = []
            for tc in tool_calls:
                name = tc.function.name
                logger.debug("AGENT chat=%s шаг=%d tool=%s args=%s", chat_id, step + 1, name, tc.function.arguments)
                if name == "ask_user":
                    if ask_pending is None:
                        ask_pending = tc
                    else:
                        logger.warning("AGENT chat=%s шаг=%d игнорирую лишний ask_user", chat_id, step + 1)
                    continue
                if name == "done":
                    try:
                        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except json.JSONDecodeError:
                        args = {}
                    done_msg = (args.get("message") or "Готово.").strip()
                    done_items = [str(i).strip() for i in (args.get("items") or []) if str(i).strip()]
                    self._append_tool_response(chat_id, tc.id, "Принято.")
                    continue
                result = self._call_tool(chat_id, name, tc.function.arguments)
                logger.debug("AGENT chat=%s шаг=%d tool=%s result=%r", chat_id, step + 1, name, result[:500])
                self._append_tool_response(chat_id, tc.id, result)

            if ask_pending is not None:
                self._register_ask(chat_id, ask_pending.id, ask_pending.function.arguments)
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
                return AgentResult(kind="done", text=done_msg, items=done_items, plan=plan)

        logger.warning("AGENT chat=%s превышен лимит шагов %d", chat_id, config.AGENT_MAX_STEPS)
        self._session(chat_id)["plan"] = []
        return AgentResult(
            kind="error",
            text="Не удалось обработать запрос за отведённое число шагов.",
        )

    def run(self, chat_id: int, user_text: str) -> AgentResult:
        with self._chat_lock(chat_id):
            self._prune()
            sess = self._session(chat_id)
            unanswered = [q for q in sess["pending_asks"] if not q["answered"]]
            if unanswered:
                # Текст пользователя — ответ на первый неотвеченный вопрос.
                q = unanswered[0]
                consume_ask_op(q["ask_id"])
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
        with self._chat_lock(chat_id):
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
        if not args.get("date_from") or not args.get("date_to"):
            return "Ошибка: get_period требует date_from и date_to (YYYY-MM-DD)."
        start, end = _resolve_period(args)
        try:
            events = caldav_service.list_events(start, end)
        except caldav_service.CalDAVError as exc:
            return f"Ошибка CalDAV: {exc}"
        if not events:
            return "Ничего не найдено в выбранном периоде."
        catalog, refs = format_catalog_compact(
            events, start=start, end=end, oneoff_limit=config.AGENT_CATALOG_LIMIT
        )
        self._session(chat_id)["refs"] = refs
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
        if args.get("all_day") is not None:
            payload["all_day"] = bool(args["all_day"])
        if args.get("alarms") is not None:
            payload["alarms"] = _norm_alarms(args["alarms"])
        if args.get("categories") is not None:
            payload["categories"] = _norm_categories(args["categories"])
        for key, allowed in (("status", ("CONFIRMED", "TENTATIVE", "CANCELLED")), ("transp", ("OPAQUE", "TRANSPARENT"))):
            if args.get(key):
                val = str(args[key]).strip().upper()
                if val not in allowed:
                    raise ValueError(f"некорректный {key}: {val}")
                payload[key] = val
        if args.get("priority") is not None:
            payload["priority"] = _norm_priority(args["priority"])
        if args.get("link"):
            payload["link"] = str(args["link"]).strip()
        return PlanAction(kind="create", payload=payload)

    def _build_delete(self, chat_id: int, args: dict) -> PlanAction:
        ref = (args.get("ref") or "").strip().strip("[]")
        obj = self._session(chat_id)["refs"].get(ref)
        if obj is None:
            raise ValueError(f"неизвестный ref '{ref}'. Вызови get_period заново и используй свежие токены.")
        ev = obj[0] if isinstance(obj, list) else obj
        return PlanAction(kind="delete", event=ev)

    def _resolve_instance(self, instances: list, date_str: Optional[str]) -> EventData:
        """Найти вхождение серии по дате (YYYY-MM-DD)."""
        if not date_str:
            dates = ", ".join(i.start.date().isoformat() for i in instances)
            raise ValueError("для exclude серии нужен date (YYYY-MM-DD) с датой вхождения. Возможные даты: " + dates)
        try:
            target = _parse_dt(date_str).date()
        except ValueError:
            raise ValueError("некорректный date. Используй формат YYYY-MM-DD.") from None
        for inst in instances:
            if inst.start.astimezone(config.TZ).date() == target:
                return inst
        dates = ", ".join(i.start.date().isoformat() for i in instances)
        raise ValueError(f"в серии нет вхождения на {date_str}. Возможные даты в периоде: {dates}")

    def _build_exclude(self, chat_id: int, args: dict) -> PlanAction:
        ref = (args.get("ref") or "").strip().strip("[]")
        obj = self._session(chat_id)["refs"].get(ref)
        if obj is None:
            raise ValueError(f"неизвестный ref '{ref}'. Вызови get_period заново и используй свежие токены.")
        if not isinstance(obj, list):
            raise ValueError("exclude работает только для повторяющейся серии. Для одиночного события используй delete.")
        ev = self._resolve_instance(obj, args.get("date"))
        return PlanAction(kind="exclude", event=ev)

    @staticmethod
    def _norm_changes(raw) -> dict:
        """Валидация правок для op='update' (ключи совместимы с update_event)."""
        if not isinstance(raw, dict):
            raise ValueError("некорректный changes: нужен объект")
        changes: dict = {}
        if raw.get("summary") is not None:
            changes["summary"] = str(raw["summary"]).strip()
        if raw.get("start") is not None:
            start_iso = str(raw["start"]).strip()
            try:
                _parse_dt(start_iso)
            except ValueError:
                raise ValueError("некорректный start. Используй формат YYYY-MM-DDTHH:MM:SS") from None
            changes["start"] = start_iso
        if raw.get("duration") is not None:
            try:
                duration = int(raw["duration"])
            except (TypeError, ValueError):
                raise ValueError("некорректный duration (минуты)") from None
            if duration < 1:
                raise ValueError("duration должен быть ≥ 1")
            changes["duration"] = duration
        if raw.get("all_day") is not None:
            changes["all_day"] = bool(raw["all_day"])
        for key in ("location", "description", "link"):
            if raw.get(key) is not None:
                changes[key] = str(raw[key])
        if raw.get("alarms") is not None:
            changes["alarms"] = _norm_alarms(raw["alarms"])
        if raw.get("categories") is not None:
            changes["categories"] = _norm_categories(raw["categories"])
        for key, allowed in (("status", ("CONFIRMED", "TENTATIVE", "CANCELLED")), ("transp", ("OPAQUE", "TRANSPARENT"))):
            if raw.get(key) is not None:
                val = str(raw[key]).strip().upper()
                if val not in allowed:
                    raise ValueError(f"некорректный {key}: {val}")
                changes[key] = val
        if raw.get("priority") is not None:
            changes["priority"] = _norm_priority(raw["priority"])
        if raw.get("rrule"):
            rrule = _normalize_rrule(raw["rrule"])
            if rrule is None:
                raise ValueError(f"некорректный rrule: {raw['rrule']}")
            changes["rrule"] = rrule
        if raw.get("freq"):
            freq = str(raw["freq"]).strip().upper()
            if freq not in RRULE_FREQS:
                raise ValueError(f"некорректный freq: {freq}")
            changes["freq"] = freq
        if raw.get("interval") is not None:
            try:
                interval = int(raw["interval"])
            except (TypeError, ValueError):
                raise ValueError("некорректный interval") from None
            if interval < 1:
                raise ValueError("interval должен быть ≥ 1")
            changes["interval"] = interval
        if raw.get("byday") is not None:
            days = [str(d).strip().upper() for d in raw["byday"]]
            if not days or any(d not in WEEKDAY_CODES for d in days):
                raise ValueError("некорректный byday: нужны коды MO..SU")
            changes["byday"] = days
        if raw.get("until") is not None:
            until = str(raw["until"]).strip()
            if until:
                try:
                    datetime.fromisoformat(until)
                except ValueError:
                    raise ValueError("некорректный until: нужен YYYY-MM-DD или YYYY-MM-DDTHH:MM:SS") from None
            changes["until"] = until
        if raw.get("count") is not None:
            try:
                count = int(raw["count"])
            except (TypeError, ValueError):
                raise ValueError("некорректный count") from None
            if count < 1:
                raise ValueError("count должен быть ≥ 1")
            changes["count"] = count
        return changes

    def _build_update(self, chat_id: int, args: dict) -> PlanAction:
        ref = (args.get("ref") or "").strip().strip("[]")
        obj = self._session(chat_id)["refs"].get(ref)
        if obj is None:
            raise ValueError(f"неизвестный ref '{ref}'. Вызови get_period заново и используй свежие токены.")
        ev = obj[0] if isinstance(obj, list) else obj
        changes = self._norm_changes(args.get("changes"))
        if not changes:
            raise ValueError("для update нужен changes — хотя бы одно поле для изменения")
        return PlanAction(kind="update", event=ev, changes=changes)

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
                elif op == "exclude":
                    action = self._build_exclude(chat_id, act)
                elif op == "update":
                    action = self._build_update(chat_id, act)
                else:
                    lines.append(f"{i}. ❌ неизвестный op: {op!r}")
                    continue
            except ValueError as exc:
                lines.append(f"{i}. ❌ {exc}")
                continue
            key = _action_key(action)
            if any(_action_key(existing) == key for existing in plan):
                lines.append(f"{i}. ⏭ уже в плане: {_action_label(action)}")
                continue
            plan.append(action)
            lines.append(f"{i}. ✅ {_action_label(action)}")
        return "План зарегистрирован:\n" + "\n".join(lines) if lines else "План пуст."


def _action_key(action: PlanAction) -> tuple:
    """Ключ тождественности действия для дедупликации плана."""
    if action.kind == "create":
        p = action.payload
        return (
            "create",
            p["summary"],
            p["start"].astimezone(config.TZ).isoformat(),
            str(p["duration"]),
            p.get("rrule"),
            tuple(p.get("alarms") or []),
        )
    ev = action.event
    if action.kind == "exclude":
        inst = getattr(ev, "instance_start", None)
        inst_date = inst.astimezone(config.TZ).date().isoformat() if inst else None
        return ("exclude", ev.url, inst_date)
    if action.kind == "update":
        return ("update", ev.url, json.dumps(action.changes, sort_keys=True, ensure_ascii=False, default=str))
    return (action.kind, ev.url)


def _action_label(action: PlanAction) -> str:
    if action.kind == "create":
        return f"создать «{action.payload['summary']}»"
    ev = action.event
    if action.kind == "delete":
        target = "вся серия" if ev.is_recurring else "событие"
        return f"удалить {target} «{ev.summary}»"
    if action.kind == "exclude":
        inst = getattr(ev, "instance_start", None)
        when = inst.astimezone(config.TZ).strftime("%d.%m.%Y") if inst else ""
        return f"исключить вхождение «{ev.summary}» от {when}"
    if action.kind == "update":
        ch = action.changes
        label = f"изменить «{ch.get('summary') or ev.summary}»"
        if ch.get("start"):
            try:
                new_dt = _parse_dt(ch["start"]).astimezone(config.TZ)
                label += f" на {new_dt:%d.%m.%Y %H:%M}"
            except ValueError:
                pass
        return label
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
