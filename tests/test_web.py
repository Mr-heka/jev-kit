"""Offline tests for scripts/web.py: local fixtures over http.server, Jev faked.

Run: python3 -m unittest discover -s tests -p 'test_web.py'
Needs playwright and its chromium. No network, no Jev key.
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="jev-web-test-")
os.environ["JEV_KIT_HOME"] = TMP  # before importing common: its paths are fixed at import
os.environ["JEV_KIT_RUNS"] = os.path.join(TMP, "runs")
os.environ.pop("OPENROUTER_API_KEY", None)
os.environ.pop("JEV_KEY_COMMAND", None)

KIT = Path(__file__).resolve().parents[1]
FIXTURES = KIT / "tests" / "fixtures" / "web"
sys.path.insert(0, str(KIT / "scripts"))

import web  # noqa: E402
from jevkit import common  # noqa: E402

# Another test module may have imported common first with the real home, so point
# every path at the temp home explicitly rather than trusting the env vars above.
common.HOME = Path(TMP)
common.RECIPES = common.HOME / "recipes"
common.DRAFTS = common.RECIPES / "drafts"
common.RUNS = common.HOME / "runs"
common.APPROVALS = common.HOME / "approvals.json"
web.BROWSER = common.HOME / "browser"
web.PROFILE = web.BROWSER / "profile"
web.SESSION = web.BROWSER / "session.json"
web.STEP_STATE = web.BROWSER / "last-step.json"


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):  # noqa: ANN002
        pass


class FakeJev:
    """Stands in for client.decide. Picks the option mentioning `word`; scores items by their stars."""

    def __init__(self, word: str | None = None):
        self.word = word
        self.calls: list[tuple] = []

    def __call__(self, state, questions):
        self.calls.append((state, questions))
        name, q = next(iter(questions.items()))
        if q["type"] == "choice":
            opts = list(q["criteria"])
            want = next((k for k, v in q["criteria"].items() if k != "none" and self.word and self.word.lower() in v.lower()),
                        "none" if self.word else opts[0])
            probs = {k: 0.9 if k == want else round(0.1 / (len(opts) - 1), 6) for k in opts}
            answer = {"type": "choice", "choice": want, "probabilities": probs, "confidence": 0.9}
        elif q["type"] == "score":
            top = len(q["criteria"]) - 1
            score = max(0, min(top, int(state["item"].get("stars") or 0) - 2))
            answer = {"type": "score", "score": float(score), "confidence": 0.9, "probabilities": {str(score): 1.0}}
        else:
            answer = {"type": "noul", "noul": 0.95}
        return {"model": "fake-jev", "answers": {name: answer}, "usage": {"cost": 0.00002}, "elapsed": 0.001}


def cli(*argv: str) -> tuple[int, dict]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = web.main(list(argv))
    return code, json.loads(buf.getvalue())


class WebEngineTest(unittest.TestCase):
    server: http.server.ThreadingHTTPServer
    base: str

    @classmethod
    def setUpClass(cls) -> None:
        handler = functools.partial(_Quiet, directory=str(FIXTURES))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        code, out = cli("open")
        assert code == 0 and out["ok"], out

    @classmethod
    def tearDownClass(cls) -> None:
        cli("close")
        cls.server.shutdown()
        cls.server.server_close()

    def recipe(self, name: str, steps: list, **extra) -> dict:
        data = {"name": name, "engine": "web", "about": "test", "read_only": extra.pop("read_only", True),
                "allow_domains": ["127.0.0.1"], "steps": steps, **extra}
        path = Path(TMP) / f"{name}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return common.load_recipe(str(path), extra.pop("params", None))

    def run_recipe(self, recipe: dict, jev: FakeJev | None = None, approved: str | None = None) -> dict:
        budget = common.Budget(decide_fn=jev or FakeJev())
        with web.Browser() as br:
            return web.run_recipe(recipe, br, budget, approved, timeout=4)

    def go(self, page: str, **expect) -> dict:
        step = {"id": f"open-{page.split('.')[0]}", "do": "goto", "url": f"{self.base}/{page}"}
        if expect:
            step["expect"] = expect
        return step

    # ------------------------------------------------------------------ tests

    def test_goto_click_fill_press_expect(self) -> None:
        result = self.run_recipe(self.recipe("basic", [
            self.go("index.html", all=["Fixture shop"]),
            {"id": "shop", "do": "click", "target": {"role": "link", "name": "Shop", "exact": True},
             "expect": {"title_contains": "Shop", "all": ["Place order"]}},
            {"id": "query", "do": "fill", "target": {"label": "Search"}, "text": "adapter"},
            {"id": "search", "do": "press", "key": "Enter", "target": {"label": "Search"},
             "expect": {"url_contains": "q=adapter", "all": ["Results for adapter"]}},
        ]))
        self.assertEqual(result["status"], "done", result.get("stopped"))
        self.assertEqual([s["id"] for s in result["steps"]], ["open-index", "shop", "query", "search"])
        self.assertEqual(common.exit_code(result), 0)

    def test_expect_failure_stops_with_screenshot(self) -> None:
        result = self.run_recipe(self.recipe("expect-fail", [
            self.go("shop.html", all=["This text is not on the page"], timeout=1),
        ]))
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(common.exit_code(result), 2)
        self.assertEqual(result["stopped"]["step"], "open-shop")
        self.assertIn("missing", result["stopped"]["reason"])
        self.assertTrue(Path(result["stopped"]["screenshot"]).is_file())
        self.assertIn("Place order", result["stopped"]["screen_text"])

    def test_publish_guard_then_approval(self) -> None:
        steps = [self.go("shop.html"),
                 {"id": "order", "do": "click", "target": {"role": "button", "name": "Place order"},
                  "expect": {"all": ["Order placed"]}}]
        blocked = self.run_recipe(self.recipe("order", steps, read_only=False))
        self.assertEqual(blocked["status"], "needs_human")
        self.assertIn("approve", blocked["stopped"]["reason"])
        self.assertIn("Not ordered yet", blocked["stopped"]["screen_text"])
        common.approve("test-order-1", "yes, place the fixture order")
        allowed = self.run_recipe(self.recipe("order", steps, read_only=False), approved="test-order-1")
        self.assertEqual(allowed["status"], "done", allowed.get("stopped"))
        self.assertTrue(allowed["steps"][-1]["guard"].startswith("approved"))
        self.assertFalse(common.approval_valid("test-order-1"), "the approval is used up by the run")
        again = self.run_recipe(self.recipe("order", steps, read_only=False), approved="test-order-1")
        self.assertEqual(again["status"], "needs_human")

    def test_sensitive_fields_refused(self) -> None:
        for label in ("Password", "Card"):
            result = self.run_recipe(self.recipe("login", [
                self.go("login.html"), {"id": "type", "do": "fill", "target": {"label": label}, "text": "hunter2"}]))
            self.assertEqual(result["status"], "needs_human", label)
            self.assertIn("never types", result["stopped"]["reason"])
        card = self.run_recipe(self.recipe("login", [
            self.go("login.html"), {"id": "type", "do": "fill", "target": {"label": "Promo"}, "text": "4111 1111 1111 1111"}]))
        self.assertEqual(card["status"], "needs_human")
        self.assertIn("card number", card["stopped"]["reason"])
        ok = self.run_recipe(self.recipe("login", [
            self.go("login.html"), {"id": "type", "do": "fill", "target": {"label": "Promo"}, "text": "SPRING10"}]))
        self.assertEqual(ok["status"], "done", ok.get("stopped"))

    def test_allow_domains_enforced(self) -> None:
        left = self.run_recipe(self.recipe("fence", [
            self.go("index.html"), {"id": "away", "do": "click", "target": {"role": "link", "name": "Away"}}]))
        self.assertEqual(left["status"], "stopped")
        self.assertIn("left the allowed sites", left["stopped"]["reason"])
        port = self.base.rsplit(":", 1)[1]
        direct = self.run_recipe(self.recipe("fence", [
            {"id": "go-away", "do": "goto", "url": f"http://localhost:{port}/index.html"}]))
        self.assertEqual(direct["status"], "stopped")
        self.assertIn("outside allow_domains", direct["stopped"]["reason"])

    def test_pick_clicks_the_chosen_candidate(self) -> None:
        jev = FakeJev(word="Adapter")
        result = self.run_recipe(self.recipe("pick", [
            self.go("results.html"),
            {"id": "pick-item", "do": "pick", "from": {"css": "#results a.name"},
             "instructions": "Which result is the travel adapter?", "save_as": "picked",
             "expect": {"all": ["Detail page for item 2"]}},
        ]), jev)
        self.assertEqual(result["status"], "done", result.get("stopped"))
        self.assertEqual(result["saved"]["picked"]["text"], "Travel Adapter")
        criteria = jev.calls[0][1]["pick"]["criteria"]
        self.assertEqual(len(criteria), 5, "four candidates plus none")
        self.assertNotIn("Sponsored", json.dumps(criteria), "only the recipe's candidates reach Jev")
        nothing = self.run_recipe(self.recipe("pick", [
            self.go("results.html"),
            {"id": "pick-item", "do": "pick", "from": {"css": "#results a.name"}, "instructions": "A tent?"}]),
            FakeJev(word="Tent"))
        self.assertEqual(nothing["status"], "stopped")
        self.assertIn("no clear match", nothing["stopped"]["reason"])

    def test_collect_filter_rank_choose(self) -> None:
        result = self.run_recipe(self.recipe("pipeline", [
            self.go("results.html"),
            {"id": "collect", "do": "collect", "rows": {"css": "#results li.item"},
             "fields": {"name": {"css": "a.name", "attr": "title"}, "price": {"css": ".price"},
                        "stars": {"css": ".stars", "attr": "class", "regex": "s(\\d)", "number": True},
                        "link": {"css": "a.name", "attr": "href"}},
             "next": {"role": "link", "name": "Next", "exact": True}, "pages": 2, "save_as": "items"},
            {"id": "cheap", "do": "filter", "from": "items", "where": [{"field": "price", "lte": "{max_price}"}],
             "save_as": "cheap"},
            {"id": "rank", "do": "rank", "from": "cheap", "instructions": "Fit for a first trip?",
             "levels": ["poor", "partial", "good", "excellent"], "fields": ["name", "price", "stars"]},
            {"id": "top", "do": "take", "from": "ranked", "n": 2, "save_as": "top"},
            {"id": "best", "do": "choose", "from": "top", "instructions": "Best one?", "label_fields": ["name"],
             "save_as": "best"},
        ], params={"max_price": "50"}, output="best"))
        self.assertEqual(result["status"], "done", result.get("stopped"))
        items = result["saved"]["items"]
        self.assertEqual(len(items), 6, "two pages collected")
        self.assertEqual(items[0], {"name": "Budget Backpack 30L", "price": "$45.00", "stars": 3.0,
                                    "link": f"{self.base}/detail.html?id=1"})
        self.assertEqual([i["name"] for i in result["saved"]["cheap"]],
                         ["Budget Backpack 30L", "Universal Travel Adapter", "Packing Cubes Set of 6", "Passport Wallet"])
        self.assertEqual(result["saved"]["ranked"][0]["item"]["name"], "Universal Travel Adapter")
        self.assertEqual(result["output"]["item"]["name"], "Universal Travel Adapter")
        self.assertEqual(result["jev"]["calls"], 5, "four ranks and one choose")

    def test_optional_step_skipped_when_absent(self) -> None:
        result = self.run_recipe(self.recipe("optional", [
            self.go("shop.html"),
            {"id": "cookies", "do": "click", "target": {"role": "button", "name": "Reject all"}, "optional": True,
             "timeout": 0.5},
            {"id": "still-here", "do": "check", "expect": {"all": ["Place order"]}},
        ]))
        self.assertEqual(result["status"], "done", result.get("stopped"))
        self.assertEqual(result["steps"][1]["skipped"], "target not on the page")

    def test_snapshot_targets_resolve(self) -> None:
        self.assertEqual(cli("step", json.dumps(self.go("shop.html")))[0], 0)
        code, snap = cli("snapshot")
        self.assertEqual(code, 0)
        targets = [e["target"] for e in snap["elements"]]
        self.assertIn({"role": "button", "name": "Place order", "exact": True}, targets)
        self.assertIn({"role": "searchbox", "name": "Search", "exact": True}, targets)
        self.assertEqual(snap["headings"][0]["text"], "Shop")

    def test_step_record_save_and_replay(self) -> None:
        name = "fixture-search"
        steps = [
            self.go("index.html", all=["Fixture shop"]),
            {"id": "shop", "do": "click", "target": {"role": "link", "name": "Shop", "exact": True},
             "expect": {"title_contains": "Shop"}},
            {"id": "query", "do": "fill", "target": {"role": "searchbox", "name": "Search"}, "text": "adapter"},
            {"id": "search", "do": "click", "target": {"role": "button", "name": "Search", "exact": True},
             "expect": {"all": ["Results for adapter"]}},
        ]
        for step in steps:
            code, out = cli("step", json.dumps(step), "--record", name)
            self.assertEqual(code, 0, out)
        code, out = cli("step", json.dumps({"do": "click", "target": {"role": "link", "name": "Nope"}, "timeout": 0.5}),
                        "--record", name)
        self.assertEqual(code, 2, "a failed step is not recorded")
        path = common.save_draft(name, "Search the fixture shop. Params: query.", templatize={"adapter": "query"})
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["steps"]), 4)
        self.assertEqual(saved["allow_domains"], ["127.0.0.1"])
        self.assertEqual(saved["params"], {"query": "adapter"})
        result = self.run_recipe(common.load_recipe(name, {"query": "packing"}))
        self.assertEqual(result["status"], "done", result.get("stopped"))
        self.assertIn("q=packing", result["steps"][-1]["url"])


class OwnBrowserTest(unittest.TestCase):
    """With no session open, a run launches its own headless browser and closes it."""

    def test_run_without_session(self) -> None:
        cli("close")
        self.assertIsNone(web.session())
        path = Path(TMP) / "blank.json"
        path.write_text(json.dumps({"name": "blank", "engine": "web", "steps": [
            {"id": "blank", "do": "goto", "url": "data:text/html,<h1>Hello offline</h1>",
             "expect": {"all": ["Hello offline"]}}]}), encoding="utf-8")
        with web.Browser() as br:
            self.assertFalse(br.attached)
            result = web.run_recipe(common.load_recipe(str(path)), br, common.Budget(decide_fn=FakeJev()))
        self.assertEqual(result["status"], "done", result.get("stopped"))


class UnitTest(unittest.TestCase):
    def test_sensitive_field_rules(self) -> None:
        refuse = [{"type": "password"}, {"autocomplete": "one-time-code"}, {"autocomplete": "cc-number"},
                  {"name": "cardNumber"}, {"name": "cvv2"}, {"label": "Expiry date"}, {"name": "bsb"},
                  {"placeholder": "Tax file number"}, {"id": "otp"}, {"aria": "Passcode"}]
        for info in refuse:
            self.assertIsNotNone(web.sensitive_field(info), info)
        for info in ({"name": "q", "type": "search"}, {"label": "Passenger name"}, {"name": "promo"}):
            self.assertIsNone(web.sensitive_field(info), info)

    def test_code_step_needs_no_browser(self) -> None:
        cli("close")
        web.STEP_STATE.parent.mkdir(parents=True, exist_ok=True)
        web.STEP_STATE.write_text(json.dumps({"saved": {"xs": [{"p": "£3"}, {"p": "£9"}]}}), encoding="utf-8")
        code, out = cli("step", json.dumps({"do": "filter", "from": "xs", "where": [{"field": "p", "lte": "5"}],
                                            "save_as": "ys"}))
        self.assertEqual(code, 0, out)
        self.assertEqual(out["saved"], {"ys": [{"p": "£3"}]})

    def test_domain_allowed(self) -> None:
        self.assertTrue(web.domain_allowed("https://www.example.com/x", ["example.com"]))
        self.assertFalse(web.domain_allowed("https://example.com.evil.io/", ["example.com"]))
        self.assertTrue(web.domain_allowed("about:blank", ["example.com"]))


def tearDownModule() -> None:
    cli("close")
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
