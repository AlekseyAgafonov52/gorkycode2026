"""Serve explicitly indexed offline reports, scoped to the imported vacancy/response."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from check_candidate import PROFILES
from contract_models import PdfReportInfo

DEFAULT_REPORTS = Path(__file__).resolve().parent.parent / "otchet_offline" / "reports"
DEFAULT_APPLICATIONS = DEFAULT_REPORTS.parent.parent / "parsed_responses"
INDEX_NAME = "platform-index.json"
PACKAGED_REPORTS = {
    uuid5(NAMESPACE_URL, f"hr-platform/vacancies/{profile}"): (profile, category)
    for profile, category in (("frontend", "frontend"), ("designer", "designer"), ("junior-python", "python"))
}
logger = logging.getLogger(__name__)


def source_digest(source: dict) -> str:
    data = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class PdfEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(pattern=r"^[1-9][0-9]*$", max_length=20)
    filename: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf$", max_length=120)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PdfIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    source_key: str = Field(min_length=1, max_length=100)
    vacancy_title: str = Field(min_length=1)
    entries: list[PdfEntry]

    @model_validator(mode="after")
    def unique_candidates_and_files(self):
        if len({entry.candidate_id for entry in self.entries}) != len(self.entries) or len({entry.filename.casefold() for entry in self.entries}) != len(self.entries):
            raise ValueError("Повторяющийся кандидат или PDF в индексе")
        return self


@lru_cache(maxsize=8)
def _read_index(path: Path, mtime: int, size: int) -> tuple[UUID | None, dict[UUID, PdfEntry]]:
    try:
        index = PdfIndex.model_validate_json(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        logger.warning("Offline PDF index is invalid or unreadable")
        return None, {}
    vacancy_id = uuid5(NAMESPACE_URL, "hr-platform/vacancies/" + index.source_key)
    entries = {uuid5(NAMESPACE_URL, f"hr-platform/responses/{index.source_key}/{entry.candidate_id}"): entry
               for entry in index.entries}
    return vacancy_id, entries


class PdfReports:
    def __init__(self, root: Path = DEFAULT_REPORTS, *, applications: Path | None = None):
        self.root = root
        self.applications = DEFAULT_APPLICATIONS if applications is None and root == DEFAULT_REPORTS else applications

    def _lookup(self, response_id: UUID, vacancy_id: UUID) -> tuple[Path, PdfEntry] | None:
        try:
            index = self.root / INDEX_NAME
            stat = index.stat()
            expected_vacancy, entries = _read_index(index.resolve(), stat.st_mtime_ns, stat.st_size)
            entry = entries.get(response_id)
            if expected_vacancy != vacancy_id or entry is None:
                return None
            path = _openable_pdf(self.root, entry.filename)
            return (path, entry) if path is not None else None
        except OSError:
            return None

    def _packaged(self, response_id: UUID, vacancy_id: UUID) -> tuple[Path, Path] | None:
        if self.applications is None or vacancy_id not in PACKAGED_REPORTS:
            return None
        profile, category = PACKAGED_REPORTS[vacancy_id]
        folder = self.root.parent / f"reports_{category}"
        try:
            entry = _packaged_files(folder, profile, category, folder.stat().st_mtime_ns).get(response_id)
            if entry is None:
                return None
            filename, candidate_id = entry
            path = _openable_pdf(folder, filename)
            original = self.applications / PROFILES[profile] / f"отклик {candidate_id}.json"
            return (path, original) if path is not None else None
        except OSError:
            return None

    def info(self, response_id: UUID, vacancy_id: UUID, source: dict) -> PdfReportInfo | None:
        found = self._lookup(response_id, vacancy_id)
        if found is not None:
            path, entry = found
            return PdfReportInfo(filename=path.name, is_stale=entry.source_sha256 != source_digest(source))
        found = self._packaged(response_id, vacancy_id)
        if found is None:
            return None
        path, original = found
        try:
            original_source = json.loads(original.read_text(encoding="utf-8-sig"))
            is_stale = source_digest(original_source) != source_digest(source)
        except (OSError, ValueError):
            is_stale = False
        return PdfReportInfo(filename=path.name, is_stale=is_stale)

    def path(self, response_id: UUID, vacancy_id: UUID) -> Path | None:
        found = self._lookup(response_id, vacancy_id) or self._packaged(response_id, vacancy_id)
        return found[0] if found is not None else None


def _openable_pdf(folder: Path, filename: str) -> Path | None:
    path = (folder / filename).resolve()
    if not path.is_relative_to(folder.resolve()) or not path.is_file():
        return None
    with path.open("rb") as pdf:
        return path if pdf.read(5) == b"%PDF-" else None


@lru_cache(maxsize=8)
def _packaged_files(folder: Path, profile: str, category: str, mtime: int) -> dict[UUID, tuple[str, str]]:
    pattern = re.compile(rf"Report_{category}_([1-9][0-9]{{0,19}})\.pdf")
    reports = {}
    for path in folder.iterdir():
        match = pattern.fullmatch(path.name)
        if match:
            candidate_id = match[1]
            response_id = uuid5(NAMESPACE_URL, f"hr-platform/responses/{profile}/{candidate_id}")
            reports[response_id] = (path.name, candidate_id)
    return reports
