from __future__ import annotations

import csv

import pytest

from c5bot.compare import append_csv, c5_lowest, compare_row
from c5bot.config import WatchItem
from c5bot.steam import SteamPrice

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
                       str(round(0.75 / 1.02, 4)), str(round(0.7 / 1.02, 4)), "1.15", "80000"]
