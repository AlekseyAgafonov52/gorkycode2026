"""Pydantic inputs and ORM read models for the persistence layer."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


VacancyStatus = Literal["draft", "active", "closed"]
Importance = Literal["required", "preferred", "irrelevant"]
WorkflowStatus = Literal["new", "forwarded", "not_forwarded"]
TaskType = Literal["matching", "questions"]
TaskStatus = Literal["pending", "processing", "completed", "failed"]
MatchStatus = Literal["green", "yellow", "red"]
EvaluationStatus = Literal["matched", "partial", "not_matched", "unknown"]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    @model_validator(mode="after")
    def finite_json(self):
        # JSONB rejects NaN/Infinity; do not silently convert them to null.
        json.dumps(self.model_dump(mode="python"), default=str, allow_nan=False)
        return self

    @field_validator("title", "name", "question", check_fields=False)
    @classmethod
    def nonblank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Текст не должен быть пустым")
        return value


class Patch(Schema):
    nullable_fields: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def only_nullable_fields_can_be_cleared(self):
        if any(getattr(self, key) is None for key in self.model_fields_set - self.nullable_fields):
            raise ValueError("Обязательные поля нельзя заменять на null")
        return self


class CriterionCreate(Schema):
    name: str = Field(min_length=1, max_length=255)
    importance: Importance = "preferred"
    description: str | None = None


class CriterionUpdate(Patch):
    nullable_fields = frozenset({"description"})
    name: str | None = Field(default=None, min_length=1, max_length=255)
    importance: Importance | None = None
    description: str | None = None


class VacancyCreate(Schema):
    title: str = Field(min_length=1, max_length=255)
    status: VacancyStatus = "active"
    source_json: dict[str, JsonValue] = Field(default_factory=dict)
    criteria: list[CriterionCreate] = Field(default_factory=list)


class VacancyUpdate(Patch):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    status: VacancyStatus | None = None
    source_json: dict[str, JsonValue] | None = None


class ResponseCreate(Schema):
    vacancy_id: UUID
    candidate_name: str | None = Field(default=None, max_length=255)
    source_json: dict[str, JsonValue]
    workflow_status: WorkflowStatus = "new"
    is_archived: bool = Field(default=False, strict=True)
    is_viewed: bool = Field(default=False, strict=True)


class ResponseUpdate(Patch):
    nullable_fields = frozenset({"candidate_name"})
    candidate_name: str | None = Field(default=None, max_length=255)
    source_json: dict[str, JsonValue] | None = None
    workflow_status: WorkflowStatus | None = None
    is_archived: bool | None = Field(default=None, strict=True)
    is_viewed: bool | None = Field(default=None, strict=True)


class TaskCreate(Schema):
    vacancy_id: UUID | None = None
    response_id: UUID | None = None
    task_type: TaskType

    @model_validator(mode="after")
    def target_required(self):
        if self.vacancy_id is None and self.response_id is None:
            raise ValueError("Нужно указать vacancy_id или response_id")
        return self


class EvaluationWrite(Schema):
    criterion_id: UUID
    status: EvaluationStatus
    explanation: str | None = None
    evidence: list[JsonValue] = Field(default_factory=list)


class MatchResultFields(Schema):
    criteria_version: int = Field(gt=0, strict=True)
    match_score: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)
    match_status: MatchStatus
    missing_data: list[JsonValue] = Field(default_factory=list)


class MatchResultWrite(MatchResultFields):
    evaluations: list[EvaluationWrite] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_criteria(self):
        ids = [item.criterion_id for item in self.evaluations]
        if len(ids) != len(set(ids)):
            raise ValueError("Критерий повторяется в evaluations")
        return self


class QuestionCreate(Schema):
    criterion_id: UUID | None = None
    question: str = Field(min_length=1)
    expected_answer: str | None = None
    follow_up_hint: str | None = None
    position: int = Field(default=0, strict=True)


class ReportFields(Schema):
    criteria_version: int = Field(gt=0, strict=True)
    summary: str | None = None
    strengths: list[JsonValue] = Field(default_factory=list)
    structured_resume: dict[str, JsonValue] = Field(default_factory=dict)


class ReportWrite(ReportFields):
    questions: list[QuestionCreate] = Field(default_factory=list)


class CriterionRead(CriterionCreate):
    id: UUID
    vacancy_id: UUID
    created_at: datetime


class VacancyRead(Schema):
    id: UUID
    title: str
    status: VacancyStatus
    closed_at: datetime | None
    source_json: dict[str, JsonValue]
    criteria_version: int
    criteria: list[CriterionRead]
    created_at: datetime
    updated_at: datetime


class ResponseRead(ResponseCreate):
    id: UUID
    is_closed: bool
    closed_at: datetime | None
    closed_by_telegram_id: int | None
    hired_at: datetime | None
    hired_by_telegram_id: int | None
    recruiter_recommendation: dict[str, JsonValue] | None
    created_at: datetime
    updated_at: datetime


class TaskRead(TaskCreate):
    id: UUID
    status: TaskStatus
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None


class EvaluationRead(EvaluationWrite):
    id: UUID
    match_result_id: UUID


class MatchResultRead(MatchResultFields):
    id: UUID
    response_id: UUID
    evaluations: list[EvaluationRead]
    analyzed_at: datetime


class QuestionRead(QuestionCreate):
    id: UUID
    report_id: UUID


class ReportRead(ReportFields):
    id: UUID
    response_id: UUID
    questions: list[QuestionRead]
    created_at: datetime
    updated_at: datetime


class AnalysisFreshness(Schema):
    criteria_version: int
    match_criteria_version: int | None
    report_criteria_version: int | None
    match_is_stale: bool | None
    report_is_stale: bool | None
