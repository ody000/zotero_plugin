"""FastAPI routes (GET /health, POST /highlight) and the JSON contract models.

POST /highlight logs the request to server/.cache/requests.jsonl, then runs pipeline.run()
(parse -> sentences -> Scim rules -> density/caps -> rectangles) and returns the highlights.

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

from pipeline import SCORER_NAME, run

CACHE_DIR = Path(__file__).parent / ".cache"
REQUEST_LOG = CACHE_DIR / "requests.jsonl"


# -------------------- The JSON contract
# Keep in sync with plugin/src/modules/highlighter.ts and docs/api-contract.md.

Facet = Literal["contribution", "method", "result"]


class HighlightRequest(BaseModel):
    pdf_path: str
    density: float = Field(0.15, ge=0.05, le=0.20)  # fraction of body sentences
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
    n_sentences: int = 0  # body sentences after parsing (abstract and references excluded)
    n_candidates: int = 0  # sentences passing Scim's score threshold
    n_selected: int = 0  # after the density budget and per-section cap
    ms: int = 0


class HighlightResponse(BaseModel):
    highlights: list[Highlight]
    scorer: str
    cached: bool = False
    stats: Stats = Field(default_factory=Stats)


# ------------------------------------- routes

app = FastAPI(title="Paper highlighter")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "scorer": SCORER_NAME}


@app.post("/highlight", response_model=HighlightResponse)
def highlight(request: HighlightRequest) -> HighlightResponse:
    started = time.perf_counter()
    pdf = Path(request.pdf_path)
    if not pdf.is_file():
        raise HTTPException(status_code=404, detail=f"PDF not found: {request.pdf_path}")

    store_request(request, pdf)
    try:
        result = run(str(pdf), request.density)
    except ValueError as error:  # pdf.py's "encrypted" / "too many pages" / "no text extracted"
        raise HTTPException(status_code=422, detail=str(error)) from error

    return HighlightResponse(
        highlights=[Highlight(**h) for h in result["highlights"]],
        scorer=SCORER_NAME,
        stats=Stats(**result["stats"], ms=round((time.perf_counter() - started) * 1000)),
    )


# ----------------------------- Helper functions below

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
