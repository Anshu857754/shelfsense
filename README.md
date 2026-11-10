# ShelfSense

AI retail shelf auditor. Upload a shelf photo and a planogram (the expected layout).
A vision LLM (via OpenRouter) reads the shelf, and Python code scores compliance.

**Status: Day 2 of 5.** FastAPI skeleton, OpenRouter vision call, and structured shelf
detection (Pydantic-validated JSON with automatic retry). Planogram scoring, UI, history
and deployment come in later days.

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
uvicorn app.main:app --reload
```

- Health check: http://localhost:8000/health
- Interactive docs: http://localhost:8000/docs

## Try it

```bash
curl -X POST http://localhost:8000/describe -F "file=@shelf.jpg;type=image/jpeg"
```

Returns `{"model": "...", "description": "..."}`. Only JPEG, PNG and WEBP up to 5 MB are accepted.

### Structured detection

```bash
curl -X POST http://localhost:8000/detect -F "file=@shelf.jpg;type=image/jpeg"
```

Returns the shelf as validated JSON:

```json
{
  "model": "anthropic/claude-haiku-5.5",
  "shelf": {
    "rows": [
      {
        "row": 1,
        "products": [{"name": "Coca-Cola 500ml", "facings": 6}],
        "empty_gaps": 2,
        "misplaced_items": []
      }
    ],
    "overall_notes": ""
  }
}
```

How it works: the model is prompted to reply with JSON only and to count facings one by
one. The reply is parsed and validated against the Pydantic models in `app/schemas.py`.
If it is not valid JSON or breaks the schema, the model is shown its own reply plus the
error and asked to try again, up to 3 attempts in total.

| Status | Meaning |
|---|---|
| 400 | Wrong file type, fake image, or larger than 5 MB |
| 500 | `OPENROUTER_API_KEY` or `OPENROUTER_MODEL` is not set |
| 502 | OpenRouter failed, or the reply stayed invalid after 3 attempts |

## Tests

```bash
pytest
```

Tests make no network calls; the OpenRouter call is replaced with a fake.

## Project layout

```
app/
  main.py      routes: /health, /describe, /detect
  config.py    settings from .env
  schemas.py   Pydantic models for detected shelves
  vision.py    OpenRouter calls, JSON parsing, retry
tests/
  test_health.py
  test_schemas.py
  test_detect.py
```

## Roadmap

See `SHELFSENSE_ROADMAP.md`.
