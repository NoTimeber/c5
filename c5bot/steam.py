"""Steam 社区市场价格，以及“C5 买、Steam 卖”能拿到几折余额的换算。"""
from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import requests

from . import __version__

log = logging.getLogger(__name__)

PRICE_OVERVIEW = "https://steamcommunity.com/market/priceoverview/"
# UA 有两种模式：
#   browser（默认）：真实浏览器 UA，并带上浏览器发市场 XHR 时的那套配套头（Accept、Accept-Language、Referer 指向饰品市场页、
#                   X-Requested-With、Sec-Fetch-*、sec-ch-ua）。只换 UA 不带这些头，Steam 一眼认出是假的，直接 429。
#   bot：老实报名字 c5bot/版本 + 随机码。实测 Steam 对裸 "Mozilla/..." 和 "python-requests/..." 回 429，这种反而放行过。
# 两种模式被 429 都会换一个 UA 再试（Steam 会按 UA 字符串记账）。
USER_AGENT = f"c5bot/{__version__} (+https://github.com/NoTimeber/c5)"
UA_MODES = ("browser", "bot")
# (UA, sec-ch-ua, sec-ch-ua-platform)；Firefox 不发 sec-ch-ua
BROWSER_AGENTS = (
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
     '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"', '"Windows"'),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
     '"Microsoft Edge";v="131", "Chromium";v="131", "Not_A Brand";v="24"', '"Windows"'),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
     '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"', '"macOS"'),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0", None, None),
)
BROWSER_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}
_CH_HEADERS = ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform")


def random_user_agent() -> str:
    return f"{USER_AGENT} {secrets.token_hex(4)}"


def listing_url(app_id: int, name: str) -> str:
    """饰品的市场页地址，浏览器模式下作 Referer。"""
    return f"https://steamcommunity.com/market/listings/{app_id}/{quote(name, safe='')}"
PRICE_HISTORY = "https://steamcommunity.com/market/pricehistory/"   # 要登录；币种跟登录账号的钱包走
USD = 1                 # Steam 币种编号：1 美元，23 人民币
STEAM_FEE_PCT = 5       # Steam 交易手续费 5%
GAME_FEE_PCT = 10       # CS2 游戏手续费 10%
MIN_INTERVAL = 3.0      # 未登录状态大约每分钟 20 次，再快就 429
MIN_INTERVAL_PROXY = 1.0  # 走轮转代理时每个请求换出口 IP，按 IP 的限流不再是瓶颈
BLOCK_SEC = 300.0       # 被 429 后多久内不再请求 Steam；连续被限流退避翻倍
BLOCK_SEC_PROXY = 30.0  # 轮转代理下 429 只是某个出口 IP 被限，退避短一点
BLOCK_MAX_SEC = 3600.0
PROXY_RETRIES = 3       # 走轮转代理时 429 换一个出口 IP 再试，最多几次


class SteamError(Exception):
    pass


class SteamLoginRequired(SteamError):
    """接口要登录，而当前没有登录态或登录态失效。"""


class SteamRateLimited(SteamError):
    """被 Steam 429 / 403。成交历史请求不触发全局退避，由账号池让这个账号歇一会儿。"""


@dataclass(frozen=True)
class SteamPrice:
    lowest: float | None    # 当前最低挂单价
    median: float | None    # 近期成交中位价
    volume: int | None      # 24 小时成交量


@dataclass(frozen=True)
class HistoryPoint:
    ts: float       # UTC 时间戳；最近一个月按小时，更早按天
    price: float    # 这一小时成交的中位价，币种是登录账号的钱包币种
    volume: int


@dataclass(frozen=True)
class PriceHistory:
    points: list[HistoryPoint]
    prefix: str = ""    # 价格前缀，"$" 美元、"¥ " 人民币：币种跟登录账号的钱包区走，不跟查询参数

    @property
    def usd(self) -> bool:
        return self.prefix.strip() == "$"


@dataclass(frozen=True)
class MarketPage:
    """饰品市场页（HTML）里嵌着的数据。页面不像接口那样被按 IP 封，而且不用登录。"""
    currency: int | None                    # 页面价格的币种编号（1 美元 / 23 人民币），来自成交历史的 ecurrency
    lowest: float | None                    # 当前最低挂单价（卖单表第一行）
    history: list[HistoryPoint]             # 小时成交中位价和成交量（和 pricehistory 接口一样的数据）
    sell_orders: list[tuple[float, int]]    # (价格, 这一档的在售件数)，价格升序，只有精确档位；挂某个价时前面排的 = 该价及以下各档之和
    buy_orders: list[tuple[float, int]]     # (价格, 这一档的求购件数)，价格降序，只有精确档位
    sell_more: tuple[float, int] | None = None   # 表最后一行“x 或更高”的合计桶：(x, 件数)
    buy_less: tuple[float, int] | None = None    # 买单表最后一行“x 或更低”的合计桶


