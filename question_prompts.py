"""Compose common, domain and role prompts; read edits afresh on every run."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROMPT_ROOT = Path(__file__).resolve().parent / "prompts" / "questions"


@dataclass(frozen=True)
class PromptProfile:
    domain: str
    label: str


PROMPT_PROFILES = {
    "frontend": PromptProfile("it", "Frontend-разработчик"),
    "junior-python": PromptProfile("it", "Младший инженер по аналитике и данным"),
    "designer": PromptProfile("it", "UX/UI-дизайнер"),
    "python": PromptProfile("it", "Инженер по аналитике и большим данным"),
    "qa": PromptProfile("it", "Инженер по тестированию ПО"),
}


@dataclass(frozen=True)
class QuestionPrompts:
    profile: str
    domain: str
    generation: str
    review: str


def _read_prompt(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Не удалось прочитать промпт в UTF-8: {path}") from exc
    if not text:
        raise ValueError(f"Файл промпта пуст: {path}")
    return text


def build_prompts(profile: str = "general", *, root: Path = PROMPT_ROOT) -> QuestionPrompts:
    if profile != "general" and profile not in PROMPT_PROFILES:
        raise ValueError(f"Неизвестный профиль промпта: {profile}")
    generation = _read_prompt(root / "generation.txt")
    review = _read_prompt(root / "review.txt")
    if profile == "general":
        return QuestionPrompts(profile, "general", generation, review)
    specification = PROMPT_PROFILES[profile]
    domain = _read_prompt(root / specification.domain / "common.txt")
    role = _read_prompt(root / specification.domain / f"{profile}.txt")
    context = (f"ПРОФИЛЬ ОБЛАСТИ: {specification.domain}\n{domain}\n\n"
               f"ПРОФИЛЬ РОЛИ: {specification.label}\n{role}")
    return QuestionPrompts(profile, specification.domain,
                           generation + "\n\n" + context, review + "\n\n" + context)
