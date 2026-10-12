"""
Read the pieces of text of a PDF with the standard library: no layout, only what is printed where.

read_pdf_text(path) returns a list of TextPiece in content-stream order, page by page:
- page: page number, from 1
- x, y: where the text starts, in points (1/72 inch) from the bottom-left corner of the page,
  rounded to 3 decimals; a larger y is higher on the page
- font: the font's BaseFont ('ArialMT'), size: its size in points
- text: the decoded string of one text-showing operator

Only what a simple generator writes is understood: a classic cross-reference table, Flate or
unfiltered streams, simple fonts with WinAnsiEncoding (decoded as cp1252), text placed with
Td and shown with one Tj per position. Anything else that could hold or move text raises
ValueError, so a changed generator fails loudly instead of being read halfway: encryption,
cross-reference streams and object streams, incremental updates, other filters or predictors,
ToUnicode maps, other encodings or font types, other text operators (TJ, Tm, Tc, ...), text in
form XObjects, annotations and forms, inline images, rotated pages or text.
"""

import re
import sys
import zlib
from dataclasses import dataclass

WHITESPACE = b"\x00\t\n\x0c\r "
TOKEN = re.compile(
    rb"[\x00\t\n\x0c\r ]+|%[^\r\n]*"
    rb"|(?P<name>/[^\x00\t\n\x0c\r ()<>\[\]{}/%]*)"
    rb"|(?P<number>[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+))(?![^\x00\t\n\x0c\r ()<>\[\]{}/%])"
    rb"|(?P<dict_open><<)|(?P<dict_close>>>)"
    rb"|(?P<hex><[0-9A-Fa-f\x00\t\n\x0c\r ]*>)"
    rb"|(?P<array_open>\[)|(?P<array_close>\])"
    rb"|(?P<string>\()"
    rb"|(?P<word>[^\x00\t\n\x0c\r ()<>\[\]{}/%]+)"
)
NAME_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
STRING_ESCAPES = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f"}
OBJECT_HEADER = re.compile(rb"([0-9]+)[\x00\t\n\x0c\r ]+([0-9]+)[\x00\t\n\x0c\r ]+obj\b")
STREAM_START = re.compile(rb"[\x00\t\n\x0c\r ]*stream(?:\r\n|\n)")
STREAM_END = re.compile(rb"(?:\r\n|\r|\n)?endstream\b")
# A text object as a token, in a form XObject that is only scanned, not interpreted
TEXT_OBJECT = re.compile(rb"(?<![^\x00\t\n\x0c\r \])>])BT(?![^\x00\t\n\x0c\r \[(/<])")

# Operators that draw or set graphics state but never show or move text
GRAPHICS_OPERATORS = {
    *"m l c v y h re S s f F f* B B* b b* n W W* w J j M d ri i g G rg RG k K cs CS sc SC scn SCN sh".split(),
    *"BMC BDC EMC MP DP BX EX".split(),
}
UNSUPPORTED_TEXT_OPERATORS = {*"TJ ' \" T* TD Tm Tc Tw Tz TL Ts Tr".split()}
SUPPORTED_FONT_TYPES = {"TrueType", "Type1"}
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


@dataclass(frozen=True)
class TextPiece:
    page: int
    x: float
    y: float
    font: str
    size: float
    text: str


class Name(str):
    """A PDF name (/Font), apart from a string, which is bytes."""


@dataclass(frozen=True)
class Ref:
    number: int
    generation: int


@dataclass(frozen=True)
class Operator:
    word: str


def read_pdf_text(path: str) -> list[TextPiece]:
    with open(path, "rb") as file:
        data = file.read()
    try:
        return Document(data).text_pieces()
    except zlib.error as exc:
        raise ValueError(f"PDF not readable: {exc}") from None


