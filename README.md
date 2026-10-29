# Jev kit

Stop paying a big model to make small decisions.

Jev kit is a skill for Claude Code and Codex. Your agent hands the small,
repeated judgments to [Jev](https://docs.typesafe.ai), a typed-judgment model
from TypeSafe served through OpenRouter. Jev picks one option, scores on ordered
levels, or gives a yes-probability, in well under a second. Your agent keeps the
thinking, the writing and the final call.

## What it does

**1. Map once, replay fast.** The first time your agent does a browser or app
chore (search a site, click through a dashboard, read a report screen), it
records the steps into a small recipe. Next time a runner replays the recipe at
machine speed, with no big model in the loop. Plain text checks are free. Only
the fuzzy calls go to Jev: is this the right page, which result is the real one,
which option fits. If anything looks different, the run stops with a screenshot
and your agent takes over from exactly there.

- `web` engine: any website or web app, through Playwright. Tested on macOS; Linux
  should work; Windows is untested.
- `screen` engine: any Mac app window, through on-screen text reading and clicks.

**2. Judge a big pile.** Search results, research passages, listings, messages,
options. Code filters the hard facts (price, dates, stock), Jev scores every item
on one rubric in parallel, code sorts them, and Jev picks a winner from the top
few. Near ties and low confidence are flagged for your agent to check.

**3. One quick decision.** Typed questions from inside your agent's own work.

**4. The built-in browser.** In the Claude desktop app your agent opens pages,
clicks buttons, fills searches and scrapes in the app's own browser pane while you
watch, then hands the scraped pile to the judge. You log in there yourself when a
site needs it. Flows that repeat become recipes.

**5. Routing.** A short block for your `CLAUDE.md` or `AGENTS.md` (your own
instructions file, read at the start of every chat) tells your agent to check for
a recipe before any browser task, use the built-in browser on its own, record new
flows, and send piles of items to the judge, without you asking each time. The
setup prompt adds it for you after you say yes.

## Measured on our machine (24 Oct 2026)

| Job | Time | Jev calls | Jev cost |
| --- | --- | --- | --- |
| Judge 10 search results and pick the best answer | 2.2 s | 11 | US$0.0002 |
| Web replay: open a shop category, collect the books, filter by price, rank for a reader, pick one | 4.6 s | 11 | US$0.0002 |
| Mac app replay: multiply in Calculator, read the result, check it | 5 to 10 s | 1 | US$0.00002 |

Mapping each demo flow the first time took the agent about five minutes. After
that, replays run with no big model in the loop and need no remapping when the
parameters change.

These are single-machine measurements on demo tasks, not a promise for your
tasks. Prices are set by OpenRouter and can change.

## Install

Copy the prompt in [SETUP-PROMPT.md](SETUP-PROMPT.md) into Claude Code or Codex.
Your agent installs the kit, asks before installing packages, helps you set up
an OpenRouter key without pasting it into chat, and runs a demo.

You need Python 3.9 or newer, an OpenRouter account with a small credit balance,
and for web replay `pip install playwright`. The screen engine is Mac only.

## Safety built in

- The kit stops at any button that buys, sends, books, posts or deletes, and at
  Enter or Space on one. It needs your yes for that recipe, recorded once per run.
  Buttons that commit under a neutral name ("Continue") are marked when mapping.
- It never types passwords, one-time codes or card numbers, and stops at CAPTCHAs.
  You log in yourself once in the kit's own browser profile.
- Page and screen text is treated as data. Jev can only choose among options the
  recipe or rubric defined.
- Every run has a call cap and a cost cap.
- Only the few fields a judgment needs are sent to Jev.

## Files

| Path | What |
| --- | --- |
| `SKILL.md` | What your agent reads: routing, the three lanes, the rules |
| `AGENTS-SNIPPET.md` | The block for `CLAUDE.md` or `AGENTS.md` |
| `scripts/jev.py` | decide, batch, judge, recipes, save, approve, doctor |
| `scripts/web.py` | Web engine: open, snapshot, step, run |
| `scripts/screen.py` | Mac screen engine: screen, step, run |
| `references/` | Recipe format, both engines, the built-in browser, the judge, question shapes |
| `recipes/examples/` | Demo recipes |
| `tests/` | Offline tests: `python3 -m unittest discover tests` |

Made by Selr AI. MIT licence. Jev and TypeSafe are TypeSafe AI's; this kit is
not affiliated with TypeSafe or OpenRouter.
