"""Minimal right-to-left PDF layout engine for ReportLab.

Arabic needs three things that ReportLab does not do on its own:

1. **Shaping.** Arabic letters change form by position in the word. Unicode
   stores the abstract letter; a PDF draws glyphs. ``arabic_reshaper`` maps one
   to the other.
2. **Bidirectional ordering.** A line mixing Arabic with Latin (``XAUUSD``,
   ``MT5``) has runs going in opposite directions. The Unicode bidi algorithm
   resolves the visual order; ``python-bidi`` implements it.
3. **A font with the glyphs.** Most Arabic fonts here carry no Latin at all, so
   ``XAUUSD`` silently renders as nothing. DejaVu Sans covers Arabic
   presentation forms, Latin, digits and punctuation in one family, which keeps
   a bilingual technical document in a single font.

Wrapping order matters and is easy to get wrong: text is wrapped in *logical*
order and only then shaped and reordered, one line at a time. Reshaping first
and wrapping the result would break words across the bidi boundary and scramble
the line.
"""
from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass

import arabic_reshaper
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

try:  # python-bidi moved this between versions
    from bidi import get_display
except ImportError:  # pragma: no cover
    from bidi.algorithm import get_display

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
FONTS = {
    "Body": os.path.join(FONT_DIR, "DejaVuSans.ttf"),
    "Bold": os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"),
    "Mono": os.path.join(FONT_DIR, "DejaVuSansMono.ttf"),
}

INK = HexColor("#1A1A1A")
MUTED = HexColor("#6B6B6B")
RULE = HexColor("#D8D8D8")
NAVY = HexColor("#16314F")
GOLD = HexColor("#B8860B")
WARN_BG = HexColor("#FDF3E3")
WARN_EDGE = HexColor("#C8901A")
CODE_BG = HexColor("#F4F5F7")
TABLE_HEAD = HexColor("#16314F")
TABLE_ALT = HexColor("#F7F8FA")


_fonts_ready = False


def register_fonts() -> None:
    """Register the DejaVu family. Idempotent, and safe to call from anywhere.

    Measuring text needs the fonts registered, and measurement happens in
    ``width_of``/``wrap_logical``, which callers reasonably use without first
    constructing a document. So registration is lazy rather than a
    precondition the caller has to remember.
    """
    global _fonts_ready
    if _fonts_ready:
        return
    for name, path in FONTS.items():
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"font not found: {path}. Install the DejaVu family "
                "(Debian/Ubuntu: apt-get install fonts-dejavu-core)."
            )
        pdfmetrics.registerFont(TTFont(name, path))
    _fonts_ready = True


#: Characters that must be swapped for their mirror image inside a
#: right-to-left run (Unicode Bidirectional Algorithm, rule L4).
_MIRROR = {
    "(": ")", ")": "(",
    "[": "]", "]": "[",
    "{": "}", "}": "{",
    "<": ">", ">": "<",
    "\u00ab": "\u00bb", "\u00bb": "\u00ab",   # guillemets
    "\u2039": "\u203a", "\u203a": "\u2039",
}


def _strong_dir(ch: str) -> str | None:
    """'L', 'R', or None for characters that take direction from context."""
    cat = unicodedata.bidirectional(ch)
    if cat == "L":
        return "L"
    if cat in ("R", "AL"):
        return "R"
    return None


def _apply_mirroring(visual: str) -> str:
    """Apply rule L4, which python-bidi does not.

    ``python-bidi`` reorders correctly but leaves bracket glyphs untouched, so
    every parenthesis inside Arabic text comes out backwards -- "(مثال)" renders
    as ")مثال(". The UBA says a mirrored character at an odd (RTL) embedding
    level is drawn with its mirror glyph.

    The resolved level is not exposed by the library, so it is inferred the way
    rules N1/N2 resolve neutrals: look outward for the nearest strong character
    on each side. A bracket surrounded by Latin on both sides sits in an LTR run
    and is left alone (``Ctrl+T (v2)``); anything else in an RTL paragraph
    resolves to RTL and is mirrored -- including brackets wrapping a Latin word
    inside an Arabic sentence (``مجلد (Experts) هنا``), which is correct.
    """
    chars = list(visual)
    n = len(chars)
    for i, ch in enumerate(chars):
        if ch not in _MIRROR:
            continue

        left = None
        for j in range(i - 1, -1, -1):
            d = _strong_dir(chars[j])
            if d:
                left = d
                break
        right = None
        for j in range(i + 1, n):
            d = _strong_dir(chars[j])
            if d:
                right = d
                break

        if left == "L" and right == "L":
            continue  # genuinely inside a left-to-right run
        chars[i] = _MIRROR[ch]
    return "".join(chars)


