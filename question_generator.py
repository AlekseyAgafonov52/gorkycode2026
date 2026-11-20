"""Generate HR questions from a vacancy, DOCX duties and existing candidate JSON.

Independent of Resume Parser and FastAPI. Documents are data, not instructions.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable
from zipfile import BadZipFile

import httpx
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table
from docx.text.paragraph import Paragraph
from dotenv import dotenv_values
from lxml.etree import XMLSyntaxError
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, TypeAdapter, ValidationError, field_validator, model_validator

from candidate_context import extract_candidate_skills
from question_prompts import PROMPT_PROFILES, build_prompts
from job_sources import extract_instruction_duties, load_vacancy_text


ENDPOINT = "https://ai.api.cloud.yandex.net/foundationModels/v1/completion"
DEFAULT_ENV_FILE = Path(__file__).resolve().with_name(".env")
JSON_OBJECT = TypeAdapter(dict[str, JsonValue], config=ConfigDict(strict=True))


class QuestionGenerationError(RuntimeError):
    """No validated result is available; no fabricated fallback is returned."""


class InsufficientGapsError(QuestionGenerationError):
    """There are fewer than three supported clarification questions."""


class YandexGPTError(QuestionGenerationError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class ClarifyingQuestion(StrictModel):
    requirement: str = Field(min_length=1, description="Дословная цитата из вакансии или раздела обязанностей ДИ")
    question: str = Field(min_length=5, description="Персонализированный вопрос только о недостающих сведениях")
    reason: str = Field(min_length=5, description="Каких конкретно сведений недостаточно, с учётом уже указанных фактов")


class QuestionDraft(StrictModel):
    candidate_id: str = Field(min_length=1)
    questions: list[ClarifyingQuestion] = Field(max_length=5)


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip().casefold()


def _requirement_quote(requirement: str, instruction: str) -> str | None:
    """Resolve spacing/case and Russian е/ё to the exact original source quote."""
    words = requirement.split()
    if not words:
        return None
    pattern = r"\s+".join(re.sub("[её]", "[её]", re.escape(word), flags=re.I) for word in words)
    if words[0][0].isalnum():
        pattern = r"(?<!\w)" + pattern
    if words[-1][-1].isalnum():
        pattern += r"(?!\w)"
    match = re.search(pattern, instruction, re.I)
    return match.group() if match else None


class ClarificationResult(QuestionDraft):
    questions: list[ClarifyingQuestion] = Field(min_length=3, max_length=5)

    @model_validator(mode="after")
    def unique_questions(self) -> ClarificationResult:
        keys = [re.sub(r"[^\w]+", "", _normalized(q.question)) for q in self.questions]
        if len(keys) != len(set(keys)):
            raise ValueError("Вопросы повторяются")
        return self


class ReviewDecision(StrictModel):
    index: int = Field(ge=0)
    requirement_is_supported: bool
    answer_is_present: bool
    reason_is_accurate: bool
    is_duplicate: bool
    is_job_related: bool
    explanation: str = Field(min_length=1)


class QuestionReview(StrictModel):
    decisions: list[ReviewDecision] = Field(min_length=3, max_length=5)


class CandidateInput(BaseModel):
    """Accept the completed parser JSON without importing or rerunning its code."""

    model_config = ConfigDict(extra="allow", strict=True)
    candidate_id: str | int

    @model_validator(mode="before")
    @classmethod
    def validate_json_object(cls, value: Any) -> dict[str, JsonValue]:
        data = JSON_OBJECT.validate_python(value)
        json.dumps(data, ensure_ascii=False, allow_nan=False)
        return data

    @field_validator("candidate_id")
    @classmethod
    def valid_identifier(cls, value: str | int) -> str | int:
        if isinstance(value, bool) or isinstance(value, int) and value < 1:
            raise ValueError("candidate_id должен быть положительным числом или непустой строкой")
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("candidate_id не должен быть пустым")
        return value


class YandexGPTSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True,
                              protected_namespaces=("model_validate", "model_dump"))
    folder_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    api_key: SecretStr | None = None
    iam_token: SecretStr | None = None
    model_uri: str | None = None
    timeout_seconds: float = Field(default=60, gt=0, le=300)
    max_tokens: int = Field(default=2500, ge=256, le=8000)
    http_retries: int = Field(default=1, ge=0, le=3)
    max_input_chars: int = Field(default=100_000, ge=1000)

    @field_validator("api_key", "iam_token", mode="before")
    @classmethod
    def clean_secret(cls, value: str | SecretStr | None) -> str | None:
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        return value.strip() or None if isinstance(value, str) else value

    @model_validator(mode="after")
    def authentication(self) -> YandexGPTSettings:
        has_key = bool(self.api_key and self.api_key.get_secret_value().strip())
        has_token = bool(self.iam_token and self.iam_token.get_secret_value().strip())
        if has_key == has_token:
            raise ValueError("Задайте ровно один способ авторизации: YANDEX_API_KEY или YANDEX_IAM_TOKEN")
        if self.model_uri is None:
            self.model_uri = f"gpt://{self.folder_id}/yandexgpt-5.1"
        if not self.model_uri.startswith(f"gpt://{self.folder_id}/"):
            raise ValueError("URI модели должен относиться к указанному каталогу Yandex Cloud")
        return self

    @classmethod
    def from_env(cls, env_file: str | Path | None = DEFAULT_ENV_FILE) -> YandexGPTSettings:
        """Read .env next to this module; process environment takes precedence.

        Pass env_file=None to use only the process environment. Values are not
        interpolated or copied into os.environ, so secrets remain literal.
        """
        values = {}
        if env_file is not None:
            path = Path(env_file)
            try:
                if path.is_file():
                    values = dict(dotenv_values(path, encoding="utf-8-sig", interpolate=False))
            except (OSError, UnicodeError):
                raise QuestionGenerationError("Не удалось прочитать файл настроек .env в UTF-8") from None
        for name in ("YANDEX_FOLDER_ID", "YANDEX_API_KEY", "YANDEX_IAM_TOKEN", "YANDEX_MODEL_URI"):
            if name in os.environ:
                values[name] = os.environ[name]
        folder = (values.get("YANDEX_FOLDER_ID") or "").strip()
        if not folder:
            raise QuestionGenerationError("Задайте YANDEX_FOLDER_ID и YANDEX_API_KEY (или YANDEX_IAM_TOKEN) в .env или окружении")
        try:
            return cls.model_validate({"folder_id": folder, "api_key": values.get("YANDEX_API_KEY") or None,
                                       "iam_token": values.get("YANDEX_IAM_TOKEN") or None,
                                       "model_uri": values.get("YANDEX_MODEL_URI") or None})
        except ValidationError:
            # Never include raw validation inputs: they may contain credentials.
            raise QuestionGenerationError("Проверьте настройки YandexGPT: каталог, URI модели и ровно один непустой ключ/токен") from None


def _container_text(container: Any) -> list[str]:
    result = []
    for block in container.iter_inner_content():
        if isinstance(block, Paragraph):
            if block.text.strip():
                result.append(block.text.strip())
        elif isinstance(block, Table):
            seen = set()
            for row in block.rows:
                cells = []
                for cell in row.cells:
                    # Merged cells appear multiple times in python-docx's grid.
                    if cell._tc in seen:
                        continue
                    seen.add(cell._tc)
                    content = "\n".join(_container_text(cell))
                    if content:
                        cells.append(content)
                if cells:
                    result.append(" | ".join(cells))
    return result


def extract_docx_text(path: str | Path) -> str:
    """Read body paragraphs and tables (including nested tables) in source order."""
    path = Path(path)
    if path.suffix.lower() != ".docx":
        raise ValueError("Ожидается должностная инструкция в формате DOCX")
    if not path.is_file():
        raise ValueError(f"DOCX-файл не найден: {path}")
    try:
        text = "\n".join(_container_text(Document(str(path))))
    except (BadZipFile, PackageNotFoundError, XMLSyntaxError, KeyError) as exc:
        raise ValueError("Не удалось прочитать DOCX: документ повреждён или имеет другой формат") from exc
    if not text.strip():
        raise ValueError("DOCX не содержит текста; изображения и сканы требуют отдельного OCR")
    return text


def load_candidate_json(path: str | Path) -> CandidateInput:
    path = Path(path)
    try:
        return CandidateInput.model_validate_json(path.read_text(encoding="utf-8-sig"))
    except (ValidationError, UnicodeError) as exc:
        raise ValueError("Некорректный JSON-отклик: нужен объект с непустым candidate_id") from exc


# Compatibility constants for callers using the general profile.
# Generation reloads files so prompt edits apply on the next invocation.
_default_prompts = build_prompts()
GENERATION_PROMPT = _default_prompts.generation
REVIEW_PROMPT = _default_prompts.review


class YandexGPTClient:
    def __init__(self, settings: YandexGPTSettings, *, http_client: httpx.Client | None = None):
        self.settings = settings
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=settings.timeout_seconds, follow_redirects=False)

    def __enter__(self) -> YandexGPTClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def complete(self, system: str, data: dict, schema: type[BaseModel]) -> str:
        try:
            user_text = json.dumps(data, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Входные данные должны содержать только корректные JSON-значения") from exc
        if len(system) + len(user_text) > self.settings.max_input_chars:
            raise ValueError("Вход превышает лимит контекста; автоматическое обрезание фактов отключено")
        api_key, iam_token = self.settings.api_key, self.settings.iam_token
        if api_key is not None:
            authorization = "Api-Key " + api_key.get_secret_value().strip()
        elif iam_token is not None:
            authorization = "Bearer " + iam_token.get_secret_value().strip()
        else:
            raise YandexGPTError("Не задан API-ключ или IAM-токен YandexGPT")
        headers = {"Authorization": authorization, "x-folder-id": self.settings.folder_id,
                   "x-data-logging-enabled": "false"}
        payload = {
            "modelUri": self.settings.model_uri,
            "completionOptions": {"stream": False, "temperature": 0, "maxTokens": str(self.settings.max_tokens)},
            "messages": [{"role": "system", "text": system}, {"role": "user", "text": user_text}],
            "jsonSchema": {"schema": schema.model_json_schema()},
        }
        response: httpx.Response | None = None
        for attempt in range(self.settings.http_retries + 1):
            try:
                response = self._http.post(ENDPOINT, json=payload, headers=headers,
                                           timeout=self.settings.timeout_seconds)
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt < self.settings.http_retries:
                    time.sleep(0.25 * 2**attempt)
                    continue
                raise YandexGPTError("YandexGPT недоступен: ошибка сети или тайм-аут") from None
            if response.status_code in (429, 500, 502, 503, 504) and attempt < self.settings.http_retries:
                time.sleep(0.25 * 2**attempt)
                continue
            if not 200 <= response.status_code < 300:
                # Provider bodies can echo prompts or credentials. Do not log them.
                raise YandexGPTError(f"YandexGPT вернул HTTP {response.status_code}; проверьте доступ, модель и квоты")
            break
        if response is None:
            raise YandexGPTError("YandexGPT не вернул ответ")
        try:
            envelope = response.json()
            # The legacy endpoint wraps the same response in 'result'.
            result = envelope.get("result", envelope)
            alternative = result["alternatives"][0]
            status = alternative["status"]
            if status != "ALTERNATIVE_STATUS_FINAL":
                raise YandexGPTError("YandexGPT не завершил генерацию: ответ обрезан, отфильтрован или частичен")
            content = alternative["message"]["text"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty content")
            return content
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise YandexGPTError("YandexGPT вернул некорректную структуру ответа") from None


def prepare_input(instruction_text: str, candidate: CandidateInput | dict, *, vacancy_text: str) -> dict:
    if not isinstance(instruction_text, str) or not instruction_text.strip():
        raise ValueError("Текст должностной инструкции пуст")
    if not isinstance(vacancy_text, str) or not vacancy_text.strip():
        raise ValueError("Текст вакансии пуст")
    duties = extract_instruction_duties(instruction_text)
    record = candidate if isinstance(candidate, CandidateInput) else CandidateInput.model_validate(candidate)
    # Check Python values before JSON serialization can turn NaN/Infinity into null.
    candidate_data = JSON_OBJECT.validate_python(record.model_dump())
    json.dumps(candidate_data, ensure_ascii=False, allow_nan=False)
    return {"vacancy": vacancy_text.strip(), "job_instruction_duties": duties,
            "candidate_id": str(record.candidate_id),
            "candidate": {"skills": extract_candidate_skills(candidate_data)}}


def generate_questions(instruction_text: str, candidate: CandidateInput | dict, *,
                       vacancy_text: str,
                       client: YandexGPTClient | None = None, max_attempts: int = 2,
                       prompt_profile: str = "general",
                       on_progress: Callable[[str], None] | None = None) -> ClarificationResult:
    """Generate, ground-check, then independently review every proposed question.

    No API or parser calls happen when validating inputs. The result always has
    3–5 questions; if fewer can be supported, raise InsufficientGapsError.
    """
    data = prepare_input(instruction_text, candidate, vacancy_text=vacancy_text)
    prompts = build_prompts(prompt_profile)
    if not 1 <= max_attempts <= 3:
        raise ValueError("max_attempts должен быть от 1 до 3")
    if client is None:
        with YandexGPTClient(YandexGPTSettings.from_env()) as owned:
            return generate_questions(instruction_text, candidate, vacancy_text=vacancy_text, client=owned, max_attempts=max_attempts,
                                      prompt_profile=prompt_profile, on_progress=on_progress)
    candidate_id = data["candidate_id"]
    issues, previous = [], None
    for attempt in range(max_attempts):
        if on_progress:
            on_progress(f"Генерация вопросов: попытка {attempt + 1}/{max_attempts}")
        generation_input = {**data, "validation_feedback": issues, "previous_questions": previous}
        raw = client.complete(prompts.generation, generation_input, QuestionDraft)
        try:
            draft = QuestionDraft.model_validate_json(raw)
        except ValidationError:
            issues = ["Ответ не соответствует JSON Schema; верни только корректный JSON"]
            continue
        if draft.candidate_id != candidate_id:
            issues = ["Изменён candidate_id; используй переданный candidate_id"]
            continue
        if len(draft.questions) < 3:
            raise InsufficientGapsError("Найдено меньше трёх обоснованных вопросов; результат не дополнен выдуманными пробелами")
        unsupported = []
        for i, question in enumerate(draft.questions):
            quote = (_requirement_quote(question.requirement, data["vacancy"])
                     or _requirement_quote(question.requirement, data["job_instruction_duties"]))
            if quote is None:
                unsupported.append(i)
            else:
                question.requirement = quote
        previous = draft.model_dump()
        try:
            result = ClarificationResult.model_validate(previous)
        except ValidationError:
            issues = ["Вопросы повторяются; нужны разные уточнения"]
            continue
        if unsupported:
            issues = [f"Вопрос {i}: requirement должен быть цитатой из вакансии или раздела обязанностей ДИ" for i in unsupported]
            continue
        if on_progress:
            on_progress("Проверка требований, причин и уже известных ответов")
        raw_review = client.complete(prompts.review, {**data, "proposed_questions": previous}, QuestionReview)
        try:
            review = QuestionReview.model_validate_json(raw_review)
        except ValidationError:
            issues = ["Проверка вопросов не вернула корректный JSON; повторно проверь факты"]
            continue
        indices = [decision.index for decision in review.decisions]
        if sorted(indices) != list(range(len(result.questions))):
            issues = ["Проверка должна охватывать каждый вопрос ровно один раз"]
            continue
        issues = [f"Вопрос {d.index}: {d.explanation}" for d in review.decisions
                  if not d.requirement_is_supported or d.answer_is_present or not d.reason_is_accurate
                  or d.is_duplicate or not d.is_job_related]
        if not issues:
            return result
        if on_progress:
            on_progress(f"Проверка отклонила вопросов: {len(issues)}")
    detail = "; ".join(issues)[:1200]
    raise QuestionGenerationError(f"Не удалось получить 3–5 вопросов, прошедших валидацию и проверку фактов. {detail}")


def generate_questions_from_files(instruction_path: str | Path, candidate_path: str | Path, *,
                                  vacancy_path: str | Path,
                                  client: YandexGPTClient | None = None,
                                  prompt_profile: str = "general",
                                  on_progress: Callable[[str], None] | None = None) -> ClarificationResult:
    return generate_questions(extract_docx_text(instruction_path), load_candidate_json(candidate_path),
                              vacancy_text=load_vacancy_text(vacancy_path), client=client,
                              prompt_profile=prompt_profile, on_progress=on_progress)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instruction", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--vacancy", type=Path, required=True, help="Основной источник требований: вакансия TXT или JSON")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--prepare-only", action="store_true", help="Подготовить вход без отправки в YandexGPT")
    parser.add_argument("--prompt-profile", choices=["general", *PROMPT_PROFILES], default="general",
                        help="Область и роль для генерации и проверки вопросов")
    args = parser.parse_args()
    try:
        instruction = extract_docx_text(args.instruction)
        candidate = load_candidate_json(args.candidate)
        vacancy = load_vacancy_text(args.vacancy)
        if args.prepare_only:
            data = prepare_input(instruction, candidate, vacancy_text=vacancy)
            prompts = build_prompts(args.prompt_profile)
            serialized = json.dumps({**data, "prompt_profile": prompts.profile, "domain": prompts.domain,
                                     "system_prompts": {"generation": prompts.generation, "review": prompts.review}},
                                    ensure_ascii=False, indent=2, allow_nan=False)
        else:
            result = generate_questions(instruction, candidate,
                                        vacancy_text=vacancy, prompt_profile=args.prompt_profile)
            serialized = result.model_dump_json(indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized + "\n", encoding="utf-8")
        else:
            print(serialized)
    except (OSError, ValueError, QuestionGenerationError) as exc:
        # Pydantic errors may echo input data. Use a neutral message for them.
        message = "Некорректные входные данные" if isinstance(exc, ValidationError) else str(exc)
        parser.exit(1, message + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
