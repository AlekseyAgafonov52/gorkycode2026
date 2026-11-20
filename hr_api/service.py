"""HR operations and durable jobs; no model calls while reading data."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker

from candidate_context import extract_candidate_skills
from storage.crud import NotFoundError, Repository, StaleCriteriaError
from storage.models import AnalysisTask, MatchResult, Report, Response, Vacancy
from storage.schemas import CriterionRead, CriterionUpdate, MatchResultRead, MatchResultWrite, ReportWrite, ResponseUpdate, TaskCreate
from .questions import QuestionContext, QuestionProvider, RequiredCriterion, YandexQuestionProvider
from .schemas import (
    CriteriaPatch, CriteriaResult, CriterionAnalysis, QuestionView, ReportView,
    ResponseDetail, ResponseSummary, TaskView, VacancyDetail, VacancySummary,
)


class HRService:
    def __init__(self, factory: sessionmaker[Session], provider: QuestionProvider | None = None):
        self.factory = factory
        self.provider = provider or YandexQuestionProvider()

    @staticmethod
    def _task_view(task: AnalysisTask, *, reused: bool = False) -> TaskView:
        return TaskView.model_validate({
            "task_id": task.id, "task_type": task.task_type, "vacancy_id": task.vacancy_id,
            "response_id": task.response_id, "status": task.status, "error_message": task.error_message,
            "created_at": task.created_at, "completed_at": task.completed_at, "reused": reused,
            "is_stub": task.task_type == "matching",
            "message": "Заглушка: сопоставление и оценка соответствия пока не выполняются" if task.task_type == "matching" else None,
        })

    @staticmethod
    def _latest(session: Session, vacancy_id: UUID, kind: str, response_id: UUID | None = None):
        query = select(AnalysisTask).where(AnalysisTask.vacancy_id == vacancy_id, AnalysisTask.task_type == kind)
        if response_id is not None:
            query = query.where(AnalysisTask.response_id == response_id)
        return session.scalar(query.order_by(AnalysisTask.created_at.desc(), AnalysisTask.id.desc()).limit(1))

    def _enqueue(self, repo: Repository, vacancy: Vacancy, *, response: Response | None = None,
                 force: bool = False) -> TaskView:
        kind = "questions" if response is not None else "matching"
        latest = self._latest(repo.session, vacancy.id, kind, response.id if response else None)
        if latest is not None and latest.status in ("pending", "processing"):
            return self._task_view(latest, reused=True)
        if kind == "matching" and latest is not None and latest.status == "completed" and latest.created_at >= vacancy.updated_at:
            return self._task_view(latest, reused=True)
        report = repo.get_report(response.id) if response is not None else None
        if response is not None and not force and report is not None and report.criteria_version == vacancy.criteria_version:
            if latest is not None and latest.status == "completed":
                return self._task_view(latest, reused=True)
            if report.questions:
                cached = repo.create_task(TaskCreate(task_type="questions", vacancy_id=vacancy.id, response_id=response.id))
                repo.update_task(cached.id, "completed")
                return self._task_view(cached, reused=True)
        if response is not None and report is None:
            repo.save_report(response.id, ReportWrite(criteria_version=vacancy.criteria_version))
        task = repo.create_task(TaskCreate.model_validate({"task_type": kind, "vacancy_id": vacancy.id,
                                                          "response_id": response.id if response else None}))
        return self._task_view(task)

    def list_vacancies(self) -> list[VacancySummary]:
        with self.factory.begin() as session:
            query = (select(Vacancy, func.count(Response.id)).outerjoin(Response)
                     .group_by(Vacancy.id).order_by(Vacancy.created_at, Vacancy.id))
            return [VacancySummary.model_validate({"id": row.id, "title": row.title, "status": row.status,
                                                   "responses_count": count}) for row, count in session.execute(query)]

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
                task_id = self._enqueue(repo, vacancy).task_id
            return CriteriaResult(vacancy_id=identifier, criteria_version=vacancy.criteria_version,
                criteria=[CriterionRead.model_validate(c) for c in repo.list_criteria(identifier)], changed=changed, task_id=task_id)

    @staticmethod
    def _summary(response: Response, version: int) -> ResponseSummary:
        match = response.match_result
        stale = match is not None and match.criteria_version != version
        current = match if not stale else None
        return ResponseSummary.model_validate({"id": response.id, "candidate_name": response.candidate_name,
            "match_score": float(current.match_score) if current and current.match_score is not None else None,
            "match_status": current.match_status if current else None,
            "missing_data_count": len(current.missing_data) if current else None,
            "workflow_status": response.workflow_status, "is_archived": response.is_archived, "is_stale": stale})

    def list_responses(self, vacancy_id: UUID, *, match_status: str | None = None,
                       workflow_status: str | None = None, is_archived: bool = False,
                       limit: int = 1000, offset: int = 0) -> list[ResponseSummary]:
        with self.factory.begin() as session:
            vacancy = Repository(session).get_vacancy(vacancy_id)
            query = (select(Response).outerjoin(MatchResult).where(Response.vacancy_id == vacancy_id,
                      Response.is_archived == is_archived).options(selectinload(Response.match_result)))
            if workflow_status is not None:
                query = query.where(Response.workflow_status == workflow_status)
            if match_status is not None:
                query = query.where(MatchResult.match_status == match_status, MatchResult.criteria_version == vacancy.criteria_version)
            score = case((MatchResult.criteria_version == vacancy.criteria_version, MatchResult.match_score), else_=None)
            rows = session.scalars(query.order_by(score.desc().nullslast(), Response.created_at, Response.id).limit(limit).offset(offset))
            return [self._summary(row, vacancy.criteria_version) for row in rows]

    def _report(self, repo: Repository, response: Response) -> ReportView:
        vacancy = repo.get_vacancy(response.vacancy_id)
        report = repo.get_report(response.id)
        match = repo.get_match_result(response.id)
        stale = report is not None and report.criteria_version != vacancy.criteria_version
        current_report = report if not stale else None
        current_match = match if match is not None and match.criteria_version == vacancy.criteria_version else None
        latest = self._latest(repo.session, vacancy.id, "questions", response.id)
        state = "not_ready"
        if latest is not None and latest.status in ("pending", "processing"):
            state = latest.status
        elif stale:
            state = "stale"
        elif current_report is not None:
            if latest is not None and latest.status == "failed" and not current_report.questions:
                state = "failed"
            else:
                state = "ready" if current_match is not None else "partial"
        analyses = []
        if current_match is not None:
            for evaluation in current_match.evaluations:
                criterion = evaluation.criterion
                analyses.append(CriterionAnalysis.model_validate({"criterion_id": criterion.id, "name": criterion.name,
                    "importance": criterion.importance, "status": evaluation.status,
                    "evidence": evaluation.evidence, "explanation": evaluation.explanation}))
        names = {criterion.id: criterion.name for criterion in vacancy.criteria}
        questions = []
        if current_report is not None:
            questions = [QuestionView.model_validate({"id": q.id, "criterion_id": q.criterion_id,
                "requirement": names.get(q.criterion_id) if q.criterion_id is not None else None, "question": q.question,
                "expected_answer": q.expected_answer, "follow_up_hint": q.follow_up_hint,
                "position": q.position}) for q in current_report.questions]
        return ReportView.model_validate({"report_id": report.id if report else None, "response_id": response.id,
            "state": state, "candidate_name": response.candidate_name,
            "match_score": float(current_match.match_score) if current_match and current_match.match_score is not None else None,
            "match_status": current_match.match_status if current_match else None,
            "summary": current_report.summary if current_report else None,
            "strengths": current_report.strengths if current_report else [], "criteria_analysis": analyses,
            "missing_data": current_match.missing_data if current_match else [], "questions": questions,
            "source": response.source_json, "criteria_version": vacancy.criteria_version, "is_stale": stale})

    def report(self, identifier: UUID) -> ReportView:
        with self.factory.begin() as session:
            repo = Repository(session)
            return self._report(repo, repo.get_response(identifier))

    def response(self, identifier: UUID) -> ResponseDetail:
        with self.factory.begin() as session:
            repo = Repository(session)
            row = repo.get_response(identifier)
            report = self._report(repo, row)
            summary = self._summary(row, report.criteria_version)
            structured = {key: row.source_json.get(key) for key in
                          ("experience_total_months", "work_experience", "education_level", "education", "skills")}
            return ResponseDetail.model_validate({**summary.model_dump(), "vacancy_id": row.vacancy_id,
                "source_json": row.source_json, "structured_resume": structured, "criteria_analysis": report.criteria_analysis,
                "missing_data": report.missing_data, "report_id": report.report_id, "report_state": report.state,
                "criteria_version": report.criteria_version})

    def update_response(self, identifier: UUID, data: ResponseUpdate) -> ResponseSummary:
        with self.factory.begin() as session:
            repo = Repository(session)
            row = repo.update_response(identifier, data)
            return self._summary(row, repo.get_vacancy(row.vacancy_id).criteria_version)

    def publish_match_result(self, identifier: UUID, data: MatchResultWrite) -> None:
        """Publication policy for a future matcher; the stub never calls it."""
        with self.factory.begin() as session:
            repo = Repository(session)
            repo.save_match_result(identifier, data)
            if data.match_status == "red":
                repo.update_response(identifier, ResponseUpdate(is_archived=True))

    def request_match(self, identifier: UUID) -> TaskView:
        with self.factory.begin() as session:
            repo = Repository(session)
            return self._enqueue(repo, repo._lock_vacancy(identifier))

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
        # Called only by the worker owning the PostgreSQL advisory lock.
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
                repo = Repository(session)
                response = repo.get_response(task.response_id) if task.response_id else None
                vacancy_id = task.vacancy_id or (response.vacancy_id if response else None)
                if vacancy_id is None:
                    raise NotFoundError("Цель задачи не найдена")
                vacancy = repo.get_vacancy(vacancy_id)
                version, kind, response_id = vacancy.criteria_version, task.task_type, task.response_id
                context = None
                if kind == "questions":
                    if response is None:
                        raise ValueError("Для генерации нужен отклик")
                    required = [c for c in vacancy.criteria if c.importance == "required"]
                    allowed = {str(c.id) for c in required}
                    match = repo.get_match_result(response.id)
                    if match is not None and match.criteria_version != version:
                        match = None
                    matching = MatchResultRead.model_validate(match).model_dump(mode="json") if match else None
                    if matching is not None:
                        matching["evaluations"] = [e for e in matching["evaluations"] if e["criterion_id"] in allowed]
                    context = QuestionContext(response_id=response.id, vacancy_title=vacancy.title,
                        skills=extract_candidate_skills(response.source_json),
                        required_criteria=[RequiredCriterion.model_validate(c) for c in required],
                        matching=matching, missing_data=match.missing_data if match else [])
                repo.update_task(identifier, "processing")

            # Matching is intentionally a no-op. No scores/colors are fabricated.
            questions = self.provider.generate(context) if context is not None and context.required_criteria else []
            with self.factory.begin() as session:
                repo = Repository(session)
                vacancy = repo._lock_vacancy(vacancy_id)
                task = session.get(AnalysisTask, identifier)
                if task is None or task.status != "processing":
                    return
                if vacancy.criteria_version != version:
                    raise StaleCriteriaError("Критерии изменились во время обработки")
                if kind == "questions" and response_id is not None:
                    allowed_ids = {c.id for c in vacancy.criteria if c.importance == "required"}
                    if any(q.criterion_id not in allowed_ids for q in questions):
                        raise ValueError("Получен вопрос не по обязательному критерию")
                    report = repo.get_report(response_id)
                    if report is None or report.criteria_version != version:
                        repo.save_report(response_id, ReportWrite(criteria_version=version, questions=questions))
                    else:
                        repo.replace_questions(response_id, questions, criteria_version=version)
                repo.update_task(identifier, "completed")
                if kind == "matching":
                    # Regenerate only reports for which questions were requested earlier.
                    responses = list(session.scalars(select(Response).join(Report).where(
                        Response.vacancy_id == vacancy_id, Report.criteria_version != version)))
                    for response in responses:
                        self._enqueue(repo, vacancy, response=response, force=True)
        except Exception:
            with self.factory.begin() as session:
                task = session.get(AnalysisTask, identifier)
                if task is not None and task.status in ("pending", "processing"):
                    Repository(session).update_task(identifier, "failed", error_message=
                        "Обработка не выполнена. Проверьте конфигурацию и повторите запрос; подробности входных данных не публикуются.")
