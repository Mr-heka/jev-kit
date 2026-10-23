#!/usr/bin/env python3
"""Web replay engine: map a browser flow once, replay it with Playwright, no big model in the loop.

  web.py open [--show] [--url URL]   start the kit's own browser (own profile, never the user's Chrome)
  web.py snapshot [--max N]          what is on the page, with ready-to-paste targets
  web.py step '<json>' [--record N]  run one step on the open session, record it if it worked
  web.py run NAME_OR_PATH            replay a recipe (uses the open session, else a private headless browser)
  web.py status | close | doctor

JSON on stdout. Exit 0 done, 2 stopped or needs a person, 1 error. Page text is
untrusted data: it only reaches Jev as state, and Jev only chooses among
candidates the recipe defined. The kit never types credentials.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("NODE_NO_WARNINGS", "1")  # Playwright's driver prints deprecation noise on stderr
from jevkit import client, common  # noqa: E402
from jevkit.common import Stop  # noqa: E402

BROWSER = common.HOME / "browser"
PROFILE = BROWSER / "profile"
SESSION = BROWSER / "session.json"
STEP_STATE = BROWSER / "last-step.json"
DEFAULT_TIMEOUT = 8.0
OPTIONAL_TIMEOUT = 2.5
TEXT_LIMIT = 3000
WEB_STEPS = {"goto", "click", "fill", "press", "select", "hover", "check", "scroll", "scroll_until",
             "read", "collect", "pick", "back", "screenshot"}
TARGETED = {"click", "fill", "select", "hover"}
TARGET_KEYS = ("role", "text", "label", "placeholder", "testid", "alt", "title", "css")
LAUNCH_FLAGS = [
    "--no-first-run", "--no-default-browser-check", "--password-store=basic", "--use-mock-keychain",
    "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding", "--disable-breakpad", "--disable-component-update",
    "--disable-default-apps", "--disable-search-engine-choice-screen", "--metrics-recording-only",
    "--window-size=1280,900",
]

# Fields the kit refuses to type into. The user logs in by hand in `open --show`.
SENSITIVE = re.compile(
    r"\b(?:pass ?(?:word|wd|code|phrase)|pwd\b|pin(?:\b|code|number)|otp\b|one ?time|2fa|mfa|totp|"
    r"verification ?code|security ?code|card ?(?:number|no\b|num)|cc ?(?:num|no\b)|credit ?card|debit ?card|"
    r"cvv|cvc|csc\b|ccv|card ?verification|expiry|expiration|exp ?(?:date|month|year|mm|yy)|iban|bsb\b|"
    r"routing ?number|sort ?code|swift|bank|account ?(?:number|no\b|num)|acct\b|tax ?file|tfn\b|ssn\b|"
    r"social ?security|(?:sms|auth|access|login|sign ?in|6 ?digit|one ?time) ?code|otc\b|"
    r"api ?key|access ?token|auth ?token|secret ?key|private ?key|recovery ?(?:code|phrase)|seed ?phrase)", re.I)
SENSITIVE_AUTOCOMPLETE = re.compile(r"(?:^|\s)(?:current-password|new-password|one-time-code|cc-[a-z-]+)(?:\s|$)", re.I)

JS_HELPERS = r"""
const _t = (s) => (s || '').replace(/\s+/g, ' ').trim();
const accName = (e) => {
  const aria = _t(e.getAttribute('aria-label')); if (aria) return aria;
  const by = e.getAttribute('aria-labelledby');
  if (by) { const s = _t(by.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(x => x.innerText).join(' ')); if (s) return s; }
  const tag = e.tagName.toLowerCase(), type = (e.getAttribute('type') || '').toLowerCase();
  if (tag === 'input' && ['submit', 'button', 'reset'].includes(type)) return _t(e.value || type);
  if (tag === 'input' && type === 'image') return _t(e.alt || e.value);
  if (['input', 'select', 'textarea'].includes(tag)) {
    if (e.labels && e.labels.length) return _t([...e.labels].map(l => l.innerText).join(' '));
    return _t(e.getAttribute('title') || e.getAttribute('placeholder'));
  }
  const txt = _t(e.innerText || e.textContent); if (txt) return txt;
  const img = e.querySelector && e.querySelector('img[alt]'); if (img && _t(img.alt)) return _t(img.alt);
  return _t(e.getAttribute('title'));
};
const roleOf = (e) => {
  const r = e.getAttribute('role'); if (r) return r.split(' ')[0];
  const tag = e.tagName.toLowerCase(), type = (e.getAttribute('type') || 'text').toLowerCase();
  if (tag === 'a') return e.hasAttribute('href') ? 'link' : null;
  if (tag === 'button') return 'button';
  if (tag === 'select') return (e.multiple || e.size > 1) ? 'listbox' : 'combobox';
  if (tag === 'textarea') return 'textbox';
  if (/^h[1-6]$/.test(tag)) return 'heading';
  if (tag === 'input') {
    const map = {checkbox: 'checkbox', radio: 'radio', submit: 'button', button: 'button', reset: 'button',
                 image: 'button', range: 'slider', number: 'spinbutton', search: 'searchbox'};
    if (map[type]) return e.hasAttribute('list') && type === 'search' ? 'combobox' : map[type];
    if (['text', 'email', 'tel', 'url'].includes(type)) return e.hasAttribute('list') ? 'combobox' : 'textbox';
    return null;
  }
  if (e.isContentEditable) return 'textbox';
  return null;
};
const isVisible = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; };
const cssPath = (el) => {
  const parts = []; let e = el;
  while (e && e.nodeType === 1 && e !== document.documentElement) {
    if (e.id && document.querySelectorAll('#' + CSS.escape(e.id)).length === 1) { parts.unshift('#' + CSS.escape(e.id)); break; }
    let p = e.tagName.toLowerCase();
    const sib = e.parentElement ? [...e.parentElement.children].filter(c => c.tagName === e.tagName) : [];
    if (sib.length > 1) p += ':nth-of-type(' + (sib.indexOf(e) + 1) + ')';
    parts.unshift(p); e = e.parentElement;
  }
  return parts.join(' > ');
};
"""
NAME_JS = "(e) => {" + JS_HELPERS + " return accName(e); }"
NAMES_JS = "(els) => {" + JS_HELPERS + " return els.map(e => accName(e) || _t(e.innerText)); }"
ATTRS_JS = "(els, a) => els.map(e => (a === 'href' || a === 'src') && e[a] ? e[a] : (e.getAttribute(a) || ''))"
HREFS_JS = "(els) => els.map(e => e.href || (e.closest('a') ? e.closest('a').href : null) || (e.querySelector && e.querySelector('a[href]') ? e.querySelector('a[href]').href : null))"
VALUE_JS = "(e, a) => (a === 'href' || a === 'src') && e[a] ? e[a] : (a ? e.getAttribute(a) : (e.innerText || e.textContent || '').replace(/\\s+/g, ' ').trim())"
FIELD_INFO_JS = """(e) => ({tag: e.tagName.toLowerCase(), type: (e.getAttribute('type') || '').toLowerCase(),
  autocomplete: e.getAttribute('autocomplete') || '', name: e.getAttribute('name') || '', id: e.id || '',
  aria: e.getAttribute('aria-label') || '', placeholder: e.getAttribute('placeholder') || '',
  label: e.labels ? [...e.labels].map(l => l.innerText).join(' ') : ''})"""
FORM_SUBMITS_JS = "(e) => {" + JS_HELPERS + """
  const el = e || document.activeElement; const f = el && (el.form || (el.closest && el.closest('form')));
  if (!f) return '';
  return [...f.querySelectorAll('button, input[type=submit], input[type=image]')]
    .filter(b => (b.getAttribute('type') || 'submit').toLowerCase() === 'submit' || b.type === 'image')
    .map(accName).join(' | '); }"""
COLLECT_JS = """(els, arg) => els.slice(0, arg.max).map(row => {
  const out = {};
  for (const [name, s] of Object.entries(arg.specs)) {
    const el = s.css ? row.querySelector(s.css) : row;
    if (!el) { out[name] = null; continue; }
    out[name] = s.attr ? ((s.attr === 'href' || s.attr === 'src') && el[s.attr] ? el[s.attr] : el.getAttribute(s.attr))
                       : (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
  }
  return out; })"""
SNAP_JS = "(max) => {" + JS_HELPERS + """
  const SEL = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=checkbox],' +
              '[role=radio],[role=menuitem],[role=option],[role=switch],[role=combobox],[role=textbox],[role=searchbox],[contenteditable=""],[contenteditable=true]';
  document.querySelectorAll('[data-jev-snap]').forEach(e => e.removeAttribute('data-jev-snap'));
  const all = [...document.querySelectorAll(SEL)].filter(e => isVisible(e) && !(e.tagName === 'INPUT' && e.type === 'hidden'));
  const items = all.map((e, i) => { const r = e.getBoundingClientRect();
    return {e, i, inView: r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth}; });
  items.sort((a, b) => (b.inView - a.inView) || (a.i - b.i));
  const out = items.slice(0, max).map(({e, inView}, k) => {
    e.setAttribute('data-jev-snap', String(k));
    const o = {k, tag: e.tagName.toLowerCase(), role: roleOf(e), name: accName(e), in_view: inView, css: cssPath(e)};
    if (e.tagName === 'INPUT') o.type = (e.getAttribute('type') || 'text').toLowerCase();
    if (e.getAttribute('placeholder')) o.placeholder = e.getAttribute('placeholder');
    if (e.labels && e.labels.length) o.label = _t([...e.labels].map(l => l.innerText).join(' '));
    if (e.getAttribute('data-testid')) o.testid = e.getAttribute('data-testid');
    if (e.tagName === 'A') o.href = e.getAttribute('href');
    if (e.tagName === 'SELECT') o.options = [...e.options].slice(0, 12).map(x => _t(x.text));
    if (['checkbox', 'radio'].includes(e.type)) o.checked = e.checked;
    return o; });
  const headings = [...document.querySelectorAll('h1,h2,h3,h4,[role=heading]')].filter(isVisible).slice(0, 30)
    .map(h => ({level: /^H\\d$/.test(h.tagName) ? +h.tagName[1] : +(h.getAttribute('aria-level') || 2), text: _t(h.innerText).slice(0, 120)}));
  return {total: all.length, items: out, headings}; }"""


def _short(exc: BaseException) -> str:
    lines = [ln for ln in str(exc).splitlines() if ln.strip()]
    return common.clip(lines[0] if lines else type(exc).__name__, 300)


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def domain_allowed(url: str, allow: list[str]) -> bool:
    if not allow or urlparse(url).scheme not in {"http", "https"}:
        return True
    host = host_of(url)
    return any(host == d or host.endswith("." + d) for d in allow)


# ------------------------------------------------------------------ session

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cdp(port: int, path: str = "/json/version") -> Any:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1.5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def session() -> dict[str, Any] | None:
    """The open session, or None (a dead one is cleared)."""
    try:
        data = json.loads(SESSION.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if _alive(int(data.get("pid", 0))) and _cdp(int(data["port"])) is not None:
        return data
    SESSION.unlink(missing_ok=True)
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _chromium_path() -> str:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        return p.chromium.executable_path


def _kill(data: dict[str, Any]) -> bool:
    pid = int(data.get("pid", 0))
    if not pid or not _alive(pid):
        return False
    os.kill(pid, signal.SIGTERM)  # Chrome shuts down cleanly on TERM and saves cookies
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)  # helpers left in its process group
    except (ProcessLookupError, PermissionError):
        pass
    return True


def cmd_open(args: argparse.Namespace) -> dict[str, Any]:
    cur = session()
    if cur and cur.get("headless") == (not args.show):
        if args.url:
            with Browser(need_session=True) as br:
                br.page.goto(args.url, wait_until="domcontentloaded")
        return {"ok": True, "already_open": True, **cur}
    if cur:
        _kill(cur)
    exe = _chromium_path()
    if not Path(exe).exists():
        return {"ok": False, "error": "chromium missing: python3 -m playwright install chromium"}
    PROFILE.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    cmd = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={PROFILE}", *LAUNCH_FLAGS]
    if not args.show:
        cmd.append("--headless=new")
    cmd.append(args.url or "about:blank")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    for _ in range(150):
        if _cdp(port) is not None:
            break
        if proc.poll() is not None:
            return {"ok": False, "error": f"chromium exited ({proc.returncode}); is another session using {PROFILE}?"}
        time.sleep(0.1)
    else:
        _kill({"pid": proc.pid})
        return {"ok": False, "error": "chromium did not open its debugging port in 15 s"}
    proc.returncode = 0  # detached on purpose; stops Popen warning that it is still running
    data = {"port": port, "pid": proc.pid, "headless": not args.show, "profile": str(PROFILE), "started": common.now_iso()}
    common._write_json(SESSION, data)
    return {"ok": True, **data,
            "next": "log in by hand in this window, then `web.py close`" if args.show else "web.py snapshot"}


def cmd_close(_args: argparse.Namespace) -> dict[str, Any]:
    cur = session()
    closed = _kill(cur) if cur else False
    SESSION.unlink(missing_ok=True)
    return {"ok": True, "closed": closed}


def cmd_status(_args: argparse.Namespace) -> dict[str, Any]:
    cur = session()
    if not cur:
        return {"ok": True, "open": False}
    pages = [{"url": p.get("url"), "title": common.clip(p.get("title", ""), 100)}
             for p in (_cdp(int(cur["port"]), "/json/list") or []) if p.get("type") == "page"]
    return {"ok": True, "open": True, **cur, "pages": pages}


class Browser:
    """The page to drive: the open session over CDP, or a private browser on the kit profile for one run."""

    def __init__(self, show: bool = False, need_session: bool = False):
        self.show, self.need_session = show, need_session
        self.attached = False
        self.pw = self.browser = self.context = self.page = None

    def __enter__(self) -> "Browser":
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        try:
            cur = session()
            if cur:
                self.browser = self.pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cur['port']}")
                self.context = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
                self.attached = True
            elif self.need_session:
                raise Stop("no browser session is open; run `web.py open` (or `open --show` to log in by hand)")
            else:
                PROFILE.mkdir(parents=True, exist_ok=True)
                self.context = self.pw.chromium.launch_persistent_context(
                    str(PROFILE), headless=not self.show, channel="chromium", args=LAUNCH_FLAGS[:-1],
                    viewport={"width": 1280, "height": 900})
            pages = [p for p in self.context.pages if not p.url.startswith(("devtools:", "chrome-extension:"))]
            self.page = pages[-1] if pages else self.context.new_page()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_exc: Any) -> None:
        try:
            if self.context is not None and not self.attached:
                self.context.close()
        finally:
            if self.pw is not None:
                self.pw.stop()  # an attached session keeps running


# ------------------------------------------------------------------ targets

def locate(scope: Any, t: Any, nth: int | None = None) -> Any:
    """Recipe target -> Playwright locator over visible matches. Prefer role+name; css is the last resort."""
    if not isinstance(t, dict) or not any(k in t for k in TARGET_KEYS):
        raise Stop(f"a target needs one of {', '.join(TARGET_KEYS)}, got {common.clip(t, 120)}")
    if t.get("within"):
        inner = locate(scope, t["within"])
        scope = inner if "nth" in t["within"] else inner.first
    exact = bool(t.get("exact", False))
    if "role" in t:
        kw: dict[str, Any] = {"exact": exact}
        if t.get("name") is not None:
            kw["name"] = str(t["name"])
        if "level" in t:
            kw["level"] = int(t["level"])
        loc = scope.get_by_role(t["role"], **kw)
    elif "text" in t:
        loc = scope.get_by_text(str(t["text"]), exact=exact)
    elif "label" in t:
        loc = scope.get_by_label(str(t["label"]), exact=exact)
    elif "placeholder" in t:
        loc = scope.get_by_placeholder(str(t["placeholder"]), exact=exact)
    elif "testid" in t:
        loc = scope.get_by_test_id(str(t["testid"]))
    elif "alt" in t:
        loc = scope.get_by_alt_text(str(t["alt"]), exact=exact)
    elif "title" in t:
        loc = scope.get_by_title(str(t["title"]), exact=exact)
    else:
        loc = scope.locator(str(t["css"]))
    if t.get("has_text"):
        loc = loc.filter(has_text=str(t["has_text"]))
    loc = loc.filter(visible=True)
    n = t.get("nth", nth)
    return loc.nth(int(n)) if n is not None else loc


def _words(value: str) -> str:
    return re.sub(r"[_\-.\[\]:]+", " ", re.sub(r"([a-z])([A-Z])", r"\1 \2", value or ""))


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def sensitive_field(info: dict[str, Any], text: str = "") -> str | None:
    """Why the kit will not type here, or None."""
    if info.get("type") == "password":
        return "it is a password field"
    if SENSITIVE_AUTOCOMPLETE.search(info.get("autocomplete", "")):
        return f"its autocomplete is '{info['autocomplete']}'"
    for key in ("name", "id", "label", "aria", "placeholder", "autocomplete"):
        hit = SENSITIVE.search(_words(str(info.get(key, ""))))
        if hit:
            return f"its {key} looks like '{hit.group(0)}'"
    digits = re.sub(r"[\s-]", "", text or "")
    if digits.isdigit() and 13 <= len(digits) <= 19 and _luhn(digits):
        return "the text looks like a card number"
    return None


# ------------------------------------------------------------------ runner

class StepLog(common.RunLog):
    """RunLog for one-off `step` calls. Saved values carry over between calls, so mapping
    can chain collect -> filter -> rank the way the replay will."""

    def __init__(self) -> None:
        self.stamp = time.strftime("%Y%m%d-%H%M%S")
        self.dir = BROWSER / "steps"
        self.path = STEP_STATE
        self.t0 = time.monotonic()
        try:
            saved = json.loads(STEP_STATE.read_text(encoding="utf-8")).get("saved", {})
        except (OSError, ValueError):
            saved = {}
        self.data = {"recipe": "step", "engine": "web", "params": {}, "started": common.now_iso(),
                     "status": "running", "steps": [], "saved": saved}
        self.write()


class Runner:
    """Runs steps against one page: act, follow a new tab, fence the domain, check, log."""

    def __init__(self, br: Browser, log: common.RunLog, budget: common.Budget, approved: str | None = None,
                 allow: list[str] | None = None, timeout: float = DEFAULT_TIMEOUT):
        self.br, self.log, self.budget, self.approved = br, log, budget, approved
        self.allow = [d.lower().lstrip(".") for d in allow or [] if d]
        self.timeout = timeout
        self.used_approval = False

    @property
    def page(self) -> Any:
        return self.br.page

    # -- reading the page
    def page_text(self) -> str:
        for _ in range(3):
            try:
                return self.page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            except Exception:  # navigation in flight: the old document is gone
                time.sleep(0.1)
        return ""

    def title(self) -> str:
        try:
            return self.page.title()
        except Exception:
            return ""

    def fence(self) -> None:
        try:  # ask the page itself: page.url is only refreshed when Playwright pumps events
            url = self.page.evaluate("() => location.href") or self.page.url
        except Exception:
            url = self.page.url
        if not domain_allowed(url, self.allow):
            raise Stop(f"left the allowed sites: now on {host_of(url)}, allowed {', '.join(self.allow)}")

    def guard(self, label: str, step: dict[str, Any], picked: bool = False) -> str | None:
        note = common.guard_click(label, step, self.approved, picked=picked, recipe=self.log.data.get("recipe"))
        if note and note.startswith("approved"):
            self.used_approval = True
        return note

    # -- one step
    def run_step(self, step: dict[str, Any]) -> None:
        if common.run_code_step(step, self.log, self.budget):
            return
        do = step["do"]
        if do not in WEB_STEPS:
            raise Stop(f"unknown step '{do}' in '{step['id']}'")
        started = time.monotonic()
        timeout = float(step.get("timeout", OPTIONAL_TIMEOUT if step.get("optional") else self.timeout))
        extra: dict[str, Any] = {}
        if step.get("optional") and do in TARGETED | {"press"} and step.get("target"):
            if not self._present(step["target"], step.get("nth"), timeout):
                self.log.step(step["id"], ms=(time.monotonic() - started) * 1000, skipped="target not on the page")
                return
        if do != "goto":
            self.fence()  # a redirect since the last step must not receive input
        before = len(self.br.context.pages)
        getattr(self, "do_" + do)(step, timeout, extra)
        if len(self.br.context.pages) > before:  # the click opened a tab: follow it
            self.br.page = self.br.context.pages[-1]
            self.br.page.wait_for_load_state("domcontentloaded")
            extra["new_tab"] = True
        self.fence()
        self.checkpoint(step, extra)
        self.fence()
        self.log.step(step["id"], ms=(time.monotonic() - started) * 1000, url=self.page.url, **extra)

    def checkpoint(self, step: dict[str, Any], extra: dict[str, Any]) -> None:
        exp, judge = step.get("expect"), step.get("judge")
        timeout = float((exp or {}).get("timeout", step.get("timeout", self.timeout)))
        end = time.monotonic() + timeout
        while True:
            text = self.page_text()
            common.check_human_gate(text)
            ok, why = self._expect_ok(exp, text)
            if ok:
                break
            if time.monotonic() >= end:
                raise Stop(f"step '{step['id']}' did not reach the expected page ({why}) within {timeout:g} s")
            time.sleep(0.15)
        if judge:
            ok, answer = common.jev_check(self.budget, judge, text,
                                          {"url": self.page.url, "title": self.title(), "step": step["id"]})
            extra["jev"] = answer
            if not ok:
                raise Stop(f"Jev says this is not the expected page: {judge['instructions']}")

    def _expect_ok(self, exp: dict[str, Any] | None, text: str) -> tuple[bool, str]:
        ok, why = common.expect_text(exp, text)
        if not ok or not exp:
            return ok, why
        for key, actual in (("url_contains", self.page.url), ("title_contains", None)):
            wanted = exp.get(key)
            if not wanted:
                continue
            actual = self.title() if actual is None else actual
            for w in [wanted] if isinstance(wanted, str) else wanted:
                if str(w).lower() not in actual.lower():
                    return False, f"{key.split('_')[0]} has no '{w}' (is '{common.clip(actual, 80)}')"
        return True, ""

    def _present(self, target: dict[str, Any], nth: Any, timeout: float) -> bool:
        try:
            locate(self.page, target, nth).first.wait_for(state="visible", timeout=timeout * 1000)
            return True
        except Exception:
            return False

    def resolve(self, step: dict[str, Any], timeout: float, key: str = "target") -> Any:
        target = step.get(key)
        if target is None:
            raise Stop(f"step '{step['id']}' needs a '{key}'")
        loc = locate(self.page, target, step.get("nth"))
        try:
            loc.first.wait_for(state="visible", timeout=timeout * 1000)
        except Exception:
            raise Stop(f"step '{step['id']}': target not on the page: {common.clip(target, 160)}") from None
        if "nth" not in target and step.get("nth") is None:
            count = loc.count()
            if count > 1:
                raise Stop(f"step '{step['id']}': target matches {count} elements; add \"nth\" or a tighter name "
                           f"(run `web.py snapshot`)")
        return loc.first

    def _label(self, loc: Any, target: dict[str, Any]) -> str:
        try:
            actual = loc.evaluate(NAME_JS) or ""
        except Exception:
            actual = ""
        named = str(target.get("name") or target.get("text") or target.get("label") or "")
        return " | ".join(x for x in dict.fromkeys([actual, named]) if x)

    def _settle(self, timeout: float) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=timeout * 1000)
        except Exception:
            pass

    # -- web steps
    def do_goto(self, step: dict[str, Any], timeout: float, extra: dict[str, Any]) -> None:
        url = step["url"]
        if not domain_allowed(url, self.allow):
            raise Stop(f"'{host_of(url)}' is outside allow_domains ({', '.join(self.allow)})")
        resp = self.page.goto(url, wait_until="domcontentloaded", timeout=max(timeout, 20) * 1000)
        if resp is not None:
            extra["http"] = resp.status

    def do_click(self, step: dict[str, Any], timeout: float, extra: dict[str, Any]) -> None:
        loc = self.resolve(step, timeout)
        note = self.guard(self._label(loc, step["target"]), step)
        if note:
            extra["guard"] = note
        loc.click(timeout=timeout * 1000)
        self._settle(timeout)

    def do_fill(self, step: dict[str, Any], timeout: float, extra: dict[str, Any]) -> None:
        loc = self.resolve(step, timeout)
        text = str(step.get("text", ""))
        why = sensitive_field(loc.evaluate(FIELD_INFO_JS), text)
        if why:
            raise Stop(f"step '{step['id']}': the kit never types here ({why}). The user types this by hand "
                       "(log in once with `web.py open --show`).", needs_human=True)
        loc.fill(text, timeout=timeout * 1000)
        extra["chars"] = len(text)

    def do_press(self, step: dict[str, Any], timeout: float, extra: dict[str, Any]) -> None:
        key = str(step["key"])
        loc = self.resolve(step, timeout) if step.get("target") else None
        focus = loc or self.page.locator(":focus").first
        has_focus = loc is not None or focus.count() > 0
        last = key.split("+")[-1].lower()
        if last in {"enter", "numpadenter", "space", " "}:
            # Enter or Space activates the focused button or link and can submit its form:
            # guard on the element's own name plus the form's submit buttons.
            own = self._label(focus, step.get("target") or {}) if has_focus else ""
            submits = focus.evaluate(FORM_SUBMITS_JS) if has_focus else self.page.evaluate(FORM_SUBMITS_JS, None)
            note = self.guard(" | ".join(x for x in (own, submits) if x), step)
            if note:
                extra["guard"] = note
        elif len(last) == 1 and has_focus:  # a typed character: same rule as fill
            why = sensitive_field(focus.evaluate(FIELD_INFO_JS))
            if why:
                raise Stop(f"step '{step['id']}': the kit never types here ({why}).", needs_human=True)
        if loc:
            loc.press(key, timeout=timeout * 1000)
        else:
            self.page.keyboard.press(key)
        self._settle(timeout)

    def do_select(self, step: dict[str, Any], timeout: float, extra: dict[str, Any]) -> None:
        loc = self.resolve(step, timeout)
        option = str(step["option"])
        try:
            loc.select_option(label=option, timeout=timeout * 1000)
        except Exception:
            loc.select_option(value=option, timeout=timeout * 1000)
        extra["option"] = option

    def do_hover(self, step: dict[str, Any], timeout: float, extra: dict[str, Any]) -> None:
        self.resolve(step, timeout).hover(timeout=timeout * 1000)

    def do_check(self, step: dict[str, Any], timeout: float, extra: dict[str, Any]) -> None:
        pass

    def do_scroll(self, step: dict[str, Any], timeout: float, extra: dict[str, Any]) -> None:
        self.page.mouse.wheel(0, float(step.get("pixels", 700)))
        time.sleep(0.25)

    def do_scroll_until(self, step: dict[str, Any], timeout: float, extra: dict[str, Any]) -> None:
        loc = locate(self.page, step["target"], step.get("nth"))
        for n in range(int(step.get("max", 15)) + 1):
            if loc.count():
                loc.first.scroll_into_view_if_needed(timeout=timeout * 1000)
                extra["scrolls"] = n
                return
            self.page.mouse.wheel(0, float(step.get("pixels", 700)))
            time.sleep(0.35)
        raise Stop(f"step '{step['id']}': {common.clip(step['target'], 120)} not found after {step.get('max', 15)} scrolls")

    def _value(self, loc: Any, spec: Any) -> Any:
        spec = spec if isinstance(spec, dict) else {"css": str(spec)}
        if any(k in spec for k in TARGET_KEYS) and not loc.count():
            return None
        try:
            return loc.first.evaluate(VALUE_JS, spec.get("attr"))
        except Exception:
            return None

    def do_read(self, step: dict[str, Any], timeout: float, extra: dict[str, Any]) -> None:
        out: dict[str, Any] = {}
        unread: list[str] = []
        required = set(step.get("required", []))
        for name, spec in (step.get("fields") or {}).items():
            target, attr = (spec["target"], spec.get("attr")) if isinstance(spec, dict) and "target" in spec else (spec, None)
            loc = locate(self.page, target)
            wait = timeout if name in required else min(timeout, 1.5)
            try:
                loc.first.wait_for(state="attached" if attr else "visible", timeout=wait * 1000)
                out[name] = _shape(loc.first.evaluate(VALUE_JS, attr), spec if isinstance(spec, dict) else {})
            except Exception:
                out[name] = None
            if out[name] in (None, ""):
                unread.append(name)
        if step.get("text"):
            out["text"] = common.clip(self.page_text(), int(step.get("text_limit", TEXT_LIMIT)))
        missing = sorted(required & set(unread))
        if missing:
            raise Stop(f"step '{step['id']}': could not read {', '.join(missing)}")
        self.log.save(step.get("save_as", step["id"]), out)
        if unread:
            extra["unread"] = unread

    def do_collect(self, step: dict[str, Any], timeout: float, extra: dict[str, Any]) -> None:
        specs = {k: (v if isinstance(v, dict) else {"css": str(v)}) for k, v in (step.get("fields") or {}).items()}
        fast = all(set(s) <= {"css", "attr", "regex", "number", "map"} for s in specs.values())
        limit, pages = int(step.get("max", 100)), int(step.get("pages", 1))
        rows: list[dict[str, Any]] = []
        first_seen = None
        for page_no in range(pages):
            loc = locate(self.page, step["rows"])
            try:
                loc.first.wait_for(state="visible", timeout=timeout * 1000)
            except Exception:
                break
            if page_no and first_seen is not None:  # wait for the next page to replace the rows
                end = time.monotonic() + timeout
                while self._value(loc, {}) == first_seen and time.monotonic() < end:
                    time.sleep(0.15)
            first_seen = self._value(loc, {})
            want = limit - len(rows)
            if fast:
                got = loc.evaluate_all(COLLECT_JS, {"specs": specs, "max": want})
            else:
                got = []
                for i in range(min(loc.count(), want)):
                    row = loc.nth(i)
                    got.append({k: self._value(locate(row, s) if any(x in s for x in TARGET_KEYS) else row, s)
                                for k, s in specs.items()})
            rows += [{k: _shape(r.get(k), specs[k]) for k in specs} for r in got]
            if len(rows) >= limit or page_no + 1 >= pages or not step.get("next"):
                break
            nxt = locate(self.page, step["next"])
            if not nxt.count():
                break
            self.guard(self._label(nxt.first, step["next"]), step)
            nxt.first.click(timeout=timeout * 1000)
            self._settle(timeout)
            self.fence()
        extra["rows"] = len(rows)
        if not rows and step.get("required", True):
            raise Stop(f"step '{step['id']}': no rows matched {common.clip(step['rows'], 120)}")
        self.log.save(step.get("save_as", step["id"]), rows)

    def do_pick(self, step: dict[str, Any], timeout: float, extra: dict[str, Any]) -> None:
        loc = locate(self.page, step["from"])
        try:
            loc.first.wait_for(state="visible", timeout=timeout * 1000)
        except Exception:
            raise Stop(f"step '{step['id']}': no candidates match {common.clip(step['from'], 120)}") from None
        texts = loc.evaluate_all(ATTRS_JS, step["attr"]) if step.get("attr") else loc.evaluate_all(NAMES_JS)
        index, answer = common.jev_pick(self.budget, step["instructions"], texts, step.get("context"),
                                        float(step.get("threshold", 0.6)))
        extra["jev"] = answer
        extra["candidates"] = len(texts)
        if index is None:
            raise Stop(f"step '{step['id']}': Jev found no clear match among {len(texts)} candidates")
        chosen = loc.nth(index)
        href = (loc.evaluate_all(HREFS_JS) or [None] * len(texts))[index]
        self.log.save(step.get("save_as", step["id"]), {"index": index, "text": texts[index], "href": href})
        if step.get("then", "click") == "click":
            note = self.guard(self._label(chosen, {}) or texts[index], step, picked=True)
            if note:
                extra["guard"] = note
            chosen.click(timeout=timeout * 1000)
            self._settle(timeout)

    def do_back(self, step: dict[str, Any], timeout: float, extra: dict[str, Any]) -> None:
        self.page.go_back(wait_until="domcontentloaded", timeout=timeout * 1000)

    def do_screenshot(self, step: dict[str, Any], timeout: float, extra: dict[str, Any]) -> None:
        path = self.log.shot_path(step.get("label", step["id"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=bool(step.get("full_page")))
        extra["screenshot"] = str(path)

    # -- stopping
    def stopped(self, stop: Stop, step_id: str) -> dict[str, Any]:
        shot: str | None = None
        try:
            path = self.log.shot_path(f"stop {step_id}")
            path.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path))
            shot = str(path)
        except Exception:
            pass
        result = self.log.stop(stop, step_id, shot, self.page_text(), self.budget)
        result["stopped"]["url"] = self.page.url
        self.log.write()
        return result


def _shape(value: Any, spec: dict[str, Any]) -> Any:
    """Post-process a read value in code: regex group, word map, number."""
    if value is None:
        return None
    if spec.get("regex"):
        m = re.search(spec["regex"], str(value))
        value = (m.group(1) if m.groups() else m.group(0)) if m else None
    if value is not None and spec.get("map"):
        value = spec["map"].get(value, value)
    if value is not None and spec.get("number"):
        value = common.parse_number(value)
    return value


def run_recipe(recipe: dict[str, Any], br: Browser, budget: common.Budget, approved: str | None = None,
               timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Replay a loaded recipe on `br`. Returns the run log (status done, stopped or needs_human)."""
    log = common.RunLog(recipe, "web")
    start = recipe.get("start_url")
    first_goto = next((s.get("url") for s in recipe["steps"] if s.get("do") == "goto" and s.get("url")), None)
    allow = recipe.get("allow_domains") or [host_of(u) for u in (start, first_goto) if u and host_of(u)][:1]
    if not allow:  # nothing names a site: local pages (data:, file:) only, any http(s) page stops the run
        allow = ["no-site-allowed.invalid"]
    runner = Runner(br, log, budget, approved, allow, timeout)
    current = "start"
    try:
        try:
            if start and recipe["steps"][0]["do"] != "goto":
                runner.run_step({"id": "start", "do": "goto", "url": start})
            for step in recipe["steps"]:
                current = step["id"]
                runner.run_step(step)
        except Stop as stop:
            return runner.stopped(stop, current)
        except KeyError as exc:
            return runner.stopped(Stop(f"step '{current}' is missing {exc}"), current)
        except Exception as exc:  # Playwright timeouts and browser errors: the page did not match
            return runner.stopped(Stop(f"browser error: {_short(exc)}"), current)
    finally:
        if runner.used_approval and approved:
            common.consume_approval(approved)
    if recipe.get("output"):
        log.data["output"] = log.data["saved"].get(recipe["output"])
    return log.finish(budget)


