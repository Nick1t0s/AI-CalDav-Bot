# AI-CalDav-Bot

Telegram-бот с интеграцией CalDAV (Яндекс Календарь).

- «что у меня завтра» — показывает события на период;
- «отмени завтрашнее занятие» — удаляет событие (с подтверждением);
- «перенеси занятие завтра на 20:00» — изменяет событие (с превью и подтверждением);
- «создай встречу с Аней завтра в 14:00 на час» — создаёт событие (с подтверждением);
- для повторяющихся событий спрашивает: «удалить все или только одно».

**Ключевой принцип безопасности:** LLM — только «переводчик» фразы в JSON-интент.
Он не имеет доступа к календарю. Всю работу с CalDAV и все подтверждения делает скрипт
(деструктивные операции всегда двухшаговые, через inline-кнопки).

## Стек

- [aiogram 3](https://docs.aiogram.dev) — бот, async, inline-кнопки;
- [caldav](https://caldav.readthedocs.io) + [icalendar](https://icalendar.readthedocs.io) — протокол CalDAV;
- [recurring-ical-events](https://pypi.org/project/recurring-ical-events/) — раскрытие повторяющихся событий;
- [openai SDK](https://pypi.org/project/openai/) — парсинг фразы в структурированный интент
  (`base_url` настраивается: OpenAI, OpenRouter, локальный vLLM и т.д.);
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

### 3. LLM (парсинг фразы)

Заполните `OPENAI_API_KEY` и при необходимости `OPENAI_BASE_URL` / `OPENAI_MODEL`:

- OpenAI: `https://api.openai.com/v1`, модель `gpt-4o-mini`;
- OpenRouter: `https://openrouter.ai/api/v1`, модель `openai/gpt-4o-mini`;
- локальный vLLM: `http://localhost:8000/v1`.

### 4. Проверка доступа к Яндексу

```bash
python check_connection.py
```

Должно показать календарь и количество событий.

## Запуск

```bash
python main.py
```

## Как это работает

1. Пользователь пишет фразу.
2. `intent_parser` (LLM, только JSON, без tools) возвращает
   `{intent, date_from/to, query, summary, start, duration, changes...}`.
   В промпт подаётся текущая дата → «завтра» превращается в абсолютную ISO-дату.
3. Скрипт диспетчеризует интент и сам выполняет подтверждения через inline-кнопки:

   - **Чтение** — сразу ответ списком.
   - **Удаление** — поиск кандидатов → кнопки на каждый:
     `🗑 Только это вхождение` / `🗑 Все серии` / `❌ Отмена`.
     Для нерегулярных — `🗑 Удалить`. После клика — второй шаг:
     `⚠️ Точно удалить?` `✅ / ❌` (деструктив всегда двухшаговый).
   - **Изменение** — превью: текущее событие + предлагаемые правки → `✅ Применить / ❌ Отмена`.
   - **Создание** — превью события → `✅ Создать / ❌ Отмена`.

4. Кнопка → callback-хендлер → выполняется `caldav_service` → сообщение обновляется
   результатом, кнопки инвалидируются.

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
├── handlers.py           # обработчики сообщений + callback-кнопок
├── intent_parser.py      # LLM: фраза -> JSON-интент
├── caldav_service.py     # CRUD через caldav/icalendar
├── confirmation.py       # реестр «ожидающих операций» + инлайн-клавиатуры
├── formatting.py         # рендер событий на русском
└── check_connection.py   # CLI-проверка доступа к Яндексу
```
