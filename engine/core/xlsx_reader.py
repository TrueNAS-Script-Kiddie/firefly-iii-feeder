"""
Read the table of an .xlsx file with the standard library (an xlsx is a zip of XML files).

The first sheet is the table, row 1 its header. Cells become text: a date-formatted number
'YYYY-MM-DD', another number its shortest exact form ('3.1000000000000001' -> '3.1',
'45.0' -> '45'), an empty cell ''. Anything that cannot be read without guessing raises
ValueError: several sheets with data, a formula without a stored value, a cell type other
than number or text, a date with a time of day, data under no header.
"""

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, timedelta
from decimal import Decimal

RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_DOCUMENT_REL = "/officeDocument"
WORKSHEET_REL = "/worksheet"
SHARED_STRINGS_REL = "/sharedStrings"
STYLES_REL = "/styles"

# Built-in number formats (ECMA-376 18.8.30) that show a date; 27-36 and 50-58 are the
# East Asian locale dates. 18-21 and 45-47 show only a time of day.
BUILTIN_DATE_FORMATS = {14, 15, 16, 17, 22, *range(27, 37), *range(50, 59)}
BUILTIN_TIME_FORMATS = {18, 19, 20, 21, 45, 46, 47}
# Parts of a format code that never show a date: quoted text, escaped characters, [colour]/[locale]
FORMAT_LITERALS = re.compile(r'"[^"]*"|\\.|\[[^\]]*\]')

EPOCH_1900 = date(1899, 12, 30)  # serial 61 = 1900-03-01; below it Excel counts a 29 Feb 1900
EPOCH_1904 = date(1904, 1, 1)
CELL_REFERENCE = re.compile(r"^([A-Z]+)([0-9]+)$")


def read_xlsx(path: str) -> tuple[list[dict[str, str]], list[str]]:
    """Rows of the first sheet as dicts under the header's names, and each row's Excel row number."""
    try:
        return read_package(path)
    except (zipfile.BadZipFile, ET.ParseError) as exc:
        raise ValueError(f"xlsx not readable: {exc}") from None


def read_package(path: str) -> tuple[list[dict[str, str]], list[str]]:
    with zipfile.ZipFile(path) as package:
        workbook_path = relationship_targets(package, "", "_rels/.rels", OFFICE_DOCUMENT_REL)[0]
        workbook = parse(package, workbook_path)
        ns = namespace(workbook)
        workbook_dir = posixpath.dirname(workbook_path)
        rels_path = posixpath.join(workbook_dir, "_rels", posixpath.basename(workbook_path) + ".rels")
        rels = relationships(package, workbook_dir, rels_path)

        properties = workbook.find(f"{{{ns}}}workbookPr")
        epoch = EPOCH_1904 if properties is not None and properties.get("date1904") in ("1", "true") else EPOCH_1900
        shared_strings = read_shared_strings(package, [t for t, kind in rels.values() if kind == SHARED_STRINGS_REL])
        date_styles, time_styles = read_date_styles(package, [t for t, kind in rels.values() if kind == STYLES_REL])

        sheets = []
        for sheet in workbook.iter(f"{{{ns}}}sheet"):
            rel_id = next((v for k, v in sheet.attrib.items() if k.endswith("}id")), None)
            target, kind = rels.get(rel_id, ("", ""))
            if kind == WORKSHEET_REL:
                sheets.append((sheet.get("name", ""), target))
        if not sheets:
            raise ValueError("xlsx has no worksheet")

        cells = CellReader(shared_strings, date_styles, time_styles, epoch)
        tables = [(name, cells.read_sheet(parse(package, target), name)) for name, target in sheets]

    with_data = [name for name, table in tables if table]
    if len(with_data) > 1 or (with_data and with_data[0] != tables[0][0]):
        raise ValueError(f"xlsx has data on more than its first sheet: {with_data}")
    return to_rows(tables[0][1], tables[0][0])


def to_rows(table: dict[int, dict[int, str]], sheet_name: str) -> tuple[list[dict[str, str]], list[str]]:
    """Row 1 is the header; every later row becomes a dict, named by its Excel row number."""
    if not table:
        return [], []
    header_cells = table.get(1, {})
    header = [header_cells.get(column, "") for column in range(max(header_cells, default=-1) + 1)]
    if not any(header):
        raise ValueError(f"xlsx sheet '{sheet_name}': row 1 (the header) is empty")
    duplicates = sorted({name for name in header if name and header.count(name) > 1})
    if duplicates:
        raise ValueError(f"xlsx sheet '{sheet_name}': header names occur more than once: {duplicates}")

    rows: list[dict[str, str]] = []
    source_lines: list[str] = []
    for row_number in sorted(n for n in table if n > 1):
        values = table[row_number]
        # A value under no header would be lost
        orphans = [c for c, v in values.items() if v and (c >= len(header) or not header[c])]
        if orphans:
            raise ValueError(f"xlsx sheet '{sheet_name}' row {row_number}: a value in a column without header")
        rows.append({name: values.get(column, "") for column, name in enumerate(header) if name})
        source_lines.append(str(row_number))
    return rows, source_lines


