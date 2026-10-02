from __future__ import annotations

import pytest

from c5bot.steam import USD, SteamMarket, discount, parse_money, seller_receives


def test_steam_session_never_advertises_brotli():
    # Steam 对带 br 的 Accept-Encoding 直接回 429
    assert "br" not in SteamMarket()._http.headers["Accept-Encoding"].split(", ")


@pytest.mark.parametrize("text, value", [
    ("¥ 1.17", 1.17), ("¥ 1,234.56", 1234.56), ("79,961", 79961.0), ("$0.03", 0.03),
    ("", None), (None, None),
])
def test_parse_money(text, value):
    assert parse_money(text) == value


@pytest.mark.parametrize("price, net", [
    (1.17, 1.02),     # 常见小额：5% 和 10% 各扣 0.05 / 0.10
    (100.00, 86.97),  # Steam 官方算法的结果，不是简单的 /1.15
    (0.03, 0.01),     # 两项手续费最低各 0.01
    (0.02, 0.00),     # 不够付手续费
    (5.00, 4.36),
])
def test_seller_receives_matches_steam(price, net):
    assert seller_receives(price) == pytest.approx(net)


def test_discount():
    assert discount(0.75, 1.17) == pytest.approx(0.75 / 1.02)
    assert discount(0.75, None) is None
    assert discount(0.75, 0.02) is None


def test_price_currency_override(monkeypatch):
    m = SteamMarket(currency=23)
    seen = []

    class Resp:
        status_code = 200

        def json(self):
            return {"success": True, "lowest_price": "$1.40", "median_price": "$1.39", "volume": "100"}

    def fake_get(url, *, timeout, params):
        seen.append(params["currency"])
        return Resp()
    monkeypatch.setattr(m._http, "get", fake_get)
    monkeypatch.setattr(m, "_throttle", lambda: None)
    assert m.price(730, "Kilowatt Case").lowest == 1.40
    assert m.price(730, "Kilowatt Case", currency=USD).lowest == 1.40
    assert seen == [23, USD]
