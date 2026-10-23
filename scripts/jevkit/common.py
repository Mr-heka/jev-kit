"""Shared pieces for the replay engines (web, screen) and the judge lane.

Recipes, parameters, the publish guard, approvals, run logs, the Jev budget,
the three Jev moves (check a screen, pick one, rank many) and the code-only
data steps (filter, sort, take). Engines import this; nothing here clicks.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from . import client

HOME = Path(os.environ.get("JEV_KIT_HOME", "~/.jev-kit")).expanduser()
RECIPES = HOME / "recipes"
DRAFTS = RECIPES / "drafts"
RUNS = Path(os.environ.get("JEV_KIT_RUNS", str(HOME / "runs"))).expanduser()
APPROVALS = HOME / "approvals.json"
KIT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = KIT_ROOT / "recipes" / "examples"

ENGINES = {"web", "screen"}
APPROVAL_HOURS = 48
SCREEN_TEXT_LIMIT = 6000
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Anything that sends, buys, books, posts or deletes. A matching click stops the
# run unless the run carries a fresh, unused approval from the user.
PUBLISH_WORDS = re.compile(
    r"\b(buy|pay|purchase|checkout|check out|place (?:your )?order|order now|add to (?:basket|cart|bag|trolley)|"
    r"book now|book (?:it|this|flight|room|seat)|reserve|"
    r"confirm|send|post|publish|share|submit|delete|remove|subscribe|unsubscribe|donate|transfer|"
    r"sign up|create account|boost|install|upgrade|accept|agree)\b",
    re.I,
)
# Money and deletion: no recipe note can wave these through, only an approval.
HIGH_RISK_WORDS = re.compile(
    r"\b(buy|pay|purchase|checkout|check out|place (?:your )?order|order now|book now|reserve|donate|"
    r"transfer|send money|delete|remove account|close account|subscribe|upgrade)\b",
    re.I,
)
# Screens a machine must never get past on its own.
HUMAN_GATES = re.compile(
    r"captcha|verify you are (?:a )?human|i'?m not a robot|are you a robot|unusual traffic|"
    r"security check|two-factor|2-step verification|verification code|one-time (?:pass)?code|"
    r"enter the (?:\d-digit )?code|code (?:we )?sent|we sent (?:you )?a code|confirm it'?s you",
    re.I,
)


class Stop(Exception):
    """The run stops here and hands back to the agent (or the user)."""

    def __init__(self, reason: str, needs_human: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.needs_human = needs_human


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def clip(text: Any, limit: int) -> str:
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _write_json(path: Path, data: Any, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    if private:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ------------------------------------------------------------------ recipes

def recipe_dirs() -> list[Path]:
    """Project recipes first, then the user's, then the bundled examples."""
    return [Path.cwd() / ".jev" / "recipes", RECIPES, EXAMPLES]


def find_recipe(name: str) -> Path:
    direct = Path(name).expanduser()
    if direct.suffix == ".json" and direct.is_file():
        return direct.resolve()
    for folder in recipe_dirs():
        path = folder / f"{name}.json"
        if path.is_file():
            return path
    raise Stop(f"no recipe called '{name}'. Map it first (see references/recipes.md) or run `jev.py recipes`.")


def list_recipes() -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for folder in recipe_dirs():
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            name = data.get("name") or path.stem
            if name in seen:
                continue
            seen.add(name)
            out.append({
                "name": name,
                "engine": data.get("engine"),
                "about": data.get("about", ""),
                "params": data.get("params", {}),
                "read_only": data.get("read_only", True),
                "steps": len(data.get("steps", [])),
                "path": str(path),
            })
    return out


def _placeholders(obj: Any, found: set[str]) -> set[str]:
    if isinstance(obj, str):
        found.update(PLACEHOLDER.findall(obj))
    elif isinstance(obj, list):
        for item in obj:
            _placeholders(item, found)
    elif isinstance(obj, dict):
        for value in obj.values():
            _placeholders(value, found)
    return found


def fill(obj: Any, params: dict[str, str]) -> Any:
    if isinstance(obj, str):
        return PLACEHOLDER.sub(lambda m: str(params[m.group(1)]) if m.group(1) in params else m.group(0), obj)
    if isinstance(obj, list):
        return [fill(item, params) for item in obj]
    if isinstance(obj, dict):
        return {key: fill(value, params) for key, value in obj.items()}
    return obj


