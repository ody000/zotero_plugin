"""Sentence scorers behind one function: TextRank placeholder/baseline and the Claude call + prompt (glossary function later).

Implemented so far: a ranking of every sentence, in two tiers.
  1. Scim's rules, ported from the released Scim code (github.com/rayfok/scim,
     model/sse_skimming/__main__.py, Apache-2.0): only sentences where the authors speak
     ("we", "this paper") score; cue words and the section add points. Typical scores 0.8-5.
  2. Extra rules for the other sentences (not Scim): reported findings in results-like sections,
     and topic sentences (first in a paragraph). Capped at 0.75, so they always rank below a
     sentence Scim itself would highlight, and only fill the budget once Scim's picks run out.
The MiniLM bonus Scim adds on top is left out for now (marked in _score).
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # type hints only: pipeline.py will import this module, so no runtime import back
    from pipeline import Sentence

SCIM_THRESHOLD = 0.8  # Scim's own cut-off; kept for reference, since we rank everything and take the top share
_FINDING_POINTS = 0.5  # tier 2: a reported finding (statistics or result wording) without "we"
_TOPIC_POINTS = 0.25  # tier 2: first sentence of a paragraph (Edmundson's location heuristic)
_PARAGRAPH_DISCOUNT = 0.1  # -0.1 for each earlier author-voice sentence in the same paragraph
_MIN_WORDS, _MAX_WORDS = 5, 80  # Scim skips sentences outside this length (run-in headings, run-ons)

# Sections where objective / novelty cues count (substring match, see _in_section).
_FRONT_SECTIONS = ("abstract", "introduction", "related work", "recent work", "conclusion")

# Keyword lists copied from Scim's word_bank.py. Scim matches them against spaCy lemmas;
# we match regex word families instead (_family), so no tokenizer or language model is needed.
_AUTHOR = ("we", "us", "our", "this article", "this paper", "this work", "this study", "this project")
_OBJECTIVE = ("aim", "goal", "purpose", "motivation", "objective")
_CONTRIBUTION = ("contribution", "contribute")
_NOVELTY = (
    "augment", "categorise", "categorize", "contrast", "demonstrate", "develop", "differ",
    "emphasize", "explore", "extend", "first", "formalize", "hypothesize", "investigate",
    "motivate", "new", "novel", "posit", "propose", "rethink", "solve", "speculate", "summarize",
    "uncover",
)
_RESULT = (
    "achieve", "demonstrate", "exceed", "find", "highlight", "observe", "obtain", "outperform",
    "reveal", "show", "surpass", "result", "takeaway", "verify", "sota", "state-of-the-art",
    "improve", "significant",  # from Scim's newer list in weak_label/word_bank.py
)

# Section groups for the tier-2 rules (substring match on the heading).
_RESULT_SECTIONS = ("result", "experiment", "evaluation", "discussion", "conclusion")
_METHOD_SECTIONS = ("method", "approach", "setup")

# Reported statistics: "p < 0.05", "43 ± 5%", "r = 0.8", "12.5%".
_STATISTICS_RE = re.compile(r"\bp\s*[<=>≤]\s*0?\.\d|±|\d\s*%|\b[rR]2?\s*=\s*-?0?\.\d")

_NO_INFLECTION = {"new", "novel", "first", "sota", "state-of-the-art"}  # "news" must not match "new"
_IRREGULAR = {"find": ("found",), "show": ("shown",)}


def _family(word: str) -> str:
    """Regex alternatives for a keyword and its inflections, standing in for spaCy's lemma match.

    propose -> propose|proposes|proposed|proposing;  verify -> verify|verifies|verified|verifying
    """
    if word in _NO_INFLECTION:
        forms = [word]
    elif word.endswith("e"):
        forms = [word, word + "s", word + "d", word[:-1] + "ing"]
    elif word.endswith("y") and word[-2] not in "aeiou":
        forms = [word, word[:-1] + "ies", word[:-1] + "ied", word + "ing"]
    else:
        forms = [word, word + "s", word + "es", word + "ed", word + "ing"]
    return "|".join(re.escape(form) for form in forms + list(_IRREGULAR.get(word, ())))


def _matcher(words: Iterable[str], inflect: bool = True) -> re.Pattern[str]:
    alternatives = "|".join(_family(w) if inflect else re.escape(w) for w in words)
    return re.compile(rf"\b(?:{alternatives})\b")


_AUTHOR_RE = _matcher(_AUTHOR, inflect=False)  # exact phrases, as in Scim
_OBJECTIVE_RE = _matcher(_OBJECTIVE)
_CONTRIBUTION_RE = _matcher(_CONTRIBUTION)
_NOVELTY_RE = _matcher(_NOVELTY)
_RESULT_RE = _matcher(_RESULT)


@dataclass
class Selection:
    sentence_id: int
    facet: str  # contribution | method | result; drives the highlight color
    label: str  # Scim's finer facet: objective | novelty | method | result
    score: float
    why: str  # rules that fired, e.g. "author voice + result cue + introduction"; goes in the annotation comment


def score_sentences(sentences: Iterable[Sentence]) -> list[Selection]:
    """Rank every sentence that any rule fires on (score > 0), best first.

    Sentences must be in reading order (the paragraph discount and topic-sentence rule look
    at earlier sentences). pipeline.select() then takes the top share of this ranking.
    """
    seen_in_paragraph: dict[int, int] = defaultdict(int)
    previous_paragraph = None
    ranked = []
    for s in sentences:
        is_topic = s.paragraph_id != previous_paragraph
        previous_paragraph = s.paragraph_id
        sel = _score(s, seen_in_paragraph) or _score_tier2(s, is_topic)
        if sel and sel.score > 0:
            ranked.append(sel)
    return sorted(ranked, key=lambda sel: (-sel.score, sel.sentence_id))


def _usable(sentence: Sentence, text: str) -> bool:
    # Scim checks "acknowledgement", which misses the US spelling "acknowledgments"; "acknowledg" catches both.
    return (_MIN_WORDS <= len(text.split()) <= _MAX_WORDS
            and "github.com" not in text and not _in_section(sentence, ("acknowledg",)))


def _score(sentence: Sentence, seen_in_paragraph: dict[int, int]) -> Selection | None:
    """Tier 1: Scim's rules. None if the sentence isn't eligible for them."""
    text = sentence.text.lower()
    if not _usable(sentence, text):
        return None
    # The gate: without the authors' own voice, Scim never highlights a sentence.
    if not _AUTHOR_RE.search(text):
        return None

    score = 1.0 - _PARAGRAPH_DISCOUNT * seen_in_paragraph[sentence.paragraph_id]
    seen_in_paragraph[sentence.paragraph_id] += 1
    labels: list[str] = []
    why = ["author voice"]

    # Each cue adds a point. The FIRST label added becomes the facet, so this order is Scim's priority.
    front = _in_section(sentence, _FRONT_SECTIONS)
    if front and _OBJECTIVE_RE.search(text):
        labels.append("objective")
        why.append("objective cue")
    if _in_section(sentence, ("abstract", "introduction", "conclusion")) and _CONTRIBUTION_RE.search(text):
        labels.append("novelty")
        why.append("contribution cue")
    result_cue = bool(_RESULT_RE.search(text))
    if (result_cue or _in_section(sentence, ("result",))) and not _in_section(sentence, ("method", "approach", "setup")):
        labels.append("result")
        why.append("result cue" if result_cue else "results section")
    if front and _NOVELTY_RE.search(text):
        labels.append("novelty")
        why.append("novelty cue")
    score += len(labels)

    if _in_section(sentence, ("abstract", "introduction")):
        score += 2
        why.append("abstract/introduction")

    # (Scim adds up to +0.1 x MiniLM probability here when the model agrees with labels[0]; added later.)

    label = labels[0] if labels else "method"  # author voice but no cue: Scim calls it a method sentence
    facet = "contribution" if label in ("objective", "novelty") else label
    return Selection(sentence.id, facet, label, round(score, 3), " + ".join(why))


def _score_tier2(sentence: Sentence, is_topic: bool) -> Selection | None:
    """Tier 2 (not Scim): sentences without "we" that still carry a finding or open a paragraph."""
    text = sentence.text.lower()
    if not _usable(sentence, text):
        return None
    in_results = _in_section(sentence, _RESULT_SECTIONS)
    in_methods = _in_section(sentence, _METHOD_SECTIONS)
    score, why = 0.0, []
    if in_results and (_STATISTICS_RE.search(text) or _RESULT_RE.search(text)):
        score += _FINDING_POINTS
        why.append("reported finding")
    # Topic sentences only in the body: in the introduction they are mostly background.
    if is_topic and (in_results or in_methods):
        score += _TOPIC_POINTS
        why.append("topic sentence")
    if not why:
        return None
    label = "method" if in_methods and not in_results else "result"
    return Selection(sentence.id, label, label, score, " + ".join(why))


def _in_section(sentence: Sentence, names: Iterable[str]) -> bool:
    # Substring match on the lowercased heading, as in Scim: "result" matches "4. Results and Discussion".
    heading = (sentence.section or "").lower()
    return any(name in heading for name in names)


if __name__ == "__main__":
    # Quick look: python scoring.py paper.pdf  ->  ranked sentences, best first
    from pdf_parsing.pdf import extract_structured
    from pipeline import split_sentences

    sentences = split_sentences(extract_structured(sys.argv[1]))
    by_id = {s.id: s for s in sentences}
    selections = score_sentences(sentences)
    print(f"{len(selections)} of {len(sentences)} sentences ranked")
    for sel in selections:
        s = by_id[sel.sentence_id]
        print(f"[{s.id}] {sel.score:.1f} {sel.label:9} | {s.section} | {sel.why} | {s.text}")
