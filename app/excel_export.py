from __future__ import annotations

import math
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape

from sqlalchemy import inspect

from . import database


MAX_EXCEL_ROWS = 1_048_576
MAX_CELL_TEXT_LENGTH = 32_767
ILLEGAL_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass
class Worksheet:
    name: str
    headers: list[str]
    rows: list[list[Any]]


EXPORT_TABLES: tuple[tuple[str, str], ...] = (
    ("companies", "Unternehmen"),
    ("contracts", "Vertraege"),
    ("license_types", "Lizenzarten"),
    ("service_types", "Dienstleistungsarten"),
    ("flat_fee_types", "Pauschalarten"),
    ("licenses", "Lizenzen"),
    ("services", "Dienstleistungen"),
    ("variable_costs", "Variable Kosten"),
    ("flat_fees", "Pauschalen"),
    ("service_time_entries", "Zeitbuchungen"),
    ("invoices", "Rechnungen"),
    ("invoice_line_items", "Rechnungspositionen"),
    ("invoice_time_entries", "Rechnung Zeiten"),
    ("notifications", "Benachrichtigungen"),
    ("characteristic_definitions", "Merkmalsdefinitionen"),
    ("characteristic_values", "Merkmalswerte"),
    ("app_settings", "Einstellungen"),
    ("users", "Benutzer"),
    ("roles", "Rollen"),
    ("permissions", "Rechte"),
    ("role_permissions", "Rollen Rechte"),
)

REDACTED_COLUMNS = {
    "users": {"password_hash"},
}

ORDER_COLUMNS = {
    "app_settings": ("key",),
    "invoice_time_entries": ("invoice_id", "time_entry_id"),
    "role_permissions": ("role_id", "permission_id"),
}


def create_database_export_workbook(exported_by: str) -> tuple[bytes, str]:
    exported_at = datetime.now(timezone.utc)
    worksheets = [export_info_sheet(exported_by, exported_at)]
    worksheets.extend(export_table_sheets())
    filename = f"vertragsverwaltung-export-{exported_at.strftime('%Y%m%d-%H%M%S')}.xlsx"
    return build_xlsx(worksheets), filename


def export_info_sheet(exported_by: str, exported_at: datetime) -> Worksheet:
    rows = [
        ["Exportiert am", exported_at.isoformat(timespec="seconds")],
        ["Exportiert von", exported_by],
        ["Datenbank", database.engine.url.render_as_string(hide_password=True)],
        ["Hinweis", "Benutzer-Passwort-Hashes werden aus Sicherheitsgruenden nicht exportiert."],
        ["Dateiuploads", "PDFs und Logos werden nicht eingebettet; Dateinamen stehen in den jeweiligen Tabellen."],
    ]
    return Worksheet("Export Info", ["Feld", "Wert"], rows)


def export_table_sheets() -> list[Worksheet]:
    inspector = inspect(database.engine)
    existing_tables = set(inspector.get_table_names())
    worksheets: list[Worksheet] = []

    with database.connect() as connection:
        for table_name, sheet_name in EXPORT_TABLES:
            if table_name not in existing_tables:
                continue
            columns = [
                column["name"]
                for column in inspector.get_columns(table_name)
                if column["name"] not in REDACTED_COLUMNS.get(table_name, set())
            ]
            if not columns:
                worksheets.append(Worksheet(sheet_name, ["Hinweis"], [["Keine exportierbaren Spalten."]]))
                continue

            quoted_columns = ", ".join(quote_identifier(column) for column in columns)
            order_columns = [column for column in ORDER_COLUMNS.get(table_name, ("id",)) if column in columns]
            order_clause = ""
            if order_columns:
                order_clause = " ORDER BY " + ", ".join(quote_identifier(column) for column in order_columns)
            rows = connection.execute(
                f"SELECT {quoted_columns} FROM {quote_identifier(table_name)}{order_clause}"
            ).fetchall()
            exported_rows = [[row.get(column) for column in columns] for row in rows[: MAX_EXCEL_ROWS - 1]]
            if len(rows) >= MAX_EXCEL_ROWS:
                exported_rows.append(["Export gekuerzt: Excel-Zeilenlimit erreicht.", *[""] * (len(columns) - 1)])
            worksheets.append(Worksheet(sheet_name, columns, exported_rows))

    return worksheets


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def build_xlsx(worksheets: list[Worksheet]) -> bytes:
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", content_types_xml(len(worksheets)))
        workbook.writestr("_rels/.rels", package_relationships_xml())
        workbook.writestr("docProps/core.xml", core_properties_xml())
        workbook.writestr("docProps/app.xml", app_properties_xml())
        workbook.writestr("xl/workbook.xml", workbook_xml(worksheets))
        workbook.writestr("xl/_rels/workbook.xml.rels", workbook_relationships_xml(len(worksheets)))
        workbook.writestr("xl/styles.xml", styles_xml())
        for index, worksheet in enumerate(worksheets, start=1):
            workbook.writestr(f"xl/worksheets/sheet{index}.xml", worksheet_xml(worksheet))
    archive.seek(0)
    return archive.read()


