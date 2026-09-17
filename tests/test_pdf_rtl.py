"""Tests for the RTL PDF layout engine.

The bracket-mirroring tests exist because python-bidi reorders text correctly
but does not implement Unicode rule L4, so every parenthesis in Arabic context
renders backwards until L4 is applied on top.
"""
from __future__ import annotations

import os

import pytest

from tools.pdf_rtl import RtlDoc, shape, width_of, wrap_logical

AR = "المنصّة والإعدادات"


def test_fonts_cover_arabic_and_latin():
    """A font with no Latin glyphs drops 'XAUUSD' silently -- no error, no text."""
    from fontTools.ttLib import TTFont
    from tools.pdf_rtl import FONTS

    probe = shape("بوت الذهب XAUUSD على MetaTrader 5 بنسبة 0.5%")
    for path in FONTS.values():
        font = TTFont(path, lazy=True)
        cmap = set()
        for table in font["cmap"].tables:
            cmap |= set(table.cmap.keys())
        missing = [c for c in probe if c.strip() and ord(c) not in cmap]
        assert not missing, f"{os.path.basename(path)} missing {missing}"
        font.close()


def test_arabic_is_reshaped_not_passed_through():
    """Reshaping maps abstract letters to positional presentation forms."""
    out = shape("مرحبا")
    assert out != "مرحبا"
    assert any(0xFE70 <= ord(c) <= 0xFEFF for c in out)


def test_arabic_is_reversed_for_visual_order():
    logical = "ابج"
    visual = shape(logical)
    assert visual[0] != shape("ا")[0] or len(visual) == len(logical)
    assert len(visual) >= 3


@pytest.mark.parametrize(
    "text,expect_open_at_left",
    [
        ("النص (مثال) هنا", True),
        ("مجلد (Experts) هنا", True),
        ("افتح (اضغط Ctrl+T) الآن", True),
    ],
)
def test_brackets_are_mirrored_in_rtl_context(text, expect_open_at_left):
    """In an RTL run the logical '(' must be drawn as its mirror.

    Without rule L4 the group comes out as ')content(' which reads as a
    backwards parenthetical to every reader.
    """
    visual = shape(text)
    opens = visual.index("(")
    closes = visual.index(")")
    assert opens < closes, f"parenthetical is inverted: {visual}"


def test_brackets_inside_a_latin_run_are_not_mirrored():
    text = "use the shortcut Ctrl+T (v2) now"
    assert shape(text) == text, "pure Latin text must pass through untouched"


def test_square_and_curly_brackets_mirror_too():
    visual = shape("القيمة [0.5] والحد {2}")
    assert visual.index("[") < visual.index("]")
    assert visual.index("{") < visual.index("}")


def test_wrap_respects_the_width_budget():
    text = "هذا نص عربي طويل جدا مخلوط بكلمات لاتينية مثل XAUUSD و MetaTrader 5 " * 4
    max_w = 300.0
    lines = wrap_logical(text, "Body", 10.5, max_w)
    assert len(lines) > 1
    for line in lines:
        assert width_of(line, "Body", 10.5) <= max_w + 0.5, line


def test_wrap_preserves_every_word():
    text = "انسخ الملف GoldBot_M5.mq5 إلى مجلد Experts ثم اضغط Refresh"
    joined = " ".join(wrap_logical(text, "Body", 10.5, 120.0))
    assert joined.split() == text.split(), "wrapping must not drop or reorder words"


def test_wrap_handles_empty_input():
    assert wrap_logical("", "Body", 10, 100) == [""]


def test_document_builds_and_paginates(tmp_path):
    out = tmp_path / "probe.pdf"
    doc = RtlDoc(str(out), AR)
    doc.cover("عنوان", "وصف", "سطر", ["معلومة"])
    for i in range(40):
        doc.h2(f"قسم رقم {i}")
        doc.p("نص تجريبي طويل " * 12)
    doc.save()
    assert out.exists() and out.stat().st_size > 5000

    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(out))
    assert len(pdf) > 3, "content that long must span several pages"


def test_table_and_note_render(tmp_path):
    out = tmp_path / "t.pdf"
    doc = RtlDoc(str(out), AR)
    doc.cover("ع", "و", "س", [])
    doc.table(
        ["الشرح", "القيمة", "الإعداد"],
        [["نص طويل نسبيا لاختبار اللف داخل الخلية", "0.5", "InpRiskPercent"]],
        [4, 1, 2],
        ltr_cols=(2,),
    )
    doc.note("تنبيه", "نص التنبيه هنا مع XAUUSD ورقم 2%")
    doc.save()
    assert out.stat().st_size > 3000


def test_code_block_rejects_arabic(tmp_path):
    """A code block is drawn verbatim, so Arabic in one renders scrambled with
    no error. It must fail loudly instead."""
    doc = RtlDoc(str(tmp_path / "c.pdf"), AR)
    doc.cover("ع", "و", "س", [])
    doc.code([r"C:\Users\me\MQL5\Experts\GoldBot_M5.mq5"])  # fine
    with pytest.raises(ValueError, match="right-to-left"):
        doc.code(["انسخ الملف هنا"])


def test_code_block_allows_paths_and_log_lines(tmp_path):
    doc = RtlDoc(str(tmp_path / "c2.pdf"), AR)
    doc.cover("ع", "و", "س", [])
    doc.code([
        "GoldBot M5 started on XAUUSD | server GMT offset +3 h |",
        "Session 07:00-16:00 GMT  =  10:00-19:00 server time",
    ])
    doc.save()
