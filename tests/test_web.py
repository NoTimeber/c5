from __future__ import annotations

import base64
import json
import time
from dataclasses import asdict, replace

import pytest
from fastapi.testclient import TestClient

from c5bot import __version__
from c5bot.compare import SteamRate
from c5bot.config import Settings, WatchItem
from c5bot.steam import (
    USD,
    HistoryPoint,
    MarketPage,
    PriceHistory,
    SteamError,
    SteamLoginRequired,
    SteamPrice,
    SteamRateLimited,
)
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
    s = Settings(app_key="k", mode="dry", data_dir=tmp_path, max_total_spend=100.0, steam_source="api")
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
    assert json.loads((tmp_path / "dashboard.json").read_text(encoding="utf-8"))["target_rate"] == 5.04
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


def test_set_proxy_from_dashboard(env, tmp_path, monkeypatch):
    dash, sweeper, client, web = env
    probes = []

    def fake_probe(proxy, timeout=15.0):
        probes.append(proxy)
        if "dead" in proxy:
            raise SteamError("代理连不通: ConnectTimeout")
        return "82.40.98.24"
    monkeypatch.setattr("c5bot.web.probe_proxy", fake_probe)
    assert web.get("/api/state").json()["proxy"] == {"active": None, "source": None, "exit_ip": None}

    r = web.post("/api/proxy", json={"proxy": "proxy.example.com:1337"})
    assert r.status_code == 400 and "格式" in r.json()["error"] and probes == []
    r = web.post("/api/proxy", json={"proxy": "http://u:p@dead.example.com:1337"})
    assert r.status_code == 400 and "连不通" in r.json()["error"]
    assert dash.proxy_override is None and dash._steam.proxy is None

    r = web.post("/api/proxy", json={"proxy": " http://Tim:secret@proxy.example.com:1337 "})
    assert r.json() == {"ok": True, "active": "http://Tim:***@proxy.example.com:1337", "exit_ip": "82.40.98.24"}
    assert dash._steam.proxy == "http://Tim:secret@proxy.example.com:1337"
    assert dash._steam._http.proxies["https"] == "http://Tim:secret@proxy.example.com:1337"
    st = web.get("/api/state").json()
    assert st["proxy"] == {"active": "http://Tim:***@proxy.example.com:1337", "source": "dashboard", "exit_ip": "82.40.98.24"}
    assert "secret" not in json.dumps(st)          # 密码不回显
    saved = json.loads((tmp_path / "dashboard.json").read_text(encoding="utf-8"))
    assert saved["steam_proxy"] == "http://Tim:secret@proxy.example.com:1337"
    # 设目标汇率不会把代理冲掉，反之亦然
    web.post("/api/rate", json={"rate": 5.0})
    saved = json.loads((tmp_path / "dashboard.json").read_text(encoding="utf-8"))
    assert saved == {"target_rate": 5.0, "steam_proxy": "http://Tim:secret@proxy.example.com:1337"}
    # 重启后两样都在
    dash2 = Dashboard(dash.s, sweeper, tmp_path / "t.sqlite", LogBuffer(), watchlist_path=tmp_path / "watchlist.toml")
    assert dash2.target_rate == 5.0 and dash2._steam.proxy == "http://Tim:secret@proxy.example.com:1337"

    assert web.post("/api/proxy", json={"proxy": None}).json() == {"ok": True, "active": None, "exit_ip": None}
    assert dash._steam.proxy is None and dash.target_rate == 5.0


def test_env_proxy_is_fallback(tmp_path):
    s = Settings(app_key="k", mode="dry", data_dir=tmp_path, steam_proxy="socks5://env.example.com:1080")
    sweeper = Sweeper(s, [CASE], FakeClient(), Store(tmp_path / "t.sqlite"), clock=Clock())
    dash = Dashboard(s, sweeper, tmp_path / "t.sqlite", LogBuffer())
    assert dash.proxy_source == "env" and dash._steam.proxy == "socks5://env.example.com:1080"
    assert dash.set_proxy(None) is None and dash._steam.proxy == "socks5://env.example.com:1080"


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


