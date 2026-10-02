from __future__ import annotations

import pytest

from c5bot.steam import SteamMarket, discount, parse_money, seller_receives


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
