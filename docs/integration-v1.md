# Integration v1 для ядра, бота и админки

Работа идёт в ветке `feature/integration-v1-core` от `90b4dd5`.
Методы с пометкой «контракт» ещё не работают в приложении.
Рабочая схема доступна в `/openapi.json`. Полная схема для разработки клиентов
лежит в `integration-v1.openapi.json`. Будущие методы в ней отмечены
`x-implementation-status: contract-only`.

## Кто что меняет

Марк отвечает за модели, схемы, миграции, основной веб, `main.py`, сервисы,
безопасность, провайдеры, AI worker, внутренние маршруты и расчёты аналитики.
Основатель отвечает за `telegram_adapter/`, `scheduler.py`, `audio_storage.py`,
`routes/audio.py`, `static/admin.*` и `deploy/`.

Бот не импортирует модели и не подключается к БД. Scheduler запускается
отдельным процессом с общим ядром и PostgreSQL.

Оба разработчика работают в своих ветках. Перед слиянием согласуем контракт
и обновляем обе стороны вместе с тестами. Миграции создаёт только Марк.
Авторизацию, миграции и production проверяет второй разработчик.

## Внутренний API

Все внутренние пути начинаются с `/internal/v1`. Для них нужен отдельный
сервисный секрет. Лучше задать `NOTES_INTERNAL_API_TOKEN_FILE`, либо использовать
`NOTES_INTERNAL_API_TOKEN`. Одновременно задавать оба нельзя.
Секрет должен быть случайным и содержать не меньше 32 символов.
Он не связан с ключом модели и не должен попадать в браузер, логи или Git.

Запрос передаёт `Authorization: Bearer <service-secret>`. Без настройки секрета
API возвращает 503, с неверным секретом возвращает 401.
Основатель закрывает `/internal/*` на публичном Caddy и настраивает межсерверную
сеть. Изменений `deploy/` в этой ветке нет. Bearer не заменяет закрытую сеть.

Telegram ID и bot ID имеют тип положительный int64 с максимумом 2^63-1.
Update ID допускает ноль. Идентификаторы сущностей ядра передаются строками UUID.
Поддерживаются только личные чаты, где chat_id равен telegram_user_id.
Новые контракты передают время в ISO 8601 UTC. Старый публичный API сохраняет
epoch seconds для совместимости. Клиент не передаёт user_id.
Ядро получает владельца из подтверждённой TelegramIdentity.

Пара `(bot_id, update_id)` общая для text, link, voice и actions.
Digest включает вид операции и все поля. Точный повтор получает сохранённый
ответ. Если данные изменились, сервер возвращает 409.
Inbox и изменения данных записываются одной транзакцией.
Ответ saved возвращается только после успешного commit.
При откате update ID остаётся свободным, запрос можно повторить.

Ошибка имеет форму `{"error":{"code":"…","message":"…"},"operation_id":"…"}`.

| HTTP | code |
|---|---|
| 401 | unauthorized |
| 403 | forbidden |
| 404 | not_found |
| 409 | conflict |
| 413 | payload_too_large |
| 422 | invalid_input |
| 429 | rate_limited |
| 503 | unavailable |

Сообщение ошибки не содержит исходный текст, ключ или содержимое исключения.
Operation ID нужен для диагностики и не заменяет bot/update ID.
При временной ошибке бот не сдвигает offset за несохранённое сообщение.
Неподдерживаемый update адаптер сам отмечает обработанным и не отправляет в ядро.

## Работающие методы

