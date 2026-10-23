# Web engine: `scripts/web.py`

Replays a mapped browser flow with Playwright. No big model runs in the loop.
Text checks are free. Only fuzzy calls (`judge`, `pick`, `rank`, `choose`) go to Jev.
The shared recipe format is in [recipes.md](recipes.md).

Needs Python 3.9+, `pip install playwright` (1.51 or newer) and
`python3 -m playwright install chromium`. Check with `web.py doctor`.

## Subcommands

JSON on stdout. Exit 0 done, 2 stopped or needs a person, 1 error.

| Command | What it does |
| --- | --- |
| `open [--show] [--url URL]` | Starts the kit's own Chromium as a background process, on its own profile in `~/.jev-kit/browser/profile`. Headless by default. `--show` opens a visible window. Running `open` in the other mode restarts it on the same profile |
| `status` | Is a session open, its port, pid and open pages |
| `close` | Shuts the session down cleanly, so cookies are saved |
| `snapshot [--max N] [--text N]` | The mapping aid: url, title, headings, visible controls (role, name, a ready-to-paste `target`) and a clip of the page text. Controls in view come first |
| `step '<json>' [--record NAME]` | Runs one step on the open session. With `--record`, a step that worked is added to the draft `NAME`. Code steps (`filter`, `rank`...) run without a browser. Saved values carry over between `step` calls. `@file.json` reads the step from a file |
| `run NAME_OR_PATH` | Replays a recipe. Uses the open session if there is one, else starts a private headless browser on the same profile and closes it after. Flags: `--param k=v`, `--approved ID`, `--max-jev-calls`, `--max-jev-cost`, `--no-jev`, `--timeout`, `--show` |
| `doctor` | Playwright version, Chromium installed, session state, Jev key found. No inference |

## Steps (`do`)

| `do` | Fields | Notes |
| --- | --- | --- |
| `goto` | `url` | Refused before loading if the host is outside `allow_domains` |
| `click` | `target` | Goes through the publish guard |
| `fill` | `target`, `text` | Refuses password, one-time code, card, CVV, expiry, bank, account and tax file fields, and any text that looks like a card number |
| `press` | `key`, optional `target` | `Enter` inside a form is guarded on the form's submit buttons |
| `select` | `target`, `option` | Matches the option label first, then its value |
| `hover` | `target` | |
| `check` | (none) | Only runs `expect` and `judge` |
| `scroll` | `pixels` | Positive scrolls down. Default 700 |
| `scroll_until` | `target`, `max` | Scrolls until the target exists, for lazy lists |
| `read` | `fields: {name: target or {target, attr}}`, `text`, `required`, `save_as` | Missing fields come back `null` and are listed in `unread` |
| `collect` | `rows`, `fields`, `max`, `next`, `pages`, `save_as` | A list of dicts, one per row. Fields are looked up inside each row |
| `pick` | `from`, `instructions`, `then` (`click` or `save`), `attr`, `threshold`, `save_as` | Jev chooses one of the elements `from` matches. Nothing clear means a stop, never a guess |
| `back` | (none) | Browser back |
| `screenshot` | `label`, `full_page` | Saved next to the run log |

Code steps (`wait`, `set`, `filter`, `sort`, `take`, `rank`, `choose`) work as in recipes.md.

Step options: `expect`, `judge`, `timeout`, `optional`, `publish`, `safe_because`, `nth`.
`expect` adds `url_contains` and `title_contains` (a string or a list, all must match).
The default wait is 8 s. An `optional` step waits 2.5 s, then is skipped.

### Field specs for `collect`

`{"css": "h3 a", "attr": "title"}` reads an attribute. Without `attr` it reads the text.
`href` and `src` come back as full URLs. A spec with no target key reads the row itself.
Post-processing runs in code: `regex` (keeps group 1), `map` (word to value), `number`.

```json
{"rating": {"css": "p.star-rating", "attr": "class", "regex": "star-rating (\\w+)",
            "map": {"One": "1/5", "Five": "5/5"}}}
```

When every field is css, a page is read in one browser call (about 20 ms for 20 rows).

## Targets

