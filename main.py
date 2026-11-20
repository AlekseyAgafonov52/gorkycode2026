"""Run with: python -m uvicorn main:app --reload."""
from contextlib import asynccontextmanager
from threading import Lock

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from hr_api.router import router as hr_router
from hr_api.service import HRService
from response_parser import CandidateResponse, parse_response
from storage.crud import NotFoundError, StaleCriteriaError
from vacancy_parser import Vacancy, decode_txt, parse_vacancy

MAX_FILE_BYTES = 2 * 1024 * 1024


async def _read_txt_upload(file: UploadFile) -> str:
    try:
        if not file.filename or not file.filename.lower().endswith(".txt"):
            raise HTTPException(415, "Ожидается TXT-файл")
        data = await file.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, "Размер файла превышает 2 МиБ")
        try:
            return decode_txt(data)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, str(exc)) from exc
    finally:
        await file.close()


async def parse_vacancy_endpoint(file: UploadFile = File(...)) -> Vacancy:
    text = await _read_txt_upload(file)
    try:
        return await run_in_threadpool(parse_vacancy, text)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


async def parse_response_endpoint(file: UploadFile = File(...)) -> CandidateResponse:
    text = await _read_txt_upload(file)
    try:
        return await run_in_threadpool(parse_response, text)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def create_app(service: HRService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        if application.state.hr_engine is not None:
            await run_in_threadpool(application.state.hr_engine.dispose)

    application = FastAPI(title="HR platform API", version="2.0.0", lifespan=lifespan,
        description="Вакансии, отклики, критерии, отчёты и задачи. Сопоставление пока работает как заглушка.")
    application.state.hr_service = service
    application.state.hr_engine = None
    application.state.hr_lock = Lock()
    application.include_router(hr_router)
    application.add_api_route("/api/v1/parse/vacancy", parse_vacancy_endpoint, methods=["POST"],
                              response_model=Vacancy, tags=["Парсинг"])
    application.add_api_route("/api/v1/parse/response", parse_response_endpoint, methods=["POST"],
                              response_model=CandidateResponse, tags=["Парсинг"])

    async def missing_handler(_, error: Exception):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    async def stale_handler(_, error: Exception):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    async def validation_handler(_, error: Exception):
        return JSONResponse(status_code=422, content={"detail": str(error)})

    async def database_handler(_, _error: Exception):
        return JSONResponse(status_code=503, content={"detail": "PostgreSQL недоступен или миграции не применены"})

    application.add_exception_handler(NotFoundError, missing_handler)
    application.add_exception_handler(StaleCriteriaError, stale_handler)
    application.add_exception_handler(ValueError, validation_handler)
    application.add_exception_handler(SQLAlchemyError, database_handler)
    return application


app = create_app()
