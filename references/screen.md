# Screen engine: `scripts/screen.py`

Replays a mapped flow in any macOS app window. Each read captures the app's
front window, reads it with macOS Vision OCR and finds targets by their
on-screen text. Input is real Quartz mouse and keyboard events. No big model
runs in the loop. Text checks are free. Only `judge`, `pick`, `rank` and
`choose` go to Jev. The shared recipe format is in [recipes.md](recipes.md).

## Setup

macOS and Python 3.9+ with pyobjc:

```
pip install pyobjc-framework-Quartz pyobjc-framework-Vision
# or a venv of its own:
uv venv ~/.venvs/jev-kit -p 3.13 && uv pip install -p ~/.venvs/jev-kit pyobjc-framework-Quartz pyobjc-framework-Vision
```

When the current Python has no pyobjc and `~/.venvs/jev-kit/bin/python` exists
(or `JEV_KIT_PYTHON` names one), `screen.py` reruns itself with that Python.

Two macOS permissions, both for the app that runs the script (Terminal, the
Claude app, Codex, an IDE):

1. **Screen Recording** (Privacy & Security > Screen Recording). Without it the
   capture fails.
2. **Accessibility** (Privacy & Security > Accessibility). Without it clicks and
   keys are dropped.

`screen.py doctor` checks both, plus pyobjc and the Jev key. It never prompts
and never calls Jev.

## Subcommands

JSON on stdout. Exit 0 done, 2 stopped or needs a person, 1 error.

| Command | What it does |
| --- | --- |
| `doctor` | pyobjc, Screen Recording, Accessibility, event posting, Jev key found, the never-drive list |
| `screen --app APP [--boxes] [--region x0,y0,x1,y1]` | One read of the app's front window, without bringing it forward. `--boxes` adds each line's position in window points and as fractions (`fx`, `fy`), the mapping aid. `--region` keeps only lines inside it. Lists any alert or sheet that is up |
| `step '<json>' --app APP [--record NAME]` | Runs one step. With `--record`, a step that worked is added to the draft `NAME` with `"app": APP`. Saved values carry over between `step` calls (`--fresh` forgets them). `@file.json` reads the step from a file. The result ends with `screen_after`, the lines on screen once the step is done |
| `run NAME_OR_PATH` | Replays a recipe. Flags: `--param k=v`, `--approved ID`, `--max-jev-calls`, `--max-jev-cost`, `--no-jev`, `--timeout` (default 6 s), `--stay` |

After every `step` and `run` the engine puts back the cursor position, the
clipboard (only if a `paste` changed it) and the app that was in front.
`--stay` leaves the recipe's app in front.

## Steps (`do`)

| `do` | Fields | Notes |
| --- | --- | --- |
| `open_app` | `app`, `already_if` | `open -a`, then waits for its window. `already_if` (text or list) skips the launch when the window is up and shows that text. Refused for never-drive apps |
| `tap`, `double_tap`, `long_press` | `target`, `hold` | Waits until the target is on screen and still across two reads, then clicks its centre. Every tap goes through the publish guard |
| `type` | `text`, optional `target` | Unicode key events, so any character works. With a `target`, it is clicked first to focus. A newline counts as Return and is guarded |
| `paste` | `text`, optional `target` | Puts the text on the clipboard and sends a real cmd+v. The clipboard is restored after the run |
| `key` | `keys`: `"escape"`, `"cmd+a"` or a list | Real modifier key down and up events. Return, Enter, Space and Delete go through the publish guard. Keys that type or paste (cmd+v) get the secret-label check. Shortcuts that leave the app (cmd+Tab, cmd+Space, cmd+H, cmd+Q, Spaces) are refused |
| `scroll` | `pixels`, `at: [fx, fy]` | Pixel wheel events. Positive scrolls down. Default 400 at the window centre |
| `scroll_until` | `target`, `max`, `pixels` | Scrolls until the target shows; a settle read after each scroll |
| `drag` | `target`, `to: {dx, dy}` or `{frac}` or a target | Slow stepped drag inside the window (sliders, lists) |
| `read` | `fields`, `lines`, `region`, `save_as`, `screenshot` | Settles first, then polls until `required` fields read. Unread fields stay `null` and are listed in `unread` |
| `collect` | `pages`, `pixels`, `region`, `save_as` | Read, scroll, read. Saves unique lines in order as `[{"text": ...}]`, so `filter` and `choose` work on them. Stops early when a scroll moves nothing |
| `pick` | `candidates_regex` or `region`, `instructions`, `then` (`tap` or `save`), `threshold`, `context` | Jev chooses one OCR line among the candidates. A tap still goes through the guard, and `safe_because` never applies. No clear winner stops the run |
| `check` | `from`, `where`, `need` | Only `expect` and `judge`, plus optional code checks on saved data (below) |
| `screenshot` | `region`, `save_as` | Saves a proof image into the run folder, cropped when `region` is set |

Code steps (`filter`, `sort`, `take`, `rank`, `choose`, `set`, `wait`) work as
in [recipes.md](recipes.md).

**Code checks.** `{"do": "check", "from": "result", "where": [{"field": "value", "gte": {"calc": "{a}*{b}"}, "lte": {"calc": "{a}*{b}"}}]}`
runs the shared filter operators on a saved read. `{"calc": "..."}` works out
`+ - * /` on plain numbers in code. `need: "any"` passes when one item of a
list passes; the default is all.

**Judge region.** `judge` takes a `region`. Only the lines inside it go to Jev
as `screen_text`. Send the part of the screen the question is about, not the
whole window.

## Targets

