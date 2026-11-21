import json
from pathlib import Path
import tempfile
import unittest
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from hr_api.index_pdf_reports import build_index
from hr_api.pdf_reports import INDEX_NAME, PdfEntry, PdfIndex, PdfReports, source_digest


class OfflinePdfTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.pdfs = PdfReports(self.root)
        self.source = {"candidate_id": 1, "source_text": "Original", "skills": ["SQL"]}
        self.vacancy = uuid5(NAMESPACE_URL, "hr-platform/vacancies/designer")
        self.response = uuid5(NAMESPACE_URL, "hr-platform/responses/designer/1")
        self.index = PdfIndex(source_key="designer", vacancy_title="Designer", entries=[
            PdfEntry(candidate_id="1", filename="report-1.pdf", source_sha256=source_digest(self.source))])
        self.save_index()
        (self.root / "report-1.pdf").write_bytes(b"%PDF-1.4\nSynthetic PDF\n%%EOF")

    def tearDown(self):
        self.directory.cleanup()

    def save_index(self):
        (self.root / INDEX_NAME).write_text(self.index.model_dump_json(), encoding="utf-8")

    def test_matching_requires_both_vacancy_and_response_and_detects_changes(self):
        info = self.pdfs.info(self.response, self.vacancy, self.source)
        assert info is not None
        self.assertFalse(info.is_stale)
        self.assertEqual(info.filename, "report-1.pdf")
        other_vacancy = uuid5(NAMESPACE_URL, "hr-platform/vacancies/frontend")
        other_response = uuid5(NAMESPACE_URL, "hr-platform/responses/frontend/1")
        self.assertIsNone(self.pdfs.path(self.response, other_vacancy))
        self.assertIsNone(self.pdfs.path(other_response, other_vacancy))
        edited = self.pdfs.info(self.response, self.vacancy, {**self.source, "source_text": "Edited"})
        assert edited is not None
        self.assertTrue(edited.is_stale)

    def test_json_key_order_does_not_make_pdf_stale(self):
        self.assertEqual(source_digest(self.source), source_digest(dict(reversed(list(self.source.items())))))

    def test_packaged_pdfs_are_scoped_to_the_imported_vacancy(self):
        from check_candidate import PROFILES

        applications = self.root / "parsed_responses"
        pdfs = PdfReports(self.root / "reports", applications=applications)
        for profile in ("designer", "frontend"):
            folder = self.root / f"reports_{profile}"
            folder.mkdir()
            (folder / f"Report_{profile}_1.pdf").write_bytes(b"%PDF-1.4\nSynthetic PDF\n%%EOF")
        originals = applications / PROFILES["designer"]
        originals.mkdir(parents=True)
        (originals / "отклик 1.json").write_text(json.dumps(self.source), encoding="utf-8")

        info = pdfs.info(self.response, self.vacancy, self.source)
        assert info is not None
        self.assertEqual(info.filename, "Report_designer_1.pdf")
        self.assertFalse(info.is_stale)
        edited = pdfs.info(self.response, self.vacancy, {**self.source, "source_text": "Edited"})
        assert edited is not None
        self.assertTrue(edited.is_stale)
        frontend_vacancy = uuid5(NAMESPACE_URL, "hr-platform/vacancies/frontend")
        frontend_response = uuid5(NAMESPACE_URL, "hr-platform/responses/frontend/1")
        self.assertIsNone(pdfs.path(self.response, frontend_vacancy))
        self.assertEqual(pdfs.path(frontend_response, frontend_vacancy),
                         self.root / "reports_frontend" / "Report_frontend_1.pdf")

    def test_missing_corrupt_index_or_file_never_breaks_report(self):
        path = self.root / "report-1.pdf"
        path.write_bytes(b"<html>not a PDF</html>")
        self.assertIsNone(self.pdfs.path(self.response, self.vacancy))
        path.unlink()
        self.assertIsNone(self.pdfs.path(self.response, self.vacancy))
        (self.root / INDEX_NAME).write_text("invalid JSON", encoding="utf-8")
        self.assertIsNone(self.pdfs.path(self.response, self.vacancy))
        (self.root / INDEX_NAME).unlink()
        self.assertIsNone(self.pdfs.path(self.response, self.vacancy))

    def test_index_rejects_traversal_and_duplicate_candidate_or_pdf(self):
        for filename in ("../report-1.pdf", "/tmp/report-1.pdf", "D:\\secret.pdf", "x.html", "folder/report-1.pdf"):
            with self.subTest(filename=filename), self.assertRaises(ValidationError):
                PdfEntry(candidate_id="1", filename=filename, source_sha256="a" * 64)
        with self.assertRaises(ValidationError):
            PdfIndex(source_key="designer", vacancy_title="X", entries=self.index.entries * 2)

    def test_builder_uses_ranking_and_original_json_and_validates_pdf(self):
        applications = self.root / "applications"
        applications.mkdir()
        (applications / "one.json").write_text(json.dumps(self.source), encoding="utf-8")
        (self.root / "ranking.json").write_text(json.dumps({"vacancy_title": "Designer", "applications": [
            {"application_id": "1", "report": "report-1.pdf"}]}), encoding="utf-8")
        index = build_index("designer", self.root, applications)
        self.assertEqual(index, self.index)
        (self.root / "report-1.pdf").write_bytes(b"invalid")
        with self.assertRaises(ValueError):
            build_index("designer", self.root, applications)


if __name__ == "__main__":
    unittest.main()
