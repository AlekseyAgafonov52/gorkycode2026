# PostgreSQL: модели и операции

В `storage/` реализованы восемь таблиц согласованной [SQL-схемы](sql/hr_platform_schema.sql). `models.py` содержит отношения, FK, ограничения и семь индексов; `schemas.py` — Pydantic-схемы ввода и чтения; `crud.py` — `Repository`; `database.py` — настройки и транзакции. Интеграции с моделями и запуск фоновых задач сюда не входят.

## Подключение

Для локального Python задайте `DATABASE_URL` в `.env` или окружении процесса. Формат: `postgresql+psycopg://<user>:<urlencoded-password>@<host>:<port>/<database>`. Настройки не имеют встроенного адреса или пароля. Переменные процесса имеют приоритет; `.env` читается буквально, без подстановок `${...}`. Реальный `.env` исключён из Git.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe check
```

При запуске [через Docker Compose](DEPLOYMENT.md) миграция применяется автоматически. Alembic создаёт служебную `alembic_version` дополнительно к восьми прикладным таблицам. Начальная миграция устанавливает `pgcrypto`; роль миграций должна иметь соответствующие права. Откат оставляет расширение, поскольку его могут использовать другие приложения.

Если исходный SQL уже применялся вручную, перед `alembic stamp 0001` проверьте совпадение структуры; повторное создание существующих таблиц не требуется. На импорте модулей подключение и таблицы не создаются.

## Транзакции

```python
from storage.crud import Repository
from storage.database import make_engine, make_session_factory, session_scope
from storage.schemas import CriterionCreate, ResponseCreate, VacancyCreate, VacancyRead

engine = make_engine()
factory = make_session_factory(engine)

with session_scope(factory) as session:
    repo = Repository(session)
    vacancy = repo.create_vacancy(VacancyCreate(
        title="Python developer",
        source_json=vacancy_json,
        criteria=[CriterionCreate(name="Python", importance="required")],
    ))
    response = repo.create_response(ResponseCreate(
        vacancy_id=vacancy.id,
        source_json=response_json,
    ))
    result = VacancyRead.model_validate(vacancy)

engine.dispose()
```

`vacancy_json` и `response_json` в примере — уже загруженные словари существующих парсеров. Полный исходный JSON сохраняется в JSONB без повторного парсинга и удаления полей. Сохраняются вложенные значения, Unicode, `null` и порядок массивов. NaN/Infinity отклоняются до SQL.

Сессия принадлежит одному запросу или одной фоновой операции. CRUD делает `flush()`, а `session_scope` фиксирует изменения при успехе и откатывает транзакцию при исключении. После ошибки SQL требуется rollback. Не передавайте одну сессию конкурентным операциям. Для обновления вложенного JSON передавайте новую запись через CRUD: изменения словаря на месте не отслеживаются.

Доступны операции:

| Данные | Методы `Repository` |
| --- | --- |
| Вакансии | `create_vacancy`, `get_vacancy`, `list_vacancies`, `update_vacancy`, `delete_vacancy` |
| Критерии | `create_criterion`, `get_criterion`, `list_criteria`, `update_criterion`, `update_criteria`, `delete_criterion` |
| Отклики | `create_response`, `get_response`, `list_responses`, `update_response`, `delete_response` |
| Задачи | `create_task`, `get_task`, `list_tasks`, `update_task`, `delete_task` |
| Результаты | `save_match_result`, `get_match_result`, `delete_match_result` |
| Отчёты | `save_report`, `get_report`, `delete_report`, `replace_questions` |
| Актуальность | `analysis_freshness` |

Частичные обновления принимают `VacancyUpdate`, `CriterionUpdate`, `ResponseUpdate`. Неуказанные поля сохраняются; nullable-поля можно очистить через `None`. Отсутствующий объект вызывает `NotFoundError`; ещё не созданный результат/отчёт существующего отклика возвращается как `None`. Списки поддерживают `limit` 1–1000 и `offset`; отклики фильтруются по статусу, архиву и цвету и сортируются по убыванию оценки, неизвестные оценки — в конце.

`save_match_result` и `save_report` атомарно заменяют текущую запись и дочерние оценки/вопросы, сохраняя идентификатор родителя. Один отклик имеет не более одного результата и отчёта. `ReportWrite.questions=[]` очищает предыдущие вопросы; для отдельной замены есть `replace_questions`. Критерии оценок и вопросов должны принадлежать вакансии отклика. Удаление критерия удаляет его оценки и оставляет вопросы с `criterion_id=null`.

## Версии критериев

Начальная версия — `1`, включая критерии из `VacancyCreate`. Добавление, удаление или изменение критерия повышает версию. Пакет `update_criteria(vacancy_id, {id: CriterionUpdate(...)})` повышает её один раз; запись тех же значений версию не меняет. Блокировка строки вакансии защищает от потери версии при конкурентных изменениях.

Анализатор передаёт фактически использованную версию в `MatchResultWrite.criteria_version` и `ReportWrite.criteria_version`. Отличие от текущей версии отклоняется с `StaleCriteriaError`: старый результат не перезапишет актуальный. `analysis_freshness(response_id)` возвращает версии и `match_is_stale` / `report_is_stale`; для отсутствующего результата признак равен `None`. Существующие устаревшие записи сохраняются.

Вопросы наследуют версию отчёта; у устаревшего отчёта сначала обновляется сам отчёт. Слой хранения не запускает перерасчёт, не вычисляет оценки и не меняет архив по цвету. Изменения исходного JSON, статусов или архива не увеличивают версию критериев. Прямой SQL/ORM в обход CRUD требует самостоятельного управления `criteria_version`; триггеров в согласованной схеме нет. `updated_at` обновляется ORM/CRUD.

## Тесты

Для интеграционных тестов задайте отдельный `TEST_DATABASE_URL` в окружении процесса. В указанной базе тесты создают и удаляют только свои случайные схемы `hr_storage_<uuid>`. Основной `DATABASE_URL` не используется как запасной вариант. Без `TEST_DATABASE_URL` интеграционные тесты явно пропускаются, unit-тесты выполняются.

```powershell
.\.venv\Scripts\python.exe -m unittest test_storage -v
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\pyright.exe
```

Проверяются миграции upgrade/downgrade, совпадение с исходным SQL, JSONB, FK, проверки, уникальность, каскады, upsert, rollback, версии и конкурентные изменения. Документация: [SQLAlchemy/PostgreSQL](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html), [Alembic](https://alembic.sqlalchemy.org/en/latest/autogenerate.html).
