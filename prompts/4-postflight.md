# Phase 4 · Postflight — run AFTER volume N's build chunks are done

This phase decides whether volume N is **complete**. Completion is a verdict
about the *cumulative system* — volumes 1..N working together, live, at
production scale — not about volume N's code existing. Only a passing
postflight may update roadmap/status claims (I-7).

Read: `prompts/INVARIANTS.md`, `prompts/DEBT.md`, the volume-N preflight
report, and `prompts/VERIFY-COOKBOOK.md`.

## Steps

1. **Spec coverage, honestly.** Table of every chapter/prompt in
   `docs/volumes/volume-{N}.md`: implemented / partial / deliberately
   deferred (with DEBT entry) / missing. The 2026-07-11 IRR found "complete"
   claims at 2/18 chapters — this table is what prevents that.

2. **Cumulative behavior test.** Define and run 2-4 end-to-end assertions
   about what the system 1..N should now do *as a whole*, phrased as
   observable behavior, and verify each live on the VM. Guide by layer:
   - through Vol 2: raw data flows — ticks/collector rows current within
     each collector's cadence during market hours (cookbook §2, §3).
   - through Vol 3: features derived from that data are fresh at their
     declared cadence — including intraday timeframes, not just "D".
   - through Vol 4: intelligence outputs *change* when their inputs change —
     pick a real recent market move from raw_ticks and show the relevant
     component's persisted history responding to it. A signal that never
     changes is the known failure mode (HDFCBANK, 2026-07-15).
   - through Vol 5+: candidates/predictions respond to the same move, and
     every emitted signal is outcome-evaluable (I-5).
   Use real market history for these — synthetic fixtures prove plumbing,
   not behavior.

3. **Full regression + performance.** Complete suite green
   (`python -m pytest app/tests -q`). Deploy final state (cookbook §10).
   Latency within reference (§5 — ~2.2s for /prediction/candidates as of
   2026-07-15; justify or fix anything materially worse). Logs clean (§1).

4. **Invariants + debt reconciliation.** Re-verify the status line of every
   invariant touched by this volume — with live evidence, and downgrade
   honestly. Update DEBT.md: resolved entries moved to Resolved, new
   deferrals added, expired entries surfaced to the user.

5. **Report + status update.** Write
   `docs/volumes/postflight-vol{N}-{YYYY-MM-DD}.md`: coverage table,
   behavior-test evidence, perf numbers, invariant/debt deltas, and the
   verdict — **COMPLETE / COMPLETE-WITH-DEBT (list) / NOT COMPLETE (gaps)**.
   Only then update roadmap.md to match, linking the report.

## Hard rules

- A behavior test that can't be run (no market data captured for the needed
  window, etc.) is reported as NOT RUN — never assumed passed.
- If the verdict is NOT COMPLETE, say so plainly and list exactly what's
  between here and complete. That report becomes the next work list; an
  honest NOT COMPLETE is a good outcome of this phase, not a failure of it.
- No new feature work inside postflight. Findings go to the report and, if
  accepted as deferrals, to DEBT.md.
