from __future__ import annotations

import pytest

from c5bot.steam import (
    BLOCK_SEC,
    BLOCK_SEC_PROXY,
    MIN_INTERVAL,
    MIN_INTERVAL_PROXY,
    USD,
    HistoryPoint,
    SteamError,
    SteamLoginRequired,
    SteamMarket,
    check_proxy_url,
    discount,
    fmt_wait,
    mask_proxy,
    parse_history_time,
    parse_money,
    sell_price_from_history,
    seller_receives,
)


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


def test_proxy_shortens_interval_and_backoff():
    direct, via = SteamMarket(), SteamMarket(proxy="http://u:p@proxy.example.com:1337")
    assert (direct._interval, direct._block_sec) == (MIN_INTERVAL, BLOCK_SEC)
    assert (via._interval, via._block_sec) == (MIN_INTERVAL_PROXY, BLOCK_SEC_PROXY)
    assert via._http.proxies == {"http": "http://u:p@proxy.example.com:1337", "https": "http://u:p@proxy.example.com:1337"}
    assert via._auth_http.proxies == via._http.proxies
    # 运行时切换：换了出口，限流状态重置
    direct._blocked_until = direct._now() + 100
    direct.set_proxy("socks5://proxy.example.com:1080")
    assert direct.proxy == "socks5://proxy.example.com:1080" and direct.blocked_for == 0
    assert direct._interval == MIN_INTERVAL_PROXY and direct._http.proxies["https"] == "socks5://proxy.example.com:1080"
    direct.set_proxy(None)
    assert direct.proxy is None and direct._http.proxies == {} and direct._interval == MIN_INTERVAL


def test_proxy_retries_429_with_new_exit_before_blocking(monkeypatch):
    m = SteamMarket(proxy="http://u:p@proxy.example.com:1337")
    assert m._http.headers["Connection"] == "close"   # 每个请求新建连接，轮转代理才会换出口
    monkeypatch.setattr(m, "_throttle", lambda: None)
    codes = iter([429, 429, 200, 429, 429, 429])
    calls = []

    class Resp:
        def __init__(self, code):
            self.status_code = code

        def json(self):
            return {"success": True, "lowest_price": "$0.16", "median_price": "$0.16", "volume": "1"}

    def fake_get(url, *, timeout, params):
        calls.append(url)
        return Resp(next(codes))
    monkeypatch.setattr(m._http, "get", fake_get)
    # 两次 429 后第三次成功：不退避
    assert m.price(730, "Kilowatt Case").lowest == 0.16
    assert len(calls) == 3 and m.blocked_for == 0
    # 三次都 429 才退避（代理模式 30 秒）
    with pytest.raises(SteamError, match="限流"):
        m.price(730, "Kilowatt Case")
    assert len(calls) == 6 and m.blocked_for == pytest.approx(BLOCK_SEC_PROXY)
    m.set_proxy(None)
    assert "Connection" not in m._http.headers


def test_proxy_url_check_and_mask():
    assert check_proxy_url("  http://u:p@proxy.example.com:1337 ") == "http://u:p@proxy.example.com:1337"
    assert check_proxy_url("socks5://proxy.example.com:1080") == "socks5://proxy.example.com:1080"
    for bad in ("proxy.example.com:1337", "ftp://x:1", "http://", "http://h:notaport"):
        with pytest.raises(SteamError):
            check_proxy_url(bad)
    assert mask_proxy("http://Tim_pool-x:secret@proxy.example.com:1337") == "http://Tim_pool-x:***@proxy.example.com:1337"
    assert mask_proxy("socks5://proxy.example.com:1080") == "socks5://proxy.example.com:1080"
    assert mask_proxy(None) is None


def test_fmt_wait():
    assert fmt_wait(45) == "45 秒" and fmt_wait(300) == "5 分钟" and fmt_wait(90) == "2 分钟"


