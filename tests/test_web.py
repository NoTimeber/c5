from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from c5bot import __version__
from c5bot.compare import SteamRate
from c5bot.config import Settings, WatchItem
from c5bot.steam import USD, SteamError, SteamPrice
from c5bot.store import Store
from c5bot.sweeper import Sweeper
from c5bot.web import Dashboard, LogBuffer, create_app
from tests.test_sweeper import Clock, FakeClient, listing

CASE = WatchItem(name="Kilowatt Case", max_price=0.7, max_qty=3)
OTHER = WatchItem(name="Revolution Case", max_price=1.0, max_qty=3)


@pytest.fixture
def env(tmp_path):
    s = Settings(app_key="k", mode="dry", data_dir=tmp_path, max_total_spend=100.0)
    client, store, clock = FakeClient(), Store(tmp_path / "t.sqlite"), Clock()
    client.stats = {CASE.name: {"sellPrice": 0.65, "sellCount": 10, "purchaseMaxPrice": 0.6},
                    OTHER.name: {"sellPrice": 1.2, "sellCount": 5}}
    client.listings = [listing(1, 0.65), listing(2, 0.68)]
    sweeper = Sweeper(s, [CASE, OTHER], client, store, clock=clock)
    dash = Dashboard(s, sweeper, tmp_path / "t.sqlite", LogBuffer(), watchlist_path=tmp_path / "watchlist.toml")
    return dash, sweeper, client, TestClient(create_app(dash))


def test_state_before_first_cycle(env):
    dash, sweeper, client, web = env
    st = web.get("/api/state").json()
    assert st["mode"] == "dry" and st["paused"] is False and st["cycle"]["n"] == 0
    assert st["version"] == __version__
    assert [i["status"] for i in st["items"]] == ["unknown", "unknown"]


def test_state_after_cycle_shows_prices_purchases_and_steam(env):
    dash, sweeper, client, web = env
    dash.steam[CASE.name] = {"price": SteamPrice(1.17, 1.15, 80000), "at": 1.0, "error": None}
    sweeper.run_cycle()
    dash.on_cycle(None)
    st = web.get("/api/state").json()
    kilo, rev = st["items"]
    # 买了 2 个（max_qty 3 还没满），价格仍在目标价内 -> 到价
    assert kilo["status"] == "hit" and kilo["bought"] == 2 and kilo["spent"] == pytest.approx(1.33)
    assert kilo["steam_net"] == pytest.approx(1.02)
    assert kilo["discount_at_lowest"] == pytest.approx(0.65 / 1.02)
    assert rev["status"] == "watch" and rev["steam_lowest"] is None
    assert st["budget"]["spent"] == pytest.approx(1.33) and st["budget"]["qty"] == 2
    assert [p["status"] for p in st["purchases"]] == ["ok", "ok"]
    assert st["cycle"]["n"] == 1


def test_pause_stops_buying_but_keeps_prices(env):
    dash, sweeper, client, web = env
    assert web.post("/api/pause").json() == {"ok": True, "paused": True}
    sweeper.run_cycle()
    st = web.get("/api/state").json()
    assert st["paused"] is True
    assert st["items"][0]["status"] == "hit" and st["items"][0]["c5_lowest"] == 0.65
    assert st["purchases"] == [] and client.count("search_products") == 0
    assert web.post("/api/resume").json()["paused"] is False
    sweeper.run_cycle()
    assert len(web.get("/api/state").json()["purchases"]) == 2


def test_reload_watchlist(env, tmp_path):
    dash, sweeper, client, web = env
    r = web.post("/api/watchlist/reload")
    assert r.status_code == 400 and "watchlist.toml" in r.json()["error"]
    (tmp_path / "watchlist.toml").write_text('[[items]]\nname = "Fracture Case"\nmax_price = 2.5\nmax_qty = 5\n', encoding="utf-8")
    assert web.post("/api/watchlist/reload").json() == {"ok": True, "count": 1}
    assert [i["name"] for i in web.get("/api/state").json()["items"]] == ["Fracture Case"]


def test_orders_refresh_only_in_live(env):
    dash, sweeper, client, web = env
    assert web.post("/api/orders/refresh").status_code == 400
    dash.s = Settings(app_key="k", mode="live", trade_url="https://t", data_dir=dash.s.data_dir, max_total_spend=1)
    assert web.post("/api/orders/refresh").json() == {"ok": True}
    assert sweeper.refresh_requested is True


def test_index_and_static_served(env):
    _, _, _, web = env
    assert "C5 扫货看板" in web.get("/").text
    assert web.get("/static/app.js").status_code == 200


# ---------- 目标汇率 ----------

