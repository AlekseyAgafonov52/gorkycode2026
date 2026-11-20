"""The eight application tables from sql/hr_platform_schema.sql."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Identity:
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True,
                                   server_default=text("gen_random_uuid()"))


class Created:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Updated:
    # The supplied SQL has no timestamp trigger; ORM updates maintain this field.
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Vacancy(Identity, Created, Updated, Base):
    __tablename__ = "vacancies"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'active', 'closed')", name="vacancies_status_check"),
        CheckConstraint("criteria_version > 0", name="vacancies_criteria_version_check"),
    )
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), server_default=text("'active'"))
    source_json: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    criteria_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))

    criteria: Mapped[list[VacancyCriterion]] = relationship(back_populates="vacancy", cascade="all, delete-orphan", passive_deletes=True)
    responses: Mapped[list[Response]] = relationship(back_populates="vacancy", cascade="all, delete-orphan", passive_deletes=True)
    tasks: Mapped[list[AnalysisTask]] = relationship(back_populates="vacancy", cascade="all, delete-orphan", passive_deletes=True)


class VacancyCriterion(Identity, Created, Base):
    __tablename__ = "vacancy_criteria"
    __table_args__ = (
        CheckConstraint("importance IN ('required', 'preferred', 'irrelevant')", name="vacancy_criteria_importance_check"),
        Index("idx_criteria_vacancy", "vacancy_id"),
    )
    vacancy_id: Mapped[UUID] = mapped_column(ForeignKey("vacancies.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    importance: Mapped[str] = mapped_column(String(20), server_default=text("'preferred'"))
    description: Mapped[str | None] = mapped_column(Text)

    vacancy: Mapped[Vacancy] = relationship(back_populates="criteria")
    evaluations: Mapped[list[CriterionEvaluation]] = relationship(back_populates="criterion", cascade="all, delete-orphan", passive_deletes=True)
    questions: Mapped[list[Question]] = relationship(back_populates="criterion", passive_deletes=True)


class Response(Identity, Created, Updated, Base):
    __tablename__ = "responses"
    __table_args__ = (
        CheckConstraint("workflow_status IN ('new', 'reviewed', 'contacted')", name="responses_workflow_status_check"),
        Index("idx_responses_vacancy", "vacancy_id"),
        Index("idx_responses_status", "vacancy_id", "workflow_status", "is_archived"),
    )
    vacancy_id: Mapped[UUID] = mapped_column(ForeignKey("vacancies.id", ondelete="CASCADE"))
    candidate_name: Mapped[str | None] = mapped_column(String(255))
    source_json: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    workflow_status: Mapped[str] = mapped_column(String(20), server_default=text("'new'"))
    is_archived: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"))

    vacancy: Mapped[Vacancy] = relationship(back_populates="responses")
    tasks: Mapped[list[AnalysisTask]] = relationship(back_populates="response", cascade="all, delete-orphan", passive_deletes=True)
    match_result: Mapped[MatchResult | None] = relationship(back_populates="response", cascade="all, delete-orphan", passive_deletes=True)
    report: Mapped[Report | None] = relationship(back_populates="response", cascade="all, delete-orphan", passive_deletes=True)


class AnalysisTask(Identity, Created, Base):
    __tablename__ = "analysis_tasks"
    __table_args__ = (
        CheckConstraint("task_type IN ('matching', 'questions')", name="analysis_tasks_task_type_check"),
        CheckConstraint("status IN ('pending', 'processing', 'completed', 'failed')", name="analysis_tasks_status_check"),
        CheckConstraint("vacancy_id IS NOT NULL OR response_id IS NOT NULL", name="analysis_tasks_target_required"),
        Index("idx_tasks_status", "status"),
    )
    vacancy_id: Mapped[UUID | None] = mapped_column(ForeignKey("vacancies.id", ondelete="CASCADE"))
    response_id: Mapped[UUID | None] = mapped_column(ForeignKey("responses.id", ondelete="CASCADE"))
    task_type: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), server_default=text("'pending'"))
    error_message: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    vacancy: Mapped[Vacancy | None] = relationship(back_populates="tasks")
    response: Mapped[Response | None] = relationship(back_populates="tasks")


class MatchResult(Identity, Base):
    __tablename__ = "match_results"
    __table_args__ = (
        UniqueConstraint("response_id", name="match_results_response_id_key"),
        CheckConstraint("match_score BETWEEN 0 AND 100", name="match_results_match_score_check"),
        CheckConstraint("match_status IN ('green', 'yellow', 'red')", name="match_results_match_status_check"),
        CheckConstraint("criteria_version > 0", name="match_results_criteria_version_check"),
    )
    response_id: Mapped[UUID] = mapped_column(ForeignKey("responses.id", ondelete="CASCADE"))
    match_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    match_status: Mapped[str] = mapped_column(String(10))
    missing_data: Mapped[list[JsonValue]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    criteria_version: Mapped[int] = mapped_column(Integer)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    response: Mapped[Response] = relationship(back_populates="match_result")
    evaluations: Mapped[list[CriterionEvaluation]] = relationship(back_populates="match_result", cascade="all, delete-orphan", passive_deletes=True)


Index("idx_match_results_score", MatchResult.match_score.desc())


class CriterionEvaluation(Identity, Base):
    __tablename__ = "criterion_evaluations"
    __table_args__ = (
        UniqueConstraint("match_result_id", "criterion_id", name="criterion_evaluations_match_result_id_criterion_id_key"),
        CheckConstraint("status IN ('matched', 'partial', 'not_matched', 'unknown')", name="criterion_evaluations_status_check"),
        Index("idx_evaluations_match", "match_result_id"),
    )
    match_result_id: Mapped[UUID] = mapped_column(ForeignKey("match_results.id", ondelete="CASCADE"))
    criterion_id: Mapped[UUID] = mapped_column(ForeignKey("vacancy_criteria.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20))
    explanation: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list[JsonValue]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))

    match_result: Mapped[MatchResult] = relationship(back_populates="evaluations")
    criterion: Mapped[VacancyCriterion] = relationship(back_populates="evaluations")


class Report(Identity, Created, Updated, Base):
    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint("response_id", name="reports_response_id_key"),
        CheckConstraint("criteria_version > 0", name="reports_criteria_version_check"),
    )
    response_id: Mapped[UUID] = mapped_column(ForeignKey("responses.id", ondelete="CASCADE"))
    summary: Mapped[str | None] = mapped_column(Text)
    strengths: Mapped[list[JsonValue]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    structured_resume: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    criteria_version: Mapped[int] = mapped_column(Integer)

    response: Mapped[Response] = relationship(back_populates="report")
    questions: Mapped[list[Question]] = relationship(back_populates="report", cascade="all, delete-orphan",
                                                   passive_deletes=True, order_by="(Question.position, Question.id)")


class Question(Identity, Base):
    __tablename__ = "questions"
    __table_args__ = (Index("idx_questions_report", "report_id", "position"),)
    report_id: Mapped[UUID] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"))
    criterion_id: Mapped[UUID | None] = mapped_column(ForeignKey("vacancy_criteria.id", ondelete="SET NULL"))
    question: Mapped[str] = mapped_column(Text)
    expected_answer: Mapped[str | None] = mapped_column(Text)
    follow_up_hint: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    report: Mapped[Report] = relationship(back_populates="questions")
    criterion: Mapped[VacancyCriterion | None] = relationship(back_populates="questions")
