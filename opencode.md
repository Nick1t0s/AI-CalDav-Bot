# opencode.md — карта проекта AI-CalDav-Bot

Справочник для внесения правок. Здесь — назначение каждого файла, точные
сигнатуры и точки вызова, контракты между слоями и «где править, если…».
Прочитал и сразу знаешь, что менять.

Документ привязан к реальному коду (имена, сигнатуры, номера строк актуальны
на момент написания). Для подробного «школьного» разбора есть `struct.md`.

---

## 1. Обзор и принципы

**Что это:** Telegram-бот для управления Яндекс-Календарём (CalDAV) на агентной
архитектуре. Пользователь пишет фразу естественным языком, LLM читает календарь
через инструменты и накапливает план изменений; план показывается человеку с
кнопкой «✅ Выполнить всё / ❌ Отмена»; исполняет план обычный код без ИИ.

**Стек:** aiogram 3 (Telegram, async), caldav + icalendar (протокол), 
recurring_ical_events (раскрытие RRULE), openai SDK (function calling,
`base_url` настраивается), python-dotenv (конфиг).

### Главный инвариант безопасности

> **LLM планирует, скрипт исполняет.**

- Нейросеть может ТОЛЬКО читать (`get_period`) и копить план (`reg_list`).
- Никакие мутации не происходят в момент планирования.
- Реальные вызовы `create_event` / `delete_event` / `exclude_occurrence` /
  `update_event` есть только в `app/handlers.py::_perform_plan` и вызываются
  строго после нажатия кнопки подтверждения.
- Нарушение этого инварианта = серьёзный баг безопасности.

### Поток сообщений (кратко)

```
Telegram (aiogram, event loop)
  text  ──► handlers.on_message
  voice ──► handlers.on_voice → stt.transcribe_audio (STT) → text
              │  asyncio.to_thread(run_agent, user_id, text)
              ▼
          agent._loop: LLM ⇄ tools (get_period/reg_list/ask_user/done)
              │  AgentResult(kind=done|ask|error)
              ▼
          handlers._handle_result
              kind=ask   → вопрос + reply-клавиатура (asks.kb_ask)
              kind=done  → PlanOp + inline-кнопки (confirmation.kb_plan_confirm)
              │  нажатие кнопки op:<id>:plan_confirm
              ▼
          handlers._perform_plan → caldav_service (мутации)
```

Кнопки плана приходят как `CallbackQuery` (`op:<op_id>:<action>`), ответы на
вопросы агента — как обычный текст (`on_message`), потому что вопрос задаётся
**reply-клавиатурой**.

---

## 2. Команды

```bash
.venv/bin/python -m app.main               # запуск бота (long polling)
.venv/bin/python -m scripts.check_connection  # проверить доступ к CalDAV
.venv/bin/python -m scripts.agent_demo        # агент без Telegram (CLI-песочница)
cp .env.example .env                          # конфигурация
docker compose up -d --build                  # запуск в Docker
```

`LOG_LEVEL=DEBUG` в `.env` — печатает весь диалог с моделью (JSON) и каждый шаг
цикла агента (`AGENT chat=... шаг=N ...`).

---

## 3. Карта файлов

```
├── .env.example          # шаблон конфигурации (см. app/config.py)
├── requirements.txt      # зависимости
├── Dockerfile            # python:3.12-slim; CMD python -m app.main
├── docker-compose.yml    # env_file: .env; restart unless-stopped
├── app/
│   ├── config.py         # константы из .env (импортируется везде)
│   ├── main.py           # точка входа: Bot + Dispatcher + polling
│   ├── handlers.py       # Telegram I/O: on_message, on_callback, исполнение плана
│   ├── agent.py          # мозг: 4 инструмента, чат-цикл, сессии, блокировки
│   ├── caldav_service.py # CRUD CalDAV: EventData, операции
│   ├── confirmation.py   # реестр планов + inline-кнопки подтверждения
│   ├── asks.py           # реестр вопросов + reply-клавиатуры вариантов
│   ├── stt.py            # распознавание голосовых (OpenAI Whisper)
│   └── formatting.py     # рендер: каталог для LLM и тексты для человека
├── scripts/
│   ├── agent_demo.py     # CLI-прогон агента без Telegram
│   └── check_connection.py
├── struct.md             # подробный разбор архитектуры (учебник)
└── opencode.md           # этот файл
```

