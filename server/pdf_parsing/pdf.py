"""Extract the title, section headings, and body text from an academic PDF."""

from __future__ import annotations

import contextlib
import io
import math
import re
import statistics
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pymupdf

MAX_PAGES = 50

# Prefixes that keep their hyphen across a line wrap (e.g. "well-known").
_COMPOUND_PREFIXES = {
    "anti", "cross", "full", "high", "inter", "intra", "large", "long", "low",
    "macro", "meta", "micro", "multi", "non", "open", "over", "pseudo", "quasi",
    "self", "semi", "short", "small", "state", "super", "ultra", "under", "well",
}

_SKIP_HEADINGS = {"abstract"}
_STOP_HEADINGS = {"references", "bibliography", "works cited", "literature cited"}
_KNOWN_HEADINGS = _SKIP_HEADINGS | _STOP_HEADINGS | {
    "acknowledgements", "acknowledgments", "appendix", "background",
    "conclusion", "conclusions", "discussion", "evaluation",
    "experimental setup", "experiments", "future work", "introduction",
    "limitations", "materials and methods", "method", "methodology", "methods",
    "overview", "preliminaries", "previous work", "related work", "results",
    "results and discussion",
}

_NUMBERED_HEADING = re.compile(r"^\d{1,2}(?:\.\d{1,2}){0,3}\.?\s+\S")
_HEADING_PREFIX = re.compile(r"^(?:\d{1,2}(?:\.\d{1,2}){0,3}|[A-Z])\.?\s+")
_CAPTION = re.compile(
    r"^(?:fig(?:ure)?|table|algorithm)\.?\s*\d+[a-z]?(?:\s*[:.\-\u2013\u2014]|$)",
    re.IGNORECASE,
)
_PAGE_NUMBER = re.compile(r"^(?:page\s*)?\d{1,4}$", re.IGNORECASE)
_MATH_CHARS = set("=<>±×÷∑∫√≤≥≠≈∞∂∇∈∉⊂⊃∧∨∀∃+-*/^~")


@dataclass
class _Line:
    page: int
    block: int
    block_start: bool
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float
    bold: bool
    page_w: float
    page_h: float


def extract(pdf_path: str | Path) -> str:
    path = Path(pdf_path)
    if not path.is_file():
        raise ValueError(f"PDF not found: {path}")

    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise ValueError("PDF is encrypted")
        if doc.page_count > MAX_PAGES:
            raise ValueError(f"PDF has {doc.page_count} pages (limit: {MAX_PAGES})")
        raw_pages = [_read_page(doc[i], i) for i in range(doc.page_count)]

    # Strip headers/footers using raw geometric coordinates before column sorting.
    pages = _strip_furniture(raw_pages)
    ordered = [
        ln
        for page_lines in pages
        if page_lines
        for ln in _sort_reading_order(page_lines, page_lines[0].page_w)
    ]

    lines, body_size = _filter_body(ordered)
    doc_title, sections = _group_sections(lines, body_size)
    output = _format_markdown(doc_title, sections)
    if not output:
        raise ValueError("no text extracted")
    return output


def _read_page(page: pymupdf.Page, page_idx: int) -> list[_Line]:
    width, height = page.rect.width, page.rect.height
    with contextlib.redirect_stdout(io.StringIO()):
        table_boxes = [t.bbox for t in page.find_tables().tables if t.cells]

    lines: list[_Line] = []
    for block_idx, block in enumerate(page.get_text("dict").get("blocks", [])):
        if block.get("type") != 0:
            continue
        is_first = True
        for raw in block.get("lines", []):
            direction = raw.get("dir", (1.0, 0.0))
            if not direction or direction[0] < 0.7:
                continue

            x0, y0, x1, y1 = raw["bbox"]
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            if any(tx0 <= cx <= tx1 and ty0 <= cy <= ty1 for tx0, ty0, tx1, ty1 in table_boxes):
                continue

            text, size, bold = _parse_spans(raw.get("spans", []))
            text = unicodedata.normalize("NFKC", text)
            text = " ".join(text.replace("\u00ad", "").split())
            if not text:
                continue

            lines.append(
                _Line(
                    page=page_idx,
                    block=block_idx,
                    block_start=is_first,
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                    text=text,
                    size=size,
                    bold=bold,
                    page_w=width,
                    page_h=height,
                )
            )
            is_first = False
    return lines


