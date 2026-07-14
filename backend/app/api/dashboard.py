"""Ad hoc visual verification dashboards — not part of the product API
surface, just a way to eyeball that a live data source is actually working.
Currently: Sensex option chain (2026-07-13, verifying the new BSE source)."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.collectors.base import CollectionError
from app.collectors.domains.options import OptionsChainSource, OptionsIntelligenceCollector
from app.collectors.registry import CollectorRegistry
from app.collectors.sources.bse_options import BseOptionChainSource
from app.collectors.sources.routing import RoutingOptionsChainSource
from app.core.container import container

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# The production collector only calls fetch_chain() every feature_engine_interval
# (300s by default), so BseOptionChainSource's own 30s anti-hammering cache never
# actually throttles it -- 30s only ever bites *this* dashboard, whose whole point
# is a tight, eyes-on-it live view. Tightened once, on the shared instance, so the
# dashboard and the production collector keep using one BSE session/cookie jar
# rather than each opening their own (friendlier to BSE, not less).
#
# 5.0 was tried and tripped BSE's rate limiting under sustained polling (a
# transient "response is missing data" that self-recovered after ~10s) --
# 10.0 is the tightened-but-not-reckless value that survived the same test.
_DASHBOARD_MIN_FETCH_INTERVAL_SECONDS = 10.0


def _chain_source() -> OptionsChainSource:
    """Reuse the running options_intelligence collector's already-warmed,
    already-cached chain source when available (avoids a fresh BSE cookie
    warm-up + full re-fetch on every dashboard poll); falls back to a new
    RoutingOptionsChainSource if the collector hasn't been discovered yet
    (e.g. registry not wired in this process)."""
    source: OptionsChainSource
    try:
        registry = container.resolve(CollectorRegistry)
        collector = registry.get("options_intelligence")
        source = collector.chain_source if isinstance(collector, OptionsIntelligenceCollector) \
            else RoutingOptionsChainSource()
    except KeyError:
        source = RoutingOptionsChainSource()

    bse = source._bse if isinstance(source, RoutingOptionsChainSource) else source
    if isinstance(bse, BseOptionChainSource):
        bse._min_interval = _DASHBOARD_MIN_FETCH_INTERVAL_SECONDS
    return source


@router.get("/sensex")
async def sensex_dashboard_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "sensex_dashboard.html")


@router.get("/intelligence")
async def intelligence_dashboard_page() -> FileResponse:
    """Volume 4, Chapter 22's 10 named dashboard components, all reading
    real /intelligence/* API data client-side -- no separate aggregator
    endpoint needed, every panel just fetches its own already-existing
    route (plus /intelligence/liquidity/{symbol} and /intelligence/correlation,
    added alongside this dashboard since nothing exposed them before)."""
    return FileResponse(STATIC_DIR / "intelligence_dashboard.html")


@router.get("/sensex/data")
async def sensex_dashboard_data() -> dict:
    source = _chain_source()
    try:
        chain = await source.fetch_chain("SENSEX")
    except CollectionError as exc:
        raise HTTPException(status_code=502, detail=f"chain fetch failed: {exc}") from exc
    strikes = sorted(chain["strikes"], key=lambda row: row["strike"])
    return {
        "symbol": "SENSEX",
        "spot": chain["spot"],
        "expiry": chain["expiry"],
        "greeks_source": chain.get("greeks_source"),
        "strikes": strikes,
    }