### 3.1 `app/config.py`

Читает `.env` (через `load_dotenv()`) → константы модуля. Ничего не экспортирует
классом; остальные модули делают `from app import config`.

Ключевые константы (подробно в `struct.md` часть 3):

| Константа | .env | По умолчанию |
|---|---|---|
| `BOT_TOKEN` | `BOT_TOKEN` | `""` |
| `ALLOWED_USER_IDS: list[int]` | `ALLOWED_USER_IDS` (через запятую) | `[]` |
| `CALDAV_URL / USERNAME / PASSWORD` | `CALDAV_*` | yandex.ru / `""` |
| `CALDAV_PRINCIPAL_PATH`, `CALENDAR_PATH`, `CALENDAR_ID` | те же | `""` |
| `TZ: ZoneInfo` | `TZ` | `Europe/Moscow` |
| `LOG_LEVEL` | `LOG_LEVEL` | `INFO` |
| `OPENAI_API_KEY / BASE_URL / MODEL` | `OPENAI_*` | `""` / `""` / `gpt-4o-mini` |
| `OPENAI_THINKING` | `OPENAI_THINKING` | `"enabled"` (см. ниже) |
| `REQUESTS_TIMEOUT_SECONDS` | `REQUESTS_TIMEOUT_SECONDS` | `60` |
| `LIST_DEFAULT_DAYS` | `LIST_DEFAULT_DAYS` | `90` |
| `AGENT_MAX_STEPS / SESSION_TTL_MIN / HISTORY_LIMIT / CATALOG_LIMIT` | `AGENT_*` | `8` / `30` / `30` / `50` |

`OPENAI_THINKING == "disabled"` → в `agent._ask` добавляется
`extra_body={"reasoning": {"enabled": False}, "thinking": {"type": "disabled"}}`
(для DeepSeek-моделей).

STT-настройки (`app/stt.py`): `STT_API_KEY` (если пуст — берётся `OPENAI_API_KEY`),
`STT_BASE_URL` (по умолчанию `https://api.openai.com/v1` — отдельно от чат-провайдера,
т.к. OpenRouter/vLLM аудио не поддерживают), `STT_MODEL` (`whisper-1`),
`STT_LANGUAGE` (`ru`).

**Что править:** добавить настройку → добавить константу здесь и строку в
`.env.example`; проверить потребителей (главные — `agent.py`, `caldav_service.py`,
`main.py`).

### 3.2 `app/main.py`

Точка входа (`python -m app.main`). Функция `main()`:

1. Настройка логов; при `DEBUG` приглушаются `openai, httpcore, httpx, urllib3,
   asyncio` до `WARNING`.
2. Проверки: нет `BOT_TOKEN` → `SystemExit`; пуст `ALLOWED_USER_IDS` → warning.
3. `Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))` —
   HTML-разметка глобально.
4. `preflight()`: `asyncio.to_thread(get_client)` — логин в CalDAV до старта,
   ошибка не роняет бота (только лог).
5. `dp.include_router(router)` → `bot.delete_webhook(...)` →
   `dp.start_polling(bot)`; в `finally` закрывается сессия бота.

**Что править:** глобальные вещи aiogram (parse_mode, middleware, webhook) — здесь.

### 3.3 `app/handlers.py` — Telegram I/O

Модуль-«клей» между aiogram и агентом/сервисом.

- `INTRO` — приветственный текст (`/start`, `/help`).
- `_PENDING_PLANS: dict[int, str]` — `chat_id → op_id` «текущего неподтверждённого
  плана». Новое сообщение отменяет висящий план (`_cancel_pending_plan`).
