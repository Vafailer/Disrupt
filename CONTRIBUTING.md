# Работа команды с репозиторием

Код проекта лежит в [Vafailer/Disrupt](https://github.com/Vafailer/Disrupt). Если репозиторий закрыт, попросите владельца добавить ваш GitHub-аккаунт. Для обычной работы достаточно доступа к веткам и pull request.

## Первый запуск

```powershell
git clone https://github.com/Vafailer/Disrupt.git
cd Disrupt
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start-demo.cmd
```

Откройте `http://127.0.0.1:8000`. Это локальная версия с `mock`, она не тратит запросы Cloud.ru. Работу с настоящей моделью владелец ключа проверяет отдельно через `start-cloudru.cmd`.

## Изменения

Сначала обновите `main` и создайте ветку для своей задачи.

```powershell
git switch main
git pull --ff-only
git switch -c feature/короткое-название
```

Перед отправкой изменений запустите проверки.

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests migrations
.\.venv\Scripts\python.exe -m pytest -q
$env:NOTES_PROVIDER='mock'
$env:NOTES_ALLOW_LIVE_REQUESTS='false'
$env:NOTES_DATABASE_URL='sqlite:///./data/notes.db'
.\.venv\Scripts\python.exe -m alembic check
```

Затем сохраните работу и отправьте ветку на GitHub.

```powershell
git add <изменённые-файлы>
git commit -m "feat: краткое описание"
git push -u origin feature/короткое-название
```

Откройте pull request в `main` и напишите, что изменилось для пользователя и как вы это проверили. Когда несколько человек работают над проектом, договоритесь об изменениях через pull request до слияния в `main`.

## Что проверить перед коммитом

- Посмотрите `git status` и `git diff --cached`. В коммит не должны попасть ключи, `.env`, база из `data/`, аудио и личные записи. Одного `.gitignore` для этого недостаточно.
- Тесты и CI должны работать без реального Cloud.ru. Ответы провайдера подменяются.
- Исходная мысль пользователя сохраняется до обработки. Предложенные выводы не смешиваются с её текстом.
- Заметки, задания и историю версий всегда выбирайте с проверкой владельца.
- Для изменения схемы базы создавайте новую миграцию Alembic. Опубликованную миграцию не меняйте.
- Если поменялись запуск, настройки или ограничения, поправьте README.
