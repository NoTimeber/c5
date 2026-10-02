from __future__ import annotations

import base64
import json
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from c5bot import __version__
from c5bot.compare import SteamRate
from c5bot.config import Settings, WatchItem
from c5bot.steam import USD, HistoryPoint, PriceHistory, SteamError, SteamLoginRequired, SteamPrice
from c5bot.steam_login import SteamLoginError, SteamSession
from c5bot.store import Store
from c5bot.sweeper import Sweeper
from c5bot.web import RATE_REFRESH_SEC, Dashboard, LogBuffer, create_app


def jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def session(access_exp: float = 4e9, refresh_exp: float = 4.1e9) -> SteamSession:
    return SteamSession("7656", "me", jwt(refresh_exp), jwt(access_exp))
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
    assert calls == [(ref, None), (ref, USD), (CASE.name, None), (OTHER.name, None)]  # 先量汇率，再查箱子
    assert dash.steam_rate.rate == pytest.approx(243.00 / 36.05) and dash.steam_rate.name == ref
    disc = 5.0 / (243.00 / 36.05)
    assert sweeper.auto_targets == {
        CASE.name: int(0.95 * disc * 100 + 1e-9) / 100,    # 1.08 -> 到手 0.95
        OTHER.name: int(1.46 * disc * 100 + 1e-9) / 100,   # 1.67 -> 到手 1.46
    }
    assert (dash.s.data_dir / "compare.csv").exists()

    # 一小时内再刷新：只查箱子，不再量汇率
    calls.clear()
    dash.refresh_steam()
    assert calls == [(CASE.name, None), (OTHER.name, None)]

    # 汇率过期后重新量，美元价查失败：沿用上次的汇率，目标价照常
    dash.steam_rate = replace(dash.steam_rate, at=time.time() - RATE_REFRESH_SEC - 1)

    def flaky(app_id, name, *, currency=None):
        if currency == USD:
            raise SteamError("Steam 限流（429）")
        return prices[(name, currency)]
    monkeypatch.setattr(dash._steam, "price", flaky)
    dash.refresh_steam()
    assert dash.steam_rate.rate == pytest.approx(243.00 / 36.05) and "429" in dash.steam_rate_error
    assert sweeper.auto_targets[CASE.name] == int(0.95 * disc * 100 + 1e-9) / 100


def test_refresh_stops_hammering_steam_when_rate_limited(env, monkeypatch):
    dash, sweeper, client, web = env
    gets = []

    class Resp:
        status_code = 429

        def json(self):
            return {}

    def fake_get(url, *, timeout, params):
        gets.append(params["market_hash_name"])
        return Resp()
    monkeypatch.setattr(dash._steam._http, "get", fake_get)
    monkeypatch.setattr(dash._steam, "_throttle", lambda: None)
    dash.refresh_steam()
    # 第一个请求（量汇率）被 429 后，饰品行情和成交历史都不再请求
    assert gets == [dash.s.steam_rate_item]
    st = web.get("/api/state").json()
    assert 0 < st["steam"]["blocked_for"] <= 300
    assert "429" in st["rate"]["steam_error"]
    assert st["items"][0]["steam_error"] is None and st["items"][1]["steam_error"] is None
    assert dash.steam_rate is None
    r = web.post("/api/steam/refresh")
    assert r.status_code == 429 and "限流" in r.json()["error"]
    # 退避期内再刷新也不发请求
    dash.refresh_steam()
    assert gets == [dash.s.steam_rate_item]


def test_manual_refresh_throttled(env):
    dash, sweeper, client, web = env
    dash.steam_at = time.time()
    r = web.post("/api/steam/refresh")
    assert r.status_code == 429 and "刚刷新过" in r.json()["error"]
    dash.steam_at = time.time() - 120
    assert web.post("/api/steam/refresh").json() == {"ok": True}


# ---------- Steam 登录与挂单价 ----------