- Обработчики (декораторы):
  - `@router.message(CommandStart())` → `cmd_start` (проверка доступа + INTRO);
  - `@router.message(Command("help"))` → `cmd_help` (то же);
  - `@router.message(F.text)` → `on_message(message)` — основной вход;
  - `@router.message(F.voice)` → `on_voice(message)` — голосовое → STT → агент;
  - `@router.callback_query(F.data.startswith("op:"))` → `on_callback(cb)` — кнопки плана.
- Помощники:
  - `_check_allowed(message) -> bool` — проверка `ALLOWED_USER_IDS`;
  - `_cancel_pending_plan(chat_id)` — `consume(op_id)` висящего плана;
  - `_safe_edit(message, text, reply_markup=None)` — `edit_text`, при падении → `answer`;
  - `_parse_dt(value) -> datetime` — ISO → datetime в `config.TZ`;
  - `_handle_result(message, result, user_id=None)` — единая точка показа результата
    (`ask` → `kb_ask`, `error` → текст, `done`+план → `kb_plan_confirm`);
  - `_perform_plan(op: PlanOp) -> list[str]` — исполнение плана (try/except на каждое
    действие);
  - `_perform_action(action: PlanAction) -> str` — диспетчер по `kind`;
  - `_run_create_action(payload)` / `_run_delete_action` / `_run_exclude_action` /
    `_run_update_action` — реальные мутации через `caldav_service`.

`on_message` (важно):
1. проверка доступа; `cleanup_expired()` (планы) и `cleanup_asks()` (вопросы);
2. пустой текст или `/...` → return;
3. `send_chat_action(TYPING)`;
4. `_cancel_pending_plan(chat_id)` — новый запрос отменяет старый план;
5. `result = await asyncio.to_thread(run_agent, message.from_user.id, text)`;
6. `_handle_result(...)`. Ключ сессии агента — `message.from_user.id`, не chat.id.

`on_callback` (важно):
- разбор `op:<op_id>:<action>`; `op = get(op_id)`; нет → alert «Операция устарела»;
- `cb.from_user.id != op.user_id` → alert «Это не ваша операция.»;
- `cancel` → `consume(op_id)` + `_safe_edit("❌ Отменено.")`;
- `plan_confirm` → `op = consume(op_id)` (одноразово) →
  `_perform_plan` в потоке → `append_assistant_text(user_id, text)` (результат в
  историю агента) → `_safe_edit(cb.message, text)`.

**Что править:** новые команды/обработчики сообщений и кнопок, реакцию на
подтверждение плана, текст результатов выполнения — здесь.

### 3.4 `app/agent.py` — мозг системы

Самый важный и самый сложный модуль (~958 строк). Класс `CalendarAgent` +
модульные обёртки внизу файла.

**Инструменты.** `build_tools() -> list[dict]` (строка ~40) — 4 схемы JSON Schema;
`TOOLS = build_tools()` на уровне модуля. `_call_tool(chat_id, name, arguments)`
диспетчеризует вызовы; `_tool_get_period` / `_tool_reg_list` реализуют логику.

**Системный промпт.** `SYSTEM_TEMPLATE` (строка ~201) — правила на русском
(нумерованные). `build_system_prompt()` подставляет текущие дату/время/TZ.
Любые изменения «поведения» агента = правка промпта (и, при необходимости,
схем инструментов).

**Сессии.** `_sessions: dict[int, dict]`; формат сессии:
```python
{
    "messages":     [...],  # история (роли без system; сквозной _trim_history)
    "refs":         {...},  # {"e1": EventData | list[EventData], ...}
    "plan":         [...],  # накопленные PlanAction
    "pending_asks": [...],  # {ask_id, tool_call_id, question, options, posted, answered}
    "ts":           float,  # последняя активность (TTL = AGENT_SESSION_TTL_MIN*60)
}
```
Блокировки: `_lock` (RLock, словари) + `_chat_lock(chat_id)` (RLock на каждый
чат — сериализация сообщений одного пользователя). `_prune()` удаляет сессии
по TTL.

