"""C5 价和 Steam 价的对比：compare 命令和看板共用。

两种看法：
- 折：C5 价 ÷ Steam 挂单价扣完手续费的到手价。0.7 = 花 7 元换 10 元 Steam 余额。
- 汇率：1 美元的 Steam 余额花多少人民币。Steam 内部有一个人民币/美元换算率（同一件饰品的人民币价 ÷ 美元价，
  约等于官方汇率），折 × 这个换算率就是你拿到余额的实际汇率。看板上输入目标汇率后，
  折扣 = 目标汇率 ÷ Steam 换算率，每个饰品的目标价 = 净到手 × 折扣，随 Steam 价自动变。
"""
from __future__ import annotations

import csv
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import WatchItem
from .steam import USD, SteamError, SteamMarket, SteamPrice, discount, seller_receives

CSV_FIELDS = ["time", "name", "c5_lowest", "c5_target", "steam_lowest", "steam_net",
              "discount_at_lowest", "discount_at_target", "steam_median", "steam_volume",
              "steam_rate", "rate_at_lowest", "rate_at_target"]

_ITEM_PRICE = object()  # compare_row 的哨兵：目标价用 watchlist 里的 max_price


@dataclass(frozen=True)
class CompareRow:
    name: str
    c5_lowest: float | None
    c5_target: float | None                 # 实际用的目标价；设了目标汇率但 Steam 价还没拿到时为 None
    steam_lowest: float | None
    steam_net: float | None                 # 挂 steam_lowest 卖掉后到手
    discount_at_lowest: float | None        # C5 当前最低价 / 净到手，0.7 = 七折
    discount_at_target: float | None        # 目标价 / 净到手
    steam_median: float | None
    steam_volume: int | None
    steam_rate: float | None = None         # Steam 内部人民币/美元换算率
    rate_at_lowest: float | None = None     # 按 C5 最低价买入，1 美元余额花多少人民币
    rate_at_target: float | None = None     # 按目标价买入


@dataclass(frozen=True)
class SteamRate:
    rate: float     # 1 美元 = rate 人民币（Steam 内部换算）
    name: str       # 按哪件饰品算的
    cny: float      # 这件饰品的人民币最低价
    usd: float      # 美元最低价
    at: float


def c5_lowest(stat: dict | None) -> float | None:
    """行情接口里的在售最低价，没有或不是正数返回 None。"""
    v = (stat or {}).get("sellPrice")
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def steam_rate(cny: float | None, usd: float | None) -> float | None:
    """Steam 内部的人民币 / 美元换算率：同一件饰品的人民币价 ÷ 美元价。"""
    if not cny or not usd or cny <= 0 or usd <= 0:
        return None
    return cny / usd


def rate_discount(target_rate: float | None, rate: float | None) -> float | None:
    """目标汇率换成折扣：想花 target_rate 元换 1 美元余额，Steam 按 rate 换算，折扣 = target_rate / rate。"""
    if not target_rate or not rate or target_rate <= 0 or rate <= 0:
        return None
    return target_rate / rate


def target_price(disc: float | None, steam_lowest: float | None) -> float | None:
    """按折扣算目标价：Steam 最低价扣完手续费的到手 × 折扣，向下取整到分。"""
    if not disc or not steam_lowest or steam_lowest <= 0:
        return None
    v = math.floor(seller_receives(steam_lowest) * disc * 100 + 1e-9) / 100
    return v if v > 0 else None


def fetch_steam_rate(steam: SteamMarket, name: str, app_id: int = 730) -> SteamRate:
    """用一件固定的参照饰品分别查钱包币种价和美元价，算 Steam 的换算率。
    参照饰品要贵：Steam 换算后向上取整到分，几十美元的饰品误差在 0.03% 以内，几毛钱的箱子会差百分之几。
    查询失败抛 SteamError。"""
    cny = steam.price(app_id, name).lowest
    usd = steam.price(app_id, name, currency=USD).lowest
    rate = steam_rate(cny, usd)
    if rate is None:
        raise SteamError(f"{name} 的 Steam 价无效，算不出汇率")
    return SteamRate(rate=rate, name=name, cny=cny, usd=usd, at=time.time())


def compare_row(item: WatchItem, c5: float | None, sp: SteamPrice | None, *,
                target: float | None | object = _ITEM_PRICE, rate: float | None = None) -> CompareRow:
    """target 不传用 watchlist 的 max_price；传 None 表示按汇率算但还没有 Steam 价。"""
    lowest = sp.lowest if sp else None
    tgt = item.max_price if target is _ITEM_PRICE else target
    d_low = discount(c5, lowest) if c5 else None
    d_tgt = discount(tgt, lowest) if tgt else None
    return CompareRow(
        name=item.name, c5_lowest=c5, c5_target=tgt, steam_lowest=lowest,
        steam_net=seller_receives(lowest) if lowest else None,
        discount_at_lowest=d_low, discount_at_target=d_tgt,
        steam_median=sp.median if sp else None,
        steam_volume=sp.volume if sp else None,
        steam_rate=rate,
        rate_at_lowest=d_low * rate if d_low and rate else None,
        rate_at_target=d_tgt * rate if d_tgt and rate else None,
    )


def append_csv(path: Path, when: str, rows: list[CompareRow]) -> None:
    """追加到历史文件，第一次写表头。"""
    if not rows:
        return
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow(CSV_FIELDS)
        for r in rows:
            d = asdict(r)
            w.writerow([when] + [round(d[k], 4) if isinstance(d[k], float) else d[k] for k in CSV_FIELDS[1:]])
