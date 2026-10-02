"""C5 价和 Steam 价的对比：compare 命令和看板共用。"""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import WatchItem
from .steam import SteamPrice, discount, seller_receives

CSV_FIELDS = ["time", "name", "c5_lowest", "c5_target", "steam_lowest", "steam_net",
              "discount_at_lowest", "discount_at_target", "steam_median", "steam_volume"]


@dataclass(frozen=True)
class CompareRow:
    name: str
    c5_lowest: float | None
    c5_target: float
    steam_lowest: float | None
    steam_net: float | None                 # 挂 steam_lowest 卖掉后到手
    discount_at_lowest: float | None        # C5 当前最低价 / 净到手，0.7 = 七折
    discount_at_target: float | None        # 目标价 / 净到手
    steam_median: float | None
    steam_volume: int | None


def c5_lowest(stat: dict | None) -> float | None:
    """行情接口里的在售最低价，没有或不是正数返回 None。"""
    v = (stat or {}).get("sellPrice")
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def compare_row(item: WatchItem, c5: float | None, sp: SteamPrice | None) -> CompareRow:
    lowest = sp.lowest if sp else None
    return CompareRow(
        name=item.name, c5_lowest=c5, c5_target=item.max_price, steam_lowest=lowest,
        steam_net=seller_receives(lowest) if lowest else None,
        discount_at_lowest=discount(c5, lowest) if c5 else None,
        discount_at_target=discount(item.max_price, lowest),
        steam_median=sp.median if sp else None,
        steam_volume=sp.volume if sp else None,
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
