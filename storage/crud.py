"""Transactional CRUD. The caller owns commit/rollback via session_scope."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from typing import TypeVar
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from .models import AnalysisTask, Base, CriterionEvaluation, MatchResult, Question, Report, Response, Vacancy, VacancyCriterion
from .schemas import (
    AnalysisFreshness, CriterionCreate, CriterionUpdate, MatchResultWrite, QuestionCreate,
    ReportWrite, ResponseCreate, ResponseUpdate, TaskCreate, TaskStatus, VacancyCreate,
    VacancyStatus, VacancyUpdate, WorkflowStatus, MatchStatus,
)


class NotFoundError(LookupError):
    pass


class StaleCriteriaError(ValueError):
    """An asynchronous result must not overwrite results for newer criteria."""


Model = TypeVar("Model", bound=Base)


class Repository:
    def __init__(self, session: Session):
        self.session = session

    def _get(self, model: type[Model], identifier: UUID) -> Model:
        row = self.session.get(model, identifier)
        if row is None:
            raise NotFoundError(f"{model.__tablename__}: запись не найдена")
        return row

    def _lock_vacancy(self, identifier: UUID) -> Vacancy:
        row = self.session.scalar(select(Vacancy).where(Vacancy.id == identifier).with_for_update()
                                  .execution_options(populate_existing=True))
        if row is None:
            raise NotFoundError("Вакансия не найдена")
        return row

    def _lock_response_vacancy(self, response_id: UUID) -> tuple[Response, Vacancy]:
        response = self._get(Response, response_id)
        vacancy = self._lock_vacancy(response.vacancy_id)
        # A concurrent update may have committed while the vacancy lock was
        # awaited, or this Session may have read the response earlier.
        self.session.refresh(response)
        return response, vacancy

    @staticmethod
    def _check_version(vacancy: Vacancy, criteria_version: int) -> None:
        if criteria_version != vacancy.criteria_version:
            raise StaleCriteriaError(f"Версия результата {criteria_version}; текущая версия критериев {vacancy.criteria_version}")

    def _check_criteria(self, vacancy_id: UUID, identifiers: Sequence[UUID]) -> None:
        expected = set(identifiers)
        if not expected:
            return
        found = set(self.session.scalars(select(VacancyCriterion.id).where(
            VacancyCriterion.vacancy_id == vacancy_id, VacancyCriterion.id.in_(expected))))
        if found != expected:
            raise ValueError("Все критерии должны существовать и относиться к вакансии отклика")

    @staticmethod
    def _page(limit: int, offset: int) -> None:
        if isinstance(limit, bool) or isinstance(offset, bool) or not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("Нужны limit от 1 до 1000 и offset не меньше 0")

    def create_vacancy(self, data: VacancyCreate) -> Vacancy:
        row = Vacancy(**deepcopy(data.model_dump(exclude={"criteria"})), criteria_version=1)
        row.criteria = [VacancyCriterion(**item.model_dump()) for item in data.criteria]
        self.session.add(row)
        self.session.flush()
        return row

    def get_vacancy(self, vacancy_id: UUID) -> Vacancy:
        row = self.session.scalar(select(Vacancy).where(Vacancy.id == vacancy_id)
                                  .options(selectinload(Vacancy.criteria)))
        if row is None:
            raise NotFoundError("Вакансия не найдена")
        return row

    def list_vacancies(self, *, status: VacancyStatus | None = None, limit: int = 100, offset: int = 0) -> list[Vacancy]:
        self._page(limit, offset)
        query = select(Vacancy).options(selectinload(Vacancy.criteria))
        if status is not None:
            query = query.where(Vacancy.status == status)
        return list(self.session.scalars(query.order_by(Vacancy.created_at, Vacancy.id).limit(limit).offset(offset)))

    def update_vacancy(self, vacancy_id: UUID, data: VacancyUpdate) -> Vacancy:
        row = self._lock_vacancy(vacancy_id)
        if "source_json" in data.model_fields_set and data.source_json != row.source_json:
            targets = select(Response.id).where(Response.vacancy_id == vacancy_id)
            self.session.execute(update(Report).where(Report.response_id.in_(targets))
                                 .values(contract_json=None, prepared_hash=None))
        for key, value in deepcopy(data.model_dump(exclude_unset=True)).items():
            setattr(row, key, value)
        self.session.flush()
        return row

    def delete_vacancy(self, vacancy_id: UUID) -> None:
        self.session.delete(self._lock_vacancy(vacancy_id))
        self.session.flush()
        self.session.expire_all()

    def get_criterion(self, criterion_id: UUID) -> VacancyCriterion:
        return self._get(VacancyCriterion, criterion_id)

    def list_criteria(self, vacancy_id: UUID) -> list[VacancyCriterion]:
        self._get(Vacancy, vacancy_id)
        return list(self.session.scalars(select(VacancyCriterion).where(VacancyCriterion.vacancy_id == vacancy_id)
                                         .order_by(VacancyCriterion.created_at, VacancyCriterion.id)))

    def create_criterion(self, vacancy_id: UUID, data: CriterionCreate) -> VacancyCriterion:
        vacancy = self._lock_vacancy(vacancy_id)
        row = VacancyCriterion(vacancy_id=vacancy_id, **data.model_dump())
        self.session.add(row)
        vacancy.criteria_version += 1
        self.session.flush()
        self.session.expire(vacancy, ["criteria"])
        return row

    def update_criteria(self, vacancy_id: UUID, changes: Mapping[UUID, CriterionUpdate]) -> Vacancy:
        """Increment once per batch, and only if at least one field changes."""
        vacancy = self._lock_vacancy(vacancy_id)
        self._check_criteria(vacancy_id, list(changes))
        changed = False
        for identifier, patch in changes.items():
            criterion = self._get(VacancyCriterion, identifier)
            self.session.refresh(criterion)
            for key, value in patch.model_dump(exclude_unset=True).items():
                if getattr(criterion, key) != value:
                    setattr(criterion, key, value)
                    changed = True
        if changed:
            vacancy.criteria_version += 1
        self.session.flush()
        return vacancy

    def update_criterion(self, criterion_id: UUID, data: CriterionUpdate) -> VacancyCriterion:
        row = self._get(VacancyCriterion, criterion_id)
        self.update_criteria(row.vacancy_id, {criterion_id: data})
        return row

    def delete_criterion(self, criterion_id: UUID) -> None:
        criterion = self._get(VacancyCriterion, criterion_id)
        vacancy = self._lock_vacancy(criterion.vacancy_id)
        self.session.delete(criterion)
        vacancy.criteria_version += 1
        self.session.flush()
        self.session.expire_all()

    def create_response(self, data: ResponseCreate) -> Response:
        self._get(Vacancy, data.vacancy_id)
        row = Response(**deepcopy(data.model_dump()))
        self.session.add(row)
        self.session.flush()
        return row

    def get_response(self, response_id: UUID) -> Response:
        return self._get(Response, response_id)

    def list_responses(self, vacancy_id: UUID, *, workflow_status: WorkflowStatus | None = None,
                       is_archived: bool | None = None, match_status: MatchStatus | None = None,
                       is_viewed: bool | None = None,
                       limit: int = 100, offset: int = 0) -> list[Response]:
        self._page(limit, offset)
        self._get(Vacancy, vacancy_id)
        query = select(Response).outerjoin(MatchResult).where(Response.vacancy_id == vacancy_id)
        if workflow_status is not None:
            query = query.where(Response.workflow_status == workflow_status)
        if is_archived is not None:
            query = query.where(Response.is_archived == is_archived)
        if is_viewed is not None:
            query = query.where(Response.is_viewed == is_viewed)
        if match_status is not None:
            query = query.where(MatchResult.match_status == match_status)
        query = query.order_by(MatchResult.match_score.desc().nullslast(), Response.created_at, Response.id)
        return list(self.session.scalars(query.limit(limit).offset(offset)))

    def update_response(self, response_id: UUID, data: ResponseUpdate) -> Response:
        row, _ = self._lock_response_vacancy(response_id)
        if any(key in data.model_fields_set and getattr(data, key) != getattr(row, key)
               for key in ("source_json", "candidate_name")):
            self.session.execute(update(Report).where(Report.response_id == response_id)
                                 .values(contract_json=None, prepared_hash=None))
        for key, value in deepcopy(data.model_dump(exclude_unset=True)).items():
            setattr(row, key, value)
        self.session.flush()
        return row

    def delete_response(self, response_id: UUID) -> None:
        self.session.delete(self._get(Response, response_id))
        self.session.flush()
        self.session.expire_all()

    def create_task(self, data: TaskCreate) -> AnalysisTask:
        if data.vacancy_id is not None:
            self._get(Vacancy, data.vacancy_id)
        if data.response_id is not None:
            response = self._get(Response, data.response_id)
            if data.vacancy_id is not None and data.vacancy_id != response.vacancy_id:
                raise ValueError("Отклик задачи относится к другой вакансии")
        row = AnalysisTask(**data.model_dump(), status="pending")
        self.session.add(row)
        self.session.flush()
        return row

    def get_task(self, task_id: UUID) -> AnalysisTask:
        return self._get(AnalysisTask, task_id)

    def list_tasks(self, *, vacancy_id: UUID | None = None, response_id: UUID | None = None,
                   status: TaskStatus | None = None, limit: int = 100, offset: int = 0) -> list[AnalysisTask]:
        self._page(limit, offset)
        query = select(AnalysisTask)
        if vacancy_id is not None:
            query = query.where(AnalysisTask.vacancy_id == vacancy_id)
        if response_id is not None:
            query = query.where(AnalysisTask.response_id == response_id)
        if status is not None:
            query = query.where(AnalysisTask.status == status)
        return list(self.session.scalars(query.order_by(AnalysisTask.created_at, AnalysisTask.id).limit(limit).offset(offset)))

    def update_task(self, task_id: UUID, status: TaskStatus, *, error_message: str | None = None) -> AnalysisTask:
        if status not in ("pending", "processing", "completed", "failed"):
            raise ValueError("Недопустимый статус задачи")
        row = self._get(AnalysisTask, task_id)
        row.status = status
        row.error_message = error_message if status == "failed" else None
        row.completed_at = datetime.now(timezone.utc) if status in ("completed", "failed") else None
        self.session.flush()
        return row

    def delete_task(self, task_id: UUID) -> None:
        self.session.delete(self._get(AnalysisTask, task_id))
        self.session.flush()

    def get_match_result(self, response_id: UUID) -> MatchResult | None:
        self._get(Response, response_id)
        return self.session.scalar(select(MatchResult).where(MatchResult.response_id == response_id)
                                   .options(selectinload(MatchResult.evaluations))
                                   .execution_options(populate_existing=True))

    def save_match_result(self, response_id: UUID, data: MatchResultWrite) -> MatchResult:
        """Replace the current result atomically without duplicating response_id."""
        response, vacancy = self._lock_response_vacancy(response_id)
        self._check_version(vacancy, data.criteria_version)
        self._check_criteria(response.vacancy_id, [item.criterion_id for item in data.evaluations])
        row = self.get_match_result(response_id)
        if row is None:
            row = MatchResult(response_id=response_id)
            self.session.add(row)
        else:
            row.evaluations.clear()
            self.session.flush()  # Delete old unique pairs before inserting replacements.
        for key, value in deepcopy(data.model_dump(exclude={"evaluations"})).items():
            setattr(row, key, value)
        row.analyzed_at = datetime.now(timezone.utc)
        row.evaluations = [CriterionEvaluation(**deepcopy(item.model_dump())) for item in data.evaluations]
        self.session.flush()
        self.session.expire(response, ["match_result"])
        return row

    def delete_match_result(self, response_id: UUID) -> None:
        self._lock_response_vacancy(response_id)
        row = self.get_match_result(response_id)
        if row is not None:
            self.session.delete(row)
            self.session.flush()
            self.session.expire_all()

    def get_report(self, response_id: UUID) -> Report | None:
        self._get(Response, response_id)
        return self.session.scalar(select(Report).where(Report.response_id == response_id)
                                   .options(selectinload(Report.questions))
                                   .execution_options(populate_existing=True))

    def save_report(self, response_id: UUID, data: ReportWrite) -> Report:
        """Replace report fields and questions; keep one report per response."""
        response, vacancy = self._lock_response_vacancy(response_id)
        self._check_version(vacancy, data.criteria_version)
        self._check_criteria(response.vacancy_id, [item.criterion_id for item in data.questions if item.criterion_id is not None])
        row = self.get_report(response_id)
        if row is None:
            row = Report(response_id=response_id)
            self.session.add(row)
        row.contract_json = None
        row.prepared_hash = None
        for key, value in deepcopy(data.model_dump(exclude={"questions"})).items():
            setattr(row, key, value)
        row.updated_at = datetime.now(timezone.utc)
        row.questions = [Question(**item.model_dump()) for item in data.questions]
        self.session.flush()
        self.session.expire(response, ["report"])
        return row

    def replace_questions(self, response_id: UUID, questions: Sequence[QuestionCreate], *, criteria_version: int) -> Report:
        response, vacancy = self._lock_response_vacancy(response_id)
        self._check_version(vacancy, criteria_version)
        row = self.get_report(response_id)
        if row is None:
            raise NotFoundError("Отчёт не найден")
        if row.criteria_version != criteria_version:
            raise StaleCriteriaError("Сначала обновите устаревший отчёт")
        self._check_criteria(response.vacancy_id, [item.criterion_id for item in questions if item.criterion_id is not None])
        row.contract_json = None
        row.prepared_hash = None
        row.questions = [Question(**item.model_dump()) for item in questions]
        row.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        return row

    def delete_report(self, response_id: UUID) -> None:
        self._lock_response_vacancy(response_id)
        row = self.get_report(response_id)
        if row is not None:
            self.session.delete(row)
            self.session.flush()
            self.session.expire_all()

    def analysis_freshness(self, response_id: UUID) -> AnalysisFreshness:
        query = (select(Vacancy.criteria_version, MatchResult.criteria_version, Report.criteria_version)
                 .select_from(Response).join(Vacancy).outerjoin(MatchResult).outerjoin(Report)
                 .where(Response.id == response_id))
        versions = self.session.execute(query).one_or_none()
        if versions is None:
            raise NotFoundError("Отклик не найден")
        current, match, report = versions
        return AnalysisFreshness(criteria_version=current, match_criteria_version=match, report_criteria_version=report,
                                 match_is_stale=None if match is None else match != current,
                                 report_is_stale=None if report is None else report != current)