def make_budget(args: argparse.Namespace) -> common.Budget:
    return common.budget_from(args)


# ------------------------------------------------------------------ commands

def cmd_run(args: argparse.Namespace) -> dict[str, Any]:
    recipe = common.load_recipe(args.recipe, common.parse_params(args.param))
    if recipe["engine"] != "web":
        raise Stop(f"'{recipe['name']}' is a {recipe['engine']} recipe; run it with screen.py")
    with Browser(show=args.show) as br:
        return run_recipe(recipe, br, make_budget(args), args.approved, args.timeout)


def _saved_key(step: dict[str, Any]) -> str | None:
    do = step["do"]
    if step.get("save_as"):
        return step["save_as"]
    if do in {"read", "collect", "pick"}:
        return step["id"]
    return {"filter": step.get("from"), "sort": step.get("from"), "take": step.get("from"),
            "rank": "ranked", "choose": "choice"}.get(do)


def cmd_step(args: argparse.Namespace) -> dict[str, Any]:
    raw = args.step_json
    if raw.startswith("@"):
        raw = Path(raw[1:]).expanduser().read_text(encoding="utf-8")
    try:
        step = json.loads(raw)
    except ValueError as exc:
        raise Stop(f"the step is not valid JSON: {exc}") from None
    if not isinstance(step, dict) or not step.get("do"):
        raise Stop("a step is a JSON object with a 'do'")
    step.setdefault("id", step["do"])
    live = common.fill(step, common.parse_params(args.param))
    draft_allow: list[str] = []
    if args.record:
        try:
            draft_allow = json.loads(common._draft_path(args.record).read_text(encoding="utf-8")).get("allow_domains", [])
        except (OSError, ValueError):
            pass
    if live["do"] not in WEB_STEPS:  # code steps need no browser
        log, budget = StepLog(), make_budget(args)
        try:
            if not common.run_code_step(live, log, budget):
                raise Stop(f"unknown step '{live['do']}'")
        except KeyError as exc:
            return log.stop(Stop(f"the step is missing {exc}"), live["id"], budget=budget)
        except Stop as stop:
            return log.stop(stop, live["id"], budget=budget)
        log.finish(budget)
        out = {"status": "done", "log": log.data["steps"][-1] if log.data["steps"] else {}}
        key = _saved_key(live)
        if key and key in log.data["saved"]:
            out["saved"] = {key: log.data["saved"][key]}
        if budget.calls:
            out["jev"] = budget.summary()
        if args.record:
            out["recorded"] = str(common.record_step(args.record, "web", step))
        return out
    with Browser(need_session=True) as br:
        log = StepLog()
        runner = Runner(br, log, make_budget(args), args.approved, draft_allow, args.timeout)
        url_before = br.page.url
        try:
            try:
                runner.run_step(live)
            except Stop:
                raise
            except KeyError as exc:
                raise Stop(f"the step is missing {exc}") from None
            except Exception as exc:
                raise Stop(f"browser error: {_short(exc)}") from None
        except Stop as stop:
            return runner.stopped(stop, live["id"])
        finally:
            if runner.used_approval and args.approved:
                common.consume_approval(args.approved)
        log.finish(runner.budget)
        out: dict[str, Any] = {"status": "done", "url": br.page.url, "title": runner.title(),
                               "log": log.data["steps"][-1] if log.data["steps"] else {}}
        key = _saved_key(live)
        if key and key in log.data["saved"]:
            out["saved"] = {key: log.data["saved"][key]}
        if runner.budget.calls:
            out["jev"] = runner.budget.summary()
        if args.record:
            start = live["url"] if live["do"] == "goto" else url_before
            hosts = [h for h in dict.fromkeys([host_of(start), host_of(br.page.url)]) if h]
            path = common.record_step(args.record, "web", step, header={"start_url": start, "allow_domains": hosts})
            out["recorded"] = str(path)
            allow = json.loads(path.read_text(encoding="utf-8")).get("allow_domains", [])
            if not domain_allowed(br.page.url, allow):
                out["warning"] = f"now on {host_of(br.page.url)}: add it to allow_domains in {path} if the flow belongs there"
        return out


