# Volume 1 Postflight — Foundation & System Architecture (2026-07-15)

**Scope:** Verify Volume 1's 20 sections and explicit Acceptance Criteria (§20)
against current code and live quantstack-vm behavior — not against prior
audit notes, which this check found to be stale on two items (§8, §13).
Method: repo inspection, live SSH/SQL checks (VERIFY-COOKBOOK.md), GitHub
Actions API.

**Verdict: COMPLETE-WITH-DEBT.** The architecture is sound and does what §20
requires, with two real gaps: CI infrastructure exists but is completely
non-functional, and Volume 1's own <2s signal-generation target is now
borderline-violated by later volumes' work.

---

## Acceptance criteria (§20), checked one by one

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | `docker compose up` starts the app | ✅ PASS | Used all session (2026-07-14/15 deploys); `docker-compose.yml` present, 3 services |
| 2 | FastAPI starts, health-checks work | ✅ PASS | `GET /health/live` → `{"status":"ok",...}`; `/health/ready` → postgres+redis both "ok" (checked live, just now) |
| 3 | Postgres, Redis, config load correctly | ✅ PASS | Same live check; `Settings` class loads via env/`.env`/`default.yaml` priority (config.py) |
| 4 | Alembic initializes complete base schema | ✅ PASS | 34 tables live (`\dt` count); migrations 0001-0005 chain cleanly, 0005 applied live this session with no error |
| 5 | APScheduler starts, executes a sample job | ✅ PASS | `system.heartbeat` job runs every 60s (scheduler/service.py); ~30 real jobs currently scheduled and firing (confirmed via new `/health/scheduler/status` endpoint added 2026-07-15) |
| 6 | Logging, DI, broker abstraction in place | ✅ PASS | `JsonFormatter` → structured JSON (core/logging.py); `Container.register/resolve` used throughout; `BrokerInterface → AngelOneAdapter` via DI (container.py) |
| 7 | **CI runs successfully with basic tests** | ❌ **FAIL** | See "CI" section below — infrastructure exists, 0 of the last 5 runs succeeded |
| 8 | Structure stable enough for new collectors/engines without restructuring | ✅ PASS | 6 volumes' worth of collectors/engines added since without a structural rewrite; `intelligence/`, `static/` added cleanly alongside the original layout |

