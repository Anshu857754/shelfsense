# Evaluating ShelfSense

This folder measures how well ShelfSense reads shelves, so that changes (a different model, a shorter
prompt, a smaller photo) can be judged by numbers instead of guesses.

You cannot train the model itself through OpenRouter. What you can do is **test the models and
settings that are available, and keep the best one**. That is what this does.

## 1. Add labelled photos

One folder per photo, inside `evaluation/cases/`:

```
evaluation/cases/
  clothing-wall-1/
    case.json
    photo.jpg
```

Copy `evaluation/cases/_template/case.json` and edit it. The important part is **`truth`**: what is
really on the shelf, written by a person who counted it.

```json
"truth": {
  "rows": [
    {"row": 1, "products": [{"product": "Chips bag", "facings": 5}, {"product": "Cereal box", "facings": 4}]},
    {"row": 2, "products": [{"product": "Biscuit pack", "facings": 0}]}
  ],
  "unexpected": [{"row": 2, "name": "wine bottle", "facings": 2}]
}
```

- `product` must be a product from the planogram. A product that is missing from the shelf gets `0`.
- `unexpected` lists things that are on the shelf but are not in the planogram.
- An empty shelf: give every product `0` and no `unexpected`.
- Rows are numbered from the top, starting at 1.
- Leave `truth` out and the case still gives speed and failure numbers (but no accuracy).

The more photos, and the more varied (categories, lighting, crowded and sparse shelves, blurry ones),
the more the numbers can be trusted. **Aim for at least 15 to 20 photos.** With only a few, treat
results as hints, not proof.

## 2. Choose what to compare

`evaluation/configs.example.json` lists configurations. Each has a model, a `detail` level
(`full`, `lean`, `minimal`: less detail = shorter answer = faster), an optional `max_image_side`
and `verify` (the zoomed second look).

## 3. Run it

```bash
python -m evaluation.run_eval --cases evaluation/cases --configs evaluation/configs.example.json
```

Useful options: `--repeats 3` (run each three times: AI answers vary), `--only haiku-5.5-lean` (one
configuration), `--parallel 4` (faster, but timings get less trustworthy).

Each run costs a little (a fraction of a cent to a few cents per audit, depending on the model).

## 4. Read the report

The table has one row per configuration (`evaluation/results/<time>.md`): failure rate, audits held
back without a score, median and slow-case seconds, tokens, and, when truth is given, count accuracy,
recall, false products, and how far the percentage is from the true percentage.

Pick by this order: **few failures and few false products first, then accurate counts, then speed.**
A configuration that is 3 seconds faster but wrong more often is not an improvement.

## What this cannot tell you

- A model can be good on your photos and worse on someone else's. Label photos that look like the ones
  you will really audit.
- Counts on folded piles and crowded racks are estimates for every model.
- With few photos, a difference of a point or two is noise.