# ---------- Steam 登录与挂单价（账号池） ----------

class FakeAuth:
    """替代 SteamAuth：begin 要验证码，guard 后 poll 成功。steamid 由账号名生成，方便登多个。"""
    instances: list = []

    def __init__(self, *, proxy=None, timeout=15.0):
        self.guards = [4, 3]
        self.steps: list = []
        self.done = False
        self.account = ""
        FakeAuth.instances.append(self)

    def begin(self, account, password):
        self.steps.append(("begin", account, password))
        self.account = account
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
        return SteamSession(f"7656-{self.account}", self.account, jwt(4.1e9), jwt(4e9)) if self.done else None


def login(web, account):
    assert web.post("/api/steam/login", json={"account": account, "password": "pw"}).json()["ok"]
    assert web.post("/api/steam/guard", json={"code": "12345"}).json() == {"ok": True, "logged_in": True}


def fake_history_factory(history, calls):
    def fake_history(app_id, name, *, steamid, cookie):
        calls.append(("history", name, steamid))
        r = history[name]
        if isinstance(r, Exception):
            raise r
        return r
    return fake_history


def test_steam_login_flow_builds_account_pool(env, tmp_path, monkeypatch):
    dash, sweeper, client, web = env
    monkeypatch.setattr("c5bot.web.SteamAuth", FakeAuth)
    assert web.post("/api/steam/login", json={"account": "", "password": ""}).status_code == 400
    r = web.post("/api/steam/login", json={"account": "me", "password": "wrong"})
    assert r.status_code == 400 and "密码" in r.json()["error"]

    r = web.post("/api/steam/login", json={"account": "me", "password": "pw"})
    assert r.json() == {"ok": True, "logged_in": False, "guard": ["device_confirmation", "device_code"]}
    st = web.get("/api/state").json()["steam_login"]
    assert st["pending"] is True and st["count"] == 0 and st["guard"] == ["device_confirmation", "device_code"]
    # 手机上还没确认
    assert web.post("/api/steam/login/poll").json() == {"ok": True, "logged_in": False}
    r = web.post("/api/steam/guard", json={"code": "00000"})
    assert r.status_code == 400 and "令牌" in r.json()["error"]
    assert web.post("/api/steam/guard", json={"code": "12345"}).json() == {"ok": True, "logged_in": True}
    login(web, "me2")
    st = web.get("/api/state").json()
    assert st["steam_login"]["count"] == 2 and st["steam_login"]["usable"] == 2 and st["steam_login"]["pending"] is False
    assert [a["account"] for a in st["steam_accounts"]] == ["me", "me2"]
    saved = json.loads((tmp_path / "steam_sessions.json").read_text(encoding="utf-8"))
    assert [d["account"] for d in saved] == ["me", "me2"]
    # 密码没有进日志
    assert not any("pw" in l["msg"] for l in dash.logs.lines)
    # 同一个账号再登一次：替换，不重复
    login(web, "me")
    assert len(dash.pool) == 2
    # 退出一个
    assert web.post("/api/steam/logout", json={"steamid": "7656-me"}).json() == {"ok": True, "count": 1}
    assert [a["account"] for a in web.get("/api/state").json()["steam_accounts"]] == ["me2"]
    # 全部退出
    assert web.post("/api/steam/logout", json={}).json() == {"ok": True, "count": 0}
    assert not (tmp_path / "steam_sessions.json").exists() and dash._steam.logins == []


