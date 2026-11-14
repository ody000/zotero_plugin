Python backend (FastAPI), flat layout — no package. Already set up with uv (fastapi, uvicorn).

Run (from this folder):  uv run uvicorn app:app --port 8765
Check:                   curl localhost:8765/health

Right now `app.py` is a stub: it stores each request from the plugin in `.cache/requests.jsonl` and returns one hard-coded highlight.
Add dependencies as the other files get written: `uv add pysbd sumy anthropic python-dotenv` (PyMuPDF already added, pinned below 2.0 for Intel Macs), `uv add --dev pytest`.