def content_types_xml(sheet_count: int) -> str:
    sheet_overrides = "\n".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  {sheet_overrides}
</Types>"""


def package_relationships_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""


def workbook_relationships_xml(sheet_count: int) -> str:
    sheet_relationships = "\n".join(
        f'  <Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheet_count + 1)
    )
    styles_id = sheet_count + 1
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{sheet_relationships}
  <Relationship Id="rId{styles_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""


def workbook_xml(worksheets: list[Worksheet]) -> str:
    sheets = "\n".join(
        f'    <sheet name="{xml_text(safe_sheet_name(worksheet.name))}" sheetId="{index}" r:id="rId{index}"/>'
        for index, worksheet in enumerate(worksheets, start=1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
{sheets}
  </sheets>
</workbook>"""


def worksheet_xml(worksheet: Worksheet) -> str:
    all_rows = [worksheet.headers, *worksheet.rows]
    row_xml = "\n".join(row_to_xml(row, row_index, header=row_index == 1) for row_index, row in enumerate(all_rows, start=1))
    column_xml = columns_xml(all_rows)
    last_column = column_name(max(len(worksheet.headers), 1))
    last_row = max(len(all_rows), 1)
    autofilter = f'<autoFilter ref="A1:{last_column}{last_row}"/>' if worksheet.headers else ""
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetViews>
    <sheetView workbookViewId="0">
      <pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>
    </sheetView>
  </sheetViews>
  {column_xml}
  <sheetData>
{row_xml}
  </sheetData>
  {autofilter}
</worksheet>"""


def row_to_xml(row: list[Any], row_index: int, header: bool = False) -> str:
    cells = "".join(cell_xml(value, row_index, column_index, style=1 if header else 0) for column_index, value in enumerate(row, start=1))
    return f'    <row r="{row_index}">{cells}</row>'


def cell_xml(value: Any, row_index: int, column_index: int, style: int = 0) -> str:
    reference = f"{column_name(column_index)}{row_index}"
    style_attr = f' s="{style}"' if style else ""
    if value is None:
        return f'<c r="{reference}"{style_attr}/>'
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"{style_attr}><v>{1 if value else 0}</v></c>'
    if isinstance(value, int):
        return f'<c r="{reference}"{style_attr}><v>{value}</v></c>'
    if isinstance(value, (float, Decimal)):
        number = float(value)
        if math.isfinite(number):
            return f'<c r="{reference}"{style_attr}><v>{number}</v></c>'
    text = xml_text(value)
    return f'<c r="{reference}" t="inlineStr"{style_attr}><is><t xml:space="preserve">{text}</t></is></c>'


def columns_xml(rows: list[list[Any]]) -> str:
    max_columns = max((len(row) for row in rows), default=1)
    widths: list[int] = []
    for column_index in range(max_columns):
        width = 10
        for row in rows:
            if column_index < len(row):
                width = max(width, min(len(display_text(row[column_index])) + 2, 60))
        widths.append(width)
    columns = "\n".join(
        f'    <col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, start=1)
    )
    return f"<cols>\n{columns}\n  </cols>"


def styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2">
    <font><sz val="11"/><name val="Aptos"/></font>
    <font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Aptos"/></font>
  </fonts>
  <fills count="3">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF2017D8"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="2">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
  </cellXfs>
</styleSheet>"""


def core_properties_xml() -> str:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
                   xmlns:dc="http://purl.org/dc/elements/1.1/"
                   xmlns:dcterms="http://purl.org/dc/terms/"
                   xmlns:dcmitype="http://purl.org/dc/dcmitype/"
                   xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>Vertragsverwaltung Export</dc:title>
  <dc:creator>Vertragsverwaltung</dc:creator>
  <cp:lastModifiedBy>Vertragsverwaltung</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified>
</cp:coreProperties>"""


def app_properties_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
            xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Vertragsverwaltung</Application>
</Properties>"""


def safe_sheet_name(name: str) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", " ", name).strip() or "Tabelle"
    return cleaned[:31]


def column_name(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters or "A"


def display_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return text if len(text) <= MAX_CELL_TEXT_LENGTH else text[: MAX_CELL_TEXT_LENGTH - 3] + "..."


def xml_text(value: Any) -> str:
    text = display_text(value)
    text = ILLEGAL_XML_CHARS.sub("", text)
    return escape(text, {'"': "&quot;"})
