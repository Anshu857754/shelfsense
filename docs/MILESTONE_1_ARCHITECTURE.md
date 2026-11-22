# ShelfSense v2 – Milestone 1: Architecture and Plan

Status: **proposal, awaiting approval. No implementation code has been written for v2.**

## 0. Starting point (what already exists)

ShelfSense v1 already runs: FastAPI + Jinja2 + vanilla JS + SQLite, an OpenRouter vision LLM as the
detector, a deterministic compliance engine (`app/compliance.py`), audit history, corrections, CSV
export, crash recovery, and 261 tests. v2 should **evolve** this, not restart it:

| Keep (already good) | Replace / add |
|---|---|
| Deterministic compliance engine and scoring rules | YOLO detector behind a `Detector` interface |
| Audit lifecycle, history, corrections, CSV, tests | Product catalog + CLIP matching |
| Pillow image prep, quality checks | PostgreSQL + Alembic (from SQLite) |
| Evaluation harness (`evaluation/`) | Admin planogram management, auth, React dashboard |

Two facts that shape everything below:
1. **Pretrained YOLO (COCO) cannot see "blue jeans" or "snack bag".** COCO has ~80 classes (person,
   bottle, cup, handbag...). A pretrained model gives *generic object boxes at best*, and often misses
   folded clothing completely. Baseline value is low until we train on shelf data (Section 6).
2. **Row numbers from vision models are unstable** (seen in v1: the same photo scored 6.6% / 16.6% / 37.6%).
   Geometry-based row assignment (cluster box y-centres) fixes this and is a real v2 improvement.

## 1. System architecture

```
 React SPA ──HTTPS/JSON──> FastAPI (api/)
                              │
        ┌─────────────────────┼───────────────────────────────┐
        │ services/ (business logic, no HTTP, no ML imports)   │
        │   audit_service  planogram_service  catalog_service  │
        └───────┬───────────────┬──────────────────┬──────────┘
                │               │                  │
        vision/ (ML)      compliance/ (pure)    agent/ (LLM)
        preprocess        compare + score       summarise only
        detector (YOLO | LLM)  rows, tasks      OpenRouter, JSON out
        recognizer (CLIP+OCR)
                │               │                  │
                └──────── PostgreSQL (+pgvector) ──┘      Object storage (images)
                          background worker (RQ/arq or FastAPI BackgroundTasks first)
```

Rules: `compliance/` is pure Python (no DB, no network, no ML) so it is trivially testable. `agent/`
only receives the finished result object. `vision/` returns plain dataclasses. API routes only call services.

## 2. Data flow

1. Employee uploads photo → validate type/size → quality check (blur, brightness, resolution) → store original → create audit (`status=queued`, unique audit id, content hash for duplicate detection).
2. Worker: preprocess (EXIF rotate, resize) → **detector** returns boxes + class + confidence.
3. **Row assignment** from box geometry (cluster by y-centre, top→bottom) → rows 1..n.
4. **Recognizer**: crop each box → CLIP embedding → pgvector nearest-neighbour against catalog → SKU or `unknown` (below threshold). OCR only for low-confidence crops with visible text.
5. **Comparison engine** (deterministic): expected vs detected per (row, SKU) → missing / wrong-row / excess / unplanned / uncertain → score.
6. **AI agent** gets the structured result (never the image arithmetic) → summary, priorities, instructions, review requests. Its output is validated against the numbers; any number it states that is not in the input is rejected.
7. Persist detections, findings, recommendations → dashboard. Human can confirm, correct, or re-run.

## 3. Technology selection

