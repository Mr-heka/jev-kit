# The block for CLAUDE.md or AGENTS.md

Add this to your global instructions (`~/.claude/CLAUDE.md` for Claude Code,
`~/.codex/AGENTS.md` for Codex) so your agent routes work to Jev without being
asked. Replace `<KIT>` with the folder the kit is installed in. The setup prompt
does this for you and shows you the block before it edits anything.

```markdown
## Jev (fast small decisions)

The jev-kit skill is installed at <KIT>. Use it without being asked:

- Before any browser or app task, run `python3 <KIT>/scripts/jev.py recipes`.
  If a recipe matches, replay it (`web.py run` or `screen.py run`) instead of
  clicking through by hand. If it stops, finish by hand from its screenshot,
  then fix the step.
- No recipe and the task will come up again: map it while you do it
  (`step --record`), save it with `jev.py save`, and replay it once to prove it.
- If you have a built-in browser (the Claude app's Browser pane), use it to
  open pages, click buttons, fill searches and scrape on your own while I watch.
  Do the clicking yourself; never send me steps to click. I log in there myself
  when a site needs it. Send scraped piles to `jev.py judge`, and turn a flow
  that will repeat into a recipe.
- Many items needing the same judgment (rank results, sort messages, pick the
  best option, check claims against sources): filter exact facts in code, then
  `jev.py judge`. Look at anything flagged before you answer.
- Keep writing, code, planning, maths, exact lookups and the final call yourself.
- Never let the kit buy, send, post, book or delete without my yes for that exact
  action (`jev.py approve`). Never type my passwords, codes or card numbers;
  I log in myself.
```
