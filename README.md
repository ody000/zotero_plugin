# Zotero Paper Highlighter

Brown CSCI 1377 final project by Ziqi Shen and Jean Yoo. 

A Zotero plugin that pre-highlights a paper's key sentences the first time its PDF is opened, colored by facet (Contribution / Method / Result), to support skimming and quick reading. The selection rules follow Scim (Fok et al., 2023).

A PDF is parsed into sections, paragraphs, and sentences which are ranked by selection rules. The top-ranked sentences are sent to the Zotero plugin, which draws highlights on the PDF and saves them as Zotero annotations.

## Major files

| File | Role |
|---|---|
| `plugin/src/modules/highlighter.ts` | Zotero side: on PDF open, asks the server for highlights and saves them as Zotero annotations; menu items and settings. |
| `server/app.py` | Local FastAPI server (`/health`, `/highlight`) the plugin talks to. |
| `server/pdf_parsing/pdf.py` | PDF → sections, paragraphs, lines and word boxes (PyMuPDF). |
| `server/pipeline.py` | Splits sentences, takes the top density share of the ranking, turns sentences into page rectangles. |
| `server/scoring.py` | Ranks sentences with Scim's rules, plus a lower tier of extra rules. |
| `docs/design-report.md` | Design decisions, data flow, and work plan. |

Run: `cd server && uv run uvicorn app:app --port 8765 --reload`, then `cd plugin && npm start`.

## Reference

Raymond Fok, Hita Kambhamettu, Luca Soldaini, Jonathan Bragg, Kyle Lo, Marti Hearst, Andrew Head, and Daniel S. Weld. 2023. Scim: Intelligent Skimming Support for Scientific Papers. In *Proceedings of IUI '23*, 476–490. https://doi.org/10.1145/3581641.3584034
