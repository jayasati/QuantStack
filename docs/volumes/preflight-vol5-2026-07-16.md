# Volume 5 Preflight — Opportunity Detection, Prediction & Conviction Engine (2026-07-16)

**Scope note — a correction to prior memory:** This preflight started from a
stale assumption (this session's own memory claimed "Prompts 5.6-5.16 not
yet started"). Direct inspection found **all 16 Volume 5 modules already
exist in code** (`app/prediction/`: opportunity, candidates, snapshot,
multi_horizon, labeling, ensemble, calibration, agreement,
historical_similarity, market_context, conviction, qualification, priority,
duplicate, lifecycle, explainability — plus `alpha_research.py`, Volume
5.5's engine). Same retroactive-check situation as Volumes 2-4. That memory
claim is corrected at the end of this report.

**Method:** repo inspection, live SQL/API checks per
`prompts/VERIFY-COOKBOOK.md`, run 2026-07-16 ~23:15-23:45 IST (market
closed).

**Verdict: GO for the narrow question this phase asks** (does Volumes 1-4's
foundation support what Volume 5 needs) **— but with the most significant
live-verification finding of any preflight this session.** Volume 5's own
spec calls itself "the decision-making core of QuantStack — the 'brain' of
the platform." Twelve of its sixteen modules — everything from Ensemble
Prediction through Explainability, the entire ML/conviction/qualification
chain — have **zero live execution evidence**, ever. Not stale, not
degraded: literally never run, scheduled, or persisted a single row.

---

## Dependency table

| Dependency | Code | Tests | Live | Verdict |
|---|---|---|---|---|
| Market State Report (`report_as_of()`, Vol 4 Ch.19) | ✅ inherited | ✅ inherited | ✅ 13,547+ persisted reports, confirmed fresh via today's Volume 4 postflight | PASS (inherited) |
| Market Structure / Institutional Flow / Liquidity / Volatility / Relative Strength intelligence (Vol 4, `opportunity.py`'s real inputs) | ✅ inherited | ✅ inherited | ✅ confirmed live via today's Volume 4 postflight (12/12 composite components reporting) — **and** now carry the DEBT-1 intraday overlay, so `OpportunityDetectionEngine` inherits that freshness improvement automatically | PASS (inherited, improved) |
| Historical Analog Engine (Vol 4 Ch.14) → `ConvictionEngine`'s `historical_analog` evidence | ✅ `HistoricalAnalogEngine` | ✅ inherited | ✅ **directly verified this pass**: manually invoked `GET /prediction/conviction/MARUTI?direction=long` returned `historical_analog: {score: 45.0, confidence: 0.7525}` — a real, non-default confidence, confirming this evidence source genuinely pulls live analog data | PASS |
| Regime Transition (Vol 4 Ch.12) → `opportunity.py`'s `regime_transition[trend].alert` trigger | ✅ inherited | ✅ inherited | ✅ live in a real candidate's `supporting_features`: `MARUTI`'s current top candidate cites `"regime_transition[trend].alert": 0.608` | PASS |
| Events Intelligence (Vol 4 Ch.11) → event-driven trigger | ✅ inherited | ✅ inherited | ✅ live: same MARUTI candidate cites `"events.score": 78.8` | PASS |

**No blockers on the dependency question.** Every Volume 4 output Volume 5
actually consumes (in the parts of Volume 5 that run) is live, fresh, and —
via this session's DEBT-1 work — fresher than it was yesterday.

## Major finding: Volume 5's decision pipeline beyond candidate generation has never executed live

`main.py` schedules exactly **one** Volume 5 job:
`prediction.candidate_generation` (`CandidateGenerationEngine.generate()`,
which internally covers Prompts 5.1-5.3: Opportunity Detection, Candidate
Generation, Feature Snapshot). Confirmed genuinely live and active:
`opportunity.detected` 8,306 rows, `trade_candidate.generated` 6,934,
`feature_snapshot.captured` 6,934 — all growing continuously.

**Nothing else is scheduled.** Checked every remaining module's own
persisted-event type directly against `market_events` (each engine's
`EVENT_TYPE` constant, grepped from source, not guessed):

| Prompt | Module | Event type | Live rows |
|---|---|---|---|
| 5.4 Multi-Horizon Prediction | `multi_horizon.py` | `multi_horizon_prediction.probability` | **0** |
| 5.6 Ensemble Prediction | `ensemble.py` | `ensemble_prediction.result` | **0** |
| 5.7 Probability Calibration | `calibration.py` | `probability_calibration.result` | **0** |
| 5.8 Model Agreement | `agreement.py` | `model_agreement.result` | **0** |
| 5.9 Historical Similarity | `historical_similarity.py` | `historical_similarity.result` | **0** |
| 5.10 Market Context Adjustment | `market_context.py` | `market_context_adjustment.result` | **0** |
| 5.11 Conviction Engine | `conviction.py` | `conviction.result` | **0** |
| 5.12 Trade Qualification | `qualification.py` | `trade_qualification.result` | **0** |
| 5.13 Signal Priority | `priority.py` | `signal_priority.result` | **0** |
| 5.14 Duplicate Signal | `duplicate.py` | `duplicate_signal.result` | **0** |
| 5.15 Opportunity Lifecycle | `lifecycle.py` | `opportunity_lifecycle.transition` | **0** |
| 5.16 Explainability Report | `explainability.py` | `explainability.report` | **0** |

(5.5 Triple Barrier Labeling is the one deliberate exception — by design an
on-demand training-data generator, not a live signal, per its own build
history. Not counted as a gap.)

Cross-checked against the live database's actual top event types (16 event
types, 250k+ total rows) — none of the 11 above appear even once.

**Confirmed this is "never scheduled," not "broken":** manually invoked
`GET /prediction/conviction/MARUTI?direction=long` (a real, currently-active
candidate) against the live API. It returned HTTP 200 with a real,
structured result — the code runs. But the substance reveals the same
gap from a different angle: of conviction's 9 weighted evidence sources,
**3 — `calibrated_probability` (35% weight, the single largest), `market_context`
(20%), `model_agreement` (5%) — all read `score: 50.0, confidence: 0.0`**,
the textbook I-8 graceful-degradation default for "no data available." That
is 60% of Conviction's total weight built on nothing, because Ensemble has
never trained a model (no model file/registry exists — `ensemble.py` has no
persistence mechanism beyond the never-written `ensemble_prediction.result`
event) and Calibration/Market-Context/Agreement have nothing to compute
from. The remaining 6 sources (institutional_flow, market_structure,
liquidity, sector_strength, options_positioning, historical_analog) are
genuinely real, since they read directly from Volume 4's already-live
components.

**Qualification does correctly reject this candidate** (`"qualified": false`),
but one of its three rejection reasons — `"Model disagreement high:
agreement 0% (low)"` — is worded as if models actively disagreed, when the
real cause is that no model prediction exists to agree or disagree on. The
system is not currently shipping bad trades on this account (missing-data
defaults tend to trigger rejection, not false confidence), but the
*explanation* is misleading about why, which matters directly for Ch.16's
own "Explainability" requirement (Reason Codes should be honest about what
happened).

## DEBT ledger check

No existing Active entry names this — it is a genuinely new finding, not a
recurrence of a tracked item. DEBT-3 (no outcome evaluator) is related but
narrower and downstream: DEBT-3 is about not knowing whether a *sent*
signal's prediction came true; this finding is that the system has never
generated a real signal through the *full* decision pipeline to begin with.
DEBT-2's expiry condition already anticipated "Volume 5's conviction/
qualification engines" as the natural home for the remaining unconsumed
intraday features — this preflight confirms those engines exist and run
(when manually invoked) but aren't live-scheduled yet, so that opportunity
is real but not yet actionable in a scheduled context.

Logged as new debt below (DEBT-13).

## Invariants check

- **I-5 (signals outcome-accountable), VIOLATED** — unaffected by this
  finding directly (DEBT-3 already covers it), but this preflight's
  discovery sharpens it: there is currently no evidence any signal has ever
  been evaluated end-to-end with genuine conviction, so I-5's gap is not
  just "we don't measure outcomes" but "we don't yet have a real outcome to
  measure" for anything beyond raw candidate detection.
- **I-1, no new violation from this finding** — the intelligence inputs
  Conviction/Qualification *do* successfully consume (institutional_flow,
  market_structure, etc.) already carry this session's DEBT-1 intraday
  fix, so whenever this pipeline does get scheduled, it inherits that
  freshness for free.
- **I-7 (status claims match reality), relevant** — `roadmap.md` currently
  marks Volume 5 "✅" with no linked postflight. Given this finding, that
  claim significantly overstates operational reality for 12 of 16 modules.
  Not fixed here (roadmap only updates after a passing postflight, matching
  I-7's own rule) but flagged plainly rather than left implicit.
- **I-8 (graceful degradation), HELD** — confirmed directly: the
  conviction call with zero real ML input didn't crash, didn't fabricate,
  degraded to honest neutral defaults exactly as designed.

---

## GO verdict (for the narrow dependency question) — with a loud caveat

Every Volume 4 output that Volume 5's *currently-running* code path
(opportunity detection → candidate generation → feature snapshot) actually
consumes is live, fresh, and improved by this session's DEBT-1 work. That
narrow question is a clean GO — nothing blocks continuing to build on top
of Volumes 1-4.

But the honest, larger picture: Volume 5 is comprehensively *coded* —
sixteen real modules, extensive tests (including a full-pipeline
integration test in `test_market_scenarios.py`), a complete API surface
matching Ch.17's spec exactly — and almost entirely *inert*. The
"decision-making core" this volume exists to build has decided nothing,
ever, outside of a handful of manual API calls made during this
investigation. **Recommended next build chunk, ahead of any new Volume 5.5+
or Volume 6 scope:** get the existing pipeline running live end-to-end at
least once — train a real ensemble model, schedule calibration/agreement/
market-context/conviction/qualification/priority into the candidate-
generation path (or a closely-following scheduled job), and verify with
live evidence (not another manual curl) that a real signal can travel from
detection to a qualified-or-rejected verdict with genuine evidence behind
every weighted input. This is DEBT-9's and DEBT-1's exact shape of problem,
at a much larger scale — and matches this project's own established lesson
that scheduling a long-dormant capability for the first time is exactly
when real scale/wiring bugs surface.

## Memory correction

This session's stored memory ("Volume 5... Prompt 5.1 done, 5.2-5.16 not
yet started") is stale and wrong — corrected via the auto-memory system
following this report. All 16 modules exist in code; the accurate status
is "built, mostly never executed live," not "not yet built."