def validate_recipe(data: Any, source: str = "recipe") -> dict[str, Any]:
    if not isinstance(data, dict):
        raise Stop(f"{source}: a recipe is a JSON object")
    if data.get("engine") not in ENGINES:
        raise Stop(f"{source}: engine must be 'web' or 'screen'")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps or not all(isinstance(s, dict) and s.get("do") for s in steps):
        raise Stop(f"{source}: steps must be a non-empty list of objects with a 'do'")
    seen: set[str] = set()
    for n, step in enumerate(steps, 1):
        step.setdefault("id", f"step-{n}")
        if step["id"] in seen:
            raise Stop(f"{source}: duplicate step id '{step['id']}'")
        seen.add(step["id"])
        if data.get("read_only", True) and step.get("publish"):
            raise Stop(f"{source}: step '{step['id']}' is marked publish but the recipe is read_only")
    return data


def load_recipe(name_or_path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    path = find_recipe(name_or_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Stop(f"could not read recipe {path}: {exc}") from None
    validate_recipe(data, str(path))
    merged = {**{k: str(v) for k, v in data.get("params", {}).items()}, **(params or {})}
    live = {k: data[k] for k in data if k not in {"about", "name", "params", "mapped", "notes"}}
    missing = sorted(_placeholders(live, set()) - set(merged))
    if missing:
        raise Stop(f"recipe '{data.get('name', path.stem)}' needs: " + ", ".join(f"--param {m}=..." for m in missing))
    out = {**data, **fill(live, merged)}
    out["name"] = data.get("name") or path.stem
    out["_path"] = str(path)
    out["_params"] = merged
    return out


def parse_params(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise Stop(f"--param needs name=value, got '{pair}'")
        key, value = pair.split("=", 1)
        out[key.strip()] = value
    return out


# ------------------------------------------------------------------ mapping

def _draft_path(name: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,60}", name):
        raise Stop("recipe names are lowercase words joined by dashes, e.g. 'books-best-pick'")
    return DRAFTS / f"{name}.json"


def record_step(name: str, engine: str, step: dict[str, Any], header: dict[str, Any] | None = None) -> Path:
    """Append a step that just worked to the draft recipe `name`."""
    path = _draft_path(name)
    if path.exists():
        draft = json.loads(path.read_text(encoding="utf-8"))
        if draft.get("engine") != engine:
            raise Stop(f"draft '{name}' is a {draft.get('engine')} recipe, not {engine}")
    else:
        draft = {"name": name, "engine": engine, "about": "", "read_only": True, "params": {}, "steps": []}
    for key, value in (header or {}).items():
        draft.setdefault(key, value)
    clean = {k: v for k, v in step.items() if not k.startswith("_")}
    ids = {s.get("id") for s in draft["steps"]}
    if clean.get("id") in ids or not clean.get("id"):
        clean["id"] = f"{clean.get('id') or clean['do']}-{len(draft['steps']) + 1}"
    if clean.get("publish"):
        draft["read_only"] = False
    draft["steps"].append(clean)
    _write_json(path, draft)
    return path


def save_draft(name: str, about: str, params: dict[str, str] | None = None,
               templatize: dict[str, str] | None = None, overwrite: bool = False) -> Path:
    """Turn a draft into a reusable recipe. `templatize` maps literal text -> param name."""
    src = _draft_path(name)
    if not src.exists():
        raise Stop(f"no draft called '{name}' in {DRAFTS}")
    dest = RECIPES / f"{name}.json"
    if dest.exists() and not overwrite:
        raise Stop(f"recipe '{name}' already exists; pass --overwrite to replace it")
    draft = json.loads(src.read_text(encoding="utf-8"))
    defaults = dict(draft.get("params", {}))
    defaults.update(params or {})
    if templatize:
        def swap(obj: Any) -> Any:
            if isinstance(obj, str):
                for literal, param in templatize.items():
                    obj = obj.replace(literal, "{" + param + "}")
                return obj
            if isinstance(obj, list):
                return [swap(v) for v in obj]
            if isinstance(obj, dict):
                return {k: swap(v) for k, v in obj.items()}
            return obj
        for key in ("steps", "start_url"):
            if key in draft:
                draft[key] = swap(draft[key])
        for literal, param in templatize.items():
            defaults.setdefault(param, literal)
    draft["about"] = about
    draft["params"] = defaults
    draft["mapped"] = {"on": _dt.date.today().isoformat(), "by": os.environ.get("JEV_KIT_AUTHOR", "agent")}
    validate_recipe(draft, name)
    _write_json(dest, draft)
    src.unlink()
    return dest


# ------------------------------------------------------------------ approvals and guard

def _ledger() -> dict[str, Any]:
    try:
        return json.loads(APPROVALS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def approve(approval_id: str, quote: str, recipe: str) -> dict[str, Any]:
    """Record the user's own words approving one publishing run of one recipe."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,80}", approval_id):
        raise Stop("approval ids are 3-80 letters, digits, dots, dashes or underscores")
    if not quote.strip():
        raise Stop("an approval needs the user's own words in --quote")
    ledger = _ledger()
    if not recipe.strip():
        raise Stop("an approval names the recipe it covers (--recipe NAME, or 'step' for one-off steps)")
    ledger[approval_id] = {"quote": quote.strip(), "recipe": recipe.strip(), "at": now_iso(),
                           "epoch": time.time(), "used": False}
    _write_json(APPROVALS, ledger, private=True)
    return {"id": approval_id, **ledger[approval_id]}


def approval_valid(approval_id: str | None, recipe: str | None = None) -> bool:
    if not approval_id:
        return False
    entry = _ledger().get(approval_id)
    if not entry or entry.get("used") or time.time() - entry.get("epoch", 0) >= APPROVAL_HOURS * 3600:
        return False
    return recipe is None or entry.get("recipe") == recipe


def consume_approval(approval_id: str) -> None:
    ledger = _ledger()
    if approval_id in ledger:
        ledger[approval_id]["used"] = True
        ledger[approval_id]["used_at"] = now_iso()
        _write_json(APPROVALS, ledger, private=True)


def _mapped_label(step: dict[str, Any]) -> str:
    target = step.get("target") or {}
    return str(target.get("name") or target.get("text") or target.get("label") or "").strip().lower()


def guard_click(label: str, step: dict[str, Any], approval_id: str | None, picked: bool = False,
                recipe: str | None = None) -> str | None:
    """Raise Stop before a click that sends, buys, posts or deletes without approval.

    Returns a note for the run log when the click is allowed for a stated reason.
    A recipe can mark a fixed, mapped button `safe_because` (a search box's
    'Submit'). The pass holds only while the button still carries the text it was
    mapped with, never for money or deletion words, and never for targets chosen
    at run time by Jev.
    """
    flagged = bool(step.get("publish")) or bool(PUBLISH_WORDS.search(label or ""))
    if not flagged:
        return None
    if step.get("safe_because") and not step.get("publish") and not picked:
        mapped = _mapped_label(step)
        seen = [part.strip().lower() for part in (label or "").split("|") if part.strip()]
        if mapped and seen and all(part == mapped for part in seen) and not HIGH_RISK_WORDS.search(label or ""):
            return f"allowed: {step['safe_because']}"
    if approval_valid(approval_id, recipe):
        return f"approved: {approval_id}"
    raise Stop(
        f"'{clip(label, 80)}' looks like it sends, buys, books, posts or deletes. "
        "Show the user exactly what will happen, record their yes with "
        "`jev.py approve <id> --recipe <recipe> --quote \"<their words>\"`, then rerun with --approved <id>.",
        needs_human=True,
    )


def check_human_gate(screen_text: str) -> None:
    match = HUMAN_GATES.search(screen_text or "")
    if match:
        raise Stop(f"the screen asks for a person ('{match.group(0)}'). The user handles this step.", needs_human=True)


def expect_text(expect: dict[str, Any] | None, text: str) -> tuple[bool, str]:
    """Plain text checks, case-insensitive substrings. Free, no Jev."""
    if not expect:
        return True, ""
    def as_list(key: str) -> list[str]:  # a lone string is one phrase, not a list of letters
        value = expect.get(key, [])
        return [value] if isinstance(value, str) else [str(v) for v in value]

    low = (text or "").lower()
    missing = [t for t in as_list("all") if t.lower() not in low]
    if missing:
        return False, "missing: " + ", ".join(missing)
    anys = as_list("any")
    if anys and not any(t.lower() in low for t in anys):
        return False, "none of: " + ", ".join(anys)
    present = [t for t in as_list("none") if t.lower() in low]
    if present:
        return False, "should not show: " + ", ".join(present)
    return True, ""


# ------------------------------------------------------------------ run log

class RunLog:
    """One JSON file per run, rewritten after every step so a stop leaves a trail."""

    def __init__(self, recipe: dict[str, Any], engine: str):
        self.stamp = time.strftime("%Y%m%d-%H%M%S")
        self.dir = RUNS / (re.sub(r"[^A-Za-z0-9._-]+", "-", str(recipe["name"])).strip(".-") or "recipe")
        self.path = self.dir / f"{self.stamp}.json"
        self.t0 = time.monotonic()
        self.data: dict[str, Any] = {
            "recipe": recipe["name"],
            "engine": engine,
            "params": recipe.get("_params", {}),
            "started": now_iso(),
            "status": "running",
            "steps": [],
            "saved": {},
        }
        self.write()

    def shot_path(self, label: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9 ._-]+", "-", label)[:60]
        return self.dir / f"{self.stamp} - {safe}.png"

    def step(self, step_id: str, ok: bool = True, ms: float | None = None, **extra: Any) -> None:
        row: dict[str, Any] = {"id": step_id, "ok": ok}
        if ms is not None:
            row["ms"] = round(ms)
        row.update(extra)
        self.data["steps"].append(row)
        self.write()

    def save(self, key: str, value: Any) -> None:
        self.data["saved"][key] = value
        self.write()

    def stop(self, stop: Stop, step_id: str, screenshot: str | None = None, screen_text: str | None = None,
             budget: "Budget | None" = None) -> dict[str, Any]:
        self.data["status"] = "needs_human" if stop.needs_human else "stopped"
        self.data["stopped"] = {"step": step_id, "reason": stop.reason}
        if screenshot:
            self.data["stopped"]["screenshot"] = screenshot
        if screen_text:
            self.data["stopped"]["screen_text"] = clip(screen_text, 3000)
        return self.finish(budget, status=self.data["status"])

    def finish(self, budget: "Budget | None" = None, status: str = "done") -> dict[str, Any]:
        self.data["status"] = status
        self.data["seconds"] = round(time.monotonic() - self.t0, 2)
        if budget is not None:
            self.data["jev"] = budget.summary()
        self.data["log"] = str(self.path)
        self.write()
        return self.data

    def write(self) -> None:
        _write_json(self.path, self.data)


# ------------------------------------------------------------------ Jev budget and moves

class Budget:
    """Caps Jev calls and reported cost for one run. Thread-safe."""

    def __init__(self, max_calls: int = 60, max_cost: float = 0.25,
                 decide_fn: Callable[[Any, dict[str, Any]], dict[str, Any]] | None = None,
                 enabled: bool = True):
        self.max_calls = max_calls
        self.max_cost = max_cost
        self.decide_fn = decide_fn or client.decide
        self.enabled = enabled
        self.calls = 0
        self.cost = 0.0
        self.seconds = 0.0
        self.models: set[str] = set()
        self._lock = threading.Lock()

    def decide(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise Stop("this step needs Jev and Jev is switched off for this run")
        with self._lock:
            if self.calls >= self.max_calls:
                raise Stop(f"Jev call cap reached ({self.max_calls}); raise --max-jev-calls if this is expected")
            if self.cost >= self.max_cost:
                raise Stop(f"Jev cost cap reached (US${self.cost:.4f}); raise --max-jev-cost if this is expected")
            self.calls += 1
        try:
            result = self.decide_fn(state, questions)
        except client.JevError as exc:
            raise Stop(f"Jev unavailable ({exc.code}{'' if exc.status is None else ' ' + str(exc.status)})") from None
        with self._lock:
            self.cost += float(result.get("usage", {}).get("cost", 0) or 0)
            self.seconds += float(result.get("elapsed", 0) or 0)
            if result.get("model"):
                self.models.add(result["model"])
        return result

    def summary(self) -> dict[str, Any]:
        return {"calls": self.calls, "cost_usd": round(self.cost, 6), "call_seconds": round(self.seconds, 2),
                "models": sorted(self.models)}


def jev_check(budget: Budget, judge: dict[str, Any], screen_text: str,
              context: Any = None) -> tuple[bool, dict[str, Any]]:
    """Is this the right screen / state? Noul with a threshold, or Choice with a wanted option."""
    kind = judge.get("type", "noul")
    question: dict[str, Any] = {"type": kind, "instructions": judge["instructions"]}
    if "criteria" in judge:
        question["criteria"] = judge["criteria"]
    state: dict[str, Any] = {"screen_text": clip(screen_text, SCREEN_TEXT_LIMIT)}
    if context:
        state["context"] = context
    answer = budget.decide(state, {"check": question})["answers"]["check"]
    if kind == "noul":
        ok = answer["noul"] >= float(judge.get("threshold", 0.8))
    elif kind == "choice":
        want = judge.get("want")
        wanted = want if isinstance(want, list) else [want]
        ok = answer["choice"] in wanted and answer["probabilities"][answer["choice"]] >= float(judge.get("threshold", 0.7))
    else:
        ok = answer["score"] >= float(judge.get("min_score", len(judge["criteria"]) - 1))
    return ok, answer


def jev_pick(budget: Budget, instructions: str, candidates: list[str], context: Any = None,
             threshold: float = 0.6) -> tuple[int | None, dict[str, Any]]:
    """Choose one of the supplied candidates, or none. Returns its index."""
    if not candidates:
        return None, {}
    if len(candidates) > 40:
        raise Stop(f"{len(candidates)} candidates is too many for one pick; narrow the region or filter first")
    criteria = {f"c{i + 1}": clip(c, 400) or "(blank)" for i, c in enumerate(candidates)}
    criteria["none"] = "None of the listed items fits, or it is unclear which one does."
    state = {"context": context} if context else {"note": "Pick from the listed options only."}
    answer = budget.decide(state, {"pick": {"type": "choice", "instructions": instructions, "criteria": criteria}})
    answer = answer["answers"]["pick"]
    choice = answer["choice"]
    if choice == "none" or answer["probabilities"][choice] < threshold:
        return None, answer
    return int(choice[1:]) - 1, answer


def _most_likely_level(answer: dict[str, Any], levels: list[str]) -> str:
    probs = answer.get("probabilities")
    if probs:
        best = max(probs.items(), key=lambda kv: (kv[1], -int(kv[0])))
        return levels[int(best[0])]
    return levels[int(round(answer["score"]))]


def jev_rank(budget: Budget, items: list[Any], instructions: str, levels: list[str],
             fields: list[str] | None = None, context: Any = None, workers: int = 4) -> list[dict[str, Any]]:
    """Score every item on the same rubric (one call each, in parallel), then sort in code.

    Weighted score, most likely level and confidence are kept apart. Near ties
    and low confidence are flagged for the agent to look at, never rounded away.
    """
    if len(levels) < 2:
        raise Stop("rank needs at least two ordered levels, worst first")

    def one(index: int) -> tuple[int, dict[str, Any]]:
        item = items[index]
        shown = {k: item.get(k) for k in fields} if fields and isinstance(item, dict) else item
        state: dict[str, Any] = {"item": shown}
        if context:
            state["context"] = context
        result = budget.decide(state, {"fit": {"type": "score", "instructions": instructions, "criteria": levels}})
        return index, result["answers"]["fit"]

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as pool:
        answers = dict(pool.map(one, range(len(items))))
    ranked = sorted(answers.items(), key=lambda kv: (-kv[1]["score"], kv[0]))
    out: list[dict[str, Any]] = []
    for rank, (index, answer) in enumerate(ranked, 1):
        row = {
            "rank": rank,
            "index": index,
            "score": round(answer["score"], 3),
            "level": _most_likely_level(answer, levels),
            "confidence": answer.get("confidence"),
            "item": items[index],
            "flags": [],
        }
        if row["confidence"] is not None and row["confidence"] < 0.5:
            row["flags"].append("low_confidence")
        out.append(row)
    for a, b in zip(out, out[1:]):
        if a["score"] - b["score"] < 0.15:
            a["flags"].append("near_tie_below")
            b["flags"].append("near_tie_above")
    return out


# ------------------------------------------------------------------ code-only data steps

NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def parse_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = NUMBER.search(value.replace(" ", " "))
        if match:
            return float(match.group(0).replace(",", ""))
    return None


def _holds(item: dict[str, Any], cond: dict[str, Any]) -> bool:
    value = item.get(cond["field"])
    for op, target in cond.items():
        if op == "field":
            continue
        if op in {"lte", "gte", "lt", "gt"}:
            a, b = parse_number(value), parse_number(target)
            if a is None or b is None:
                return False
            if not {"lte": a <= b, "gte": a >= b, "lt": a < b, "gt": a > b}[op]:
                return False
        elif op == "eq":
            if str(value).strip().lower() != str(target).strip().lower():
                return False
        elif op == "ne":
            if str(value).strip().lower() == str(target).strip().lower():
                return False
        elif op == "contains":
            if str(target).lower() not in str(value or "").lower():
                return False
        elif op == "not_contains":
            if str(target).lower() in str(value or "").lower():
                return False
        elif op == "regex":
            if not re.search(str(target), str(value or ""), re.I):
                return False
        elif op == "in":
            if str(value).strip().lower() not in {str(t).strip().lower() for t in target}:
                return False
        else:
            raise Stop(f"unknown filter operator '{op}'")
    return True


def filter_items(items: list[dict[str, Any]], where: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if all(_holds(item, cond) for cond in where)]


def run_code_step(step: dict[str, Any], log: RunLog, budget: Budget) -> bool:
    """Run a step that needs no screen. Returns False if `do` is not a code step."""
    do = step["do"]
    saved = log.data["saved"]

    def source() -> list[Any]:
        name = step.get("from")
        if name not in saved or not isinstance(saved[name], list):
            raise Stop(f"step '{step['id']}' needs a saved list called '{name}'")
        return saved[name]

    started = time.monotonic()
    if do == "wait":
        time.sleep(float(step.get("secs", 1)))
        log.step(step["id"], ms=(time.monotonic() - started) * 1000)
    elif do == "set":
        log.save(step["save_as"], step.get("value"))
        log.step(step["id"])
    elif do == "filter":
        kept = filter_items(source(), step.get("where", []))
        log.save(step.get("save_as", step["from"]), kept)
        log.step(step["id"], kept=len(kept))
        if not kept and step.get("required", True):
            raise Stop(f"nothing passed the filter in '{step['id']}'")
    elif do == "sort":
        key = step["by"]
        rows = sorted(source(), key=lambda r: (parse_number(r.get(key)) is None, parse_number(r.get(key)) or 0, str(r.get(key))),
                      reverse=step.get("order") == "desc")
        log.save(step.get("save_as", step["from"]), rows)
        log.step(step["id"])
    elif do == "take":
        rows = source()[: int(step.get("n", 1))]
        log.save(step.get("save_as", step["from"]), rows)
        log.step(step["id"], kept=len(rows))
    elif do == "rank":
        ranked = jev_rank(budget, source(), step["instructions"], step["levels"], step.get("fields"),
                          step.get("context"), int(step.get("workers", 4)))
        if step.get("top"):
            ranked = ranked[: int(step["top"])]
        log.save(step.get("save_as", "ranked"), ranked)
        log.step(step["id"], ms=(time.monotonic() - started) * 1000, jev_calls=len(source()),
                 flags=sorted({f for r in ranked for f in r["flags"]}))
    elif do == "choose":
        rows = source()
        label = step.get("label_fields")
        texts = [json.dumps({k: (r.get("item", r) if isinstance(r, dict) else r).get(k) for k in label}, ensure_ascii=False)
                 if label else clip(r, 400) for r in rows]
        index, answer = jev_pick(budget, step["instructions"], texts, step.get("context"),
                                 float(step.get("threshold", 0.6)))
        log.save(step.get("save_as", "choice"), None if index is None else rows[index])
        log.step(step["id"], ms=(time.monotonic() - started) * 1000, jev=answer)
        if index is None and step.get("required", True):
            raise Stop(f"Jev found no clear winner in '{step['id']}'; review the shortlist")
    else:
        return False
    return True


# ------------------------------------------------------------------ CLI helpers

def add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--param", action="append", default=[], metavar="NAME=VALUE", help="recipe parameter")
    parser.add_argument("--approved", metavar="ID", help="approval id recorded with `jev.py approve`")
    parser.add_argument("--max-jev-calls", type=int, default=60)
    parser.add_argument("--max-jev-cost", type=float, default=0.25, help="US$ cap on reported Jev cost")
    parser.add_argument("--no-jev", action="store_true", help="stop at any step that needs Jev")


def budget_from(args: argparse.Namespace) -> Budget:
    return Budget(args.max_jev_calls, args.max_jev_cost, enabled=not getattr(args, "no_jev", False))


def emit(data: Any) -> None:
    print(json.dumps(data, indent=1, ensure_ascii=False))


def exit_code(result: dict[str, Any]) -> int:
    return 0 if result.get("status") == "done" else 2