def cmd_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    with Browser(need_session=True) as br:
        page = br.page
        snap = page.evaluate(SNAP_JS, args.max)
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in snap["items"]:
            if item["role"] and item["name"]:
                groups.setdefault((item["role"], item["name"]), []).append(item)
        for (role, name), items in groups.items():
            target: dict[str, Any] = {"role": role, "name": name, "exact": True} if len(name) <= 100 else \
                {"role": role, "name": name[:80]}
            try:
                order = locate(page, target).evaluate_all("els => els.map(e => e.getAttribute('data-jev-snap'))")
            except Exception:
                order = []
            for item in items:
                if str(item["k"]) in order:
                    item["target"] = dict(target, nth=order.index(str(item["k"]))) if len(order) > 1 else target
        elements = []
        for item in snap["items"]:
            target = item.get("target")
            if not target:
                for key in ("label", "placeholder", "testid"):
                    if item.get(key):
                        target = {key: item[key], "exact": True} if key != "testid" else {key: item[key]}
                        break
                else:
                    target = {"css": item["css"]}
            row = {"role": item["role"], "name": common.clip(item["name"], 120), "target": target}
            for key in ("type", "href", "options", "checked"):
                if key in item:
                    row[key] = item[key]
            if item.get("type") == "password":
                row["note"] = "password field: the kit never types here"
            if not item["in_view"]:
                row["below_fold"] = True
            elements.append(row)
        page.evaluate("() => document.querySelectorAll('[data-jev-snap]').forEach(e => e.removeAttribute('data-jev-snap'))")
        text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        return {"ok": True, "url": page.url, "title": page.title(), "headings": snap["headings"],
                "elements": elements, "shown": len(elements), "total": snap["total"],
                "text": common.clip(text, args.text)}


