# ShelfSense

AI retail shelf auditor. Upload a shelf photo and a planogram (the expected layout).
A vision LLM (via OpenRouter) reads the shelf, and plain Python code compares it with the
planogram and computes a compliance score, gaps and misplaced items.

**Status: Day 3 of 5.** FastAPI backend, structured detection with retry, planogram
compliance engine, and a simple website. History storage, model comparison, evaluation
and deployment come next.

## Setup

```bash
python -m venv .venv
# Windows (PowerShell):  .venv\Scripts\Activate.ps1
# macOS/Linux:           source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in:
- `OPENROUTER_API_KEY`: your key from openrouter.ai
- `OPENROUTER_MODEL`: a vision-capable model id from https://openrouter.ai/models
  (filter by image input; free models end in `:free`, but names change often)

`.env` is git-ignored. Never commit your key.

## Run

```bash
python -m uvicorn app.main:app --reload
```

- Website: http://127.0.0.1:8000/
- API docs (Swagger): http://127.0.0.1:8000/docs

## Website

Open `/`, choose a shelf photo, edit the planogram JSON, and click **Run audit**.
The result page shows the compliance score, what to fix, per-row scores, gaps,
misplaced items, and what the model saw.

## The planogram

A planogram says what each shelf row should hold. `facings` is how many units of a
product stand side by side at the front. `aliases` are optional other names the model
might use for the same product.

```json
{
  "store": "Store-101",
  "shelf": "Beverages-A",
  "rows": [
    {
      "row": 1,
      "expected": [
        {"product": "Coca-Cola 500ml", "facings": 6, "aliases": ["Coke 500ml"]},
        {"product": "Pepsi 500ml", "facings": 4}
      ]
    },
    {"row": 2, "expected": [{"product": "Sprite 500ml", "facings": 5}]}
  ]
}
```

## How scoring works

All scoring is plain Python in `app/compliance.py`, so it is deterministic and explainable.

1. Each product the model saw is matched to a planogram product by fuzzy name matching
   (case, punctuation and "500 ml" vs "500ml" are ignored; similarity must be at least 88/100,
   so "Pepsi" is not confused with "Pepsi Max"). Aliases cover nicknames.
2. Each match is classified:
   - **in place**: found in the row it belongs to
   - **misplaced**: found in a different row than the planogram says
   - **unexpected**: matches nothing in the planogram
3. **Gaps** are computed from the planogram: expected facings minus facings found in place.
   (The model's own "empty gap" guess is not used, because without a planogram it cannot
   know what should be there.)
4. **Row score** = correct facings / expected facings x 100, minus 5 points for each
   misplaced or unexpected item found in that row. Extra units earn no extra credit.
5. **Overall score** = row scores weighted by each row's expected facings.

## API

| Endpoint | What it does |
|---|---|
| `GET /health` | Liveness check |
| `POST /describe` | Photo in, plain-text description out |
| `POST /detect` | Photo in, validated structured shelf JSON out |
| `POST /score` | Hand-written detected JSON + planogram in, audit out. No image, no model call, free |
| `POST /audit` | Photo + planogram in, detected shelf + audit out |

```bash
curl.exe -X POST http://127.0.0.1:8000/audit -F "file=@shelf.jpg;type=image/jpeg" -F "planogram=@planogram.json"
```

`/detect` asks the model for JSON only and to count facings one by one. The reply is
validated against the Pydantic models in `app/schemas.py`. If it is invalid, the model is
shown its own reply plus the error and asked again, up to 3 attempts.

| Status | Meaning |
|---|---|
| 400 | Wrong file type, fake image, larger than 5 MB, or invalid planogram |
| 500 | `OPENROUTER_API_KEY` or `OPENROUTER_MODEL` is not set |
| 502 | OpenRouter failed, or the reply stayed invalid after 3 attempts |

## Limitations

Vision models estimate counts. Expect results to be off by a unit or two on crowded or
low-resolution photos, and expect generic product names when labels are unreadable.
Use sharp, front-on photos and add aliases to the planogram for best results.

## Tests

```bash
python -m pytest -q
```

Tests make no network calls; the OpenRouter call is replaced with a fake.

## Project layout

```
app/
  main.py         API routes and website routes
  config.py       settings from .env
  schemas.py      Pydantic models: detected shelf, planogram, audit result
  vision.py       OpenRouter calls, JSON parsing, retry
  compliance.py   name matching and scoring
  templates/      HTML pages (Jinja2)
  static/         CSS
tests/
  test_health.py
  test_schemas.py
  test_detect.py
  test_compliance.py
  test_web.py
```

## Roadmap

See `SHELFSENSE_ROADMAP.md`.