| Метод | Что делает |
|---|---|
| POST /api/v1/telegram/link-code | Требует Cookie и CSRF. Возвращает link_request_id, code, expires_at. Код случайный, 256 бит, действует 10 минут. В БД хранится SHA-256. Новый код отменяет старые запросы |
| POST /internal/v1/telegram/link-request | Принимает code, bot_id, update_id, telegram_user_id, chat_id. Возвращает link_request_id и status=pending. Код используется один раз, атомарно |
| GET /api/v1/telegram/links | Требует Cookie. Возвращает свои identities и неистёкшие pending с Telegram ID для проверки |
| POST /api/v1/telegram/links/{id}/confirm | Требует Cookie и CSRF. Подтверждает свой pending с проверкой срока. Не сливает аккаунты и не заменяет существующую связь. Повтор безопасен |
| POST /internal/v1/telegram/updates | Принимает bot_id, update_id, telegram_user_id, chat_id, text, processing_mode=ai/manual. Возвращает capture_id, job_id/null, status=saved, note_url. По умолчанию ai |
| POST /api/v1/captures/text | Старый AI-ответ сохранён. processing_mode=manual создаёт заметку и ревизию без Job. Ответ содержит capture_id, job_id=null, status=saved, note_id. Idempotency-Key учитывает режим |
| GET /api/v1/captures/{id} | Требует Cookie и проверяет владельца. Возвращает оригинал, режим, note_id и job/null, в том числе при ошибке обработки |
| GET /admin | Требует серверную роль admin. Отдаёт static/admin.html, когда файл появится. Пока возвращает 503. Без сессии 401, обычному пользователю 403. /static/admin.* тоже закрыты |

Текст ограничен 12 000 символами. Ручной режим сохраняет оригинал и Markdown
без изменений и создаёт первую ревизию. Он не вызывает провайдер и не создаёт Job.
Заполненная очередь не мешает ручному сохранению.

В вебе можно выбрать режим, создать код, проверить pending и подтвердить связь.
В Telegram основатель реализует `/start <code>`.

Ссылка `note_url = PUBLIC_ORIGIN + /?capture=<UUID>` работает до появления Note.
После входа веб находит Capture, открывает заметку или ждёт Job.
При ошибке обработки оригинал остаётся доступным. Сама ссылка не даёт
авторизацию и не открывает чужую запись.

## Следующие методы

Пути в таблице тоже начинаются с `/internal/v1`.
Все методы требуют сервисный Bearer. Они описаны в OpenAPI, реализация
и проверки гонок доставки ещё впереди.

| Метод | Запрос | Ответ |
|---|---|---|
| POST /telegram/voice | multipart с bot_id, update_id, telegram_user_id, chat_id, audio, processing_mode=ai | TelegramCaptureResponse. Сервер проверяет пределы 10 МБ и 180 секунд |
| POST /telegram/actions | bot_id, update_id, telegram_user_id, callback_token | status=completed/already_completed. Callback проверяется по владельцу |
| POST /deliveries/claim | bot_id, limit от 1 до 100 | items[]. Для пустой очереди items=[] |
| POST /deliveries/{delivery_id}/authorize | lease_token, generation | send=true/false. Разрешается одна актуальная попытка |
| POST /deliveries/{delivery_id}/result | lease_token, generation, status=sent/blocked/retryable/unknown, telegram_message_id/null, error_code/null, retry_after_seconds/null | status=recorded. Точный повтор безопасен, несовместимый результат даёт 409 |

Элемент claim содержит delivery_id, lease_token, generation, chat_id, text,
note_url и callback_token/null.

## Модели для scheduler и аудио

Миграция `651eb026c9bd` создаёт девять таблиц. Это TelegramIdentity,
LinkRequest, Item, Category, Reminder, Inbox, Outbox, ProductEvent и ProviderUsage.
У аккаунта появляются роль, источник и тестовый признак. У Capture добавляются
канал, режим и поля аудио. У Note появляются основная категория и отметка
подтверждения структуры. Старые даты регистрации остаются NULL, если неизвестны.
Миграция не создаёт за старых пользователей регистрацию или активность.

Reminder хранит user_id, note_id, item_id/null, scheduled_at, timezone, text,
status, generation и confirmed_at. В БД scheduled_at задан как UTC epoch,
timezone как зона IANA. Item.due_at хранит срок задачи отдельно от уведомления.
Reminder создаётся после подтверждения абсолютного времени и зоны.
Правка или отмена увеличивает generation. Выполнение задачи отменяет
её актуальные напоминания в той же транзакции.

Scheduler выбирает confirmed с scheduled_at <= now. Повторный проход не создаёт
второй Outbox для `(reminder_id, generation)`.
Владельцы Note, Item и Reminder должны совпадать. Получателя берём только
из действующей TelegramIdentity с включённой доставкой.
Если подтверждения или связи нет, отправлять некому.

Outbox хранит reminder_id, user_id, bot_id, chat_id и generation.
Его состояния pending, leased, authorized, sent, blocked, retryable, unknown,
cancelled и failed. Lease и callback хранятся в виде хешей.