class FakeAuth:
    """替代 SteamAuth：begin 要验证码，guard 后 poll 成功。"""
    instances: list = []

    def __init__(self, *, proxy=None, timeout=15.0):
        self.guards = [4, 3]
        self.steps: list = []
        self.done = False
        FakeAuth.instances.append(self)

    def begin(self, account, password):
        self.steps.append(("begin", account, password))
        if password == "wrong":
            raise SteamLoginError("密码错误")
        return ["device_confirmation", "device_code"]

    def guard(self, code):
        self.steps.append(("guard", code))
        if code != "12345":
            raise SteamLoginError("手机令牌验证码不对")
        self.done = True

    def poll(self):
        self.steps.append(("poll",))
        return session() if self.done else None


def test_steam_login_flow_persists_session(env, tmp_path, monkeypatch):
    dash, sweeper, client, web = env
    monkeypatch.setattr("c5bot.web.SteamAuth", FakeAuth)
    assert web.post("/api/steam/login", json={"account": "", "password": ""}).status_code == 400
    r = web.post("/api/steam/login", json={"account": "me", "password": "wrong"})
    assert r.status_code == 400 and "密码" in r.json()["error"]

    r = web.post("/api/steam/login", json={"account": "me", "password": "pw"})
    assert r.json() == {"ok": True, "logged_in": False, "guard": ["device_confirmation", "device_code"]}
    st = web.get("/api/state").json()["steam_login"]
    assert st["pending"] is True and st["account"] is None and st["guard"] == ["device_confirmation", "device_code"]
    # 手机上还没确认
    assert web.post("/api/steam/login/poll").json() == {"ok": True, "logged_in": False}
    r = web.post("/api/steam/guard", json={"code": "00000"})
    assert r.status_code == 400 and "令牌" in r.json()["error"]
    assert web.post("/api/steam/guard", json={"code": "12345"}).json() == {"ok": True, "logged_in": True}
    st = web.get("/api/state").json()["steam_login"]
    assert st["account"] == "me" and st["pending"] is False and st["refresh_exp"] == 4.1e9
    assert json.loads((tmp_path / "steam_session.json").read_text(encoding="utf-8"))["account"] == "me"
    assert dash._steam.logged_in
    # 密码没有进日志
    assert not any("pw" in l["msg"] for l in dash.logs.lines)

    assert web.post("/api/steam/logout").json() == {"ok": True}
    assert not (tmp_path / "steam_session.json").exists() and not dash._steam.logged_in
    assert web.get("/api/state").json()["steam_login"]["account"] is None


def test_session_loaded_at_start(tmp_path):
    from c5bot.steam_login import save_session
    save_session(tmp_path / "steam_session.json", session())
    s = Settings(app_key="k", mode="dry", data_dir=tmp_path)
    sweeper = Sweeper(s, [CASE], FakeClient(), Store(tmp_path / "t.sqlite"), clock=Clock())
    dash = Dashboard(s, sweeper, tmp_path / "t.sqlite", LogBuffer())
    assert dash.steam_session.account == "me" and dash._steam.logged_in


