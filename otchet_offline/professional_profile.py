"""Evidence-based comparison of a vacancy's role and an application profile."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class RoleDefinition:
    id: str
    title_aliases: tuple[str, ...]
    related_titles: tuple[str, ...]
    signals: tuple[str, ...]


@dataclass(frozen=True)
class ProfessionalProfile:
    title: str
    family_ids: tuple[str, ...]
    title_aliases: tuple[str, ...]
    related_titles: tuple[str, ...]
    signals: tuple[str, ...]
    can_detect_mismatch: bool


@dataclass(frozen=True)
class ProfileFinding:
    status: str
    detail: str
    evidence: tuple[tuple[str, str], ...] = ()
    question: str = ""
    job_indices: tuple[int, ...] = ()


GRADE_WORDS = frozenset("junior jr middle senior sr intern junior-level trainee lead principal младший старший ведущий главный стажер стажировка".split())
GENERAL_TITLE_WORDS = frozenset("a an of and the по и из для или в на работа должность специалист сотрудник работник менеджер эксперт консультант помощник ассистент руководитель разработчик оператор инженер специалиста вакансии developer consultant specialist manager officer assistant associate engineer coordinator employee operator vacancy position".split())
URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
CONTACT = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}|(?<!\w)@[\w_]{3,}|(?<!\w)\+?\d[\d(). -]{8,}\d(?!\w)")
NEGATION_BEFORE = re.compile(r"(?:\b(?:нет|без) (?:опыта (?:работы )?(?:в |с |по )?)?|\bне (?:(?:знаю|владею|использу\w*|использова\w*|работа\w*|занима\w*) )?(?:(?:в|с|по) )?|\bno (?:experience (?:with |in )?)?)$")
NEGATION_AFTER = re.compile(r"^ (?:не (?:знаю|владею|использу\w*|использова\w*|работа\w*)|только изучаю)\b")
EMPTY_PORTFOLIO = frozenset({"", "no", "none", "null", "n a", "нет", "отсутствует", "нету", "не указано"})
TITLE_PLACEHOLDERS = frozenset({
    "без опыта", "без опыта работы", "в поиске работы", "должность не указана", "ищу работу", "кандидат",
    "не заполнено", "не указано", "резюме", "соискатель", "студент", "студентка", "выпускник", "выпускница",
    "candidate", "job seeker", "open to work", "student", "graduate", "not specified", "n a", "none",
})
PORTFOLIO_LABEL = re.compile(r"\b(?:мое портфолио|ссылка на портфолио|my portfolio|portfolio link)\b")
PORTFOLIO_LINK = re.compile(r"(?:портфолио|portfolio)\s*[:—-]?\s*(?:https?://|www\.)[^\s]+", re.IGNORECASE)


def _text(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()) if isinstance(value, str) else ""


def _normalize(text: str) -> str:
    return re.sub(r"[^\w+#]+|_+", " ", text.casefold().replace("ё", "е")).strip()


def _read_names(mapping: dict, key: str) -> tuple[str, ...]:
    value = mapping.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not _normalize(_text(item)) for item in value):
        raise ValueError(f"Поле role_profile.{key} должно содержать массив непустых строк.")
    return tuple(_text(item) for item in value)


@lru_cache(maxsize=256)
def _patterns(names: tuple[str, ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple((name, re.compile(rf"(?<![\w+#]){re.escape(_normalize(name))}(?![\w+#])"))
                 for name in names if _normalize(name))


def _match(names: tuple[str, ...], normalized: str) -> str | None:
    for name, pattern in _patterns(names):
        for candidate in pattern.finditer(normalized):
            before = normalized[max(0, candidate.start() - 65):candidate.start()]
            after = normalized[candidate.end():candidate.end() + 40]
            if not NEGATION_BEFORE.search(before) and not NEGATION_AFTER.search(after):
                return name
    return None


@lru_cache(maxsize=1)
def _catalog() -> tuple[RoleDefinition, ...]:
    path = Path(__file__).with_name("professional_profiles.json")
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Не удалось прочитать справочник профессиональных профилей {path.name}.") from exc
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise ValueError(f"Справочник {path.name} должен содержать массив профилей.")
    definitions = tuple(RoleDefinition(_text(record.get("id")), _read_names(record, "title_aliases"),
                                       _read_names(record, "related_titles"), _read_names(record, "signals"))
                        for record in records)
    if any(not item.id or not item.title_aliases for item in definitions):
        raise ValueError(f"У каждого профиля в {path.name} должны быть id и title_aliases.")
    if len({item.id for item in definitions}) != len(definitions):
        raise ValueError(f"В справочнике {path.name} повторяются id.")
    return definitions


def _informative_title(title: str) -> tuple[str, ...]:
    text = re.sub(r"\([^)]*\)", " ", title)
    words = [word for word in _normalize(text).split() if word not in GRADE_WORDS]
    if any(len(word) >= 3 and word not in GENERAL_TITLE_WORDS for word in words):
        return (" ".join(words),)
    return ()


def build_profile(vacancy: dict) -> ProfessionalProfile:
    """Use a named category, title recognition, or a vacancy-specific role profile."""
    title = _text(vacancy.get("title"))
    configured = vacancy.get("role_profile", {})
    if not isinstance(configured, dict):
        raise ValueError("Поле role_profile должно быть объектом.")
    if "id" in configured and (not isinstance(configured["id"], str) or not _text(configured["id"])):
        raise ValueError("Поле role_profile.id должно содержать непустую строку.")
    selected = _text(configured.get("id"))
    catalog = _catalog()
    if selected:
        roles = tuple(item for item in catalog if item.id == selected)
        if not roles:
            raise ValueError(f"В справочнике отсутствует профессиональный профиль «{selected}».")
    else:
        roles = tuple(item for item in catalog if _match(item.title_aliases, _normalize(title)))

    explicit_aliases = _read_names(configured, "title_aliases")
    explicit_signals = _read_names(configured, "signals")
    aliases = explicit_aliases + tuple(name for item in roles for name in item.title_aliases) + _informative_title(title)
    adjacent = _read_names(configured, "related_titles") + tuple(name for item in roles for name in item.related_titles)
    signals = explicit_signals + tuple(name for item in roles for name in item.signals)
    strong_profile = len(roles) == 1 or bool(explicit_aliases and explicit_signals)
    return ProfessionalProfile(title, tuple(item.id for item in roles), tuple(dict.fromkeys(aliases)),
                               tuple(dict.fromkeys(adjacent)), tuple(dict.fromkeys(signals)), strong_profile)


def _safe_title(value: str) -> str:
    cleaned = _text(CONTACT.sub("", URL.sub("", value)))
    return (cleaned[:117] + "…") if len(cleaned) > 118 else cleaned


@lru_cache(maxsize=512)
def _title_terms(value: str) -> frozenset[str]:
    return frozenset(word for word in _normalize(value).split() if word not in GRADE_WORDS)


def evaluate_title(profile: ProfessionalProfile, resume: dict) -> ProfileFinding:
    title = _text(CONTACT.sub("", URL.sub("", _text(resume.get("resume_title")))))
    headline = _safe_title(title)
    normalized = _normalize(title)
    question = f"На какую должность вы претендуете? Целевая роль — «{profile.title}»."
    evidence = ((headline, "/resume_title"),) if headline else ()
    if not normalized or normalized in TITLE_PLACEHOLDERS:
        return ProfileFinding("unknown", "В заголовке резюме недостаточно сведений о должности.", evidence, question)
    if not profile.title_aliases:
        return ProfileFinding("unknown", "Для сравнения нужно уточнить название должности в вакансии.", evidence, question)
    terms = _title_terms(title)
    alias_terms = tuple(_title_terms(alias) for alias in profile.title_aliases)
    if _match(profile.title_aliases, normalized) or (terms and terms in alias_terms):
        return ProfileFinding("met", "Указанная должность соответствует одному из названий целевой роли.", evidence)
    if _match(profile.related_titles, normalized):
        return ProfileFinding("partial", "В резюме указана смежная должность. Профиль задач следует уточнить.", evidence, question)
    if not _informative_title(title):
        return ProfileFinding("unknown", "В заголовке резюме недостаточно сведений о должности.", evidence, question)
    # Полное вхождение короткого названия показывает общую профессию.
    # Дополнительные слова в самом резюме могут обозначать другую специализацию.
    if any(terms < alias for alias in alias_terms):
        question = f"С какими проектами или задачами по направлению «{profile.title}» вы работали?"
        return ProfileFinding("partial", "В резюме указано общее название профессии. Нужно уточнить специализацию по вакансии.",
                              evidence, question)
    return ProfileFinding("mismatch", f"В резюме указана должность «{headline}». Целевая роль — «{profile.title}».",
                          evidence, question)


def _string_fields(value: object, path: str) -> Iterator[tuple[str, str]]:
    """Read strings inside explicitly selected sections, preserving source paths."""
    if isinstance(value, str):
        yield value, path
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _string_fields(item, f"{path}/{index}")
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {"name", "title", "description", "specialty", "specialization", "faculty", "details", "skills", "tools", "course", "text"}:
                yield from _string_fields(item, f"{path}/{key}")


def _signal_evidence(names: tuple[str, ...], fields: Iterable[tuple[str, str]]) -> tuple[str, str] | None:
    if names:
        for value, path in fields:
            for signal_text in re.split(r"[\n;!?]+|(?<=\.)\s+", value):
                match = _match(names, _normalize(_text(URL.sub("", signal_text))))
                if match:
                    return f"Профессиональное свидетельство «{match}».", path
    return None


def _portfolio_evidence(resume: dict) -> tuple[str, str] | None:
    for key in ("portfolio", "portfolio_url", "portfolio_urls", "portfolio_links"):
        value = resume.get(key)
        for item in value if isinstance(value, list) else (value,):
            if isinstance(item, dict):
                item = item.get("url") or item.get("link") or item.get("title")
            text = _normalize(_text(item))
            if text not in EMPTY_PORTFOLIO and not text.startswith(("нет ", "без ", "отсутствует ")):
                return "В отклике указано портфолио для проверки.", f"/{key}"
    for key in ("about_me", "cover_letter"):
        text = _text(resume.get(key))
        if PORTFOLIO_LINK.search(text) or PORTFOLIO_LABEL.search(_normalize(text)):
            return "В тексте отклика упомянуто портфолио для проверки.", f"/{key}"
    return None


def evaluate_profile(profile: ProfessionalProfile, resume: dict) -> ProfileFinding:
    """Assess role evidence without using absent data as evidence of mismatch."""
    question = f"Расскажите о практических задачах, проектах или обучении по направлению «{profile.title}»."
    headline = _text(resume.get("resume_title"))
    normalized_headline = _normalize(headline)
    exact_headline = _match(profile.title_aliases, normalized_headline)
    jobs = resume.get("work_experience")
    jobs = jobs if isinstance(jobs, list) else []
    exact_jobs = []
    adjacent_jobs = []
    positions = []
    job_signals = []
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            continue
        title = _text(job.get("position"))
        normalized = _normalize(title)
        evidence = (_safe_title(title), f"/work_experience/{index}/position")
        if normalized:
            positions.append((normalized, evidence))
        if _match(profile.title_aliases, normalized):
            exact_jobs.append((index, evidence))
            continue
        signal = _signal_evidence(profile.signals, _string_fields(job.get("description"), f"/work_experience/{index}/description"))
        if signal:
            job_signals.append(signal)
        if _match(profile.related_titles, normalized):
            if signal:
                adjacent_jobs.append((index, (evidence, signal)))
            else:
                adjacent_jobs.append((None, (evidence,)))

    identified_adjacent_indices = tuple(index for index, _ in adjacent_jobs if index is not None)
    if exact_jobs:
        indices = tuple(sorted([index for index, _ in exact_jobs] + list(identified_adjacent_indices)))
        return ProfileFinding("met", "Профиль подтверждается должностями из опыта работы.",
                              tuple(proof for _, proof in exact_jobs[:3]), job_indices=indices)
    if exact_headline and identified_adjacent_indices:
        proof = next(proofs for index, proofs in adjacent_jobs if index is not None)
        return ProfileFinding("met", "Профиль резюме подтверждается смежной должностью и описанием профильных задач.",
                              ((_safe_title(headline), "/resume_title"), *proof), job_indices=identified_adjacent_indices)

    secondary_fields = ("skills", "projects", "project", "case_studies", "courses", "certifications",
                        "education", "education_level", "about_me", "cover_letter")
    resume_signal = _signal_evidence(profile.signals, (item for key in secondary_fields for item in _string_fields(resume.get(key), f"/{key}")))
    portfolio = _portfolio_evidence(resume)
    headline_matches = exact_headline or _match(profile.related_titles, normalized_headline)
    evidence = ([(_safe_title(headline), "/resume_title")] if headline_matches else [])
    evidence.extend(proof for _, proofs in adjacent_jobs[:2] for proof in proofs)
    evidence.extend(item for item in (resume_signal, portfolio, *job_signals[:1]) if item)
    evidence = tuple(dict.fromkeys(evidence))[:4]
    if evidence:
        return ProfileFinding("partial", "В отклике есть признаки профильных задач или перехода в нужную профессию.",
                              evidence, question, identified_adjacent_indices)

    if profile.can_detect_mismatch and normalized_headline:
        headline_roles = [item for item in _catalog() if _match(item.title_aliases, normalized_headline)]
        if len(headline_roles) == 1 and headline_roles[0].id not in profile.family_ids:
            unrelated = tuple(proof for normalized, proof in positions if _match(headline_roles[0].title_aliases, normalized))
            if unrelated:
                return ProfileFinding("mismatch", f"Заголовок резюме и должность в опыте указывают на другую профессию. Целевая роль — «{profile.title}».",
                                      ((_safe_title(headline), "/resume_title"), *unrelated[:2]), question)

    return ProfileFinding("unknown", "В отклике недостаточно сведений для проверки профессионального профиля.", question=question)
