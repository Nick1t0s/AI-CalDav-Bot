"""Чтение конфигурации из .env."""
from __future__ import annotations

import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _int_list(value: str) -> list[int]:
    return [int(x) for x in value.split(",") if x.strip()]


BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
CALDAV_URL: str = os.getenv("CALDAV_URL", "https://caldav.yandex.ru")
CALDAV_USERNAME: str = os.getenv("CALDAV_USERNAME", "")
CALDAV_PASSWORD: str = os.getenv("CALDAV_PASSWORD", "")
CALDAV_PRINCIPAL_PATH: str = os.getenv("CALDAV_PRINCIPAL_PATH", "")
CALENDAR_PATH: str = os.getenv("CALENDAR_PATH", "")
CALENDAR_ID: str = os.getenv("CALENDAR_ID", "")
ALLOWED_USER_IDS: list[int] = _int_list(os.getenv("ALLOWED_USER_IDS", ""))
TZ: ZoneInfo = ZoneInfo(os.getenv("TZ", "Europe/Moscow"))

OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "")
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
REQUESTS_TIMEOUT_SECONDS: int = int(os.getenv("REQUESTS_TIMEOUT_SECONDS", "60"))

# Окно поиска (дней вперёд) для запросов «когда <событие>» / «что у меня есть» без явной даты.
LIST_DEFAULT_DAYS: int = int(os.getenv("LIST_DEFAULT_DAYS", "90"))
