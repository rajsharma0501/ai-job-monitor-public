# Writing these docs

Where a new fact belongs, and how to keep these files true.
`tools/docs_lint.py` enforces the mechanical half; the rest is judgment.

## The one rule

**If a code edit can falsify it, it lives beside the code. If it counts things,
it isn't written down at all.**

Facts don't decay — *copies* of facts decay, at a rate set by how far the copy
sits from the original. So a fact is written in the repo that changes with it,
and a number that will be wrong next week is replaced by a pointer to where the
number actually lives.

## This repo has a public remote

That adds a second rule: **describe the mechanism, not the target list.**

Company names, filter criteria, score distributions, and match counts belong in
`config.json` and the data files, all of which are gitignored on purpose.
Copying any of them into a doc publishes them.

What is safe to document: how an extractor talks to a platform, how scoring
combines its inputs, how deduplication works, which boards are permanently
broken and why. None of that reveals what you are looking for.

## What each file is for

| File | Holds | Answers |
|---|---|---|
| `AGENTS.md` | the map | "what is this, how do I run it, where do I look?" |
| `docs/architecture.md` | extractors, scoring, dedup, data flow | "how does it work?" |
| `docs/operations.md` | commands, state files, adding a company | "how do I run and maintain it?" |
| `docs/decisions.md` | rationale, and what is known broken | "why is it like this? did we try X?" |
| `README.md` | features and quick start, for a stranger | "what is this project?" |
| `docs/writing-docs.md` | this file | "where does this new fact go?" |

One fact has one home. A module described in two files will eventually be
described differently in each — the linter fails a build when that happens.

## Reading

Start at `AGENTS.md`. Its routing table names the file for each kind of
question. Follow the table rather than grepping the tree.

Read `docs/decisions.md` before proposing an approach — it exists so a rejected
idea is not re-proposed, and it records the boards that are permanently broken
so nobody spends an afternoon fixing one.

## Writing

**`AGENTS.md`** — capped at 80 lines, because it loads on every session. It
holds pointers and rules, never detail. No counts, no dates, no run logs.

**`docs/*.md`** — capped at 400 lines. Past that an agent skims instead of
reads. Split by *question*, not by size. Every file under `docs/` must be named
in the map, or nothing routes to it.

**`docs/decisions.md`** — append-only, dated, one decision per section. Say
what was chosen, what over, and what the evidence was. It is the only file
allowed to state counts, because it is a historical record.

Write the decision when you make it. A decision recorded a week later has
already lost the alternative you rejected.

## Updating

**Ship the doc edit in the same commit as the code edit.** That is the entire
maintenance mechanism — not a cleanup pass later.

- **Added or renamed a module?** Update `docs/architecture.md`'s table.
- **Changed a scoring weight or threshold?** Change nothing in the docs. They
  point at `job_monitor.py` rather than copying the value.
- **Found a board permanently broken?** Record it in `docs/decisions.md` so the
  next person does not try to fix it.
- **Reversed a decision?** Add a new entry. Never edit the old one.

## What never goes in

- **Counts.** Company totals, job counts, score distributions. One command
  away, wrong within days, and on a public repo they leak your search.
- **Company names.** They live in the gitignored config.
- **Run diaries.** Session logs and "as of" measurements. Metrics belong in
  `logs/metrics.jsonl`; reasoning belongs in `docs/decisions.md`.
- **Anything derivable in under a minute** from reading one file.

## Enforcement

```bash
python tools/docs_lint.py
```

Runs pre-commit and blocks, and again at session start where it reports drift
left by earlier work. Install the commit hook on a fresh clone with
`python tools/install_hooks.py`.

It checks that referenced paths exist, the module inventory matches both ways,
files stay within budget, every doc is routed from the map, no live doc states
a count, and no module is described twice.

What it cannot check is meaning. If scoring changes and `architecture.md` still
describes the old weighting, every check passes and the doc is wrong. The only
defense is that the doc sits close enough to the code that whoever edits one
sees the other.
