#!/usr/bin/env python3
"""Jev kit command line: decisions, batches, the judge, recipes and health checks.

  jev.py doctor [--online]              what is installed and working
  jev.py decide INPUT.json              one typed decision
  jev.py batch MANIFEST.json [--execute --out PATH]
  jev.py judge ITEMS.json --instructions "..." --levels "poor|ok|good|great" [--where JSON] [--top N] [--pick]
  jev.py recipes [show NAME]            mapped flows this machine can replay
  jev.py save NAME --about "..." [--templatize "Travel=category"]
  jev.py approve ID --quote "the user's words"

JSON on stdout. Exit 0 done, 2 stopped or incomplete, 1 error.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jevkit import __version__, client, common  # noqa: E402
from jevkit.common import Stop  # noqa: E402

MAX_BATCH_JOBS = 500


def _read_json(path: str) -> Any:
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).expanduser().read_text(encoding="utf-8")
        return json.loads(raw)
    except (OSError, ValueError) as exc:
        raise Stop(f"could not read JSON from {path}: {exc}") from None


def _new_output(path: str | None) -> Path | None:
    if not path:
        return None
    out = Path(path).expanduser()
    if out.exists():
        raise Stop(f"{out} already exists; pick a new output path")
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def _write(out: Path | None, data: Any) -> None:
    if out is not None:
        out.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ doctor

def cmd_doctor(args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {
        "kit_version": __version__,
        "python": platform.python_version(),
        "python_ok": sys.version_info >= (3, 9),
        "platform": platform.system(),
        "home": str(common.HOME),
        "jev_key": "available" if client.key_available() else "missing: set OPENROUTER_API_KEY or JEV_KEY_COMMAND",
        "web_engine": "ready" if importlib.util.find_spec("playwright") else "not installed: pip install playwright && python3 -m playwright install chromium",
        "screen_engine": "macOS only",
        "recipes": len(common.list_recipes()),
    }
    if platform.system() == "Darwin":
        have = importlib.util.find_spec("Quartz") and importlib.util.find_spec("Vision")
        report["screen_engine"] = "ready" if have else "not installed: pip install pyobjc-framework-Quartz pyobjc-framework-Vision"
    if args.online and report["jev_key"] == "available":
        try:
            report["jev_account"] = client.check_key()
        except client.JevError as exc:
            report["jev_account"] = exc.as_dict()
    report = {k: v for k, v in report.items() if v is not None}
    report["status"] = "done"
    return report


# ------------------------------------------------------------------ decide and batch

def cmd_decide(args: argparse.Namespace) -> dict[str, Any]:
    body = client.validate_request(_read_json(args.input))
    try:
        result = client.decide(body["state"], body["questions"])
    except client.JevError as exc:
        return {"status": "error", **exc.as_dict()}
    return {"status": "done", **result}


def cmd_batch(args: argparse.Namespace) -> dict[str, Any]:
    manifest = _read_json(args.input)
    jobs = manifest.get("jobs") if isinstance(manifest, dict) else None
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= MAX_BATCH_JOBS:
        raise Stop(f"a batch is {{\"jobs\": [...]}} with 1-{MAX_BATCH_JOBS} jobs")
    ids: set[str] = set()
    clean: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict) or not isinstance(job.get("id"), str) or job["id"] in ids:
            raise Stop("every job needs a unique string id")
        ids.add(job["id"])
        body = client.validate_request({"state": job.get("state"), "questions": job.get("questions")})
        clean.append({"id": job["id"], **body})
    if not args.execute:
        return {"status": "done", "dry_run": True, "jobs": len(clean),
                "next": "add --execute --out NEW_PATH to send them (caps: --max-jev-calls, --max-jev-cost)"}
    out = _new_output(args.out)
    if out is None:
        raise Stop("--execute needs --out NEW_PATH")
    budget = common.Budget(args.max_jev_calls, args.max_jev_cost)
    started = time.monotonic()
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}

    def one(job: dict[str, Any]) -> None:
        try:
            r = budget.decide(job["state"], job["questions"])
            results[job["id"]] = {"answers": r["answers"], "model": r["model"], "cost": r["usage"].get("cost")}
        except Stop as exc:
            errors[job["id"]] = exc.reason

    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as pool:
        list(pool.map(one, clean))
    unattempted = [j["id"] for j in clean if j["id"] not in results and j["id"] not in errors]
    data = {
        "status": "done" if not errors and not unattempted else "incomplete",
        "completed": len(results), "failed": errors, "unattempted": unattempted,
        "seconds": round(time.monotonic() - started, 2), "jev": budget.summary(),
        "results": results, "out": str(out),
    }
    _write(out, data)
    return {k: v for k, v in data.items() if k != "results"}


# ------------------------------------------------------------------ judge

def _levels(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("["):
        levels = json.loads(raw)
    else:
        levels = [part.strip() for part in raw.split("|")]
    if len(levels) < 2 or not all(isinstance(v, str) and v for v in levels):
        raise Stop("--levels needs two or more ordered levels, worst first, e.g. 'poor fit|ok fit|great fit'")
    return levels


def cmd_judge(args: argparse.Namespace) -> dict[str, Any]:
    data = _read_json(args.input)
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise Stop("judge input is a list of items, or {\"items\": [...]}")
    out = _new_output(args.out)
    budget = common.Budget(args.max_jev_calls, args.max_jev_cost)
    started = time.monotonic()
    where = json.loads(args.where) if args.where else []
    kept = common.filter_items(items, where if isinstance(where, list) else [where]) if where else items
    report: dict[str, Any] = {"items": len(items), "after_filter": len(kept)}
    if not kept:
        return {"status": "done", **report, "ranked": [], "note": "nothing passed the filter; Jev not called"}
    if len(kept) > args.max_jev_calls:
        raise Stop(f"{len(kept)} items would need {len(kept)} Jev calls; raise --max-jev-calls or filter first")
    fields = [f.strip() for f in args.fields.split(",")] if args.fields else None
    ranked = common.jev_rank(budget, kept, args.instructions, _levels(args.levels), fields, args.context, args.workers)
    top = ranked[: args.top] if args.top else ranked
    report["ranked"] = top
    if args.pick and len(top) > 1:
        texts = [json.dumps({k: r["item"].get(k) for k in fields} if fields else r["item"], ensure_ascii=False) for r in top]
        index, answer = common.jev_pick(budget, args.pick_instructions or args.instructions, texts, args.context)
        report["pick"] = None if index is None else top[index]
        report["pick_answer"] = answer
    report["review"] = sorted({f for r in top for f in r["flags"]})
    report["seconds"] = round(time.monotonic() - started, 2)
    report["jev"] = budget.summary()
    report["status"] = "done"
    _write(out, report)
    if out is not None:
        report["out"] = str(out)
    return report


# ------------------------------------------------------------------ recipes, save, approve

def cmd_recipes(args: argparse.Namespace) -> dict[str, Any]:
    if args.action == "show":
        if not args.name:
            raise Stop("recipes show NAME")
        path = common.find_recipe(args.name)
        return {"status": "done", "path": str(path), "recipe": json.loads(path.read_text(encoding="utf-8"))}
    return {"status": "done", "recipes": common.list_recipes(),
            "folders": [str(p) for p in common.recipe_dirs()]}


def cmd_save(args: argparse.Namespace) -> dict[str, Any]:
    templatize = common.parse_params(args.templatize)
    path = common.save_draft(args.name, args.about, common.parse_params(args.param), templatize, args.overwrite)
    return {"status": "done", "saved": str(path),
            "next": f"replay it: python3 scripts/<web|screen>.py run {args.name}"}


def cmd_approve(args: argparse.Namespace) -> dict[str, Any]:
    entry = common.approve(args.id, args.quote)
    return {"status": "done", "approval": entry,
            "note": f"valid once, for {common.APPROVAL_HOURS} hours: rerun with --approved {args.id}"}


# ------------------------------------------------------------------ main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="what is installed and working")
    p.add_argument("--online", action="store_true", help="also check the key with OpenRouter (no inference, no spend)")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("decide", help="one typed decision from a {state, questions} file")
    p.add_argument("input")
    p.set_defaults(fn=cmd_decide)

    p = sub.add_parser("batch", help="many independent decisions; dry run unless --execute")
    p.add_argument("input")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--out")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--max-jev-calls", type=int, default=100)
    p.add_argument("--max-jev-cost", type=float, default=0.25)
    p.set_defaults(fn=cmd_batch)

    p = sub.add_parser("judge", help="score many items on one rubric, sort in code, optionally pick one")
    p.add_argument("input", help="JSON list of items (or {\"items\": [...]})")
    p.add_argument("--instructions", required=True, help="the judgment, in full sentences")
    p.add_argument("--levels", required=True, help="ordered levels worst first, 'a|b|c' or a JSON list")
    p.add_argument("--fields", help="comma list of item fields to show Jev (default: the whole item)")
    p.add_argument("--where", help="code filter first, JSON list like [{\"field\":\"price\",\"lte\":20}]")
    p.add_argument("--context", help="short shared context, e.g. the user's goal")
    p.add_argument("--top", type=int, default=0)
    p.add_argument("--pick", action="store_true", help="final Jev Choice among the top items")
    p.add_argument("--pick-instructions")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--max-jev-calls", type=int, default=200)
    p.add_argument("--max-jev-cost", type=float, default=0.25)
    p.add_argument("--out")
    p.set_defaults(fn=cmd_judge)

    p = sub.add_parser("recipes", help="list mapped flows, or show one")
    p.add_argument("action", nargs="?", choices=["list", "show"], default="list")
    p.add_argument("name", nargs="?")
    p.set_defaults(fn=cmd_recipes)

    p = sub.add_parser("save", help="turn a recorded draft into a reusable recipe")
    p.add_argument("name")
    p.add_argument("--about", required=True, help="one line: what it does and its params")
    p.add_argument("--param", action="append", default=[], metavar="NAME=DEFAULT")
    p.add_argument("--templatize", action="append", default=[], metavar="LITERAL=PARAM",
                   help="replace literal text in the steps with {PARAM}")
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(fn=cmd_save)

    p = sub.add_parser("approve", help="record the user's yes for one publishing run")
    p.add_argument("id")
    p.add_argument("--quote", required=True)
    p.set_defaults(fn=cmd_approve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.fn(args)
    except Stop as exc:
        common.emit({"status": "needs_human" if exc.needs_human else "stopped", "reason": exc.reason})
        return 2
    except client.JevError as exc:
        common.emit({"status": "error", **exc.as_dict()})
        return 1
    common.emit(result)
    return {"done": 0, "error": 1}.get(result.get("status"), 2)


if __name__ == "__main__":
    raise SystemExit(main())
