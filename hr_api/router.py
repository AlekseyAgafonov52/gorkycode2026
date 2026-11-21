from __future__ import annotations

from typing import Annotated
from uuid import UUID

import pymupdf
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from contract_models import ContractReport, ContractResponse, ContractVacancy, ErrorEnvelope, RecommendationResult, RecommendationUpdate, SourceTextUpdate, VacancyStatusUpdate, ViewedResult, ViewedUpdate, WorkflowResult, WorkflowUpdate
from storage.crud import NotFoundError

from storage.database import make_engine, make_session_factory
from storage.schemas import MatchStatus, ResponseUpdate, WorkflowStatus
from .schemas import (
    ArchivePatch, CriteriaPatch, CriteriaResult, GenerateQuestions,
    ResponseDetail, TaskView, VacancyDetail,
)
from .service import HRService


router = APIRouter(prefix="/api/v1", responses={
    404: {"model": ErrorEnvelope, "description": "Объект не найден"},
    409: {"model": ErrorEnvelope, "description": "Данные изменились или пул вопросов не подготовлен"},
    422: {"model": ErrorEnvelope, "description": "Ошибка валидации"},
    500: {"model": ErrorEnvelope, "description": "Внутренняя ошибка"},
})


def get_service(request: Request) -> HRService:
    with request.app.state.hr_lock:
        if request.app.state.hr_service is None:
            try:
                engine = make_engine()
            except ValueError:
                raise HTTPException(500, "Подключение PostgreSQL не настроено") from None
            request.app.state.hr_engine = engine
            request.app.state.hr_service = HRService(make_session_factory(engine))
        return request.app.state.hr_service


Service = Annotated[HRService, Depends(get_service)]


