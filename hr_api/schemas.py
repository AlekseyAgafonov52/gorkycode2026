from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, JsonValue, model_validator
from contract_models import ContractResponse

from storage.schemas import CriterionRead, Importance, Schema, TaskStatus, TaskType, VacancyStatus


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


class ResponseDetail(ContractResponse):
    vacancy_id: UUID
    source_json: dict[str, JsonValue]
    structured_resume: dict[str, JsonValue]
    criteria_version: int
    is_stale: bool
    is_archived: bool


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


class ArchivePatch(Schema):
    is_archived: bool = Field(strict=True)


class GenerateQuestions(Schema):
    force: bool = False
