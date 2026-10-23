#!/usr/bin/env python3
"""Screen engine: replay a mapped flow in any macOS app window.

Each read captures the app's front window (screencapture -l), reads it with
macOS Vision OCR and finds targets by their on-screen text. Input is real
Quartz mouse and keyboard events. Plain text checks are free; only fuzzy calls
go to Jev. Anything unexpected stops the run with a screenshot and the screen
text so the agent can take over and fix the recipe.

  screen.py doctor
  screen.py screen --app Calculator --boxes
  screen.py step '{"do": "tap", "target": {"text": "AC"}}' --app Calculator --record my-flow
  screen.py run calculator-multiply --param a=12 --param b=9

Needs pyobjc: pip install pyobjc-framework-Quartz pyobjc-framework-Vision.
Exit 0 done, 2 stopped or needs a person, 1 error. JSON on stdout.
"""

from __future__ import annotations

import argparse
import ast
import ctypes
import importlib.util
import json
import operator
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jevkit import client, common  # noqa: E402
from jevkit.common import Stop  # noqa: E402

INSTALL = ("the screen engine needs pyobjc: pip install pyobjc-framework-Quartz pyobjc-framework-Vision "
           "(or uv pip install -p <python> pyobjc-framework-Quartz pyobjc-framework-Vision)")
KIT_PYTHON = Path.home() / ".venvs" / "jev-kit" / "bin" / "python"

# Apps the engine never opens or drives. A recipe cannot override this.
DENIED_NAMES = {"messages", "mobilesms", "mail", "wallet", "systemsettings", "systempreferences", "keychainaccess",
                "passwords", "terminal", "iterm", "iterm2", "warp", "warpstable"}
DENIED_PARTS = ("1password", "keeper", "bitwarden", "bank")
DENIED_SHOWN = ["Messages", "Mail", "Wallet", "System Settings", "System Preferences", "Keychain Access", "Passwords",
                "1Password", "Keeper", "Bitwarden", "Terminal", "iTerm2", "Warp", "any app with 'bank' in its name"]
# Labels that mean the field wants a secret. The kit never types into those.
SECRET_LABEL = re.compile(
    r"pass(?:word|code|phrase)|\bpin\b|one[- ]?time (?:pass)?code|\botp\b|verification code|security code|"
    r"card (?:number|no\b)|credit card|\bcv[vc]2?\b|account (?:number|no\b)|tax file (?:number|no\b)|\btfn\b",
    re.I,
)
# Processes whose windows are system dialogs (auth prompts, Gatekeeper, permission asks).
SYSTEM_DIALOG_OWNERS = {"UserNotificationCenter", "CoreServicesUIAgent", "SecurityAgent", "coreautha",
                        "universalAccessAuthWarn", "AuthenticationServicesAgent", "LocalAuthenticationRemoteService"}
OVERLAY_OWNERS = {"Dock", "Window Server"}
MODAL_LAYER = 8  # kCGModalPanelWindowLevel: app-modal alerts

KEYCODES = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51, "forwarddelete": 117,
    "escape": 53, "esc": 53, "left": 123, "right": 124, "down": 125, "up": 126, "home": 115, "end": 119,
    "pageup": 116, "pagedown": 121, "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
    "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38,
    "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
}
# name -> (modifier keycode, CGEventFlags mask)
MODS = {"cmd": (55, 0x100000), "command": (55, 0x100000), "shift": (56, 0x20000), "alt": (58, 0x80000),
        "option": (58, 0x80000), "opt": (58, 0x80000), "ctrl": (59, 0x40000), "control": (59, 0x40000)}
NUM = re.compile(r"^[+\-−]?\d[\d,]*(\.\d+)?\s*[KkMm]?%?$")


class SetupError(Exception):
    """The engine cannot start on this machine (missing pyobjc, not macOS)."""


# ------------------------------------------------------------------ pure helpers (tested offline)

def denied_app(name: str | None) -> bool:
    raw = re.sub(r"\.app/?$", "", Path(str(name or "").strip().rstrip("/")).name, flags=re.I).lower()
    if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", raw):  # a bundle id: com.apple.MobileSMS
        raw = raw.rsplit(".", 1)[-1]
    compact = re.sub(r"[^a-z0-9]", "", raw)
    return compact in DENIED_NAMES or any(p in raw for p in DENIED_PARTS)


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def same_text(a: str, b: str) -> bool:
    na, nb = norm(a), norm(b)
    return na == nb if (na or nb) else a.strip().lower() == b.strip().lower()


def to_win(nx: float, ny: float, nw: float, nh: float, win_w: float, win_h: float) -> dict[str, float]:
    """Vision's normalised box (origin bottom-left) -> window points (origin top-left)."""
    return {"x": nx * win_w, "y": (1 - ny - nh) * win_h, "w": nw * win_w, "h": nh * win_h}


def crop_rect(region: list[float], win: dict[str, Any], scale: float) -> tuple[int, int, int, int]:
    """Window fractions -> (x, y, w, h) in screenshot pixels (retina scale 2)."""
    x0, y0, x1, y1 = region
    return (round(x0 * win["w"] * scale), round(y0 * win["h"] * scale),
            round((x1 - x0) * win["w"] * scale), round((y1 - y0) * win["h"] * scale))


def png_size(path: Path) -> tuple[int, int]:
    with open(path, "rb") as fh:
        head = fh.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", head[16:24])


def center(b: dict[str, Any]) -> tuple[float, float]:
    return b["x"] + b["w"] / 2, b["y"] + b["h"] / 2


def in_region(b: dict[str, Any], region: list[float] | None, win: dict[str, Any]) -> bool:
    if not region:
        return True
    cx, cy = center(b)
    fx, fy = cx / win["w"], cy / win["h"]
    return region[0] <= fx <= region[2] and region[1] <= fy <= region[3]


def find_target(boxes: list[dict[str, Any]], t: dict[str, Any], win: dict[str, Any],
                narrow: Callable[[dict, str], dict] | None = None) -> dict[str, Any] | None:
    """The nth OCR box matching a text or regex target inside its region."""
    if not t or ("text" not in t and "regex" not in t):
        return None
    narrow = narrow or (lambda b, needle: b)
    rx = re.compile(t["regex"], re.I) if "regex" in t else None
    exact = t.get("match", "contains") == "exact"
    hits = []
    for b in boxes:
        if not in_region(b, t.get("region"), win):
            continue
        if rx is not None:
            m = rx.search(b["text"])
            if m:
                hits.append(narrow(b, m.group(0)))
        elif exact:
            if same_text(b["text"], t["text"]):
                hits.append(b)
        elif t["text"].lower() in b["text"].lower():
            hits.append(narrow(b, t["text"]))
    n = int(t.get("nth", 0))
    return hits[n] if -len(hits) <= n < len(hits) else None


def point_for(boxes: list[dict[str, Any]], t: dict[str, Any], win: dict[str, Any],
              narrow: Callable[[dict, str], dict] | None = None) -> tuple[float, float, dict | None] | None:
    """Window point to act on: frac targets, or the matched box's centre plus offset."""
    if "frac" in t:
        return win["w"] * t["frac"][0], win["h"] * t["frac"][1], None
    b = find_target(boxes, t, win, narrow)
    if b is None:
        return None
    dx, dy = t.get("offset", [0, 0])
    cx, cy = center(b)
    return cx + dx, cy + dy, b


