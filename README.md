# ShelfSense

AI retail shelf auditor. Upload a shelf photo (or capture one with your camera), describe how the
shelf **should** look (the planogram), and ShelfSense tells you how closely the real shelf matches,
what is missing or misplaced, and what to do about it. Every audit is saved, so you can come back to it.

A vision model (via OpenRouter) reads the photo. **Plain Python code** scores it against the planogram,
so the score is deterministic and explainable.

## Features

- **Any retail category:** grocery, beverages, clothing, cosmetics, electronics, pharmacy, household, or your own.
  The category and planogram tell the model what to look for; they never limit what it can report.
- **Structured detection:** for every item the model returns name, category, quantity, row, position,
  bounding box, confidence and visual attributes. Items it is unsure of are kept apart as *uncertain*.
- **Honest scoring:** `compliance = correct facings / expected facings x 100`. If detection fails, the audit is
  marked *incomplete* with no score, instead of a misleading 0%.
- **Two input modes:** upload (with drag and drop) or live camera snapshot (start, stop, switch, capture, retake, confirm).
- **Audit history:** SQLite database, dashboard statistics and charts, search and filters.
- **Manual corrections:** fix names, quantities and rows, assign an item to a planogram product, ignore a wrong
  detection, add a missed one, then re-run the comparison instantly (no extra AI call).
- **Saved to-do list:** tasks are generated from the result, prioritised, and your ticks are stored in the database.

## Setup

```bash
python -m venv .venv
# Windows (PowerShell):  .venv\Scripts\Activate.ps1
# macOS/Linux:           source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in:

| Setting | Meaning |
|---|---|
| `OPENROUTER_API_KEY` | Your key from openrouter.ai |
| `OPENROUTER_MODEL` | A vision-capable model id from https://openrouter.ai/models |
| `DATA_DIR` | Where the database and photos are stored (default `data`) |
| `CONFIDENCE_THRESHOLD` | Detections below this (0 to 1, default `0.5`) are *uncertain* and not counted |
| `MAX_IMAGE_SIDE` | Photos are shrunk to this many pixels on the long side (default `1600`) |
| `ACCESS_PASSWORD` | If set, every page asks for this password (any username). **Set it for any public deployment.** |
| `MAX_AUDITS_PER_DAY` | Cap on AI runs per day (`0` = unlimited), to protect your OpenRouter credit |
| `DETECT_PASSES` | `1` (default) to `5`: how many times the AI looks at each photo, in parallel. The middle result is used and disagreement lowers the trust level. Costs that many times as much |
| `DETAIL_LEVEL` | `full` (default), `lean` or `minimal`: how much the AI is asked to report. Less is faster; `minimal` has no boxes on the photo |
| `VERIFY_UNCERTAIN` | `true` (default): take a zoomed-in second look at uncertain items |
| `LOG_LEVEL` | `INFO` (default), `DEBUG`, ... |

`.env` and `data/` are git-ignored. Never commit your key.

## Run

```bash
python -m uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/ in your browser.

| Page | What it is |
|---|---|
| `/` | Dashboard: statistics, charts, recent audits |
| `/new` | New audit: category, photo (upload or camera), planogram |
| `/audits` | History with search and filters |
| `/audits/{id}` | One audit: live progress, result, corrections, to-do list |
| `/docs` | Interactive API docs |

The camera needs a secure context: it works on `http://localhost` and on any `https://` page.

## How an audit works

1. **Photo:** validated (type, size, real image content), rotated upright from phone metadata, shrunk, and
   checked for sharpness and resolution. Blurry or small photos get a warning.
2. **Detection:** the photo, the category and the planogram go to the vision model. It reports what it sees and,
   for each group, which planogram product it is (if any), with its own confidence.
3. **Matching:** a manual assignment wins, then the model's planogram match, then fuzzy name matching
   (aliases supported; "Pepsi" is not confused with "Pepsi Max").
4. **Scoring:** correct, missing, over-stocked, misplaced and unexpected items; the score is weighted by facings.
5. **Status:** `completed`, or `needs_review` when no honest score is possible (nothing detected, the photo is
   unusable, nothing matches the planogram, or more than 30% of what was seen is uncertain). `failed` means the AI
   service errored; `processing` means it is still running.

### Why a score can be withheld

A failed or unsure detection says nothing about the shelf. Earlier versions string-matched the model's
free-text names against the planogram ("Doritos bags" vs "Chips bag") and reported 0 detected and every product
missing. Now the model is told the planogram, resolves names itself, and anything it is unsure of is shown for
review instead of being counted or declared missing.

## The planogram