| Choice | Why | Caveat / my recommendation |
|---|---|---|
| FastAPI, Pydantic, SQLAlchemy 2, Alembic | Your strength; typed; auto OpenAPI docs | Keep. |
| PostgreSQL + pgvector | One store for relational data and embedding search | Needs hosted Postgres (Neon/Supabase free tier works). SQLite stays for local dev via the same SQLAlchemy models only if pgvector is optional. |
| Ultralytics YOLO | Fast local detector, trainable | **AGPL-3.0 licence**: fine for a portfolio, a problem for a closed commercial product. Flag it. |
| CLIP (open_clip) | Zero-shot + few-shot product matching without training per SKU | CPU inference ~0.1–0.3 s/crop; batch crops. |
| PaddleOCR | Reads labels/prices | Heavy install. **Defer**; add only if evaluation shows text is needed. |
| OpenRouter LLM | Already working | Used for the *agent*, and as an optional fallback detector. |
| React + TS + Tailwind + Recharts + Axios | Your spec | You said you do not know JS. It doubles maintenance risk. Alternative: keep Jinja + small JS, add Recharts-style charts via Chart.js. **Your decision (Question B).** |
| Docker, Render/AWS | Reproducible deploy | Torch makes the image ~2–3 GB; Render free tier (512 MB RAM) **cannot** run YOLO + CLIP. Plan for a paid instance or a separate inference service. |

## 4. Database schema (PostgreSQL)

```
users(id, email unique, password_hash, role[admin|auditor], created_at)
stores(id, code unique, name, created_at)
shelves(id, store_id fk, code, name, levels int, unique(store_id, code))
products(id, sku unique, name, category, colour, created_at)
product_images(id, product_id fk, path, embedding vector(512), created_at)   -- ivfflat/hnsw index
planograms(id, shelf_id fk, version int, active bool, created_by fk, created_at, unique(shelf_id, version))
planogram_rows(id, planogram_id fk, row_no int, unique(planogram_id, row_no))
planogram_items(id, row_id fk, product_id fk, expected_qty int check(>0))
audits(id text pk 'SS-XXXXXXXX', store_id, shelf_id, planogram_id, user_id, status, stage, score numeric,
       trust_level, content_hash, attempts, created_at, completed_at, confirmed_at, error)
uploaded_images(id, audit_id fk, path, mime, width, height, quality_json, sha256)
detections(id, audit_id fk, row_no, product_id fk null, label, qty, confidence, count_confidence,
           box_x, box_y, box_w, box_h, status[ok|short|misplaced|unexpected|uncertain|unknown], source[yolo|llm|human])
audit_findings(id, audit_id fk, kind[missing|misplaced|excess|unplanned|uncertain], row_no, product_id, expected, found)
audit_recommendations(id, audit_id fk, priority, text, done bool)
indexes: audits(store_id, created_at desc), audits(content_hash), detections(audit_id), findings(audit_id)
```
Mapping from v1: `audits`, `tasks`→`audit_recommendations`, `runs` (daily limit) stays as a small table.

## 5. API design (`/api/v1`, JWT bearer auth, OpenAPI at `/docs`)

Your list plus the gaps I think are needed:
```
POST /auth/login                      POST /audits              (creates; returns existing id on duplicate hash)
POST /audits/{id}/upload              POST /audits/{id}/analyze (202, async)
GET  /audits/{id}  (includes status/stage for polling)   GET /audits?store=&from=&to=&status=&page=
POST /audits/{id}/confirm             POST /audits/{id}/rerun
POST /audits/{id}/corrections         (human fixes; needed for ground-truth collection)
GET  /audits/{id}/export?format=csv   GET /audits/{id}/image?overlay=true
GET/POST /products  (+ /products/{id}/images)      GET/POST/PUT /planograms
GET/POST /stores    GET/POST /shelves              GET /stats/dashboard
```
Errors: RFC-7807-style `{type,title,detail}`; 422 validation, 409 duplicate, 413/415 for bad uploads.
Rate/daily limits kept from v1.

## 6. Computer-vision strategy (honest version)

- **Stage 0 – baseline (Milestone 4):** pretrained `yolo11n/s` on our photos. Report precision/recall on a held-out set. Expect weak results for clothing. We say so in the README.
- **Stage 1 – class-agnostic "product" detector:** fine-tune YOLO with ONE class (`product`) on ~300–500 annotated shelf photos. This is much easier than SKU detection and is the realistic big win.
- **Stage 2 – recognition by retrieval, not classification:** crop → CLIP embedding → nearest catalog image. New SKU = add 3–5 reference photos, no retraining. Unknown below a threshold tuned on validation data.
- **Stage 3 – counting/overlap:** NMS tuning; low `count_confidence` for heavy overlap; folded piles flagged "approximate".
- **Fallback:** the existing LLM detector stays selectable (`DETECTOR=yolo|llm`), and the evaluation harness compares both on the same labelled cases.
- **Metrics:** detection precision/recall/mAP@0.5 (Ultralytics `val`); recognition top-1 accuracy; end-to-end: unit-count error and compliance-score error vs hand-labelled truth. No number is quoted before it is measured on held-out photos.

