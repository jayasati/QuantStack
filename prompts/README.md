# QuantStack Prompt System

Process redesign adopted 2026-07-15, after the HDFCBANK finding (225 consecutive
"long" signals through a real 1.1% intraday collapse) proved the old
volume-by-volume prompting produced internally-correct chapters with broken
seams between them. This folder is the fix: every volume now runs through a
fixed lifecycle, and no phase may be skipped.

## The lifecycle

```
/volume-preflight N        BEFORE starting volume N.
        |                  Verifies what volumes 1..N-1 ACTUALLY deliver
        |                  (code + tests + LIVE behavior on the VM), not what
        |                  the docs claim. Produces a GO/NO-GO report.
        v
/volume-build N            DURING volume N. The template every build prompt
        |    ^             must follow: contract, backward compat,
        |    |             scale-realistic tests, live verification.
        v    |
/volume-seam-check N       Every 2-3 chapters DURING volume N. Catches
        |                  write-only outputs and stale wiring while the
        |                  context is still fresh, not months later.
        v
/volume-postflight N       AFTER volume N. Proves the CUMULATIVE system
                           (volumes 1..N together) works as intended, live,
                           at production scale. Only a passing postflight may
                           mark a volume "complete" anywhere.
```

## The files

| File | What it is |
|---|---|
| `INVARIANTS.md` | System-wide rules every prompt must respect. Included by reference in every phase. Violations are tracked honestly, not hidden. |
| `DEBT.md` | Ledger of deliberate deferrals. Every entry has an **expiry condition** — the event that makes it stop being acceptable. Checked by preflight and audit. |
| `VERIFY-COOKBOOK.md` | Copy-paste commands for live verification on quantstack-vm: freshness SQL, EXPLAIN ANALYZE, latency loops, scheduler pause/resume. These found every major bug so far — use them, don't guess. |
| `1-preflight.md` | Full instructions for the preflight phase. |
| `2-build.md` | The build-prompt template. |
| `3-seam-check.md` | Mid-volume wiring audit. |
| `4-postflight.md` | Cumulative acceptance + honest status update. |

Slash commands live in `.claude/commands/` and just point at these files with
the volume number as argument.

## Non-negotiable rules

1. **No volume starts without a preflight report**, and preflight blockers are
   fixed (or explicitly accepted by the user in writing) before building.
2. **"Complete" is a postflight verdict**, not a build verdict. The IRR audit
   found roadmap.md claiming completeness the code didn't have — that class of
   drift is what postflight exists to prevent.
3. **Every deferral goes in DEBT.md with an expiry condition.** Docstring
   comments like "accepted v1 redundancy — not a hot path" are banned as the
   only record of a deferral; that exact comment went stale and cost days.
4. **Tests at toy scale prove nothing about this system.** feature_store holds
   170k+ rows per symbol/timeframe live. Any new or changed query must be
   verified at that scale (cookbook has the how).
5. **Live verification on the VM is part of done.** The DISTINCT ON regression
   passed all 1185 tests and broke in production within hours. Deploy, measure,
   observe real behavior — every time.
6. Commits follow the existing per-prompt rhythm, sole author, no co-author
   trailers.

## Volume spec naming

Specs live in `docs/volumes/volume-{N}.md`, with dots as dashes:
volume 5.5 → `volume-5-5.md`. Reports produced by these phases also go in
`docs/volumes/` (same place as the existing audits):
`preflight-vol{N}-{YYYY-MM-DD}.md`, `postflight-vol{N}-{YYYY-MM-DD}.md`.
