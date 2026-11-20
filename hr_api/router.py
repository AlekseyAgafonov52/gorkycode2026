from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from storage.database import make_engine, make_session_factory
from storage.schemas import MatchStatus, ResponseUpdate, WorkflowStatus
from .schemas import (
    ArchivePatch, CriteriaPatch, CriteriaResult, GenerateQuestions, ReportView,
    ResponseDetail, ResponseSummary, StatusPatch, TaskView, VacancyDetail, VacancySummary,
)
from .service import HRService


router = APIRouter(prefix="/api/v1")


def get_service(request: Request) -> HRService:
    with request.app.state.hr_lock:
        if request.app.state.hr_service is None:
            try:
                engine = make_engine()
            except ValueError:
                raise HTTPException(503, "Подключение PostgreSQL не настроено") from None
            request.app.state.hr_engine = engine
            request.app.state.hr_service = HRService(make_session_factory(engine))
        return request.app.state.hr_service


Service = Annotated[HRService, Depends(get_service)]


@router.get("/vacancies", response_model=list[VacancySummary], tags=["Вакансии"])
def vacancies(service: Service):
    return service.list_vacancies()


@router.get("/vacancies/{vacancy_id}", response_model=VacancyDetail, tags=["Вакансии"])
def vacancy(vacancy_id: UUID, service: Service):
    return service.vacancy(vacancy_id)


@router.patch("/vacancies/{vacancy_id}/criteria", response_model=CriteriaResult, tags=["Вакансии"])
def criteria(vacancy_id: UUID, payload: CriteriaPatch, service: Service):
    return service.update_criteria(vacancy_id, payload)


@router.get("/vacancies/{vacancy_id}/responses", response_model=list[ResponseSummary], tags=["Отклики"])
def responses(vacancy_id: UUID, service: Service, match_status: MatchStatus | None = None,
              workflow_status: WorkflowStatus | None = None, is_archived: bool = False,
              limit: int = Query(default=1000, ge=1, le=1000), offset: int = Query(default=0, ge=0)):
    return service.list_responses(vacancy_id, match_status=match_status, workflow_status=workflow_status,
                                   is_archived=is_archived, limit=limit, offset=offset)


@router.get("/responses/{response_id}", response_model=ResponseDetail, tags=["Отклики"])
def response(response_id: UUID, service: Service):
    return service.response(response_id)


@router.patch("/responses/{response_id}/status", response_model=ResponseSummary, tags=["Отклики"])
def status(response_id: UUID, payload: StatusPatch, service: Service):
    return service.update_response(response_id, ResponseUpdate(workflow_status=payload.workflow_status))


@router.patch("/responses/{response_id}/archive", response_model=ResponseSummary, tags=["Отклики"])
def archive(response_id: UUID, payload: ArchivePatch, service: Service):
    return service.update_response(response_id, ResponseUpdate(is_archived=payload.is_archived))


@router.post("/vacancies/{vacancy_id}/match", response_model=TaskView, status_code=202, tags=["Сопоставление"],
             summary="Запустить заглушку сопоставления",
             description="Возвращает фоновую задачу. FAISS и расчёт оценки пока не реализованы; is_stub=true.")
def match(vacancy_id: UUID, service: Service):
    return service.request_match(vacancy_id)


@router.get("/tasks/{task_id}", response_model=TaskView, tags=["Задачи"])
def task(task_id: UUID, service: Service):
    return service.task(task_id)


@router.get("/responses/{response_id}/report", response_model=ReportView, tags=["Отчёты"])
def report(response_id: UUID, service: Service):
    return service.report(response_id)


@router.post("/responses/{response_id}/questions/generate", response_model=TaskView, status_code=202, tags=["Отчёты"],
             description="Поставить генерацию в очередь. Используются только skills и required-критерии. GET модель не вызывает.")
def questions(response_id: UUID, service: Service, payload: GenerateQuestions | None = None):
    return service.request_questions(response_id, force=payload.force if payload else False)