## 7. AI agent workflow

Input: validated `AuditResult` JSON (score, findings, tasks, uncertain list). Steps: (1) deterministic tasks already computed; (2) LLM prioritises and rewrites into employee-friendly sentences using only provided fields; (3) output schema `{summary, priorities[], instructions[], review_requests[]}`; (4) **guard**: every integer in the output must appear in the input, and product names must exist in the input, else fall back to the deterministic text; (5) low-confidence items always produce a "please verify" request. Tool calling is *not* needed in v2.0 (one structured call is simpler and cheaper); add tools only if the agent must query history.
Compliance formula (already implemented, documented in README): per-row `correct/expected*100 − 5×issues`, weighted by expected facings; only the backend computes it.

## 8. Dataset collection and annotation

- Collect 300–500 photos initially (2–3 stores/racks, different lighting, angles, fullness). Split by **store/session**, not random, to avoid leakage: 70 / 15 / 15.
- Annotate with Label Studio or Roboflow (free tiers): boxes for `product`; later SKU labels on a subset. Two-person review on 10% for agreement.
- Truth per photo for end-to-end tests: counts per SKU per row (format already defined in `evaluation/README.md`).
- Bootstrapping: use v1 LLM detections + human correction (v1 already stores corrections) as pre-labels to cut annotation time.
- Catalog: 3–5 reference photos per SKU (front, angled).
- Privacy: blur faces; keep raw data out of Git.

## 9. Roadmap (your 10 milestones, with sizing)

| M | Deliverable | Effort | Notes |
|---|---|---|---|
| 2 | New package layout (`api/ services/ vision/ compliance/ agent/`), Postgres+Alembic, auth, config | 1–2 days | Port v1 code, tests stay green |
| 3 | Upload + OpenCV quality checks, duplicate hash | 1 day | Replaces Pillow-only checks |
| 4 | Detector interface, pretrained YOLO baseline, eval report | 1–2 days | Baseline will be weak; documented |
| 5 | Catalog, CLIP embeddings, pgvector matching, unknown status | 2 days | |
| 6 | Row clustering + comparison engine port | 1–2 days | Mostly moves v1 code |
| 7 | Agent with number guard | 1 day | |
| 8–9 | Dashboard + integration | 3–5 days | React path; ~2 days if Jinja path |
| 10 | Tests, Docker, deploy | 2 days | |
| ∞ | Data collection and YOLO fine-tune | 1–2 weeks elapsed | Blocks real accuracy claims |

## 10. Technical risks and mitigations

| Risk | Mitigation |
|---|---|
| Pretrained YOLO misses clothing/products | Single-class fine-tune; keep LLM detector fallback |
| No labelled data | Human-in-the-loop corrections as labels; start with 300 photos |
| Unstable row numbering | Geometric row clustering from boxes |
| Folded/overlapping stock miscounted | `count_confidence`, "approximate" flag, human review |
| Look-alike SKUs (colour variants) | CLIP + colour histogram feature; unknown → human |
| Memory/compute on free hosting | Separate inference service or paid instance; small YOLO + ONNX |
| AGPL licence of Ultralytics | Disclose; swap to RT-DETR/YOLOX if commercial |
| LLM invents numbers | Backend computes everything; output guard |
| Scope creep | Milestone gates; each ends with tests + your approval |
| Overclaiming accuracy | Metrics only from held-out evaluation, in README |

## Decisions I need from you before Milestone 2

- **A. Evolve v1 (recommended) or rewrite from scratch?**
- **B. Frontend:** React + TS as specified, or keep Jinja + light JS (faster, you can maintain it)?
- **C. Database:** go to Postgres now, or SQLite until deployment?
- **D. Scope:** clothing first as specified; do you have clothing-shelf photos available?
- **E. Reference screenshot:** the message mentions a "supplied shelf audit screenshot", but none was attached. Please send it.