**Цикл.** `run(chat_id, user_text)` → (если есть неотвеченные вопросы — текст
становится ответом на первый, иначе добавляется как user-сообщение) → `_loop()`.
`_loop()` крутит до `AGENT_MAX_STEPS` шагов: собирает `[system] + messages`,
вызывает `_ask(...)`, обрабатывает пачку `tool_calls`. Если в пачке был
`ask_user` — пауза (`kind="ask"`); если `done` — `kind="done"` с планом; если
пачка пустая или без терминала — `kind="error"`. `resume(chat_id)` продолжает
цикл после ответов на вопросы раунда. `answer_ask(...)` подставляет ответ как
результат инструмента; возвращает `True`, когда отвечены все вопросы раунда.

**Мутации НЕ выполняются в цикле** — `_tool_reg_list` только валидирует и
складывает `PlanAction` в `session["plan"]`.

**Валидация/построение действий** (все вызываются из `_tool_reg_list`):
- `_build_add(args) -> PlanAction(kind="create", payload=...)`;
- `_build_delete(chat_id, args) -> PlanAction(kind="delete", event=...)`;
- `_build_exclude(chat_id, args) -> PlanAction(kind="exclude", event=<вхождение>)`;
- `_build_update(chat_id, args) -> PlanAction(kind="update", event=..., changes=...)`;
- `_norm_changes(raw) -> dict` — валидация правок `update`;
- `_resolve_instance(instances, date_str)` — найти вхождение серии по дате;
- `_normalize_rrule`, `_norm_alarms`, `_norm_categories`, `_norm_priority` — валидаторы;
- `_action_key(action) -> tuple` — дедупликация плана;
- `_action_label(action) -> str` — читаемый лог-лейбл.

Пара вспомогательных: `_parse_dt`, `_day_start`, `_now`, `_resolve_period(args)`
(окно поиска), `_trim_history(msgs, limit)` (не разрывает пару assistant(tool_calls)→tool).

**Модульный фасад** (внизу): `_agent = CalendarAgent()`; `run_agent`,
`resume_agent`, `answer_ask`, `append_assistant_text`. Класс `AgentError(Exception)`;
dataclass `AgentResult(kind, text, items, plan, questions)`.

**Что править:** логика диалога, инструменты, промпт, сессии, лимиты — здесь.

### 3.5 `app/caldav_service.py` — CalDAV CRUD

Dataclass `EventData` (см. раздел 4.1). Класс `CalDAVClient` + модульный фасад
(синглтон через `get_client()` с double-checked locking).

Discovery: `_find_principal` (`CALDAV_PRINCIPAL_PATH` или `client.principal()`),
`_find_calendar` (`CALENDAR_PATH` → список → `CALENDAR_ID` по имени → первый
не-«Корзина»/trash).

Чтение: `list_events(start, end)` → для каждого мастера `_expand(master, start, end)`
раскрывает RRULE через `recurring_ical_events.of(cal).between(start, end)` →
список `EventData` (серия = несколько вхождений с общим `url`).

Мутации:
- `create_event(summary, start, duration, location, description, rrule, all_day,
  alarms, categories, status, transp, priority, link) -> EventData`;
- `delete_event(ev)` — удаление всего ресурса по `ev.url`;
- `exclude_occurrence(ev)` — добавить `EXDATE` в мастер (нужен `ev.instance_start`);
- `update_event(ev, changes)` — правка мастер-события (UID сохраняется);
- `update_instance(ev, changes)` — detached VEVENT с `RECURRENCE-ID`;
  **агентом не используется** (для одного вхождения агент делает exclude+add),
  оставлен для совместимости.

