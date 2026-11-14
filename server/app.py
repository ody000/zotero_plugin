"""FastAPI routes (GET /health, POST /highlight), JSON contract models, and stub mode.

Minimal version for now: every request from highlighter.ts is appended to
server/.cache/requests.jsonl, and the server answers with one hard-coded "stub"
highlight. That lets the plugin be tested end to end before pdf.py / scoring.py /
pipeline.py exist. Later, `highlight()` will call pipeline.run() instead.

Run (from server/):  uv run uvicorn app:app --port 8765
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

CACHE_DIR = Path(__file__).parent / ".cache"
REQUEST_LOG = CACHE_DIR / "requests.jsonl"
SCORER = "stub"  # later: read from .env ("stub" | "textrank" | "llm")


# ------------------------------------------------------------------ JSON contract
# Keep in sync with plugin/src/modules/highlighter.ts and docs/api-contract.md.

Facet = Literal["contribution", "method", "result"]


class HighlightRequest(BaseModel):
    pdf_path: str
    density: float = Field(0.10, ge=0.05, le=0.20)  # fraction of body sentences
    force: bool = False  # True = ignore the cache


class Highlight(BaseModel):
    sentence_id: int
    page_index: int  # 0-based
    rects: list[list[float]]  # [[x1, y1, x2, y2], ...] PDF points, bottom-left origin
    sort_index: str  # "PPPPP|OOOOOO|TTTTT"
    text: str
    facet: Facet
    label: str  # objective | novelty | method | result
    why: str = ""


class Stats(BaseModel):
    n_sentences: int = 0
    n_selected: int = 0
    ms: int = 0


class HighlightResponse(BaseModel):
    highlights: list[Highlight]
    scorer: str
    cached: bool = False
    stats: Stats = Field(default_factory=Stats)


# ------------------------------------------------------------------ routes

app = FastAPI(title="Paper highlighter")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "scorer": SCORER}


@app.post("/highlight", response_model=HighlightResponse)
def highlight(request: HighlightRequest) -> HighlightResponse:
    started = time.perf_counter()
    pdf = Path(request.pdf_path)
    if not pdf.is_file():
        raise HTTPException(status_code=404, detail=f"PDF not found: {request.pdf_path}")

    store_request(request, pdf)
    highlights = [stub_highlight()]

    return HighlightResponse(
        highlights=highlights,
        scorer=SCORER,
        stats=Stats(n_selected=len(highlights), ms=round((time.perf_counter() - started) * 1000)),
    )


# ------------------------------------------------------------------ helpers

def store_request(request: HighlightRequest, pdf: Path) -> None:
    """Append what the plugin sent (plus a timestamp and the file size) as one JSON line."""
    CACHE_DIR.mkdir(exist_ok=True)
    record = {
        "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **request.model_dump(),
        "pdf_bytes": pdf.stat().st_size,
    }
    with REQUEST_LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\n")


def stub_highlight() -> Highlight:
    """One box near the TOP of page 1 (US Letter is 612 x 792 pt, y measured from the bottom).

    If it shows up near the bottom of the page in Zotero, the y-axis convention is wrong.
    sort_index top = floor(792 - 714) = 78.
    """
    return Highlight(
        sentence_id=0,
        page_index=0,
        rects=[[72.0, 700.0, 540.0, 714.0]],
        sort_index="00000|000000|00078",
        text="(stub highlight near the top of page 1)",
        facet="result",
        label="result",
        why="stub response from app.py",
    )
