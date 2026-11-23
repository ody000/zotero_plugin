from pdf import _Line, _group_sections, _is_equation, _join_wrapped, _sort_reading_order

"""
run with: uv run pytest -v
"""
def make_line(
    text: str,
    *,
    page: int = 0,
    block: int = 0,
    block_start: bool = True,
    x0: float = 72.0,
    y0: float = 100.0,
    x1: float = 300.0,
    y1: float = 112.0,
    size: float = 10.0,
    bold: bool = False,
) -> _Line:
    return _Line(page, block, block_start, x0, y0, x1, y1, text, size, bold, 612.0, 792.0)


def test_join_wrapped_hyphens():
    assert _join_wrapped(["classi-", "fier"]) == "classifier"
    assert _join_wrapped(["well-", "known method"]) == "well-known method"


def test_is_equation():
    assert _is_equation("x = (a + b) / c")
    assert _is_equation("(12)")
    assert not _is_equation("We set learning rate x = 0.01 during training.")


def test_two_column_reading_order():
    lines = [
        make_line("Title spanning both columns", x0=72, x1=540, y0=80, y1=96),
        make_line("L1", x0=72, x1=280, y0=120, y1=132),
        make_line("R1", x0=330, x1=540, y0=120, y1=132),
        make_line("L2", x0=72, x1=280, y0=140, y1=152),
        make_line("R2", x0=330, x1=540, y0=140, y1=152),
        make_line("L3", x0=72, x1=280, y0=160, y1=172),
        make_line("R3", x0=330, x1=540, y0=160, y1=172),
    ]
    ordered = [ln.text for ln in _sort_reading_order(lines, page_w=612.0)]
    assert ordered == ["Title spanning both columns", "L1", "L2", "L3", "R1", "R2", "R3"]


def test_group_sections_skips_abstract_and_stops_at_references():
    lines = [
        make_line("Abstract", size=12.0, bold=True),
        make_line("This abstract sentence should be skipped entirely."),
        make_line("1. Introduction", size=12.0, bold=True),
        make_line("First body sentence is kept here."),
        make_line("References", size=12.0, bold=True),
        make_line("[1] Smith et al. Should not appear."),
    ]
    sections = _group_sections(lines, body_size=10.0)
    assert sections == [("1. Introduction", ["First body sentence is kept here."])]