"""Offline tests for the shared core: client validation, recipes, guard, approvals, judge pieces."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from jevkit import client, common  # noqa: E402
from jevkit.common import Stop  # noqa: E402


def fake_decide(answers_for):
    """A stand-in for client.decide: answers_for(state, questions) -> answers dict."""
    def decide(state, questions):
        return {"model": "fake-jev", "id": "x", "answers": answers_for(state, questions),
                "usage": {"cost": 0.00003}, "elapsed": 0.01}
    return decide


class TempHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(common, "HOME", home),
            mock.patch.object(common, "RECIPES", home / "recipes"),
            mock.patch.object(common, "DRAFTS", home / "recipes" / "drafts"),
            mock.patch.object(common, "RUNS", home / "runs"),
            mock.patch.object(common, "APPROVALS", home / "approvals.json"),
        ]
        for p in self.patches:
            p.start()
        self.cwd = os.getcwd()
        os.chdir(home)

    def tearDown(self):
        os.chdir(self.cwd)
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class ClientValidation(unittest.TestCase):
    def test_accepts_three_types(self):
        body = client.validate_request({"state": {"m": "hi"}, "questions": {
            "a": {"type": "choice", "instructions": "x", "criteria": {"p": "one", "q": "two"}},
            "b": {"type": "score", "instructions": "x", "criteria": ["low", "high"]},
            "c": {"type": "noul", "instructions": "x"},
        }})
        self.assertEqual(set(body["questions"]), {"a", "b", "c"})

    def test_rejects_bad_shapes(self):
        bad = [
            {"state": {}, "questions": {}, "extra": 1},
            {"state": {}, "questions": {"a": {"type": "choice", "instructions": "x", "criteria": {"only": "one"}}}},
            {"state": {}, "questions": {"a": {"type": "score", "instructions": "x", "criteria": ["one"]}}},
            {"state": {}, "questions": {"a": {"type": "chat", "instructions": "x"}}},
            {"state": {}, "questions": {"a": {"type": "noul", "instructions": " "}}},
            {"state": {}, "questions": {"a": {"type": "noul", "instructions": "x", "options": []}}},
        ]
        for body in bad:
            with self.assertRaises(client.JevError):
                client.validate_request(body)

    def test_answer_validation(self):
        q = {"a": {"type": "choice", "instructions": "x", "criteria": {"p": "1", "q": "2"}}}
        ok = {"a": {"type": "choice", "choice": "p", "probabilities": {"p": 0.9, "q": 0.1}, "confidence": 0.8}}
        self.assertEqual(client._check_answers(ok, q)["a"]["choice"], "p")
        with self.assertRaises(client.JevError):
            client._check_answers({"a": {**ok["a"], "choice": "zzz"}}, q)

    def test_key_command(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "JEV_KEY_COMMAND": "echo sk-test"}):
            self.assertEqual(client._lookup_key(), "sk-test")
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "JEV_KEY_COMMAND": ""}):
            with self.assertRaises(client.JevError):
                client._lookup_key()


class Recipes(TempHome):
    def write(self, folder: Path, data: dict) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{data['name']}.json").write_text(json.dumps(data))

    def test_params_fill_and_missing(self):
        self.write(common.RECIPES, {"name": "demo", "engine": "web", "about": "uses {who}", "params": {"who": "Ann"},
                                    "steps": [{"do": "goto", "url": "https://x.test/{who}/{page}"}]})
        with self.assertRaises(Stop) as ctx:
            common.load_recipe("demo")
        self.assertIn("--param page=", ctx.exception.reason)
        recipe = common.load_recipe("demo", {"page": "2"})
        self.assertEqual(recipe["steps"][0]["url"], "https://x.test/Ann/2")
        self.assertEqual(recipe["about"], "uses {who}")

    def test_regex_braces_left_alone(self):
        self.write(common.RECIPES, {"name": "rx", "engine": "screen", "steps": [{"do": "tap", "target": {"regex": "\\d{2,3}"}}]})
        self.assertEqual(common.load_recipe("rx")["steps"][0]["target"]["regex"], "\\d{2,3}")

    def test_project_recipe_wins(self):
        self.write(common.RECIPES, {"name": "dup", "engine": "web", "about": "user", "steps": [{"do": "wait"}]})
        self.write(Path.cwd() / ".jev" / "recipes", {"name": "dup", "engine": "web", "about": "project", "steps": [{"do": "wait"}]})
        self.assertEqual([r["about"] for r in common.list_recipes() if r["name"] == "dup"], ["project"])

    def test_read_only_blocks_publish_steps(self):
        self.write(common.RECIPES, {"name": "ro", "engine": "web", "steps": [{"do": "click", "publish": True}]})
        with self.assertRaises(Stop):
            common.load_recipe("ro")

    def test_record_then_save_with_templatize(self):
        common.record_step("my-flow", "web", {"id": "go", "do": "goto", "url": "https://shop.test/c/Travel"},
                           header={"start_url": "https://shop.test/", "allow_domains": ["shop.test"]})
        common.record_step("my-flow", "web", {"id": "go", "do": "click", "target": {"text": "Travel"}})
        path = common.save_draft("my-flow", "Open a category. Params: category.", templatize={"Travel": "category"})
        data = json.loads(path.read_text())
        self.assertEqual(data["params"], {"category": "Travel"})
        self.assertEqual(data["steps"][0]["url"], "https://shop.test/c/{category}")
        self.assertEqual(len({s["id"] for s in data["steps"]}), 2)
        self.assertEqual(common.load_recipe("my-flow", {"category": "Poetry"})["steps"][1]["target"]["text"], "Poetry")
        with self.assertRaises(Stop):
            common.save_draft("my-flow", "again")


class Guard(TempHome):
    def test_publish_words_stop_without_approval(self):
        for label in ["Place your order", "Send", "Book now", "Delete account", "Accept all cookies", "Add to basket"]:
            with self.assertRaises(Stop) as ctx:
                common.guard_click(label, {"id": "x"}, None)
            self.assertTrue(ctx.exception.needs_human)
        self.assertIsNone(common.guard_click("Travel", {"id": "x"}, None))
        self.assertIsNone(common.guard_click("The Book of Days", {"id": "x"}, None))

    def test_safe_because_only_for_fixed_targets(self):
        step = {"id": "x", "safe_because": "search box button"}
        self.assertIn("allowed", common.guard_click("Submit", step, None))
        with self.assertRaises(Stop):
            common.guard_click("Submit", step, None, picked=True)

    def test_approval_is_single_use(self):
        with self.assertRaises(Stop):
            common.approve("a", "yes")
        common.approve("post-1", "Yes, post it at 6pm")
        self.assertIn("approved", common.guard_click("Post", {"id": "x", "publish": True}, "post-1"))
        common.consume_approval("post-1")
        with self.assertRaises(Stop):
            common.guard_click("Post", {"id": "x", "publish": True}, "post-1")

    def test_human_gates(self):
        with self.assertRaises(Stop):
            common.check_human_gate("Please verify you are human")
        common.check_human_gate("Search results for flights")

    def test_expect_text(self):
        ok, _ = common.expect_text({"all": ["Results"], "none": ["Log in"]}, "12 results found")
        self.assertTrue(ok)
        ok, why = common.expect_text({"any": ["Cart", "Basket"]}, "Home page")
        self.assertFalse(ok)
        self.assertIn("none of", why)


class JevMoves(TempHome):
    def test_rank_sorts_in_code_and_flags_ties(self):
        scores = {"a": 0.2, "b": 2.9, "c": 2.85, "d": 1.0}

        def answers(state, questions):
            return {"fit": {"type": "score", "score": scores[state["item"]["id"]], "confidence": 0.4 if state["item"]["id"] == "d" else 0.9}}
        budget = common.Budget(decide_fn=fake_decide(answers))
        ranked = common.jev_rank(budget, [{"id": k} for k in "abcd"], "fit?", ["poor", "ok", "good", "great"])
        self.assertEqual([r["item"]["id"] for r in ranked], ["b", "c", "d", "a"])
        self.assertIn("near_tie_below", ranked[0]["flags"])
        self.assertIn("low_confidence", ranked[2]["flags"])
        self.assertEqual(budget.calls, 4)

    def test_pick_none_and_threshold(self):
        def answers(state, questions):
            opts = list(questions["pick"]["criteria"])
            return {"pick": {"type": "choice", "choice": "c2", "probabilities": {o: (0.55 if o == "c2" else 0.45 / (len(opts) - 1)) for o in opts}, "confidence": 0.5}}
        budget = common.Budget(decide_fn=fake_decide(answers))
        self.assertEqual(common.jev_pick(budget, "which?", ["x", "y", "z"], threshold=0.5)[0], 1)
        self.assertIsNone(common.jev_pick(budget, "which?", ["x", "y", "z"], threshold=0.6)[0])

    def test_budget_caps(self):
        budget = common.Budget(max_calls=1, decide_fn=fake_decide(lambda s, q: {"c": {"type": "noul", "noul": 0.9}}))
        budget.decide({}, {"c": {"type": "noul", "instructions": "x"}})
        with self.assertRaises(Stop):
            budget.decide({}, {"c": {"type": "noul", "instructions": "x"}})
        with self.assertRaises(Stop):
            common.Budget(enabled=False).decide({}, {})

    def test_jev_errors_become_stops(self):
        def broken(state, questions):
            raise client.JevError("http_error", status=402)
        with self.assertRaises(Stop) as ctx:
            common.Budget(decide_fn=broken).decide({}, {})
        self.assertIn("402", ctx.exception.reason)

    def test_check_noul_and_choice(self):
        budget = common.Budget(decide_fn=fake_decide(lambda s, q: {"check": {"type": "noul", "noul": 0.7}}))
        self.assertFalse(common.jev_check(budget, {"instructions": "right page?"}, "text")[0])
        self.assertTrue(common.jev_check(budget, {"instructions": "right page?", "threshold": 0.6}, "text")[0])

    def test_code_steps_pipeline(self):
        recipe = {"name": "pipe", "_params": {}}
        log = common.RunLog(recipe, "web")
        log.save("books", [{"t": "A", "price": "£51.00"}, {"t": "B", "price": "£12.50"}, {"t": "C", "price": "£30"}])
        budget = common.Budget(decide_fn=fake_decide(lambda s, q: {"fit": {"type": "score", "score": {"B": 1.0, "C": 3.0}[s["item"]["t"]]}}))
        common.run_code_step({"id": "f", "do": "filter", "from": "books", "where": [{"field": "price", "lte": "45"}], "save_as": "ok"}, log, budget)
        common.run_code_step({"id": "r", "do": "rank", "from": "ok", "instructions": "fit?", "levels": ["a", "b", "c", "d"], "save_as": "ranked"}, log, budget)
        self.assertEqual([r["item"]["t"] for r in log.data["saved"]["ranked"]], ["C", "B"])
        self.assertFalse(common.run_code_step({"id": "x", "do": "click"}, log, budget))
        result = log.finish(budget)
        self.assertEqual(result["status"], "done")
        self.assertTrue(Path(result["log"]).exists())

    def test_parse_number(self):
        self.assertEqual(common.parse_number("£1,299.50"), 1299.5)
        self.assertEqual(common.parse_number(7), 7.0)
        self.assertIsNone(common.parse_number("free"))


if __name__ == "__main__":
    unittest.main()
