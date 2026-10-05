# Как забрать ветку

Работа находится в `feature/integration-v1-core` и начата от `90b4dd5`.
В существующем клоне Disrupt выполните команды.

```powershell
git fetch origin
git switch --track origin/feature/integration-v1-core
```

Если локальная ветка с таким именем уже есть, сначала посмотрите её историю
и незакоммиченные изменения. Не используйте `--force`.
Обычная команда создания ветки в этом случае откажется её перезаписывать.

Ветки объединяем после проверки обеих частей и CI.
Модели, схемы и миграции берём из этой ветки.
`telegram_adapter/`, `scheduler.py`, `audio_storage.py`, `routes/audio.py`,
`static/admin.*` и `deploy/` остаются за основателем.
