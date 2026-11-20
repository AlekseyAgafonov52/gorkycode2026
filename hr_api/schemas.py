from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from storage.schemas import CriterionRead, Importance, MatchStatus, Schema, TaskStatus, TaskType, VacancyStatus, WorkflowStatus


class VacancySummary(Schema):
    id: UUID
    title: str
    status: VacancyStatus
    responses_count: int


class VacancyDetail(VacancySummary):
    source_json: dict[str, JsonValue]
    criteria_version: int
    criteria: list[CriterionRead]


class CriterionChange(Schema):
    id: UUID
    importance: Importance
    name: str | None = None


class CriteriaPatch(Schema):
    criteria: list[CriterionChange] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({item.id for item in self.criteria}) != len(self.criteria):
            raise ValueError("Идентификатор критерия повторяется")
        return self


class CriteriaResult(Schema):
    vacancy_id: UUID
    criteria_version: int
    criteria: list[CriterionRead]
    changed: bool
    task_id: UUID | None


class ResponseSummary(Schema):
    id: UUID
    candidate_name: str | None
    match_score: float | None = Field(ge=0, le=100)
    match_status: MatchStatus | None
    missing_data_count: int | None
    workflow_status: WorkflowStatus
    is_archived: bool
    is_stale: bool


class CriterionAnalysis(Schema):
    criterion_id: UUID
    name: str
    importance: Importance
    status: Literal["matched", "partial", "not_matched", "unknown"]
    evidence: list[JsonValue]
    explanation: str | None


class QuestionView(Schema):
    id: UUID
    criterion_id: UUID | None
    requirement: str | None
    question: str
    expected_answer: str | None
    follow_up_hint: str | None
    position: int


ReportState = Literal["not_ready", "pending", "processing", "partial", "ready", "stale", "failed"]


class ReportView(Schema):
    report_id: UUID | None
    response_id: UUID
    state: ReportState
    candidate_name: str | None
    match_score: float | None
    match_status: MatchStatus | None
    summary: str | None
    strengths: list[JsonValue]
    criteria_analysis: list[CriterionAnalysis]
    missing_data: list[JsonValue]
    questions: list[QuestionView]
    source: dict[str, JsonValue]
    criteria_version: int
    is_stale: bool


class ResponseDetail(ResponseSummary):
    vacancy_id: UUID
    source_json: dict[str, JsonValue]
    structured_resume: dict[str, JsonValue]
    criteria_analysis: list[CriterionAnalysis]
    missing_data: list[JsonValue]
    report_id: UUID | None
    report_state: ReportState
    criteria_version: int


class TaskView(Schema):
    task_id: UUID
    task_type: TaskType
    vacancy_id: UUID | None
    response_id: UUID | None
    status: TaskStatus
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None
    is_stub: bool
    message: str | None
    reused: bool = False


class StatusPatch(Schema):
    workflow_status: WorkflowStatus


class ArchivePatch(Schema):
    is_archived: bool = Field(strict=True)


class GenerateQuestions(Schema):
    force: bool = False
