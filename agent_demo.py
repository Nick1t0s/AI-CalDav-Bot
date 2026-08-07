"""CLI-харнес для проверки агента без Telegram.

Запуск: .venv/bin/python agent_demo.py
Каждая фраза — отдельная сессия (id = номер строки), чтобы не тащить контекст.
"""
from __future__ import annotations

import sys

from agent import AgentError, run_agent


def main() -> None:
    chat_id = 9000
    print("Агент CalDAV. Пустая строка — выход. Ctrl+C — завершить.")
    while True:
        try:
            phrase = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not phrase:
            return
        chat_id += 1
        try:
            result = run_agent(chat_id, phrase)
        except AgentError as exc:
            print(f"😕 Ошибка агента: {exc}")
            continue
        print("--- ответ агента ---")
        print(result.text)
        if result.plan:
            print(f"--- план ({len(result.plan)} действий) ---")
            for a in result.plan:
                print(f"  {a.kind} | scope={a.scope} | event={getattr(a.event, 'summary', None) if a.event else None} | payload={a.payload}")
        else:
            print("(план пуст)")


if __name__ == "__main__":
    sys.exit(main())
