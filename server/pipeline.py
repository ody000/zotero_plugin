"""Runs one request end to end: parse, split sentences, rank, take the top density share, draw rectangles.

run() is the entry point app.py calls:
  pdf.py paragraphs -> sentences (each linked to its PDF words) -> scoring.py ranking
  -> select() (top density share, spread across sections and paragraphs) -> page rectangles.
The abstract never reaches this file (pdf.py skips it). Not yet: the cache by PDF hash.
"""

from __future__ import annotations

import math
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

import pymupdf
import pysbd

from pdf_parsing.pdf import extract_structured
from scoring import Selection, score_sentences

SCORER_NAME = "scim-rules"  # reported to the plugin; changes when the MiniLM is added

_segmenter = pysbd.Segmenter(language="en", clean=False, char_span=True)

# Symbol-font glyphs PyMuPDF can't decode come out as control characters
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# How far past the cursor a word may sit in the paragraph text (spaces, undecodable symbols).
_SEARCH_SLACK = 8

# select(): a section may hold at most 1.2 x its fair share of highlights (density x its size) ...
_SECTION_SLACK = 1.2
# ... and each earlier pick in a paragraph costs a candidate this much (about one cue word),
# so uncovered paragraphs win close calls (Scim paper 5.3: prefer paragraphs without highlights).
_SAME_PARAGRAPH_PENALTY = 1.0


@dataclass
class Sentence:
    id: int  # unique across the paper; scoring and selection refer to sentences only by this id
    section: str | None  # top-level heading, e.g. "Introduction"; None if no headings
    paragraph_id: int
    text: str
    # PDF words this sentence covers, in reading order: {"text", "bbox", "page", "line"}.
    # bbox is in PyMuPDF page coordinates (points, origin top-left); _highlights() flips it for Zotero.
    words: list[dict] = field(default_factory=list)


def run(pdf_path: str, density: float) -> dict:
    """Highlights for one PDF: {"highlights": [Highlight dicts, see app.py], "stats": {...}}."""
    sentences = split_sentences(extract_structured(pdf_path))
    ranked = score_sentences(sentences)
    chosen = select(ranked, sentences, density)
    with pymupdf.open(pdf_path) as doc:
        highlights = _highlights(chosen, sentences, doc)
    stats = {"n_sentences": len(sentences), "n_candidates": len(ranked), "n_selected": len(chosen)}
    return {"highlights": highlights, "stats": stats}


def split_sentences(doc: dict) -> list[Sentence]:
    """Split every paragraph of extract_structured() output into sentences with their words."""
    sentences: list[Sentence] = []
    for section in doc["sections"]:
        for para in section["paragraphs"]:
            # Strip undecodable symbols first: inside inline math they make pysbd split mid-formula.
            para_text = _CONTROL_CHARS.sub("", para["text"])
            # "line" (index within the paragraph) lets _highlights() draw one rectangle per text line.
            words = [
                dict(word, page=line["page"], line=i)
                for i, line in enumerate(para["lines"])
                for word in line["words"]
            ]
            spans = _word_spans(para_text, words)
            for seg in _segmenter.segment(para_text):
                text = seg.sent.strip()
                if not text:
                    continue
                # A word belongs to every sentence its characters overlap. Normally that's exactly one;
                # a word split by a line-break hyphen ("classi-" / "fier") gives two pieces, both inside it.
                covered = [w for w, (start, end) in zip(words, spans) if start < seg.end and end > seg.start]
                sentences.append(Sentence(len(sentences), section["heading"], para["id"], text, covered))
    return sentences


