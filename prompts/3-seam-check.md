# Phase 3 · Seam check — run every 2-3 build chunks DURING volume N

A short adversarial pass over what was just built, while the context is
fresh. Your stance: assume the newest code is lying about being wired up,
and try to prove it. Every major defect in this project's history was a seam
defect that each side's own tests couldn't see.

Scope: everything added or changed since the last seam check (or since
preflight, for the first one). Read `prompts/INVARIANTS.md` and
`prompts/DEBT.md` first.

## Checks

1. **Producer→consumer sweep (I-2).** For every new output (feature, engine
   result, event, table write): grep for who reads it. "The next chapter
   will use it" only counts if a DEBT entry says so with an expiry. For every
   new consumer: verify its input actually exists live, at the cadence it
   assumes (cookbook §2) — not just in the test fixtures.

2. **Live production check.** Deploy if not already deployed, then confirm
   the new producers are writing on the VM right now: fresh rows in
   feature_store / market_events with current timestamps, new API fields
   present in real responses, dashboard panels populating. An engine that
   runs in tests and stalls live has happened before
   (IntradayRiskFeatureEngine, DEBT-2).

3. **Freshness end to end (I-1).** For any signal path the new code
   participates in: walk it backward to raw data and record the actual
   worst-case staleness at each hop. If a "live" signal turns out to bottom
   out in a midnight-computed feature, that's a blocker for the chunk, not a
   note.

4. **Scale spot-check (I-3).** Any query added since the last check: confirm
   its plan was measured at production scale. If the build phase skipped it,
   do it now (cookbook §4).

5. **Debt & invariants drift.** Did the new code silently resolve, worsen, or
   trigger the expiry of any DEBT entry? Did any invariant's status change?
   Update both files — they only work if they track reality.

## Output

A short report **in the conversation** (no file needed unless findings are
substantial — then `docs/volumes/seam-check-vol{N}-{date}.md`):
each finding as *what's unwired/stale/unmeasured → evidence → severity*.
Blockers stop the build phase until fixed; the fix belongs to whichever
chunk created it.

Do not pad. "All seams verified, evidence: …" in five lines is a perfect
outcome. Findings invented to look thorough are worse than none — they burn
trust in the process.
