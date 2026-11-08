# ShelfSense

AI retail shelf auditor. Upload a shelf photo and a planogram (the expected layout).
A vision LLM (via OpenRouter) reads the shelf, and Python code scores compliance.

**Status: Day 1 of 5.** FastAPI skeleton and a first OpenRouter vision call. Planogram
scoring, UI, history and deployment come in later days.

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

| Status | Meaning |
|---|---|
| 400 | Wrong file type, fake image, or larger than 5 MB |
| 500 | `OPENROUTER_API_KEY` or `OPENROUTER_MODEL` is not set |
| 502 | OpenRouter failed or returned an unusable reply |

## Tests

```bash
pytest
```

Tests make no network calls; the OpenRouter call is replaced with a fake.

## Project layout

```
app/
  main.py      routes: /health, /describe
  config.py    settings from .env
  vision.py    OpenRouter call
tests/
  test_health.py
```

## Roadmap

See `SHELFSENSE_ROADMAP.md`.
