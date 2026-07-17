# Preflight — Data Foundation Initiative — 2026-07-17

**Run per `prompts/1-preflight.md`, adapted.** This initiative has no numbered-volume
spec (`docs/volumes/volume-{N}.md`) — its "spec" is
`docs/volumes/data-foundation-audit-2026-07-17.md`, this morning's audit against the
Principal Quant Engineer mandate. Per the prompt system's own rule ("if the spec
itself contradicts an invariant... flag the contradiction rather than silently
building either side of it"), that is exactly what Section "Verdict" below does for
one of the mandate's asks. No feature code was written in this phase.

**Method — level 3 (live) verification actually performed, not simulated:** SSH access
to `quantstack-vm` is configured in this environment (`gcloud compute ssh`, key present).
All dependency checks below ran directly against the live Postgres instance and the
live `/health` API this session, not inferred from the 2026-07-11/07-13/07-14 audit
docs alone. VM confirmed on commit `740697e` (one docs-only commit behind local
`HEAD`=`f8c3d76`, functionally current). Stack: all 3 containers `Up`/`healthy`,
`GET /health/ready` → `200 OK`.

---

## 1. Dependencies extracted from the mandate

The mandate's asks decompose into two kinds of dependency:

- **Additive** (new columns/tables/collectors that don't change how existing live
  code behaves): feature-row metadata completion, model/dataset registry, new
  corporate-action collectors, news event-store enrichment (explicitly not wired to
  the model).
- **Behavior-changing** (rewrites something already live): pooled/cross-symbol
  training, symbol-normalized features — both mean changing what `ensemble.py`
  actually fits, the one Volume 5 module that started producing real live
  predictions for the first time **today** (DEBT-13, commit `740697e`, ~2 hours old
  on the VM at preflight time).

This split matters for the verdict below — the two categories carry very different
risk given what's currently stable versus fragile.

## 2. Dependency verification (code / tests / live)

