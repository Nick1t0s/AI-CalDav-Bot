"""LLM-парсер фразы в JSON-интент (через OpenAI-совместимый API).

Ключевой принцип безопасности: LLM — только «переводчик». Никаких tools,
никакого доступа к календарю. Всю работу с CalDAV и подтверждения делает скрипт.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import config


class IntentParseError(Exception):
    """Не удалось распарсить фразу в интент."""


VALID_INTENTS = {"list", "create", "update", "delete", "none"}

SYSTEM_PROMPT = """Ты — «переводчик» фраз пользователя о календаре в JSON-интент. \
Ты НЕ имеешь доступа к календарю и НЕ выполняешь действий — только разбираешь фразу.

Верни СТРОГО ОДИН валидный JSON (без markdown-обёрток ```json, без комментариев) по схеме:
{
  "intent": "list | create | update | delete | none",
  "date_from": "YYYY-MM-DD или null",
  "date_to": "YYYY-MM-DD или null",
  "query": "строка для поиска события (по названию/месту/описанию) или null",
  "summary": "название нового события (для create) или null",
  "start": "YYYY-MM-DDTHH:MM:SS или null",
  "duration": "длительность в минутах или null",
  "location": "место или null",
  "description": "описание или null",
  "apply_to": "instance | all | null (для delete/update повторяющегося события)",
  "changes": {
    "summary": "новое название или null",
    "start": "новое время начала YYYY-MM-DDTHH:MM:SS или null",
    "shift_minutes": "сдвиг в минутах (положительный — позже, отрицательный — раньше) или null",
    "duration": "новая длительность в минутах или null",
    "location": "новое место или null"
  }
}

Правила:
1. «сегодня», «завтра», «послезавтра», «в понедельник», «на следующей неделе» превращай в \
абсолютные даты YYYY-MM-DD относительно сегодняшнего дня.
2. list: date_from = начало периода, date_to = конец. Для одного дня date_from = date_to.
   Если дата НЕ указана — date_from = date_to = null (бот сам найдёт события в ближайшем будущем).
3. delete/update: заполняй query (название/тема) и период date_from–date_to, где искать.
   Если день не указан — бери ближайший логичный день.
4. create: заполняй summary, start, duration (по умолчанию 60). Если время не указано — start = null.
5. update: правки клади в changes. «перенеси на 20:00» → changes.start = абсолютное время.
   «на час позже» → changes.shift_minutes = 60. «сделай 30 минут» → changes.duration = 30.
   «переименуй в X» → changes.summary.
6. delete/update повторяющегося события: «все повторения/серию» → apply_to = "all",
   «только это/одно» → "instance", иначе null.
7. Если пользователь спрашивает, когда состоится какое-то событие («когда вебинар...», \
«во сколько занятие...»), и не называет дату — intent = "list", date_from/date_to = null, \
заполни query названием/темой события.
8. Если фраза — мусор, оффтоп или не про календарь («флбфваапр», «привет», «расскажи анекдот») — \
intent = "none", все остальные поля null.
9. НЕ выдумывай события и факты. Неизвестные поля = null. Без лишних ключей в JSON.
"""


def _now() -> datetime:
    return datetime.now(config.TZ)


def build_system_prompt() -> str:
    now = _now()
    header = (
        f"Сегодня: {now:%Y-%m-%d} ({now:%d.%m.%Y}). Сейчас: {now:%H:%M}. "
        f"Часовой пояс: {now.tzinfo}.\n\n"
    )
    examples = """Примеры:
- «что у меня завтра» → {"intent":"list","date_from":"2026-08-08","date_to":"2026-08-08","query":null,...,"changes":{...}}
- «что на следующей неделе» → {"intent":"list","date_from":"2026-08-10","date_to":"2026-08-16",...}
- «что у меня есть» → {"intent":"list","date_from":null,"date_to":null,"query":null,...}
- «когда вебинар по поступлению в ВШЭ» → {"intent":"list","date_from":null,"date_to":null,"query":"вебинар по поступлению в ВШЭ",...}
- «отмени завтрашнее занятие» → {"intent":"delete","date_from":"2026-08-08","date_to":"2026-08-08","query":"занятие",...}
- «отмени все повторения английского» → {"intent":"delete",...,"query":"английск","apply_to":"all",...}
- «создай встречу с Аней завтра в 14:00 на час» → {"intent":"create","summary":"Встреча с Аней","start":"2026-08-08T14:00:00","duration":60,...}
- «перенеси занятие завтра на 20:00» → {"intent":"update",...,"query":"занятие","changes":{"start":"2026-08-08T20:00:00",...}}
- «сдвинь тренировку на час позже» → {"intent":"update",...,"changes":{"shift_minutes":60},...}
- «флбфваапр» → {"intent":"none",...}
- «расскажи анекдот» → {"intent":"none",...}
"""
    return header + SYSTEM_PROMPT + "\n" + examples


def _extract_json(content: str) -> dict:
    content = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL | re.IGNORECASE)
    if fence:
        content = fence.group(1).strip()
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise IntentParseError("LLM вернул ответ без JSON")
    return json.loads(content[start : end + 1])


def _extract_json_list(content: str) -> list:
    content = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL | re.IGNORECASE)
    if fence:
        content = fence.group(1).strip()
    start = content.find("[")
    end = content.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise IntentParseError("LLM вернул ответ без массива")
    return json.loads(content[start : end + 1])


def _clean(data: dict) -> dict:
    for key in ("date_from", "date_to", "query", "summary", "start", "location", "description"):
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
        data[key] = value or None
    intent = data.get("intent")
    if intent not in VALID_INTENTS:
        raise IntentParseError(f"неизвестный intent: {intent!r}")
    data["intent"] = intent
    apply_to = data.get("apply_to")
    if apply_to not in (None, "instance", "all"):
        data["apply_to"] = None
    data["duration"] = int(data["duration"]) if data.get("duration") else None
    changes = data.get("changes") or {}
    for key in ("summary", "start", "location"):
        if key in changes and isinstance(changes[key], str):
            changes[key] = changes[key].strip() or None
    if changes.get("shift_minutes"):
        changes["shift_minutes"] = int(changes["shift_minutes"])
    if changes.get("duration"):
        changes["duration"] = int(changes["duration"])
    data["changes"] = changes
    return data


def _ask_llm(system: str, user: str) -> str:
    """Один вызов LLM; возвращает сырой текст ответа."""
    if not config.OPENAI_API_KEY:
        raise IntentParseError("не настроен OPENAI_API_KEY")
    from openai import OpenAI

    client = OpenAI(
        api_key=config.OPENAI_API_KEY,
        base_url=config.OPENAI_BASE_URL or None,
        timeout=config.REQUESTS_TIMEOUT_SECONDS,
    )
    try:
        response = client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0,
        )
    except Exception as exc:
        raise IntentParseError(f"ошибка LLM: {exc}") from exc
    return response.choices[0].message.content or ""


def parse_intent(text: str) -> dict:
    if not text.strip():
        raise IntentParseError("пустой запрос")
    try:
        return _clean(_extract_json(_ask_llm(build_system_prompt(), text.strip())))
    except json.JSONDecodeError as exc:
        raise IntentParseError(f"LLM вернул некорректный JSON: {exc}") from exc


MATCH_SYSTEM_PROMPT = """Ты подбираешь события календаря под вопрос пользователя. \
Тебе дан пронумерованный список событий (каждая строка — «номер. описание»). \
Верни СТРОГО ОДИН валидный JSON — массив номеров строк, которые соответствуют \
вопросу. Если ничего не подходит — верни []. Учитывай смысл: перестановку слов, \
аббревиатуры, перефразирование. Не выдумывай номера вне списка и не добавляй \
пояснения и markdown-обёртки.
"""


def match_events(question: str, catalog: str) -> list[int]:
    """Номера (1-based) строк каталога, подходящих под вопрос пользователя."""
    if not question.strip():
        return []
    content = _ask_llm(
        MATCH_SYSTEM_PROMPT,
        f"Вопрос: {question.strip()}\n\nСписок событий:\n{catalog}",
    )
    try:
        numbers = _extract_json_list(content)
    except (json.JSONDecodeError, IntentParseError) as exc:
        raise IntentParseError(f"LLM вернул некорректный список номеров: {exc}") from exc
    if not isinstance(numbers, list):
        raise IntentParseError("LLM вернул не массив номеров")
    total = len([line for line in catalog.splitlines() if line.strip()])
    seen: set[int] = set()
    out: list[int] = []
    for n in numbers:
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= total and n not in seen:
            seen.add(n)
            out.append(n)
    return out