def test_pool_loaded_at_start_and_legacy_file_migrated(tmp_path):
    (tmp_path / "steam_session.json").write_text(json.dumps(asdict(session())), encoding="utf-8")
    s = Settings(app_key="k", mode="dry", data_dir=tmp_path)
    sweeper = Sweeper(s, [CASE], FakeClient(), Store(tmp_path / "t.sqlite"), clock=Clock())
    dash = Dashboard(s, sweeper, tmp_path / "t.sqlite", LogBuffer())
    assert [a.session.account for a in dash.pool.accounts] == ["me"]
    assert not (tmp_path / "steam_session.json").exists() and (tmp_path / "steam_sessions.json").exists()
    dash2 = Dashboard(s, sweeper, tmp_path / "t.sqlite", LogBuffer())
    assert len(dash2.pool) == 1


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
    monkeypatch.setattr(dash._steam, "price", fake_price)
    monkeypatch.setattr(dash._steam, "price_history", fake_history_factory(history, calls))

    # 没登录：不拉历史，挂单价 = 最低价
    dash.refresh_steam()
    assert not any(c[0] == "history" for c in calls)
    kilo = web.get("/api/state").json()["items"][0]
    assert kilo["sell_src"] == "lowest" and kilo["steam_sell"] == 1.17

    # 登录后：挂单价 = 累计 30% 成交量的价 1.30，净到手和目标价都按它算
    dash.pool.add(session())
    dash.set_target_rate(5.0)
    dash.refresh_steam()
    assert [(c[1], c[2]) for c in calls if c[0] == "history"] == [(CASE.name, "7656"), (OTHER.name, "7656")]
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
    web.post("/api/steam/logout", json={})
    assert web.get("/api/state").json()["items"][0]["sell_src"] == "lowest"
    assert sweeper.auto_targets[CASE.name] == int(1.02 * disc * 100 + 1e-9) / 100


def test_pool_rotates_accounts_and_cools_down_rate_limited_one(env, monkeypatch):
    dash, sweeper, client, web = env
    now = time.time()
    for i in (1, 2, 3):
        dash.pool.add(SteamSession(f"id{i}", f"acc{i}", jwt(4.1e9), jwt(4e9)))
    monkeypatch.setattr(dash._steam, "price", lambda app_id, name, **kw: SteamPrice(1.17, 1.15, 80000))
    calls = []

    def fake_history(app_id, name, *, steamid, cookie):
        calls.append(steamid)
        if steamid == "id2":
            raise SteamRateLimited("Steam 限流（429）")
        return PriceHistory([HistoryPoint(now - 3600, 1.30, 2000)], "¥ ")
    monkeypatch.setattr(dash._steam, "price_history", fake_history)
    dash.refresh_steam()
    # 两个箱子：第一个用 acc1；第二个轮到 acc2 被 429 -> acc2 歇着，换 acc3 成功
    assert calls == ["id1", "id2", "id3"]
    st = web.get("/api/state").json()
    accounts = {a["account"]: a for a in st["steam_accounts"]}
    assert accounts["acc2"]["cooldown_for"] > 0 and "429" in accounts["acc2"]["error"]
    assert accounts["acc1"]["requests"] == 1 and accounts["acc3"]["requests"] == 1
    assert st["steam"]["blocked_for"] == 0          # 账号被限流不触发全局退避
    assert all(i["sell_src"] == "history" for i in st["items"])
    assert st["steam_login"] == {"count": 3, "usable": 2, "error": None, "pending": False, "guard": []}
    # 下一轮：acc2 歇着跳过，acc1 / acc3 轮流
    calls.clear()
    dash.refresh_steam()
    assert calls == ["id1", "id3"]


def test_usd_wallet_mode(tmp_path, monkeypatch):
    """卖货账号是美元区：Steam 价、净到手都是美元，不量 Steam 汇率，目标价 = 到手美元 × 目标汇率。"""
    s = Settings(app_key="k", mode="dry", data_dir=tmp_path, steam_currency=USD, steam_source="api")
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
                        lambda app_id, name, *, steamid, cookie: PriceHistory([HistoryPoint(now - 3600, 0.181, 9035)], "$"))
    dash.pool.add(session())
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