def test_refresh_uses_history_sell_price_when_logged_in(env, monkeypatch):
    dash, sweeper, client, web = env
    now = time.time()
    prices = {CASE.name: SteamPrice(1.17, 1.15, 80000), OTHER.name: SteamPrice(1.67, 1.67, 101052),
              dash.s.steam_rate_item: SteamPrice(243.0, 250.0, 93)}
    # 1.30 这一档成交 2000 件，占窗口内 5000 件的 40% >= 30% -> 挂 1.30
    history = {CASE.name: PriceHistory([HistoryPoint(now - 86400, 1.30, 2000), HistoryPoint(now - 3600, 1.17, 3000),
                                        HistoryPoint(now - 10 * 86400, 2.00, 100)], "¥ "),
               OTHER.name: PriceHistory([HistoryPoint(now - 3600, 9.00, 10)], "¥ ")}   # 和当前中位价 1.67 差太多：疑似币种不对
    calls = []

    def fake_price(app_id, name, *, currency=None):
        calls.append(("price", name, currency))
        return SteamPrice(36.05, 37.0, 93) if currency == USD else prices[name]

    def fake_history(app_id, name):
        calls.append(("history", name))
        return history[name]
    monkeypatch.setattr(dash._steam, "price", fake_price)
    monkeypatch.setattr(dash._steam, "price_history", fake_history)

    # 没登录：不拉历史，挂单价 = 最低价
    dash.refresh_steam()
    assert not any(c[0] == "history" for c in calls)
    kilo = web.get("/api/state").json()["items"][0]
    assert kilo["sell_src"] == "lowest" and kilo["steam_sell"] == 1.17

    # 登录后：挂单价 = 3 天内最高小时中位价 1.30，净到手和目标价都按它算
    dash._set_session(session())
    dash.set_target_rate(5.0)
    dash.refresh_steam()
    assert [c[1] for c in calls if c[0] == "history"] == [CASE.name, OTHER.name]
    st = web.get("/api/state").json()
    kilo, rev = st["items"]
    assert kilo["sell_src"] == "history" and kilo["steam_sell"] == 1.30
    assert (kilo["sell_volume"], kilo["sell_total"], kilo["sell_high"], kilo["sell_share"]) == (2000, 5000, 1.30, 0.3)
    assert kilo["steam_lowest"] == 1.17 and kilo["steam_net"] == pytest.approx(1.14)
    disc = 5.0 / (243.0 / 36.05)
    assert sweeper.auto_targets[CASE.name] == int(1.14 * disc * 100 + 1e-9) / 100
    assert rev["sell_src"] == "lowest" and "币种" in rev["history_error"]
    assert sweeper.auto_targets[OTHER.name] == int(1.46 * disc * 100 + 1e-9) / 100

    # 美元区账号：历史是美元价，按 Steam 汇率换算成人民币（0.19 × 6.74 = 1.28）
    history[CASE.name] = PriceHistory([HistoryPoint(now - 3600, 0.19, 900)], "$")
    dash.refresh_steam()
    kilo = web.get("/api/state").json()["items"][0]
    rate = 243.0 / 36.05
    assert kilo["sell_src"] == "history" and kilo["steam_sell"] == round(0.19 * rate, 2) and kilo["sell_usd"] == 0.19

    # 退出登录：回到最低价
    web.post("/api/steam/logout")
    assert web.get("/api/state").json()["items"][0]["sell_src"] == "lowest"
    assert sweeper.auto_targets[CASE.name] == int(1.02 * disc * 100 + 1e-9) / 100


def test_usd_wallet_mode(tmp_path, monkeypatch):
    """卖货账号是美元区：Steam 价、净到手都是美元，不量 Steam 汇率，目标价 = 到手美元 × 目标汇率。"""
    s = Settings(app_key="k", mode="dry", data_dir=tmp_path, steam_currency=USD)
    client, store = FakeClient(), Store(tmp_path / "t.sqlite")
    client.stats = {CASE.name: {"sellPrice": 0.65, "sellCount": 10}}
    client.listings = [listing(1, 0.65)]
    sweeper = Sweeper(s, [CASE], client, store, clock=Clock())
    dash = Dashboard(s, sweeper, tmp_path / "t.sqlite", LogBuffer(), watchlist_path=tmp_path / "w.toml")
    web = TestClient(create_app(dash))
    calls = []

    def fake_price(app_id, name, *, currency=None):
        calls.append((name, currency))
        return SteamPrice(0.17, 0.17, 80000)   # 美元
    monkeypatch.setattr(dash._steam, "price", fake_price)
    now = time.time()
    monkeypatch.setattr(dash._steam, "price_history",
                        lambda app_id, name: PriceHistory([HistoryPoint(now - 3600, 0.181, 9035)], "$"))
    dash._set_session(session())
    dash.set_target_rate(5.2)
    dash.refresh_steam()
    assert calls == [(CASE.name, None)]          # 不量 Steam 汇率
    assert dash.steam_rate is None and dash.discount() == pytest.approx(5.2)
    sweeper.run_cycle()
    st = web.get("/api/state").json()
    assert st["usd_wallet"] is True and st["rate"]["steam"] is None and st["rate"]["discount"] == pytest.approx(5.2)
    kilo = st["items"][0]
    # 美元区账号的历史不换算：挂单价 $0.181，到手 $0.16（18 分，两项手续费各扣最低 1 分）
    assert kilo["steam_sell"] == 0.181 and kilo["sell_usd"] is None and kilo["steam_net"] == pytest.approx(0.16)
    # 汇率 = C5 人民币价 ÷ 到手美元
    assert kilo["rate_at_lowest"] == pytest.approx(0.65 / 0.16)
    # 目标价 = 到手美元 × 目标汇率 = 0.16 × 5.2 = 0.832 -> 0.83
    assert kilo["c5_target"] == 0.83 and kilo["rate_at_target"] == pytest.approx(0.83 / 0.16)
    assert kilo["status"] == "hit" and len(st["purchases"]) == 1


