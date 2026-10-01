# Работа команды с репозиторием

Репозиторий: https://github.com/Vafailer/Disrupt

## Первый запуск

```powershell
git clone https://github.com/Vafailer/Disrupt.git
cd Disrupt
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start-demo.cmd
```

Локальное демо не использует Cloud.ru. Откройте `http://127.0.0.1:8000`. Для настоящей модели владелец ключа запускает `start-cloudru.cmd` и вводит ключ скрыто в своём терминале.

## Изменения

Перед началом работы обновите `main` и создайте отдельную ветку:

```powershell
git switch main
git pull --ff-only
git switch -c feature/короткое-название
```

После изменений выполните проверки:

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests migrations
.\.venv\Scripts\python.exe -m pytest -q
$env:NOTES_PROVIDER='mock'
$env:NOTES_ALLOW_LIVE_REQUESTS='false'
$env:NOTES_DATABASE_URL='sqlite:///./data/notes.db'
.\.venv\Scripts\python.exe -m alembic check
```

Затем сохраните и опубликуйте ветку:

```powershell
git add <изменённые-файлы>
git commit -m "feat: краткое описание"
git push -u origin feature/короткое-название
```

На GitHub откройте pull request в `main`. В описании укажите, какое поведение изменилось и как оно проверено. Не отправляйте изменения напрямую в `main`, когда над проектом работает несколько человек.

## Обязательные правила

- Никогда не добавляйте API-ключи, `.env`, локальные базы `data/`, аудио пользователей и другие личные данные. Они исключены через `.gitignore`, но перед каждым коммитом всё равно проверяйте `git status` и `git diff --cached`.
- Не используйте реальный Cloud.ru в автоматических тестах и CI. Все тесты провайдеров работают через подменённый HTTP-транспорт.
- Сохраняйте исходный ввод пользователя до обработки. Выводы модели должны оставаться отдельно от слов пользователя.
- Все запросы к заметкам, заданиям и версиям фильтруйте по владельцу.
- Изменение структуры базы оформляйте новой миграцией Alembic; не редактируйте уже опубликованную миграцию.
- Обновляйте README, если меняются команды запуска, настройки или известные ограничения.

## Доступ к GitHub

Если репозиторий закрытый, владелец должен добавить участников по их GitHub-именам в настройках репозитория. Достаточно роли с правом создавать ветки и pull request; права администратора для обычной разработки не нужны.