Перед отправкой authorize атомарно проверяет generation, status и lease,
затем разрешает одну попытку. Если её результат неизвестен, ставим unknown
без автоматического повтора. Истёкший lease можно взять снова, если отправка
ещё не была авторизована. Отмена после authorize не гарантирует остановку
сетевой отправки. Эту гонку нужно отражать в статусе.
Отправку прямо из confirmed без authorize не используем.

Аудиозапись использует input_kind=audio, audio_key, audio_bytes, audio_seconds,
original_text='', transcript/null и transcript_version.
Audio_key не должен быть произвольным путём. До расшифровки original_text пуст.
Оригиналом остаётся аудио. Расшифровка может содержать ошибки.
STT и структурирование выполняются отдельно.
Основатель делает пути и проверку медиаданных, Марк делает AI worker
и правку transcript с проверкой версии. Маршруты аудио пока не готовы.

## Админка

Будущие методы GET `/api/admin/summary` и `/api/admin/export` используют фильтры
`from=YYYY-MM-DD&to=YYYY-MM-DD&channel=all|web|telegram&source=all`.
Даты включаются целиком в зоне Europe/Moscow. Эти методы ещё не реализованы.

Роль admin назначается на сервере командой
`python -m app.admin <user_id> --role admin`.
Для снятия роли используется `--role user`.

Summary содержит cards, daily[], funnel[], retention, usage[], quality
и generated_at. Поля и типы описаны в `app/contracts.py` и OpenAPI.
Пример лежит в `docs/fixtures/admin-summary.json`.
Доли содержат numerator, denominator и value. Value задан в процентах от 0 до 100,
при denominator=0 он равен null.
Деньги передаются десятичными строками в рублях. Если полная стоимость
неизвестна, она равна null. Известный подытог передаётся отдельно.
В usage только псевдонимы, без текстов, аудио, имён и контактов.

cards.dau показывает DAU последнего завершённого дня выбранного интервала.
Если интервал содержит только текущий день, показывается наблюдаемый DAU.
Поэтому generated_at обязателен.

Daily содержит строку для каждого дня, включая дни без активности.
unique_users считает активных людей за весь период.
new_users и returning_users определяются по первому входу.
AI/manual activation и retention учитывают только пользователей и когорты,
для которых уже прошло время проверки. Остальные показываются как pending.
Funnel строится по новым пользователям периода в порядке событий.
Возврат считается после активации. Один человек в разных каналах
остаётся одним пользователем.

Для usage действуют usage_limit=50 с диапазоном 1..100 и usage_offset=0.
Если есть следующая страница, ответ содержит заголовок X-Next-Usage-Offset.
Пагинация не меняет карточки и агрегаты. CSV содержит все агрегаты того же среза,
без raw usage и содержимого записей. Текстовые поля защищаются от формул.
Оба ответа используют no-store.

В DAU входят capture_saved, note_opened, note_edited, reminder_confirmed
и task_completed. Фоновая обработка и отправка, login, опрос статуса и просмотр
списка не входят. Note_opened требует отдельного действия пользователя.
GET при опросе статуса не должен создавать это событие.

Серверное событие содержит event_id, occurred_at UTC, user_id, channel, source,
app_version, is_test, name, operation_id и session_id.
Уникальность задаётся `(user_id, name, operation_id)`.
Пользовательских текстов в событиях нет.
Сессия объединяет каналы при перерыве не больше 30 минут. Фон её не продлевает.

Тестовые аккаунты исключаются также по текущему User.is_test.
Если аккаунт пометили тестовым позже, его прошлые события тоже исключаются.
Пока реализованы события регистрации, входа, привязки, сохранения, успеха
и ошибки обработки, ручной правки. Остальные события и агрегаты запланированы
на второй и третий дни.

ProviderUsage хранит input/output/cache tokens, STT seconds, measured cost,
отдельный estimated_cost, модель, тариф, latency, статус и request_id.
NULL означает неизвестное значение, а не ноль.
LLM cost/DAU считается как стоимость LLM, делённая на сумму дневных DAU
того же среза. STT учитывается отдельно. Тарифы и дневные бюджеты ещё не готовы.
Mock не подтверждает стоимость настоящих вызовов.
