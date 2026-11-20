"""REST orchestration. Reads use stored results; preparation runs in the worker."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker

from contract_models import ContractReport, ContractResponse, ContractVacancy, ViewedResult, WorkflowResult
from question_pool import PoolBuilder
from storage.crud import NotFoundError, Repository
from storage.models import AnalysisTask, MatchResult, Response, Vacancy
from storage.schemas import CriterionRead, CriterionUpdate, ResponseUpdate, TaskCreate
from .preparation import ReportPreparer
from .schemas import CriteriaPatch, CriteriaResult, ResponseDetail, TaskView, VacancyDetail


class ReportNotReadyError(RuntimeError):
    pass


class HRService:
    def __init__(self, factory: sessionmaker[Session], pool_builder: PoolBuilder | None = None):
        self.factory = factory
        self.preparer = ReportPreparer(factory, pool_builder)

    @staticmethod
    def _task_view(task: AnalysisTask, *, reused: bool = False) -> TaskView:
        return TaskView.model_validate({
            "task_id": task.id, "task_type": task.task_type, "vacancy_id": task.vacancy_id,
            "response_id": task.response_id, "status": task.status, "error_message": task.error_message,
            "created_at": task.created_at, "completed_at": task.completed_at, "reused": reused,
            "is_stub": False, "message": None})

    @staticmethod
    def _latest(session: Session, vacancy_id: UUID, kind: str, response_id: UUID | None = None):
        query = select(AnalysisTask).where(AnalysisTask.vacancy_id == vacancy_id, AnalysisTask.task_type == kind)
        query = query.where(AnalysisTask.response_id == response_id)
        return session.scalar(query.order_by(AnalysisTask.created_at.desc(), AnalysisTask.id.desc()).limit(1))

    def _enqueue(self, repo: Repository, vacancy: Vacancy, *, response: Response | None = None,
                 force: bool = False) -> TaskView:
        kind = "questions" if response is not None else "matching"
        latest = self._latest(repo.session, vacancy.id, kind, response.id if response else None)
        if latest is not None and latest.status in ("pending", "processing"):
            return self._task_view(latest, reused=True)
        if not force and latest is not None and latest.status == "completed" and latest.created_at >= vacancy.updated_at:
            return self._task_view(latest, reused=True)
        task = repo.create_task(TaskCreate.model_validate({"task_type": kind, "vacancy_id": vacancy.id,
                                                          "response_id": response.id if response else None}))
        return self._task_view(task)

    def list_vacancies(self) -> list[ContractVacancy]:
        with self.factory.begin() as session:
            query = (select(Vacancy, func.count(Response.id)).outerjoin(Response)
                     .group_by(Vacancy.id).order_by(Vacancy.created_at, Vacancy.id))
            return [ContractVacancy(id=str(row.id), title=row.title, responses_count=count)
                    for row, count in session.execute(query)]

    def vacancy(self, identifier: UUID) -> VacancyDetail:
        with self.factory.begin() as session:
            row = Repository(session).get_vacancy(identifier)
            count = session.scalar(select(func.count()).select_from(Response).where(Response.vacancy_id == identifier))
            return VacancyDetail.model_validate({"id": row.id, "title": row.title, "status": row.status,
                "responses_count": count, "source_json": row.source_json, "criteria_version": row.criteria_version,
                "criteria": [CriterionRead.model_validate(c) for c in row.criteria]})

    def update_criteria(self, identifier: UUID, data: CriteriaPatch) -> CriteriaResult:
        with self.factory.begin() as session:
            repo = Repository(session)
            vacancy = repo._lock_vacancy(identifier)
            old_version = vacancy.criteria_version
            criteria = {criterion.id: criterion for criterion in repo.list_criteria(identifier)}
            for item in data.criteria:
                if item.id not in criteria or item.name is not None and item.name != criteria[item.id].name:
                    raise ValueError("Критерий не принадлежит вакансии или его имя изменено")
            repo.update_criteria(identifier, {item.id: CriterionUpdate(importance=item.importance) for item in data.criteria})
            changed = vacancy.criteria_version != old_version
            task_id = None
            if changed:
                targets = select(Response.id).where(Response.vacancy_id == identifier)
                session.execute(update(AnalysisTask).where(
                    (AnalysisTask.vacancy_id == identifier) | AnalysisTask.response_id.in_(targets),
                    AnalysisTask.status.in_(("pending", "processing"))).values(
                        status="failed", error_message="Критерии изменились; задача заменена новой",
                        completed_at=datetime.now(timezone.utc)))
                task_id = self._enqueue(repo, vacancy, force=True).task_id
            return CriteriaResult(vacancy_id=identifier, criteria_version=vacancy.criteria_version,
                criteria=[CriterionRead.model_validate(c) for c in repo.list_criteria(identifier)], changed=changed, task_id=task_id)

    @staticmethod
    def _report(response: Response) -> ContractReport:
        report = response.report
        if report is None or report.contract_json is None:
            raise ReportNotReadyError("Отчёт ещё не подготовлен; запустите импорт или обработку вакансии")
        if report.criteria_version != response.vacancy.criteria_version:
            raise ReportNotReadyError("Критерии обновлены; дождитесь подготовки актуального отчёта")
        source = response.source_json.get("source_text")
        return ContractReport.model_validate({**report.contract_json,
            "workflow_status": response.workflow_status, "is_viewed": response.is_viewed,
            "source_text": source if isinstance(source, str) else ""})

    @classmethod
    def _summary(cls, response: Response) -> ContractResponse:
        report = cls._report(response)
        return ContractResponse.model_validate(report.model_dump(include=set(ContractResponse.model_fields)))

    def list_responses(self, vacancy_id: UUID, *, match_status: str | None = None,
                       workflow_status: str | None = None, is_archived: bool | None = None,
                       is_viewed: bool | None = None,
                       limit: int = 1000, offset: int = 0) -> list[ContractResponse]:
        with self.factory.begin() as session:
            Repository(session).get_vacancy(vacancy_id)
            query = (select(Response).outerjoin(MatchResult).where(Response.vacancy_id == vacancy_id)
                     .options(selectinload(Response.report)))
            if is_archived is not None:
                query = query.where(Response.is_archived == is_archived)
            if workflow_status is not None:
                query = query.where(Response.workflow_status == workflow_status)
            if is_viewed is not None:
                query = query.where(Response.is_viewed == is_viewed)
            if match_status is not None:
                query = query.where(MatchResult.match_status == match_status)
            priority = case((MatchResult.match_status == "green", 0), (MatchResult.match_status == "yellow", 1), else_=2)
            rows = session.scalars(query.order_by(priority, MatchResult.match_score.desc().nullslast(),
                                  Response.created_at, Response.id).limit(limit).offset(offset))
            return [self._summary(row) for row in rows]

    def report(self, identifier: UUID) -> ContractReport:
        with self.factory.begin() as session:
            return self._report(Repository(session).get_response(identifier))

    def response(self, identifier: UUID) -> ResponseDetail:
        with self.factory.begin() as session:
            row = Repository(session).get_response(identifier)
            summary = self._summary(row)
            return ResponseDetail.model_validate({**summary.model_dump(), "vacancy_id": row.vacancy_id,
                "source_json": row.source_json, "structured_resume": row.report.structured_resume if row.report else {},
                "criteria_version": row.vacancy.criteria_version,
                "is_stale": row.report is not None and row.report.criteria_version != row.vacancy.criteria_version,
                "is_archived": row.is_archived})

    def update_response(self, identifier: UUID, data: ResponseUpdate) -> ContractResponse:
        with self.factory.begin() as session:
            row = Repository(session).update_response(identifier, data)
            return self._summary(row)

    def update_status(self, identifier: UUID, status: str) -> WorkflowResult:
        with self.factory.begin() as session:
            row = Repository(session).update_response(identifier, ResponseUpdate.model_validate({"workflow_status": status}))
            return WorkflowResult.model_validate({"id": str(row.id), "workflow_status": row.workflow_status})

    def update_viewed(self, identifier: UUID, is_viewed: bool) -> ViewedResult:
        with self.factory.begin() as session:
            row = Repository(session).update_response(identifier, ResponseUpdate(is_viewed=is_viewed))
            return ViewedResult(id=str(row.id), is_viewed=row.is_viewed)

    def request_match(self, identifier: UUID) -> TaskView:
        with self.factory.begin() as session:
            repo = Repository(session)
            return self._enqueue(repo, repo._lock_vacancy(identifier), force=True)

    def request_questions(self, identifier: UUID, *, force: bool = False) -> TaskView:
        with self.factory.begin() as session:
            repo = Repository(session)
            response, vacancy = repo._lock_response_vacancy(identifier)
            return self._enqueue(repo, vacancy, response=response, force=force)

    def task(self, identifier: UUID) -> TaskView:
        with self.factory.begin() as session:
            return self._task_view(Repository(session).get_task(identifier))

    def next_task_id(self) -> UUID | None:
        with self.factory.begin() as session:
            return session.scalar(select(AnalysisTask.id).where(AnalysisTask.status == "pending")
                                  .order_by(AnalysisTask.created_at, AnalysisTask.id).limit(1))

    def recover_processing_tasks(self) -> None:
        with self.factory.begin() as session:
            session.execute(update(AnalysisTask).where(AnalysisTask.status == "processing")
                            .values(status="pending", error_message=None, completed_at=None))

    def run_task(self, identifier: UUID) -> None:
        try:
            with self.factory.begin() as session:
                task = session.scalar(select(AnalysisTask).where(AnalysisTask.id == identifier,
                    AnalysisTask.status == "pending").with_for_update(skip_locked=True))
                if task is None:
                    return
                if task.vacancy_id is None:
                    if task.response_id is None:
                        raise NotFoundError("Цель задачи не найдена")
                    vacancy_id = Repository(session).get_response(task.response_id).vacancy_id
                else:
                    vacancy_id = task.vacancy_id
                response_id = task.response_id
                Repository(session).update_task(identifier, "processing")
            self.preparer.prepare_vacancy(vacancy_id, response_id=response_id)
            with self.factory.begin() as session:
                task = session.get(AnalysisTask, identifier)
                if task is not None and task.status == "processing":
                    Repository(session).update_task(identifier, "completed")
        except Exception:
            with self.factory.begin() as session:
                task = session.get(AnalysisTask, identifier)
                if task is not None and task.status in ("pending", "processing"):
                    Repository(session).update_task(identifier, "failed", error_message=
                        "Обработка не выполнена. Проверьте настройки и повторите запрос; входные данные не публикуются.")