def test_set_target_rate_computes_targets_and_persists(env, tmp_path):
    dash, sweeper, client, web = env
    dash.steam[CASE.name] = {"price": SteamPrice(1.17, 1.15, 80000), "at": 1e12, "error": None}
    dash.steam_rate = SteamRate(rate=7.2, name=CASE.name, cny=1.17, usd=0.1625, at=1e12)

    assert web.post("/api/rate", json={"rate": "abc"}).status_code == 400
    assert web.post("/api/rate", json={"rate": 500}).status_code == 400
    assert sweeper.auto_targets is None

    assert web.post("/api/rate", json={"rate": 5.04}).json() == {"ok": True, "rate": 5.04}
    assert (tmp_path / "dashboard.json").read_text(encoding="utf-8") == '{"target_rate": 5.04}'
    # 5.04 / 7.2 = 0.7 折；Kilowatt 到手 1.02 × 0.7 = 0.714 -> 0.71；Revolution 没有 Steam 价 -> None
    assert sweeper.auto_targets == {CASE.name: 0.71, OTHER.name: None}
    sweeper.run_cycle()
    st = web.get("/api/state").json()
    assert st["rate"]["target"] == 5.04 and st["rate"]["discount"] == pytest.approx(0.7)
    assert st["rate"]["steam"]["rate"] == 7.2 and st["rate"]["steam"]["name"] == CASE.name
    kilo, rev = st["items"]
    assert kilo["c5_target"] == 0.71 and kilo["target_auto"] is True and kilo["max_price"] == 0.7
    assert kilo["status"] == "hit" and kilo["rate_at_lowest"] == pytest.approx(0.65 / 1.02 * 7.2)
    assert rev["c5_target"] is None and rev["status"] == "nosteam" and rev["target_auto"] is True
    # 在售 0.65 和 0.68 都 <= 0.71，买了两个
    assert len(st["purchases"]) == 2

    assert web.post("/api/rate", json={"rate": None}).json() == {"ok": True, "rate": None}
    assert sweeper.auto_targets is None
    st = web.get("/api/state").json()
    assert st["items"][0]["c5_target"] == 0.7 and st["items"][0]["target_auto"] is False
    assert st["items"][1]["status"] == "watch"


def test_target_rate_loaded_at_start_and_waits_for_steam(tmp_path):
    (tmp_path / "dashboard.json").write_text('{"target_rate": 5.0}', encoding="utf-8")
    s = Settings(app_key="k", mode="dry", data_dir=tmp_path)
    sweeper = Sweeper(s, [CASE], FakeClient(), Store(tmp_path / "t.sqlite"), clock=Clock())
    dash = Dashboard(s, sweeper, tmp_path / "t.sqlite", LogBuffer())
    assert dash.target_rate == 5.0
    assert sweeper.auto_targets == {CASE.name: None}  # 还没有 Steam 价，先不买


def test_refresh_steam_fetches_rate_and_applies_targets(env, monkeypatch):
    dash, sweeper, client, web = env
    ref = dash.s.steam_rate_item
    prices = {
        (CASE.name, None): SteamPrice(1.08, 1.08, 86298),
        (OTHER.name, None): SteamPrice(1.67, 1.67, 101052),
        (ref, None): SteamPrice(243.00, 250.93, 93),   # 参照饰品查人民币价和美元价
        (ref, USD): SteamPrice(36.05, 37.23, 93),
    }
    calls = []

    def fake_price(app_id, name, *, currency=None):
        calls.append((name, currency))
        return prices[(name, currency)]
    monkeypatch.setattr(dash._steam, "price", fake_price)
    dash.set_target_rate(5.0)
    dash.refresh_steam()
    assert calls == [(CASE.name, None), (OTHER.name, None), (ref, None), (ref, USD)]
    assert dash.steam_rate.rate == pytest.approx(243.00 / 36.05) and dash.steam_rate.name == ref
    disc = 5.0 / (243.00 / 36.05)
    assert sweeper.auto_targets == {
        CASE.name: int(0.95 * disc * 100 + 1e-9) / 100,    # 1.08 -> 到手 0.95
        OTHER.name: int(1.46 * disc * 100 + 1e-9) / 100,   # 1.67 -> 到手 1.46
    }
    assert (dash.s.data_dir / "compare.csv").exists()

    # 美元价查失败：沿用上次的汇率，目标价照常
    def flaky(app_id, name, *, currency=None):
        if currency == USD:
            raise SteamError("Steam 限流（429）")
        return prices[(name, currency)]
    monkeypatch.setattr(dash._steam, "price", flaky)
    dash.refresh_steam()
    assert dash.steam_rate.rate == pytest.approx(243.00 / 36.05) and "429" in dash.steam_rate_error
    assert sweeper.auto_targets[CASE.name] == int(0.95 * disc * 100 + 1e-9) / 100


def test_reload_watchlist_recomputes_targets(env, tmp_path):
    dash, sweeper, client, web = env
    dash.steam_rate = SteamRate(rate=7.0, name=CASE.name, cny=1.17, usd=0.167, at=1e12)
    dash.set_target_rate(4.9)  # 7 折
    (tmp_path / "watchlist.toml").write_text('[[items]]\nname = "Fracture Case"\nmax_price = 2.5\nmax_qty = 5\n', encoding="utf-8")
    assert web.post("/api/watchlist/reload").json()["ok"] is True
    assert sweeper.auto_targets == {"Fracture Case": None}
