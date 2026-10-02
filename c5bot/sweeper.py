"""监控 + 扫货核心逻辑。"""
from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass

from .client import C5Client, C5Error, C5NetworkError
from .config import Settings, WatchItem
from .store import COUNTED, Purchase, Store, Total

log = logging.getLogger(__name__)

EPS = 1e-6
FAILED_SKIP_SEC = 60.0          # 买失败的在售，这段时间内不再尝试
UNKNOWN_RECHECK_SEC = 5.0       # 结果未知的单多久查一次
UNKNOWN_GIVEUP_SEC = 120.0      # 结果未知的单超过这么久平台仍查不到，按失败处理
ORDER_RECHECK_SEC = 300.0       # 未完结订单多久刷新一次状态
REFRESH_PER_CYCLE = 3           # 每轮最多查几笔订单，避免拖慢监控
ORDER_CANCELLED = (11, 220)     # 已取消 / 已撤回
ORDER_FINAL = (10, 200)         # 已收货 / 结算


@dataclass(frozen=True)
class Listing:
    product_id: int
    price: float
    amount: int = 1


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _left(cap: float, used: float) -> float:
    return cap - used if cap > 0 else math.inf


def new_out_trade_no() -> str:
    """商户单号：毫秒时间戳 + 6 位随机数，纯数字。"""
    return f"{int(time.time() * 1000)}{random.randint(0, 999999):06d}"


def parse_listing(raw: dict) -> Listing | None:
    try:
        return Listing(
            product_id=int(raw["productId"]),
            price=float(raw["price"]),
            amount=int((raw.get("assetInfo") or {}).get("amount") or 1),
        )
    except (KeyError, TypeError, ValueError):
        return None


def pick_listings(listings: list[Listing], *, max_price: float, qty_left: int, budget_left: float,
                  skip_ids: set[int] | frozenset[int] = frozenset()) -> list[Listing]:
    """从在售里挑要买的：价格从低到高，直到数量或预算用完。"""
    picks: list[Listing] = []
    for l in sorted(listings, key=lambda l: l.price):
        if len(picks) >= qty_left or l.price > max_price + EPS:
            break
        # amount > 1 的堆叠在售不确定 price 是单价还是总价，不碰
        if l.price <= 0 or l.amount != 1 or l.product_id in skip_ids:
            continue
        if l.price > budget_left + EPS:
            break
        picks.append(l)
        budget_left -= l.price
    return picks