def test_page_source_is_default_and_feeds_prices_history_and_queue(tmp_path, monkeypatch):
    """默认 STEAM_SOURCE=page：最低价、成交历史、卖单深度全从市场页来，不碰接口、不需要登录账号。"""
    s = Settings(app_key="k", mode="dry", data_dir=tmp_path, steam_currency=USD)
    assert s.steam_source == "page"
    client, store = FakeClient(), Store(tmp_path / "t.sqlite")
    client.stats = {CASE.name: {"sellPrice": 0.65, "sellCount": 10}, OTHER.name: {"sellPrice": 1.2, "sellCount": 5}}
    sweeper = Sweeper(s, [CASE, OTHER], client, store, clock=Clock())
    dash = Dashboard(s, sweeper, tmp_path / "t.sqlite", LogBuffer(), watchlist_path=tmp_path / "w.toml")
    web = TestClient(create_app(dash))
    now = time.time()
    pages = {CASE.name: MarketPage(currency=USD, lowest=0.16,
                                   history=[HistoryPoint(now - 7200, 0.17, 5783), HistoryPoint(now - 3600, 0.16, 2734),
                                            HistoryPoint(now - 5 * 86400, 0.30, 100)],
                                   sell_orders=[(0.16, 4990), (0.17, 27130), (0.18, 35243)], buy_orders=[(0.15, 1200)],
                                   sell_more=(0.19, 651155)),
             OTHER.name: SteamError("Steam 市场页 HTTP 500")}
    calls = []

    def fake_page(app_id, name):
        calls.append(("page", name))
        r = pages[name]
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(dash._steam, "market_page", fake_page)
    monkeypatch.setattr(dash._steam, "price", lambda app_id, name, **kw: calls.append(("price", name)) or SteamPrice(1.67, 1.67, 1))
    monkeypatch.setattr(dash._steam, "price_history", lambda *a, **kw: calls.append(("history",)) or PriceHistory([]))
    dash.set_target_rate(5.2)
    dash.refresh_steam()
    # 千瓦箱：页面成功，不再查接口；变革箱：页面失败回退到 priceoverview；没登录账号所以不查历史
    assert calls == [("page", CASE.name), ("page", OTHER.name), ("price", OTHER.name)]
    sweeper.run_cycle()
    st = web.get("/api/state").json()
    kilo, rev = st["items"]
    assert st["steam_source"] == "page" and st["steam_login"]["count"] == 0
    assert kilo["steam_lowest"] == 0.16 and kilo["steam_volume"] == 5783 + 2734
    # 过去 3 天两个点共 8517 件，30% = 2555：0.17 这一档就有 5783 -> 挂 0.17
    assert kilo["sell_src"] == "history" and kilo["steam_sell"] == 0.17 and kilo["sell_high"] == 0.17
    assert kilo["queue_ahead"] == 4990 + 27130 and kilo["queue_min"] is False   # 挂 0.17：0.16 和 0.17 两档都排在前面
    assert kilo["sell_orders"][0] == [0.16, 4990] and kilo["sell_more"] == [0.19, 651155]
    assert kilo["steam_net"] == pytest.approx(0.15) and kilo["c5_target"] == 0.78   # 0.15 × 5.2 = 0.78
    assert rev["steam_lowest"] == 1.67 and rev["sell_src"] == "lowest" and rev["queue_ahead"] is None
    # 挂单价高过表里最后一个精确档位：合计桶里有一部分也排在前面但分不出，只给下限
    dash.history[CASE.name]["sell"] = 0.25
    kilo = web.get("/api/state").json()["items"][0]
    assert kilo["queue_ahead"] == 4990 + 27130 + 35243 and kilo["queue_min"] is True


