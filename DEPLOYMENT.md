# Развёртывание Docker Compose

Запуск из корня проекта:

```powershell
docker compose up -d --build
docker compose ps -a
```

Для текущего рабочего каталога параметры уже добавлены в `.env`, пароль PostgreSQL сгенерирован. На новом компьютере создайте `.env` по `.env.example`, заполните `POSTGRES_PASSWORD` и существующие настройки, которые нужны приложению. Существующий `.env` сохраняйте при обновлении проекта.

## Сервисы

| Сервис | Назначение |
| --- | --- |
| `frontend` | Прототип HR на nginx; запросы `/api/v1` проксируются к FastAPI |
| `db` | PostgreSQL 17, данные в именованном volume `postgres_data` |
| `migrate` | Однократное применение `alembic upgrade head` после готовности БД |
| `api` | Текущий FastAPI backend, запуск после успешной миграции |
| `worker` | Обработка сохранённых в PostgreSQL задач сопоставления и генерации вопросов |

Состояние `migrate: Exited (0)` означает успешное завершение миграции. У `frontend`, `db` и `api` есть healthcheck. Frontend запускается после готовности API. API проверяется вместе с доступностью БД и совпадением применённых миграций с текущей версией образа. Порядок запуска основан на [условиях зависимостей Compose](https://docs.docker.com/compose/how-tos/startup-order).

Адреса при текущих значениях `.env`:

- Интерфейс HR: http://localhost:5173/
- Swagger: http://localhost:8000/docs
- OpenAPI: http://localhost:8000/openapi.json
- PostgreSQL с хоста: `127.0.0.1:5433`.

Порты меняются через `FRONTEND_PORT` (по умолчанию 5173), `API_PORT` и `POSTGRES_PORT`. Frontend обращается к API через свой адрес `/api/v1`, nginx использует внутренний `api:8000`. Внутри Compose PostgreSQL доступен приложению как `db:5432`; хостовый `DATABASE_URL` для контейнеров не используется. Entrypoint собирает внутренний URL из переменных и корректно кодирует пароль. Для обычного Python на хосте используется `DATABASE_URL` из `.env`.

Подробности frontend и отдельный запуск для разработки: [frontend/README.md](frontend/README.md).

В образ включены приложение, слой хранения, миграции и промпты. `.env`, Git, локальное окружение и исходные данные исключены из build context. Файл `.env` монтируется в API только для чтения; текущие настройки YandexGPT остаются доступными существующим модулям. Отклики монтируются из `parsed_responses`, вакансии — из `vacansies`. При запуске контейнеров генерация вопросов не вызывается. Восьмитабличная схема применяется, автоматического импорта откликов пока нет.

## Управление

```powershell
# Логи и проверка миграций
docker compose logs --tail 100 api migrate db
docker compose run --rm migrate python -m alembic current
docker compose run --rm migrate python -m alembic check

# Пересобрать и применить изменения
docker compose up -d --build

# Остановить или удалить контейнеры, сохранив данные БД
docker compose stop
docker compose down
```

Данные PostgreSQL переживают пересоздание контейнеров. Изменение пароля в `.env` после инициализации volume само по себе не меняет пароль роли PostgreSQL: его нужно согласованно изменить в БД и настройках. Удаление volume в обычный сценарий обновления не входит.

Команды через `compose run` сохраняют entrypoint и автоматически получают внутренний `DATABASE_URL`. Для команд через `exec` используйте обёртку: `docker compose exec api python -m docker.entrypoint python -m alembic current`. Это нужно потому, что URL вычисляется при запуске процесса, а `exec` получает исходные переменные контейнера.

Слой хранения и CRUD описаны в [DATABASE.md](DATABASE.md), HR-эндпоинты и импорт данных — в [HR_API.md](HR_API.md). API включает операции HR-платформы и существующие парсеры. Сопоставление считает долю навыков по контракту MVP; вопросы берутся из сохранённого пула вакансии.

После запуска подготовьте данные явно:

```powershell
docker compose exec -T api python -m docker.entrypoint python -m hr_api.import_data
```

Первый запуск создаёт один пул YandexGPT для каждой вакансии и сохраняет отчёты всех откликов. Повторный запуск использует актуальные пулы и отчёты. На старте API и при GET внешних вызовов нет. Для загрузки без подготовки используйте `--data-only`.