def boxes_at(boxes: list[dict[str, Any]], px: float, py: float, pad: float = 6) -> list[dict[str, Any]]:
    return [b for b in boxes if b["x"] - pad <= px <= b["x"] + b["w"] + pad and b["y"] - pad <= py <= b["y"] + b["h"] + pad]


def secret_label(boxes: list[dict[str, Any]], field: dict[str, Any] | None) -> str | None:
    """OCR text that marks `field` as a secret: the field itself, a label on the same
    row to its left, or one just above it. With no field, any line on screen counts."""
    if field is None:
        near = boxes
    else:
        cx, cy = center(field)
        near = [field]
        for b in boxes:
            if b is field:
                continue
            bx, by = center(b)
            same_row = abs(by - cy) <= max(field["h"], b["h"]) * 0.8 and bx < cx and field["x"] - (b["x"] + b["w"]) < 420
            gap = field["y"] - (b["y"] + b["h"])
            above = -2 <= gap <= max(40, 2.5 * field["h"]) and b["x"] < field["x"] + field["w"] + 20 \
                and b["x"] + b["w"] > field["x"] - 60
            if same_row or above:
                near.append(b)
    for b in near:
        if SECRET_LABEL.search(b.get("text", "")):
            return b["text"]
    return None


def _inside(a: dict[str, Any], b: dict[str, Any], pad: float = 4) -> bool:
    return (a["x"] >= b["x"] - pad and a["y"] >= b["y"] - pad and a["x"] + a["w"] <= b["x"] + b["w"] + pad
            and a["y"] + a["h"] <= b["y"] + b["h"] + pad)


def _overlaps(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]


def _owned(w: dict[str, Any], app: str) -> bool:
    return w["owner"].lower() == re.sub(r"\.app$", "", app.strip(), flags=re.I).lower()


def pick_main(windows: list[dict[str, Any]], app: str) -> dict[str, Any] | None:
    """The app's frontmost normal window that is not a sheet inside a bigger one."""
    mine = [w for w in windows if _owned(w, app) and w["layer"] == 0 and w["w"] >= 80 and w["h"] >= 60 and w["alpha"] > 0]
    for w in mine:  # CGWindowList is front to back
        if not any(o is not w and o["w"] * o["h"] > w["w"] * w["h"] and _inside(w, o) for o in mine):
            return w
    return None


def alert_windows(windows: list[dict[str, Any]], main: dict[str, Any], app: str) -> list[dict[str, Any]]:
    """Sheets, app-modal alerts and system dialogs that are up right now."""
    out = []
    for w in windows:
        if w["id"] == main["id"] or w["alpha"] <= 0.05 or w["w"] < 60 or w["h"] < 40:
            continue
        if _owned(w, app) and ((w["layer"] == 0 and _inside(w, main)) or w["layer"] == MODAL_LAYER):
            out.append(w)
        elif w["owner"] in SYSTEM_DIALOG_OWNERS and w["layer"] >= 0:
            out.append(w)
    return out


def covering_window(windows: list[dict[str, Any]], gx: float, gy: float) -> dict[str, Any] | None:
    """Topmost normal or floating window under a global point (menus, Dock and overlays skipped)."""
    for w in windows:
        if not 0 <= w["layer"] < 20 or w["alpha"] <= 0.05 or w["owner"] in OVERLAY_OWNERS:
            continue
        if w["x"] <= gx < w["x"] + w["w"] and w["y"] <= gy < w["y"] + w["h"]:
            return w
    return None


def parse_combo(combo: str) -> tuple[list[tuple[int, int]], int | None, str]:
    """'cmd+shift+a' -> ([(55, cmd), (56, shift)], 0, 'a'). Unknown single chars get code None (typed)."""
    combo = combo.strip()
    parts = [p for p in re.split(r"\+(?=.)", combo.lower())] if len(combo) > 1 else [combo.lower()]
    *mods, key = parts
    for m in mods:
        if m not in MODS:
            raise Stop(f"unknown modifier '{m}' in '{combo}'")
    if key not in KEYCODES and len(key) != 1:
        raise Stop(f"unknown key '{key}' in '{combo}'")
    if key not in KEYCODES and mods:
        raise Stop(f"'{combo}': use a plain key with modifiers")
    return [MODS[m] for m in mods], KEYCODES.get(key), key


def submit_label(combo: str) -> str | None:
    """Keys that submit, send or delete get the same guard as a click on such a button."""
    mods, _code, key = parse_combo(combo)
    if key in ("return", "enter"):
        return f"submit ({combo} key)"
    if key in ("delete", "backspace", "forwarddelete") and any(m[1] == MODS["cmd"][1] for m in mods):
        return f"delete ({combo} shortcut)"
    return None


def _digits(text: str) -> str:
    """OCR reads a lone zero as the letter O."""
    t = text.strip()
    return "0" if t in ("O", "o") else t


def read_field(boxes: list[dict[str, Any]], f: dict[str, Any], win: dict[str, Any]) -> str | None:
    """One value: a regex match (nth or the largest text), or the value right of, below or above a label."""
    pool = [b for b in boxes if in_region(b, f.get("region"), win)]
    numeric = f.get("number", True)
    if "regex" in f:
        rx = re.compile(f["regex"], re.I)
        hits = []
        for b in pool:
            m = rx.search(_digits(b["text"]) if numeric else b["text"])
            if m:
                hits.append((b, (m.group(1) if m.groups() else m.group(0)).strip()))
        if not hits:
            return None
        if f.get("pick") == "largest":
            return max(hits, key=lambda h: h[0]["h"])[1]
        n = int(f.get("nth", 0))
        return hits[n][1] if -len(hits) <= n < len(hits) else None
    lab = find_target(pool, {"text": f["label"], "match": "exact"}, win) or find_target(pool, {"text": f["label"]}, win)
    if lab is None:
        return None
    want = f.get("pick", "right")
    lx, ly = center(lab)
    best, dist = None, 1e9
    for b in pool:
        if b is lab or (numeric and not NUM.match(_digits(b["text"]))):
            continue
        bx, by = center(b)
        if want == "right" and abs(by - ly) < max(lab["h"], b["h"]) * 0.7 and bx > lx:
            d = bx - lx
        elif want == "below" and by > ly and abs(bx - lx) < max(60, lab["w"]):
            d = (by - ly) + abs(bx - lx)
        elif want == "above" and by < ly and abs(bx - lx) < max(60, lab["w"]):
            d = (ly - by) + abs(bx - lx)
        else:
            continue
        if d < dist:
            best, dist = b, d
    if best is None:
        return None
    return _digits(best["text"]) if numeric else best["text"]


