from __future__ import annotations

import csv
import io
import zipfile
from decimal import Decimal
from html import escape as html_escape


def csv_safe_cell(value: object) -> str:
    text = "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def build_expenses_csv(rows: list[dict[str, object]]) -> str:
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(
        ["Дата", "Список", "Категория", "Название", "Сумма", "Учитывать в аналитике", "Учитывать в прогнозе"]
    )
    for row in rows:
        writer.writerow(
            [
                row["display_date"],
                csv_safe_cell(row["list_title"]),
                csv_safe_cell(row["category"]),
                csv_safe_cell(row["title"]),
                str(row["amount"]),
                "да" if row["include_in_analytics"] else "нет",
                "да" if row["include_in_forecast"] else "нет",
            ]
        )
    return output.getvalue()


def _excel_column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _excel_text(value: object) -> str:
    text = "" if value is None else str(value)
    clean = "".join(char for char in text if char in "\t\n\r" or ord(char) >= 32)
    return html_escape(clean, quote=False)


def build_expenses_xlsx(rows: list[dict[str, object]]) -> bytes:
    headers = [
        "Дата",
        "Список",
        "Категория",
        "Название",
        "Сумма",
        "Владелец списка",
        "Учитывать в аналитике",
        "Учитывать в прогнозе",
    ]
    sheet_rows = [headers] + [
        [
            row["date"],
            row["list_title"],
            row["category"],
            row["title"],
            row["amount"],
            row["owner"],
            "да" if row["include_in_analytics"] else "нет",
            "да" if row["include_in_forecast"] else "нет",
        ]
        for row in rows
    ]
    row_xml = []
    for row_index, values in enumerate(sheet_rows, start=1):
        cells = []
        for column_index, value in enumerate(values, start=1):
            reference = f"{_excel_column_name(column_index)}{row_index}"
            if row_index > 1 and column_index == 5:
                cells.append(f'<c r="{reference}" s="2"><v>{Decimal(str(value))}</v></c>')
            else:
                style = ' s="1"' if row_index == 1 else ""
                cells.append(f'<c r="{reference}" t="inlineStr"{style}><is><t>{_excel_text(value)}</t></is></c>')
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    last_row = max(1, len(sheet_rows))
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        '<cols><col min="1" max="1" width="13" customWidth="1"/><col min="2" max="3" width="20" customWidth="1"/>'
        '<col min="4" max="4" width="34" customWidth="1"/><col min="5" max="5" width="14" customWidth="1"/>'
        '<col min="6" max="6" width="20" customWidth="1"/><col min="7" max="8" width="24" customWidth="1"/></cols>'
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        f'<autoFilter ref="A1:H{last_row}"/>'
        '</worksheet>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF2563EB"/><bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        '<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
        '</styleSheet>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>',
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Траты" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>',
        )
        archive.writestr("xl/styles.xml", styles)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()
