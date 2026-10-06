"""GFM cell/header utilities shared by parsing and chunking.

Adapted from WeKnora's table rendering/header-tracking design; see
THIRD_PARTY_NOTICES.md. Escaped pipes must not change the number of columns.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

from app.errors import DocumentParseError


def cells(line: str) -> list[str]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    trailing = re.search(r"(\\*)\|$", value)
    if trailing and len(trailing.group(1)) % 2 == 0:
        value = value[:-1]
    result: list[str] = []
    current: list[str] = []
    backslashes = 0
    for char in value:
        if char == "|" and backslashes % 2 == 0:
            result.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        backslashes = backslashes + 1 if char == "\\" else 0
    result.append("".join(current).strip())
    return result


def is_separator(line: str) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells(line))


def is_table_start(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and "|" in lines[index]
        and is_separator(lines[index + 1])
        and len(cells(lines[index])) == len(cells(lines[index + 1]))
    )


def escape_cell(value: str) -> str:
    # A literal pipe is escaped once; an already escaped pipe stays escaped.
    return re.sub(r"(?<!\\)\|", r"\\|", re.sub(r"\s+", " ", value).strip())


def render(rows: list[list[str]], *, already_escaped: bool = False) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    if not width:
        return ""
    normalized = []
    for row in rows:
        values = row + [""] * (width - len(row))
        if not already_escaped:
            values = [escape_cell(value) for value in values]
        normalized.append("| " + " | ".join(values) + " |")
    separator = "| " + " | ".join("---" for _ in range(width)) + " |"
    return "\n".join([normalized[0], separator, *normalized[1:]])


class _SpanTable(HTMLParser):
    """One HTML table, bounded cells/spans, no script execution or image fetching."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, Any]]] = []
        self.row: list[dict[str, Any]] | None = None
        self.cell: dict[str, Any] | None = None
        self.depth = 0
        self.tables = 0
        self.skip = 0
        self.in_head = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "table":
            self.depth += 1
            self.tables += 1
            if self.depth > 1 or self.tables > 1:
                raise DocumentParseError("OCR 表格含嵌套/多个 table，需拆分后核对")
        elif tag == "thead":
            self.in_head = True
        elif tag == "tr" and self.depth:
            if self.row is not None:
                raise DocumentParseError("OCR 表格行标签不完整")
            self.row = []
        elif tag in {"th", "td"} and self.row is not None:
            if self.cell is not None:
                raise DocumentParseError("OCR 表格单元格标签不完整")
            attributes = dict(attrs)
            try:
                spans = [int(attributes.get(key) or "1") for key in ("rowspan", "colspan")]
            except ValueError as exc:
                raise DocumentParseError("OCR 表格 span 必须是正整数") from exc
            if any(not 1 <= span <= 256 for span in spans):
                raise DocumentParseError("OCR 表格 span 超过范围")
            self.cell = {
                "text": "",
                "rowspan": spans[0],
                "colspan": spans[1],
                "header": tag == "th" or self.in_head,
            }
        elif tag == "br" and self.cell is not None:
            self.cell["text"] += "\n"

    def handle_data(self, data: str) -> None:
        if self.cell is not None and not self.skip:
            self.cell["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in {"th", "td"} and self.cell is not None and self.row is not None:
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.cell is not None:
                raise DocumentParseError("OCR 表格单元格未关闭")
            self.rows.append(self.row)
            if len(self.rows) > 10000:
                raise DocumentParseError("OCR 表格行数超过上限")
            self.row = None
        elif tag == "thead":
            self.in_head = False
        elif tag == "table":
            self.depth = max(0, self.depth - 1)


def expand_html_table(value: str) -> dict[str, Any]:
    """Expand spans into a logical grid, retaining origin cells and header row mapping."""
    parser = _SpanTable()
    parser.feed(value)
    parser.close()
    if not parser.rows or parser.depth or parser.row is not None:
        raise DocumentParseError("OCR 表格为空或标签未闭合")
    occupied: dict[tuple[int, int], str] = {}
    spans: list[dict[str, Any]] = []
    for row_index, row in enumerate(parser.rows):
        column = 0
        for cell in row:
            while (row_index, column) in occupied:
                column += 1
            height, width = cell["rowspan"], cell["colspan"]
            if row_index + height > len(parser.rows):
                raise DocumentParseError("OCR 表格 rowspan 超出实际行数")
            if column + width > 256 or len(occupied) + width * height > 100000:
                raise DocumentParseError("OCR 表格展开超过安全上限")
            text = re.sub(r"\s+", " ", cell["text"]).strip()
            for r in range(row_index, row_index + height):
                for c in range(column, column + width):
                    if (r, c) in occupied:
                        raise DocumentParseError("OCR 表格合并范围重叠")
                    occupied[r, c] = text
            spans.append({"row": row_index, "column": column, **cell, "text": text})
            column += width
    width = max(column for _, column in occupied) + 1 if occupied else 0
    if not width:
        raise DocumentParseError("OCR 表格没有单元格")
    if width * len(parser.rows) > 100000:
        raise DocumentParseError("OCR 表格逻辑网格超过安全上限")
    grid = [
        [occupied.get((row, column), "") for column in range(width)]
        for row in range(len(parser.rows))
    ]
    headers = 0
    for row in parser.rows:
        if row and all(cell["header"] for cell in row):
            headers += 1
        else:
            break
    warnings = []
    if not headers:
        headers = 1
        warnings.append("OCR 表格未显式标记表头，首行按候选表头保留，请核对")
    header = []
    for column in range(width):
        labels = list(
            dict.fromkeys(grid[row][column] for row in range(headers) if grid[row][column])
        )
        header.append(" > ".join(labels))
    return {
        "text": render([header, *grid[headers:]]),
        "rows": grid,
        "spans": spans,
        "header_rows": headers,
        "normalized_row_to_source": list(range(headers, len(grid))),
        "original_html": value,
        "warnings": warnings,
    }
