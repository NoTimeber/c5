from __future__ import annotations

import pytest

from c5bot import __version__
from c5bot.steam import (
    BLOCK_SEC,
    BLOCK_SEC_PROXY,
    MIN_INTERVAL,
    MIN_INTERVAL_PROXY,
    USD,
    USER_AGENT,
    HistoryPoint,
    SteamError,
    SteamLoginRequired,
    SteamMarket,
    SteamRateLimited,
    check_proxy_url,
    discount,
    fmt_wait,
    mask_proxy,
    parse_history_time,
    parse_market_page,
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
    assert via._new_session().proxies == via._http.proxies      # 后建的账号会话也带代理
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
        calls.append(m._http.headers["User-Agent"])
        return Resp(next(codes))
    monkeypatch.setattr(m._http, "get", fake_get)
    # 两次 429 后第三次成功：不退避；每次 429 之后 UA 都换了一个
    assert m.price(730, "Kilowatt Case").lowest == 0.16
    assert len(calls) == 3 and m.blocked_for == 0
    assert calls[0] != calls[1] and calls[1] != calls[2] and all(c.startswith("Mozilla/") for c in calls)
    # 三次都 429 才退避（代理模式 30 秒）
    with pytest.raises(SteamError, match="限流"):
        m.price(730, "Kilowatt Case")
    assert len(calls) == 6 and m.blocked_for == pytest.approx(BLOCK_SEC_PROXY)
    m.set_proxy(None)
    assert "Connection" not in m._http.headers


def test_browser_user_agent_mode_is_default(monkeypatch):
    m = SteamMarket()
    assert m.ua_mode == "browser" and m.user_agent.startswith("Mozilla/")
    h = m._http.headers
    assert h["User-Agent"] == m.user_agent and h["X-Requested-With"] == "XMLHttpRequest"
    assert h["Sec-Fetch-Mode"] == "cors" and h["Accept"] == "*/*" and "br" not in h["Accept-Encoding"]
    if "Firefox" in m.user_agent:
        assert "sec-ch-ua" not in h
    else:
        assert "Chrom" in h["sec-ch-ua"] or "Edge" in h["sec-ch-ua"]
    # 请求时 Referer 指向饰品的市场页
    seen = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"success": True, "lowest_price": "$0.16", "median_price": "$0.16", "volume": "1"}

    def fake_get(url, *, timeout, params):
        seen["referer"] = m._http.headers.get("Referer")
        return Resp()
    monkeypatch.setattr(m._http, "get", fake_get)
    monkeypatch.setattr(m, "_throttle", lambda: None)
    m.price(730, "AK-47 | Redline (Field-Tested)")
    assert seen["referer"] == "https://steamcommunity.com/market/listings/730/AK-47%20%7C%20Redline%20%28Field-Tested%29"
    # 轮换：换成另一个浏览器 UA，账号会话也跟着换
    http = m._login_http["x"] = m._new_session()
    before = m.user_agent
    m.rotate_user_agent()
    assert m.user_agent != before and m.user_agent.startswith("Mozilla/") and http.headers["User-Agent"] == m.user_agent


def test_bot_and_fixed_user_agent_modes():
    a, b = SteamMarket(user_agent="bot"), SteamMarket(user_agent="bot")
    assert a.ua_mode == "bot" and a.user_agent != b.user_agent and a.user_agent.startswith(USER_AGENT + " ")
    assert "X-Requested-With" not in a._http.headers and "sec-ch-ua" not in a._http.headers
    before = a.user_agent
    a.rotate_user_agent()
    assert a.user_agent != before and a._http.headers["User-Agent"] == a.user_agent
    fixed = SteamMarket(user_agent="curl/8.9.0")
    fixed.rotate_user_agent()
    assert fixed.ua_mode == "fixed" and fixed._http.headers["User-Agent"] == "curl/8.9.0"
    custom = SteamMarket(user_agent="Mozilla/5.0 (X11; Linux x86_64) Firefox/120.0")
    assert custom._http.headers["Sec-Fetch-Mode"] == "cors"      # 自定义的浏览器样子 UA 也配浏览器头


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


def test_price_history_per_account_sessions(monkeypatch):
    m = SteamMarket()
    monkeypatch.setattr(m, "_throttle", lambda: None)
    http = m._login_http["7656"] = m._new_session()
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
        seen["cookie"] = http.cookies.get("steamLoginSecure", domain="steamcommunity.com")
        seen["params"] = params
        return next(replies)
    monkeypatch.setattr(http, "get", fake_get)
    hist = m.price_history(730, "Kilowatt Case", steamid="7656", cookie="7656%7C%7Ctoken")
    assert seen["cookie"] == "7656%7C%7Ctoken" and seen["params"]["market_hash_name"] == "Kilowatt Case"
    assert [(p.price, p.volume) for p in hist.points] == [(1.08, 3514), (1.1, 12)]
    assert hist.prefix == "¥ " and not hist.usd
    # 美元区账号：前缀是 $
    assert m.price_history(730, "Kilowatt Case", steamid="7656", cookie="7656%7C%7Ctoken").usd
    # 行情那个会话始终不带登录 cookie；每个账号各自的会话
    assert m._http.cookies.get("steamLoginSecure", domain="steamcommunity.com") is None
    assert m.logins == ["7656"]
    # 登录态失效：Steam 回 400 + []
    with pytest.raises(SteamLoginRequired, match="失效"):
        m.price_history(730, "Kilowatt Case", steamid="7656", cookie="7656%7C%7Ctoken")
    m.drop_login("7656")
    assert m.logins == []