def shape(text: str) -> str:
    """Logical Arabic/mixed text -> visually ordered, shaped glyph string."""
    return _apply_mirroring(get_display(arabic_reshaper.reshape(text)))


def width_of(text: str, font: str, size: float) -> float:
    register_fonts()
    return pdfmetrics.stringWidth(shape(text), font, size)


def wrap_logical(text: str, font: str, size: float, max_width: float) -> list[str]:
    """Greedy word wrap performed on LOGICAL text.

    Each candidate line is measured after shaping, because shaping changes the
    rendered width, but the split points are chosen on the original word order.
    """
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if width_of(trial, font, size) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


@dataclass
class Theme:
    page_size: tuple = A4
    margin_x: float = 46
    margin_top: float = 62
    margin_bottom: float = 54
    body_size: float = 10.5
    body_leading: float = 18.5


class RtlDoc:
    """A right-to-left flowing document with automatic pagination."""

    def __init__(self, path: str, title: str, subtitle: str = "", theme: Theme | None = None):
        register_fonts()
        self.theme = theme or Theme()
        self.W, self.H = self.theme.page_size
        self.c = canvas.Canvas(path, pagesize=self.theme.page_size)
        self.c.setTitle(title)
        self.c.setAuthor("goldbot")
        self.c.setSubject(subtitle)
        self.title = title
        self.page_no = 0
        self.y = 0.0
        self._first_page_started = False

    # ------------------------------------------------------------- geometry
    @property
    def right(self) -> float:
        return self.W - self.theme.margin_x

    @property
    def left(self) -> float:
        return self.theme.margin_x

    @property
    def content_width(self) -> float:
        return self.W - 2 * self.theme.margin_x

    # ------------------------------------------------------------ page flow
    def _start_page(self, with_header: bool = True) -> None:
        self.page_no += 1
        self.y = self.H - self.theme.margin_top
        if with_header and self.page_no > 1:
            self.c.setFont("Body", 8)
            self.c.setFillColor(MUTED)
            self.c.drawRightString(self.right, self.H - 34, shape(self.title))
            self.c.setStrokeColor(RULE)
            self.c.setLineWidth(0.5)
            self.c.line(self.left, self.H - 42, self.right, self.H - 42)
        self._footer()

    def _footer(self) -> None:
        self.c.setFont("Body", 8)
        self.c.setFillColor(MUTED)
        self.c.drawCentredString(self.W / 2, 30, str(self.page_no))
        self.c.setFillColor(INK)

    def ensure(self, needed: float) -> None:
        if not self._first_page_started:
            self._start_page()
            self._first_page_started = True
            return
        if self.y - needed < self.theme.margin_bottom:
            self.page_break()

    def page_break(self) -> None:
        self.c.showPage()
        self._start_page()

    def space(self, amount: float) -> None:
        self.ensure(amount)
        self.y -= amount

    # --------------------------------------------------------------- blocks
    def h1(self, text: str) -> None:
        self.ensure(58)
        self.y -= 14
        self.c.setFont("Bold", 17)
        self.c.setFillColor(NAVY)
        self.c.drawRightString(self.right, self.y, shape(text))
        self.y -= 9
        self.c.setStrokeColor(GOLD)
        self.c.setLineWidth(2)
        self.c.line(self.right - 62, self.y, self.right, self.y)
        self.c.setFillColor(INK)
        self.y -= 18

    def h2(self, text: str) -> None:
        self.ensure(42)
        self.y -= 8
        self.c.setFont("Bold", 12.5)
        self.c.setFillColor(NAVY)
        self.c.drawRightString(self.right, self.y, shape(text))
        self.c.setFillColor(INK)
        self.y -= 19

    def p(self, text: str, size: float | None = None, color=INK, gap: float = 7) -> None:
        size = size or self.theme.body_size
        lines = wrap_logical(text, "Body", size, self.content_width)
        lead = self.theme.body_leading if size >= 10 else size + 6
        for line in lines:
            self.ensure(lead)
            self.c.setFont("Body", size)
            self.c.setFillColor(color)
            self.c.drawRightString(self.right, self.y, shape(line))
            self.y -= lead
        self.c.setFillColor(INK)
        self.y -= gap

    def bullet(self, text: str, marker: str = "\u2022") -> None:
        size = self.theme.body_size
        indent = 16
        lines = wrap_logical(text, "Body", size, self.content_width - indent)
        lead = self.theme.body_leading
        for idx, line in enumerate(lines):
            self.ensure(lead)
            self.c.setFont("Body", size)
            self.c.setFillColor(INK)
            self.c.drawRightString(self.right - indent, self.y, shape(line))
            if idx == 0:
                self.c.setFillColor(GOLD)
                self.c.drawRightString(self.right, self.y, marker)
                self.c.setFillColor(INK)
            self.y -= lead
        self.y -= 3

    def numbered(self, n: int, text: str) -> None:
        size = self.theme.body_size
        indent = 22
        lines = wrap_logical(text, "Body", size, self.content_width - indent)
        lead = self.theme.body_leading
        for idx, line in enumerate(lines):
            self.ensure(lead)
            self.c.setFont("Body", size)
            self.c.setFillColor(INK)
            self.c.drawRightString(self.right - indent, self.y, shape(line))
            if idx == 0:
                self.c.setFont("Bold", size)
                self.c.setFillColor(NAVY)
                self.c.drawRightString(self.right, self.y, f"{n}.")
                self.c.setFillColor(INK)
            self.y -= lead
        self.y -= 4

    def code(self, lines: list[str], size: float = 9) -> None:
        """Left-to-right monospace block: paths, settings, log output.

        Content must be Latin/ASCII. A code block is drawn verbatim, with no
        shaping and no bidi reordering, because reordering a file path or a log
        line would corrupt it. Arabic put in here therefore renders as
        unjoined letters in reverse -- silently, with no error. Rather than let
        that ship, it is rejected: explanatory prose belongs in ``p()`` next to
        the block, not inside it.
        """
        for line in lines:
            bad = [c for c in line if _strong_dir(c) == "R"]
            if bad:
                raise ValueError(
                    "code() received right-to-left text, which cannot be drawn "
                    f"verbatim: {line!r}. Move the Arabic into p() or note()."
                )
        lead = size + 5
        pad = 9
        height = len(lines) * lead + 2 * pad
        self.ensure(height + 8)
        top = self.y + 4
        self.c.setFillColor(CODE_BG)
        self.c.setStrokeColor(RULE)
        self.c.setLineWidth(0.6)
        self.c.roundRect(self.left, top - height, self.content_width, height, 4, stroke=1, fill=1)
        self.c.setFillColor(INK)
        ty = top - pad - size + 1
        for line in lines:
            self.c.setFont("Mono", size)
            self.c.drawString(self.left + pad, ty, line)
            ty -= lead
        self.y = top - height - 12

    def note(self, title: str, body: str, bg=WARN_BG, edge=WARN_EDGE) -> None:
        size = self.theme.body_size - 0.5
        inner = self.content_width - 24
        body_lines = wrap_logical(body, "Body", size, inner)
        lead = size + 7
        height = 20 + len(body_lines) * lead + 14
        self.ensure(height + 10)
        top = self.y + 6
        self.c.setFillColor(bg)
        self.c.setStrokeColor(edge)
        self.c.setLineWidth(0.9)
        self.c.roundRect(self.left, top - height, self.content_width, height, 4, stroke=1, fill=1)
        # accent bar on the right edge (RTL reading start)
        self.c.setFillColor(edge)
        self.c.rect(self.right - 3.5, top - height, 3.5, height, stroke=0, fill=1)

        ty = top - 17
        self.c.setFont("Bold", size + 0.5)
        self.c.setFillColor(edge)
        self.c.drawRightString(self.right - 12, ty, shape(title))
        ty -= lead + 2
        self.c.setFillColor(INK)
        for line in body_lines:
            self.c.setFont("Body", size)
            self.c.drawRightString(self.right - 12, ty, shape(line))
            ty -= lead
        self.y = top - height - 12

    def table(self, headers: list[str], rows: list[list[str]], widths: list[float],
              size: float = 8.8, ltr_cols: tuple = ()) -> None:
        """RTL table: the first header sits at the right edge.

        ``ltr_cols`` names column indices holding Latin identifiers (parameter
        names, paths), which are drawn left-aligned so they stay readable.
        """
        total = sum(widths)
        scale = self.content_width / total
        widths = [w * scale for w in widths]
        lead = size + 6
        head_h = lead + 8

        def row_height(cells: list[str]) -> float:
            n = 1
            for idx, cell in enumerate(cells):
                n = max(n, len(wrap_logical(cell, "Body", size, widths[idx] - 10)))
            return n * lead + 7

        self.ensure(head_h + row_height(rows[0]) + 6 if rows else head_h + 6)

        def draw_header() -> None:
            self.c.setFillColor(TABLE_HEAD)
            self.c.rect(self.left, self.y - head_h + 4, self.content_width, head_h, stroke=0, fill=1)
            x = self.right
            for idx, head in enumerate(headers):
                self.c.setFont("Bold", size)
                self.c.setFillColor(HexColor("#FFFFFF"))
                self.c.drawRightString(x - 6, self.y - head_h + 11, shape(head))
                x -= widths[idx]
            self.y -= head_h

        draw_header()
        alt = False
        for cells in rows:
            h = row_height(cells)
            if self.y - h < self.theme.margin_bottom:
                self.page_break()
                draw_header()
            if alt:
                self.c.setFillColor(TABLE_ALT)
                self.c.rect(self.left, self.y - h + 4, self.content_width, h, stroke=0, fill=1)
            alt = not alt

            x = self.right
            for idx, cell in enumerate(cells):
                col_w = widths[idx]
                lines = wrap_logical(cell, "Body", size, col_w - 10)
                ty = self.y - lead + 1
                for line in lines:
                    if idx in ltr_cols:
                        self.c.setFont("Mono", size - 0.4)
                        self.c.setFillColor(NAVY)
                        self.c.drawString(x - col_w + 6, ty, line)
                    else:
                        self.c.setFont("Body", size)
                        self.c.setFillColor(INK)
                        self.c.drawRightString(x - 6, ty, shape(line))
                    ty -= lead
                x -= col_w

            self.c.setStrokeColor(RULE)
            self.c.setLineWidth(0.4)
            self.c.line(self.left, self.y - h + 4, self.right, self.y - h + 4)
            self.y -= h
        self.y -= 10

    # ----------------------------------------------------------------- cover
    def cover(self, title: str, subtitle: str, tagline: str, meta: list[str]) -> None:
        self._start_page(with_header=False)
        self._first_page_started = True
        self.c.setFillColor(NAVY)
        self.c.rect(0, self.H - 250, self.W, 250, stroke=0, fill=1)
        self.c.setFillColor(GOLD)
        self.c.rect(0, self.H - 256, self.W, 6, stroke=0, fill=1)

        self.c.setFillColor(HexColor("#FFFFFF"))
        self.c.setFont("Bold", 27)
        self.c.drawRightString(self.right, self.H - 120, shape(title))
        self.c.setFont("Body", 14)
        self.c.setFillColor(HexColor("#C9D6E4"))
        self.c.drawRightString(self.right, self.H - 152, shape(subtitle))
        self.c.setFont("Body", 10.5)
        self.c.setFillColor(GOLD)
        self.c.drawRightString(self.right, self.H - 186, shape(tagline))

        self.y = self.H - 300
        self.c.setFillColor(INK)
        for line in meta:
            self.c.setFont("Body", 10)
            self.c.drawRightString(self.right, self.y, shape(line))
            self.y -= 19
        self.y -= 16

    def save(self) -> None:
        self.c.showPage()
        self.c.save()
