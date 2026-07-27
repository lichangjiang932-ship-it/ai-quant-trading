from src.data.realtime.realtime_data import RealtimeData


def quote(price):
    return {
        "price": price,
        "pre_close": 10.0,
        "change_pct": (price / 10.0 - 1) * 100,
        "volume": 1000,
        "amount": 100_000,
    }


def test_quotes_fall_back_per_symbol_and_record_provenance(monkeypatch):
    realtime = RealtimeData()
    monkeypatch.setattr(
        realtime,
        "get_realtime_quote_pytdx",
        lambda symbols: {
            "sh600000": quote(10.1),
            "sz000001": {"price": 0},
        },
    )
    monkeypatch.setattr(
        realtime,
        "get_realtime_quote_tencent",
        lambda symbols: {"sz000001": quote(11.2)},
    )

    result = realtime.get_quotes(
        ["sh600000", "sz000001"],
        sources=["pytdx", "tencent"],
    )

    assert set(result) == {"sh600000", "sz000001"}
    assert result["sh600000"]["data_source"] == "pytdx"
    assert result["sz000001"]["data_source"] == "tencent"
    assert result["sh600000"]["received_at"]
    health = realtime.get_source_health()
    assert health["pytdx"]["resolved"] == 1
    assert health["tencent"]["resolved"] == 1


def test_quotes_drop_invalid_prices(monkeypatch):
    realtime = RealtimeData()
    monkeypatch.setattr(
        realtime,
        "get_realtime_quote_sina",
        lambda symbols: {"sh600000": {"price": -1, "change_pct": 0}},
    )

    assert realtime.get_quotes(["sh600000"], sources=["sina"]) == {}
    assert realtime.get_source_health()["sina"]["status"] == "degraded"


class FakeResponse:
    def __init__(self, text):
        self.text = text
        self.encoding = ""


class FakeSession:
    def __init__(self, text):
        self.text = text
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return FakeResponse(self.text)


def test_sina_uses_one_batch_request_for_multiple_symbols():
    fields = [""] * 32
    fields[0] = "浦发银行"
    fields[1:6] = ["10", "10", "10.1", "10.2", "9.9"]
    fields[8:10] = ["1000", "10000"]
    fields[30:32] = ["10:00:00", "2026-07-10"]
    session = FakeSession(
        f'var hq_str_sh600000="{",".join(fields)}";\n'
        f'var hq_str_sz000001="{",".join(fields)}";'
    )
    realtime = RealtimeData()
    realtime._session = session

    result = realtime.get_realtime_quote_sina(["sh600000", "sz000001"])

    assert set(result) == {"sh600000", "sz000001"}
    assert len(session.urls) == 1
    assert "sh600000,sz000001" in session.urls[0]


def test_tencent_uses_one_batch_request_for_multiple_symbols():
    fields = [""] * 53
    fields[1:7] = ["浦发银行", "600000", "10.1", "10", "10", "10"]
    fields[30:34] = ["20260710100000", "0.1", "1", "10.2"]
    fields[34] = "9.9"
    fields[37:40] = ["1", "1", "10"]
    fields[43:50] = ["2", "100", "80", "1", "11", "9", "1.2"]
    fields[52] = "9"
    raw = "~".join(fields)
    session = FakeSession(
        f'v_sh600000="{raw}";\n'
        f'v_sz000001="{raw}";'
    )
    realtime = RealtimeData()
    realtime._session = session

    result = realtime.get_realtime_quote_tencent(["sh600000", "sz000001"])

    assert set(result) == {"sh600000", "sz000001"}
    assert len(session.urls) == 1
    assert "sh600000,sz000001" in session.urls[0]
