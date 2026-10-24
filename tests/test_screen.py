"""Offline tests for the screen engine. No clicks, no network.

  python3 -m unittest discover -s tests -p 'test_screen.py' -v
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import screen  # noqa: E402
from jevkit import common  # noqa: E402
from jevkit.common import Stop  # noqa: E402

HAVE_PYOBJC = sys.platform == "darwin" and all(importlib.util.find_spec(m) for m in ("Quartz", "Vision", "AppKit"))
WIN = {"id": 7, "pid": 4242, "owner": "Calculator", "title": "Calculator", "layer": 0, "alpha": 1.0,
       "x": 100.0, "y": 50.0, "w": 200.0, "h": 400.0}


def box(text: str, x: float, y: float, w: float = 20, h: float = 16) -> dict:
    return {"text": text, "x": x, "y": y, "w": w, "h": h, "conf": 1.0}


def png_bytes(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x00" * width * 3 for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


KEYPAD = [box("7×6", 140, 30, 40, 20), box("42", 120, 60, 60, 35), box("AC", 70, 150, 30, 18),  # reading order
          box("7", 27, 203), box("8", 80, 203), box("X", 184, 201), box("8", 80, 300), box("=", 189, 363)]


class TestTargets(unittest.TestCase):
    def test_contains_exact_nth_region_regex(self):
        self.assertEqual(screen.find_target(KEYPAD, {"text": "7"}, WIN)["text"], "7×6")
        self.assertEqual(screen.find_target(KEYPAD, {"text": "7", "match": "exact"}, WIN)["x"], 27)
        self.assertEqual(screen.find_target(KEYPAD, {"text": "8", "match": "exact", "nth": 1}, WIN)["y"], 300)
        self.assertEqual(screen.find_target(KEYPAD, {"text": "8", "match": "exact", "nth": -1}, WIN)["y"], 300)
        self.assertIsNone(screen.find_target(KEYPAD, {"text": "8", "nth": 5}, WIN))
        low = screen.find_target(KEYPAD, {"text": "8", "region": [0, 0.7, 1, 1]}, WIN)
        self.assertEqual(low["y"], 300)
        self.assertEqual(screen.find_target(KEYPAD, {"regex": "^[×xX*]$"}, WIN)["x"], 184)
        self.assertIsNone(screen.find_target(KEYPAD, {"regex": "^[×xX*]$", "region": [0, 0, 0.5, 1]}, WIN))
        self.assertIsNone(screen.find_target(KEYPAD, {}, WIN))

    def test_glyph_exact_match_is_not_empty_wildcard(self):
        boxes = [box("×", 0, 0), box("÷", 30, 0)]
        self.assertEqual(screen.find_target(boxes, {"text": "÷", "match": "exact"}, WIN)["x"], 30)

    def test_offset_and_frac(self):
        px, py, b = screen.point_for(KEYPAD, {"text": "AC", "offset": [10, -5]}, WIN)
        self.assertEqual((px, py), (70 + 15 + 10, 150 + 9 - 5))
        self.assertEqual(b["text"], "AC")
        px, py, b = screen.point_for(KEYPAD, {"frac": [0.5, 0.25]}, WIN)
        self.assertEqual((px, py, b), (100, 100, None))


class TestCoordinates(unittest.TestCase):
    def test_vision_box_to_window_points(self):
        got = screen.to_win(0.25, 0.5, 0.5, 0.1, 200, 400)
        self.assertEqual(got, {"x": 50, "y": 160, "w": 100, "h": 40})

    def test_retina_scale_and_crop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.png"
            path.write_bytes(png_bytes(400, 800))  # a 200x400 pt window on a 2x display
            self.assertEqual(screen.png_size(path), (400, 800))
            scale = screen.png_size(path)[0] / WIN["w"]
        self.assertEqual(scale, 2.0)
        self.assertEqual(screen.crop_rect([0, 0.1, 0.5, 0.35], WIN, scale), (0, 80, 200, 200))

    def test_public_box_fractions(self):
        pb = screen.public_box(box("=", 180, 360, 20, 20), WIN)
        self.assertEqual((pb["fx"], pb["fy"]), (0.95, 0.925))


class TestSecrets(unittest.TestCase):
    def test_label_left_on_same_row(self):
        field = box("", 200, 100, 180, 22)
        self.assertEqual(screen.secret_label([box("Password", 100, 103, 80, 16), field], field), "Password")

    def test_label_just_above(self):
        field = box("", 100, 140, 200, 24)
        self.assertEqual(screen.secret_label([box("Card number", 100, 115, 90, 16), field], field), "Card number")
        self.assertTrue(screen.secret_label([box("CVV", 100, 118, 30, 16), field], field))
        self.assertTrue(screen.secret_label([box("Tax file number", 100, 118, 30, 16), field], field))

    def test_field_placeholder(self):
        field = box("Enter one-time code", 100, 100, 160, 20)
        self.assertTrue(screen.secret_label([field], field))

    def test_far_or_unrelated_labels_pass(self):
        field = box("", 200, 100, 180, 22)
        far = [box("Email", 100, 103, 60, 16), box("Password", 100, 300, 80, 16), box("Pinned notes", 200, 80, 90, 16), field]
        self.assertIsNone(screen.secret_label(far, field))

    def test_no_target_checks_whole_screen(self):
        self.assertEqual(screen.secret_label([box("Enter your PIN", 0, 380)], None), "Enter your PIN")
        self.assertIsNone(screen.secret_label(KEYPAD, None))


class TestDenied(unittest.TestCase):
    def test_denied(self):
        for name in ["Messages", "mail", "Mail.app", "/System/Applications/Mail.app", "com.apple.MobileSMS",
                     "Wallet", "System Settings", "System Preferences", "Keychain Access", "Passwords",
                     "1Password 7", "Keeper Password Manager", "Bitwarden", "Terminal", "iTerm2", "Warp",
                     "CommBank", "NAB Banking", "com.apple.Terminal"]:
            self.assertTrue(screen.denied_app(name), name)

    def test_allowed(self):
        for name in ["Calculator", "Notes", "Safari", "Mailchimp", "Preview", "iPhone Mirroring", ""]:
            self.assertFalse(screen.denied_app(name), name)


class TestWindows(unittest.TestCase):
    def test_sheet_is_not_the_main_window_and_is_an_alert(self):
        sheet = {**WIN, "id": 8, "x": 120, "y": 80, "w": 160, "h": 120}
        wins = [sheet, WIN]
        self.assertEqual(screen.pick_main(wins, "Calculator")["id"], 7)
        self.assertEqual([a["id"] for a in screen.alert_windows(wins, WIN, "Calculator")], [8])

    def test_modal_and_system_dialogs(self):
        modal = {**WIN, "id": 9, "layer": 8, "x": 600, "w": 260, "h": 150}
        system = {**WIN, "id": 10, "owner": "SecurityAgent", "layer": 1000, "x": 700, "w": 300, "h": 200}
        other = {**WIN, "id": 11, "owner": "Finder", "x": 0, "w": 900, "h": 900}
        found = [a["id"] for a in screen.alert_windows([modal, system, other, WIN], WIN, "Calculator")]
        self.assertEqual(found, [9, 10])

    def test_covering_skips_overlays(self):
        dock = {**WIN, "id": 1, "owner": "Dock", "layer": 20, "x": 0, "y": 0, "w": 2000, "h": 2000}
        hud = {**WIN, "id": 2, "owner": "Wispr Flow", "layer": 1000, "x": 0, "y": 0, "w": 2000, "h": 2000}
        self.assertEqual(screen.covering_window([hud, dock, WIN], 150, 100)["id"], 7)
        floating = {**WIN, "id": 3, "owner": "Notes", "layer": 3, "x": 140, "y": 90, "w": 50, "h": 50}
        self.assertEqual(screen.covering_window([floating, WIN], 150, 100)["owner"], "Notes")


class TestKeysAndFields(unittest.TestCase):
    def test_parse_combo(self):
        mods, code, key = screen.parse_combo("cmd+shift+a")
        self.assertEqual(([m[0] for m in mods], code, key), ([55, 56], 0, "a"))
        self.assertEqual(screen.parse_combo("*")[1], None)  # typed as a character
        with self.assertRaises(Stop):
            screen.parse_combo("hyper+a")
        with self.assertRaises(Stop):
            screen.parse_combo("cmd+launchrockets")

    def test_submit_keys_are_guarded(self):
        self.assertIn("submit", screen.submit_label("return"))
        self.assertIn("submit", screen.submit_label("cmd+enter"))
        self.assertIn("delete", screen.submit_label("cmd+delete"))
        self.assertIsNone(screen.submit_label("escape"))
        self.assertIn("delete", screen.submit_label("delete"))
        self.assertTrue(screen.switches_app("cmd+tab"))
        self.assertTrue(screen.switches_app("command+space"))
        self.assertFalse(screen.switches_app("cmd+a"))

    def test_read_field(self):
        region = [0, 0, 1, 0.3]
        f = {"regex": r"^[\d,]+$", "region": region, "pick": "largest"}
        self.assertEqual(screen.read_field(KEYPAD, f, WIN), "42")
        rows = [box("Views", 20, 100, 50, 16), box("1,204", 150, 101, 40, 16), box("Likes", 20, 140, 50, 16),
                box("O", 22, 160, 10, 16)]
        self.assertEqual(screen.read_field(rows, {"label": "Views"}, WIN), "1,204")
        self.assertEqual(screen.read_field(rows, {"label": "Likes", "pick": "below"}, WIN), "0")
        self.assertIsNone(screen.read_field(rows, {"label": "Shares"}, WIN))

    def test_calc(self):
        self.assertEqual(screen.calc("7*6"), "42")
        self.assertEqual(screen.calc("125 × 48"), "6000")
        self.assertEqual(screen.calc("7/2"), "3.5")
        for bad in ["2**8", "__import__('os')", "a*b", "1/0"]:
            with self.assertRaises(Stop):
                screen.calc(bad)
        self.assertEqual(screen.resolve_calc([{"field": "v", "gte": {"calc": "3*3"}}]), [{"field": "v", "gte": "9"}])


# ------------------------------------------------------------------ engine with a fake Mac

class FakeIO:
    """Scripted windows and OCR; records input instead of sending it."""

    def __init__(self, boxes: list[dict], front: tuple[int, str] = (4242, "Calculator")):
        self.boxes, self._front = boxes, front
        self.wins = [dict(WIN)]
        self.after_click: list[dict] | None = None
        self.can_activate = True
        self.clicks: list[tuple] = []
        self.keys: list[str] = []
        self.typed: list[str] = []
        self.clip = "user clipboard"
        self.warped = None

    def windows(self):
        return [dict(w) for w in self.wins]

    def capture(self, win, path):
        return 2.0

    def ocr(self, path, win):
        return [dict(b) for b in self.boxes]

    def front(self):
        return self._front

    def activate(self, pid, quick=False):
        if self.can_activate:
            self._front = (pid, "Calculator" if pid == WIN["pid"] else "Claude")
        return self.can_activate

    def open_app(self, name):
        self.keys.append(f"open:{name}")

    def click(self, gx, gy, clicks=1, hold=0.0):
        self.clicks.append((gx, gy, clicks))
        if self.after_click is not None:
            self.wins = self.after_click + self.wins

    def drag(self, p0, p1, steps=20, delay=0.02):
        self.clicks.append(("drag", p0, p1))

    def scroll(self, gx, gy, pixels, ticks=16):
        self.keys.append(f"scroll:{pixels}")

    def key(self, combo):
        self.keys.append(combo)
        if combo == "cmd+v":
            self.typed.append(self.clip)

    def type_text(self, text):
        self.typed.append(text)

    def mouse_pos(self):
        return (5, 5)

    def warp(self, pos):
        self.warped = pos

    def clip_snapshot(self):
        return [{"public.utf8-plain-text": self.clip}]

    def clip_set(self, text):
        self.clip = text

    def clip_restore(self, items):
        self.clip = items[0]["public.utf8-plain-text"]


def fake_jev(choice: str = "c2", noul: float = 0.9):
    calls = []

    def decide(state, questions):
        calls.append((state, questions))
        name, q = next(iter(questions.items()))
        if q["type"] == "choice":
            probs = {k: (0.9 if k == choice else 0.1 / (len(q["criteria"]) - 1)) for k in q["criteria"]}
            ans = {"type": "choice", "choice": choice, "probabilities": probs, "confidence": 0.9}
        else:
            ans = {"type": "noul", "noul": noul}
        return {"model": "fake-jev", "answers": {name: ans}, "usage": {"cost": 0.00003}, "elapsed": 0.01}
    decide.calls = calls  # type: ignore[attr-defined]
    return decide


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [mock.patch.object(common, "RUNS", root / "runs"),
                        mock.patch.object(common, "DRAFTS", root / "drafts"),
                        mock.patch.object(common, "APPROVALS", root / "approvals.json")]
        for p in self.patches:
            p.start()
        sleep = mock.patch.object(screen.time, "sleep", lambda s: None)
        sleep.start()
        self.patches.append(sleep)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def run_steps(self, steps, io, approved=None, decide=None, app="Calculator", name="test"):
        args = argparse.Namespace(approved=approved, timeout=0.3, stay=False, max_jev_calls=5, max_jev_cost=0.1,
                                  no_jev=False, budget=common.Budget(5, 0.1, decide_fn=decide or fake_jev()))
        recipe = common.validate_recipe({"name": name, "engine": "screen", "app": app, "steps": steps,
                                         "read_only": not any(s.get("publish") for s in steps)})
        result, _eng = screen.execute(recipe, args, io=io)
        return result


class TestEngine(EngineCase):
    def test_tap_lands_on_window_coordinates_and_restores(self):
        io = FakeIO(KEYPAD)
        res = self.run_steps([{"id": "eq", "do": "tap", "target": {"text": "=", "match": "exact"}}], io)
        self.assertEqual(res["status"], "done")
        self.assertEqual(io.clicks, [(100 + 189 + 10, 50 + 363 + 8, 1)])
        self.assertEqual(io.warped, (5, 5))

    def test_send_button_needs_approval(self):
        io = FakeIO(KEYPAD + [box("Send", 20, 380, 40, 16)])
        res = self.run_steps([{"id": "send", "do": "tap", "target": {"text": "Send"}}], io)
        self.assertEqual(res["status"], "needs_human")
        self.assertEqual(io.clicks, [])
        still = self.run_steps([{"id": "send", "do": "tap", "target": {"text": "Send"},
                                 "safe_because": "test fixture"}], FakeIO(KEYPAD + [box("Send", 20, 380, 40, 16)]))
        self.assertEqual(still["status"], "needs_human", "safe_because never waives Send")
        ok = self.run_steps([{"id": "submit", "do": "tap", "target": {"text": "Submit"},
                              "safe_because": "search box"}], FakeIO(KEYPAD + [box("Submit", 20, 380, 50, 16)]))
        self.assertEqual(ok["status"], "done")

    def test_app_switch_shortcut_refused(self):
        io = FakeIO(KEYPAD)
        res = self.run_steps([{"id": "switch", "do": "key", "keys": ["cmd+tab", "x"]}], io)
        self.assertEqual(res["status"], "stopped")
        self.assertEqual(io.keys, [])

    def test_paste_shortcut_respects_secret_labels(self):
        io = FakeIO(KEYPAD + [box("Password", 20, 400, 60, 16)])
        res = self.run_steps([{"id": "paste", "do": "key", "keys": "cmd+v"}], io)
        self.assertEqual(res["status"], "needs_human")
        self.assertEqual(io.keys, [])

    def test_publish_marked_key_and_plain_delete_guarded(self):
        for step in ({"id": "del", "do": "key", "keys": "delete"},
                     {"id": "x", "do": "key", "keys": "escape", "publish": True}):
            io = FakeIO(KEYPAD)
            res = self.run_steps([step], io)
            self.assertEqual(res["status"], "needs_human", step)
            self.assertEqual(io.keys, [])

    def test_pick_rechecked_after_jev_answers(self):
        io = FakeIO(KEYPAD + [box("Help", 20, 380, 40, 16), box("Adapter", 20, 420, 60, 16)])
        inner = fake_jev("c2")

        def swap(state, questions):  # the screen changes while Jev is thinking
            io.boxes = KEYPAD + [box("Help", 20, 380, 40, 16), box("Buy now", 20, 420, 60, 16)]
            return inner(state, questions)
        res = self.run_steps([{"id": "pick", "do": "pick", "candidates_regex": "Help|Adapter",
                               "instructions": "Which is the adapter?"}], io, decide=swap)
        self.assertEqual(res["status"], "stopped")
        self.assertEqual(io.clicks, [])

    def test_typed_keys_respect_secret_labels(self):
        io = FakeIO(KEYPAD + [box("PIN", 20, 400, 30, 16)])
        res = self.run_steps([{"id": "pin", "do": "key", "keys": ["1", "2"]}], io)
        self.assertEqual(res["status"], "needs_human")
        self.assertEqual(io.keys, [])

    def test_denied_by_bundle_id(self):
        self.assertTrue(screen.denied_app("com.apple.MobileSMS"))
        self.assertTrue(screen.denied_app("com.bitwarden.desktop"))
        self.assertTrue(screen.denied_app("com.mitchellh.ghostty"))
        self.assertFalse(screen.denied_app("com.apple.calculator"))

    def _approved_run(self, approval, io):
        path = Path(self.tmp.name) / "send-it.json"
        path.write_text(json.dumps({"name": "send-it", "engine": "screen", "app": "Calculator", "read_only": False,
                                    "steps": [{"id": "send", "do": "tap", "target": {"text": "Send"}, "publish": True}]}))
        args = argparse.Namespace(approved=approval, timeout=0.3, stay=False, max_jev_calls=5, max_jev_cost=0.1,
                                  no_jev=False, budget=common.Budget(5, 0.1, decide_fn=fake_jev()))
        return screen.execute(common.load_recipe(str(path)), args, io=io)[0], path

    def test_approval_allows_once_then_is_used(self):
        with self.assertRaises(Stop):  # one-off steps never publish
            common.approve("calc-ok", "yes, send it", "step")
        _, path = self._approved_run(None, FakeIO(KEYPAD + [box("Send", 20, 380, 40, 16)]))
        common.approve("calc-ok", "yes, send it", str(path))
        io = FakeIO(KEYPAD + [box("Send", 20, 380, 40, 16)])
        self.assertEqual(self._approved_run("calc-ok", io)[0]["status"], "done")
        self.assertEqual(len(io.clicks), 1)
        self.assertFalse(common.approval_valid("calc-ok"))
        again = self._approved_run("calc-ok", FakeIO(KEYPAD + [box("Send", 20, 380, 40, 16)]))[0]
        self.assertEqual(again["status"], "needs_human", "used up, even in the same process")

    def test_tab_in_typed_text_refused(self):
        io = FakeIO(KEYPAD)
        res = self.run_steps([{"id": "t", "do": "type", "text": "alice\tpa55word"}], io)
        self.assertEqual(res["status"], "stopped")
        self.assertEqual(io.typed, [])

    def test_return_key_is_guarded(self):
        io = FakeIO(KEYPAD)
        self.assertEqual(self.run_steps([{"id": "go", "do": "key", "keys": "return"}], io)["status"], "needs_human")
        self.assertEqual(io.keys, [])
        io = FakeIO(KEYPAD)
        res = self.run_steps([{"id": "clear", "do": "key", "keys": ["escape", "escape"]}], io)
        self.assertEqual((res["status"], io.keys), ("done", ["escape", "escape"]))

    def test_pick_with_fake_jev(self):
        rows = [box("Ad: Cheap flights", 10, 100, 150, 16), box("Qantas QF500", 10, 140, 150, 16),
                box("Virgin VA900", 10, 180, 150, 16)]
        decide = fake_jev("c2")
        io = FakeIO(rows)
        res = self.run_steps([{"id": "pick", "do": "pick", "candidates_regex": ".", "region": [0, 0.2, 1, 0.5],
                               "instructions": "Which row is the real Qantas result, not an ad?"}], io, decide=decide)
        self.assertEqual(res["status"], "done")
        self.assertEqual(io.clicks, [(100 + 85, 50 + 148, 1)])
        self.assertEqual(res["jev"]["calls"], 1)
        state, questions = decide.calls[0]
        self.assertIn("Qantas QF500", json.dumps(questions))
        self.assertIn("screen_text", state["context"])

    def test_pick_never_gets_safe_because(self):
        rows = [box("Buy now", 10, 100, 80, 16), box("Details", 10, 140, 80, 16)]
        io = FakeIO(rows)
        res = self.run_steps([{"id": "pick", "do": "pick", "candidates_regex": ".", "instructions": "x",
                               "safe_because": "should not apply"}], io, decide=fake_jev("c1"))
        self.assertEqual(res["status"], "needs_human")
        self.assertEqual(io.clicks, [])

    def test_pick_save_and_none(self):
        rows = [box("Alpha", 10, 100), box("Beta", 10, 140)]
        res = self.run_steps([{"id": "p", "do": "pick", "candidates_regex": ".", "instructions": "x", "then": "save",
                               "save_as": "best"}], FakeIO(rows), decide=fake_jev("c2"))
        self.assertEqual(res["saved"]["best"]["text"], "Beta")
        res = self.run_steps([{"id": "p", "do": "pick", "candidates_regex": ".", "instructions": "x"}],
                             FakeIO(rows), decide=fake_jev("none"))
        self.assertEqual(res["status"], "stopped")

    def test_type_refuses_secret_fields(self):
        screen_boxes = [box("Password", 20, 100, 70, 16), box("Enter here", 110, 100, 80, 16)]
        io = FakeIO(screen_boxes)
        res = self.run_steps([{"id": "t", "do": "type", "text": "hunter2", "target": {"text": "Enter here"}}], io)
        self.assertEqual(res["status"], "needs_human")
        self.assertEqual((io.typed, io.clicks), ([], []))
        io = FakeIO(screen_boxes)
        res = self.run_steps([{"id": "p", "do": "paste", "text": "x"}], io)  # no target: whole screen counts
        self.assertEqual((res["status"], io.typed), ("needs_human", []))

    def test_type_and_paste_into_plain_field(self):
        io = FakeIO([box("Search", 20, 100, 60, 16)])
        res = self.run_steps([{"id": "t", "do": "type", "text": "flights", "target": {"text": "Search"}},
                              {"id": "p", "do": "paste", "text": "to Cairns", "target": {"text": "Search"}}], io)
        self.assertEqual(res["status"], "done")
        self.assertEqual(io.typed, ["flights", "to Cairns"])
        self.assertEqual(len(io.clicks), 2)
        self.assertEqual(io.clip, "user clipboard")  # restored after the run

    def test_denied_app_and_front_checks(self):
        io = FakeIO(KEYPAD)
        self.assertEqual(self.run_steps([{"id": "o", "do": "open_app", "app": "Messages"}], io)["status"], "needs_human")
        self.assertEqual(self.run_steps([{"id": "eq", "do": "tap", "target": {"text": "="}}], io,
                                        app="Terminal")["status"], "needs_human")
        stuck = FakeIO(KEYPAD, front=(999, "Notes"))
        stuck.can_activate = False
        res = self.run_steps([{"id": "eq", "do": "tap", "target": {"text": "="}}], stuck)
        self.assertEqual((res["status"], stuck.clicks), ("stopped", []))

    def test_new_sheet_stops_but_old_system_dialog_is_noted(self):
        io = FakeIO(KEYPAD)
        io.after_click = [{**WIN, "id": 8, "x": 120, "y": 80, "w": 160, "h": 120}]
        res = self.run_steps([{"id": "eq", "do": "tap", "target": {"text": "="}},
                              {"id": "again", "do": "tap", "target": {"text": "="}}], io)
        self.assertEqual((res["status"], res["stopped"]["step"]), ("stopped", "again"))
        self.assertIn("alert or sheet", res["stopped"]["reason"])
        io = FakeIO(KEYPAD)
        io.wins.append({**WIN, "id": 20, "owner": "UserNotificationCenter", "layer": 8, "x": 700})
        res = self.run_steps([{"id": "eq", "do": "tap", "target": {"text": "="}}], io)
        self.assertEqual(res["status"], "done")
        self.assertTrue(res["warnings"])

    def test_human_gate_stops(self):
        res = self.run_steps([{"id": "r", "do": "read"}], FakeIO([box("Enter the verification code", 10, 10, 150)]))
        self.assertEqual(res["status"], "needs_human")

    def test_read_then_code_check(self):
        steps = [{"id": "r", "do": "read", "save_as": "result",
                  "fields": {"value": {"regex": r"^[\d,]+$", "region": [0, 0, 1, 0.3], "pick": "largest", "required": True}}},
                 {"id": "c", "do": "check", "from": "result",
                  "where": [{"field": "value", "gte": {"calc": "7*6"}, "lte": {"calc": "7*6"}}]}]
        res = self.run_steps(steps, FakeIO(KEYPAD))
        self.assertEqual((res["status"], res["saved"]["result"]["value"]), ("done", "42"))
        steps[1]["where"] = [{"field": "value", "gte": {"calc": "7*7"}, "lte": {"calc": "7*7"}}]
        self.assertEqual(self.run_steps(steps, FakeIO(KEYPAD))["status"], "stopped")

    def test_judge_region_sends_only_those_lines(self):
        decide = fake_jev(noul=0.95)
        res = self.run_steps([{"id": "j", "do": "check", "judge": {"instructions": "Finished?", "region": [0, 0, 1, 0.3]}}],
                             FakeIO(KEYPAD), decide=decide)
        self.assertEqual(res["status"], "done")
        self.assertEqual(decide.calls[0][0]["screen_text"], "7×6\n42")
        low = self.run_steps([{"id": "j", "do": "check", "judge": {"instructions": "Finished?"}}], FakeIO(KEYPAD),
                             decide=fake_jev(noul=0.3))
        self.assertEqual(low["status"], "stopped")

    def test_expect_and_optional(self):
        steps = [{"id": "maybe", "do": "tap", "target": {"text": "Nope"}, "optional": True},
                 {"id": "eq", "do": "tap", "target": {"text": "="}, "expect": {"all": ["42"], "none": ["Error"]}}]
        res = self.run_steps(steps, FakeIO(KEYPAD))
        self.assertEqual(res["status"], "done")
        self.assertFalse(res["steps"][0]["ok"])
        bad = self.run_steps([{"id": "eq", "do": "tap", "target": {"text": "="}, "expect": {"all": ["99"]}}], FakeIO(KEYPAD))
        self.assertEqual(bad["status"], "stopped")


    def test_scroll_default_is_positive_down(self):
        io = FakeIO(KEYPAD)
        self.assertEqual(self.run_steps([{"id": "s", "do": "scroll"}], io)["status"], "done")
        self.assertEqual(io.keys, ["scroll:400"])

    def test_saved_values_carry_into_a_run(self):
        args = argparse.Namespace(approved=None, timeout=0.3, stay=False, budget=common.Budget(decide_fn=fake_jev()))
        recipe = {"name": "t", "engine": "screen", "app": "Calculator",
                  "steps": [{"id": "c", "do": "check", "from": "result", "where": [{"field": "value", "eq": "42"}]}]}
        result, _ = screen.execute(recipe, args, io=FakeIO(KEYPAD), saved={"result": {"value": "42"}})
        self.assertEqual(result["status"], "done")


class TestRecording(EngineCase):
    def test_record_step_header(self):
        path = screen.record("calc-test", {"id": "eq", "do": "tap", "target": {"text": "="}}, "Calculator")
        draft = json.loads(path.read_text())
        self.assertEqual((draft["engine"], draft["app"], draft["steps"][0]["id"]), ("screen", "Calculator", "eq"))

    def test_bundled_example_loads(self):
        recipe = common.load_recipe(str(common.EXAMPLES / "calculator-multiply.json"), {"a": "12", "b": "9"})
        self.assertEqual((recipe["engine"], recipe["app"], recipe["read_only"]), ("screen", "Calculator", True))
        check = next(s for s in recipe["steps"] if s["id"] == "check-product")
        self.assertEqual(screen.resolve_calc(check["where"])[0]["gte"], "108")
        self.assertEqual(sum(1 for s in recipe["steps"] if s.get("judge")), 1)
        self.assertTrue(all(s["do"] in screen.ACTIONS for s in recipe["steps"]))


@unittest.skipUnless(HAVE_PYOBJC, "pyobjc (Quartz, Vision, AppKit) not installed")
class TestVision(unittest.TestCase):
    def test_ocr_reads_rendered_text_in_window_points(self):
        import AppKit
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "text.png"
            img = AppKit.NSImage.alloc().initWithSize_((400, 200))
            img.lockFocus()
            AppKit.NSColor.whiteColor().set()
            AppKit.NSRectFill(((0, 0), (400, 200)))
            attrs = {AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(40),
                     AppKit.NSForegroundColorAttributeName: AppKit.NSColor.blackColor()}
            AppKit.NSString.stringWithString_("Total 42").drawAtPoint_withAttributes_((40, 120), attrs)
            img.unlockFocus()
            rep = AppKit.NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
            rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(path), True)
            boxes = screen.MacIO().ocr(path, {"w": 400, "h": 200})
        hit = next(b for b in boxes if "42" in b["text"])
        self.assertLess(hit["y"], 100)  # drawn in the top half; window points have a top-left origin
        self.assertGreater(hit["x"], 20)

    def test_window_list_shape(self):
        wins = screen.MacIO().windows()
        self.assertTrue(all({"id", "pid", "owner", "layer", "x", "y", "w", "h"} <= set(w) for w in wins))


if __name__ == "__main__":
    unittest.main()
