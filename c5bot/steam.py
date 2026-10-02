"""Steam 社区市场价格，以及“C5 买、Steam 卖”能拿到几折余额的换算。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from . import __version__

PRICE_OVERVIEW = "https://steamcommunity.com/market/priceoverview/"
# 实测 Steam 对 "Mozilla/..." 和 "python-requests/..." 这类 UA 直接回 429（不看 IP），老老实实报自己的名字反而放行
USER_AGENT = f"c5bot/{__version__} (+https://github.com/NoTimeber/c5)"
PRICE_HISTORY = "https://steamcommunity.com/market/pricehistory/"   # 要登录；币种跟登录账号的钱包走
USD = 1                 # Steam 币种编号：1 美元，23 人民币
STEAM_FEE_PCT = 5       # Steam 交易手续费 5%
GAME_FEE_PCT = 10       # CS2 游戏手续费 10%
MIN_INTERVAL = 3.0      # 未登录状态大约每分钟 20 次，再快就 429
MIN_INTERVAL_PROXY = 1.0  # 走轮转代理时每个请求换出口 IP，按 IP 的限流不再是瓶颈
BLOCK_SEC = 300.0       # 被 429 后多久内不再请求 Steam；连续被限流退避翻倍
BLOCK_SEC_PROXY = 30.0  # 轮转代理下 429 只是某个出口 IP 被限，退避短一点
BLOCK_MAX_SEC = 3600.0


class SteamError(Exception):
    pass


class SteamLoginRequired(SteamError):
    """接口要登录，而当前没有登录态或登录态失效。"""


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


class SteamMarket:
    def __init__(self, *, proxy: str | None = None, currency: int = 23, timeout: float = 10.0,
                 clock=time.monotonic):
        # 两个会话：行情用匿名的，成交历史用带登录 cookie 的。登录 cookie 只发给必须登录的接口，
        # 实测带着 cookie 查行情会被 Steam 按账号限流，匿名反而没事
        self._http = self._session(proxy)
        self._auth_http = self._session(proxy)
        self._currency = currency
        self._timeout = timeout
        self._now = clock
        self._last = 0.0
        self._interval = MIN_INTERVAL_PROXY if proxy else MIN_INTERVAL
        self._blocked_until = 0.0       # 被限流后这个时间之前不再请求
        self._block_base = BLOCK_SEC_PROXY if proxy else BLOCK_SEC
        self._block_sec = self._block_base  # 下次被限流退避多久，连续被限流翻倍
        self.logged_in = False

    @staticmethod
    def _session(proxy: str | None) -> requests.Session:
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        # 实测 Accept-Encoding 里只要有 br，Steam 就直接回 429；requests 装了 brotli 后默认会带上
        s.headers["Accept-Encoding"] = "gzip, deflate"
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        return s

    @property
    def blocked_for(self) -> float:
        """还要等多少秒才会再请求 Steam；0 = 没被限流。"""
        return max(0.0, self._blocked_until - self._now())

    def set_login(self, cookie: str | None) -> None:
        """设置 / 清除 steamLoginSecure（看板登录后由 Dashboard 调用）。只给成交历史那个会话用。"""
        try:
            self._auth_http.cookies.clear(domain="steamcommunity.com", path="/", name="steamLoginSecure")
        except KeyError:
            pass
        if cookie:
            self._auth_http.cookies.set("steamLoginSecure", cookie, domain="steamcommunity.com", path="/")
        self.logged_in = bool(cookie)

    def _get(self, url: str, params: dict, *, http: requests.Session | None = None) -> requests.Response:
        """带限流退避的 GET。被 Steam 限流（429 / 403）后 BLOCK_SEC 内直接抛 SteamError、不发请求；
        连续被限流退避时间翻倍。被限流后继续请求只会把封锁拖长，所以不做逐个重试。"""
        wait = self.blocked_for
        if wait > 0:
            raise SteamError(f"Steam 限流中，{fmt_wait(wait)}后再试")
        self._throttle()
        try:
            resp = (http or self._http).get(url, timeout=self._timeout, params=params)
        except requests.RequestException as e:
            raise SteamError(f"Steam 网络错误: {type(e).__name__}") from None
        if resp.status_code in (429, 403):
            block = self._block_sec
            self._blocked_until = self._now() + block
            self._block_sec = min(block * 2, BLOCK_MAX_SEC)
            raise SteamError(f"Steam 限流（{resp.status_code}），{fmt_wait(block)}内不再请求 Steam")
        self._block_sec = self._block_base  # 正常拿到响应就把退避复位
        return resp

    def price(self, app_id: int, name: str, *, currency: int | None = None) -> SteamPrice:
        """当前最低挂单价等。currency 不传用初始化时的币种（默认人民币），传 USD 查美元价。"""
        resp = self._get(PRICE_OVERVIEW, {
            "appid": app_id, "currency": self._currency if currency is None else currency,
            "market_hash_name": name})
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

    def price_history(self, app_id: int, name: str) -> PriceHistory:
        """成交历史（市场页那张图的数据）。要登录；没登录或登录态失效时 Steam 回 400 + 空数组。"""
        if not self.logged_in:
            raise SteamLoginRequired("查成交历史要先在看板上登录 Steam")
        resp = self._get(PRICE_HISTORY, {"appid": app_id, "market_hash_name": name}, http=self._auth_http)
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
