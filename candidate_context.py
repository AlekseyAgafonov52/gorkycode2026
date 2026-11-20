"""Extract only the skills array from a completed candidate JSON, offline."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from pydantic import JsonValue


def extract_candidate_skills(candidate: Mapping[str, JsonValue]) -> list[JsonValue]:
    """Copy skills verbatim, including levels, original wording and order.

    Missing/null skills become an empty array. Never infer skills from other
    fields or fall back to source_text. The input JSON is never modified.
    """
    skills = candidate.get("skills")
    if skills is None:
        return []
    if not isinstance(skills, list):
        raise ValueError("Поле skills в JSON кандидата должно быть массивом")
    return deepcopy(skills)
