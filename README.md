# AI-CalDav-Bot

Telegram-бот с интеграцией CalDAV (Яндекс Календарь) на агентной архитектуре.

- «что у меня завтра» — показывает события на период;
- «удали ближайшее вычесывание бобров» — сам находит ближайшее вхождение и предлагает удалить;
- «перенеси занятие завтра на 20:00» — изменяет событие (с превью и подтверждением);
- «перенеси тренировку на 19:00 и создай встречу с Аней в 20:00» — несколько действий за одну команду;
- «создай встречу с Аней завтра в 14:00 на час» — создаёт событие (с подтверждением);
- для повторяющихся событий спрашивает: «удалить все или только одно».

**Ключевой принцип безопасности:** LLM — агент с инструментами (function calling). Он может
читать календарь и планировать изменения, но любые мутации (создание/изменение/удаление)
накапливаются в общий план и исполняются скриптом ТОЛЬКО после подтверждения кнопкой
«✅ Выполнить всё / ❌ Отмена». Нейросеть никогда не пишет в календарь напрямую.

## Стек

- [aiogram 3](https://docs.aiogram.dev) — бот, async, inline-кнопки;
- [caldav](https://caldav.readthedocs.io) + [icalendar](https://icalendar.readthedocs.io) — протокол CalDAV;
- [recurring-ical-events](https://pypi.org/project/recurring-ical-events/) — раскрытие повторяющихся событий;
- [openai SDK](https://pypi.org/project/openai/) — агент с tools (`base_url` настраивается:
  OpenAI, OpenRouter, локальный vLLM и т.д.; провайдер должен поддерживать function calling);
- [python-dotenv](https://pypi.org/project/python-dotenv/) — конфигурация.

## Установка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # затем отредактируйте .env
```

## Настройка

### 1. Telegram-бот

Создайте бота у [@BotFather](https://t.me/BotFather) и получите токен — в `BOT_TOKEN`.

Укажите ваш Telegram `user_id` в `ALLOWED_USER_IDS` (через запятую, если несколько).
Пустой список = доступ запрещён всем.

### 2. Яндекс Календарь (CalDAV)

1. Создайте App password типа «Календарь»:
   https://id.yandex.ru/security/app-passwords
2. В `.env`:
   - `CALDAV_URL=https://caldav.yandex.ru`
   - `CALDAV_USERNAME` — ваша полная почта `you@yandex.ru`;
   - `CALDAV_PASSWORD` — App password.
3. Опционально:
   - `CALDAV_PRINCIPAL_PATH` — если `client.principal()` не сработает;
   - `CALENDAR_PATH` — прямой URL календаря;
   - `CALENDAR_ID` — выбрать календарь по имени (иначе берётся первый не-«Корзина»).

### 3. LLM (агент)

Заполните `OPENAI_API_KEY` и при необходимости `OPENAI_BASE_URL` / `OPENAI_MODEL`:

- OpenAI: `https://api.openai.com/v1`, модель `gpt-4o-mini`;
- OpenRouter: `https://openrouter.ai/api/v1`, модель `openai/gpt-4o-mini`;
- локальный vLLM: `http://localhost:8000/v1` (должен поддерживать `tools`).

Опциональные параметры агента (в `.env`):
- `AGENT_MAX_STEPS` — максимум итераций цикла на сообщение (по умолчанию 8);
- `AGENT_SESSION_TTL_MIN` — время жизни сессии-диалога (по умолчанию 30 мин);
- `AGENT_HISTORY_LIMIT` — обрезка истории сообщений (по умолчанию 30);
- `AGENT_CATALOG_LIMIT` — лимит строк каталога, отдаваемого модели (по умолчанию 50).

### 4. Проверка доступа к Яндексу

```bash
python check_connection.py
```

Должно показать календарь и количество событий.

## Запуск

```bash
python main.py
```

Проверка логики агента без Telegram:

```bash
python agent_demo.py
```

## Как это работает

1. Пользователь пишет фразу.
2. `agent.run()` запускает чат-цикл с моделью (история на каждый chat_id, с TTL).
   Модель получает текущую дату/время/TZ и набор tools.
3. Инструменты:
   - `list_events(date_from?, date_to?, query?)` — **read-only**, выполняется сразу;
     возвращает нумерованный каталог с токенами `[eN]`;
   - `propose_create / propose_delete / propose_update` — **staged**: действие кладётся в план;
   - накопленные действия показываются одним списком с кнопкой `✅ Выполнить всё / ❌ Отмена`.
4. По кнопке скрипт исполняет план через `caldav_service` и отвечает результатом.
   Результат дописывается в историю агента, диалог продолжается.

## Повторяющиеся события

- Обнаружение серии: наличие `RRULE` в VEVENT.
- «Удалить только одно» → добавляется `EXDATE` в мастер-событие; «удалить все» → `event.delete()`.
- «Изменить только одно» → `EXDATE` + новое отдельное VEVENT; «все» → правка мастера.

## Структура

```
├── .env.example          # шаблон конфигурации
├── requirements.txt
├── config.py             # чтение .env
├── main.py               # запуск aiogram
├── agent.py              # агент: tools, чат-цикл, сессии
├── handlers.py           # обработчики сообщений + подтверждение плана кнопкой
├── caldav_service.py     # CRUD через caldav/icalendar
├── confirmation.py       # PlanOp + клавиатура подтверждения
├── formatting.py         # рендер каталога для модели и плана для пользователя
├── agent_demo.py         # CLI-проверка агента без Telegram
└── check_connection.py   # CLI-проверка доступа к Яндексу
```
