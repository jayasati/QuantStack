# Design Audit — Remaining Feature Store Gaps — 2026-07-17

**Scope:** how the six items below can be implemented in the existing system —
symbol-normalized features, point-in-time feature storage, historical feature
regeneration, data versioning, feature versioning, and what "true feature
store" actually requires beyond what exists. **Design only, per the request —
no code written.** Every claim below is sourced to a direct read of the current
code this session (`backend/app/features/store.py`, `normalize.py`, `base.py`,
`replay.py`, `registry.py`, `schema.py`, `api/features.py`,
`prediction/ensemble.py`), not the earlier audits' summaries — two of this
session's earlier findings turned out to be more optimistic than stated once
read directly, corrected below.

**Correction to the 2026-07-17 status report, found while producing this
design:** "historical feature regeneration — not touched" undersold what
exists. `BaseFeatureEngine.run(symbol, timeframe, full=True)` — "bypasses the
incremental watermarks and re-upserts the whole history" — is real, wired, and
already exposed via `POST /features/run/{symbol}?full=true`
(`api/features.py:384`). "Feature versioning — mechanism already existed but
unexercised" is also more complete than implied: `FeatureDefinition.version`
(`features/schema.py:29`) flows automatically into every `FeatureValue` write
(`base.py:95`, `build_values_at`) and into `feature_versions` via
`FeatureRegistry.sync_to_db()` (`registry.py:83`) — bumping one string field
in an engine's `_definitions()` is the entire mechanism; nothing new needs to
be built for the write side. What's actually missing for each is narrower
than "build it from scratch" — detailed per-item below.

---

## 1. Point-in-time feature storage — already solid, one real integrity gap

**Current state:** `FeatureStoreRow` is uniquely keyed on `(feature_name,
feature_version, symbol, timeframe, ts)` (`database/tables.py:62-67`),
queried via `HistoricalReplayEngine.replay()`/`replay_matrix()` which strictly
enforce `ts <= as_of` (`replay.py:80`, `124`) and `EnsemblePredictionEngine`'s
own `_as_of_value()` (`ensemble.py:237-245`, bisect-based, same guarantee).
This is genuinely correct point-in-time infrastructure — no design change
needed for the read path.

**The one real gap, found this session:** `FeatureStore._write_offline()`
(`store.py:142-150`) upserts on the identity key with
`on_conflict_do_update(set_={"value": stmt.excluded.value})`. If the *same*
`(feature_name, feature_version, symbol, timeframe, ts)` is written twice with
a different value — which happens today via `run(full=True)`'s re-upsert path
— the old value is silently overwritten in place, with no record that it ever
held a different number. This is the correct behavior when the underlying
*raw data* changed (late-arriving/corrected OHLCV, the documented use case for
`full=True`) but silently wrong when the *calculation logic* changed without a
version bump: a model trained yesterday against `data_hash` X, if literally
re-run against `feature_store` today, could see different numbers under the
same version tag, breaking exactly the reproducibility guarantee point-in-time
storage exists for.

**Design (no code):** don't change the upsert mechanism — it's correct for
late-arriving data. Add a *policy*, enforced at review time initially and
mechanically later: any change to a feature engine's `_compute()` logic must
bump `FeatureDefinition.version` in the same commit. A cheap mechanical check
already implementable later: a script diffing `_compute()`'s AST/source hash
per feature against the last-registered version's stored hash (a new column
on `FeatureVersion`, `source_hash`) and failing CI (once CI exists again) or a
pre-push hook if they diverge without a version bump. Not needed to ship this
item — the discipline is the fix, the automation is a nice-to-have.

---

## 2. Historical feature regeneration — mechanism exists; two real gaps remain

**What already works** (confirmed live-code, not assumed): `run(symbol,
timeframe, full=True)` re-derives the full series from `_load_candles()`'s
window and re-upserts every value (`base.py:116-119`), reachable per-symbol
via `POST /features/run/{symbol}?full=true`. `HistoricalReplayEngine` can
reconstruct any past feature state from what's already stored. Together these
cover "recompute today's calculation logic against history I already have"
and "read back what a model would have seen at any past moment."