def test_page_source_converts_usd_history_for_cny_wallet(env, monkeypatch):
    """人民币钱包 + 页面是美元价（走美国代理时常见）：按 Steam 汇率换算。"""
    dash, sweeper, client, web = env
    dash.s = replace(dash.s, steam_source="page")
    now = time.time()
    monkeypatch.setattr(dash._steam, "price", lambda app_id, name, **kw: SteamPrice(243.0 if kw.get("currency") != USD else 36.05, 0, 1))
    page = MarketPage(currency=USD, lowest=0.16, history=[HistoryPoint(now - 3600, 0.17, 5783)],
                      sell_orders=[(0.16, 4990), (0.17, 27130)], buy_orders=[])
    monkeypatch.setattr(dash._steam, "market_page", lambda app_id, name: page)
    dash.refresh_steam()
    kilo = web.get("/api/state").json()["items"][0]
    rate = 243.0 / 36.05
    assert kilo["steam_sell"] == round(0.17 * rate, 2) and kilo["sell_usd"] == 0.17
    assert kilo["steam_lowest"] == round(0.16 * rate, 2)          # 最低价和卖单价也换成人民币
    assert kilo["sell_orders"][0] == [round(0.16 * rate, 2), 4990] and kilo["queue_ahead"] == 4990 + 27130


def test_access_token_renewed_before_use(env, monkeypatch):
    dash, sweeper, client, web = env
    dash.pool.add(session(access_exp=time.time() + 600))   # 10 分钟后过期
    renewed = session(access_exp=time.time() + 86400)
    monkeypatch.setattr("c5bot.steam_login.renew", lambda sess, **kw: renewed)
    monkeypatch.setattr(dash._steam, "price", lambda app_id, name, **kw: SteamPrice(1.17, 1.15, 1))
    seen = []

    def fake_history(app_id, name, *, steamid, cookie):
        seen.append(cookie)
        return PriceHistory([])
    monkeypatch.setattr(dash._steam, "price_history", fake_history)
    dash.refresh_steam()
    assert dash.pool.accounts[0].session.access_token == renewed.access_token
    assert seen and seen[0] == renewed.cookie        # 用的是续期后的 cookie


def test_rejected_account_is_marked_dead_when_renew_fails(env, monkeypatch):
    dash, sweeper, client, web = env
    dash.pool.add(session())
    monkeypatch.setattr(dash._steam, "price", lambda app_id, name, **kw: SteamPrice(1.17, 1.15, 1))
    hist_calls = []

    def rejected(app_id, name, *, steamid, cookie):
        hist_calls.append(name)
        raise SteamLoginRequired("Steam 登录态失效，请在看板上重新登录")
    monkeypatch.setattr(dash._steam, "price_history", rejected)

    def dead(sess, **kw):
        raise SteamLoginError("Steam 不再接受这个登录态")
    monkeypatch.setattr("c5bot.steam_login.renew", dead)
    dash.refresh_steam()
    assert hist_calls == [CASE.name]          # 唯一的账号废了，第二个箱子不再拉历史
    st = web.get("/api/state").json()
    acc = st["steam_accounts"][0]
    assert acc["dead"] is True and "失效" in acc["error"] and st["steam_login"]["usable"] == 0
    assert "没有可用的 Steam 账号" in st["items"][0]["history_error"]
    assert dash._steam.logins == []


def test_expired_refresh_token_marks_account(env):
    dash, sweeper, client, web = env
    dash.pool.add(session(access_exp=time.time() - 10, refresh_exp=time.time() - 5))
    assert dash.pool.usable() == [] and dash.pool.pick() is None
    assert dash.pool.accounts[0].dead and "过期" in dash.pool.accounts[0].error


def test_reload_watchlist_recomputes_targets(env, tmp_path):
    dash, sweeper, client, web = env
    dash.steam_rate = SteamRate(rate=7.0, name=CASE.name, cny=1.17, usd=0.167, at=1e12)
    dash.set_target_rate(4.9)  # 7 折
    (tmp_path / "watchlist.toml").write_text('[[items]]\nname = "Fracture Case"\nmax_price = 2.5\nmax_qty = 5\n', encoding="utf-8")
    assert web.post("/api/watchlist/reload").json()["ok"] is True
    assert sweeper.auto_targets == {"Fracture Case": None}