Вспомогательные (часто правимые при работе с датами):
`_ensure_aware`, `_parse_dt`, `_norm`, `_as_dt`, `_is_all_day`, `_replace_prop`,
`_get_alarms` / `_set_alarms` (VALARM), `_patch_rrule` (частичная правка RRULE:
freq/interval/byday/until/count), `_align_weekly_byday` (перенос недельной серии
на другой день недели → автообновление BYDAY), `_add_rdate`, `_restore_exdate`,
`_drop_prop`, `_set_start` (DTSTART/DTEND с учётом all_day/UTC), `_duration_of`.

Константы: `RRULE_FREQS`, `WEEKDAY_CODES`, `UTC`.

**Что править:** протокол, работа с icalendar, даты/TZ, RRULE/EXDATE/RDATE — здесь.

### 3.6 `app/confirmation.py` — реестр планов и кнопка

- `CALLBACK_PREFIX = "op:"`, `OP_TTL_SECONDS = 15*60`.
- Dataclass'ы: `BaseOp(user_id)`, `PlanAction(kind, event, payload, scope, changes)`,
  `PlanOp(BaseOp, actions)`.
- Реестр: `PENDING: dict[str, BaseOp]`, `_CREATED_AT: dict[str, float]`;
  `register(op) -> op_id` (uuid4().hex[:12]), `get`, `consume` (извлечь+удалить),
  `cleanup_expired()` (TTL 15 мин).
- `kb_plan_confirm(op_id)` — inline-кнопки «✅ Выполнить всё» (`plan_confirm`) /
  «❌ Отмена» (`cancel`). Callback-формат: `op:<op_id>:<action>`.

**Что править:** время жизни планов, кнопки/лейблы подтверждения, формат callback — здесь.

### 3.7 `app/asks.py` — реестр вопросов и reply-клавиатуры

- `ASK_TTL_SECONDS = 15*60`.
- Dataclass `AskQ(user_id, tool_call_id, question, options)`.
- Реестр: `register_ask`, `get_ask`, `consume_ask`, `cleanup_expired`.
- `kb_ask(options) -> ReplyKeyboardMarkup | None` — **reply-клавиатура**
  (кнопки по 2 в ряд, максимум 40 символов на кнопку, `one_time_keyboard=True`).
  Нажатие отправляет текст кнопки как обычное сообщение (путь через `on_message`).

**Что править:** варианты ответов, раскладку кнопок, текст подсказки — здесь.

### 3.7.1 `app/stt.py` — распознавание голосовых

- `STTError(Exception)`.
- `transcribe_audio(data: bytes, mime_type=None, filename=None) -> str` — синхронная
  функция (вызывается из `handlers.on_voice` через `asyncio.to_thread`): создаёт
  `OpenAI(base_url=config.STT_BASE_URL, api_key=STT_API_KEY or OPENAI_API_KEY)`,
  вызывает `audio.transcriptions.create(model=STT_MODEL, file=BytesIO, language=STT_LANGUAGE)`.
  Пустой результат → `STTError`. Конфиг — `config.STT_*`.

**Что править:** провайдер/модель распознавания, обработка ошибок — здесь. Потребитель:
`handlers.on_voice`.

### 3.8 `app/formatting.py` — рендер

Два «лица»: каталог для LLM (без HTML) и тексты для человека (HTML).

- Даты: `WEEKDAYS`, `MONTHS_GEN`, `_ORDINALS`, `_WDAY`; `fmt_date(d)`,
  `fmt_dtime(dt)` («сегодня»/«завтра»/дата), `_parse_byday`, `describe_rrule(rrule)`
  (RRULE → «каждый Пн», «раз в 2 недели», «до …», «5 раз»).
- `describe_event(ev)` — плоское описание события для каталога.
- `format_catalog_compact(events, start, end, oneoff_limit=None) -> (str, refs)` —
  серии одной строкой `[eN] ... · каждый Пн · кроме …`, одиночные по дням;
  `refs` = токены → EventData/список вхождений.
