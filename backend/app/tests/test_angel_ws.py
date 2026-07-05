"""SmartAPI WebSocket feed tests: binary parsing, buffer, token mapping."""

import struct
import time

from app.market.angel_ws import (
    MODE_LTP,
    MODE_QUOTE,
    AngelWebSocketFeed,
    parse_packet,
    ws_token,
)


def build_ltp_packet(token: str, ltp_paise: int, mode: int = MODE_LTP) -> bytes:
    packet = bytearray(51)
    packet[0] = mode
    packet[1] = 1  # nse_cm
    packet[2 : 2 + len(token)] = token.encode()
    struct.pack_into("<qqq", packet, 27, 7, 1_700_000_000_000, ltp_paise)
    return bytes(packet)


def build_quote_packet(token: str, ltp_paise: int) -> bytes:
    packet = bytearray(123)
    packet[:51] = build_ltp_packet(token, ltp_paise, mode=MODE_QUOTE)
    struct.pack_into("<qqq", packet, 51, 25, 2_427_000, 123_456)  # ltq, atp, volume
    struct.pack_into("<dd", packet, 75, 1000.0, 900.0)  # buy/sell qty
    struct.pack_into("<qqqq", packet, 91, 2_400_000, 2_450_000, 2_390_000, 2_410_000)
    return bytes(packet)


def test_ws_token_strips_index_prefix() -> None:
    assert ws_token("99926000") == "26000"
    assert ws_token("99926009") == "26009"
    assert ws_token("2885") == "2885"  # equities unchanged


def test_parse_ltp_packet() -> None:
    tick = parse_packet(build_ltp_packet("26000", 2_427_085))
    assert tick is not None
    assert tick["token"] == "26000"
    assert tick["ltp"] == 24270.85
    assert tick["mode"] == MODE_LTP
    assert "close" not in tick


def test_parse_quote_packet() -> None:
    tick = parse_packet(build_quote_packet("26009", 5_793_850))
    assert tick is not None
    assert tick["ltp"] == 57938.5
    assert tick["volume"] == 123_456
    assert tick["avg_traded_price"] == 24270.0
    assert tick["open"] == 24000.0
    assert tick["close"] == 24100.0
    assert tick["total_buy_quantity"] == 1000.0


def test_parse_rejects_short_packets() -> None:
    assert parse_packet(b"\x01\x02short") is None


def make_feed() -> AngelWebSocketFeed:
    return AngelWebSocketFeed(
        jwt_token="jwt", api_key="key", client_code="C1", feed_token="feed"
    )


def test_feed_buffer_latest_and_staleness() -> None:
    feed = make_feed()
    feed._on_message(build_quote_packet("26000", 2_427_085))
    assert feed.metrics.packets == 1

    tick = feed.latest("99926000")  # REST token maps onto ws token
    assert tick is not None and tick["ltp"] == 24270.85

    # Stale ticks are not served
    feed._ticks["26000"]["received_at"] = time.time() - 120
    assert feed.latest("99926000", max_age_seconds=30) is None


def test_feed_counts_dropped_packets() -> None:
    feed = make_feed()
    feed._on_message(b"garbage")
    assert feed.metrics.dropped_packets == 1


def test_subscribe_message_shape() -> None:
    import json

    feed = make_feed()
    feed.subscribe("NSE", "99926000")
    feed.subscribe("NSE", "2885")
    message = json.loads(feed._subscribe_message())
    assert message["action"] == 1
    assert message["params"]["mode"] == MODE_QUOTE
    tokens = message["params"]["tokenList"][0]["tokens"]
    assert set(tokens) == {"26000", "2885"}
