# ShelfSense: 5-Day Roadmap and Daily GitHub PR Workflow

Daily slot: 9:00 AM - 1:00 PM IST. Start: 2026-11-08.

## Roadmap

| Day | Date | Goal | Done when |
|---|---|---|---|
| 1 | Sun Nov 8 | Repo setup, FastAPI skeleton, first OpenRouter vision call | `GET /health` works; one shelf image returns a model response |
| 2 | Mon Nov 9 | Pydantic schemas, prompt design, JSON validation + retry | `POST /detect` returns validated detected-shelf JSON |
| 3 | Tue Nov 10 | Planogram format, fuzzy matching, compliance scoring, pytest | `POST /audit` returns score + gaps + misplaced; tests pass |
| 4 | Wed Nov 11 | Jinja2 UI (upload, result, history), DB storage, 2-model comparison | Full flow works in the browser; history persists |
| 5 | Thu Nov 12 | Eval on 15-20 labeled images, README, Vercel deploy, LinkedIn post | Live URL works; README has real metrics + screenshots |

## Daily PR workflow (one PR per day)

First time only:
```bash
git init
git branch -M main
# create an empty repo named shelfsense on GitHub, then:
git remote add origin https://github.com/<your-username>/shelfsense.git
# .gitignore must contain: .env, __pycache__/, .venv/
git add . && git commit -m "chore: initial project" && git push -u origin main
```

Every day:
```bash
git checkout main && git pull
git checkout -b day-N-short-name        # e.g. day-2-schemas-and-vision
# ... work, commit small and often ...
git add . && git commit -m "feat: validated vision output with retry"
git push -u origin day-N-short-name
gh pr create --title "Day N: <goal>" --body "What I built, how I tested it, screenshots"
gh pr merge --squash --delete-branch    # after checks pass (or review your own diff first)
git checkout main && git pull
```

Rules:
- Commit prefixes: `feat:`, `fix:`, `test:`, `docs:`, `chore:`.
- Never commit `.env` or your API key.
- Merge before you stop for the day, so `main` is always working.
- Day 3 onward: run `pytest` before opening the PR.
- Optional: add a GitHub Actions workflow on Day 3 that runs pytest on every PR.

## Daily checklist
1. Pull `main`, create the day branch
2. Finish the day's goal (see table)
3. Run the app and tests
4. Commit, push, open PR, merge
5. Note one thing you learned (use it in the README and LinkedIn post)
