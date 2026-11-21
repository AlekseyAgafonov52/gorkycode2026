"""Render the saved offline report as selectable HTML."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from html import escape
from itertools import groupby, pairwise, takewhile
from pathlib import Path

import pymupdf


@dataclass(frozen=True)
class Line:
    text: str
    x: float
    y: float
    size: float
    color: int
    bold: bool


def _lines(page: pymupdf.Page) -> list[Line]:
    lines = []
    for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]:
        for line in block["lines"]:
            spans = line["spans"]
            first = spans[0]
            text = "".join(span["text"] for span in spans).replace("\x00", "")
            lines.append(Line(text, *first["origin"], first["size"], first["color"],
                              bool(first["flags"] & pymupdf.TEXT_FONT_BOLD)))
    return lines


def _paragraphs(lines: list[Line], *, headings: bool = False) -> str:
    paragraphs = []
    for (size, color, bold), group in groupby(lines, key=lambda line: (line.size, line.color, line.bold)):
        previous_y = None
        chunks: list[list[str]] = []
        for line in group:
            if previous_y is None or not 0 < line.y - previous_y <= 1.4 * size:
                chunks.append([])
            chunks[-1].append(escape(line.text))
            previous_y = line.y
        tag = "p"
        if headings and bold:
            if size >= 20:
                tag = "h1"
            elif size >= 11.5:
                tag = "h2"
            elif size >= 10:
                tag = "h3"
        weight = 700 if bold else 400
        style = f'font-size:{size * 1.5:.2f}px;color:#{color:06x};font-weight:{weight}'
        paragraphs.extend(f'<{tag} style="{style}">{" ".join(chunk)}</{tag}>' for chunk in chunks)
    return "".join(paragraphs)


def _columns(lines: list[Line], starts: list[float]) -> list[list[Line]]:
    boundaries = [(left + right) / 2 for left, right in pairwise(starts)]
    columns: list[list[Line]] = [[] for _ in starts]
    for line in lines:
        columns[bisect_right(boundaries, line.x)].append(line)
    return columns


def _summary(lines: list[Line], fills: list[dict]) -> str:
    starts = sorted({round(line.x, 1) for line in lines})
    if len(starts) != 4:
        return _paragraphs(lines)
    cards = _columns(lines, starts)
    background = "#" + "".join(f"{round(channel * 255):02x}" for channel in fills[0]["fill"])
    return (
        '<section class="summary" aria-label="Сводка по оценке">'
        f'<div style="background:{background}">{_paragraphs(cards[0])}</div>'
        + "".join(f"<div>{_paragraphs(card)}</div>" for card in cards[1:])
        + "</section>"
    )


def _take_rows(fills: list[dict], start: int) -> list[dict]:
    header = fills[start]["rect"]
    return list(takewhile(
        lambda fill: abs(fill["rect"].x0 - header.x0) < 0.1 and abs(fill["rect"].x1 - header.x1) < 0.1,
        fills[start:],
    ))


def _table(lines: list[Line], fills: list[dict]) -> str:
    columns = sorted(line.x for line in lines if line.y < fills[0]["rect"].y1)
    if len(columns) != 3:
        return _paragraphs(lines)
    rows: list[list[Line]] = [[] for _ in fills]
    row_ends = [fill["rect"].y1 for fill in fills[:-1]]
    for line in lines:
        rows[bisect_right(row_ends, line.y)].append(line)
    header, *body = [_columns(row, columns) for row in rows]
    return (
        '<table aria-label="Проверка требований"><thead><tr>'
        + "".join(f'<th scope="col">{_paragraphs(cell)}</th>' for cell in header)
        + "</tr></thead><tbody>"
        + "".join("<tr>" + "".join(f"<td>{_paragraphs(cell)}</td>" for cell in row) + "</tr>" for row in body)
        + "</tbody></table>"
    )


def _page(page: pymupdf.Page) -> str:
    # ReportLab paints the footer first and adds a filled rectangle for each table row.
    lines = _lines(page)
    footer_at = page.rect.height - 31
    footer = [line for line in lines if line.y > footer_at]
    remaining = [line for line in lines if line.y <= footer_at]
    fills = [drawing for drawing in page.get_drawings() if drawing["type"] == "f"]
    parts = ['<section class="report-sheet">']
    if len(fills) >= 2:
        left, right = (fill["rect"] for fill in fills[:2])
        if abs(left.x1 - right.x0) < 0.1 and abs(left.y0 - right.y0) < 0.1 and abs(left.y1 - right.y1) < 0.1:
            parts.append(_paragraphs([line for line in remaining if line.y < left.y0], headings=True))
            parts.append(_summary([line for line in remaining if left.y0 <= line.y < left.y1], fills))
            remaining = [line for line in remaining if line.y >= left.y1]
    for index, fill in enumerate(fills):
        rect = fill["rect"]
        if rect.width > page.rect.width * 0.8 and any(
            line.text == "Критерий" and rect.y0 <= line.y < rect.y1 for line in remaining
        ):
            rows = _take_rows(fills, index)
            bottom = rows[-1]["rect"].y1
            parts.append(_paragraphs([line for line in remaining if line.y < rect.y0], headings=True))
            parts.append(_table([line for line in remaining if rect.y0 <= line.y < bottom], rows))
            remaining = [line for line in remaining if line.y >= bottom]
            break
    parts.append(_paragraphs(remaining, headings=True))
    parts.append(f"<footer>{_paragraphs(footer)}</footer></section>")
    return "".join(parts)


STYLE = """
*, *::before, *::after { box-sizing: border-box; }
body { margin: 0; background: #f0f4f9; color: #1a2941; font: 16px/1.45 Arial, sans-serif; }
main { max-width: 1000px; margin: auto; padding: 16px; }
nav { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 10px; margin-bottom: 16px; }
nav a { color: #1a2941; background: white; border: 1px solid #b8c7d9; border-radius: 5px; padding: 8px 14px; }
.report-sheet { background: white; border: 1px solid #d9e2ec; margin-bottom: 16px; padding: clamp(16px, 4vw, 50px); }
h1, h2, h3, p { margin: 0 0 .45em; overflow-wrap: anywhere; }
h2, h3 { margin-top: 1em; }
.summary { display: grid; grid-template-columns: 2.9fr repeat(3, 1fr); gap: 2px; margin: 1em 0; }
.summary div { background: #f0f4f9; padding: 10px; min-width: 0; }
table { border-collapse: collapse; width: 100%; table-layout: fixed; }
th, td { text-align: left; vertical-align: top; padding: 8px; }
th:nth-child(1) { width: 30%; }
th:nth-child(2) { width: 17%; }
thead { background: #1a2941; }
tbody tr:nth-child(even) { background: #f6f8fb; }
footer { display: flex; justify-content: space-between; border-top: 1px solid #d9e2ec; margin-top: 25px; padding-top: 6px; }
.stale { background: #fff4d9; margin-bottom: 16px; padding: 12px; }
@media (max-width: 620px) {
    main { padding: 8px; }
    .summary { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .summary div:first-child { grid-column: 1 / -1; }
    th, td { padding: 5px; }
    th:nth-child(2) { width: 25%; }
}
@media print {
    body { background: white; print-color-adjust: exact; }
    nav { display: none; }
    main, .report-sheet { padding: 0; border: 0; }
    tr { break-inside: avoid; }
    .report-sheet + .report-sheet { break-before: page; }
}
"""


def render_report_html(path: Path, *, back_url: str, is_stale: bool) -> str:
    with pymupdf.open(path) as document:
        title = escape(document.metadata.get("title") or "Офлайн-отчёт")
        pages = "".join(_page(page) for page in document)
    stale = ('<p class="stale" role="status">Текст резюме изменён после создания отчёта. '
             'Здесь сохранена предыдущая версия.</p>') if is_stale else ""
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title><style>{STYLE}</style></head><body><main>"
        f'<nav aria-label="Действия с отчётом"><a href="{escape(back_url)}">К отклику</a>'
        '<a href="pdf?download=true">Скачать PDF</a></nav>'
        f"{stale}<article>{pages}</article></main></body></html>"
    )