class CellReader:
    def __init__(self, shared_strings: list[str], date_styles: set[int], time_styles: set[int], epoch: date):
        self.shared_strings = shared_strings
        self.date_styles = date_styles
        self.time_styles = time_styles
        self.epoch = epoch

    def read_sheet(self, sheet: ET.Element, sheet_name: str) -> dict[int, dict[int, str]]:
        """Excel row number -> {0-based column: text}; rows without any value are left out."""
        ns = namespace(sheet)
        table: dict[int, dict[int, str]] = {}
        row_number = 0
        for row in sheet.iter(f"{{{ns}}}row"):
            row_number = int(row.get("r") or row_number + 1)
            values: dict[int, str] = {}
            column = -1
            for cell in row.iter(f"{{{ns}}}c"):
                reference = cell.get("r")
                column = column_index(reference) if reference else column + 1
                where = f"xlsx sheet '{sheet_name}' cell {reference or f'{column + 1} of row {row_number}'}"
                value = self.read_cell(cell, ns, where)
                if value:
                    values[column] = value
            if values:
                table[row_number] = values
        return table

    def read_cell(self, cell: ET.Element, ns: str, where: str) -> str:
        cell_type = cell.get("t", "n")
        value = cell.find(f"{{{ns}}}v")
        if cell.find(f"{{{ns}}}f") is not None and value is None:
            raise ValueError(f"{where}: formula without a stored value")
        if cell_type == "inlineStr":
            inline = cell.find(f"{{{ns}}}is")
            return string_item_text(inline, ns) if inline is not None else ""
        if value is None or value.text is None:
            return ""
        if cell_type == "s":
            return self.shared_strings[int(value.text)]
        if cell_type == "str":
            return value.text
        if cell_type != "n":
            raise ValueError(f"{where}: cell type '{cell_type}' is not supported")
        style = int(cell.get("s", "0"))
        if style in self.time_styles:
            raise ValueError(f"{where}: time format is not supported")
        if style in self.date_styles:
            return self.serial_date(value.text, where)
        return shortest_number(value.text, where)

    def serial_date(self, text: str, where: str) -> str:
        serial = Decimal(text)
        if serial != serial.to_integral_value():
            raise ValueError(f"{where}: date with a time of day is not supported ({text})")
        if self.epoch == EPOCH_1900 and serial < 61:
            raise ValueError(f"{where}: date before 1900-03-01 is not supported ({text})")
        return (self.epoch + timedelta(days=int(serial))).isoformat()


def shortest_number(text: str, where: str) -> str:
    """The shortest decimal that reads back as the same double, without exponent ('45.0' -> '45')."""
    number = float(text)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{where}: not a number ({text})")
    if number == 0:
        return "0"
    return format(Decimal(repr(number)).normalize(), "f")


def column_index(reference: str) -> int:
    """'A1' -> 0, 'K2' -> 10, 'AA3' -> 26."""
    match = CELL_REFERENCE.match(reference)
    if not match:
        raise ValueError(f"xlsx: unreadable cell reference '{reference}'")
    index = 0
    for letter in match.group(1):
        index = index * 26 + ord(letter) - ord("A") + 1
    return index - 1


def string_item_text(item: ET.Element, ns: str) -> str:
    """Text of a shared-string or inline-string item: plain <t>, or the <t> of every rich-text run."""
    plain = item.find(f"{{{ns}}}t")
    if plain is not None:
        return plain.text or ""
    return "".join(run.findtext(f"{{{ns}}}t") or "" for run in item.findall(f"{{{ns}}}r"))


def read_shared_strings(package: zipfile.ZipFile, targets: list[str]) -> list[str]:
    if not targets:
        return []
    root = parse(package, targets[0])
    ns = namespace(root)
    return [string_item_text(item, ns) for item in root.findall(f"{{{ns}}}si")]


def read_date_styles(package: zipfile.ZipFile, targets: list[str]) -> tuple[set[int], set[int]]:
    """Indexes of the cell styles that show a date, and of those that show only a time of day."""
    if not targets:
        return set(), set()
    root = parse(package, targets[0])
    ns = namespace(root)
    custom = {int(f.get("numFmtId")): f.get("formatCode", "") for f in root.iter(f"{{{ns}}}numFmt")}
    date_styles: set[int] = set()
    time_styles: set[int] = set()
    cell_formats = root.find(f"{{{ns}}}cellXfs")
    for index, xf in enumerate(cell_formats if cell_formats is not None else []):
        format_id = int(xf.get("numFmtId", "0"))
        if format_id in custom:
            code = FORMAT_LITERALS.sub("", custom[format_id]).lower()
            shows_date = bool(re.search(r"[dy]", code))
            shows_time = not shows_date and bool(re.search(r"[hs]", code))
        else:
            shows_date = format_id in BUILTIN_DATE_FORMATS
            shows_time = format_id in BUILTIN_TIME_FORMATS
        if shows_date:
            date_styles.add(index)
        elif shows_time:
            time_styles.add(index)
    return date_styles, time_styles


def relationships(package: zipfile.ZipFile, base_dir: str, rels_path: str) -> dict[str, tuple[str, str]]:
    """Relationship id -> (part path in the zip, kind such as '/worksheet')."""
    root = parse(package, rels_path)
    found = {}
    for rel in root.iter(f"{{{RELATIONSHIPS_NS}}}Relationship"):
        target = rel.get("Target", "")
        path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(base_dir, target))
        kind = rel.get("Type", "")
        found[rel.get("Id", "")] = (path, kind[kind.rfind("/") :])
    return found


def relationship_targets(package: zipfile.ZipFile, base_dir: str, rels_path: str, kind: str) -> list[str]:
    targets = [path for path, found_kind in relationships(package, base_dir, rels_path).values() if found_kind == kind]
    if not targets:
        raise ValueError(f"xlsx: no {kind.lstrip('/')} part")
    return targets


def parse(package: zipfile.ZipFile, part: str) -> ET.Element:
    try:
        return ET.fromstring(package.read(part))
    except KeyError:
        raise ValueError(f"xlsx: part '{part}' is missing") from None


def namespace(root: ET.Element) -> str:
    """The element's XML namespace: transitional and strict xlsx use different ones."""
    if not root.tag.startswith("{"):
        raise ValueError(f"xlsx: element '{root.tag}' has no namespace")
    return root.tag[1:].partition("}")[0]