```json
{
  "store": "Store-101",
  "shelf": "Apparel-A",
  "rows": [
    {
      "row": 1,
      "expected": [
        {"product": "Shirt", "facings": 6, "aliases": ["t-shirt"], "category": "tops"},
        {"product": "Jeans", "facings": 5}
      ]
    }
  ]
}
```

Use any product names and categories. `facings` is how many units stand side by side at the front (for clothing:
visible garments or stacks). `aliases` and `category` are optional.

## API

Stored audits (JSON):

| Endpoint | What it does |
|---|---|
| `GET /api/audits` | List with `q`, `status`, `category`, `date_from`, `date_to`, `page`, `page_size` |
| `GET /api/audits/{id}` | One audit, including result and tasks |
| `POST /api/audits/{id}/tasks/{task_id}` | `{"done": true}`: tick a task (saved) |
| `POST /api/audits/{id}/corrections` | Apply corrections and recompute |
| `GET /api/stats` | Total, completed, needs attention, processing |
| `GET /api/categories` | Supported categories with sample planograms |

Stateless endpoints (kept for compatibility, nothing is stored): `GET /health`, `POST /describe`, `POST /detect`,
`POST /score`, `POST /audit`.

## Project layout

```
app/
  main.py        app setup, stateless API
  routes.py      pages and stored-audit API
  pipeline.py    background audit job, corrections
  vision.py      prompts, OpenRouter call, parsing, retry
  compliance.py  matching and scoring
  imaging.py     photo validation, rotation, resizing, sharpness
  categories.py  retail categories and sample planograms
  db.py          SQLite storage
  schemas.py     data models
  uploads.py     request helpers
  web.py         template setup
  templates/     HTML pages
  static/        style.css, app.js (new audit page), result.js (audit page)
tests/           unit, scenario and page tests
data/            database and photos (created at runtime, git-ignored)
```

## Tests

```bash
python -m pytest -q
```

The suite makes no network calls. The AI is replaced by a scripted fake that returns prepared replies; everything
after it (parsing, validation, matching, scoring, database, pages) is the real code. `tests/test_scenarios.py`
covers the ten required scenarios end to end.

## Deploying

The app needs a **disk that persists** (SQLite database and saved photos) and a process that keeps running
(audits run in the background). A plain serverless host such as Vercel does not provide either, so use a
host with a persistent volume (Render, Railway, Fly.io, or any VPS).

```bash
docker build -t shelfsense .
docker run -p 8000:8000 -v shelfsense-data:/data \
  -e OPENROUTER_API_KEY=... -e OPENROUTER_MODEL=anthropic/claude-haiku-5.5 \
  -e ACCESS_PASSWORD=choose-a-password -e MAX_AUDITS_PER_DAY=50 \
  shelfsense
```

Before exposing it to the internet:
- Set `ACCESS_PASSWORD`, otherwise anyone who finds the address can run audits on **your** OpenRouter credit
  and see every stored photo.
- Set `MAX_AUDITS_PER_DAY` and a spending limit on your OpenRouter key.
- Serve it over HTTPS (your host usually does this). The camera only works on HTTPS or localhost.

The Dockerfile and the GitHub Actions workflow (`.github/workflows/tests.yml`, which runs the tests on every
push) have not been run on a real Docker host or on GitHub yet, so check the first build.

## Reports

A scored audit can be downloaded as a CSV (summary, planogram vs shelf, to-do list), and the audit page prints
as a clean one-page report (use the browser's Print). An audit and its photo can be deleted from its page.

## Measuring and tuning accuracy, speed and errors

You cannot train the model through OpenRouter, but you can **test the available models and settings and
keep the best one**. `evaluation/` runs the real pipeline over photos you have counted by hand and reports
count accuracy, recall, false products, how far the percentage is from the true percentage, failures,
and seconds per audit, for several models and detail levels side by side:

```bash
python -m evaluation.run_eval --cases evaluation/cases --configs evaluation/configs.example.json --repeats 2
```

See `evaluation/README.md` for how to label photos. Every stored audit also records how long it took and
how many tokens it used (shown on the audit page).

## Limitations

- Vision models estimate counts and positions. Expect errors of a unit or two on crowded or low-resolution photos,
  and treat boxes as approximate. Confidence is the model's own estimate, not a measured accuracy.
- There are no user accounts: anyone who can reach the server can see its audits.
- Audits live in a local SQLite file and local photo folder. A host with an ephemeral disk (such as Vercel
  serverless functions) will lose them and does not suit the background audit job; use a host with a persistent
  disk or swap `app/db.py` for a hosted database.

## Roadmap

See `SHELFSENSE_ROADMAP.md`.
