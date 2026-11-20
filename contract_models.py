"""Public MVP contracts, shared by parsers, matching, question pools and API."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator


Color = Literal["green", "yellow", "red"]
Level = Literal["advanced", "intermediate", "basic"]
Workflow = Literal["new", "forwarded", "not_forwarded"]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class ErrorDetail(ContractModel):
    code: Literal["not_found", "validation_error", "internal_error", "conflict"]
    message: str


class ErrorEnvelope(ContractModel):
    error: ErrorDetail


class Contacts(ContractModel):
    phone: str | None = None
    email: str | None = None
    telegram: str | None = None
    other: list[str] = Field(default_factory=list)

    @field_validator("phone", "email", "telegram")
    @classmethod
    def blank_is_missing(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class ResumeSkill(ContractModel):
    name: str
    level: Level | None = None


class ResumeData(ContractModel):
    candidate_name: str | None = None
    contacts: Contacts = Field(default_factory=Contacts)
    title: str | None = None
    experience_months: int | None = Field(default=None, ge=0)
    skills: list[ResumeSkill] = Field(default_factory=list)
    education: str | None = None
    about: str | None = None
    _source_text: str = PrivateAttr(default="")
    _similar_experience_months: int | None = PrivateAttr(default=None)

    @field_validator("candidate_name")
    @classmethod
    def blank_is_missing(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class DraftQuestion(ContractModel):
    kind: Literal["missing", "stack"]
    question: str = Field(min_length=1)
    expected_answer: str | None = None
    follow_up_hint: str | None = None
    repeat_of: str | None = None

    @model_validator(mode="after")
    def stack_answer_required(self):
        if self.kind == "stack" and not (self.expected_answer or "").strip():
            raise ValueError("У вопроса stack обязателен expected_answer")
        return self


class InterviewQuestion(DraftQuestion):
    id: str


class PoolQuestion(ContractModel):
    skill: str = Field(min_length=1)
    question: str = Field(min_length=1)
    expected_answer: str = Field(min_length=1)
    follow_up_hint: str | None = None
    repeat_of: str | None = None


class SkillMatch(ContractModel):
    skill: str
    resume_level: Level | None
    relevance: Color
    explanation: str
    questions: list[DraftQuestion] = Field(default_factory=list)


class SkillReport(ContractModel):
    skill: str
    resume_level: Level | None
    relevance: Color
    explanation: str
    questions: list[InterviewQuestion] = Field(default_factory=list)


class MissingField(ContractModel):
    field: str
    text: str


class MissingData(MissingField):
    id: str


class MatchResult(ContractModel):
    match_status: Color
    match_share: float = Field(ge=0, le=1)
    summary: tuple[str, str, str]
    skills: list[SkillMatch]
    missing_data: list[MissingField]


class RecruiterRecommendation(ContractModel):
    strengths: str = Field(min_length=1, max_length=800, strict=True)
    weaknesses: str = Field(min_length=1, max_length=800, strict=True)
    interview_focus: str = Field(min_length=1, max_length=800, strict=True)

    @field_validator("strengths", "weaknesses", "interview_focus")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("Заполните рекомендацию рекрутера")
        return value.strip()


class HiredCandidate(ContractModel):
    id: str
    candidate_name: str | None
    hired_at: datetime


class ContractReport(ContractModel):
    id: str
    vacancy_id: str
    candidate_name: str | None
    contacts: Contacts
    match_status: Color
    match_label: str
    match_share: float = Field(ge=0, le=1)
    summary: tuple[str, str, str]
    skills: list[SkillReport]
    missing_data: list[MissingData]
    ai_text_level: Literal["low", "medium", "high"] | None = None
    short_summary: str | None = None
    workflow_status: Workflow
    is_viewed: bool = Field(default=False, strict=True)
    is_closed: bool = Field(default=False, strict=True)
    closed_at: datetime | None = None
    hired_at: datetime | None = None
    recruiter_recommendation: RecruiterRecommendation | None = None
    source_text: str


class ContractResponse(ContractModel):
    id: str
    candidate_name: str | None
    match_status: Color
    match_share: float = Field(ge=0, le=1)
    short_summary: str | None = None
    workflow_status: Workflow
    is_viewed: bool = Field(default=False, strict=True)
    is_closed: bool = Field(default=False, strict=True)
    closed_at: datetime | None = None
    hired_at: datetime | None = None


class ContractVacancy(ContractModel):
    id: str
    title: str
    responses_count: int
    status: Literal["draft", "active", "closed"] = "active"
    hired_candidates: list[HiredCandidate] = Field(default_factory=list)


class WorkflowUpdate(ContractModel):
    workflow_status: Workflow
    recruiter_recommendation: RecruiterRecommendation | None = None


class WorkflowResult(ContractModel):
    id: str
    workflow_status: Workflow


class RecommendationUpdate(ContractModel):
    recruiter_recommendation: RecruiterRecommendation


class RecommendationResult(RecommendationUpdate):
    id: str


class VacancyStatusUpdate(ContractModel):
    status: Literal["active", "closed"]


class ViewedUpdate(ContractModel):
    is_viewed: bool = Field(strict=True)


class ViewedResult(ViewedUpdate):
    id: str


MAX_SOURCE_TEXT_CHARS = 2 * 1024 * 1024


class SourceTextUpdate(ContractModel):
    source_text: str = Field(strict=True, min_length=1, max_length=MAX_SOURCE_TEXT_CHARS)
    expected_source_text: str | None = Field(default=None, strict=True, max_length=MAX_SOURCE_TEXT_CHARS)

    @field_validator("source_text")
    @classmethod
    def valid_text(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("Текст резюме должен быть непустым и не содержать нулевых символов")
        return value
