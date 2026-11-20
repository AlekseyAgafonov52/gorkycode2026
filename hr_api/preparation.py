"""Prepare and persist contract reports on import or an explicit background job."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from candidate_analysis import GREEN_THRESHOLD, canonical_skill, extract_vacancy_skills, match, match_status_from_share, resume_from_json
from contract_models import ContractReport, DraftQuestion, PoolQuestion
from question_pool import PoolBuilder, build_question_pool, pick_questions, pool_hash, validate_pool
from storage.crud import Repository, StaleCriteriaError
from storage.models import Response, Vacancy
from storage.schemas import EvaluationWrite, MatchResultWrite, QuestionCreate, ReportWrite

LABELS = {"green": "Подходит", "yellow": "Частично подходит"}
PREPARATION_VERSION = 2


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def vacancy_input(vacancy: Vacancy) -> tuple[str, list[str]]:
    source = vacancy.source_json
    raw = source.get("source_text")
    text_value = raw if isinstance(raw, str) and raw.strip() else json.dumps(source, ensure_ascii=False)
    if isinstance(raw, str) and raw.strip():
        skills = extract_vacancy_skills(raw)
    else:
        skills = []
        for field in ("required_skills", "preferred_skills"):
            values = source.get(field)
            if isinstance(values, list):
                skills.extend(canonical_skill(value) for value in values if isinstance(value, str))
        if not skills:
            skills = [canonical_skill(c.name) for c in vacancy.criteria if c.importance != "irrelevant"]
    excluded = {canonical_skill(c.name).casefold() for c in vacancy.criteria if c.importance == "irrelevant"}
    return text_value, list(dict.fromkeys(skill for skill in skills if skill.casefold() not in excluded))


class ReportPreparer:
    def __init__(self, factory: sessionmaker[Session], pool_builder: PoolBuilder | None = None):
        self.factory = factory
        self.pool_builder = pool_builder or build_question_pool

    def prepare_vacancy(self, identifier: UUID, *, response_id: UUID | None = None) -> int:
        # Session-level advisory lock also protects simultaneous CLI imports. No
        # candidate data is sent to the model, and no response row is locked here.
        with self.factory() as lock_session:
            lock_session.execute(text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"),
                                 {"key": "hr-question-pool/" + str(identifier)})
            try:
                pool, expected_hash = self._pool(identifier)
                with self.factory.begin() as session:
                    query = select(Response.id).where(Response.vacancy_id == identifier)
                    if response_id is not None:
                        query = query.where(Response.id == response_id)
                    identifiers = list(session.scalars(query))
                return sum(self._prepare_response(item, pool, expected_hash) for item in identifiers)
            finally:
                lock_session.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                                     {"key": "hr-question-pool/" + str(identifier)})
                lock_session.commit()

    def _pool(self, identifier: UUID) -> tuple[list[PoolQuestion], str]:
        with self.factory.begin() as session:
            vacancy = Repository(session).get_vacancy(identifier)
            vacancy_text, skills = vacancy_input(vacancy)
            expected_hash = pool_hash(vacancy_text, skills)
            if vacancy.question_pool_hash == expected_hash:
                pool = [PoolQuestion.model_validate(item) for item in vacancy.question_pool]
                return validate_pool(pool, skills), expected_hash
        pool = validate_pool(self.pool_builder(vacancy_text, skills), skills) if skills else []
        with self.factory.begin() as session:
            vacancy = Repository(session)._lock_vacancy(identifier)
            current_text, current_skills = vacancy_input(vacancy)
            if pool_hash(current_text, current_skills) != expected_hash:
                raise StaleCriteriaError("Вакансия изменилась во время генерации пула")
            vacancy.question_pool = [item.model_dump(mode="json") for item in pool]
            vacancy.question_pool_hash = expected_hash
        return pool, expected_hash

    def _prepare_response(self, identifier: UUID, pool: list[PoolQuestion], expected_pool_hash: str) -> bool:
        with self.factory.begin() as session:
            return self.prepare_response_in_session(session, identifier, pool, expected_pool_hash)

    def prepare_response_in_session(self, session: Session, identifier: UUID, pool: list[PoolQuestion],
                                    expected_pool_hash: str) -> bool:
        repo = Repository(session)
        response, vacancy = repo._lock_response_vacancy(identifier)
        vacancy_text, skills = vacancy_input(vacancy)
        if pool_hash(vacancy_text, skills) != expected_pool_hash or vacancy.question_pool_hash != expected_pool_hash:
            raise StaleCriteriaError("Пул вопросов устарел")
        inputs = [GREEN_THRESHOLD, response.source_json, response.candidate_name, skills,
                  vacancy.criteria_version, [item.model_dump() for item in pool]]
        prepared_hash = fingerprint([PREPARATION_VERSION, *inputs])
        existing = repo.get_report(identifier)
        if existing is not None and existing.prepared_hash == prepared_hash and existing.contract_json is not None:
            return False
        # Version 2 only removes the identity/contact gate. Preserve selected
        # questions and their IDs when all other preparation inputs match v1.
        if (PREPARATION_VERSION == 2 and existing is not None and existing.contract_json is not None
                and existing.prepared_hash == fingerprint([1, *inputs])):
            saved_match = repo.get_match_result(identifier)
            saved_share = existing.contract_json.get("match_share")
            if (saved_match is not None and saved_match.criteria_version == vacancy.criteria_version
                    and isinstance(saved_share, (int, float)) and not isinstance(saved_share, bool)):
                status = match_status_from_share(saved_share)
                existing.contract_json = {**existing.contract_json, "match_status": status, "match_label": LABELS[status]}
                existing.prepared_hash = prepared_hash
                saved_match.match_status = status
                return True
        resume = resume_from_json(response.source_json)
        if response.candidate_name:
            resume.candidate_name = response.candidate_name
        result = match(resume, skills)
        selected = pick_questions(pool, resume, None)
        for skill in result.skills:
            if skill.relevance != "red":
                skill.questions = [DraftQuestion(kind="stack", question=item.question,
                    expected_answer=item.expected_answer) for item in selected if item.skill == skill.skill]
                if not skill.questions:
                    raise ValueError("В пуле нет вопроса для найденного навыка")
        criteria = {canonical_skill(c.name).casefold(): c.id for c in vacancy.criteria}
        evaluations = [EvaluationWrite.model_validate({"criterion_id": criteria[row.skill.casefold()],
            "status": {"green": "matched", "yellow": "partial", "red": "not_matched"}[row.relevance],
            "explanation": row.explanation}) for row in result.skills if row.skill.casefold() in criteria]
        repo.save_match_result(identifier, MatchResultWrite(criteria_version=vacancy.criteria_version,
            match_score=Decimal(str(result.match_share * 100)).quantize(Decimal("0.01")),
            match_status=result.match_status, missing_data=[item.model_dump() for item in result.missing_data],
            evaluations=evaluations))
        questions = []
        for skill in result.skills:
            for question in skill.questions:
                questions.append(QuestionCreate(criterion_id=criteria.get(skill.skill.casefold()),
                    question=question.question, expected_answer=question.expected_answer, position=len(questions)))
        for item in result.missing_data:
            questions.append(QuestionCreate(question=item.text, position=len(questions)))
        report = repo.save_report(identifier, ReportWrite(criteria_version=vacancy.criteria_version,
            summary="\n".join(result.summary), structured_resume=resume.model_dump(mode="json"), questions=questions))
        ids = iter(str(item.id) for item in sorted(report.questions, key=lambda q: q.position))
        rows = []
        for skill in result.skills:
            rows.append({**skill.model_dump(mode="json"), "questions": [
                {**question.model_dump(mode="json"), "id": next(ids)} for question in skill.questions]})
        contract = ContractReport.model_validate({"id": str(identifier), "vacancy_id": str(vacancy.id),
            "candidate_name": resume.candidate_name, "contacts": resume.contacts,
            "match_status": result.match_status, "match_label": LABELS[result.match_status],
            "match_share": result.match_share, "summary": result.summary, "skills": rows,
            "missing_data": [{**item.model_dump(), "id": next(ids)} for item in result.missing_data],
            "workflow_status": response.workflow_status, "is_viewed": response.is_viewed, "source_text": resume._source_text})
        report.contract_json = contract.model_dump(mode="json", exclude={"source_text", "workflow_status", "is_viewed", "is_closed", "closed_at", "hired_at", "recruiter_recommendation", "pdf_report"})
        report.prepared_hash = prepared_hash
        return True