def _word_spans(text: str, words: list[dict]) -> list[tuple[int, int]]:
    """Locate each PDF word's character range in the paragraph text.

    The paragraph text is not a plain join of the words. pdf.py normalizes characters, rejoins
    hyphenated line breaks ("classi-" + "fier" -> "classifier"), and keeps symbols the word list
    drops. So instead of counting characters we walk forward and search just past the cursor.
    A word that can't be found gets (-1, -1) and is left out of every sentence.
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    for word in words:
        token = unicodedata.normalize("NFKC", word["text"]).replace("­", "")
        # Second try without the trailing hyphen, for words whose line-break hyphen was removed.
        for candidate in dict.fromkeys((token, token.rstrip("-"))):
            if not candidate:
                continue
            start = text.find(candidate, cursor, cursor + len(candidate) + _SEARCH_SLACK)
            if start != -1:
                spans.append((start, start + len(candidate)))
                cursor = start + len(candidate)
                break
        else:
            spans.append((-1, -1))
    return spans


def select(ranked: list[Selection], sentences: list[Sentence], density: float) -> list[Selection]:
    """Take the top `density` share of the ranking, spread across sections and paragraphs.

    budget = density x number of body sentences. Two spreading rules on top of the ranking:
    - proportional cap: a section holds at most _SECTION_SLACK x density x its own sentence count
      (a 127-sentence Methods section gets more room than a 3-sentence Conclusion);
    - paragraph penalty: each pick already made in a paragraph lowers its other candidates.
    Changing density only re-slices the same ranking, so it never needs re-scoring.
    """
    by_id = {s.id: s for s in sentences}
    budget = max(1, round(density * len(sentences)))
    section_sizes = Counter(s.section for s in sentences)
    cap = {sec: max(1, math.ceil(_SECTION_SLACK * density * n)) for sec, n in section_sizes.items()}

    chosen: list[Selection] = []
    per_section: Counter[str | None] = Counter()
    per_paragraph: Counter[int] = Counter()
    remaining = list(ranked)
    while remaining and len(chosen) < budget:
        # Greedy: the penalty changes as picks are made, so re-evaluate each round.
        # max() keeps the earlier (higher-ranked) candidate on ties.
        best = max(remaining, key=lambda sel: sel.score - _SAME_PARAGRAPH_PENALTY * per_paragraph[by_id[sel.sentence_id].paragraph_id])
        remaining.remove(best)
        sentence = by_id[best.sentence_id]
        if per_section[sentence.section] < cap[sentence.section]:
            chosen.append(best)
            per_section[sentence.section] += 1
            per_paragraph[sentence.paragraph_id] += 1
    return chosen


def _highlights(chosen: list[Selection], sentences: list[Sentence], doc: pymupdf.Document) -> list[dict]:
    """Turn chosen sentences into Zotero highlight dicts: one rectangle per text line, per page."""
    by_id = {s.id: s for s in sentences}

    # sortIndex orders annotations within a page. Its middle part should be a character offset;
    # the sentence's first-word position among all words on that page gives the same order.
    offsets: dict[tuple[int, int], int] = {}
    words_on_page: Counter[int] = Counter()
    for s in sentences:
        for w in s.words:
            offsets.setdefault((s.id, w["page"]), words_on_page[w["page"]])
            words_on_page[w["page"]] += 1

    highlights = []
    for sel in chosen:
        sentence = by_id[sel.sentence_id]
        lines: dict[int, dict[int, list[dict]]] = {}  # page -> line -> words
        for w in sentence.words:
            lines.setdefault(w["page"], {}).setdefault(w["line"], []).append(w)

        # A Zotero annotation lives on one page, so a sentence crossing pages becomes two highlights.
        for page_index, page_lines in lines.items():
            page = doc[page_index]
            to_pdf = ~page.transformation_matrix  # PyMuPDF (top-left origin) -> PDF (bottom-left), handles crop boxes
            rects, top = [], math.inf
            for words in page_lines.values():
                box = pymupdf.Rect(
                    min(w["bbox"][0] for w in words), min(w["bbox"][1] for w in words),
                    max(w["bbox"][2] for w in words), max(w["bbox"][3] for w in words),
                )
                top = min(top, box.y0)
                pdf_box = box * to_pdf
                rects.append([round(v, 2) for v in (pdf_box.x0, pdf_box.y0, pdf_box.x1, pdf_box.y1)])

            offset = min(offsets[(sentence.id, page_index)], 999_999)
            text = sentence.text if len(lines) == 1 else " ".join(w["text"] for ws in page_lines.values() for w in ws)
            highlights.append({
                "sentence_id": sentence.id,
                "page_index": page_index,
                "rects": rects,
                "sort_index": f"{page_index:05d}|{offset:06d}|{min(int(top), 99_999):05d}",
                "text": text,
                "facet": sel.facet,
                "label": sel.label,
                "why": sel.why,
            })
    return highlights


if __name__ == "__main__":
    # Quick look: python pipeline.py paper.pdf  ->  one sentence per line
    for s in split_sentences(extract_structured(sys.argv[1])):
        pages = sorted({w["page"] + 1 for w in s.words})
        print(f"[{s.id}] {s.section} | para {s.paragraph_id} | p{pages} | {len(s.words)} words | {s.text}")
