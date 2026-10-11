# Вход через Telegram, восстановление пароля, почта и политика

Статус: код и тесты написаны, но pytest, jsdom и alembic в среде разработки не запускались. Живой Telegram и живой SMTP не проверялись.

## Вход через Telegram

Без сторонних виджетов, только ссылка на бота.

1. Браузер вызывает `POST /api/v1/auth/telegram/start` с `accept_policy`, `policy_version` и `confirm_age` (мне исполнилось 18 лет). Лимит по IP. Ответ: `login_id`, `deep_link` вида `https://t.me/beresta_ru_bot?start=login_<token>`, `expires_at` (5 минут).
2. В базе лежит только хеш токена и хеш секрета привязки. Сам секрет уходит в HttpOnly cookie `notes_tg_login`.
3. Человек открывает ссылку, бот получает `/start login_<token>` и только спрашивает «Да, это я» или «Нет, не я». Ссылку мог прислать посторонний, поэтому ядро бот не трогает. После «Да, это я» (callback `login:<token>`) бот вызывает `POST /internal/v1/telegram/login-confirm` через `process_update`. «Нет, не я» (`login-no`) ничего не вызывает. Формат токена проверяется строго (43 символа).
4. Браузер раз в 2 секунды спрашивает `GET /api/v1/auth/telegram/status/{login_id}`. Если cookie не совпадает, ответ 404.
5. Если Telegram уже привязан, создаётся сессия. Если нет, создаётся новый аккаунт: имя из Telegram или `tg<id>`, непригодный пароль, привязка сразу. Вход одноразовый, повтор не работает. В аналитике событие `login` с методом `telegram`.

## Восстановление пароля

- `POST /api/v1/auth/recovery/telegram {username}` всегда отвечает одинаково. Если аккаунт есть и привязан Telegram, в Outbox кладётся сообщение со ссылкой `{web_url}/#reset=<token>`. Срок 15 минут. Лимит по имени и по IP, плюс не больше 5 токенов в час на человека.
- Страница читает `#reset=`, сразу очищает адрес и вызывает `POST /api/v1/auth/password-reset {token, password}`. Все сессии пользователя закрываются.
- После доставки текст сообщения стирается из `outbox.message_text`. До доставки ссылка лежит там открытым текстом, не дольше 15 минут.
- Через почту работает `POST /api/v1/auth/recovery/email`, но пока только в коде (см. ниже).

## Почта

Отправка выключена. `DisabledMailer` отвечает 503 «Почта пока не подключена».

- Поля `users.email` (уникальное, в нижнем регистре), `users.email_verified_at`, таблица `email_verifications`.
- Эндпоинты `POST /api/v1/account/email` и `POST /api/v1/account/email/verify`. Адрес привязывается только после подтверждения.
- `app/mailer.py`: протокол `Mailer`, `DisabledMailer`, `SmtpBzMailer` (HTTPS API SMTP.BZ, `POST https://api.smtp.bz/v1/smtp/send`, ключ в заголовке `Authorization`) и `SmtpMailer` (STARTTLS, запасной вариант). Ответ сервиса и адрес получателя в ошибки и логи не попадают. Живой SMTP.BZ не проверялся, только подменённый HTTP-транспорт.
- Переменные: `NOTES_MAIL_ENABLED`, `NOTES_MAIL_TRANSPORT` (`smtp_bz` по умолчанию или `smtp`), `NOTES_SMTP_BZ_API_KEY_FILE`, `NOTES_SMTP_HOST`, `NOTES_SMTP_PORT`, `NOTES_SMTP_USER`, `NOTES_SMTP_PASSWORD_FILE`, `NOTES_MAIL_FROM`. В `compose.production.yaml` они проброшены, по умолчанию выключено.

## Политика и согласие

- Страница `/privacy`, файл `app/static/privacy.html`.
- При регистрации и входе через Telegram нужна галочка. Без неё 422.
- В `users` пишутся `policy_version` и `policy_accepted_at`.
- Старые пользователи видят один баннер и принимают политику через `POST /api/v1/account/accept-policy`. Ничего не блокируется.
- Ссылки есть в подвале лендинга и в меню аккаунта.

## Удаление аккаунта

В меню «Удалить аккаунт». Подтверждение паролем или через Telegram. Ставится `users.deletion_requested_at`, сессии закрываются. Запрос можно отменить через `POST /api/v1/account/delete-request/cancel` при следующем входе. Данные удаляет оператор вручную.

## Тестовые аккаунты

`users.is_test` уже есть. Админские метрики его исключают. Команду для пометки добавляет другой агент.

## Что нужно сделать владельцу

1. Заполнить оператора в `app/static/privacy.html` вместо «[Оператор: ФИО/ИП, контакт]».
2. Показать текст юристу. Заметки для него лежат в HTML-комментарии в начале файла.
3. При смене текста политики поднять версию в трёх местах: `app/policy.py`, `data-policy-version` в `index.html` и в `privacy.html`. Пользователи увидят баннер заново.
4. Чтобы включить почту: ключ SMTP.BZ уже лежит на ядре в `secrets/smtp_bz_api_key.txt`. Смонтировать его в контейнер api как secret в `compose.production.yaml`, указать путь в `NOTES_SMTP_BZ_API_KEY_FILE`, в `NOTES_MAIL_FROM` написать адрес отправителя без имени (домен должен быть подтверждён в SMTP.BZ) и поставить `NOTES_MAIL_ENABLED=true`. Первое настоящее письмо отправляет владелец сам.
5. Проверить `NOTES_TELEGRAM_BOT_USERNAME`, если бот называется иначе.

## Что не сделано

- Нет автоматического удаления данных после запроса.
- В интерфейсе нет привязки почты и ссылки `#verify-email=`. Работает только API.
- Живой Telegram и SMTP не проверялись.
- У аккаунта, созданного через Telegram, нет пароля. Пароль появится только после восстановления через бота.
- Миграция `b8d2f3e4a5c6` идёт после `a7c1e2d3f4b5` (админка).
- OpenAPI-контракт не пересобирался, `test_contracts` упадёт, пока его не обновят.
