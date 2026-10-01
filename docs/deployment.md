# Развёртывание для пользователей

В серверной схеме пользователь не знает ключ Cloud.ru. Браузер общается только с API приложения. API сохраняет исходный текст и задание в PostgreSQL. Отдельный worker читает задание, получает ключ из Docker Secret, обращается к Cloud.ru и сохраняет готовую заметку. Веб/API-контейнер не получает секрет модели.

```text
Пользователь → HTTPS / Caddy → API → PostgreSQL ← worker → Cloud.ru
                                 без ключа        с ключом
```

## Требования

- Linux-сервер с публичным IP, Docker Engine и Docker Compose;
- домен, A/AAAA-запись которого указывает на сервер;
- открытые входящие порты 80 и 443;
- API-ключ Cloud.ru Foundation Models;
- репозиторий `https://github.com/Vafailer/Disrupt`.

Текущий production compose рассчитан на один сервер для пилота. Перед масштабированием нужны централизованное хранилище файлов/аудио, резервное копирование и наблюдаемость.

## Подготовка сервера

```sh
git clone https://github.com/Vafailer/Disrupt.git
cd Disrupt
cp production.env.example .env.production
mkdir -p secrets
chmod 700 secrets
```

Отредактируйте `.env.production`: укажите домен, ID модели и лимиты. Затем создайте два секрета. Команды ниже не помещают значения в историю shell:

```sh
read -rsp "Cloud.ru API key: " CLOUD_KEY; printf '%s' "$CLOUD_KEY" > secrets/cloudru_api_key.txt; unset CLOUD_KEY; echo
read -rsp "PostgreSQL password: " DB_PASSWORD; printf '%s' "$DB_PASSWORD" > secrets/postgres_password.txt; unset DB_PASSWORD; echo
chmod 600 secrets/*.txt
```

Каталог `secrets/` и `.env.production` исключены из Git. Не передавайте эти файлы участникам команды и не копируйте их в issue, pull request или чат.

## Запуск

```sh
docker compose --env-file .env.production -f compose.production.yaml config
docker compose --env-file .env.production -f compose.production.yaml up -d --build
docker compose --env-file .env.production -f compose.production.yaml ps
```

Caddy автоматически получает TLS-сертификат для домена. API и PostgreSQL не публикуют порты наружу; единственная внешняя точка входа — HTTPS через Caddy.

После запуска откройте `https://ваш-домен/`, зарегистрируйте тестовые аккаунты и отправьте одну запись. Интерфейс покажет модель и общий остаток запросов. После создания аккаунтов пилота установите `NOTES_ALLOW_REGISTRATION=false` в `.env.production` и повторно выполните `up -d`.

## Проверка изоляции секрета

На сервере:

```sh
docker compose --env-file .env.production -f compose.production.yaml exec api sh -lc 'test ! -e /run/secrets/cloudru_api_key'
docker compose --env-file .env.production -f compose.production.yaml exec worker sh -lc 'test -r /run/secrets/cloudru_api_key'
```

Первая команда должна завершиться успешно: ключа нет в API-контейнере. Вторая подтверждает, что secret смонтирован worker. Не печатайте содержимое файла.

## Обновление

```sh
git pull --ff-only
docker compose --env-file .env.production -f compose.production.yaml up -d --build
```

Миграции выполняются отдельным одноразовым контейнером до старта новой версии API и worker.

## Резервное копирование

До тестов с реальными пользователями настройте регулярный `pg_dump` в каталог вне Docker volume и проверяемое восстановление. Резервная копия содержит пользовательские заметки и должна храниться с ограниченным доступом. Docker volume сам по себе не является резервной копией.

## Ограничения текущей версии

- Нет административной панели и восстановления пароля.
- Регистрация либо открыта для всех, либо закрыта целиком переменной окружения.
- Лимиты считаются числом зарезервированных обращений, а не токенами или рублями.
- Не настроены почта, мониторинг, централизованные логи и автоматические резервные копии.
- Голос и Telegram ещё не реализованы.