def test_access_token_renewed_before_expiry(env, monkeypatch):
    dash, sweeper, client, web = env
    dash._set_session(session(access_exp=time.time() + 600))   # 10 分钟后过期
    renewed = session(access_exp=time.time() + 86400)
    monkeypatch.setattr("c5bot.web.renew", lambda sess, **kw: renewed)
    monkeypatch.setattr(dash._steam, "price", lambda app_id, name, **kw: SteamPrice(1.17, 1.15, 1))
    monkeypatch.setattr(dash._steam, "price_history", lambda app_id, name: PriceHistory([]))
    dash.refresh_steam()
    assert dash.steam_session.access_token == renewed.access_token
    assert dash._steam._auth_http.cookies.get("steamLoginSecure", domain="steamcommunity.com") == renewed.cookie


def test_rejected_session_is_dropped_when_renew_fails(env, monkeypatch):
    dash, sweeper, client, web = env
    dash._set_session(session())
    monkeypatch.setattr(dash._steam, "price", lambda app_id, name, **kw: SteamPrice(1.17, 1.15, 1))
    hist_calls = []

    def rejected(app_id, name):
        hist_calls.append(name)
        raise SteamLoginRequired("Steam 登录态失效，请在看板上重新登录")
    monkeypatch.setattr(dash._steam, "price_history", rejected)

    def dead(sess, **kw):
        raise SteamLoginError("Steam 不再接受这个登录态")
    monkeypatch.setattr("c5bot.web.renew", dead)
    dash.refresh_steam()
    assert hist_calls == [CASE.name]          # 第一个饰品失败后不再拉历史
    st = web.get("/api/state").json()["steam_login"]
    assert st["account"] is None and "失效" in st["error"]
    assert not dash._steam.logged_in


def test_expired_refresh_token_requires_relogin(env):
    dash, sweeper, client, web = env
    dash._set_session(session(access_exp=time.time() - 10, refresh_exp=time.time() - 5))
    assert dash._ensure_session() is False
    assert dash.steam_session is None and "过期" in dash.steam_login_error


def test_reload_watchlist_recomputes_targets(env, tmp_path):
    dash, sweeper, client, web = env
    dash.steam_rate = SteamRate(rate=7.0, name=CASE.name, cny=1.17, usd=0.167, at=1e12)
    dash.set_target_rate(4.9)  # 7 折
    (tmp_path / "watchlist.toml").write_text('[[items]]\nname = "Fracture Case"\nmax_price = 2.5\nmax_qty = 5\n', encoding="utf-8")
    assert web.post("/api/watchlist/reload").json()["ok"] is True
    assert sweeper.auto_targets == {"Fracture Case": None}