def _parse_spans(spans: list[dict]) -> tuple[str, float, bool]:
    parts: list[str] = []
    size_weights: Counter[float] = Counter()
    bold_chars = 0
    total_chars = 0
    prev_x1: float | None = None

    for span in spans:
        piece = span.get("text") or ""
        if not piece:
            continue

        span_size = float(span.get("size") or 0.0)
        n_chars = len(piece.strip())
        if n_chars:
            size_weights[round(span_size * 2) / 2] += n_chars
            total_chars += n_chars
            if _is_bold(span):
                bold_chars += n_chars

        x0 = span["bbox"][0]
        if parts and prev_x1 is not None and not parts[-1].endswith(" ") and not piece.startswith(" "):
            if x0 - prev_x1 > max(span_size, 1.0) * 0.15:
                parts.append(" ")
        parts.append(piece)
        prev_x1 = span["bbox"][2]

    dominant_size = size_weights.most_common(1)[0][0] if size_weights else 0.0
    is_bold = total_chars > 0 and bold_chars > total_chars / 2
    return "".join(parts), dominant_size, is_bold


def _is_bold(span: dict) -> bool:
    if int(span.get("flags") or 0) & 16:
        return True
    font = str(span.get("font") or "").casefold()
    return any(tok in font for tok in ("bold", "black", "heavy", "semibold", "demi"))


def _strip_furniture(pages: list[list[_Line]]) -> list[list[_Line]]:
    def norm_key(text: str) -> str:
        return " ".join(re.sub(r"\d+", "#", text.casefold()).split())

    top_counts: Counter[str] = Counter()
    bottom_counts: Counter[str] = Counter()
    for lines in pages:
        top_band = [ln for ln in lines if ln.y0 < 0.13 * ln.page_h]
        bot_band = [ln for ln in lines if ln.y1 > 0.87 * ln.page_h]
        if top_band:
            top_counts[norm_key(min(top_band, key=lambda ln: ln.y0).text)] += 1
        if bot_band:
            bottom_counts[norm_key(max(bot_band, key=lambda ln: ln.y1).text)] += 1

    threshold = max(2, math.ceil(len(pages) * 0.6))
    repeated_top = {k for k, v in top_counts.items() if k and v >= threshold}
    repeated_bot = {k for k, v in bottom_counts.items() if k and v >= threshold}

    cleaned: list[list[_Line]] = []
    for lines in pages:
        kept: list[_Line] = []
        for ln in lines:
            if ln.y1 < 0.055 * ln.page_h or ln.y0 > 0.945 * ln.page_h:
                continue
            if _PAGE_NUMBER.match(ln.text):
                continue
            key = norm_key(ln.text)
            if ln.y0 < 0.13 * ln.page_h and key in repeated_top:
                continue
            if ln.y1 > 0.87 * ln.page_h and key in repeated_bot:
                continue
            kept.append(ln)
        cleaned.append(kept)
    return cleaned


def _sort_reading_order(lines: list[_Line], page_w: float) -> list[_Line]:
    gutter = _find_gutter(lines, page_w)
    if gutter is None:
        return sorted(lines, key=lambda ln: (round(ln.y0, 1), ln.x0))

    ordered: list[_Line] = []
    pending: list[_Line] = []

    def flush() -> None:
        if not pending:
            return
        left = [ln for ln in pending if (ln.x0 + ln.x1) / 2 < gutter]
        right = [ln for ln in pending if (ln.x0 + ln.x1) / 2 >= gutter]
        ordered.extend(sorted(left, key=lambda ln: (round(ln.y0, 1), ln.x0)))
        ordered.extend(sorted(right, key=lambda ln: (round(ln.y0, 1), ln.x0)))
        pending.clear()

    for ln in sorted(lines, key=lambda ln: (round(ln.y0, 1), ln.x0)):
        wide = (ln.x1 - ln.x0) > page_w * 0.62
        crosses = ln.x0 < gutter - 8 and ln.x1 > gutter + 8
        if wide or crosses:
            flush()
            ordered.append(ln)
        else:
            pending.append(ln)
    flush()
    return ordered


