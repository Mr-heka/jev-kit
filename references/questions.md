# Question shapes

A request is `{"state": ..., "questions": {...}}`. `state` is the evidence (a
string, list or object; prefer named fields). Each question has a `type`,
`instructions` (the full judgment in plain words; the question's key is never
shown to Jev) and, for most types, `criteria`. Up to 32 questions per request;
they run in parallel and cannot see each other's answers.

## choice: one of a set

```json
"queue": {
  "type": "choice",
  "instructions": "Which support queue should handle this message?",
  "criteria": {
    "billing": "Charges, invoices, refunds or subscription payments.",
    "technical": "Errors, access problems or broken features.",
    "other": "Anything else, or not clear."
  }
}
```

Answer: `{"type": "choice", "choice": "billing", "probabilities": {...}, "confidence": 0.97}`.
Always include an "other" or "none" option when nothing may fit. Jev cannot pick
an option you left out.

## score: ordered levels

```json
"urgency": {
  "type": "score",
  "instructions": "How urgent is this message for the business?",
  "criteria": ["can wait a week", "this week", "today", "right now"]
}
```

Answer: `{"type": "score", "score": 2.3, "probabilities": {"0": 0.02, "1": 0.1, "2": 0.4, "3": 0.48}, "confidence": 0.6}`.
`score` is a probability-weighted position (0 = first level). The most likely
level can differ from the rounded score; keep both.

## noul: is this true?

```json
"supported": {
  "type": "noul",
  "instructions": "Does the passage support the claim that the product ships to New Zealand?"
}
```

Optional `"criteria": {"true": "...", "false": "..."}` defines both sides.
Answer: `{"type": "noul", "noul": 0.91}`, the probability of yes. 0.5 means
"could go either way", not "half true".

## Good questions

- One narrow judgment each. Split "relevant and positive" into two questions.
- Give the evidence in `state`; do not expect Jev to know facts.
- Say who it is for: "for an ordinary user", "for a buyer on a budget".
- Thresholds are yours, set by testing on real examples. Confidence is how
  concentrated the answer is, not proof it is right.