**6 of 8 pass cleanly. 1 fails outright (CI). 1 (#5) passes but see the
performance-target note below — the scheduler works, but the request path it
now competes with is a later finding, not a Volume 1 defect.**

---

## Section-by-section notes (only where there's something to say)

- **§2 Core Principles** — Principle 2 ("deterministic before AI, LLM only
  explains") can't be checked yet: no LLM synthesis layer exists (later
  volume). Principle 3 (measurability) holds structurally — every engine
  exposes score/confidence; `collector_health` table has live quality scores
  per collector (13 rows checked live, e.g. `live_market` 99.9,
  `news_intelligence` 23.3 — the low ones are a Volume 2 collector-health
  matter, out of scope here, but worth your attention separately).
- **§9 Broker Abstraction** — `BrokerInterface` → `AngelOneAdapter`, resolved
  via DI, wrapped in `CircuitBreakerRegistry.get("broker.angel_one")`.
- **§10 Event Bus** — matches the spec's own stated design *exactly*:
  observability/audit spine, not the scoring call path. This was verified
  concretely this session (perf-audit-2026-07-14 finding 17): the bus has
  zero production subscribers today, and every publish call site was
  audited/fixed to check `has_subscribers()` before building a payload —
  which only makes sense, and is only cheap, because the spec never intended
  the bus to be load-bearing for scoring. Working as intended.
- **§13 Error Handling** — `Network → Retry → Backoff → Circuit Breaker →
  Fallback → Alert`: `CircuitBreaker`/`CircuitBreakerRegistry` exist in
  `core/circuit_breaker.py` and are wired into the broker adapter. **This
  corrects stale memory** from the 2026-07-09 audit ("no circuit breaker") —
  it exists now.
- **§16 Performance Targets** — tick processing <100ms: not independently
  measured this pass. **Signal generation <2s: currently ~2.2s** live
  steady-state (measured 2026-07-15, post all perf fixes) — a ~10% miss
  against Volume 1's own target. Not a Volume 1 defect (the target was
  reasonable; later volumes' scope grew past what it budgeted for) but
  worth tracking; logged as DEBT-7 below. Collector uptime 99.9%: `live_market`
  quality score matches; not a true uptime metric, no dedicated measurement
  exists.
- **§18 CI/CD** — see below.

## CI/CD — the real finding

`.github/workflows/backend-tests.yml` exists, is well-formed (Postgres +
Redis services, applies Alembic migrations, runs the full suite with
coverage, uploads the report) — genuinely built to address the 2026-07-11
IRR's "no CI" finding (commit `edcfabf`).

**It is completely non-functional right now.** Checked via GitHub's public
API (repo is public, so this needed no auth):

```
2026-07-15T10:55:36Z  failure  main  fb1b6ef (this session's prompt-system commit)
2026-07-15T10:36:15Z  failure  main
2026-07-15T09:11:12Z  failure  main
2026-07-15T09:00:21Z  failure  main
2026-07-15T08:56:59Z  failure  main
```

Every one of the last 5 runs — spanning this entire session's commits —
failed. Critically, the job's own timestamps show it **started and
completed within the same second, with zero steps recorded**:

```
created_at:   2026-07-15T10:55:37Z
started_at:   2026-07-15T10:55:37Z
completed_at: 2026-07-15T10:55:38Z
steps: []
```

That is not a test failure — a real test run (Postgres/Redis boot, pip
install, migrations, ~1185 tests) takes minutes, not one second. This is
GitHub never assigning a runner to the job at all. The repo is public
(confirmed via API), which normally means free unlimited Actions minutes, so
the most likely explanations are a repo/org **Actions permission setting**
("Disable Actions" or "Allow select actions only" blocking this workflow) or
an account-level restriction — neither diagnosable via the anonymous API,
and neither fixable from code. **This needs a look at
github.com/jayasati/QuantStack → Settings → Actions → General.**

Net effect: every commit pushed today, including all of this session's perf
fixes and the prompt-system itself, went to `main` with **zero CI signal**,
silently. Acceptance criterion 7 has looked satisfied (the file exists) for
however long this has been broken, which is exactly the "looks done, isn't"
failure mode this whole postflight process exists to catch.

---

## Invariant/Debt reconciliation

**Corrections to prior records** (both were stale, not wrong when written —
exactly why I-7 exists):
- Circuit breaker: previously logged as missing (2026-07-09 audit) — **now
  exists and is wired.** No longer a gap.
- CI: previously logged as "doesn't exist" — **more precisely, exists and is
  broken.** Different problem, different fix. DEBT-5 updated below.

**DEBT.md updates:**
- **DEBT-5 revised**: "No CI/CD" → "CI workflow exists but 0/5 recent runs
  execute (job fails before any step runs, likely a GitHub Actions
  permission setting) — every push today shipped with zero test signal."
  Expiry condition unchanged: before the next volume's build phase begins.
  **This is now the single highest-priority item in the ledger** — it's not
  that CI is absent, it's that the team has been operating under a false
  sense of safety net.
- **New DEBT-7**: Signal generation at ~2.2s vs. Volume 1's <2s target.
  Expiry: before Volume 1's target is cited as met anywhere, or when request
  latency is next worked (natural pairing with DEBT-6, Redis coverage —
  populating the Redis fast path is the next lever and would likely close
  most of this gap).

No invariants (I-1 through I-10) are specific to Volume 1's own scope; none
change status from this check.

---

## Bottom line

Volume 1's actual architecture — DI, broker abstraction, circuit breaker,
structured logging, event bus as audit spine not call path, migrations,
scheduler — is solid and matches its own spec. The gap isn't the
architecture; it's that the safety net meant to catch regressions in
everything built on top of it has been silently broken. Fix the GitHub
Actions setting before starting the next volume — it's a five-minute repo
setting, not a code change, and it's currently worth more than any single
feature.