_CALC_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def calc(expr: str) -> str:
    """Safe + - * / on plain numbers, for checks like {"calc": "{a}*{b}"}. Code, never Jev."""
    if len(expr) > 200:
        raise Stop("calc expression too long")

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _CALC_OPS:
            return _CALC_OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = ev(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        raise Stop(f"calc only does + - * / on numbers: '{expr}'")
    try:
        value = ev(ast.parse(expr.replace(",", "").replace("×", "*").replace("−", "-"), mode="eval"))
    except (SyntaxError, ZeroDivisionError) as exc:
        raise Stop(f"calc could not work out '{expr}': {exc}") from None
    return str(int(value)) if float(value).is_integer() else repr(round(value, 10))


def resolve_calc(obj: Any) -> Any:
    if isinstance(obj, dict):
        if set(obj) == {"calc"}:
            return calc(str(obj["calc"]))
        return {k: resolve_calc(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_calc(v) for v in obj]
    return obj


def public_box(b: dict[str, Any], win: dict[str, Any]) -> dict[str, Any]:
    cx, cy = center(b)
    return {"text": b["text"], "x": round(b["x"]), "y": round(b["y"]), "w": round(b["w"]), "h": round(b["h"]),
            "fx": round(cx / win["w"], 3), "fy": round(cy / win["h"], 3), "conf": b.get("conf")}


def ax_trusted() -> bool | None:
    try:
        lib = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except (OSError, AttributeError):
        return None


# ------------------------------------------------------------------ macOS layer (pyobjc, loaded lazily)

_OBJC: dict[str, Any] = {}


def objc() -> dict[str, Any]:
    if not _OBJC:
        if sys.platform != "darwin":
            raise SetupError("the screen engine runs on macOS only")
        try:
            import AppKit
            import Foundation
            import Quartz
            import Vision
        except ImportError:
            raise SetupError(INSTALL) from None
        _OBJC.update(Q=Quartz, V=Vision, A=AppKit, F=Foundation)
    return _OBJC


class Screen:
    """One read: the screenshot, OCR boxes in window points, and the window."""

    def __init__(self, path: Path | None, boxes: list[dict[str, Any]], win: dict[str, Any],
                 alerts: list[dict[str, Any]] | None = None):
        self.path, self.boxes, self.win, self.alerts = path, boxes, win, alerts or []

    @property
    def lines(self) -> list[str]:
        return [b["text"] for b in self.boxes]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def has(self, needle: str) -> bool:
        return needle.lower() in self.text.lower()


class MacIO:
    """Everything that touches the Mac. Tests swap in a fake."""

    def __init__(self) -> None:
        o = objc()
        self.Q, self.V, self.A, self.F = o["Q"], o["V"], o["A"], o["F"]

    # windows and capture
    def windows(self) -> list[dict[str, Any]]:
        Q = self.Q
        out = []
        opts = Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements
        for w in Q.CGWindowListCopyWindowInfo(opts, Q.kCGNullWindowID) or []:
            b = w.get("kCGWindowBounds") or {}
            out.append({"id": int(w.get("kCGWindowNumber", 0)), "pid": int(w.get("kCGWindowOwnerPID", 0)),
                        "owner": str(w.get("kCGWindowOwnerName") or ""), "title": str(w.get("kCGWindowName") or ""),
                        "layer": int(w.get("kCGWindowLayer", 0)), "alpha": float(w.get("kCGWindowAlpha", 1)),
                        "x": float(b.get("X", 0)), "y": float(b.get("Y", 0)),
                        "w": float(b.get("Width", 0)), "h": float(b.get("Height", 0))})
        return out

    def capture(self, win: dict[str, Any], path: Path) -> float:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        r = subprocess.run(["screencapture", "-x", "-o", "-l", str(win["id"]), str(path)],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not path.exists() or path.stat().st_size == 0:
            raise Stop(f"screencapture failed ({r.stderr.strip()[:120] or 'no image'}); "
                       "grant Screen Recording to the app running this script")
        return png_size(path)[0] / max(1.0, win["w"])

    def ocr(self, path: Path, win: dict[str, Any]) -> list[dict[str, Any]]:
        V, F = self.V, self.F
        handler = V.VNImageRequestHandler.alloc().initWithURL_options_(F.NSURL.fileURLWithPath_(str(path)), None)
        req = V.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(V.VNRequestTextRecognitionLevelAccurate)
        req.setUsesLanguageCorrection_(False)
        req.setRecognitionLanguages_(os.environ.get("JEV_OCR_LANGS", "en-US").split(","))
        try:
            req.setRevision_(2)  # revision 3 silently drops lone "0" digits
        except Exception:  # noqa: BLE001
            pass
        ok, err = handler.performRequests_error_([req], None)
        if not ok:
            raise Stop(f"Vision OCR failed: {err}")
        boxes = []
        for obs in req.results() or []:
            cand = obs.topCandidates_(1)
            if not cand:
                continue
            c, bb = cand[0], obs.boundingBox()
            boxes.append({"text": str(c.string()), "conf": round(float(c.confidence()), 2), "_cand": c,
                          **to_win(bb.origin.x, bb.origin.y, bb.size.width, bb.size.height, win["w"], win["h"])})
        boxes.sort(key=lambda b: (round(b["y"] / 6), b["x"]))
        return boxes

    def narrow(self, win: dict[str, Any]) -> Callable[[dict, str], dict]:
        """Tighter box for a needle inside a longer OCR line, when Vision can give one."""
        def go(box: dict[str, Any], needle: str) -> dict[str, Any]:
            i = box["text"].lower().find(needle.lower())
            if i < 0 or len(needle) == len(box["text"]) or "_cand" not in box:
                return box
            try:
                res = box["_cand"].boundingBoxForRange_error_(self.F.NSMakeRange(i, len(needle)), None)
                rect = res[0] if isinstance(res, tuple) else res
                if rect is not None:
                    bb = rect.boundingBox()
                    return {**box, "text": needle, **to_win(bb.origin.x, bb.origin.y, bb.size.width, bb.size.height,
                                                            win["w"], win["h"])}
            except Exception:  # noqa: BLE001
                pass
            return box
        return go

    # apps
    def _pump(self) -> None:
        self.F.NSRunLoop.currentRunLoop().runUntilDate_(self.F.NSDate.dateWithTimeIntervalSinceNow_(0.01))

    def front(self) -> tuple[int, str]:
        self._pump()  # NSWorkspace only refreshes its front app on the run loop
        app = self.A.NSWorkspace.sharedWorkspace().frontmostApplication()
        return (int(app.processIdentifier()), str(app.localizedName() or "")) if app is not None else (0, "")

    def activate(self, pid: int, quick: bool = False) -> bool:
        app = self.A.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None:
            return False
        app.activateWithOptions_(self.A.NSApplicationActivateIgnoringOtherApps)
        for _ in range(16):
            if self.front()[0] == pid:
                return True
            time.sleep(0.05)
        url = app.bundleURL()
        if quick or url is None:
            return False
        subprocess.run(["open", "-a", str(url.path())], capture_output=True, timeout=15)
        for _ in range(40):
            if self.front()[0] == pid:
                return True
            time.sleep(0.05)
        return False

    def open_app(self, name: str) -> None:
        r = subprocess.run(["open", "-a", name], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise Stop(f"could not open '{name}': {r.stderr.strip()[:160]}")

    # input
    def _post(self, ev: Any) -> None:
        self.Q.CGEventPost(self.Q.kCGHIDEventTap, ev)

    def _mouse(self, kind: Any, x: float, y: float, clicks: int = 0) -> None:
        Q = self.Q
        ev = Q.CGEventCreateMouseEvent(None, kind, (x, y), Q.kCGMouseButtonLeft)
        if clicks:
            Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, clicks)
        Q.CGEventSetFlags(ev, 0)  # a key the user is holding must not turn this into a cmd-click
        self._post(ev)

    def click(self, gx: float, gy: float, clicks: int = 1, hold: float = 0.0) -> None:
        Q = self.Q
        self._mouse(Q.kCGEventMouseMoved, gx, gy)
        time.sleep(0.03)
        for n in range(1, clicks + 1):
            self._mouse(Q.kCGEventLeftMouseDown, gx, gy, n)
            time.sleep(max(0.04, hold))
            self._mouse(Q.kCGEventLeftMouseUp, gx, gy, n)
            if n < clicks:
                time.sleep(0.07)

    def drag(self, p0: tuple[float, float], p1: tuple[float, float], steps: int = 20, delay: float = 0.02) -> None:
        Q = self.Q
        self._mouse(Q.kCGEventMouseMoved, *p0)
        self._mouse(Q.kCGEventLeftMouseDown, *p0, clicks=1)
        time.sleep(0.12)
        for i in range(1, steps + 1):
            t = i / steps
            self._mouse(Q.kCGEventLeftMouseDragged, p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t, 1)
            time.sleep(delay)
        time.sleep(0.05)
        self._mouse(Q.kCGEventLeftMouseUp, *p1, clicks=1)

    def scroll(self, gx: float, gy: float, pixels: int, ticks: int = 16) -> None:
        """Pixel-unit wheel scroll. Positive scrolls down (shows what is below), like the web engine."""
        Q = self.Q
        self._mouse(Q.kCGEventMouseMoved, gx, gy)
        per = -int(pixels / ticks) or (-1 if pixels > 0 else 1)  # the wheel's sign is the other way round
        for _ in range(ticks):
            ev = Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitPixel, 1, per)
            Q.CGEventSetLocation(ev, (gx, gy))
            self._post(ev)
            time.sleep(0.016)

    def key(self, combo: str) -> None:
        """Real modifier key down/up events around the key: a bare flag on the letter is
        dropped by some apps (cmd+v arrives as 'v')."""
        Q = self.Q
        mods, code, key = parse_combo(combo)
        if code is None:
            self.type_text(key)
            return
        flags = 0
        for mcode, mask in mods:
            flags |= mask
            ev = Q.CGEventCreateKeyboardEvent(None, mcode, True)
            Q.CGEventSetFlags(ev, flags)
            self._post(ev)
            time.sleep(0.05)
        for down in (True, False):
            ev = Q.CGEventCreateKeyboardEvent(None, code, down)
            Q.CGEventSetFlags(ev, flags)
            self._post(ev)
            time.sleep(0.04)
        for mcode, mask in reversed(mods):
            flags &= ~mask
            ev = Q.CGEventCreateKeyboardEvent(None, mcode, False)
            Q.CGEventSetFlags(ev, flags)
            self._post(ev)
            time.sleep(0.02)

    def type_text(self, text: str) -> None:
        """Unicode key events, one character each (any language, no keyboard layout)."""
        Q = self.Q
        for ch in text:
            if ch in "\r\n":
                self.key("return")
                continue
            if ch == "\t":
                self.key("tab")
                continue
            for down in (True, False):
                ev = Q.CGEventCreateKeyboardEvent(None, 0, down)
                Q.CGEventSetFlags(ev, 0)
                Q.CGEventKeyboardSetUnicodeString(ev, len(ch.encode("utf-16-le")) // 2, ch)
                self._post(ev)
                time.sleep(0.006)
            time.sleep(0.014)

    # cursor and clipboard
    def mouse_pos(self) -> Any:
        return self.Q.CGEventGetLocation(self.Q.CGEventCreate(None))

    def warp(self, pos: Any) -> None:
        self.Q.CGWarpMouseCursorPosition(pos)
        self.Q.CGAssociateMouseAndMouseCursorPosition(True)

    def clip_snapshot(self) -> list[dict[str, Any]]:
        pb = self.A.NSPasteboard.generalPasteboard()
        items = []
        for item in pb.pasteboardItems() or []:
            kept = {}
            for kind in item.types() or []:
                data = item.dataForType_(kind)
                if data is not None:
                    kept[str(kind)] = data
            items.append(kept)
        return items

    def clip_set(self, text: str) -> None:
        pb = self.A.NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, self.A.NSPasteboardTypeString)

    def clip_restore(self, items: list[dict[str, Any]]) -> None:
        pb = self.A.NSPasteboard.generalPasteboard()
        pb.clearContents()
        objs = []
        for kept in items:
            item = self.A.NSPasteboardItem.alloc().init()
            for kind, data in kept.items():
                item.setData_forType_(data, kind)
            objs.append(item)
        if objs:
            pb.writeObjects_(objs)


# ------------------------------------------------------------------ engine

ACTIONS = {"open_app", "tap", "double_tap", "long_press", "type", "paste", "key", "scroll", "scroll_until", "drag",
           "read", "collect", "pick", "check", "screenshot"}


class Engine:
    """Runs screen steps against one app's front window."""

    def __init__(self, app: str, args: argparse.Namespace, log: common.RunLog, budget: common.Budget,
                 io: Any = None):
        self.app, self.args, self.log, self.budget = app, args, log, budget
        self.io = io if io is not None else MacIO()
        self.win: dict[str, Any] | None = None
        self.shot = Path(tempfile.gettempdir()) / f"jev-screen-{os.getpid()}.png"
        self.extra: dict[str, Any] = {}
        self.reads = 0
        self.used_approval = False
        self.alert_evidence: tuple[str, str] | None = None
        self.known_dialogs: set[int] | None = None
        self._mouse: Any = None
        self._front: tuple[int, str] | None = None
        self._clip: list[dict[str, Any]] | None = None

    # -- reading
    def observe(self, strict: bool = True) -> Screen:
        wins = self.io.windows()
        win = pick_main(wins, self.app)
        if win is None:
            raise Stop(f"no {self.app} window on screen. Open it (open_app step) or bring it back, then rerun.")
        if denied_app(win["owner"]):
            raise Stop(f"refused: {win['owner']} is on the kit's never-drive list", needs_human=True)
        alerts = alert_windows(wins, win, self.app)
        if self.known_dialogs is None:  # system dialogs already up, clear of the window: noted, not fatal
            self.known_dialogs = {a["id"] for a in alerts if not _owned(a, self.app) and not _overlaps(a, win)}
            if self.known_dialogs:
                self.log.data.setdefault("warnings", []).append(
                    "a system dialog was already on screen (" + ", ".join(sorted({a["owner"] for a in alerts})) +
                    "); left alone, input only goes to the app's own window")
        fresh = [a for a in alerts if a["id"] not in self.known_dialogs]
        if fresh and strict:
            self._alert_stop(fresh[0])
        win["scale"] = self.io.capture(win, self.shot)
        self.win = win
        s = Screen(self.shot, self.io.ocr(self.shot, win), win, alerts)
        self.reads += 1
        if strict:
            common.check_human_gate(s.text)
        return s

    def _alert_stop(self, w: dict[str, Any]) -> None:
        path = self.log.shot_path(f"alert from {w['owner']}")
        text = "(unreadable)"
        try:
            self.io.capture(w, path)
            text = " ".join(b["text"] for b in self.io.ocr(path, w)) or text
        except Stop:
            path = None  # type: ignore[assignment]
        self.alert_evidence = (str(path) if path else "", text)
        raise Stop(f"a macOS alert or sheet is up ({w['owner']}): {common.clip(text, 300)}. Not clicking through it.",
                   needs_human=w["owner"] in SYSTEM_DIALOG_OWNERS)

    def wait_for(self, pred: Callable[[Screen], bool], timeout: float, interval: float = 0.12) -> Screen:
        end = time.monotonic() + timeout
        while True:
            s = self.observe()
            if pred(s) or time.monotonic() >= end:
                return s
            time.sleep(interval)

    def settle(self, timeout: float = 2.0) -> Screen:
        """Read until two reads in a row show the same text (animations, scroll momentum)."""
        end = time.monotonic() + timeout
        prev = None
        while True:
            s = self.observe()
            if s.lines == prev or time.monotonic() >= end:
                return s
            prev = s.lines
            time.sleep(0.06)

    def find(self, s: Screen, t: dict[str, Any]) -> dict[str, Any] | None:
        return find_target(s.boxes, t, s.win, self._narrow(s.win))

    def _narrow(self, win: dict[str, Any]) -> Callable[[dict, str], dict] | None:
        return self.io.narrow(win) if hasattr(self.io, "narrow") else None

    def point(self, s: Screen, t: dict[str, Any]) -> tuple[float, float, dict | None] | None:
        return point_for(s.boxes, t, s.win, self._narrow(s.win))

    def wait_stable(self, t: dict[str, Any], timeout: float) -> Screen:
        """Wait until the target is on screen and has not moved between two reads."""
        end = time.monotonic() + timeout
        last = None
        while True:
            s = self.wait_for(lambda sc: self.find(sc, t) is not None, max(0.05, end - time.monotonic()))
            b = self.find(s, t)
            if b is not None and last is not None and abs(b["x"] - last["x"]) < 2 and abs(b["y"] - last["y"]) < 2:
                return s
            last = b
            if time.monotonic() >= end:
                return s
            time.sleep(0.05)

    def target_screen(self, step: dict[str, Any], timeout: float) -> tuple[Screen, float, float, dict | None]:
        t = step.get("target")
        if not isinstance(t, dict):
            raise Stop(f"step '{step['id']}' needs a target")
        s = self.observe() if "frac" in t else self.wait_stable(t, timeout)
        p = self.point(s, t)
        if p is None:
            raise Stop(f"target not on screen: {json.dumps(t, ensure_ascii=False)}")
        return (s, *p)

    # -- input, always guarded
    def _begin_input(self) -> None:
        if self._mouse is None:
            self._mouse = self.io.mouse_pos()
            self._front = self.io.front()

    def ensure_front(self, gx: float | None = None, gy: float | None = None) -> None:
        """The app must be in front, and the point must land on its window, before any input."""
        assert self.win is not None
        pid, name = self.io.front()
        if pid != self.win["pid"]:
            self.io.activate(self.win["pid"])
            pid, name = self.io.front()
        if denied_app(name):
            raise Stop(f"refused: {name} is in front and is on the never-drive list", needs_human=True)
        if pid != self.win["pid"]:
            raise Stop(f"could not bring {self.app} to the front ({name or 'another app'} stayed there); no input sent")
        if gx is None:
            return
        for attempt in range(3):
            cover = covering_window(self.io.windows(), gx, gy)
            if cover is not None and cover["id"] == self.win["id"]:
                return
            time.sleep(0.15)
        who = cover["owner"] if cover else "nothing"
        raise Stop(f"the point to click is covered by {who}, not {self.app}'s window; no click sent")

    def guard(self, label: str, step: dict[str, Any], picked: bool = False) -> None:
        note = common.guard_click(label, step, self.args.approved, picked=picked)
        if note:
            self.extra.setdefault("guard", []).append(note)
            if note.startswith("approved:"):
                self.used_approval = True

    def click_at(self, s: Screen, px: float, py: float, box: dict | None, step: dict[str, Any],
                 picked: bool = False, clicks: int = 1, hold: float = 0.0) -> None:
        under = [b["text"] for b in boxes_at(s.boxes, px, py)]
        label = " | ".join(dict.fromkeys(([box["text"]] if box else []) + under)) or step["id"]
        self.guard(label, step, picked)
        if not (0 <= px <= s.win["w"] and 0 <= py <= s.win["h"]):
            raise Stop(f"click point {round(px)},{round(py)} is outside the {self.app} window")
        gx, gy = s.win["x"] + px, s.win["y"] + py
        self._begin_input()
        self.ensure_front(gx, gy)
        self.io.click(gx, gy, clicks, hold)
        self.extra["clicked"] = common.clip(label, 80)

    def refuse_secret(self, s: Screen, field: dict | None) -> None:
        hit = secret_label(s.boxes, field)
        if hit:
            raise Stop(f"refused: '{common.clip(hit, 60)}' marks a password, code or card field. "
                       "The kit never types these; the user types it.", needs_human=True)

    def focus_field(self, step: dict[str, Any], timeout: float) -> None:
        if not step.get("target"):
            self.refuse_secret(self.observe(), None)
            return
        s, px, py, box = self.target_screen(step, timeout)
        dx, dy = step["target"].get("offset", [0, 0])
        field = {**box, "x": box["x"] + dx, "y": box["y"] + dy} if box else {"text": "", "x": px - 6, "y": py - 8, "w": 12, "h": 16}
        self.refuse_secret(s, field)
        if box is not None:
            self.refuse_secret(s, box)
        self.click_at(s, px, py, box, step)
        time.sleep(float(step.get("focus_wait", 0.15)))

    # -- steps
    def do(self, step: dict[str, Any]) -> None:
        act = step["do"]
        if act not in ACTIONS:
            raise Stop(f"unknown screen step '{act}' in '{step['id']}'")
        timeout = float(step.get("timeout", self.args.timeout))
        getattr(self, "do_" + act)(step, timeout)
        self.checkpoint(step, timeout)

    def do_open_app(self, step: dict[str, Any], timeout: float) -> None:
        name = str(step.get("app") or self.app)
        if denied_app(name):
            raise Stop(f"refused: {name} is on the kit's never-drive list", needs_human=True)
        base = re.sub(r"\.app$", "", Path(name.rstrip("/")).name, flags=re.I)
        here = step.get("already_if")
        if here and pick_main(self.io.windows(), base) is not None:
            self.app = base
            s = self.observe()
            if any(s.has(t) for t in ([here] if isinstance(here, str) else here)):
                self._begin_input()
                self.ensure_front()
                self.extra["skipped"] = "already open"
                return
        self._begin_input()
        self.io.open_app(name)
        wait = max(timeout, 10)
        end = time.monotonic() + wait
        while pick_main(self.io.windows(), base) is None:
            if time.monotonic() >= end:
                raise Stop(f"opened {name} but no window owned by '{base}' showed in {wait:.0f}s. "
                           "Set the recipe's app to the window owner's name.")
            time.sleep(0.15)
        self.app = base
        self.settle(1.5)
        self.ensure_front()

    def do_tap(self, step: dict[str, Any], timeout: float, clicks: int = 1, hold: float = 0.0) -> None:
        s, px, py, box = self.target_screen(step, timeout)
        self.click_at(s, px, py, box, step, clicks=clicks, hold=hold)

    def do_double_tap(self, step: dict[str, Any], timeout: float) -> None:
        self.do_tap(step, timeout, clicks=2)

    def do_long_press(self, step: dict[str, Any], timeout: float) -> None:
        self.do_tap(step, timeout, hold=float(step.get("hold", 0.8)))

    def do_type(self, step: dict[str, Any], timeout: float) -> None:
        text = str(step.get("text", ""))
        if not text:
            raise Stop(f"step '{step['id']}' has no text to type")
        self.focus_field(step, timeout)
        if "\n" in text or "\r" in text:
            self.guard("submit (return key in typed text)", step)
        self._begin_input()
        self.ensure_front()
        self.io.type_text(text)

    def do_paste(self, step: dict[str, Any], timeout: float) -> None:
        text = str(step.get("text", ""))
        if not text:
            raise Stop(f"step '{step['id']}' has no text to paste")
        self.focus_field(step, timeout)
        self._begin_input()
        self.ensure_front()
        if self._clip is None:
            self._clip = self.io.clip_snapshot()
        self.io.clip_set(text)
        time.sleep(0.05)
        self.io.key("cmd+v")
        time.sleep(0.25)

    def do_key(self, step: dict[str, Any], timeout: float) -> None:
        keys = step.get("keys", step.get("key"))
        combos = keys if isinstance(keys, list) else [keys] if keys else []
        if not combos:
            raise Stop(f"step '{step['id']}' needs keys, e.g. \"return\" or \"cmd+a\"")
        for combo in combos:
            label = submit_label(str(combo))
            if label:
                self.guard(label, step)
        if self.win is None:
            self.observe()
        self._begin_input()
        self.ensure_front()
        for combo in combos:
            self.io.key(str(combo))
            time.sleep(0.03)

    def _scroll(self, step: dict[str, Any], s: Screen | None = None) -> None:
        s = s or self.observe()
        fx, fy = step.get("at", [0.5, 0.5])
        gx, gy = s.win["x"] + s.win["w"] * fx, s.win["y"] + s.win["h"] * fy
        self._begin_input()
        self.ensure_front(gx, gy)
        self.io.scroll(gx, gy, int(step.get("pixels", 400)))

    def do_scroll(self, step: dict[str, Any], timeout: float) -> None:
        self._scroll(step)

    def do_scroll_until(self, step: dict[str, Any], timeout: float) -> None:
        t = step.get("target") or {}
        s = self.settle()
        for _ in range(int(step.get("max", 8))):
            if self.find(s, t) is not None:
                return
            self._scroll(step, s)
            time.sleep(0.12)
            s = self.settle()
        if self.find(s, t) is None:
            raise Stop(f"scrolled {step.get('max', 8)} times and never saw {json.dumps(t, ensure_ascii=False)}")

    def do_drag(self, step: dict[str, Any], timeout: float) -> None:
        s, px, py, box = self.target_screen(step, timeout)
        to = step.get("to") or {}
        if "frac" in to:
            qx, qy = s.win["w"] * to["frac"][0], s.win["h"] * to["frac"][1]
        elif "text" in to or "regex" in to:
            p = self.point(s, to)
            if p is None:
                raise Stop(f"drag end not on screen: {json.dumps(to, ensure_ascii=False)}")
            qx, qy = p[0], p[1]
        else:
            qx, qy = px + float(to.get("dx", 0)), py + float(to.get("dy", 0))
        label = " | ".join(([box["text"]] if box else []) + [b["text"] for b in boxes_at(s.boxes, px, py)]) or step["id"]
        self.guard(label, step)
        for x, y in ((px, py), (qx, qy)):
            if not (0 <= x <= s.win["w"] and 0 <= y <= s.win["h"]):
                raise Stop("drag would leave the app window")
        g0, g1 = (s.win["x"] + px, s.win["y"] + py), (s.win["x"] + qx, s.win["y"] + qy)
        self._begin_input()
        self.ensure_front(*g0)
        self.io.drag(g0, g1, int(step.get("steps", 20)), float(step.get("step_delay", 0.02)))

    def do_read(self, step: dict[str, Any], timeout: float) -> None:
        fields = step.get("fields") or {}
        required = [k for k, f in fields.items() if f.get("required")]
        end = time.monotonic() + timeout
        s = self.settle()
        while True:
            rec: dict[str, Any] = {k: read_field(s.boxes, f, s.win) for k, f in fields.items()}
            missing = [k for k in required if rec[k] is None]
            if not missing or time.monotonic() >= end:
                break
            time.sleep(0.12)
            s = self.observe()
        if step.get("lines", not fields):
            rec["lines"] = [b["text"] for b in s.boxes if in_region(b, step.get("region"), s.win)]
        unread = [k for k in fields if rec.get(k) is None]
        if unread:
            rec["unread"] = unread
        if step.get("screenshot"):
            rec["screenshot"] = self._save_shot(s, step.get("save_as", step["id"]), step.get("region"))
        self.log.save(step.get("save_as", step["id"]), rec)
        self.extra["read"] = {k: v for k, v in rec.items() if k in fields}
        if missing:
            raise Stop(f"could not read required fields: {', '.join(missing)}")

    def do_collect(self, step: dict[str, Any], timeout: float) -> None:
        seen: set[str] = set()
        lines: list[str] = []
        prev = None
        pages = int(step.get("pages", 3))
        for i in range(pages):
            s = self.settle()
            page = [b["text"] for b in s.boxes if in_region(b, step.get("region"), s.win)]
            for t in page:
                if t not in seen:
                    seen.add(t)
                    lines.append(t)
            if page == prev:
                break  # nothing moved: the end of the list
            prev = page
            if i < pages - 1:
                self._scroll(step, s)
                time.sleep(0.12)
        self.log.save(step.get("save_as", step["id"]), [{"text": t} for t in lines])
        self.extra["lines"] = len(lines)

    def do_pick(self, step: dict[str, Any], timeout: float) -> None:
        """Jev chooses one OCR line among the recipe's candidates, then it is tapped or saved."""
        rx = re.compile(step["candidates_regex"], re.I) if step.get("candidates_regex") else None
        region = step.get("region")
        if rx is None and not region:
            raise Stop(f"pick '{step['id']}' needs candidates_regex or region")
        if not step.get("instructions"):
            raise Stop(f"pick '{step['id']}' needs instructions")
        s = self.settle()
        cands = [b for b in s.boxes if (rx is None or rx.search(b["text"])) and in_region(b, region, s.win)]
        if not cands:
            raise Stop(f"no candidates on screen for pick '{step['id']}'")
        texts = [f"{b['text']} (at {round(center(b)[0] / s.win['w'] * 100)}% across, "
                 f"{round(center(b)[1] / s.win['h'] * 100)}% down)" for b in cands]
        context = {"screen_text": common.clip(s.text, 3000)}
        if step.get("context"):
            context["goal"] = step["context"]
        index, answer = common.jev_pick(self.budget, step["instructions"], texts, context,
                                        float(step.get("threshold", 0.6)))
        self.extra["jev"] = answer
        if index is None:
            raise Stop(f"Jev saw no clear match for '{step['instructions']}' among {len(cands)} candidates")
        b = cands[index]
        self.extra["picked"] = b["text"]
        if step.get("then", "tap") == "tap":
            px, py = center(b)
            self.click_at(s, px, py, b, step, picked=True)
        else:
            self.log.save(step.get("save_as", step["id"]), {"text": b["text"], **public_box(b, s.win)})

    def do_check(self, step: dict[str, Any], timeout: float) -> None:
        """Code checks on saved data: {"from": "result", "where": [{"field": "value", "eq": "42"}]}."""
        if "from" not in step:
            return
        saved = self.log.data["saved"]
        if step["from"] not in saved:
            raise Stop(f"check '{step['id']}' needs saved data called '{step['from']}'")
        value = saved[step["from"]]
        items = value if isinstance(value, list) else [value]
        rows = [i for i in items if isinstance(i, dict)]
        kept = common.filter_items(rows, resolve_calc(step.get("where", [])))
        ok = bool(kept) if step.get("need") == "any" else (len(kept) == len(items) and bool(items))
        self.extra["passed"] = f"{len(kept)}/{len(items)}"
        if not ok:
            raise Stop(f"check '{step['id']}' failed on {common.clip(value, 200)}")

    def do_screenshot(self, step: dict[str, Any], timeout: float) -> None:
        s = self.settle()
        path = self._save_shot(s, step.get("save_as", step["id"]), step.get("region"))
        self.extra["screenshot"] = path
        if step.get("save_as"):
            self.log.save(step["save_as"], path)

    def _save_shot(self, s: Screen, label: str, region: list[float] | None) -> str:
        dest = self.log.shot_path(label)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if s.path is None or not Path(s.path).exists():
            return ""
        if region:
            x, y, w, h = crop_rect(region, s.win, s.win.get("scale", 2.0))
            subprocess.run(["sips", "-c", str(h), str(w), "--cropOffset", str(y), str(x), str(s.path), "--out", str(dest)],
                           capture_output=True, timeout=20)
        else:
            dest.write_bytes(Path(s.path).read_bytes())
        return str(dest)

    def checkpoint(self, step: dict[str, Any], timeout: float) -> None:
        exp, judge = step.get("expect"), step.get("judge")
        if not exp and not judge:
            if step["do"] not in ("read", "collect", "check", "screenshot", "pick"):
                time.sleep(float(step.get("settle", 0.2)))
            return
        if exp:
            limit = float(exp.get("timeout", timeout))
            s = self.wait_for(lambda sc: common.expect_text(exp, sc.text)[0], limit)
            ok, why = common.expect_text(exp, s.text)
            if not ok:
                raise Stop(f"expected screen did not come within {limit:g}s ({why})")
        if judge:
            time.sleep(float(judge.get("settle", 0.1)))
            s = self.settle(1.5)
            text = "\n".join(b["text"] for b in s.boxes if in_region(b, judge.get("region"), s.win))  # send the minimum
            ok, answer = common.jev_check(self.budget, judge, text, judge.get("context") or {"step": step["id"]})
            self.extra["jev"] = answer
            if not ok:
                raise Stop(f"Jev check failed: '{judge['instructions']}' -> {json.dumps(answer)}")

    # -- wrap-up
    def evidence(self, label: str) -> tuple[str | None, str | None]:
        if self.alert_evidence:
            return self.alert_evidence[0] or None, self.alert_evidence[1]
        try:
            win = pick_main(self.io.windows(), self.app)
            if win is None:
                return None, None
            path = self.log.shot_path(f"stopped at {label}")
            self.io.capture(win, path)
            return str(path), "\n".join(b["text"] for b in self.io.ocr(path, win))
        except Exception:  # noqa: BLE001
            return None, None

    def restore(self) -> None:
        """Put back the clipboard, the cursor and the app that was in front."""
        for fn in (self._restore_clip, self._restore_mouse, self._restore_front):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    def _restore_clip(self) -> None:
        if self._clip is not None:
            self.io.clip_restore(self._clip)
            self._clip = None

    def _restore_mouse(self) -> None:
        if self._mouse is not None:
            self.io.warp(self._mouse)

    def _restore_front(self) -> None:
        if self._front and not getattr(self.args, "stay", False) and self.win and self._front[0] != self.win["pid"]:
            self.io.activate(self._front[0], quick=True)


# ------------------------------------------------------------------ running

def execute(recipe: dict[str, Any], args: argparse.Namespace, io: Any = None,
            saved: dict[str, Any] | None = None) -> tuple[dict[str, Any], Engine | None]:
    """Run every step; stop cleanly with evidence on the first failure."""
    log = common.RunLog(recipe, "screen")
    if saved:
        log.data["saved"].update(saved)
    budget = getattr(args, "budget", None) or common.budget_from(args)
    current = recipe["steps"][0].get("id", "start")
    eng = None
    try:
        app = str(recipe.get("app") or "")
        if not app:
            raise Stop('a screen recipe names its app: "app": "<App name>"')
        if denied_app(app):
            raise Stop(f"refused: {app} is on the kit's never-drive list", needs_human=True)
        eng = Engine(app, args, log, budget, io)
        for step in recipe["steps"]:
            current = step["id"]
            started = time.monotonic()
            eng.extra = {}
            try:
                if common.run_code_step(step, log, budget):
                    continue
                eng.do(step)
                log.step(current, ms=(time.monotonic() - started) * 1000, **eng.extra)
            except Stop as exc:
                if step.get("optional") and not exc.needs_human:
                    log.step(current, ok=False, ms=(time.monotonic() - started) * 1000, skipped=exc.reason)
                    continue
                raise
        log.data["screen_reads"] = eng.reads
        result = log.finish(budget)
    except Stop as exc:
        shot, text = eng.evidence(current) if eng else (None, None)
        if eng:
            log.data["screen_reads"] = eng.reads
        result = log.stop(exc, current, shot, text, budget)
    finally:
        if eng:
            eng.restore()
    if args.approved and eng and eng.used_approval:
        common.consume_approval(args.approved)
    return result, eng


def record(name: str, step: dict[str, Any], app: str) -> Path:
    return common.record_step(name, "screen", step, header={"app": app})


# ------------------------------------------------------------------ CLI

def cmd_doctor(args: argparse.Namespace) -> int:
    rep: dict[str, Any] = {"python": sys.version.split()[0], "executable": sys.executable}
    fixes = []
    try:
        Q = objc()["Q"]
        rep["pyobjc"] = True
        rep["screen_recording"] = bool(Q.CGPreflightScreenCaptureAccess())
        rep["post_events"] = bool(Q.CGPreflightPostEventAccess())
    except SetupError as exc:
        rep.update(pyobjc=False, screen_recording=None, post_events=None)
        fixes.append(str(exc))
    rep["accessibility"] = ax_trusted()
    rep["jev_key"] = client.key_available()
    rep["denied_apps"] = DENIED_SHOWN
    if rep["screen_recording"] is False:
        fixes.append("System Settings > Privacy & Security > Screen Recording: allow the app that runs this script")
    if rep["accessibility"] is False or rep["post_events"] is False:
        fixes.append("System Settings > Privacy & Security > Accessibility: allow the app that runs this script")
    if not rep["jev_key"]:
        fixes.append("no Jev key: set OPENROUTER_API_KEY or JEV_KEY_COMMAND (runs still work with --no-jev)")
    rep["fix"] = fixes
    ready = rep["pyobjc"] and rep["screen_recording"] and rep["accessibility"] is not False and rep["post_events"]
    rep["status"] = "done" if ready else "stopped"
    common.emit(rep)
    return 0 if ready else 2


def cmd_screen(args: argparse.Namespace) -> int:
    if denied_app(args.app):
        common.emit({"status": "needs_human", "reason": f"refused: {args.app} is on the kit's never-drive list"})
        return 2
    region = [float(v) for v in args.region.split(",")] if args.region else None
    if region is not None and len(region) != 4:
        raise Stop("--region is x0,y0,x1,y1 as fractions of the window")
    log = common.RunLog({"name": "screen-reads"}, "screen")
    eng = Engine(args.app, args, log, common.Budget(enabled=False))
    started = time.monotonic()
    s = eng.observe(strict=False)
    ms = round((time.monotonic() - started) * 1000)
    dest = common.RUNS / "screen-reads" / f"{re.sub(r'[^A-Za-z0-9 ._-]+', '-', args.app)} - screen.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(Path(s.path).read_bytes())
    boxes = [b for b in s.boxes if in_region(b, region, s.win)]
    win = {k: round(s.win[k], 2) for k in ("x", "y", "w", "h", "scale")}
    out: dict[str, Any] = {"status": "done", "app": args.app, "window": win, "ms": ms, "screenshot": str(dest)}
    if args.boxes:
        out["boxes"] = [public_box(b, s.win) for b in boxes]
    else:
        out["lines"] = [b["text"] for b in boxes]
    if s.alerts:
        out["alerts"] = [{"owner": a["owner"], "layer": a["layer"]} for a in s.alerts]
    gate = common.HUMAN_GATES.search(s.text)
    if gate:
        out["human_gate"] = gate.group(0)
    log.path.unlink(missing_ok=True)  # a plain read leaves no run log
    common.emit(out)
    return 0


def cmd_step(args: argparse.Namespace) -> int:
    raw = args.step
    try:
        if raw.startswith("@"):
            raw = Path(raw[1:]).expanduser().read_text(encoding="utf-8")
        step = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise Stop(f"the step is not valid JSON: {exc}") from None
    if not isinstance(step, dict) or not step.get("do"):
        raise Stop("a step is a JSON object with a 'do'")
    step = common.fill(step, common.parse_params(args.param))
    step.setdefault("id", step["do"])
    recipe = {"name": "screen-step", "engine": "screen", "app": args.app, "steps": [step]}
    carry = common.RUNS / "screen-step" / "saved.json"  # saved values carry over between step calls
    saved: dict[str, Any] = {}
    if carry.is_file() and not args.fresh:
        try:
            saved = json.loads(carry.read_text(encoding="utf-8"))
        except ValueError:
            saved = {}
    result, eng = execute(recipe, args, saved=saved if isinstance(saved, dict) else {})
    carry.parent.mkdir(parents=True, exist_ok=True)
    carry.write_text(json.dumps(result.get("saved", {}), ensure_ascii=False), encoding="utf-8")
    if result.get("status") == "done":
        if args.record:
            result["recorded"] = str(record(args.record, step, args.app))
        try:
            if eng is not None:
                s = eng.observe(strict=False)
                result["screen_after"] = s.lines[:120]
        except Stop:
            pass
    common.emit(result)
    return common.exit_code(result)


def cmd_run(args: argparse.Namespace) -> int:
    recipe = common.load_recipe(args.recipe, common.parse_params(args.param))
    if recipe.get("engine") != "screen":
        raise Stop(f"recipe '{recipe['name']}' is a {recipe.get('engine')} recipe; run it with {recipe.get('engine')}.py")
    result, _eng = execute(recipe, args)
    common.emit(result)
    return common.exit_code(result)


def _reexec_with_kit_python() -> None:
    """Without pyobjc here, rerun under the kit's own venv when it exists."""
    if os.environ.get("JEV_KIT_REEXEC"):
        return
    cand = os.environ.get("JEV_KIT_PYTHON") or str(KIT_PYTHON)
    if Path(cand).is_file() and os.path.abspath(cand) != os.path.abspath(sys.executable):
        os.environ["JEV_KIT_REEXEC"] = "1"
        os.execv(cand, [cand, str(Path(__file__).resolve()), *sys.argv[1:]])


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="pyobjc, permissions, Jev key")
    sc = sub.add_parser("screen", help="one OCR read of the app's front window")
    sc.add_argument("--app", required=True)
    sc.add_argument("--boxes", action="store_true", help="positions in window points and fractions")
    sc.add_argument("--region", help="x0,y0,x1,y1 as fractions of the window")
    st = sub.add_parser("step", help="run one JSON step (mapping)")
    st.add_argument("step", help="the step as JSON, or @file.json")
    st.add_argument("--app", required=True)
    st.add_argument("--record", metavar="NAME", help="append the step to draft NAME when it works")
    st.add_argument("--fresh", action="store_true", help="forget values saved by earlier step calls")
    rn = sub.add_parser("run", help="replay a recipe")
    rn.add_argument("recipe")
    for p in (st, rn):
        common.add_run_args(p)
        p.add_argument("--timeout", type=float, default=6.0, help="seconds to wait for a target or expected screen")
        p.add_argument("--stay", action="store_true", help="leave the app in front afterwards")
    return ap


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "darwin" and not all(importlib.util.find_spec(m) for m in ("Quartz", "Vision", "AppKit")):
        _reexec_with_kit_python()
    args = build_parser().parse_args(argv)
    try:
        return {"doctor": cmd_doctor, "screen": cmd_screen, "step": cmd_step, "run": cmd_run}[args.cmd](args)
    except SetupError as exc:
        common.emit({"status": "error", "error": str(exc)})
        return 1
    except Stop as exc:
        common.emit({"status": "needs_human" if exc.needs_human else "error", "error": exc.reason})
        return 2 if exc.needs_human else 1


if __name__ == "__main__":
    sys.exit(main())