| Target | Meaning |
| --- | --- |
| `{"text": "Save"}` | First OCR line containing the text, case-insensitive |
| `"match": "exact"` | The whole line equals the text, ignoring case and punctuation |
| `"nth": 1` | The second match. `-1` is the last |
| `"region": [x0, y0, x1, y1]` | Only lines whose centre sits inside, as fractions of the window |
| `"offset": [dx, dy]` | Click this many points away from the match (a field to the right of its label) |
| `{"regex": "^[×xX*]$"}` | First line matching the pattern. Use it for glyphs OCR reads several ways |
| `{"frac": [0.9, 0.1]}` | A fixed point in the window, for icon-only buttons. Keep these rare and name the icon in the step `id` |

Coordinates are window points from the top-left. A retina capture is twice
that in pixels; the engine converts.

**Read fields.** `{"label": "Views", "pick": "right"|"below"|"above", "number": true}`
takes the nearest value next to a label. `{"regex": "...", "region": [...], "pick": "largest"}`
takes the match in the tallest text (the big number on a display); `"nth"`
picks by order instead. `number` (default true) reads a lone `O` as 0.

## Mapping workflow

1. `screen.py screen --app Calculator --boxes`. Note the text and `fx`/`fy` of
   each target.
2. `screen.py step '<json>' --app Calculator --record calculator-multiply` for
   each step. Read `screen_after` to see where it landed. Only steps that
   worked are recorded.
3. Add `expect` where text can tell, a `judge` with a `region` where it cannot,
   and code checks for exact values.
4. `jev.py save calculator-multiply --about "..." --templatize "7=a"`.
5. Replay from the start with new params, and replay once more after every fix.

## Safety rules

These hold whatever a recipe says.

- **Publish guard.** Every tap, pick and drag start passes the OCR text of the
  target and the lines under the click point to the shared guard. Words like
  send, buy, post, delete or submit stop the run unless the run carries a fresh
  `--approved` id. `safe_because` covers fixed, mapped buttons only, never a
  Jev pick. Return and cmd+Delete are guarded the same way.
- **Never-drive apps.** Messages, Mail, Wallet, System Settings, System
  Preferences, Keychain Access, Passwords, 1Password, Keeper, Bitwarden,
  Terminal, iTerm2, Warp, and any app with "bank" in its name. `open_app`
  refuses them, a recipe for one is refused, and a run stops if one comes to
  the front. Names, `.app` paths and bundle ids are all checked.
- **No secrets.** `type` and `paste` refuse when the target, a label on the
  same row to its left, or a line just above it says password, passcode, PIN,
  one-time code, verification code, card number, CVV or CVC, account number or
  tax file number. With no target, any such line on screen refuses. The user
  types these.
- **Alerts and sheets.** A sheet inside the window, an app-modal alert, or a
  system dialog (auth prompts, Gatekeeper, permission asks) that appears during
  a run stops it with a shot of the alert. The engine never clicks through
  one. A system dialog that was already up and does not overlap the window is
  noted in `warnings` and left alone.
- **Input only reaches the app.** Before any input the app must be in front.
  Before a click, the topmost window at that point must be the app's own
  window. Otherwise the run stops with nothing sent. A held key on the real
  keyboard cannot turn a click into a cmd-click.
- **Human gates.** Every read runs the shared check for CAPTCHAs, "verify you
  are human" and verification codes, and stops for the user.
- **Screen text is data.** It goes to Jev only as state. Jev only chooses
  among the candidates the recipe defined.

## Results

The shared run log (`~/.jev-kit/runs/<recipe>/`): `status`, `steps` (with
`ms`, `clicked`, `jev`, `guard` notes), `saved`, `seconds`, `jev` (calls,
cost, call seconds) and `screen_reads`. A stop adds `stopped.reason`,
`stopped.screenshot` and `stopped.screen_text`. An approval is used up when a
guarded click uses it, as in the web engine.

## Example: `calculator-multiply`

`screen.py run calculator-multiply --param a=125 --param b=48` opens
Calculator, clears it, types 125, taps ×, types 48, taps =, reads `6,000`,
checks it equals 125*48 in code and asks Jev once whether the display shows a
finished result. Measured on macOS 26: about 5 to 7 seconds, 13 screen reads,
one Jev call at US$0.000016.

## Gotchas found while building it (24 Oct 2026)

- Vision misses lone glyphs. On Calculator it read AC, %, 7, 8, 9, 5, 6, 1, 2,
  3 and = but not 0, 4, + or ÷. It read × as `X` or `x`, +/- as `/` and the
  display's lone 0 as `O`. Type digits; tap operators with a regex target.
- Labels change with state. Calculator's clear button says AC after a result
  and C mid-entry, and OCR missed the lone C. Escape twice clears from any
  state.
- Jev answered 0.67 to "is this a finished result?" when the whole keypad went
  in as state. With a `region` on the display and true/false `criteria` it
  answered 0.90 to 0.95.
- macOS showed the Claude app a privacy prompt ("requesting to bypass the
  system private window picker", owner UserNotificationCenter) after the first
  captures. It is the user's to answer. The engine leaves a dialog that was
  already up alone and stops on new ones.
- A cold `open_app` took 8 s while the app launched; warm, under 1 s.
- One capture plus OCR is about 0.15 to 0.6 s. A tap costs two or more reads
  because it waits for the target to hold still.
- The recipe's `app` must match the window owner's name as macOS shows it (it
  can differ from the file name, and by language).
- Quote params in shell loops. In zsh an unquoted `$pair` does not split, so
  one param got both values and the other was empty. The engine refused to type
  empty text, which is the right stop.