- Человеческие: `format_ask(question)`, `format_done(message, items)` (эмодзи,
  жирные заголовки), `format_plan(actions)`, `_plan_action_line(a)`.

**Что править:** любой текст для модели или пользователя, эмодзи, HTML — здесь.
⚠️ Любой пользовательский/событийный текст в HTML-сообщениях экранируется `esc()`
из `html` — при добавлении новых строк это обязательный шаг.

### 3.9 `scripts/`

- `agent_demo.py` — CLI-песочница: каждая фраза = отдельная сессия
  (chat_id инкрементится от 9000), отвечает на `ask_user` в цикле
  (`_resolve_asks`), печатает текст/items/план.
- `check_connection.py` — проверка CalDAV: создаёт `CalDAVClient()`, печатает
  principal/calendar, читает события за [сегодня-1, +30 дней].

---

## 4. Контракты между слоями (критично для правок)

### 4.1 `EventData` (caldav_service.py)

Поля: `url` (URL мастера), `uid`, `summary`, `location`, `description`,
`start`, `end`, `all_day`, `is_recurring`, `instance_start` (только для вхождения
серии), `rrule` (сырая строка), `series_count`, `series_first`, `series_last`,
`exdates` (локальные date), `rdates`, `alarms` (минуты до начала), `categories`,
`status`, `transp`, `priority`, `link`. Свойство `duration = end - start`.

**Инварианты:**
- Одна мастер-серия даёт МНОГО `EventData` (по вхождению) с одинаковыми `url`,
  `uid`, `rrule`, `is_recurring=True`, но разными `start`/`instance_start`.
- `instance_start` обязателен для `exclude_occurrence` (иначе `CalDAVError`).
- `all_day=True` → даты «без времени» (на wire date, в TZ); иначе — UTC на wire,
  отображение в `config.TZ`. Любая работа с датами должна это учитывать.
- `url` — это URL мастер-события; по нему перезагружают ресурс для правки/удаления.

### 4.2 `PlanAction` (confirmation.py)

`kind` ∈ `{"create", "delete", "exclude", "update"}`. Семантика полей:

| kind | Что хранит | Исполняется как |
|---|---|---|
| `create` | `payload: dict` (summary, start: datetime, duration: timedelta, location, description, rrule, all_day, alarms, categories, status, transp, priority, link) | `handlers._run_create_action` → `create_event(**payload)` |
| `delete` | `event: EventData` (весь объект: одиночное или вся серия) | `_run_delete_action` → `delete_event(ev)` |
| `exclude` | `event: EventData` вхождение серии (нужен `instance_start`) | `_run_exclude_action` → `exclude_occurrence(ev)` |
| `update` | `event: EventData` + `changes: dict` | `_run_update_action` → `update_event(ev, changes)` |

`changes` для update: `summary`, `start` (ISO-строка), `duration` (минуты),
`all_day`, `location`/`description`/`link` (пустая строка очищает), `alarms`,
`categories`, `status`, `transp`, `priority`, `rrule` (полное правило) либо
`freq`/`interval`/`byday`/`until`/`count` (частичные правки RRULE).

**Инвариант:** `update` и `delete` работают только с ЦЕЛЫМ объектом (UID
сохраняется). Одно вхождение серии — только через `exclude` + `add` (два
PlanAction).

### 4.3 `PlanOp` / `AskQ` / `AgentResult` / сессия

- `PlanOp(BaseOp)`: `user_id` + `actions: list[PlanAction]`. Хранится в
  `confirmation.PENDING`, живёт 15 мин, одноразовый (`consume`).
- `AskQ`: `user_id`, `tool_call_id`, `question`, `options`. Реестр в `asks.py`.
- `AgentResult`: `kind` (`done`|`ask`|`error`), `text`, `items`, `plan`,
  `questions` (список `{"ask_id", "question", "options"}`).
- Сессия агента: `{"messages", "refs", "plan", "pending_asks", "ts"}` (см. 3.4).
  `refs` — связь токенов `[eN]` из каталога с реальными объектами; `_build_*`
  берут из него объекты по `ref`.