# ---------------------------------------------------------------------------
# Lexer and object parser
# ---------------------------------------------------------------------------
class Lexer:
    def __init__(self, data: bytes, position: int = 0):
        self.data = data
        self.position = position

    def token(self):
        """The next token as a value (number, Name, bytes, Operator, '[', ']', '<<', '>>'); None at the end."""
        while True:
            match = TOKEN.match(self.data, self.position)
            if not match:
                if self.position >= len(self.data):
                    return None
                raise ValueError(f"PDF: unreadable syntax at byte {self.position}")
            self.position = match.end()
            kind = match.lastgroup
            if kind is None:
                continue  # whitespace or comment
            text = match.group(kind)
            if kind == "name":
                return Name(NAME_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), text[1:]).decode("latin-1"))
            if kind == "number":
                return float(text) if b"." in text else int(text)
            if kind == "hex":
                digits = bytes(c for c in text[1:-1] if c not in WHITESPACE)
                return bytes.fromhex((digits + b"0" if len(digits) % 2 else digits).decode())
            if kind == "string":
                return self.literal_string()
            if kind == "word":
                return Operator(text.decode("latin-1"))
            return {"dict_open": "<<", "dict_close": ">>", "array_open": "[", "array_close": "]"}[kind]

    def literal_string(self) -> bytes:
        data, i, depth, out = self.data, self.position, 1, bytearray()
        while True:
            if i >= len(data):
                raise ValueError("PDF: string not closed")
            byte = data[i]
            i += 1
            if byte == 0x5C:  # backslash
                if i >= len(data):
                    raise ValueError("PDF: string not closed")
                escaped = data[i]
                i += 1
                if escaped in STRING_ESCAPES:
                    out += STRING_ESCAPES[escaped]
                elif 0x30 <= escaped <= 0x37:
                    digits = bytes([escaped])
                    while len(digits) < 3 and i < len(data) and 0x30 <= data[i] <= 0x37:
                        digits += data[i : i + 1]
                        i += 1
                    out.append(int(digits, 8) & 0xFF)
                elif escaped == 0x0D:  # line continuation
                    if i < len(data) and data[i] == 0x0A:
                        i += 1
                elif escaped != 0x0A:
                    out.append(escaped)  # '\(', '\)', '\\', and an unknown escape is the character itself
            elif byte == 0x28:
                depth += 1
                out.append(byte)
            elif byte == 0x29:
                depth -= 1
                if depth == 0:
                    self.position = i
                    return bytes(out)
                out.append(byte)
            elif byte == 0x0D:  # an end of line in a string is a newline, whatever its form
                if i < len(data) and data[i] == 0x0A:
                    i += 1
                out.append(0x0A)
            else:
                out.append(byte)

    def value(self, token=None, references: bool = True):
        """One complete value: arrays and dictionaries in full, 'n g R' as a Ref when references are allowed."""
        if token is None:
            token = self.token()
        if token == "[":
            items = []
            while (item := self.token()) != "]":
                if item is None:
                    raise ValueError("PDF: array not closed")
                items.append(self.value(item, references))
            return items
        if token == "<<":
            entries = {}
            while (key := self.token()) != ">>":
                if not isinstance(key, Name):
                    raise ValueError(f"PDF: dictionary key is not a name: {key!r}")
                entries[key] = self.value(None, references)
            return entries
        if references and isinstance(token, int) and token >= 0:
            saved = self.position
            generation, keyword = self.token(), self.token()
            if isinstance(generation, int) and keyword == Operator("R"):
                return Ref(token, generation)
            self.position = saved
        if isinstance(token, Operator) and token.word in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[token.word]
        if token is None or token in (">>", "]"):
            raise ValueError("PDF: value expected")
        return token