PAGE_HTML = (
    '<html><script>window.__state = "{\\"queries\\":[{\\"state\\":{\\"data\\":{\\"ecurrency\\":1,\\"prices\\":['
    '{\\"time\\":1759400000,\\"price_median\\":0.17,\\"purchases\\":5783},'
    '{\\"time\\":1759403600,\\"price_median\\":0.16,\\"purchases\\":2734},'
    '{\\"time\\":1759403600,\\"price_median\\":0.16,\\"purchases\\":2734}]}}}]}"</script>'
    '<h2>Sell orders</h2><table><tr><td><span class="x1">$0.16</span></td><td><span class="x1">4,990</span></td></tr>'
    '<tr><td><span class="x1">$0.17</span></td><td><span class="x1">27,130</span></td></tr>'
    '<tr><td><span class="x1">$0.18</span></td><td><span class="x1">35,243</span></td></tr>'
    '<tr><td><span class="x1">$0.19 or more</span></td><td><span class="x1">651,155</span></td></tr></table>'
    '<h2>Buy orders</h2><table><tr><td><span class="x1">$0.15</span></td><td><span class="x1">1,200</span></td></tr>'
    '<tr><td><span class="x1">$0.14</span></td><td><span class="x1">9,000</span></td></tr>'
    '<tr><td><span class="x1">$0.13 or less</span></td><td><span class="x1">1,796,424</span></td></tr></table></html>'
)


def test_parse_market_page():
    page = parse_market_page(PAGE_HTML)
    assert page.currency == USD and page.lowest == 0.16
    assert [(p.ts, p.price, p.volume) for p in page.history] == [(1759400000.0, 0.17, 5783), (1759403600.0, 0.16, 2734)]  # 去重、按时间排
    assert page.sell_orders == [(0.16, 4990), (0.17, 27130), (0.18, 35243)] and page.sell_more == (0.19, 651155)
    assert page.buy_orders == [(0.15, 1200), (0.14, 9000)] and page.buy_less == (0.13, 1796424)
    empty = parse_market_page("<html>nothing</html>")
    assert empty.lowest is None and empty.history == [] and empty.currency is None


def test_market_page_uses_navigation_session(monkeypatch):
    m = SteamMarket()
    monkeypatch.setattr(m, "_throttle", lambda: None)
    h = m._page_http.headers
    assert h["Sec-Fetch-Mode"] == "navigate" and h["Accept"].startswith("text/html") and "X-Requested-With" not in h
    assert m._http.headers["Sec-Fetch-Mode"] == "cors"      # XHR 那套没被改
    seen = {}

    class Resp:
        status_code = 200
        text = PAGE_HTML

    def fake_get(url, *, timeout, params):
        seen["url"] = url
        seen["referer"] = m._page_http.headers.get("Referer")
        return Resp()
    monkeypatch.setattr(m._page_http, "get", fake_get)
    page = m.market_page(730, "Kilowatt Case")
    assert seen["url"] == "https://steamcommunity.com/market/listings/730/Kilowatt%20Case" and seen["referer"] is None
    assert page.lowest == 0.16 and len(page.history) == 2
    assert m._page_http.cookies.get("Steam_Language", domain="steamcommunity.com") == "english"

    class Empty:
        status_code = 200
        text = "<html>changed</html>"
    monkeypatch.setattr(m._page_http, "get", lambda url, *, timeout, params: Empty())
    with pytest.raises(SteamError, match="页面结构"):
        m.market_page(730, "Kilowatt Case")
    m.set_proxy("http://u:p@proxy.example.com:1337")
    assert m._page_http.proxies["https"] == "http://u:p@proxy.example.com:1337"   # 抓页面也走代理


def test_history_429_does_not_block_globally(monkeypatch):
    m = SteamMarket()
    monkeypatch.setattr(m, "_throttle", lambda: None)
    http = m._login_http["a"] = m._new_session()

    class Resp:
        status_code = 429

        def json(self):
            return None
    monkeypatch.setattr(http, "get", lambda url, *, timeout, params: Resp())
    with pytest.raises(SteamRateLimited):
        m.price_history(730, "x", steamid="a", cookie="c")
    assert m.blocked_for == 0        # 由账号池让这个账号歇着，行情和别的账号不受影响
