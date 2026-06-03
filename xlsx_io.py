from __future__ import annotations

import re
import zipfile
from html import escape
from pathlib import Path
from xml.etree import ElementTree as ET


TASK_COLUMNS = [
    "id",
    "question",
    "platform",
    "round",
    "status",
    "screenshot_path",
    "answer_text_path",
    "answer_url",
    "url_text_path",
    "remark",
    "updated_at",
]

NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "officeRel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def read_tasks_xlsx(path: Path, sheet_name: str = "Tasks") -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Excel file not found: {path}")

    with zipfile.ZipFile(path) as zf:
        worksheet_path = _find_sheet_path(zf, sheet_name)
        shared_strings = _read_shared_strings(zf)
        root = ET.fromstring(zf.read(worksheet_path))

    rows: dict[int, dict[int, str]] = {}
    for row in root.findall(".//main:sheetData/main:row", NS):
        row_index = int(row.attrib.get("r", "0") or 0)
        values: dict[int, str] = {}
        for cell in row.findall("main:c", NS):
            ref = cell.attrib.get("r", "")
            col_index = _column_index(ref)
            if col_index:
                values[col_index] = _cell_text(cell, shared_strings)
        if row_index:
            rows[row_index] = values

    header_row = rows.get(1, {})
    header_map = {value.strip(): column for column, value in header_row.items() if value and value.strip()}
    missing = [column for column in ("id", "question", "platform", "round", "status") if column not in header_map]
    if missing:
        raise ValueError(f"Tasks sheet missing required columns: {', '.join(missing)}")

    tasks: list[dict] = []
    for row_index in sorted(index for index in rows if index > 1):
        row_values = rows[row_index]
        record = {"source_row": row_index}
        for column in TASK_COLUMNS:
            source_col = header_map.get(column)
            record[column] = row_values.get(source_col, "").strip() if source_col else ""
        if not record["question"]:
            continue
        record["platform"] = (record["platform"] or "deepseek").strip().lower()
        record["status"] = (record["status"] or "pending").strip().lower()
        record["round"] = _normalize_round(record["round"])
        tasks.append(record)
    return tasks


def write_tasks_xlsx(path: Path, tasks: list[dict], sheet_name: str = "Tasks") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [TASK_COLUMNS]
    for task in tasks:
        rows.append([str(task.get(column) or "") for column in TASK_COLUMNS])

    sheet_xml = _worksheet_xml(rows)
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font><sz val="11"/><name val="Arial"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        "</styleSheet>"
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/styles.xml", styles)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def _find_sheet_path(zf: zipfile.ZipFile, sheet_name: str) -> str:
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall("rel:Relationship", NS)}

    for sheet in workbook.findall(".//main:sheets/main:sheet", NS):
        if sheet.attrib.get("name") != sheet_name:
            continue
        rel_id = sheet.attrib.get(f"{{{NS['officeRel']}}}id")
        target = rel_map.get(rel_id or "")
        if not target:
            break
        target = target.lstrip("/")
        return target if target.startswith("xl/") else "xl/" + target
    raise ValueError(f"Sheet not found: {sheet_name}")


def _read_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    strings = []
    for item in root.findall("main:si", NS):
        parts = [node.text or "" for node in item.findall(".//main:t", NS)]
        strings.append("".join(parts))
    return strings


def _cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "s":
        value = cell.findtext("main:v", default="", namespaces=NS)
        try:
            return shared_strings[int(value)]
        except Exception:
            return ""
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//main:t", NS))
    return cell.findtext("main:v", default="", namespaces=NS) or ""


def _column_index(cell_ref: str) -> int:
    letters = re.sub(r"[^A-Z]", "", cell_ref.upper())
    total = 0
    for char in letters:
        total = total * 26 + (ord(char) - ord("A") + 1)
    return total


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def _normalize_round(value: str) -> str:
    try:
        return str(int(float(value or "1")))
    except Exception:
        return "1"


def _worksheet_xml(rows: list[list[str]]) -> str:
    row_xml = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for col_index, value in enumerate(row, start=1):
            ref = f"{_column_name(col_index)}{row_index}"
            safe_value = escape(str(value or ""))
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{safe_value}</t></is></c>')
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<dimension ref="A1:{_column_name(len(TASK_COLUMNS))}{max(1, len(rows))}"/>'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        '<sheetData>'
        + "".join(row_xml)
        + "</sheetData></worksheet>"
    )
