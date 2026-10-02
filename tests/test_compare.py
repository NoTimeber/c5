from __future__ import annotations

import csv

import pytest

from c5bot.compare import (
    append_csv,
    c5_lowest,
    compare_row,
    fetch_steam_rate,
    rate_discount,
    steam_rate,
    target_price,
)
from c5bot.config import WatchItem
from c5bot.steam import USD, SteamError, SteamPrice

CASE = WatchItem(name="Kilowatt Case", max_price=0.7, max_qty=10)


def test_c5_lowest_ignores_missing_or_zero():
    assert c5_lowest({"sellPrice": 0.75}) == 0.75
    assert c5_lowest({"sellPrice": 0}) is None
    assert c5_lowest({}) is None and c5_lowest(None) is None


def test_compare_row_math():
    r = compare_row(CASE, 0.75, SteamPrice(1.17, 1.15, 80000))
    assert r.steam_net == pytest.approx(1.02)
    assert r.discount_at_lowest == pytest.approx(0.75 / 1.02)
    assert r.discount_at_target == pytest.approx(0.70 / 1.02)
    r = compare_row(CASE, None, None)
    assert r.steam_net is None and r.discount_at_lowest is None and r.discount_at_target is None


def test_append_csv_writes_header_once(tmp_path):
    path = tmp_path / "compare.csv"
    row = compare_row(CASE, 0.75, SteamPrice(1.17, 1.15, 80000))
    append_csv(path, "2026-09-30 10:00:00", [row])
    append_csv(path, "2026-09-30 10:10:00", [row])
    append_csv(path, "2026-09-30 10:20:00", [])
    rows = list(csv.reader(path.open(encoding="utf-8")))
    assert rows[0][:3] == ["time", "name", "c5_lowest"]
    assert len(rows) == 3
    assert rows[1] == ["2026-09-30 10:00:00", "Kilowatt Case", "0.75", "0.7", "1.17", "1.02",
                       str(round(0.75 / 1.02, 4)), str(round(0.7 / 1.02, 4)), "1.15", "80000", "", "", ""]


# ---------- 汇率 ----------

def test_steam_rate_and_discount():
    assert steam_rate(10.07, 1.40) == pytest.approx(7.1929, abs=1e-4)
    assert steam_rate(None, 1.4) is None and steam_rate(10.0, 0) is None
    # 想 5 元换 1 美元余额，Steam 按 7.2 换算 -> 6.94 折
    assert rate_discount(5.0, 7.2) == pytest.approx(5.0 / 7.2)
    assert rate_discount(None, 7.2) is None and rate_discount(5.0, None) is None


def test_target_price_floors_to_cents():
    # Steam 1.17 -> 到手 1.02，× 0.7 = 0.714 -> 0.71（向下取整，宁可少买）
    assert target_price(0.7, 1.17) == 0.71
    assert target_price(0.7, None) is None and target_price(None, 1.17) is None
    assert target_price(0.7, 0.02) is None  # 到手 0，没有目标价


def test_compare_row_with_auto_target_and_rate():
    r = compare_row(CASE, 0.75, SteamPrice(1.17, 1.15, 80000), target=0.71, rate=7.2)
    assert r.c5_target == 0.71
    assert r.discount_at_target == pytest.approx(0.71 / 1.02)
    assert r.rate_at_lowest == pytest.approx(0.75 / 1.02 * 7.2)
    assert r.rate_at_target == pytest.approx(0.71 / 1.02 * 7.2)
    # 设了汇率但还没有 Steam 价：目标价 None，别的字段照常
    r = compare_row(CASE, 0.75, None, target=None, rate=7.2)
    assert r.c5_target is None and r.discount_at_target is None and r.rate_at_target is None
    assert r.c5_lowest == 0.75


class FakeSteam:
    def __init__(self, prices):
        self.prices = prices  # (name, currency) -> SteamPrice | Exception
        self.calls = []

    def price(self, app_id, name, *, currency=None):
        self.calls.append((name, currency))
        r = self.prices[(name, currency)]
        if isinstance(r, Exception):
            raise r
        return r


def test_fetch_steam_rate_uses_most_expensive_item():
    cheap, dear = WatchItem("Kilowatt Case", 0.7, 1), WatchItem("Dreams & Nightmares Case", 6.0, 1)
    steam = FakeSteam({(dear.name, USD): SteamPrice(1.40, 1.40, 1)})
    r = fetch_steam_rate(steam, [(cheap, SteamPrice(1.08, 1.08, 1)), (dear, SteamPrice(10.07, 10.05, 1))])
    assert r.name == dear.name and r.cny == 10.07 and r.usd == 1.40
    assert r.rate == pytest.approx(10.07 / 1.40)
    assert steam.calls == [(dear.name, USD)]  # 只多查一次，而且是贵的那个


def test_fetch_steam_rate_errors():
    with pytest.raises(SteamError):
        fetch_steam_rate(FakeSteam({}), [])
    it = WatchItem("Kilowatt Case", 0.7, 1)
    with pytest.raises(SteamError):
        fetch_steam_rate(FakeSteam({(it.name, USD): SteamError("429")}), [(it, SteamPrice(1.08, 1.08, 1))])
    with pytest.raises(SteamError):
        fetch_steam_rate(FakeSteam({(it.name, USD): SteamPrice(None, None, None)}), [(it, SteamPrice(1.08, 1.08, 1))])