| Dependency | Code | Tests | Live (this session) | Verdict |
|---|---|---|---|---|
| `feature_store` schema has room for additive columns | ✅ real table, `uq_feature_store_identity` on `(feature_name, feature_version, symbol, timeframe, ts)` | — | ✅ confirmed via live `\d feature_store` on `quantstack-postgres-1`: columns are exactly `id, created_at, data(jsonb), feature_name, feature_version, symbol, timeframe, ts, value, window_size` — no `collector_version`/`last_updated`/`feature_quality_score`, matches the morning audit exactly | **Verified, additive migration is safe** — no existing reader depends on the row shape staying narrow |
| Feature versioning mechanism is real and won't break on a version bump | ✅ | partial | ✅ `SELECT count(DISTINCT version) FROM feature_versions` = **1**, live, today — DEBT-10 unchanged | **Usable but untested past v1** — first real version bump (if metadata work changes a feature's calculation) is genuinely the first live exercise of this mechanism, not a formality |
| `ModelVersion`/`RetrainingRun` tables exist to receive registry writes | ✅ tables exist | none | ✅ `SELECT count(*) FROM model_versions` = **0**, `retraining_runs` = **0**, live, today | **Confirmed dead, safe to start writing to** — no existing code reads them, so wiring writes can't break anything live |
| `ensemble.py`'s live training path is stable enough to instrument | ✅ | ✅ (existing suite) | ✅ `market_events` has exactly **6** `ensemble_prediction_engine` rows total, system-wide, as of this session — confirms DEBT-13's own count, this is genuinely fresh/thin, not a typo | **Verified but fragile** — 6 total live predictions since this morning's fix is not a track record; see verdict below |
| Watchlist depth for pooled training | ✅ `Settings.watchlist` | — | ✅ `feature_store` has **25 tradable symbols** (22 stocks + NIFTY/BANKNIFTY/SENSEX) at 330k-365k D-bar rows each (≈2yr), the other 28 distinct `symbol` values are macro/sector reference series, not separate instruments; live `ohlcv_candles` 5m depth ≈600-674 bars/symbol over the last 10 days (≈7-8 trading days) | **Depth is real and sufficient to attempt pooling** — 25-wide cross-section at 2yr D / 7-8 day 5m is exactly the kind of width a pooled model would benefit from most |
| Corporate-action collectors for buybacks/credit ratings | ❌ code | — | ✅ live full-text search (`data::text ILIKE '%buyback%' OR '%credit_rating%'`) across all of `market_events` → **0 rows, system-wide** | **Confirmed absent, not just unindexed** — this is a build-from-scratch item, no partial implementation to build on |
| `freshness_seconds` as a usable staleness signal for any new data-quality work | ⚠️ schema field exists | — | ✅ live: **100% null** across 14 of 15 checked event sources (`options_intelligence`, `historical_candles`, `institutional_flow`, `event_calendar`, `market_confidence_engine`, `market_state_report_engine`, `opportunity_detection_engine`, `feature_snapshot_engine`, `candidate_generation_engine`, `breadth`, `event_risk`, `liquidity`, `trend`, +1 more); only `live_market` populates it (96.8%, 50,628/52,309) | **Sharper than the 2026-07-13 audit found** — that audit checked 13 raw collectors; this session's query also covers 15+ derived engine sources and found the same gap is effectively system-wide, not collector-specific |
| Volume 5's remaining live-wiring gap (relevant since registry work touches the same modules) | ✅ code exists for all 16 modules | ✅ (unit-level) | ✅ live counts today: `historical_similarity_engine`=2, `probability_calibration_engine`=2, `model_agreement_engine`=2, `market_context_adjustment_engine`=1, `trade_qualification_engine`=1, `conviction_engine`=1, `signal_priority`/`duplicate_signal`/`opportunity_lifecycle`/`explainability_report`=**0** (don't even appear in the source list) | **DEBT-13 status confirmed current, with a small update**: the 6 named modules above now show 1-2 rows each (manual/ad-hoc verification calls since the last DEBT.md entry, per DEBT-13's own note that ad-hoc calls don't count as "scheduled live") — still zero for the last 4 modules in the chain. Not a blocker for this initiative; noted for whoever next updates DEBT-13. |

## 3. Debt ledger check (`prompts/DEBT.md`, current through 2026-07-17)

None of the Active entries have an expiry condition triggered by *starting* this
initiative. Two are directly relevant to *how* it should be sequenced:

- **DEBT-3 (no outcome evaluator)** — still open, still zero code. This is the one
  that changes the verdict below: there is currently no way to measure whether any
  new training methodology (pooled, symbol-normalized) is actually better than what's
  running today.
- **DEBT-13 (Volume 5 live-wiring, 10 of 16 modules)** — Ensemble Prediction is the
  *only* module in this initiative's blast radius that's actually live. Anything that
  changes what it trains on inherits DEBT-13's own fragility (474-495 samples,
  overlapping 5m/6-bar labels, self-flagged possible autocorrelation artifact in the
  reported accuracy lift).

## 4. Invariants check (`prompts/INVARIANTS.md`, current through 2026-07-17)