def identifier(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFoundError("Объект с идентификатором " + value + " не найден") from None


@router.get("/vacancies", response_model=list[ContractVacancy], tags=["Вакансии"])
def vacancies(service: Service):
    return service.list_vacancies()


@router.get("/vacancies/{vacancy_id}", response_model=VacancyDetail, tags=["Вакансии"])
def vacancy(vacancy_id: UUID, service: Service):
    return service.vacancy(vacancy_id)


@router.patch("/vacancies/{vacancy_id}/criteria", response_model=CriteriaResult, tags=["Вакансии"])
def criteria(vacancy_id: UUID, payload: CriteriaPatch, service: Service):
    return service.update_criteria(vacancy_id, payload)


@router.patch("/vacancies/{vacancy_id}/status", response_model=VacancyDetail, tags=["Вакансии"],
              summary="HR закрывает или повторно открывает вакансию")
def vacancy_status(vacancy_id: UUID, payload: VacancyStatusUpdate, service: Service):
    return service.update_vacancy_status(vacancy_id, payload.status)


@router.get("/vacancies/{vacancy_id}/responses", response_model=list[ContractResponse], tags=["Отклики"])
def responses(vacancy_id: str, service: Service, match_status: MatchStatus | None = None,
              workflow_status: WorkflowStatus | None = None, is_archived: bool | None = None,
              is_viewed: bool | None = None,
              limit: int = Query(default=1000, ge=1, le=1000), offset: int = Query(default=0, ge=0)):
    return service.list_responses(identifier(vacancy_id), match_status=match_status, workflow_status=workflow_status,
                                   is_archived=is_archived, is_viewed=is_viewed, limit=limit, offset=offset)


@router.get("/responses/{response_id}", response_model=ResponseDetail, tags=["Отклики"])
def response(response_id: UUID, service: Service):
    return service.response(response_id)


@router.patch("/responses/{response_id}/status", response_model=WorkflowResult, tags=["Отклики"])
def status(response_id: str, payload: WorkflowUpdate, service: Service):
    return service.update_status(identifier(response_id), payload.workflow_status, payload.recruiter_recommendation)


@router.patch("/responses/{response_id}/department-summary", response_model=RecommendationResult, tags=["Отклики"],
              summary="Рекомендации рекрутера для департамента")
def department_summary(response_id: UUID, payload: RecommendationUpdate, service: Service):
    return service.update_recommendation(response_id, payload.recruiter_recommendation)


@router.patch("/responses/{response_id}/viewed", response_model=ViewedResult, tags=["Отклики"],
              summary="Отметить отклик просмотренным или непросмотренным")
def viewed(response_id: str, payload: ViewedUpdate, service: Service):
    return service.update_viewed(identifier(response_id), payload.is_viewed)


@router.patch("/responses/{response_id}/source-text", response_model=ContractReport, tags=["Отклики"],
              summary="Изменить исходный текст резюме",
              description="Сохраняет текст, повторно извлекает поля и атомарно обновляет отчёт из сохранённого пула. YandexGPT не вызывается.")
def source_text(response_id: str, payload: SourceTextUpdate, service: Service):
    return service.update_source_text(identifier(response_id), payload)


@router.patch("/responses/{response_id}/archive", response_model=ContractResponse, tags=["Отклики"])
def archive(response_id: UUID, payload: ArchivePatch, service: Service):
    return service.update_response(response_id, ResponseUpdate(is_archived=payload.is_archived))


@router.post("/vacancies/{vacancy_id}/match", response_model=TaskView, status_code=202, tags=["Сопоставление"],
             summary="Подготовить отчёты вакансии",
             description="Фоновое сопоставление навыков и выбор вопросов из сохранённого пула вакансии.")
def match(vacancy_id: UUID, service: Service):
    return service.request_match(vacancy_id)


@router.get("/tasks/{task_id}", response_model=TaskView, tags=["Задачи"])
def task(task_id: UUID, service: Service):
    return service.task(task_id)


@router.get("/responses/{response_id}/report", response_model=ContractReport, tags=["Отчёты"])
def report(response_id: str, service: Service):
    return service.report(identifier(response_id))


@router.get("/responses/{response_id}/report/html", response_class=HTMLResponse, tags=["Отчёты"],
            summary="Офлайн-отчёт по отклику в HTML")
def report_html(response_id: str, service: Service):
    return HTMLResponse(service.offline_report_html(identifier(response_id)), headers={
        "Cache-Control": "private, no-cache",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'",
    })


@router.get("/responses/{response_id}/report/pdf", response_class=FileResponse, tags=["Отчёты"],
            responses={200: {"content": {"application/pdf": {}}}}, summary="Сохранённый PDF-отчёт по отклику")
def report_pdf(response_id: str, service: Service, download: bool = False):
    path = service.pdf_report_path(identifier(response_id))
    return FileResponse(path, media_type="application/pdf", filename=path.name,
        content_disposition_type="attachment" if download else "inline",
        headers={"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"})


@router.get("/responses/{response_id}/report/pdf/preview", response_class=JSONResponse, tags=["Отчёты"],
            responses={200: {"content": {"image/png": {}}}}, summary="Размеры страниц PDF или изображение выбранной страницы")
def report_pdf_preview(response_id: str, service: Service, page: Annotated[int | None, Query(ge=1)] = None):
    path = service.pdf_report_path(identifier(response_id))
    headers = {"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"}
    try:
        with pymupdf.open(path) as pdf:
            if pdf.needs_pass or not pdf.page_count:
                raise HTTPException(422, "Предпросмотр этого PDF недоступен")
            if page is None:
                return JSONResponse({"pages": [{"width": p.rect.width, "height": p.rect.height} for p in pdf]}, headers=headers)
            if page > pdf.page_count:
                raise NotFoundError("Страница PDF не найдена")
            pdf_page = pdf[page - 1]
            zoom = min(2, 1700 / max(pdf_page.rect.width, pdf_page.rect.height))
            image = pdf_page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            return Response(image.tobytes("png"), media_type="image/png", headers=headers)
    except (OSError, pymupdf.FileDataError):
        raise HTTPException(422, "Предпросмотр этого PDF недоступен") from None


@router.post("/responses/{response_id}/questions/generate", response_model=TaskView, status_code=202, tags=["Отчёты"],
             description="Подготовить отчёт из пула вакансии. Модель вызывается лишь при отсутствии актуального пула; GET её не вызывает.")
def questions(response_id: UUID, service: Service, payload: GenerateQuestions | None = None):
    return service.request_questions(response_id, force=payload.force if payload else False)