def _find_gutter(lines: list[_Line], page_w: float) -> float | None:
    if not lines:
        return None
    page_h = lines[0].page_h
    narrow = [
        ln
        for ln in lines
        if (ln.x1 - ln.x0) < page_w * 0.55 and 0.06 * page_h < ln.y0 < 0.94 * page_h
    ]
    if len(narrow) < 6:
        return None

    best: tuple[int, float] | None = None
    max_cross = max(2, int(0.15 * len(narrow)))
    for frac in (0.46, 0.50, 0.54):
        split = page_w * frac
        left = right = cross = 0
        for ln in narrow:
            if ln.x0 < split - 6 and ln.x1 > split + 6:
                cross += 1
            elif (ln.x0 + ln.x1) / 2 < split:
                left += 1
            else:
                right += 1
        if left >= 3 and right >= 3 and cross <= max_cross:
            score = min(left, right) - cross
            if best is None or score > best[0]:
                best = (score, split)
    return best[1] if best else None


def _filter_body(lines: list[_Line]) -> tuple[list[_Line], float]:
    caption_blocks = {
        (ln.page, ln.block)
        for ln in lines
        if ln.block_start and _CAPTION.match(ln.text)
    }
    lines = [ln for ln in lines if (ln.page, ln.block) not in caption_blocks]
    if not lines:
        return [], 0.0

    body_size = statistics.median(ln.size for ln in lines)
    kept: list[_Line] = []
    for ln in lines:
        if _parse_heading(ln, body_size) is not None:
            kept.append(ln)
        elif ln.size >= body_size * 0.8 and not _is_equation(ln.text):
            kept.append(ln)
    return kept, body_size