### 4.4 Инструменты агента — три уровня

| Уровень | Функции | Файл |
|---|---|---|
| Объявление схемы | `build_tools()` (4 схемы) | agent.py |
| Исполнение | `_call_tool` → `_tool_get_period` / `_tool_reg_list` | agent.py |
| Валидация/построение действий | `_build_add/delete/exclude/update`, `_norm_changes`, `_resolve_instance` | agent.py |
| Вопросы | `_register_ask` | agent.py |
| Реальные мутации | `_perform_plan` (только после кнопки) | handlers.py |

При добавлении **нового действия** (напр. `op="restore"`) нужно затронуть:
`build_tools` (схема) + `SYSTEM_TEMPLATE` (правило) → `_tool_reg_list` +
новый `_build_*` + `_action_key`/`_action_label` → `PlanAction` (опционально
новое поле) → `handlers._perform_action` + `_run_*` → метод `caldav_service`.

---

## 5. «Где править, если…»

- **Изменить поведение агента/промпт** → `agent.py`: `SYSTEM_TEMPLATE`,
  `build_tools()` (схемы), `_tool_*`, лимиты из `config.py`.
- **Добавить новый инструмент** → `build_tools()` + `_call_tool` +
  (при надобности) правило в `SYSTEM_TEMPLATE`.
- **Добавить новый тип действия плана** → полный путь см. 4.4.
- **Добавить/убрать поле события (EventData)** → `EventData` в caldav_service.py;
  заполнение в `_to_event_data` / `create_event`; рендер в `formatting.describe_event`/
  `format_catalog_compact`; создание/правка в `create_event`/`update_event`.
- **Изменить работу с RRULE/EXDATE/RDATE/сериями** → caldav_service.py:
  `_patch_rrule`, `_align_weekly_byday`, `_add_rdate`, `_restore_exdate`,
  `exclude_occurrence`, `update_event`; промпт `agent.py` (правила 7–8).
- **Изменить подтверждение плана (кнопки, TTL, текст)** → confirmation.py
  (`kb_plan_confirm`, `OP_TTL_SECONDS`) + handlers.py `on_callback` +
  formatting.py `format_plan`/`_plan_action_line`.
- **Изменить вопросы агента (клавиатуру, варианты)** → asks.py (`kb_ask`,
  `ASK_TTL_SECONDS`) + formatting.py `format_ask` + agent.py (`_register_ask`,
  правило 11 в промпте).
- **Изменить тексты сообщений бота** → formatting.py (для агентных) и handlers.py
  (`INTRO`, `_run_*` строки результатов, сообщения ошибок).
- **Изменить распознавание голосовых / STT** → `stt.py` (`transcribe_audio`,
  `STTError`) + `config.STT_*` (ключ, base_url, модель, язык) + `.env.example`;
  входная точка — `handlers.on_voice`.
- **Добавить команду Telegram** → handlers.py: новый `@router.message(Command(...))`.
- **Добавить настройку** → config.py + `.env.example` + потребители.
- **Изменить каталог, который видит модель** → formatting.py
  `format_catalog_compact`/`describe_event` + лимит `config.AGENT_CATALOG_LIMIT`.
- **Добавить фильтр доступа / безопасность** → handlers.py `_check_allowed`,
  `_PENDING_PLANS`; confirmation/asks TTL.
- **Добавить тест** → проекту нужны юнит-тесты; кандидаты-чистые функции:
  `agent._resolve_period`, `_normalize_rrule`, `_build_*`, `_action_key`,
  `_norm_changes`; `formatting.describe_rrule`, `format_catalog_compact`,
  `_plan_action_line`.

---

## 6. Подводные камни и нюансы (важно при правках)

1. **`_trim_history`** не должен разрывать пару
   `assistant(tool_calls) → tool`. Если первый сохранённый элемент — роль `tool`,
   отступаем дальше (`agent._trim_history`). Иначе провайдер вернёт 400
   «tool must be a response to tool_calls».