@dataclass(frozen=True)
class SellPrice:
    price: float    # 挂单价：窗口内从高价往下累计成交量，累计到总量 share 比例时的那个小时中位价
    volume: int     # 窗口内在这个价或更高价成交了多少件
    total: int      # 窗口内总成交量
    high: float     # 窗口内最高的小时中位价（参考）
    share: float    # 用的比例


def parse_money(text) -> float | None:
    """'¥ 1,234.56' -> 1234.56。只处理用点作小数点的货币。"""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(text or ""))
    return float(m.group().replace(",", "")) if m else None


def parse_history_time(text: str) -> float:
    """成交历史的时间格式 'Oct 01 2026 01: +0'（UTC）-> 时间戳。"""
    head = text.split(":")[0].strip()
    return datetime.strptime(head, "%b %d %Y %H").replace(tzinfo=timezone.utc).timestamp()


_PAGE_HISTORY_RE = re.compile(r'"time":(\d+),"price_median":([\d.]+),"purchases":(\d+)')
_PAGE_CURRENCY_RE = re.compile(r'"ecurrency":(\d+),"prices":\[')
_PAGE_ORDER_ROW_RE = re.compile(r'<td><span class="[^"]*">([^<]{1,24})</span></td><td><span class="[^"]*">([\d,]+)</span></td>')


def parse_market_page(html: str) -> MarketPage:
    """从饰品市场页的 HTML 里解析：成交历史（页面状态里的 JSON，字符串被反斜杠转义过）、
    卖单 / 买单深度表（两张 价格|数量 的表，卖单价格升序、买单价格降序，数量都是累计）。"""
    txt = html.replace('\\\\"', '"').replace('\\"', '"')
    m = _PAGE_CURRENCY_RE.search(txt)
    currency = int(m.group(1)) if m else None
    seen: dict[int, HistoryPoint] = {}
    for t, p, v in _PAGE_HISTORY_RE.findall(txt):
        # 页面里的中位价是带浮点噪声的（0.3799999952…），和接口一样保留到 3 位小数
        seen[int(t)] = HistoryPoint(float(int(t)), round(float(p), 3), int(v))
    history = [seen[k] for k in sorted(seen)]
    # 两张表的行：(价格, 件数, 是否合计桶)。页面固定英文，合计桶写成 "$0.21 or more" / "$0.10 or less"
    pairs: list[tuple[float, int, bool]] = []
    for ptxt, ctxt in _PAGE_ORDER_ROW_RE.findall(txt):
        price = parse_money(ptxt)
        if price is not None:
            pairs.append((price, int(ctxt.replace(",", "")), bool(re.search(r"or (more|higher|less|lower)", ptxt))))
    runs: list[list[tuple[float, int, bool]]] = []
    for row in pairs:
        if not runs or len(runs[-1]) == 1:
            (runs[-1] if runs else runs.append([]) or runs[-1]).append(row)
            continue
        run = runs[-1]
        d_prev, d_new = run[-1][0] - run[-2][0], row[0] - run[-1][0]
        if d_prev == 0 or d_new == 0 or (d_prev > 0) == (d_new > 0):
            run.append(row)
        else:
            runs.append([row])
    sell = next((r for r in runs if len(r) >= 2 and r[-1][0] > r[0][0]), [])
    buy = next((r for r in runs if len(r) >= 2 and r[-1][0] < r[0][0]), [])
    if not sell and pairs and not buy:
        sell = pairs[:1]
    sell_more = next(((p, c) for p, c, bucket in sell if bucket), None)
    buy_less = next(((p, c) for p, c, bucket in buy if bucket), None)
    sell_exact = [(p, c) for p, c, bucket in sell if not bucket]
    buy_exact = [(p, c) for p, c, bucket in buy if not bucket]
    return MarketPage(currency=currency, lowest=sell_exact[0][0] if sell_exact else None, history=history,
                      sell_orders=sell_exact, buy_orders=buy_exact, sell_more=sell_more, buy_less=buy_less)


