"""Serve explicitly indexed offline reports, scoped to the imported vacancy/response."""
from __future__ import annotations

import hashlib
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contract_models import PdfReportInfo

DEFAULT_REPORTS = Path(__file__).resolve().parent.parent / "otchet_offline" / "reports"
INDEX_NAME = "platform-index.json"
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
    def __init__(self, root: Path = DEFAULT_REPORTS):
        self.root = root

    def _lookup(self, response_id: UUID, vacancy_id: UUID) -> tuple[Path, PdfEntry] | None:
        try:
            index = self.root / INDEX_NAME
            stat = index.stat()
            expected_vacancy, entries = _read_index(index.resolve(), stat.st_mtime_ns, stat.st_size)
            entry = entries.get(response_id)
            if expected_vacancy != vacancy_id or entry is None:
                return None
            path = (self.root / entry.filename).resolve()
            if not path.is_relative_to(self.root.resolve()) or not path.is_file():
                return None
            with path.open("rb") as source:
                if source.read(5) != b"%PDF-":
                    return None
            return path, entry
        except OSError:
            return None

    def info(self, response_id: UUID, vacancy_id: UUID, source: dict) -> PdfReportInfo | None:
        found = self._lookup(response_id, vacancy_id)
        if found is None:
            return None
        path, entry = found
        return PdfReportInfo(filename=path.name, is_stale=entry.source_sha256 != source_digest(source))

    def path(self, response_id: UUID, vacancy_id: UUID) -> Path | None:
        found = self._lookup(response_id, vacancy_id)
        return found[0] if found is not None else None