class Sweeper:
    def __init__(self, settings: Settings, items: list[WatchItem], client: C5Client, store: Store,
                 *, clock=time.time):
        self.s = settings
        self.items = items
        self.client = client
        self.store = store
        self._clock = clock
        self._skip_until: dict[int, float] = {}         # 在售 id -> 此时间前不再尝试
        self._pause_until: dict[str, float] = {}        # 饰品名 -> 此时间前不再下单
        self._last_price: dict[str, tuple] = {}
        # 下面几个给看板用：看板线程只读 view、只写 paused / refresh_requested / auto_targets
        self.paused = False                 # 暂停扫货：照常看行情，不下单
        self.refresh_requested = False      # 请求下一轮立即对全部订单对账
        self.view: dict = {}                # 每轮结束后发布的快照，整体替换不原地改
        # 看板按目标汇率算出的每个饰品的目标价；None = 没设目标汇率，用 watchlist 的 max_price。
        # 设了汇率但某个饰品的 Steam 价还没拿到，对应值是 None，这个饰品这一轮不买
        self.auto_targets: dict[str, float | None] | None = None

    def target(self, it: WatchItem) -> float | None:
        """这一轮实际用的目标价。"""
        auto = self.auto_targets
        return it.max_price if auto is None else auto.get(it.name)

    # ---------- 主循环的一轮 ----------

    def run_cycle(self) -> bool:
        """跑一轮。返回 False 表示所有目标都已买满或预算用完。"""
        live = self.s.mode == "live"
        if live:
            force, self.refresh_requested = self.refresh_requested, False
            self.refresh_orders(None if force else REFRESH_PER_CYCLE, force=force)
        items = self.items
        totals = self.store.totals(self.s.mode)
        budget = _left(self.s.max_total_spend, sum(t.spent for t in totals.values()))
        todo = [it for it in items
                if self._qty_left(it, totals) > 0 and self._spend_left(it, totals) > EPS]
        if not todo or budget <= EPS:
            self.view = {**self.view, "at": self._clock(), "done": True}
            # 还有结果未知的单时先不退出：对账后可能释放额度
            return bool(self.store.select(self.s.mode, statuses=("unknown",)))

        stats: dict[str, dict] = {}
        for app_id in sorted({it.app_id for it in todo}):
            stats.update(self.client.item_stats(app_id, [it.name for it in todo if it.app_id == app_id]))

        cycle_left = self.s.max_buy_per_cycle
        balance: float | None = None  # 实盘下有触发时才查
        for it in todo:
            st = stats.get(it.name) or {}
            price = _num(st.get("sellPrice"))
            tgt = self.target(it)
            self._log_price(it, st, price, tgt)
            if price is None or price <= 0 or tgt is None or price > tgt + EPS:
                continue
            if self.paused or cycle_left <= 0 or self._clock() < self._pause_until.get(it.name, 0.0):
                continue
            if live and balance is None:
                balance = (_num(self.client.balance().get("moneyAmount")) or 0.0) - self.s.min_balance_reserve
            money = min(budget, self._spend_left(it, totals), math.inf if balance is None else balance)
            qty = min(self._qty_left(it, totals), cycle_left)
            if self.s.strategy == "quick":
                bought, cost = self._sweep_quick(it, price, qty, money, tgt)
            else:
                bought, cost = self._sweep_listing(it, qty, money, tgt)
            cycle_left -= bought
            budget -= cost
            if balance is not None:
                balance -= cost
        self.view = {"at": self._clock(), "stats": stats, "pause_until": dict(self._pause_until),
                     "done": False}
        return True

    def _qty_left(self, it: WatchItem, totals: dict[str, Total]) -> int:
        return it.max_qty - totals.get(it.name, Total()).qty

    def _spend_left(self, it: WatchItem, totals: dict[str, Total]) -> float:
        return _left(it.max_spend, totals.get(it.name, Total()).spent)

    def _log_price(self, it: WatchItem, st: dict, price: float | None, tgt: float | None) -> None:
        # 价格或目标价有变化才记一条，避免每 3 秒刷屏
        if self._last_price.get(it.name) == (price, tgt):
            return
        self._last_price[it.name] = (price, tgt)
        if price is None:
            log.warning("查不到 %s 的行情，检查 name 是否是正确的 marketHashName", it.name)
        else:
            log.info("%s 最低 %.2f（%s） 在售 %s 求购最高 %s", it.name, price,
                     f"目标 ≤ {tgt:.2f}" if tgt is not None else "目标价等 Steam 价",
                     st.get("sellCount"), st.get("purchaseMaxPrice"))

    def _pause(self, it: WatchItem, reason) -> None:
        self._pause_until[it.name] = self._clock() + self.s.error_cooldown
        log.warning("%s 下单被拒，暂停 %.0fs: %s", it.name, self.s.error_cooldown, reason)

    # ---------- listing 策略：查在售列表，按在售 id 批量买 ----------

    def _sweep_listing(self, it: WatchItem, qty: int, money: float, tgt: float) -> tuple[int, float]:
        raw = self.client.search_products(app_id=it.app_id, name=it.name, price_max=tgt,
                                          delivery=it.delivery, asset_type=it.asset_type)
        now = self._clock()
        self._skip_until = {pid: t for pid, t in self._skip_until.items() if t > now}
        picks = pick_listings(
            [l for l in map(parse_listing, raw) if l],
            max_price=tgt, qty_left=qty, budget_left=money,
            skip_ids=self.store.product_ids(self.s.mode) | self._skip_until.keys(),
        )
        if not picks:
            return 0, 0.0
        if self.s.mode == "dry":
            for p in picks:
                self.store.add(new_out_trade_no(), mode="dry", name=it.name, product_id=p.product_id,
                               price=p.price, status="ok", ts=now)
            total = sum(p.price for p in picks)
            log.info("[模拟] 买入 %s × %d，合计 %.2f（%s）", it.name, len(picks), total,
                     " ".join(f"{p.price:.2f}" for p in picks))
            return len(picks), total
        return self._batch_buy(it, picks)

    def _batch_buy(self, it: WatchItem, picks: list[Listing]) -> tuple[int, float]:
        # 先落库再发请求：进程中途挂掉或超时，这几笔也会按“结果未知”占住预算
        orders = {new_out_trade_no(): p for p in picks}
        for no, p in orders.items():
            self.store.add(no, mode="live", name=it.name, product_id=p.product_id, price=p.price,
                           status="unknown", ts=self._clock())
        try:
            data = self.client.batch_buy(self.s.trade_url, [
                {"productId": p.product_id, "buyPrice": p.price, "outTradeNo": no}
                for no, p in orders.items()
            ])
        except C5NetworkError as e:
            log.error("%s 下单结果未知（%s），%d 笔先按已花费计入预算，稍后自动对账", it.name, e, len(picks))
            return len(picks), sum(p.price for p in picks)
        except C5Error as e:
            for no in orders:
                self.store.update(no, status="failed", error=str(e))
            self._pause(it, e)
            return 0, 0.0

        bought, cost = 0, 0.0
        for row in data.get("successList") or []:
            no = str(row.get("outTradeNo"))
            p = orders.pop(no, None)
            if p is None:
                continue
            pay = _num(row.get("actualPay")) or p.price
            self.store.update(no, status="ok", order_id=str(row.get("orderId") or ""), actual_pay=pay)
            log.info("买入 %s %.2f，订单 %s", it.name, pay, row.get("orderId"))
            bought, cost = bought + 1, cost + pay
        for row in data.get("failedList") or []:
            no = str(row.get("outTradeNo"))
            p = orders.pop(no, None)
            if p is None:
                continue
            self.store.update(no, status="failed", error="平台返回购买失败")
            self._skip_until[p.product_id] = self._clock() + FAILED_SKIP_SEC
            log.info("没买到 %s %.2f（在售 %s）", it.name, p.price, p.product_id)
        for no, p in orders.items():  # 两个列表里都没有的，保持未知，等对账
            log.warning("%s 单号 %s 不在返回结果里，先按已花费计入预算", it.name, no)
            bought, cost = bought + 1, cost + p.price
        return bought, cost

    # ---------- quick 策略：平台挑最低价，一次一件 ----------

    def _sweep_quick(self, it: WatchItem, price: float, qty: int, money: float, tgt: float) -> tuple[int, float]:
        if self.s.mode == "dry":
            # 拿不到在售明细，每轮按当前最低价模拟买一件
            if price > money + EPS:
                return 0, 0.0
            self.store.add(new_out_trade_no(), mode="dry", name=it.name, product_id="", price=price,
                           status="ok", ts=self._clock())
            log.info("[模拟] 快速购买 %s %.2f", it.name, price)
            return 1, price

        bought, cost = 0, 0.0
        # 成交价事先不知道，预算按目标价预留
        while bought < qty and tgt <= money - cost + EPS:
            no = new_out_trade_no()
            self.store.add(no, mode="live", name=it.name, product_id="", price=tgt,
                           status="unknown", ts=self._clock())
            try:
                data = self.client.quick_buy(out_trade_no=no, trade_url=self.s.trade_url,
                                             app_id=it.app_id, name=it.name, max_price=tgt,
                                             delivery=it.delivery)
            except C5NetworkError as e:
                log.error("%s 下单结果未知（%s），先按 %.2f 计入预算，稍后自动对账", it.name, e, tgt)
                return bought + 1, cost + tgt
            except C5Error as e:
                self.store.update(no, status="failed", error=str(e))
                self._pause(it, e)
                break
            if data.get("payStatus") == 2:
                self.store.update(no, status="failed", error="支付失败")
                self._pause(it, "支付失败")
                break
            pay = _num(data.get("actualPay")) or tgt
            self.store.update(no, status="ok", order_id=str(data.get("orderId") or ""), actual_pay=pay)
            log.info("买入 %s %.2f，订单 %s", it.name, pay, data.get("orderId"))
            bought, cost = bought + 1, cost + pay
        return bought, cost

    # ---------- 对账 ----------

    def refresh_orders(self, limit: int | None = None, *, force: bool = False) -> int:
        """结果未知的单查清楚，未完结的单刷新状态；被取消的不再占用数量和预算。返回查了几笔。"""
        now = self._clock()
        due = [p for p in self.store.select("live", statuses=COUNTED)
               if p.order_status not in ORDER_FINAL
               and (force or now - p.checked_at >= (UNKNOWN_RECHECK_SEC if p.status == "unknown"
                                                    else ORDER_RECHECK_SEC))]
        due.sort(key=lambda p: p.checked_at)  # 最久没查的优先，谁也不会被饿死
        due = due[:limit]
        for p in due:
            self._refresh(p, now)
        return len(due)

    def _refresh(self, p: Purchase, now: float) -> None:
        try:
            detail = self.client.order_detail(p.out_trade_no)
        except C5Error as e:
            # 查询本身失败不代表订单不存在，状态保持不变
            self.store.update(p.out_trade_no, checked_at=now)
            log.warning("查询订单 %s 失败: %s", p.out_trade_no, e)
            return
        if detail is None:
            if p.status == "unknown" and now - p.ts > UNKNOWN_GIVEUP_SEC:
                self.store.update(p.out_trade_no, status="failed", error="对账：平台没有这笔订单",
                                  checked_at=now)
                log.info("对账：%s 单号 %s 平台没有订单，按未成交处理", p.name, p.out_trade_no)
            else:
                self.store.update(p.out_trade_no, checked_at=now)
            return

        order_status = int(_num(detail.get("status")) or 0)
        fields = {"order_status": order_status, "checked_at": now,
                  "order_id": str(detail.get("orderId") or p.order_id or ""),
                  "product_id": str(detail.get("productId") or p.product_id)}
        if order_status in ORDER_CANCELLED:
            fields |= {"status": "cancelled", "error": detail.get("failedDesc") or detail.get("statusName")}
            log.warning("%s 订单 %s 已取消（%s），释放额度", p.name, fields["order_id"], fields["error"])
        elif p.status == "unknown":
            fields["status"] = "ok"
            log.info("对账：%s 单号 %s 已成交", p.name, p.out_trade_no)
        self.store.update(p.out_trade_no, **fields)