- **I-5 (signals are outcome-accountable) — VIOLATED.** Directly load-bearing here:
  building "improved" training on top of a system that can't measure whether training
  improved anything just deepens I-5's hole, the same failure mode INVARIANTS.md
  itself calls out for I-1 ("building signal logic on top of I-1 while it's violated
  just deepens the hole").
- **I-2 (every producer has a consumer) — VIOLATED** (DEBT-2). Not directly blocking,
  but a caution: any new feature-metadata column must get a real consumer promptly,
  not join `IntradayRiskFeatureEngine`'s 6 still-unconsumed features.
- **I-7 (status claims match reality) — VIOLATED.** This initiative's own report
  must not be marked "done" anywhere without a postflight-equivalent verification —
  same discipline the volume lifecycle already enforces elsewhere.
- I-1, I-3, I-4, I-6, I-8, I-9, I-11 — not implicated by this initiative's scope.

## 5. Verdict

**GO — split by risk, per the additive-vs-behavior-changing distinction in Section 1.**

### GO now (additive, no invariant depends on getting this "right" the first time)
1. **Feature-row metadata completion** — add `collector_version`, `last_updated`,
   `feature_quality_score` to `FeatureStoreRow` (or a joined table, if a migration on
   a live, 3M+-row table needs to be non-blocking — worth a sizing check before
   choosing which). Purely additive; live-verified nothing reads the row expecting
   exactly today's 7 columns.
2. **Model/dataset registry wiring** — persist a real `ModelVersion` row (and
   `RetrainingRun` row) on every `ensemble.py` `train()` call, including a data hash
   (hash of the training rows/feature set) and the git commit hash at train time.
   Live-verified both target tables are currently empty and unread — zero regression
   risk, and it directly serves DEBT-13's just-fixed live training path rather than
   competing with it. **This is the first build chunk**, per the earlier
   recommendation to make the registry the connective piece between this initiative
   and DEBT-13, not a parallel effort.
3. **Corporate-action collectors (buybacks, credit ratings)** — net-new, independent
   of everything else live. Lower priority than 1-2 but no blocker.
4. **News event-store enrichment** (embedding hash, decay tiers) — net-new, and the
   mandate's own instruction to keep it disconnected from the ML model is already
   independently satisfied live (confirmed by grep, zero `news_*` in
   `ENSEMBLE_FEATURE_SPECS`). Safe to build in isolation as long as that stays true.

### NO-GO for now — flagging a real spec/invariant contradiction, not softening it
**Pooled/cross-symbol training and cross-sectional symbol normalization should not
start until DEBT-3's outcome evaluator exists**, or until the user explicitly accepts
building it without one (which would need its own new DEBT.md entry with an expiry
condition, per the project's own non-negotiable rule 3). Reasoning, concretely:

- The current per-symbol Ensemble model became genuinely live for the first time
  ~2 hours before this preflight ran. It has 6 total live predictions, ever.
- There is currently no code path that checks whether *any* trained model's
  predictions were right (I-5, DEBT-3). Rewriting the training methodology now would
  mean shipping a second untested training approach on top of a first one that's had
  no chance to prove or disprove itself — the exact "internally consistent chapter,
  no way to verify it against reality before the next chapter builds on it" pattern
  that caused the 2026-07-15 process redesign in the first place.
- This is not a recommendation to abandon pooled training — Section 7 of this
  morning's audit still names it the single highest-leverage fix available, and
  nothing found this session changes that. It's a sequencing call: build the
  registry first (item 2 above) so that when pooled training does ship, its result
  is a registered, comparable model — and build DEBT-3's evaluator so "better" means
  something measurable rather than an opinion.

---

## Report to user

**GO** on: feature-row metadata migration, model/dataset registry wiring
(recommended first build chunk — lowest risk, highest immediate value, directly
extends DEBT-13), corporate-action collectors, news event-store enrichment
(kept disconnected from the model, as instructed).

**NO-GO, pending your call:** pooled/cross-symbol training and cross-sectional
normalization. Recommend building DEBT-3 (outcome evaluator) either just before or
alongside the registry work, then revisiting pooled training with a way to actually
measure whether it helped. If you'd rather proceed with pooled training now anyway,
say so explicitly and I'll log it in `prompts/DEBT.md` with a real expiry condition,
per the project's own rule — not silently.

**Next step if you agree with the sequencing above:** proceed to
`/volume-build`-equivalent for the model/dataset registry chunk (item 2), scoped to:
`ModelVersion`/`RetrainingRun` writes from `ensemble.py`'s `train()`, keyed by a data
hash + git commit hash, with a live-verified check afterward that a real row appears
on the next scheduled `prediction.ensemble_training_sweep` fire — not just on a manual
call.
