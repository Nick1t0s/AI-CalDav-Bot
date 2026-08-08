"""CLI-харнес для проверки агента без Telegram.

Запуск: .venv/bin/python agent_demo.py
Каждая фраза — отдельная сессия (id = номер строки), чтобы не тащить контекст.
"""
from __future__ import annotations

import logging
import sys

import config
from agent import AgentError, answer_ask, resume_agent, run_agent


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if config.LOG_LEVEL == "DEBUG":
        for noisy in ("openai", "httpcore", "httpx", "urllib3", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    _setup_logging()
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
            result = _resolve_asks(chat_id, result)
        except AgentError as exc:
            print(f"😕 Ошибка агента: {exc}")
            continue
        if result.kind == "error":
            print(f"😕 {result.text}")
            continue
        print("--- ответ агента ---")
        print(result.text)
        if result.items:
            print("Детали:")
            for item in result.items:
                print(f"  • {item}")
        if result.plan:
            print(f"--- план ({len(result.plan)} действий) ---")
            for a in result.plan:
                print(f"  {a.kind} | scope={a.scope} | event={getattr(a.event, 'summary', None) if a.event else None} | payload={a.payload}")
        else:
            print("(план пуст)")


def _resolve_asks(chat_id: int, result) -> object:
    """Отвечаем на вопросы агента в цикле, пока не получим done/error."""
    while result.kind == "ask":
        for q in result.questions:
            print(f"❓ {q['question']}")
            for i, option in enumerate(q["options"], 1):
                print(f"  {i}. {option}")
            try:
                answer = input("Ответ (номер или текст): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                raise SystemExit(0)
            if answer.isdigit() and 1 <= int(answer) <= len(q["options"]):
                answer = q["options"][int(answer) - 1]
            answer_ask(chat_id, q["ask_id"], answer)
        result = resume_agent(chat_id)
    return result


if __name__ == "__main__":
    sys.exit(main())
