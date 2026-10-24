# Recipes: the shared format

A recipe is one JSON file that replays a flow you worked out once. Both engines
read the same format; only the screen steps and targets differ (see
[web.md](web.md) and [screen.md](screen.md)).

## Where recipes live

Searched in this order, first match wins:

1. `.jev/recipes/` in the current project (commit these with the project)
2. `~/.jev-kit/recipes/` (yours, on this machine; set `JEV_KIT_HOME` to move it)
3. `recipes/examples/` in the kit

Drafts being recorded sit in `~/.jev-kit/recipes/drafts/`. Run logs and stop
screenshots go to `~/.jev-kit/runs/<recipe>/` (or `JEV_KIT_RUNS`).

## Shape

```json
{
  "name": "books-best-pick",
  "engine": "web",
  "about": "Rank one books.toscrape.com category for a reader's goal. Params: category, max_price, want.",
  "read_only": true,
  "params": {"category": "Travel", "max_price": "45", "want": "a light read for a first trip"},
  "start_url": "https://books.toscrape.com/",
  "allow_domains": ["books.toscrape.com"],
  "steps": [
    {"id": "open-category", "do": "click", "target": {"role": "link", "name": "{category}"},
     "expect": {"all": ["{category}"], "timeout": 8}}
  ]
}
```

- `about` is how an agent finds the recipe later. Say what it does and list the params.
- `{name}` anywhere in the steps is filled from `--param name=value`, falling back to `params`.
- `read_only: false` is required before any step can be marked `publish`.
- `allow_domains` (web) or `app` (screen) fences the run in.

## Step options every engine understands

| Option | Meaning |
| --- | --- |
| `id` | Short name, shown in logs and stops |
| `expect` | Free text checks after the action: `all`, `any`, `none` (case-insensitive), `timeout` seconds. Web adds `url_contains`, `title_contains` |
| `judge` | A Jev check after `expect` passes, for what text cannot tell (below) |
| `optional` | Skip quietly when the target is not there (a cookie banner's "Reject all") |
| `publish` | This click sends, buys, books, posts or deletes. Needs `--approved` |
| `safe_because` | A fixed, mapped "Submit" button that only looks risky (a search box). Write the reason. Waives nothing else. Never allowed on Jev-picked targets |
| `timeout` | Seconds to wait for the target or the expected screen |

## Jev steps

```json
{"id": "right-page", "do": "check",
 "judge": {"type": "noul", "instructions": "Is this the search results page for flights, not an ad or a login page?", "threshold": 0.8}}
```

- `judge` with `noul`: passes when the yes-probability is at least `threshold` (default 0.8).
- `judge` with `choice`: give `criteria` (option id to description) and `want` (one id or a list); passes when that option wins with at least `threshold` (default 0.7).
- `pick` (engine step): Jev chooses one of the on-screen candidates for `instructions`, then the engine clicks it or saves it. Returns none when nothing fits; the run stops instead of guessing.
- `rank` (code step): score every item of a saved list on one rubric, sorted in code.
- `choose` (code step): one winner among a short saved list.

Any Jev failure, low confidence or no clear winner stops the run. It never guesses.

## Code steps (no screen, no Jev unless named)

| `do` | What it does |
| --- | --- |
| `filter` | `{"from": "books", "where": [{"field": "price", "lte": "{max_price}"}], "save_as": "affordable"}`. Operators: `lte gte lt gt eq ne contains not_contains regex in`. Numbers are read out of text ("£12.50" is 12.5) |
| `sort` | `{"from": "x", "by": "price", "order": "asc"}` |
| `take` | `{"from": "x", "n": 3}` |
| `rank` | `{"from": "x", "instructions": "...", "levels": ["poor", "ok", "good", "great"], "fields": ["title", "price"], "context": "{want}", "top": 5, "save_as": "ranked"}` |
| `choose` | `{"from": "ranked", "instructions": "...", "label_fields": ["title", "price"], "save_as": "winner"}` |
| `set`, `wait` | Save a constant; pause `secs` |

## Mapping a new flow

1. Open the engine's session (`web.py open`) or the app.
2. Look at the screen with the mapping aid (`web.py snapshot`, `screen.py screen --boxes`).
3. Run one step at a time with `step '<json>' --record <name>`. Only steps that
   worked are recorded. Add `expect` to every step that changes the screen, so a
   replay notices when it lands somewhere else.
4. Add a `judge` only where text alone cannot tell two screens apart.
5. Save: `jev.py save <name> --about "..." --templatize "Travel=category"`.
6. Replay it once from the start. Note the time against doing it by hand.

Good recipes are boring: stable targets (role and name beat position), an
`expect` after every move, and a stop rather than a guess.

## When a replay stops

The result has `status` (`stopped` or `needs_human`), `stopped.step`,
`stopped.reason`, `stopped.screenshot` and `stopped.screen_text`.

- `needs_human`: a login, a code, a CAPTCHA or a publish button without approval.
  Tell the user what is needed. Do not work around it.
- `stopped`: the screen changed. Finish the job by hand, then edit that step
  (new target text, longer timeout, an `optional` banner step) and replay once.

## Approvals

`jev.py approve <id> --recipe <name> --quote "<the user's own words>"` records a
yes for one publishing run of that exact recipe file: editing the recipe after the
yes voids it. It is valid for 48 hours and is claimed (used up) at the first
guarded click it allows, so a second run cannot use it even if both start at once.
Use `--recipe step` for one-off `step` commands. Rerun with `--approved <id>`.
Without it, the engines stop at any button that sends, buys, books, posts or
deletes, whatever the recipe says.

The guard reads the text of what is clicked or pressed: Enter, Space and Delete
(outside a text field) included, and dropdown choices. A button that commits under
a neutral name ("Continue", an arrow icon) needs `"publish": true` on its step;
that guards any click, key or select. `safe_because` only covers the exact button
text it was mapped with, and only ever waives the word "Submit".