def test_429_blocks_further_requests_with_backoff(monkeypatch):
    now = [1000.0]
    m = SteamMarket(clock=lambda: now[0])
    codes = iter([429, 429, 200])
    calls = []

    class Resp:
        def __init__(self, code):
            self.status_code = code

        def json(self):
            return {"success": True, "lowest_price": "¥ 1.08", "median_price": "¥ 1.08", "volume": "1"}

    def fake_get(url, *, timeout, params):
        calls.append(now[0])
        return Resp(next(codes))
    monkeypatch.setattr(m._http, "get", fake_get)
    monkeypatch.setattr(m, "_throttle", lambda: None)

    with pytest.raises(SteamError, match="限流"):
        m.price(730, "Kilowatt Case")
    assert m.blocked_for == pytest.approx(BLOCK_SEC)
    # 退避期内直接报错，不发请求
    with pytest.raises(SteamError, match="限流中"):
        m.price(730, "Kilowatt Case")
    assert len(calls) == 1
    # 退避到期后再试，又被限流 -> 退避翻倍
    now[0] += BLOCK_SEC
    with pytest.raises(SteamError):
        m.price(730, "Kilowatt Case")
    assert len(calls) == 2 and m.blocked_for == pytest.approx(BLOCK_SEC * 2)
    # 再到期，成功一次就复位
    now[0] += BLOCK_SEC * 2
    assert m.price(730, "Kilowatt Case").lowest == 1.08
    assert m.blocked_for == 0 and m._block_sec == BLOCK_SEC


# ---------- 成交历史（登录后） ----------

def test_parse_history_time():
    assert parse_history_time("Oct 01 2026 01: +0") == 1790816400.0   # 2026-10-01 01:00 UTC
    assert parse_history_time("Jan 02 2026 00: +0") == 1767312000.0


def test_sell_price_from_history_by_volume_share():
    now = 1790900000.0
    pts = [HistoryPoint(now - 5 * 86400, 9.99, 3000),    # 窗口外
           HistoryPoint(now - 2 * 86400, 1.12, 2500),
           HistoryPoint(now - 1 * 86400, 1.15, 10),      # 最高价只成交了 10 件：挂这个价要排队
           HistoryPoint(now - 3600, 1.06, 3100),
           HistoryPoint(now - 7200, 1.15, 0)]            # 没成交量的不算
    # 总量 5610，30% = 1683：1.15 只累计 10，1.12 累计 2510 -> 挂 1.12
    s = sell_price_from_history(pts, 3, now)
    assert (s.price, s.volume, s.total, s.high, s.share) == (1.12, 2510, 5610, 1.15, 0.3)
    assert sell_price_from_history(pts, 3, now, share=0.001).price == 1.15   # 比例极小 = 最高价
    assert sell_price_from_history(pts, 3, now, share=1.0).price == 1.06     # 全部成交量 = 窗口内最低
    assert sell_price_from_history(pts, 0.5, now).price == 1.06              # 窗口只剩最近 12 小时
    assert sell_price_from_history([], 3, now) is None


def test_price_history_requires_login_and_parses(monkeypatch):
    m = SteamMarket()
    monkeypatch.setattr(m, "_throttle", lambda: None)
    with pytest.raises(SteamLoginRequired):
        m.price_history(730, "Kilowatt Case")

    seen = {}

    class Resp:
        def __init__(self, code, body):
            self.status_code, self._body = code, body

        def json(self):
            return self._body

    replies = iter([Resp(200, {"success": True, "price_prefix": "¥ ", "prices": [
        ["Oct 01 2026 01: +0", 1.08, "3514"], ["Oct 01 2026 02: +0", "1.1", "12"], ["bad", 1, 1]]}),
        Resp(200, {"success": True, "price_prefix": "$", "prices": [["Oct 01 2026 01: +0", 0.18, "900"]]}),
        Resp(400, [])])

    def fake_get(url, *, timeout, params):
        seen["cookie"] = m._auth_http.cookies.get("steamLoginSecure", domain="steamcommunity.com")
        seen["params"] = params
        return next(replies)
    monkeypatch.setattr(m._auth_http, "get", fake_get)
    m.set_login("7656%7C%7Ctoken")
    hist = m.price_history(730, "Kilowatt Case")
    assert seen["cookie"] == "7656%7C%7Ctoken" and seen["params"]["market_hash_name"] == "Kilowatt Case"
    assert [(p.price, p.volume) for p in hist.points] == [(1.08, 3514), (1.1, 12)]
    assert hist.prefix == "¥ " and not hist.usd
    # 美元区账号：前缀是 $
    assert m.price_history(730, "Kilowatt Case").usd
    # 行情那个会话始终不带登录 cookie
    assert m._http.cookies.get("steamLoginSecure", domain="steamcommunity.com") is None
    # 登录态失效：Steam 回 400 + []
    with pytest.raises(SteamLoginRequired, match="失效"):
        m.price_history(730, "Kilowatt Case")
    m.set_login(None)
    assert m._auth_http.cookies.get("steamLoginSecure", domain="steamcommunity.com") is None and not m.logged_in
