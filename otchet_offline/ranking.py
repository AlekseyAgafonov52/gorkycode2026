"""Локальная оценка и ранжирование JSON-откликов по вакансии."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from application_data import find_application_data
from professional_profile import ProfessionalProfile, build_profile, evaluate_profile, evaluate_title
from retrieval import LexicalIndex


GREEN_SCORE = 85
GREEN_COVERAGE = 80
WEIGHTS = {"required": 2, "preferred": 1, "condition": 0, "profile": 0, "interview": 0, "application": 0}
POINTS = {"met": 1, "partial": 0.5, "mismatch": 0}
PRIORITY_LABEL = {
    "required": "Обязательное", "preferred": "Желательное", "condition": "Условие вакансии",
    "profile": "Профессиональные сведения", "interview": "Для интервью", "application": "Данные отклика",
}
STATUS_LABEL = {"met": "Совпадает", "partial": "Частично", "mismatch": "Расхождение", "unknown": "Мало данных"}
STATUS_INK = {"met": "#14634D", "partial": "#79540A", "mismatch": "#79540A", "unknown": "#47566E"}
TRAFFIC = {
    "green": ("Зелёный · Подходит", "#14634D", "#E8F5EF"),
    "yellow": ("Жёлтый · Нужно уточнить", "#79540A", "#FFF4D9"),
}


class InputError(ValueError):
    """Данные не соответствуют входному формату."""


@dataclass(frozen=True)
class Term:
    name: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class Criterion:
    name: str
    priority: str
    kind: str = "skill"
    terms: tuple[Term, ...] = ()
    target: int | str | None = None
    match: str = "all"
    question: str = ""

    @property
    def weight(self) -> int:
        return WEIGHTS[self.priority]


@dataclass(frozen=True)
class Evidence:
    text: str
    path: str


@dataclass(frozen=True)
class Segment:
    text: str
    normalized: str
    path: str


@dataclass(frozen=True)
class ResumeText:
    jobs: list[dict]
    statements: list[Segment]
    passages: list[Segment]


@dataclass(frozen=True)
class Finding:
    criterion: Criterion
    status: str
    detail: str
    evidence: tuple[Evidence, ...] = ()
    question: str = ""
    related: Evidence | None = None


@dataclass(frozen=True)
class Vacancy:
    title: str
    organization: str
    location: str
    vacancy_id: str
    criteria: tuple[Criterion, ...]
    profile: ProfessionalProfile


@dataclass(frozen=True)
class Assessment:
    application_id: str
    resume_title: str
    findings: tuple[Finding, ...]
    score: float | None
    coverage: float
    confirmed: float | None
    color: str
    reason: str
    lexical_score: float = 0.0


def plain(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split())


def normalize(value: object) -> str:
    return plain(value).casefold().replace("ё", "е")


def identifier(value: object) -> str:
    return str(value) if type(value) is int else plain(value)


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as error:
        raise InputError(f"Не удалось прочитать {path.name} ({error}).") from error
    if not isinstance(data, dict):
        raise InputError(f"{path.name} должен содержать один объект JSON.")
    return data


def array(data: dict, key: str) -> list:
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise InputError(f"Поле {key} должно быть массивом.")
    return value


def strings(data: dict, key: str) -> list[str]:
    values = array(data, key)
    if any(not isinstance(value, str) for value in values):
        raise InputError(f"Поле {key} должно содержать строки.")
    return [plain(value) for value in values if plain(value)]


FORMAT_IDS = {
    "onsite": "onsite", "on-site": "onsite", "office": "onsite", "офис": "onsite",
    "офисный": "onsite", "на месте работодателя": "onsite",
    "remote": "remote", "удаленно": "remote", "удаленный": "remote",
    "hybrid": "hybrid", "гибрид": "hybrid", "гибридный": "hybrid",
}
FORMAT_NAMES = {"onsite": "Офис", "remote": "Удалённая работа", "hybrid": "Гибрид"}
URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
CONTACT = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}|(?<!\w)@[\w_]{3,}|(?<!\w)(?:\+?7|8)[ -]*\(?\d{3}\)?[ -]*(?:\d[ -]*){7}(?!\d)")
NEGATIVE_PREFIX = re.compile(r"(?:\bбез|\bнет опыта(?: работы)?(?: с)?|\bне (?:использовал[аи]?|работал[аи]? с|знаю|владею))\s*$")
NEGATIVE_SUFFIX = re.compile(r"^\s*[,–—:-]?\s*(?:не (?:знаю|использовал[аи]?|работал[аи]?)|только изучаю)\b")
OFFICE_REFUSAL = re.compile(
    r"\bне (?:готов[аы]? (?:работать|к работе)|могу работать|рассматриваю работу) в офисе\b"
    r"|\bне рассматриваю офисн\w* (?:работу|формат)\b"
    r"|\b(?:ищу|рассматриваю) (?:только|исключительно) (?:полностью )?удален\w*"
)
OFFICE_CONSENT = re.compile(
    r"(?<!не )\b(?:готов[аы]? (?:работать|к работе)|могу работать|соглас(?:ен|на) работать) в офисе\b"
    r"|(?<!не )\bсоглас(?:ен|на) на офисн\w* формат\b"
)
WORK_PREFERENCE = re.compile(r"\b(?:ищу|рассматриваю|предпочитаю|готов\w*)\b[^.!?]{0,45}\b(?:удален\w*|гибрид\w*|remote)\b")
REQUIREMENT_PREFIX = re.compile(
    r"^(?:(?:уверенн\w*|базов\w*|хорош\w*|практическ\w*) )?"
    r"(?:знание|владение|понимание|опыт работы|навыки работы)\s+(?:(?:в|с)\s+)?"
)


@lru_cache(maxsize=1)
def skill_rules() -> dict:
    return read_json(Path(__file__).with_name("skill_rules.json"))


def requirement_key(label: str) -> str:
    return normalize(label).strip(" .;,")


def term(name: str, aliases: list[str] | None = None) -> Term:
    rules = skill_rules()
    canonical = rules["names"].get(requirement_key(name), name)
    primary = rules["patterns"].get(canonical, re.escape(requirement_key(name)))
    alternatives = "|".join([primary, *(re.escape(requirement_key(alias)) for alias in (aliases or []))])
    return Term(name, re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)"))


def structured_criteria(rows: list) -> list[Criterion]:
    criteria = []
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise InputError(f"Критерий № {i} должен быть объектом JSON.")
        name = plain(row.get("name"))
        priority = row.get("priority", "required")
        match = row.get("match", "all")
        question = plain(row.get("question"))
        if not name or priority not in ("required", "preferred", "interview") or match not in ("all", "any"):
            raise InputError(f"Проверьте name, priority и match в критерии № {i}.")
        if priority == "interview":
            criteria.append(Criterion(name, priority, "interview", question=question))
            continue
        parts = array(row, "terms") if "terms" in row else [name]
        if not parts:
            raise InputError(f"Заполните terms в критерии № {i}.")
        terms = []
        for part in parts:
            term_name = plain(part.get("name")) if isinstance(part, dict) else plain(part)
            if not requirement_key(term_name):
                raise InputError(f"В критерии № {i} каждый элемент terms должен содержать название.")
            aliases = strings(part, "aliases") if isinstance(part, dict) else []
            if any(not requirement_key(alias) for alias in aliases):
                raise InputError(f"В критерии № {i} найдена пустая формулировка в aliases.")
            terms.append(term(term_name, aliases))
        criteria.append(Criterion(name, priority, terms=tuple(terms), match=match, question=question))
    return criteria


def legacy_criteria(data: dict) -> list[Criterion]:
    rules = skill_rules()
    original = data.get("original_requirements")
    source = " ".join(normalize(line) for line in strings(original, "required")) if isinstance(original, dict) else ""
    context = {rule["term"]: (rule["name"], rule["terms"]) for rule in rules["context_rules"]
               if re.search(rule["when"], source)}
    weighted = []
    interview = {}
    for key, priority in (("required_skills", "required"), ("additional_requirements", "required"),
                          ("preferred_skills", "preferred")):
        for label in strings(data, key):
            label = label.strip(" .;,")
            normalized = requirement_key(label)
            if not normalized:
                continue
            if normalized in rules["interview_only"]:
                interview[normalized] = Criterion(label, "interview", "interview", question=rules["interview_only"][normalized])
                continue
            short_name = REQUIREMENT_PREFIX.sub("", normalized)
            canonical = rules["names"].get(normalized) or rules["names"].get(short_name)
            if not canonical:
                # Для новых навыков снимаем обычные вводные слова; сложные условия задаются через criteria.
                canonical = label[len(normalized) - len(short_name):] or label
            name, parts = rules["groups"].get(normalized, context.get(canonical, (canonical, (canonical,))))
            weighted.append(Criterion(name, priority, terms=tuple(map(term, parts))))

    # Составные требования получают вес раньше отдельных тегов, которые в них уже входят.
    used = set()
    added = {}
    order = sorted(enumerate(weighted), key=lambda pair: (pair[1].priority == "preferred", -len(pair[1].terms)))
    for index, criterion in order:
        remaining = tuple(part for part in criterion.terms if requirement_key(part.name) not in used)
        if remaining:
            label = criterion.name if remaining == criterion.terms else ", ".join(part.name for part in remaining)
            added[index] = Criterion(label, criterion.priority, terms=remaining)
            used.update(requirement_key(part.name) for part in remaining)
    return [*(added[i] for i in sorted(added)), *interview.values()]


def format_id(value: object) -> str:
    return FORMAT_IDS.get(normalize(value), normalize(value))


def make_vacancy(data: dict) -> Vacancy:
    title = plain(data.get("title"))
    if not title:
        raise InputError("В вакансии нужно заполнить поле title.")

    profile = build_profile(data)
    criteria = [Criterion("Должность в резюме", "condition", "title"),
                Criterion("Профессиональный профиль", "profile", "profession")]
    years = data.get("experience_min_years")
    if years is not None and (type(years) not in (int, float) or not math.isfinite(years) or years < 0):
        raise InputError("Поле experience_min_years должно быть неотрицательным числом.")
    if years:
        months = math.ceil(years * 12)
        criteria.append(Criterion(f"Профильный опыт от {months} мес.", "required", "experience", target=months))

    education = plain(data.get("education"))
    if education:
        criteria.append(Criterion(f"Образование · {education}", "required", "education", (term(education),)))

    evaluated = structured_criteria(array(data, "criteria")) if data.get("criteria") is not None else legacy_criteria(data)
    criteria.extend(evaluated)

    mode = format_id(data.get("work_format"))
    if mode:
        criteria.insert(1, Criterion(f"Формат работы · {FORMAT_NAMES.get(mode, mode)}", "condition", "format", target=mode))
    return Vacancy(title, plain(data.get("organization")), plain(data.get("location")),
                   identifier(data.get("vacancy_id", data.get("id"))), tuple(criteria), profile)


def make_segments(value: object, path: str) -> list[Segment]:
    if not isinstance(value, str):
        return []
    segments = []
    for line in re.split(r"\n+|(?<=[.!?])\s+", value):
        clean = plain(CONTACT.sub("", URL.sub("", line)))
        if clean:
            segments.append(Segment(clean, normalize(clean), path))
    return segments


def excerpt(segment: Segment, start: int = 0) -> Evidence:
    if len(segment.text) <= 140:
        return Evidence(segment.text, segment.path)
    left = max(0, start - 24)
    right = min(len(segment.text), start + 115)
    text = segment.text[left:right].strip()
    return Evidence(("…" if left else "") + text + ("…" if right < len(segment.text) else ""), segment.path)


def find_term(required: Term, segments: list[Segment]) -> Evidence | None:
    for segment in segments:
        for matched in required.pattern.finditer(segment.normalized):
            before = segment.normalized[max(0, matched.start() - 52):matched.start()]
            after = segment.normalized[matched.end():matched.end() + 40]
            if not (NEGATIVE_PREFIX.search(before) or NEGATIVE_SUFFIX.search(after)):
                return excerpt(segment, matched.start())
    return None


def skill_finding(criterion: Criterion, segments: list[Segment]) -> Finding:
    matches = [(part.name, find_term(part, segments)) for part in criterion.terms]
    evidence = tuple(dict.fromkeys(proof for _, proof in matches if proof is not None))
    missing = [name for name, proof in matches if proof is None]
    if not missing or (criterion.match == "any" and evidence):
        return Finding(criterion, "met", "Сведения найдены в резюме.", evidence)

    if criterion.question:
        question = criterion.question
    elif criterion.kind == "education":
        question = f"Какое у вас образование? Уточните, соответствует ли оно требованию «{criterion.terms[0].name}»."
    elif criterion.match == "any":
        question = f"С какими вариантами из списка «{', '.join(missing)}» вы работали? Приведите пример задачи."
    elif len(criterion.terms) > 1:
        topics = ", ".join(missing)
        question = f"Можете привести пример практической задачи по теме «{topics}» и описать свою роль?"
    else:
        question = f"Есть ли у вас практический опыт по требованию «{missing[0]}»? Приведите пример задачи."
    if evidence:
        return Finding(criterion, "partial", f"Требуют уточнения {', '.join(missing)}.", evidence, question)
    return Finding(criterion, "unknown", "В отклике недостаточно сведений для проверки.", question=question)


def month(value: object) -> int | None:
    matched = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])(?:-\d{2})?", plain(value))
    if matched and int(matched[1]) > 0:
        return int(matched[1]) * 12 + int(matched[2]) - 1
    return None


def nonnegative_integer(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def count_months(intervals: list[tuple[int, int]]) -> int:
    total = 0
    end = 0
    for left, right in sorted(intervals):
        total += max(0, right - max(left, end))
        end = max(end, right)
    return total


def experience_finding(criterion: Criterion, vacancy: Vacancy, resume: dict, jobs: list[dict],
                       job_indices: tuple[int, ...]) -> Finding:
    minimum = int(criterion.target)
    question = f"Сколько месяцев у вас суммарно профильного опыта для позиции «{vacancy.title}»? Назовите проекты и периоды работы."
    similar = nonnegative_integer(resume.get("similar_experience_months"))
    if similar is not None:
        status = "met" if similar >= minimum else ("partial" if similar > 0 else "mismatch")
        evidence = (Evidence(f"Указано {similar} мес. профильного опыта.", "/similar_experience_months"),)
        return Finding(criterion, status, f"{similar} мес. при минимуме {minimum} мес.", evidence,
                       question if status != "met" else "")

    intervals = []
    undated = []
    sources = []
    for i in job_indices:
        job = jobs[i]
        position = plain(job.get("position"))
        start = month(job.get("start_date"))
        end = month(job.get("end_date"))
        duration = nonnegative_integer(job.get("duration_months"))
        if start is not None and end is not None and start <= end:
            duration = end - start + 1
        if not duration:
            continue
        if start is None:
            undated.append(duration)
        else:
            intervals.append((start, start + duration))
        sources.append(Evidence(f"{position} · {duration} мес.", f"/work_experience/{i}"))

    known = max(count_months(intervals), max(undated, default=0))
    if not known:
        return Finding(criterion, "unknown", "Не удалось вывести длительность профильного опыта из записей о работе.", question=question)
    status = "met" if known >= minimum else "partial"
    detail = f"По профильным позициям учтено {known} мес. при минимуме {minimum} мес. Пересечения дат учтены один раз."
    return Finding(criterion, status, detail, tuple(sources), question if status != "met" else "")


def project_segments(resume: dict) -> list[Segment]:
    segments = []
    for key in ("projects", "portfolio", "portfolio_url"):
        value = resume.get(key)
        records = enumerate(value) if isinstance(value, list) else [(None, value)]
        for index, record in records:
            path = f"/{key}" if index is None else f"/{key}/{index}"
            if isinstance(record, dict):
                for field in ("title", "name", "description"):
                    segments.extend(make_segments(record.get(field), f"{path}/{field}"))
                link = plain(record.get("url"))
            else:
                segments.extend(make_segments(record, path))
                link = plain(record)
            if key.startswith("portfolio") and URL.search(link):
                segments.append(Segment("В отклике указана ссылка на портфолио.", "портфолио", path))
    return segments


def format_finding(criterion: Criterion, vacancy: Vacancy, resume: dict, statements: list[Segment]) -> Finding:
    expected = criterion.target
    question = f"Вакансия предусматривает формат «{FORMAT_NAMES.get(expected, expected)}»"
    if vacancy.location and expected in {"onsite", "hybrid"}:
        question += f" ({vacancy.location})"
    question += ". Подходит ли вам этот формат?"
    accepted = {format_id(value) for value in strings(resume, "accepted_work_formats")}
    if accepted:
        values = ", ".join(FORMAT_NAMES.get(value, value) for value in sorted(accepted))
        evidence = (Evidence(f"Подходящие форматы по анкете — {values}.", "/accepted_work_formats"),)
        if expected in accepted:
            return Finding(criterion, "met", "Кандидат указал, что этот формат подходит.", evidence)
        return Finding(criterion, "mismatch", "Список подходящих кандидату форматов расходится с условием вакансии.", evidence, question)

    if expected == "onsite":
        refusals = [excerpt(item, match.start()) for item in statements if (match := OFFICE_REFUSAL.search(item.normalized))]
        confirmations = [excerpt(item, match.start()) for item in statements
                         if not OFFICE_REFUSAL.search(item.normalized) and (match := OFFICE_CONSENT.search(item.normalized))]
        if refusals and confirmations:
            return Finding(criterion, "unknown", "В отклике есть разные сведения о готовности работать в офисе.",
                           (refusals[0], confirmations[0]), question)
        if refusals:
            return Finding(criterion, "mismatch", "Кандидат явно указал, что офисный формат ему не подходит.",
                           (refusals[0],), question)
        if confirmations:
            return Finding(criterion, "met", "В отклике указана готовность работать в офисе.", (confirmations[0],))
        for item in statements:
            if match := WORK_PREFERENCE.search(item.normalized):
                return Finding(criterion, "unknown", "Кандидат упомянул другой формат. Готовность к офису требует уточнения.",
                               (excerpt(item, match.start()),), question)
    return Finding(criterion, "unknown", "Готовность к указанному формату требует уточнения.", question=question)


def application_data_finding(resume: dict) -> Finding:
    criterion = Criterion("ФИО и контакты", "application", "application_data")
    name_paths, contact_path = find_application_data(resume)
    evidence = tuple(Evidence("Поле содержит часть ФИО." if len(name_paths) > 1 else "ФИО указано.", path)
                     for path in name_paths)
    if contact_path:
        evidence += (Evidence("Указан способ связи.", contact_path),)
    if name_paths and contact_path:
        return Finding(criterion, "met", "ФИО и контактные данные указаны.", evidence)
    if name_paths:
        return Finding(criterion, "partial", "ФИО указано. Контактные данные требуют уточнения.", evidence,
                       "Как с вами удобнее связаться?")
    if contact_path:
        return Finding(criterion, "partial", "Контактные данные указаны. ФИО требует уточнения.", evidence,
                       "Уточните, пожалуйста, ваши имя и фамилию.")
    return Finding(criterion, "unknown", "В JSON отклика отсутствуют ФИО и контактные данные.",
                   question="Уточните, пожалуйста, ваши имя, фамилию и удобный способ связи.")


def prepare_resume_text(resume: dict) -> ResumeText:
    jobs = array(resume, "work_experience")
    if any(not isinstance(job, dict) for job in jobs):
        raise InputError("Поле work_experience должно содержать объекты с описаниями позиций.")
    segments = []
    for i, job in enumerate(jobs):
        for key in ("description", "position"):
            segments.extend(make_segments(job.get(key), f"/work_experience/{i}/{key}"))
    for i, skill in enumerate(array(resume, "skills")):
        if isinstance(skill, dict):
            segments.extend(make_segments(skill.get("name"), f"/skills/{i}/name"))
        else:
            segments.extend(make_segments(skill, f"/skills/{i}"))

    statements = [item for key in ("cover_letter", "about_me") for item in make_segments(resume.get(key), f"/{key}")]
    segments.extend(statements)
    segments.extend(project_segments(resume))
    return ResumeText(jobs, statements, segments)


def assess(vacancy: Vacancy, resume: dict, fallback_id: str, *, prepared: ResumeText | None = None) -> Assessment:
    vacancy_id = identifier(resume.get("vacancy_id"))
    if vacancy.vacancy_id and vacancy_id and vacancy_id != vacancy.vacancy_id:
        raise InputError(f"Отклик относится к другой вакансии ({vacancy_id}).")

    prepared = prepare_resume_text(resume) if prepared is None else prepared
    jobs, statements, segments = prepared.jobs, prepared.statements, prepared.passages
    title = evaluate_title(vacancy.profile, resume)
    profile = evaluate_profile(vacancy.profile, resume)
    application_data = application_data_finding(resume)
    findings = [application_data]
    for criterion in vacancy.criteria:
        if criterion.kind == "title":
            evidence = tuple(Evidence(text, path) for text, path in title.evidence)
            result = Finding(criterion, title.status, title.detail, evidence, title.question)
        elif criterion.kind == "format":
            result = format_finding(criterion, vacancy, resume, statements)
        elif criterion.kind == "profession":
            evidence = tuple(Evidence(text, path) for text, path in profile.evidence)
            result = Finding(criterion, profile.status, profile.detail, evidence, profile.question)
        elif criterion.kind == "experience":
            result = experience_finding(criterion, vacancy, resume, jobs, profile.job_indices)
        elif criterion.kind == "interview":
            question = criterion.question or f"Приведите пример, по которому можно оценить требование «{criterion.name}»."
            result = Finding(criterion, "unknown", "Критерий для обсуждения на интервью.", question=question)
        elif criterion.kind == "education":
            education = make_segments(resume.get("education_level"), "/education_level")
            for i, entry in enumerate(array(resume, "education")):
                if isinstance(entry, dict):
                    education.extend(make_segments(entry.get("level"), f"/education/{i}/level"))
                    for j, detail in enumerate(array(entry, "details")):
                        education.extend(make_segments(detail, f"/education/{i}/details/{j}"))
            result = skill_finding(criterion, education)
        else:
            result = skill_finding(criterion, segments)
        findings.append(result)

    total_weight = sum(item.criterion.weight for item in findings)
    known_weight = sum(item.criterion.weight for item in findings if item.status in POINTS)
    earned = sum(item.criterion.weight * POINTS.get(item.status, 0) for item in findings)
    score = 100 * earned / known_weight if known_weight else None
    coverage = 100 * known_weight / total_weight if total_weight else 0.0
    confirmed = 100 * earned / total_weight if total_weight else None
    required = [item for item in findings if item.criterion.priority == "required"]
    if profile.status == "mismatch" and required and all(item.status == "met" for item in required):
        index = next(i for i, item in enumerate(findings) if item.criterion.kind == "profession")
        role = findings[index]
        proof = next((source for item in required for source in item.evidence), None)
        findings[index] = Finding(role.criterion, "partial",
                                  "Должности относятся к другой профессии. По обязательным требованиям найдены совпадения, которые стоит обсудить.",
                                  role.evidence[:1] + ((proof,) if proof else ()), role.question)

    unresolved = [item for item in findings if item.criterion.priority in ("required", "condition", "profile")
                  and item.status != "met" and (item.criterion.kind not in ("title", "profession")
                                                or vacancy.profile.title_aliases)]
    if not unresolved and score is not None and score >= GREEN_SCORE and coverage >= GREEN_COVERAGE:
        color, reason = "green", "Обязательные требования выполнены по сведениям из отклика."
    elif unresolved:
        names = ", ".join(item.criterion.name for item in unresolved[:3])
        rest = f" и ещё {len(unresolved) - 3}" if len(unresolved) > 3 else ""
        color, reason = "yellow", f"Уточните обязательные требования — {names}{rest}."
    elif not total_weight:
        color, reason = "yellow", "Для рейтинга требуются оцениваемые критерии вакансии."
    elif coverage < GREEN_COVERAGE:
        color, reason = "yellow", "Покрытие профессиональных требований ниже порога 80%."
    else:
        color, reason = "yellow", "Балл соответствия ниже порога 85%."

    application_id = identifier(resume.get("application_id")) or identifier(resume.get("candidate_id")) or fallback_id
    return Assessment(application_id, plain(resume.get("resume_title")), tuple(findings),
                      score, coverage, confirmed, color, reason)


def ranking_key(assessment: Assessment) -> tuple[int, float, float, float, str]:
    color_order = {"green": 0, "yellow": 1}
    score = assessment.confirmed if assessment.confirmed is not None else -1
    return color_order[assessment.color], -score, -assessment.lexical_score, -assessment.coverage, assessment.application_id.casefold()


def add_lexical_evidence(assessment: Assessment, index: LexicalIndex, position: int,
                         passages: list[Segment], requirements: list[str]) -> Assessment:
    findings = []
    for item in assessment.findings:
        if item.criterion.kind == "skill" and item.status in ("partial", "unknown"):
            query = " ".join(term.name for term in item.criterion.terms)
            for match in index.search(position, query, limit=3):
                evidence = excerpt(passages[match.index])
                if match.score >= 0.2 and evidence not in item.evidence:
                    item = replace(item, related=evidence)
                    break
        findings.append(item)
    return replace(assessment, findings=tuple(findings), lexical_score=index.score_application(position, requirements))


def rank_applications(vacancy: Vacancy, vacancy_data: dict,
                      entries: list[tuple[Path, Assessment, list[Segment]]]) -> list[tuple[Path, Assessment]]:
    """Сопоставить фрагменты откликов с текстом вакансии и выдать стабильный порядок."""
    index = LexicalIndex([[segment.text for segment in passages] for _, _, passages in entries])
    requirements = [term.name for criterion in vacancy.criteria if criterion.kind == "skill" for term in criterion.terms]
    duties = vacancy_data.get("responsibilities")
    if isinstance(duties, list):
        requirements.extend(plain(item) for item in duties)
    else:
        requirements.append(plain(duties))
    requirements.extend([vacancy.title, plain(vacancy_data.get("description"))])
    ranked = [
        (application, add_lexical_evidence(assessment, index, position, passages, requirements))
        for position, (application, assessment, passages) in enumerate(entries)
    ]
    ranked.sort(key=lambda item: (ranking_key(item[1]), item[0].name.casefold(), str(item[0])))
    return ranked
