# Live Verification Cookbook

The exact commands that found every major bug so far. Run these instead of
reasoning from the code about what "should" be happening. All commands run
from the local machine; SSH goes through gcloud.

**Target:** `quantstack-vm` (GCP, zone `asia-south1-a`, project
`quantstack-prod`), repo at `/opt/quantstack`, app on port 8000
(open to the internet — dashboards reachable at http://34.47.246.73:8000).
Containers: `quantstack-backend-1`, `quantstack-postgres-1`,
`quantstack-redis-1`.

SSH wrapper used throughout (PowerShell users: run these in Git Bash):

```bash
GC="/c/Users/jayra/AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin/gcloud"
echo y | "$GC" compute ssh quantstack-vm --zone=asia-south1-a --command="<CMD>"
```

---

## 1 · Is the stack up and clean?

```bash
sudo docker compose -f /opt/quantstack/docker-compose.yml ps
sudo docker compose -f /opt/quantstack/docker-compose.yml logs backend --since 10m 2>&1 \
  | grep -iE 'error|exception|traceback' | grep -v 'angel_one\|403'
```
(The Angel One `optionGreek` 403 is known background noise — filter it out.)

## 2 · Feature freshness (the check that found the HDFCBANK bug)

Last update time per feature — if `last_update` is midnight during a trading
session, that feature is NOT live, whatever the code claims:

```sql
SELECT feature_name, timeframe,
       max(ts AT TIME ZONE 'Asia/Kolkata') AS last_update, count(*)
FROM feature_store
WHERE symbol = 'HDFCBANK'          -- or any symbol
GROUP BY feature_name, timeframe
ORDER BY last_update DESC;
```

Run via:
```bash
sudo docker exec quantstack-postgres-1 psql -U quantstack -d quantstack -c "<SQL>"
```

Row counts per symbol/timeframe (scale awareness — I-3):
```sql
SELECT symbol, timeframe, count(*) FROM feature_store
GROUP BY symbol, timeframe ORDER BY count(*) DESC LIMIT 15;
```

## 3 · Did the market actually do X? (ground truth)

Raw ticks, minute-bucketed — this is what confirmed the 12:45 collapse:
```sql
SELECT to_char(ts AT TIME ZONE 'Asia/Kolkata','HH24:MI') AS minute,
       min(ltp), max(ltp), avg(ltp)::numeric(10,2)
FROM raw_ticks
WHERE symbol='HDFCBANK' AND ts::date = current_date
  AND ts AT TIME ZONE 'Asia/Kolkata' BETWEEN '<START>' AND '<END>'
GROUP BY minute ORDER BY minute;
```
Note: `ohlcv_candles` timeframe "D" is one row per calendar day — for intraday
truth, `raw_ticks` (15s cadence) is the only source.

## 4 · Query performance at real scale (I-3)

```sql
EXPLAIN ANALYZE <the exact query the code will run>;
```
Reference points from 2026-07-15: unbounded DISTINCT ON over NIFTY/D
(170k rows) = 374ms; with a 14-day ts bound = 8.8ms. If your plan shows a
sort over >10k rows on a hot path, it will hurt at 264 calls/request.

## 5 · Request latency

```bash
for i in 1 2 3 4 5; do
  curl -s -o /dev/null -w "run $i HTTP:%{http_code} time:%{time_total}s\n" \
    http://localhost:8000/prediction/candidates
done
```
Reference: ~2.2s steady state as of 2026-07-15 (was 10.6s on 2026-07-14).
First run after a restart is not representative — the staggered sweeps'
first fire collides with it; wait ~5 min or use §6.

## 6 · Isolate request path from background jobs

```bash
curl -s -X POST http://localhost:8000/health/scheduler/pause    # everything background stops
# ... measure ...
curl -s -X POST http://localhost:8000/health/scheduler/resume   # ALWAYS resume
curl -s http://localhost:8000/health/scheduler/status           # verify
```
Pause survives until resumed or the process restarts. Never leave it paused —
paused = no data collection.

## 7 · Redis online-store population

```bash
sudo docker exec quantstack-redis-1 redis-cli DBSIZE
sudo docker exec quantstack-redis-1 redis-cli KEYS 'qs:features:*'
```
Watch for: no `:D`-timeframe keys ⇒ the intelligence read path is falling
through to Postgres on every call (DEBT-6).

## 8 · Signal history for a symbol

```bash
curl -s 'http://localhost:8000/prediction/candidates/HDFCBANK?limit=500'
```
Or visually: http://34.47.246.73:8000/dashboard/intelligence → set symbol →
"Signal History" panel (direction strip + confidence sparkline; warns when
direction never changes).

## 9 · DB connection pressure during a request

```sql
SELECT count(*), state FROM pg_stat_activity
WHERE datname='quantstack' GROUP BY state;
```
Reference: pool_size=40; live burst ~36-48 checkouts. Sustained overflow past
pool_size = SCRAM churn (perf-audit item 18).

## 10 · Deploy (the full, verified sequence — I-9)

```bash
# local: commit, push (sole author, no co-author trailer)
git push origin main
# VM:
cd /opt/quantstack && sudo git pull origin main
export GIT_COMMIT=$(git rev-parse HEAD)          # model registry provenance (2026-07-17)
sudo -E docker compose up -d --build backend     # -E: preserve GIT_COMMIT for compose; runs alembic on start
until sudo docker inspect --format='{{.State.Health.Status}}' quantstack-backend-1 \
  | grep -q healthy; do sleep 2; done
# then: §1 logs clean, §5 latency, and observe the changed behavior itself.
```
Market hours (09:15–15:30 IST): container recreate = ~10-15s collection gap.
Get explicit user approval before restarting during a session.