**Gap A — regeneration is lookback-window-scoped, not date-range-scoped.**
`_load_candles()` (`base.py:334-363`) always pulls the trailing
`feature_candle_lookback` bars ending *now* — there's no way to say
"regenerate March 2026 for HDFCBANK" specifically; a `full=True` call always
recomputes from whatever the current lookback window covers backward. For a
platform now holding ~2 years of daily OHLCV (per the 2026-07-17 audit), this
means genuine historical regeneration (e.g., after fixing a bug in
`price.py`'s momentum calculation) can only reach as far back as the lookback
window allows in one call, not the full history.

**Design:** add an optional `(start, end)` window parameter to `run()`
alongside `full`, threaded into a new `_load_candles(symbol, timeframe,
start=None, end=None)` overload that replaces the `.limit(lookback)` bound
with an explicit `ts BETWEEN` when a range is given. `_compute()` itself
already operates on whatever candle sequence it's handed — no change needed
there. The API surface becomes `POST /features/run/{symbol}?start=...&end=...`
alongside the existing `full=true` (which stays the "whole available history"
shorthand). This is additive (new optional params, existing calls unaffected)
and touches one file's data-loading boundary, not the calculation layer.

**Gap B — no watchlist-wide/batch orchestration.** Regeneration today is
one symbol, one timeframe, one engine (`container.resolve(SpecificEngine)`
per the API route) per call — there's no "regenerate this feature across all
25 watchlist symbols" job. Given `BaseFeatureEngine.run_all()` already
iterates `settings.watchlist × settings.feature_timeframes` sequentially
(`base.py:203-220`), a batch regeneration job is a thin wrapper: a
`run_all(full=True, start=..., end=...)` passthrough, exposed as a POST
endpoint or an admin CLI script, explicitly *not* auto-scheduled (a
watchlist-wide date-ranged full recompute is exactly the kind of CPU cost the
2026-07-14 perf audit found this box already strains under during market
hours — this should be a deliberate, after-hours-gated, manually-triggered
operation, matching the existing `after_hours_only` convention used for
`feature_selection_sweep`/`ensemble_training_sweep`).

---

## 3. Feature versioning — mechanism complete; what's missing is a real bump and a pinning policy

**What already works:** `FeatureDefinition.version` flows through
`build_values_at()` into every stored row automatically (`base.py:95`), and
`FeatureRegistry.sync_to_db()` upserts the corresponding `feature_versions`
row on registry sync (`registry.py:83-132`, confirmed via grep). `FeatureStore
.history()` already accepts an explicit `version=` filter
(`store.py:276`,`310-311`) — a consumer *can* pin today, the plumbing exists.
`GET /{feature_name}/versions` (`api/features.py:138-164`) already exposes
version history. **None of this needs new code.**

**Gap A — nothing has ever exercised a version bump.** DEBT-10's finding
(1,075/1,075 features stuck at "v1") isn't a missing mechanism, it's zero
usage. The first real exercise should be deliberate and low-risk: pick one
feature with a known, already-documented v1 limitation (e.g.,
`volume_rvol_20` or any feature whose docstring already flags a known
approximation), change its calculation, bump `version="v2"` in its
`FeatureDefinition`, and verify live that (a) old `v1` rows are untouched
(different unique-key value, no upsert collision), (b) new writes land as
`v2`, (c) `latest()` picks up `v2` going forward, (d) `GET
/{feature_name}/versions` shows both. This is a verification exercise, not a
build — the risk is entirely in *not having tried it*, per this project's own
"tests at toy scale prove nothing, verify live" discipline.

**Gap B — `latest()` doesn't filter by version, so a mid-migration window is
ambiguous.** `store.py:246-260`'s `DISTINCT ON (feature_name) ... ORDER BY
feature_name, ts DESC` picks the single newest row *regardless of version* —
correct for "give me whatever's freshest" (live serving), silently wrong for
"give me what this pinned model expects" if v1 and v2 rows interleave in time
during a rollout (e.g., a partial redeploy where only some symbols have
started producing v2 rows). **Design:** add an optional `version:
str | None` parameter to `latest()`, defaulting to `None` (today's
behavior, unchanged — backward compatible), that adds a `WHERE
feature_version = :version` filter when provided. `EnsemblePredictionEngine`
and any future pooled-training path should pin explicitly once this exists,
rather than silently riding whatever's newest — directly relevant to Section
5's dataset-registry design below, since a registered dataset needs to name
*which* feature version it was built from, not just "the current one."

**Gap C — no consumer-side pinning policy exists yet.**
`EnsemblePredictionEngine._fetch_feature_series()` (`ensemble.py:747-756`)
calls `self.store.history(feature_name, symbol=key_symbol,
timeframe=timeframe, limit=FEATURE_HISTORY_LIMIT)` with no `version=` —
it silently rides whatever's stored. Once Gap B's parameter exists, the design
is: `ModelVersion.data` (already extended this session with a `feature_names`
list, per the model-registry chunk) should also record `feature_versions:
dict[str, str]` — the exact version pinned per feature at training time —
so a later regeneration or version bump doesn't retroactively change what a
registered model is understood to have been trained on.

---

## 4. Data versioning — a dataset registry, distinct from the per-run hash already built

**What already exists (this session):** `EnsemblePredictionEngine.train()`
computes `dataset_hash()` (SHA-256 over the assembled training rows) and
stores it on each `ModelVersion` row — real, but scoped to *one training run's
already-assembled rows*, not a named, independently queryable "dataset."
There's no way today to ask "what dataset versions exist" or "what's in
dataset X" without re-deriving from a specific model's row.

**Design — a `DatasetVersion` registry table**, additive, same narrow-columns
+ `data` JSONB pattern as `ModelVersion`:
- Identity: `name` (e.g. `"ensemble-5m-30minhold"`), `version` (integer or
  timestamp-based), `created_at` (inherited).
- Provenance: `symbol_scope` (JSON list — which watchlist symbols/indices are
  included; relevant once pooled training exists), `timeframe`,
  `date_range_start`/`date_range_end`, `feature_versions` (JSON dict, per
  Section 3 Gap C), `row_count`, `data_hash` (same algorithm as
  `dataset_hash()`, generalized to a named dataset rather than one training
  call's rows).
- Purpose: a `ModelVersion` row references a `dataset_version_id` (a second
  FK, following the precedent this session's chunk already set) instead of —
  or in addition to — its own inline `data_hash`. This is what makes "what
  exactly did model X train on" answerable without re-deriving it, and is the
  natural place pooled training's "which symbols were pooled" question lives
  once that work is unblocked (per the 2026-07-17 preflight's NO-GO — this
  table is a prerequisite for pooled training's own registry needs, not
  something that needs pooled training to exist first).

**Sequencing note:** build this *after* Section 3's feature-version pinning
(Gap B/C) exists — a `DatasetVersion` that can't name which feature version
each column came from is only half the provenance story the mandate asked
for.

---

## 5. Symbol-normalized features — genuinely new work, no existing mechanism to extend

**Current state:** `normalize.py`'s six methods (`rolling_zscore`,
`rolling_minmax`, `rolling_robust`, `rolling_percentile_rank`, `log_transform`,
`rolling_winsorize`) are all **within-symbol, across-time** — a rolling window
of one symbol's own history (`normalize.py:36-84` etc.). There is no
across-symbol, single-timestamp normalization anywhere in the codebase
(confirmed by reading the full file — every function takes one `Series`,
never a cross-section of symbols at one `ts`). This is the one item on this
list that is genuinely new engineering, not "exercise an existing mechanism."

**Design — a new, thin pass, not a new feature engine per se:**
1. **Shape of the computation:** for a given `(feature_name, feature_version,
   timeframe, ts)`, pull that feature's value across every watchlist symbol
   at that timestamp, then compute each symbol's z-score/rank *within that
   cross-section*. This is structurally different from every existing engine
   (which computes one symbol's own series independently) — it needs all 25
   symbols' latest values for the same feature at (approximately) the same
   time, which today would mean 25 separate `FeatureStore.latest()`/
   `history()` calls unless a new bulk read method is added.
2. **New read method, additive:** `FeatureStore.cross_section(feature_name,
   timeframe, ts, symbols) -> dict[symbol, value]` — one query filtering
   `feature_name`+`timeframe`+`ts <=` (point-in-time, same discipline as
   `replay.py`) across the whole symbol list, `DISTINCT ON (symbol)` ordered
   by `ts DESC` per symbol (same pattern `latest()` already uses per
   feature_name, just pivoted to per-symbol). This is the one genuinely new
   query shape needed — bounded by symbol count (25, small) so no I-3
   scale concern.
3. **New pass, scheduled after the per-symbol feature engines complete each
   cycle** (not a 17th independent engine with its own `run_all()` — it
   structurally depends on that cycle's per-symbol outputs already being
   written): for each feature the mandate wants normalized (a configurable
   list — likely the same D-timeframe core set `ensemble.py` already
   consumes, `CORE_FEATURE_NAMES`), compute the cross-sectional z-score/rank
   across all watchlist symbols at the latest common timestamp, write results
   back as new `FeatureValue` rows named e.g. `{feature_name}_xs_z` (a
   distinct feature_name, not a version bump of the original — this is a
   *different feature*, the raw value's cross-sectional position, not a
   recalculation of the raw value itself), `feature_version` starting at
   `"v1"` like everything else.
4. **Where it lives / how it's triggered:** a new module,
   `app/features/cross_sectional.py`, following `BaseFeatureEngine`'s
   conventions where they fit (DI-constructed, `session_factory`/`cache`
   optional, I-8 graceful degradation) but with its own `run()` shaped around
   "one pass over the whole watchlist for one feature," not
   "`_compute()` given one symbol's candles." Scheduled in `main.py` *after*
   the existing `feature_engines` list's jobs on the same tick (ordering
   matters — this consumes their output), same interval family as the other
   `feature_engine_interval`-driven jobs.
5. **Consumer:** these `_xs_z` features become new entries in
   `ENSEMBLE_FEATURE_SPECS` (INSTRUMENT-mode, like the raw features they're
   derived from) once pooled training is unblocked — this is precisely the
   input pooled training needs to be comparable across symbols in the first
   place (a raw `price_momentum_20` isn't on the same scale for a ₹40 stock
   vs. a ₹4,000 stock; its cross-sectional rank is). This item and pooled
   training are more tightly coupled than the mandate's flat list implies:
   building this without pooled training gives symbol-normalized features
   with no consumer yet (an I-2 concern, resolvable the same way DEBT-2 was —
   a named DEBT.md entry with pooled training as the expiry condition, not a
   blocker to building it first).

**Risk/cost:** bounded and low — 25 symbols × ~16 core features × one
extra read/write pass per cycle is small next to the existing per-symbol
engine cost the perf audit already characterized. The real cost is design
correctness (point-in-time alignment across symbols whose feature-engine runs
may complete at slightly different wall-clock moments within a cycle — worth
an explicit "as of the same feature_engine cycle's nominal timestamp, not
wall-clock now" rule, mirroring how `replay.py` treats `ts <= as_of` as
authoritative over real-time arrival order).

---

## 6. "True Feature Store" — what combination of the above actually closes this

Restating the 2026-07-17 audit's finding precisely: the *storage* half
(point-in-time correctness, versioning infrastructure, quality/drift
scoring) was already close to institutional-grade before this session. What
was missing splits into two categories now that this design pass has looked
closer:

**Already-built, needs exercising/wiring (cheap, do first):**
- Feature versioning (Section 3) — bump one version once, verify live, add
  the `version=` param to `latest()`, add pinning to the model registry's
  `data`.
- Historical regeneration (Section 2) — add date-range scoping and a
  batch/watchlist-wide wrapper around what `run(full=True)` already does.

**Genuinely new (Section 4/5, real engineering):**
- `DatasetVersion` registry table.
- Cross-sectional (symbol-normalized) feature pass — the one item with no
  existing code to extend.

**Still not addressed by anything in this design** (carried over from the
2026-07-17 audit, unchanged): the literal per-row metadata gap
(`collector_version`, `last_updated`, `feature_quality_score` as columns on
`FeatureStoreRow` itself, not a separate table) — that was marked GO in
preflight alongside the model registry and simply wasn't built this session.
It's additive and low-risk by the same reasoning as the registry migration
(empty-of-those-columns today, nothing reads a fixed 7-column shape), and
should be sequenced *before* the cross-sectional pass if the intent is for
`_xs_z` features to carry full metadata from day one rather than needing a
second migration to add it retroactively.

**Suggested build order**, given dependencies made explicit above:
1. Feature-row metadata migration (independent, already GO'd, not yet built).
2. Feature-version pinning (`latest(version=...)`, `data_hash`/
   `feature_versions` on `ModelVersion.data`) — cheap, unblocks #4.
3. Date-range-scoped + batch regeneration — independent, cheap.
4. `DatasetVersion` registry table — depends on #2 for real provenance.
5. Cross-sectional normalization pass — depends on #1 (should carry full
   metadata) and is the real prerequisite for pooled training, which itself
   stays NO-GO per the 2026-07-17 preflight until DEBT-3's outcome evaluator
   exists.

None of this was implemented in this pass — this is the audit you asked for.