| Target | Matches |
| --- | --- |
| `{"role": "button", "name": "Search", "exact": true}` | Accessible role and name. Use this first |
| `{"label": "Email"}` | A form field by its label |
| `{"placeholder": "Search books"}` | A field by its placeholder |
| `{"text": "Add a comment"}` | Visible text |
| `{"testid": "submit-btn"}` | `data-testid` |
| `{"alt": "..."}`, `{"title": "..."}` | Image alt text, title attribute |
| `{"css": "article.product_pod"}` | Last resort |

Any target can add `nth` (0 first, -1 last), `has_text` and `within` (another target).
Without `exact`, names match case-insensitive substrings. Only visible elements count.
A target that matches more than one element stops the step. Add `nth` or a tighter name.
`snapshot` prints `nth` for you when names repeat.

## Session and login

- The kit never drives the user's own Chrome and never types credentials.
- To use a site that needs a login: `web.py open --show --url <login page>`. The user
  logs in by hand in that window. Then `web.py close`. Later runs use the same profile, so they stay logged in.
- The profile lives in `~/.jev-kit/browser/profile` (or `$JEV_KIT_HOME/browser/profile`).
- `run` with no session open starts a private browser on that same profile. Two
  browsers cannot share a profile, so `run` uses the open session when there is one.
- A click that opens a new tab moves the run to that tab.

## Safety

- Every click goes through the publish guard on the element's real accessible name and
  the recipe's name for it. Buy, pay, book, send, post, delete and similar stop the run
  unless `--approved <id>` carries a fresh recorded yes. The approval is used up after the run.
- `pick` targets never get the `safe_because` pass.
- `allow_domains` is checked before each `goto` and after every step. A missing list
  falls back to the host of `start_url`.
- A CAPTCHA, a "verify you are human" page or a code prompt stops the run as `needs_human`.
- Page text is data. It only reaches Jev as state, and Jev only chooses among candidates the recipe defined.

## Mapping workflow

1. `web.py open`, then `web.py step '{"do":"goto","url":"https://..."}' --record my-flow`.
2. `web.py snapshot`. Copy a `target` from the elements list.
3. One `step ... --record my-flow` per move. Give each step an `id` and an `expect`.
   The first recorded step sets `start_url` and `allow_domains`.
4. For lists: a `collect`, then code steps (`filter`, `rank`, `take`, `choose`).
   To save Jev calls while mapping, add the Jev steps to the draft by hand
   (`~/.jev-kit/recipes/drafts/my-flow.json`) and test them once in the full replay.
5. `jev.py save my-flow --about "..." --templatize "Travel=category"`.
   `--templatize` swaps literal text in string values only. Check numbers in `where`
   by hand, for example `"lte": "{max_price}"`.
6. `web.py close`, then `web.py run my-flow` once from the start.

Mapping `books-best-pick` took about 5 minutes of agent time (under a minute of
commands): 4 recorded steps, 2 snapshots, one hand edit for the Jev steps, and one
wording fix after the first live runs.

## Gotchas found while building it (24 Oct 2026)

- **Expected text that is on both pages passes too early.** "Travel" is in the
  home page's sidebar and on the category page. Add `title_contains` or `url_contains`
  so the check waits for the new page.
- **`choose` stopped on all three first live runs.** With "Which single book best suits
  the goal?", Jev put about half its weight on "none of these" (c1 0.49 to 0.53, none
  0.44 to 0.48, threshold 0.6). Saying "these are already shortlisted, choose the closest
  even if none is perfect" moved it to 0.82. Shortlist choices need that framing.
- `rank` flags `low_confidence` on the top item even when it wins every run. Read the
  flags. They are advice, not a stop.
- Chrome for Testing asks for the macOS Keychain unless it gets `--use-mock-keychain`
  and `--password-store=basic`. The engine passes both.
- Playwright's driver prints Node deprecation warnings on attach. The engine sets `NODE_NO_WARNINGS=1`.
- The books recipe takes about 2.4 s inside the run, plus about 0.6 s to start its own
  browser when no session is open. About 1.6 s of the 2.4 s is Jev (8 calls, 4 at a time).
- The guard reads button and link text. A button with a neutral name ("Continue",
  "Next", an icon) that commits something is invisible to it: mark that step `publish: true` when mapping.
