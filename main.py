"""Run with: python -m uvicorn main:app --reload."""
from fastapi import FastAPI, File, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from response_parser import CandidateResponse, parse_response
from vacancy_parser import Vacancy, decode_txt, parse_vacancy

app = FastAPI(title="Vacancy and response parsers", version="1.1.0")
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


@app.post("/api/v1/parse/vacancy", response_model=Vacancy)
async def parse_vacancy_endpoint(file: UploadFile = File(...)) -> Vacancy:
    text = await _read_txt_upload(file)
    try:
        return await run_in_threadpool(parse_vacancy, text)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/v1/parse/response", response_model=CandidateResponse)
async def parse_response_endpoint(file: UploadFile = File(...)) -> CandidateResponse:
    text = await _read_txt_upload(file)
    try:
        return await run_in_threadpool(parse_response, text)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
