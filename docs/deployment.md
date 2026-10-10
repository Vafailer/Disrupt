# Развёртывание для пользователей

Пользователю не нужен ключ Cloud.ru. Браузер отправляет текст в API приложения, тот сохраняет исходник и задание в PostgreSQL. Worker забирает задание, читает ключ из Docker Secret, обращается к модели и сохраняет заметку. В контейнере API секрета нет.

```text
Пользователь → HTTPS / Caddy → API → PostgreSQL ← worker → Cloud.ru
                                 без ключа        с ключом
```

## Что понадобится

- Linux-сервер с публичным IP, Docker Engine и Docker Compose;
- домен, A/AAAA-запись которого указывает на сервер;
- открытые входящие порты 80 и 443;
- API-ключ Cloud.ru Foundation Models;
- репозиторий `https://github.com/Vafailer/Disrupt`.

`compose.production.yaml` рассчитан на один сервер и небольшой пилот. Для большего числа пользователей понадобятся наблюдение за работой сервиса, резервные копии и отдельное хранилище файлов, когда появится аудио.

## Подготовка сервера

```sh
git clone https://github.com/Vafailer/Disrupt.git
cd Disrupt
cp production.env.example .env.production
mkdir -p secrets
chmod 700 secrets
```

В `.env.production` укажите домен, ID модели и лимиты. После этого создайте два файла с секретами. Следующие команды запросят значения скрытым вводом, чтобы они не попали в историю shell.

```sh
read -rsp "Cloud.ru API key: " CLOUD_KEY; printf '%s' "$CLOUD_KEY" > secrets/cloudru_api_key.txt; unset CLOUD_KEY; echo
read -rsp "PostgreSQL password: " DB_PASSWORD; printf '%s' "$DB_PASSWORD" > secrets/postgres_password.txt; unset DB_PASSWORD; echo
chmod 600 secrets/*.txt
```

Третий файл нужен для шифрования данных. Создайте его один раз и не перезаписывайте. Потеря этого ключа делает тексты и записи нечитаемыми, поэтому сразу сохраните копию вне сервера. Подробности в [encryption-v1.md](encryption-v1.md).

```sh
umask 077
openssl rand -base64 32 > secrets/data_key.txt
```

`secrets/` и `.env.production` исключены из Git. Не пересылайте эти файлы участникам команды и не вставляйте их содержимое в issue, pull request или чат.

## Запуск

В ветке развёртывания worker по умолчанию выключен. Обычный `up -d` запускает только ядро. Владелец отдельно задаёт `NOTES_ALLOW_LIVE_REQUESTS=true` и включает профиль `owner-live`. Подробный порядок для двух VPS находится в [server-deploy-v1.md](server-deploy-v1.md).

```sh
docker compose --env-file .env.production -f compose.production.yaml config
docker compose --env-file .env.production -f compose.production.yaml up -d --build
docker compose --env-file .env.production -f compose.production.yaml ps
```

Caddy получает TLS-сертификат для домена. Снаружи доступен только HTTPS через Caddy. Порты API и PostgreSQL наружу не открываются.

После запуска откройте `https://ваш-домен/`, создайте тестовые аккаунты и отправьте одну запись. Интерфейс покажет модель и остаток запросов. Когда все участники пилота зарегистрируются, установите `NOTES_ALLOW_REGISTRATION=false` в `.env.production` и повторите `up -d`.

## Проверка изоляции секрета

На сервере:

```sh
docker compose --env-file .env.production -f compose.production.yaml exec api sh -lc 'test ! -e /run/secrets/cloudru_api_key'
docker compose --env-file .env.production -f compose.production.yaml exec worker sh -lc 'test -r /run/secrets/cloudru_api_key'
```

Обе команды должны завершиться успешно. Первая проверяет отсутствие ключа в API, вторая проверяет доступ worker к secret-файлу. Не выводите содержимое файла на экран.

## Обновление

```sh
git pull --ff-only
docker compose --env-file .env.production -f compose.production.yaml up -d --build
```

Перед запуском новой версии API и worker Compose применяет миграции в отдельном одноразовом контейнере.

## Резервное копирование

До пилота настройте регулярный `pg_dump` за пределы Docker volume и один раз проверьте восстановление. В копии будут заметки пользователей, поэтому доступ к ней нужно ограничить. Сам Docker volume резервной копией не считается.

## Ошибки

Если обработка записи не удалась, в задании остаётся короткий код ошибки. Журнал сохраняет время, код и место сбоя без текста заметки и ключа. Посмотреть ошибки API и worker на сервере можно так:

```sh
docker compose --env-file .env.production -f compose.production.yaml logs api worker
```

У неожиданной ошибки API есть `error_id` в ответе. По нему ищите запись в журнале. Внутри контейнера также создаётся `data/errors.log`, но этот файл пропадёт при пересоздании контейнера. Для постоянного хранения настройте сбор Docker-логов на сервере.

## Ограничения текущей версии

- Нет административной панели и восстановления пароля.
- Регистрация открывается и закрывается целиком через переменную окружения.
- Лимиты считают зарезервированные обращения. Они не показывают расход токенов или денег.
- Почта, мониторинг, общие логи и автоматическое резервное копирование ещё не настроены.
- Голос и Telegram ещё не реализованы.
