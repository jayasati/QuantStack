"""Routes option-chain requests to the correct exchange source per
instrument: BSE-listed indices (Sensex, ...) to BseOptionChainSource,
everything else to NseOptionChainSource. NSE has zero BSE data and BSE has
zero NSE data (different exchanges), so no single source can serve both.
"""

from typing import Any

from app.collectors.domains.options import OptionsChainSource
from app.collectors.sources.bse_options import BSE_INSTRUMENTS, BseOptionChainSource
from app.collectors.sources.nse_options import NseOptionChainSource


class RoutingOptionsChainSource(OptionsChainSource):
    def __init__(
        self,
        nse: OptionsChainSource | None = None,
        bse: OptionsChainSource | None = None,
    ) -> None:
        self._nse = nse or NseOptionChainSource()
        self._bse = bse or BseOptionChainSource()

    async def fetch_chain(self, instrument: str) -> dict[str, Any]:
        if instrument.upper() in BSE_INSTRUMENTS:
            return await self._bse.fetch_chain(instrument)
        return await self._nse.fetch_chain(instrument)

    async def close(self) -> None:
        for source in (self._nse, self._bse):
            closer = getattr(source, "close", None)
            if closer is not None:
                await closer()