def _is_equation(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return False
    if re.fullmatch(r"\(?\d{1,3}\)?", compact):
        return True
    if len(re.findall(r"[A-Za-z]{4,}", text)) >= 2:
        return False
    if not (re.search(r"[α-ωΑ-Ω]", text) or any(ch in _MATH_CHARS for ch in text)):
        return False
    latin = sum(ch.isascii() and ch.isalpha() for ch in compact)
    return latin / len(compact) < 0.7


def _parse_heading(line: _Line, body_size: float) -> str | None:
    text = line.text[:-1].rstrip() if line.text.endswith(":") else line.text
    words = text.split()
    if not text or len(text) > 100 or len(words) > 12 or text[-1] in ".?!;":
        return None
    norm = _norm_heading(text)
    if not norm:
        return None

    known = norm in _KNOWN_HEADINGS
    numbered = bool(_NUMBERED_HEADING.match(text))
    larger = line.size >= body_size + 1.4
    bold = line.bold and line.size + 0.6 >= body_size
    caps = text.isupper() and len(words) <= 6

    if known and (bold or larger or caps or numbered):
        return text
    if numbered and (bold or larger or caps):
        return text
    if larger and len(words) <= 10:
        return text
    if bold and caps:
        return text
    return None


def _norm_heading(text: str) -> str:
    stripped = _HEADING_PREFIX.sub("", text)
    return " ".join(re.findall(r"[a-z0-9]+", stripped.casefold()))


def _extract_title(preamble: list[_Line], body_size: float) -> str | None:
    p0 = [ln for ln in preamble if ln.page == 0]
    if not p0:
        return None
    max_size = max(ln.size for ln in p0)
    if max_size < body_size + 1.0:
        return None
    title_lines = [ln.text for ln in p0 if ln.size >= max_size - 0.8]
    return _join_wrapped(title_lines) or None


def _group_sections(
    lines: list[_Line], body_size: float
) -> tuple[str | None, list[tuple[str | None, list[str]]]]:
    tagged: list[tuple[_Line, str | None]] = []
    for ln in lines:
        head = _parse_heading(ln, body_size)
        # Merge wrapped multi-line headings into a single heading entry.
        if head and tagged and tagged[-1][1] is not None:
            prev_ln, prev_head = tagged[-1]
            same_page = ln.page == prev_ln.page
            close_y = 0 <= (ln.y0 - prev_ln.y1) <= max(prev_ln.size, 8.0) * 1.2
            same_style = abs(ln.size - prev_ln.size) <= 0.6 and ln.bold == prev_ln.bold
            if same_page and same_style and (ln.block == prev_ln.block or close_y):
                tagged[-1] = (ln, f"{prev_head} {head}")
                continue
        tagged.append((ln, head))

    start = next(
        (
            i
            for i, (_, h) in enumerate(tagged)
            if h and (_norm_heading(h) in _KNOWN_HEADINGS or _NUMBERED_HEADING.match(h))
        ),
        None,
    )
    if start is None:
        paras = _build_paragraphs([ln for ln, _ in tagged])
        return None, ([(None, paras)] if paras else [])

    doc_title = _extract_title([ln for ln, _ in tagged[:start]], body_size)
    sections: list[tuple[str | None, list[str]]] = []
    title: str | None = None
    buf: list[_Line] = []
    skipping = False

    for ln, head in tagged[start:]:
        if head is not None:
            if not skipping and buf:
                paras = _build_paragraphs(buf)
                if paras:
                    sections.append((title, paras))
            buf = []
            norm = _norm_heading(head)
            if norm in _STOP_HEADINGS:
                break
            skipping = norm in _SKIP_HEADINGS
            if not skipping:
                title = head
        elif not skipping:
            buf.append(ln)

    if not skipping and buf:
        paras = _build_paragraphs(buf)
        if paras:
            sections.append((title, paras))
    return doc_title, sections


def _build_paragraphs(lines: list[_Line]) -> list[str]:
    if not lines:
        return []

    groups: list[list[str]] = [[lines[0].text]]
    for prev, curr in zip(lines, lines[1:]):
        same_block = prev.page == curr.page and prev.block == curr.block
        prev_ends_sent = prev.text[-1] in ".?!\"')]"
        line_h = max(prev.y1 - prev.y0, prev.size, 8.0)
        large_gap = (
            prev.page == curr.page
            and curr.y0 >= prev.y0
            and (curr.y0 - prev.y1) > line_h * 1.5
        )
        if (not same_block and prev_ends_sent) or large_gap:
            groups.append([curr.text])
        else:
            groups[-1].append(curr.text)

    paragraphs: list[str] = []
    for group in groups:
        para = _join_wrapped(group)
        if sum(any(ch.isalpha() for ch in w) for w in para.split()) >= 3:
            paragraphs.append(para)
    return paragraphs


def _join_wrapped(lines: list[str]) -> str:
    text = ""
    for line in lines:
        piece = " ".join(line.replace("\u00ad", "").split())
        if not piece:
            continue
        if not text:
            text = piece
        elif text.endswith("-") and len(text) >= 2 and text[-2].isalpha() and piece[0].islower():
            prefix = text[:-1].split()[-1]
            text = (text + piece) if prefix.casefold() in _COMPOUND_PREFIXES else (text[:-1] + piece)
        else:
            text = f"{text} {piece}"
    return text


def _format_markdown(
    doc_title: str | None, sections: list[tuple[str | None, list[str]]]
) -> str:
    blocks: list[str] = []
    if doc_title:
        blocks.append(f"# {doc_title}")
    for heading, paragraphs in sections:
        if heading:
            blocks.append(f"## {heading}")
        blocks.extend(paragraphs)
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: python pdf.py path/to/paper.pdf")
    sys.stdout.write(extract(args[0]))


if __name__ == "__main__":
    main()