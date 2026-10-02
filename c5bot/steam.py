"""Steam 社区市场价格，以及“C5 买、Steam 卖”能拿到几折余额的换算。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

import requests

PRICE_OVERVIEW = "https://steamcommunity.com/market/priceoverview/"
USD = 1                 # Steam 币种编号：1 美元，23 人民币
STEAM_FEE_PCT = 5       # Steam 交易手续费 5%
GAME_FEE_PCT = 10       # CS2 游戏手续费 10%
MIN_INTERVAL = 3.0      # 未登录状态大约每分钟 20 次，再快就 429
BLOCK_SEC = 300.0       # 被 429 后多久内不再请求 Steam；连续被限流退避翻倍
BLOCK_MAX_SEC = 3600.0


class SteamError(Exception):
    pass


@dataclass(frozen=True)
class SteamPrice:
    lowest: float | None    # 当前最低挂单价
    median: float | None    # 近期成交中位价
    volume: int | None      # 24 小时成交量


def parse_money(text) -> float | None:
    """'¥ 1,234.56' -> 1234.56。只处理用点作小数点的货币。"""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(text or ""))
    return float(m.group().replace(",", "")) if m else None


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


def fmt_wait(sec: float) -> str:
    sec = max(1, round(sec))
    return f"{-(-sec // 60)} 分钟" if sec >= 60 else f"{sec} 秒"


class SteamMarket:
    def __init__(self, *, proxy: str | None = None, currency: int = 23, timeout: float = 10.0,
                 clock=time.monotonic):
        self._http = requests.Session()
        self._http.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) c5bot"
        # 实测 Accept-Encoding 里只要有 br，Steam 就直接回 429；requests 装了 brotli 后默认会带上
        self._http.headers["Accept-Encoding"] = "gzip, deflate"
        if proxy:
            self._http.proxies = {"http": proxy, "https": proxy}
        self._currency = currency
        self._timeout = timeout
        self._now = clock
        self._last = 0.0
        self._blocked_until = 0.0       # 被限流后这个时间之前不再请求
        self._block_sec = BLOCK_SEC     # 下次被限流退避多久，连续被限流翻倍

    @property
    def blocked_for(self) -> float:
        """还要等多少秒才会再请求 Steam；0 = 没被限流。"""
        return max(0.0, self._blocked_until - self._now())

    def price(self, app_id: int, name: str, *, currency: int | None = None) -> SteamPrice:
        """当前最低挂单价等。currency 不传用初始化时的币种（默认人民币），传 USD 查美元价。
        被 Steam 限流（429 / 403）后 BLOCK_SEC 内直接抛 SteamError、不发请求；连续被限流退避时间翻倍。
        被限流后继续请求只会把封锁拖长，所以不做逐个重试。"""
        wait = self.blocked_for
        if wait > 0:
            raise SteamError(f"Steam 限流中，{fmt_wait(wait)}后再试")
        self._throttle()
        try:
            resp = self._http.get(PRICE_OVERVIEW, timeout=self._timeout, params={
                "appid": app_id, "currency": self._currency if currency is None else currency,
                "market_hash_name": name})
        except requests.RequestException as e:
            raise SteamError(f"Steam 网络错误: {type(e).__name__}") from None
        if resp.status_code in (429, 403):
            block = self._block_sec
            self._blocked_until = self._now() + block
            self._block_sec = min(block * 2, BLOCK_MAX_SEC)
            raise SteamError(f"Steam 限流（{resp.status_code}），{fmt_wait(block)}内不再请求 Steam")
        self._block_sec = BLOCK_SEC  # 正常拿到响应就把退避复位
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

    def _throttle(self) -> None:
        wait = self._last + MIN_INTERVAL - self._now()
        if wait > 0:
            time.sleep(wait)
        self._last = self._now()