def cmd_doctor(_args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {"python": sys.version.split()[0]}
    ok = True
    try:
        version = importlib.metadata.version("playwright")
        out["playwright"] = version
        if tuple(int(x) for x in version.split(".")[:2]) < (1, 51):
            out["playwright"] += " (needs 1.51+: pip install -U playwright)"
            ok = False
        exe = _chromium_path()
        out["chromium"] = "installed" if Path(exe).exists() else "missing: python3 -m playwright install chromium"
        ok = ok and Path(exe).exists()
    except Exception as exc:  # not installed, or the driver will not start
        out["playwright"] = f"not ready ({_short(exc)}): pip install playwright && python3 -m playwright install chromium"
        ok = False
    cur = session()
    out["session"] = {"open": True, "port": cur["port"], "headless": cur["headless"]} if cur else {"open": False}
    out["profile"] = str(PROFILE)
    out["jev_key"] = "available" if client.key_available() else "missing: set OPENROUTER_API_KEY or JEV_KEY_COMMAND"
    out["ok"] = ok
    return out


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="web.py", description="Jev kit web replay engine (Playwright).")
    sub = p.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open", help="start the kit's own browser session")
    o.add_argument("--show", action="store_true", help="visible window, e.g. to log in by hand once")
    o.add_argument("--url")
    sub.add_parser("close", help="close the session")
    sub.add_parser("status", help="is a session open, which pages")
    sub.add_parser("doctor", help="check playwright, chromium, session and Jev key (no inference)")
    s = sub.add_parser("snapshot", help="page map with ready-to-paste targets")
    s.add_argument("--max", type=int, default=80, help="most elements to list (in view first)")
    s.add_argument("--text", type=int, default=2500, help="characters of visible text to include")
    st = sub.add_parser("step", help="run one step on the open session")
    st.add_argument("step_json", help="the step as JSON, or @file.json")
    st.add_argument("--record", metavar="NAME", help="append the step to draft NAME if it worked")
    st.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    common.add_run_args(st)
    r = sub.add_parser("run", help="replay a recipe")
    r.add_argument("recipe")
    r.add_argument("--show", action="store_true", help="visible window when no session is open")
    r.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    common.add_run_args(r)
    return p


COMMANDS = {"open": cmd_open, "close": cmd_close, "status": cmd_status, "doctor": cmd_doctor,
            "snapshot": cmd_snapshot, "step": cmd_step, "run": cmd_run}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = COMMANDS[args.cmd](args)
    except Stop as stop:
        result = {"status": "needs_human" if stop.needs_human else "stopped", "reason": stop.reason}
    except Exception as exc:
        result = {"status": "error", "error": f"{type(exc).__name__}: {_short(exc)}"}
    common.emit(result)
    if "status" in result:
        return common.exit_code(result) if result["status"] != "error" else 1
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
