# План: Telegram-бот AI-CalDav

## Цель
Телеграм-бот с интеграцией CalDAV (Яндекс Календарь) на агентной архитектуре:
- LLM — агент с инструментами (function calling), читает календарь и планирует действия;
- мутации (create/update/delete) накапливаются в общий план;
- план исполняет **скрипт** после подтверждения кнопкой (не ИИ).

## Стек
- **aiogram 3** — бот (async, inline-кнопки для подтверждений);
- **caldav 3.x** + **icalendar** — протокол CalDAV (Яндекс);
- **openai SDK** — агент с `tools` (`base_url` настраивается: OpenAI, OpenRouter, локальный vLLM);
- **python-dotenv** — конфигурация.

**Ключевой принцип безопасности:** LLM — только «планировщик». Все записи в календарь делает скрипт
после явного подтверждения кнопкой «✅ Выполнить всё / ❌ Отмена».

## Файлы
```
AI-CalDav-Bot/
├── .env.example / .env (gitignored)   # токены, календарь, TZ
├── requirements.txt
├── config.py            # чтение .env: токен, caldav creds, ALLOWED_USER_IDS, TZ, AGENT_*
├── main.py              # запуск aiogram
├── handlers.py          # обработчики сообщений + подтверждение плана кнопкой
├── agent.py             # агент: tools, чат-цикл, сессии
├── caldav_service.py    # CRUD через caldav/icalendar
├── confirmation.py      # PlanOp + клавиатура подтверждения
├── formatting.py        # каталог для модели (format_catalog) и план для пользователя (format_plan)
├── agent_demo.py        # CLI-проверка агента без Telegram
└── check_connection.py  # CLI-проверка доступа к Яндексу (до запуска бота)
```

## Настройка Яндекс (в README)
- App password: https://id.yandex.ru/security/app-passwords → тип «Календарь»;
- `CALDAV_URL=https://caldav.yandex.ru`, логин = полная почта, пароль = app password;
- опция `CALDAV_PRINCIPAL_PATH` на случай, если `client.principal()` не сработает.

## Поток обработки (безопасность)
1. Пользователь пишет фразу.
2. `agent.run(chat_id, text)` — чат-цикл с моделью (история на chat_id, TTL).
   В системный промпт подаются текущие дата/время/TZ.
3. Tools:
   - `list_events(date_from?, date_to?, query?)` — read-only, исполняется сразу;
     возвращает каталог с токенами `[eN]` (серии свёрнуты в ближайшее вхождение).
   - `propose_create / propose_delete / propose_update` — staged, кладутся в план.
4. Цикл идёт, пока модель не вернёт финальный текст (без tool_calls) либо не исчерпает
   `AGENT_MAX_STEPS`. Все накопленные действия = план.
5. План показывается списком + кнопка `✅ Выполнить всё / ❌ Отмена`.
6. Подтверждение → скрипт исполняет действия через `caldav_service` (EXDATE для вхождений и т.п.),
   результат дописывается в историю агента.

## Работа с повторяющимися событиями
- Обнаружение серии: наличие `RRULE` в VEVENT.
- «Удалить только одно» → добавить `EXDATE` в мастер-событие. «Удалить все» → `event.delete()`.
- «Изменить только одно» → `EXDATE` + новое отдельное VEVENT; «все» → правка мастера.

## Прочее
- Доступ только для `ALLOWED_USER_IDS` (иначе чужие смогут менять календарь).
- Часовой пояс в конфиге (по умолчанию `Europe/Moscow`).
- Провайдер LLM должен поддерживать function calling (tools).
- Модули названы не `caldav.py`/`calendar.py` (ломают импорты библиотеки).

## Порядок реализации
1. `.gitignore`, `requirements.txt`, `.env.example`, `config.py`
2. `caldav_service.py` + `check_connection.py`
3. `agent.py` (tools + цикл + сессии) и `formatting.py` (каталог/план)
4. `confirmation.py` (PlanOp + кнопка) и `handlers.py` + `main.py`
5. `agent_demo.py`, `README.md` (настройка Яндекс, запуск)
