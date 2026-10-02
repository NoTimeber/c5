from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from c5bot.config import Settings, WatchItem
from c5bot.steam import SteamPrice
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