def seller_receives(price: float) -> float:
    """买家付 price，卖家到手多少。按 Steam 的算法：两项手续费各按分向下取整，最低 0.01。"""
    p = round(price * 100)

    def total(s: int) -> int:
        return s + max(1, s * STEAM_FEE_PCT // 100) + max(1, s * GAME_FEE_PCT // 100)

    s = p * 100 // (100 + STEAM_FEE_PCT + GAME_FEE_PCT)
    while total(s + 1) <= p:
        s += 1
    while s > 0 and total(s) > p:
        s -= 1
    return s / 100


def discount(c5_price: float, steam_price: float | None) -> float | None:
    """花 c5_price 元买入，挂 steam_price 卖掉，1 元 Steam 余额的成本。0.7 = 七折。"""
    if not steam_price or c5_price <= 0:
        return None
    net = seller_receives(steam_price)
    return c5_price / net if net > 0 else None


def sell_price_from_history(points: list[HistoryPoint], days: float, now: float | None = None,
                            share: float = 0.3) -> SellPrice | None:
    """能大批量卖出的挂单价：最近 days 天的小时成交记录按价从高到低累计成交量，累计到总量的 share 比例时的那个价。
    share=0.3 表示最近几天有 30% 的成交是在这个价或更高价成交的：这个价经常到、量也大，挂上去能走。
    只取最高价会排队等价格回来，share 越小越接近最高价、越难卖；share=1 就是窗口内最低的小时中位价。"""
    now = time.time() if now is None else now
    recent = [p for p in points if p.ts >= now - days * 86400 and p.volume > 0 and p.price > 0]
    if not recent:
        return None
    share = min(max(share, 0.001), 1.0)
    total = sum(p.volume for p in recent)
    high = max(p.price for p in recent)
    acc = 0
    for p in sorted(recent, key=lambda p: p.price, reverse=True):
        acc += p.volume
        if acc >= total * share:
            return SellPrice(price=p.price, volume=acc, total=total, high=high, share=share)
    p = recent[-1]
    return SellPrice(price=p.price, volume=total, total=total, high=high, share=share)


def fmt_wait(sec: float) -> str:
    sec = max(1, round(sec))
    return f"{-(-sec // 60)} 分钟" if sec >= 60 else f"{sec} 秒"


PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
IP_ECHO = "https://api.ipify.org"


def check_proxy_url(proxy: str) -> str:
    """看板上填的代理地址：去空白，校验格式。返回规范化后的地址，不合法抛 SteamError。"""
    proxy = proxy.strip()
    u = urlsplit(proxy)
    if u.scheme not in PROXY_SCHEMES or not u.hostname:
        raise SteamError("代理地址格式应为 http://用户名:密码@地址:端口 或 socks5://…")
    try:
        u.port
    except ValueError:
        raise SteamError("代理端口不对") from None
    return proxy


def mask_proxy(proxy: str | None) -> str | None:
    """显示用：把密码换成 ***。"""
    if not proxy:
        return None
    u = urlsplit(proxy)
    if u.password is None:
        return proxy
    auth = f"{u.username}:***@"
    return f"{u.scheme}://{auth}{u.hostname}{':' + str(u.port) if u.port else ''}{u.path}"


def probe_proxy(proxy: str, timeout: float = 15.0) -> str:
    """通过代理访问一次 IP 回显服务，返回出口 IP；连不通抛 SteamError。"""
    try:
        r = requests.get(IP_ECHO, proxies={"http": proxy, "https": proxy}, timeout=timeout,
                         headers={"User-Agent": USER_AGENT})
    except requests.RequestException as e:
        raise SteamError(f"代理连不通: {type(e).__name__}") from None
    if r.status_code != 200 or not r.text.strip():
        raise SteamError(f"代理测试失败: HTTP {r.status_code}")
    return r.text.strip()


class SteamMarket:
    def __init__(self, *, proxy: str | None = None, currency: int = 23, timeout: float = 10.0,
                 clock=time.monotonic, user_agent: str | None = None):
        self.proxy: str | None = proxy or None
        # user_agent：None / "browser" 浏览器模式；"bot" 老实报名字；其它字符串 = 固定用这个 UA 不轮换
        mode = (user_agent or "browser").strip()
        self.ua_mode = mode if mode in UA_MODES else "fixed"
        self._ua_entry: tuple = (mode, None, None)   # (UA, sec-ch-ua, sec-ch-ua-platform)
        self.user_agent = mode
        if self.ua_mode != "fixed":
            self._pick_user_agent()
        # 匿名会话查行情、汇率；每个 Steam 账号一个带登录 cookie 的会话查成交历史。登录 cookie 只发给必须登录的接口，
        # 实测带着 cookie 查行情会被 Steam 按账号限流，匿名反而没事
        self._http = self._new_session()
        self._page_http = self._new_session(page=True)   # 抓市场页用：导航型请求头
        self._login_http: dict[str, requests.Session] = {}
        self._currency = currency
        self._timeout = timeout
        self._now = clock
        self._last = 0.0
        self._blocked_until = 0.0       # 被限流后这个时间之前不再请求
        self.set_proxy(proxy)

    def _pick_user_agent(self) -> None:
        """选一个和当前不同的 UA。browser 模式从真实浏览器 UA 里挑，bot 模式换随机码。"""
        if self.ua_mode == "bot":
            self._ua_entry = (random_user_agent(), None, None)
        else:
            choices = [e for e in BROWSER_AGENTS if e[0] != self.user_agent] or list(BROWSER_AGENTS)
            self._ua_entry = secrets.choice(choices)
        self.user_agent = self._ua_entry[0]

    def rotate_user_agent(self) -> None:
        """被 429 后换一个 UA：Steam 按 UA 字符串记账的话，等于换了个身份。.env 指定了固定 UA 时不动。"""
        if self.ua_mode == "fixed":
            return
        self._pick_user_agent()
        for s in (self._http, *self._login_http.values()):
            self._apply_identity(s)
        self._apply_identity(self._page_http, page=True)

    def _apply_identity(self, s: requests.Session, page: bool = False) -> None:
        """UA 和配套请求头。浏览器样子的 UA 要配浏览器的头，否则一眼假。page=True 是打开网页的那套头，不是 XHR。"""
        ua, ch_ua, ch_platform = self._ua_entry
        s.headers["User-Agent"] = ua
        for k in (*BROWSER_HEADERS, *_CH_HEADERS, "Upgrade-Insecure-Requests"):
            s.headers.pop(k, None)
        if ua.startswith("Mozilla/"):
            s.headers.update(BROWSER_HEADERS)
            if page:
                s.headers.pop("X-Requested-With", None)
                s.headers.update({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                                  "Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document",
                                  "Upgrade-Insecure-Requests": "1"})
            if ch_ua:
                s.headers.update({"sec-ch-ua": ch_ua, "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": ch_platform})

    def _new_session(self, page: bool = False) -> requests.Session:
        s = requests.Session()
        self._apply_identity(s, page=page)
        if page:    # 页面固定要英文版，解析不受语言影响（价格币种跟地区走，不跟语言）
            s.cookies.set("Steam_Language", "english", domain="steamcommunity.com", path="/")
        # 实测 Accept-Encoding 里只要有 br，Steam 就直接回 429；requests 装了 brotli 后默认会带上
        s.headers["Accept-Encoding"] = "gzip, deflate"
        self._apply_proxy(s)
        return s

    def _apply_proxy(self, s: requests.Session) -> None:
        proxy = self.proxy
        s.proxies = {"http": proxy, "https": proxy} if proxy else {}
        # 轮转代理按“新连接换出口 IP”：长连接复用会让一整轮请求都走同一个出口，所以走代理时每个请求都新建连接
        if proxy:
            s.headers["Connection"] = "close"
        else:
            s.headers.pop("Connection", None)

    def set_proxy(self, proxy: str | None) -> None:
        """切换 / 清除代理（看板上改了代理时调用）。换了出口 IP，限流退避状态一并重置。"""
        self.proxy = proxy or None
        for s in (self._http, self._page_http, *self._login_http.values()):
            self._apply_proxy(s)
        self._interval = MIN_INTERVAL_PROXY if proxy else MIN_INTERVAL
        self._block_base = BLOCK_SEC_PROXY if proxy else BLOCK_SEC
        self._block_sec = self._block_base  # 下次被限流退避多久，连续被限流翻倍
        self._blocked_until = 0.0

    @property
    def blocked_for(self) -> float:
        """还要等多少秒才会再请求 Steam；0 = 没被限流。"""
        return max(0.0, self._blocked_until - self._now())

    @property
    def logins(self) -> list[str]:
        """已经建了会话的账号 steamid。"""
        return list(self._login_http)

    def drop_login(self, steamid: str | None = None) -> None:
        """账号退出登录：丢掉它的会话；不传 steamid 全部丢掉。"""
        if steamid is None:
            self._login_http.clear()
        else:
            self._login_http.pop(steamid, None)

    def _get(self, url: str, params: dict, *, http: requests.Session | None = None,
             block: bool = True, referer: str | None = None) -> requests.Response:
        """带限流退避的 GET。被 Steam 限流（429 / 403）后抛 SteamRateLimited；block=True 时还会在 BLOCK_SEC 内
        拒绝后续请求（连续被限流退避翻倍），被限流后继续请求只会把封锁拖长。成交历史请求 block=False，
        由账号池让那个账号歇一会儿，不影响别的账号和行情。"""
        wait = self.blocked_for
        if wait > 0:
            raise SteamError(f"Steam 限流中，{fmt_wait(wait)}后再试")
        http = http or self._http
        if referer and self.user_agent.startswith("Mozilla/"):
            http.headers["Referer"] = referer       # 浏览器是从饰品市场页发的 XHR
        else:
            http.headers.pop("Referer", None)
        attempts = PROXY_RETRIES if self.proxy else 1   # 轮转代理：429 多半只是这个出口 IP 被限，换一个再试
        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                resp = http.get(url, timeout=self._timeout, params=params)
            except requests.RequestException as e:
                raise SteamError(f"Steam 网络错误: {type(e).__name__}") from None
            if resp.status_code not in (429, 403):
                self._block_sec = self._block_base  # 正常拿到响应就把退避复位
                return resp
            self.rotate_user_agent()
            if attempt < attempts:
                log.info("Steam %s，换 UA 和代理出口重试（%d/%d）", resp.status_code, attempt, attempts)
        if not block:
            raise SteamRateLimited(f"Steam 限流（{resp.status_code}）")
        sec = self._block_sec
        self._blocked_until = self._now() + sec
        self._block_sec = min(sec * 2, BLOCK_MAX_SEC)
        raise SteamRateLimited(f"Steam 限流（{resp.status_code}），{fmt_wait(sec)}内不再请求 Steam")

    def price(self, app_id: int, name: str, *, currency: int | None = None) -> SteamPrice:
        """当前最低挂单价等。currency 不传用初始化时的币种（默认人民币），传 USD 查美元价。"""
        resp = self._get(PRICE_OVERVIEW, {
            "appid": app_id, "currency": self._currency if currency is None else currency,
            "market_hash_name": name}, referer=listing_url(app_id, name))
        try:
            data = resp.json()
        except ValueError:
            raise SteamError(f"Steam HTTP {resp.status_code}") from None
        if not isinstance(data, dict) or not data.get("success"):
            raise SteamError(f"Steam 市场没有 {name}")
        volume = parse_money(data.get("volume"))
        return SteamPrice(lowest=parse_money(data.get("lowest_price")),
                          median=parse_money(data.get("median_price")),
                          volume=int(volume) if volume else None)

    def market_page(self, app_id: int, name: str) -> MarketPage:
        """抓饰品的市场页，解析最低价、成交历史、买卖单深度。匿名、不走被封的接口。"""
        resp = self._get(listing_url(app_id, name), {}, http=self._page_http)
        if resp.status_code != 200:
            raise SteamError(f"Steam 市场页 HTTP {resp.status_code}")
        page = parse_market_page(resp.text)
        if page.lowest is None and not page.history:
            raise SteamError(f"Steam 市场页里没解析到 {name} 的价格数据（页面结构可能变了）")
        return page

    def price_history(self, app_id: int, name: str, *, steamid: str, cookie: str) -> PriceHistory:
        """成交历史（市场页那张图的数据）。用指定账号的登录 cookie 查；登录态失效时 Steam 回 400 + 空数组。
        每个账号一个会话（各自的 cookie 罐），被 429 不触发全局退避，交给账号池处理。"""
        http = self._login_http.get(steamid)
        if http is None:
            http = self._login_http[steamid] = self._new_session()
        http.cookies.set("steamLoginSecure", cookie, domain="steamcommunity.com", path="/")
        resp = self._get(PRICE_HISTORY, {"appid": app_id, "market_hash_name": name}, http=http, block=False,
                         referer=listing_url(app_id, name))
        try:
            data = resp.json()
        except ValueError:
            raise SteamError(f"Steam HTTP {resp.status_code}") from None
        if resp.status_code == 400 or not isinstance(data, dict) or not data.get("success"):
            if resp.status_code in (400, 401) or not isinstance(data, dict):
                raise SteamLoginRequired("Steam 登录态失效，请在看板上重新登录")
            raise SteamError(f"Steam 没有 {name} 的成交历史")
        points = []
        for row in data.get("prices") or []:
            try:
                when, price, volume = row
                points.append(HistoryPoint(parse_history_time(str(when)), float(price), int(float(volume))))
            except (ValueError, TypeError):
                continue
        return PriceHistory(points, str(data.get("price_prefix") or ""))

    def _throttle(self) -> None:
        wait = self._last + self._interval - self._now()
        if wait > 0:
            time.sleep(wait)
        self._last = self._now()