2. **`ask_user` приоритетнее `done`** в одной пачке tool_calls: если в пачке и
   вопрос, и done — будет `kind="ask"`, done «потеряется», но план уже накоплен и
   переживёт паузу. Задаётся по одному вопросу за раз (лишние `ask_user`
   игнорируются с warning в лог).
3. **Reply-клавиатура ≠ inline.** Вопрос агента — reply-кнопки: ответ приходит
   как обычное сообщение в `on_message`, а не callback. Inline-кнопки — только
   подтверждение плана (`op:...`). Не путать при добавлении новых кнопок.
4. **`_PENDING_PLANS`**: новое сообщение пользователя отменяет висящий
   неподтверждённый план (`_cancel_pending_plan`). Это ожидаемое поведение.
5. **Дедупликация плана** — `agent._action_key`; одинаковые действия не
   добавляются повторно (ответ `N. ⏭ уже в плане: …`).
6. **Одноразовость** — `consume(op_id)` забирает операцию; повторное нажатие
   кнопки даст «Операция устарела». Callback проверяет владельца
   (`cb.from_user.id == op.user_id`).
7. **TZ vs UTC.** На wire (CalDAV) события по времени — UTC, all-day — чистые
   date. Внутри проекта нормализация — в `config.TZ`. Править даты аккуратно:
   используй `_ensure_aware`/`_norm`/`_as_dt`/`_is_all_day`.
8. **HTML-экранирование.** Все пользовательские/событийные строки в HTML-сообщениях
   обязаны проходить через `esc()` (html). Забыл — возможна «инъекция» в разметку.
9. **`scope` в PlanAction не используется** (оставлен для совместимости) — см.
   `confirmation.py` и `struct.md` 4.2.
10. **`update_instance` агентом не вызывается** — для одного вхождения серии
    агент формирует `exclude` + `add`. Если добавишь вызов `update_instance`,
    проверь, что агент действительно его использует (иначе мёртвый код).
11. **Ключ сессии агента = `message.from_user.id`**, а не `chat.id` (см.
    handlers.on_message и agent.run). Для приватных чатов совпадает, но не
    полагайся на это в коде.
12. **План пуст при `kind="done"`** → показывается просто текст; при `error`
    план очищается, история не рвётся.

---

## 7. Конвенции кода

- **Async + потоки:** aiogram-обработчики async; тяжёлые синхронные вызовы
  (`run_agent`, `_perform_plan`, `answer_ask`, `resume_agent`) — через
  `asyncio.to_thread(...)`. Не блокировать event loop.
- **Блокировки:** per-chat `RLock` в агенте (сериализация сообщений пользователя);
  `RLock` в `CalDAVClient` вокруг всех сетевых операций; `_client_lock` для
  синглтона `get_client()` (double-checked locking).
- **Стиль:** `from __future__ import annotations`; dataclass'ы; докстринги и
  сообщения на русском; имена функций в snake_case; пустые строки-разделители
  секций; строки без trailing whitespace. Комментарии в коде не добавлять, если
  не просили.
- **Логирование:** `logger = logging.getLogger(__name__)`; на `DEBUG` — весь
  диалог модели, шаги цикла, вызовы инструментов.
- **Обработка ошибок:** CalDAV → `CalDAVError`; агент → `AgentError`;
  `_perform_plan` оборачивает каждое действие в try/except (одна ошибка не
  роняет остальные действия).

---

## 8. Возможные расширения (куда смотреть)

- Персистентность (Redis/SQLite) для сессий/планов/вопросов — сейчас всё
  in-memory (`agent._sessions`, `confirmation.PENDING`, `asks.PENDING`).
- Очередь задач / параллельные агенты на пользователя.
- Юнит-тесты на чистые функции (см. список в разделе 5).
- Реальный webhook вместо long polling — менять `app/main.py`.