# ---------------------------------------------------------------------------
# Document structure
# ---------------------------------------------------------------------------
class Document:
    def __init__(self, data: bytes):
        if not data.startswith(b"%PDF-"):
            raise ValueError("Not a PDF: no %PDF- header")
        self.data = data
        self.offsets: dict[int, int] = {}
        self.objects: dict[int, object] = {}
        self.streams: dict[int, bytes] = {}
        self.trailer = self.read_cross_references()
        if "Encrypt" in self.trailer:
            raise ValueError("PDF is encrypted")

    def read_cross_references(self) -> dict:
        tail = self.data[-1024:]
        found = re.findall(rb"startxref[\x00\t\n\x0c\r ]+([0-9]+)", tail)
        if not found:
            raise ValueError("PDF: no startxref")
        lexer = Lexer(self.data, int(found[-1]))
        keyword = lexer.token()
        if isinstance(keyword, int):
            raise ValueError("PDF: cross-reference stream (object streams) is not supported")
        if keyword != Operator("xref"):
            raise ValueError("PDF: no cross-reference table at startxref")
        while (token := lexer.token()) != Operator("trailer"):
            first, count = token, lexer.token()
            if not isinstance(first, int) or not isinstance(count, int):
                raise ValueError("PDF: unreadable cross-reference table")
            for number in range(first, first + count):
                offset, _generation, kind = lexer.token(), lexer.token(), lexer.token()
                if kind == Operator("n") and isinstance(offset, int):
                    self.offsets[number] = offset
                elif kind != Operator("f"):
                    raise ValueError("PDF: unreadable cross-reference table")
        trailer = lexer.value()
        if not isinstance(trailer, dict):
            raise ValueError("PDF: unreadable trailer")
        for key, what in (("Prev", "incremental update"), ("XRefStm", "cross-reference stream")):
            if key in trailer:
                raise ValueError(f"PDF: {what} is not supported")
        return trailer

    def resolve(self, value):
        return self.load(value.number) if isinstance(value, Ref) else value

    def load(self, number: int):
        if number in self.objects:
            return self.objects[number]
        if number not in self.offsets:
            raise ValueError(f"PDF: object {number} is not in the cross-reference table")
        offset = self.offsets[number]
        header = OBJECT_HEADER.match(self.data, offset)
        if not header or int(header.group(1)) != number:
            raise ValueError(f"PDF: object {number} is not where the cross-reference table says")
        lexer = Lexer(self.data, header.end())
        value = lexer.value()
        if isinstance(value, dict):
            start = STREAM_START.match(self.data, lexer.position)
            if start:
                length = self.resolve(value.get("Length"))
                if not isinstance(length, int):
                    raise ValueError(f"PDF: stream {number} has no length")
                end = STREAM_END.match(self.data, start.end() + length)
                if not end:
                    raise ValueError(f"PDF: stream {number} does not end at its length")
                self.streams[number] = self.data[start.end() : start.end() + length]
        self.objects[number] = value
        return value

    def stream_data(self, reference) -> bytes:
        """The decoded data of a stream object: unfiltered or Flate without predictor, nothing else."""
        if not isinstance(reference, Ref):
            raise ValueError("PDF: stream expected as an indirect object")
        dictionary = self.load(reference.number)
        if reference.number not in self.streams:
            raise ValueError(f"PDF: object {reference.number} is not a stream")
        filters = self.resolve(dictionary.get("Filter"))
        filters = filters if isinstance(filters, list) else [] if filters is None else [filters]
        if any(self.resolve(f) != "FlateDecode" for f in filters):
            raise ValueError(f"PDF: stream filter {filters} is not supported, only FlateDecode")
        if "DecodeParms" in dictionary:
            raise ValueError("PDF: stream decode parameters (predictors) are not supported")
        data = self.streams[reference.number]
        for _ in filters:
            data = zlib.decompress(data)
        return data

    def pages(self) -> list[tuple[dict, dict]]:
        """Every page in order, with its resources (inherited from the page tree when the page has none)."""
        catalog = self.resolve(self.trailer.get("Root"))
        if not isinstance(catalog, dict):
            raise ValueError("PDF: no document catalog")
        if "AcroForm" in catalog:
            raise ValueError("PDF: interactive forms are not supported")
        found: list[tuple[dict, dict]] = []
        self.walk_pages(catalog.get("Pages"), {}, found, set())
        return found

    def walk_pages(self, reference, inherited: dict, found: list, seen: set) -> None:
        if isinstance(reference, Ref):
            if reference.number in seen:
                raise ValueError("PDF: the page tree loops")
            seen.add(reference.number)
        node = self.resolve(reference)
        if not isinstance(node, dict):
            raise ValueError("PDF: unreadable page tree")
        attributes = {**inherited, **{k: node[k] for k in ("Resources", "Rotate") if k in node}}
        if node.get("Type") == "Pages":
            for kid in self.resolve(node.get("Kids", [])):
                self.walk_pages(kid, attributes, found, seen)
        elif node.get("Type") == "Page":
            if self.resolve(attributes.get("Rotate", 0)) % 360:
                raise ValueError(f"PDF: page {len(found) + 1} is rotated")
            if node.get("Annots"):
                raise ValueError(f"PDF: page {len(found) + 1} has annotations")
            found.append((node, self.resolve(attributes.get("Resources", {}))))
        else:
            raise ValueError(f"PDF: unknown page tree node {node.get('Type')!r}")

    def text_pieces(self) -> list[TextPiece]:
        pieces: list[TextPiece] = []
        for page_number, (page, resources) in enumerate(self.pages(), start=1):
            contents = self.resolve(page.get("Contents"))
            parts = contents if isinstance(contents, list) else [] if contents is None else [page.get("Contents")]
            data = b"\n".join(self.stream_data(part) for part in parts)
            ContentReader(self, page_number, resources, pieces).run(data)
        return pieces

    def font(self, resources: dict, resource_name: str) -> str:
        """BaseFont of a font resource we can decode as cp1252; anything else is refused."""
        font = self.resolve(self.resolve(resources.get("Font", {})).get(resource_name))
        if not isinstance(font, dict):
            raise ValueError(f"PDF: font {resource_name} is not in the resources")
        base_font = font.get("BaseFont", resource_name)
        if font.get("Subtype") not in SUPPORTED_FONT_TYPES:
            raise ValueError(f"PDF: font {base_font} of type {font.get('Subtype')} is not supported")
        if "ToUnicode" in font:
            raise ValueError(f"PDF: font {base_font} has a ToUnicode map, which is not supported")
        if self.resolve(font.get("Encoding")) != "WinAnsiEncoding":
            raise ValueError(f"PDF: font {base_font} is not WinAnsiEncoding")
        return str(base_font)

    def form_shows_text(self, reference, seen: set) -> bool:
        """Whether a form XObject, or a form it draws, holds a text object."""
        if not isinstance(reference, Ref) or reference.number in seen:
            return False
        seen.add(reference.number)
        form = self.load(reference.number)
        if TEXT_OBJECT.search(self.stream_data(reference)):
            return True
        resources = self.resolve(form.get("Resources", {}))
        for inner in self.resolve(resources.get("XObject", {})).values():
            inner_object = self.resolve(inner)
            if isinstance(inner_object, dict) and inner_object.get("Subtype") == "Form":
                if self.form_shows_text(inner, seen):
                    return True
        return False


