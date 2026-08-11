"""CLI-проверка доступа к CalDAV (Яндекс) до запуска бота.

Запуск: python -m scripts.check_connection
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app import config
from app.caldav_service import CalDAVClient, CalDAVError


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    print("Проверка CalDAV...")
    print(f"  URL:      {config.CALDAV_URL}")
    print(f"  Логин:    {config.CALDAV_USERNAME}")
    print(f"  Пароль:   {'***' if config.CALDAV_PASSWORD else '(пусто)'}")
    if config.CALDAV_PRINCIPAL_PATH:
        print(f"  Principal path: {config.CALDAV_PRINCIPAL_PATH}")
    if config.CALENDAR_PATH:
        print(f"  Calendar path:  {config.CALENDAR_PATH}")
    print()

    try:
        client = CalDAVClient()
    except CalDAVError as exc:
        print(f"❌ Не удалось подключиться: {exc}")
        return

    print("✅ Подключение установлено")
    print(f"   Principal: {client.principal.url}")
    print(f"   Календарь: {client.calendar.name}")

    now = datetime.now(config.TZ)
    try:
        events = client.list_events(now - timedelta(days=1), now + timedelta(days=30))
        print(f"✅ Событий в ближайшие 30 дней: {len(events)}")
        for ev in events[:5]:
            when = ev.start.strftime("%d.%m %H:%M") if not ev.all_day else "весь день"
            print(f"   - {when}: {ev.summary}")
    except CalDAVError as exc:
        print(f"⚠️ Не удалось прочитать события: {exc}")


if __name__ == "__main__":
    main()
