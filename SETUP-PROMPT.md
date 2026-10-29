# Install the Jev kit

Copy everything inside the block into Claude Code or Codex.

```text
Install the Jev kit from https://github.com/Mr-heka/jev-kit for my user account.
Do not replace or delete any of my existing skills or instructions.

1. Work out whether you are Claude Code or Codex and use this host's user skill
   folder (Claude Code: ~/.claude/skills, Codex: ~/.codex/skills).
2. Clone the repository into a folder named jev-kit there. If that folder exists,
   look inside first and keep my files; pull updates instead of overwriting.
   If an older jev-helper folder exists, leave it alone and tell me jev-kit replaces it.
3. Read SKILL.md, AGENTS-SNIPPET.md and the scripts in scripts/ before running anything.
4. Check Python 3.9 or newer is available. The core needs no packages.
5. Ask me before installing anything. Then, ideally in a virtual environment:
   - web replay (any computer): pip install playwright, then python -m playwright install chromium
   - screen replay (Mac only, optional): pip install pyobjc-framework-Quartz pyobjc-framework-Vision.
     It also needs Screen Recording and Accessibility permission for the app you run me in;
     tell me where to switch those on, do not change settings yourself.
6. Explain that Jev runs through OpenRouter: I need an account, an API key and a
   small credit balance. A few dollars goes a long way (the kit's own tests measured
   about US$0.00002 to US$0.00003 per decision), but prices can change.
   Ask me to create a key at https://openrouter.ai/settings/keys and store it as
   OPENROUTER_API_KEY using a secure method on this machine, or set JEV_KEY_COMMAND to
   a password-manager command that prints it. Never ask me to paste the key into chat,
   a file, source code or Git.
7. Run: python3 <kit>/scripts/jev.py doctor --online and show me the result.
8. My CLAUDE.md (Claude Code: ~/.claude/CLAUDE.md) or AGENTS.md (Codex: ~/.codex/AGENTS.md)
   is my own instructions file, read at the start of every chat. This step is what
   makes Jev work without me asking. Explain that in one line, show me the block from
   AGENTS-SNIPPET.md with the real kit path filled in, and add it on my yes. Create the
   file if I don't have one. Keep everything already in it. If a Jev block is already
   there, update it instead of adding a second. Read the file back to prove it is in.
9. Ask before the demo, which makes about 12 Jev calls (well under US$0.01):
   python3 <kit>/scripts/web.py run books-best-pick
   Show me the winning book, the run time and the cost.
10. If you have a built-in browser (the Claude app's Browser pane), prove it: open
    https://books.toscrape.com in it, click into the Travel category, scrape the book
    titles and prices yourself, and send them to jev.py judge to pick the best one for
    a first-time traveller. Show me what came back.
11. Tell me in three lines: where it is installed, what it will now do automatically,
    and how to map my first flow ("map this for next time" while you do a browser task).

Do not add hooks, background services or MCP servers. Keep writing, code,
permissions and final decisions outside Jev.
```