# ---------------------------------------------------------------------------
# Content stream
# ---------------------------------------------------------------------------
def multiply(first: tuple, second: tuple) -> tuple:
    a, b, c, d, e, f = first
    p, q, r, s, t, u = second
    return (a * p + b * r, a * q + b * s, c * p + d * r, c * q + d * s, e * p + f * r + t, e * q + f * s + u)


class ContentReader:
    def __init__(self, document: Document, page: int, resources: dict, pieces: list[TextPiece]):
        self.document = document
        self.page = page
        self.resources = resources
        self.pieces = pieces
        # Graphics state: transformation matrix, font (BaseFont) and size; q/Q save and restore it
        self.state = (IDENTITY, None, None)
        self.saved: list[tuple] = []
        self.line_matrix: tuple | None = None  # inside BT ... ET only
        self.shown_since_move = False

    def run(self, data: bytes) -> None:
        lexer = Lexer(data)
        operands: list = []
        while (token := lexer.token()) is not None:
            if isinstance(token, Operator) and token.word not in ("true", "false", "null"):
                if token.word == "BI":
                    raise ValueError(f"PDF page {self.page}: inline images are not supported")
                self.operator(token.word, operands)
                operands = []
            else:
                operands.append(lexer.value(token, references=False))

    def operator(self, word: str, operands: list) -> None:
        where = f"PDF page {self.page}"
        matrix, font, size = self.state
        if word == "q":
            self.saved.append(self.state)
        elif word == "Q":
            if not self.saved:
                raise ValueError(f"{where}: Q without q")
            self.state = self.saved.pop()
        elif word == "cm":
            self.state = (multiply(tuple(self.numbers(operands, 6, word)), matrix), font, size)
        elif word == "gs":
            name = operands[-1] if operands else None
            graphics_state = self.document.resolve(self.document.resolve(self.resources.get("ExtGState", {})).get(name))
            if isinstance(graphics_state, dict) and "Font" in graphics_state:
                raise ValueError(f"{where}: a font set through gs is not supported")
        elif word == "Do":
            self.draw_object(operands)
        elif word == "BT":
            if self.line_matrix is not None:
                raise ValueError(f"{where}: BT inside a text object")
            self.line_matrix = IDENTITY
            self.shown_since_move = False
        elif word == "ET":
            self.line_matrix = None
        elif word == "Tf":
            if len(operands) != 2 or not isinstance(operands[0], Name):
                raise ValueError(f"{where}: unreadable Tf")
            font = self.document.font(self.resources, operands[0])
            self.state = (matrix, font, self.numbers(operands[1:], 1, word)[0])
        elif word == "Td":
            tx, ty = self.numbers(operands, 2, word)
            self.line_matrix = multiply((1.0, 0.0, 0.0, 1.0, tx, ty), self.in_text_object(word))
            self.shown_since_move = False
        elif word == "Tj":
            self.show(operands)
        elif word in UNSUPPORTED_TEXT_OPERATORS:
            raise ValueError(f"{where}: text operator {word} is not supported")
        elif word not in GRAPHICS_OPERATORS:
            raise ValueError(f"{where}: unknown operator {word}")

    def show(self, operands: list) -> None:
        where = f"PDF page {self.page}"
        line_matrix = self.in_text_object("Tj")
        if len(operands) != 1 or not isinstance(operands[0], bytes):
            raise ValueError(f"{where}: unreadable Tj")
        matrix, font, size = self.state
        if font is None:
            raise ValueError(f"{where}: Tj without a font")
        # A second string at the same position would need the glyph widths to place it
        if self.shown_since_move:
            raise ValueError(f"{where}: Tj after Tj without Td is not supported")
        self.shown_since_move = True
        a, b, c, d, x, y = multiply(line_matrix, matrix)
        if b or c or a <= 0 or d <= 0:
            raise ValueError(f"{where}: rotated or mirrored text is not supported")
        try:
            text = operands[0].decode("cp1252")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{where}: byte not in WinAnsiEncoding: {exc}") from None
        if text:
            self.pieces.append(TextPiece(self.page, round(x, 3), round(y, 3), font, round(size * d, 3), text))

    def draw_object(self, operands: list) -> None:
        name = operands[-1] if operands else None
        reference = self.document.resolve(self.resources.get("XObject", {})).get(name)
        drawn = self.document.resolve(reference)
        if not isinstance(drawn, dict):
            raise ValueError(f"PDF page {self.page}: XObject {name} is not in the resources")
        if drawn.get("Subtype") == "Form":
            if self.document.form_shows_text(reference, set()):
                raise ValueError(f"PDF page {self.page}: text in form XObject {name} is not supported")
        elif drawn.get("Subtype") != "Image":
            raise ValueError(f"PDF page {self.page}: XObject {name} of type {drawn.get('Subtype')} is not supported")

    def in_text_object(self, word: str) -> tuple:
        if self.line_matrix is None:
            raise ValueError(f"PDF page {self.page}: {word} outside BT ... ET")
        return self.line_matrix

    def numbers(self, operands: list, count: int, word: str) -> list[float]:
        if len(operands) != count or not all(isinstance(v, int | float) and not isinstance(v, bool) for v in operands):
            raise ValueError(f"PDF page {self.page}: unreadable {word}")
        return [float(v) for v in operands]


def main() -> None:
    """Print the pieces of one PDF: page, x, y, font, size, text."""
    for piece in read_pdf_text(sys.argv[1]):
        print(f"p{piece.page} {piece.x:8.3f} {piece.y:8.3f} {piece.font} {piece.size:g} {piece.text!r}")


if __name__ == "__main__":
    main()
