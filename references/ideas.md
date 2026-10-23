# Find where Jev fits in a project

When the user asks where Jev could help, scan the project locally. Send nothing
to Jev during the scan.

Read instruction and workflow files: `README.md`, `AGENTS.md`, `CLAUDE.md`,
skill folders, workflow definitions, scripts and tests. Skip `.git`,
dependencies, build output, caches, `.env*`, credential stores, customer
exports and private datasets.

Look for:

- **Repeated browser or app chores** someone does by hand (checking a dashboard,
  searching a supplier site, pulling a report): candidates for a replay recipe.
- **Repeated judgments with a fixed answer space**: routing between named
  options, ranking a candidate list, scoring against levels, checking a claim
  against supplied evidence: candidates for `judge`, `decide` or `batch`.

Reject exact rules, calculations, lookups, writing, coding, strategy,
permissions and actions.

Write or update `JEV-IDEAS.md` in the project root, keeping anything already
there, with three to five suggestions:

```markdown
# Jev ideas for this project

Updated: YYYY-MM-DD

### 1. Short name
- Evidence: `path/to/file`
- Lane: replay / judge / decide
- Jev's part: the exact judgment (choice, score or noul) and its options or levels
- Minimum state: only the fields needed
- Stays outside Jev: rules, maths, permissions, actions, the final call
- Safe test: a fictional input with an obvious expected answer
- Status: suggested / selected / tested / rejected
```

Read this file first on later Jev requests so the user does not have to explain
the setup again.
