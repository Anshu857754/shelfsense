"""Run ShelfSense over labelled photos with different settings, and compare the results.

    python -m evaluation.run_eval --cases evaluation/cases --configs evaluation/configs.example.json

Every case is a folder holding case.json and its photo (see evaluation/README.md). Every
configuration is a model plus settings. Each (case, configuration) pair is run through the real
pipeline: image preparation, the AI call, the optional second look, and scoring.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from app import compliance, vision
from app.config import Settings, get_settings
from app.imaging import prepare_image
from app.schemas import Planogram
from evaluation import metrics

DEFAULT_CONFIG = {"detail": "full", "max_image_side": 1600, "verify": True, "passes": 1}


def load_cases(root: Path) -> list[dict]:
    """Every folder under root with a case.json. Folders starting with _ are templates."""
    cases = []
    for case_file in sorted(Path(root).glob("*/case.json")):
        if case_file.parent.name.startswith("_"):
            continue
        case = json.loads(case_file.read_text(encoding="utf-8-sig"))  # -sig: Windows editors add a BOM
        case["id"] = case.get("id") or case_file.parent.name
        image = case_file.parent / case["image"]
        if not image.exists():
            raise FileNotFoundError(f"{case_file}: the photo {case['image']!r} is not next to case.json")
        case["image_path"] = str(image)
        Planogram.model_validate(case["planogram"])  # fail early with a clear message
        cases.append(case)
    return cases


def load_configs(path: Path) -> list[dict]:
    configs = json.loads(Path(path).read_text(encoding="utf-8-sig"))  # -sig: Windows editors add a BOM
    names = [c["name"] for c in configs]
    if len(set(names)) != len(names):
        raise ValueError("Every configuration needs its own unique name.")
    return [{**DEFAULT_CONFIG, **c} for c in configs]


async def run_one(case: dict, config: dict, base: Settings, truth) -> dict:
    """One audit of one photo with one configuration. Never raises: failures are results too."""
    settings = base.model_copy(
        update={"openrouter_model": config["model"], "detail_level": config["detail"]}
    )
    planogram = Planogram.model_validate(case["planogram"])
    prepared = prepare_image(Path(case["image_path"]).read_bytes(), max_side=config["max_image_side"])

    usage = vision.track_usage()
    started = time.monotonic()
    row = {
        "case": case["id"], "config": config["name"], "ok": False, "complete": False,
        "seconds": 0.0, "tokens": 0, "units": 0, "score": None, "true_score": truth.score if truth else None,
        "code": None, "error": None, "metrics": None,
    }
    try:
        shelf, pass_scores = await vision.detect_best_of(
            prepared.data, prepared.mime, settings, passes=config["passes"], planogram=planogram,
            category=case.get("category"), custom_category=case.get("category_name", ""),
            detail=config["detail"],
        )
        if config["verify"] and vision.verification_candidates(shelf, settings.confidence_threshold):
            try:
                await vision.verify_detections(
                    prepared.data, shelf, settings, planogram, settings.confidence_threshold
                )
            except Exception as exc:  # same rule as the app: the second look never sinks an audit
                row["verify_error"] = str(exc)
        result = compliance.audit(
            planogram, shelf, confidence_threshold=settings.confidence_threshold, pass_scores=pass_scores
        )
        row.update(
            ok=True, complete=result.complete, score=result.score, code=result.incomplete_code,
            units=result.detected_units + result.uncertain_units,
            metrics=metrics.score_run(result, truth),
        )
    except vision.VisionError as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        row["seconds"] = round(time.monotonic() - started, 2)
        row["tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return row


async def evaluate(
    cases: list[dict], configs: list[dict], repeats: int = 1, parallel: int = 1,
    base: Settings | None = None, progress=print,
) -> list[dict]:
    """Run every case with every configuration, `repeats` times each.

    parallel > 1 is faster but makes the timings less trustworthy (calls compete for the network).
    """
    base = base or get_settings()
    truths = {case["id"]: metrics.build_truth(case) for case in cases}
    gate = asyncio.Semaphore(parallel)

    async def task(case: dict, config: dict, repeat: int) -> dict:
        async with gate:
            row = await run_one(case, config, base, truths[case["id"]])
        row["repeat"] = repeat
        status = row["error"] or (f"score {row['score']}" if row["complete"] else f"no score ({row['code']})")
        progress(f"  {config['name']:<24} {case['id']:<22} #{repeat}  {row['seconds']:>5.1f}s  {status}")
        return row

    jobs = [task(c, cfg, r) for c in cases for cfg in configs for r in range(1, repeats + 1)]
    return list(await asyncio.gather(*jobs))


def report(rows: list[dict], configs: list[dict]) -> str:
    """Markdown: a comparison table, then one line per run."""
    summaries = {c["name"]: metrics.summarize([r for r in rows if r["config"] == c["name"]]) for c in configs}
    text = metrics.render_markdown(summaries)
    lines = ["", "## Every run", "", "| Case | Config | # | s | tokens | units | score | true score | result |", "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r["case"], r["config"], r["repeat"])):
        outcome = r["error"] or ("scored" if r["complete"] else f"no score ({r['code']})")
        lines.append(
            f"| {r['case']} | {r['config']} | {r['repeat']} | {r['seconds']} | {r['tokens']} | {r['units']} "
            f"| {'-' if r['score'] is None else r['score']} | {'-' if r['true_score'] is None else r['true_score']} | {outcome} |"
        )
    return text + "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", default="evaluation/cases", help="folder of case folders")
    parser.add_argument("--configs", default="evaluation/configs.example.json", help="JSON list of configurations")
    parser.add_argument("--repeats", type=int, default=1, help="runs per case and configuration (default 1)")
    parser.add_argument("--parallel", type=int, default=1, help="runs at the same time (default 1: the most honest timings)")
    parser.add_argument("--only", default="", help="comma-separated configuration names to run")
    parser.add_argument("--out", default="evaluation/results", help="where to save the report")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)
    settings = get_settings()
    if not settings.openrouter_api_key:
        print("OPENROUTER_API_KEY is not set (see .env).", file=sys.stderr)
        return 2

    cases = load_cases(Path(args.cases))
    configs = load_configs(Path(args.configs))
    if args.only:
        wanted = {n.strip() for n in args.only.split(",")}
        configs = [c for c in configs if c["name"] in wanted]
    if not cases or not configs:
        print(f"Nothing to run: {len(cases)} case(s), {len(configs)} configuration(s).", file=sys.stderr)
        return 2

    total = len(cases) * len(configs) * args.repeats
    print(f"Running {total} audit(s): {len(cases)} case(s) x {len(configs)} configuration(s) x {args.repeats}")
    rows = asyncio.run(evaluate(cases, configs, args.repeats, args.parallel, settings))

    text = report(rows, configs)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out / f"{stamp}.json").write_text(json.dumps({"configs": configs, "runs": rows}, indent=2), encoding="utf-8")
    (out / f"{stamp}.md").write_text(text, encoding="utf-8")
    print()
    print(text)
    print(f"Saved to {out / (stamp + '.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
