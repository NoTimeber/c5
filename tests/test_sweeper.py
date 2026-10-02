from __future__ import annotations

import pytest

from c5bot.client import C5Error, C5NetworkError
from c5bot.config import Settings, WatchItem
from c5bot.store import Store
from c5bot.sweeper import (
    UNKNOWN_GIVEUP_SEC,
    Listing,
    Sweeper,
    parse_listing,
    pick_listings,
)

CASE = WatchItem(name="Kilowatt Case", max_price=5.5, max_qty=5)


class FakeClient:
    def __init__(self):
        self.stats: dict[str, dict] = {}
        self.listings: list[dict] = []
        self.money = 1000.0
        self.buy_result: dict | Exception | None = None  # None = 全部成功
        self.orders: dict[str, dict | None | Exception] = {}
        self.calls: list[tuple] = []

    def item_stats(self, app_id, names):
        self.calls.append(("item_stats", app_id, tuple(names)))
        return {n: self.stats[n] for n in names if n in self.stats}

    def search_products(self, **kw):
        self.calls.append(("search_products", kw))
        return [l for l in self.listings if l["price"] <= kw["price_max"]]

    def balance(self):
        self.calls.append(("balance",))
        return {"moneyAmount": self.money}

    def batch_buy(self, trade_url, products):
        self.calls.append(("batch_buy", trade_url, products))
        if isinstance(self.buy_result, Exception):
            raise self.buy_result
        if self.buy_result is not None:
            return self.buy_result
        return {"successList": [
            {"outTradeNo": p["outTradeNo"], "productId": str(p["productId"]), "actualPay": p["buyPrice"],
             "orderId": f"o{p['productId']}"} for p in products
        ], "failedList": []}

    def quick_buy(self, **kw):
        self.calls.append(("quick_buy", kw))
        if isinstance(self.buy_result, Exception):
            raise self.buy_result
        return self.buy_result or {"actualPay": 5.0, "orderId": 1, "payStatus": 1}

    def order_detail(self, out_trade_no):
        self.calls.append(("order_detail", out_trade_no))
        r = self.orders.get(out_trade_no)
        if isinstance(r, Exception):
            raise r
        return r

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def listing(pid, price, **asset):
    return {"productId": pid, "price": price, "delivery": 1, "assetInfo": {"assetId": pid, **asset}}


@pytest.fixture
def env(tmp_path):
    def make(mode="live", strategy="listing", items=(CASE,), **kw):
        kw.setdefault("max_total_spend", 100.0)
        s = Settings(app_key="k", mode=mode, strategy=strategy, trade_url="https://t", data_dir=tmp_path, **kw)
        client, store, clock = FakeClient(), Store(tmp_path / "t.sqlite"), Clock()
        client.stats = {CASE.name: {"sellPrice": 5.0, "sellCount": 10}}
        return Sweeper(s, list(items), client, store, clock=clock), client, store, clock
    return make


# ---------- 纯函数 ----------

def test_pick_cheapest_first_within_qty():
    ls = [Listing(1, 5.4), Listing(2, 5.0), Listing(3, 5.2), Listing(4, 5.6)]
    picks = pick_listings(ls, max_price=5.5, qty_left=2, budget_left=100)
    assert [p.product_id for p in picks] == [2, 3]


def test_pick_respects_budget_and_skips():
    ls = [Listing(1, 5.0), Listing(2, 5.0), Listing(3, 5.0), Listing(4, 0.0), Listing(5, 5.0, amount=3)]
    picks = pick_listings(ls, max_price=5.5, qty_left=10, budget_left=10.0, skip_ids={1})
    assert [p.product_id for p in picks] == [2, 3]


def test_parse_listing_keeps_int64_and_drops_garbage():
    assert parse_listing(listing(1444544795338493955, 2.2)).product_id == 1444544795338493955
    assert parse_listing({"price": 1}) is None
    assert parse_listing(listing(1, 2.0, amount=4)).amount == 4


# ---------- 监控 ----------

def test_no_buy_above_target(env):
    sw, client, store, _ = env()
    client.stats[CASE.name]["sellPrice"] = 5.51
    assert sw.run_cycle() is True
    assert client.count("search_products") == 0 and client.count("batch_buy") == 0
    assert store.select("live") == []


def test_dry_never_calls_buy_and_does_not_rebuy_same_listing(env):
    sw, client, store, _ = env(mode="dry")
    client.listings = [listing(1, 5.0), listing(2, 5.2)]
    sw.run_cycle()
    sw.run_cycle()
    assert client.count("batch_buy") == 0 and client.count("balance") == 0
    assert [p.product_id for p in store.select("dry")] == ["1", "2"]


# ---------- 实盘下单 ----------

def test_live_buys_and_records(env):
    sw, client, store, _ = env()
    client.listings = [listing(1444544795338493955, 5.0), listing(2, 5.2), listing(3, 5.9)]
    sw.run_cycle()
    (_, url, products), = [c for c in client.calls if c[0] == "batch_buy"]
    assert url == "https://t"
    assert [(p["productId"], p["buyPrice"]) for p in products] == [(1444544795338493955, 5.0), (2, 5.2)]
    rows = store.select("live")
    assert [r.status for r in rows] == ["ok", "ok"]
    assert rows[0].order_id == "o1444544795338493955"
    assert store.totals("live")[CASE.name].spent == pytest.approx(10.2)


def test_qty_cap_holds_across_cycles_and_then_stops(env):
    sw, client, store, _ = env()
    client.listings = [listing(i, 5.0) for i in range(1, 4)]
    sw.run_cycle()
    client.listings = [listing(i, 5.0) for i in range(10, 20)]
    sw.run_cycle()
    assert store.totals("live")[CASE.name].qty == CASE.max_qty
    assert sw.run_cycle() is False
    assert client.count("batch_buy") == 2


def test_per_cycle_cap(env):
    sw, client, store, _ = env(max_buy_per_cycle=2)
    client.listings = [listing(i, 5.0) for i in range(1, 6)]
    sw.run_cycle()
    assert store.totals("live")[CASE.name].qty == 2


def test_total_budget_and_balance_reserve(env):
    sw, client, store, _ = env(max_total_spend=12.0)
    client.listings = [listing(i, 5.0) for i in range(1, 6)]
    sw.run_cycle()
    assert store.totals("live")[CASE.name].qty == 2  # 12 元只够 2 个

    sw, client, store, _ = env(min_balance_reserve=990.0)
    store.clear("live")
    client.listings = [listing(i, 5.0) for i in range(1, 6)]
    sw.run_cycle()
    assert store.totals("live")[CASE.name].qty == 2  # 余额 1000 留 990，可用 10


def test_failed_listing_is_skipped_then_retried(env):
    sw, client, store, clock = env()
    client.listings = [listing(1, 5.0)]

    def fail_all(trade_url, products):
        client.calls.append(("batch_buy", trade_url, products))
        return {"successList": [], "failedList": [
            {"productId": str(p["productId"]), "outTradeNo": p["outTradeNo"]} for p in products]}
    client.batch_buy = fail_all

    sw.run_cycle()
    sw.run_cycle()
    assert client.count("batch_buy") == 1  # 冷却期内不重复尝试
    assert store.totals("live") == {}
    clock.now += 61
    sw.run_cycle()
    assert client.count("batch_buy") == 2


def test_rejected_request_frees_budget_and_pauses_item(env):
    sw, client, store, clock = env()
    client.listings = [listing(1, 5.0)]
    client.buy_result = C5Error("余额不足")
    sw.run_cycle()
    assert [r.status for r in store.select("live")] == ["failed"]
    sw.run_cycle()
    assert client.count("batch_buy") == 1  # 暂停中
    clock.now += 11
    sw.run_cycle()
    assert client.count("batch_buy") == 2


# ---------- 结果未知与对账 ----------

def test_timeout_counts_as_spent_until_reconciled(env):
    sw, client, store, clock = env(max_total_spend=10.0)
    client.listings = [listing(1, 5.0), listing(2, 5.0)]
    client.buy_result = C5NetworkError("超时")
    sw.run_cycle()
    assert [r.status for r in store.select("live")] == ["unknown", "unknown"]
    assert store.totals("live")[CASE.name].spent == pytest.approx(10.0)

    # 预算被未知单占满：不再下单，但也不退出
    client.buy_result = None
    client.listings = [listing(3, 5.0)]
    assert sw.run_cycle() is True
    assert client.count("batch_buy") == 1

    # 一笔查到已成交，一笔平台没有 -> 超过等待时间后按失败释放额度
    first, second = store.select("live")
    client.orders = {first.out_trade_no: {"orderId": 77, "productId": 1, "status": "1"}}
    clock.now += UNKNOWN_GIVEUP_SEC + 1
    sw.run_cycle()
    assert store.get(first.out_trade_no).status == "ok"
    assert store.get(first.out_trade_no).order_id == "77"
    assert store.get(second.out_trade_no).status == "failed"
    sw.run_cycle()
    assert client.count("batch_buy") == 2


def test_query_failure_keeps_unknown(env):
    sw, client, store, clock = env()
    store.add("n1", mode="live", name=CASE.name, product_id=1, price=5.0, status="unknown", ts=clock.now)
    client.orders = {"n1": C5Error("限流")}
    clock.now += UNKNOWN_GIVEUP_SEC + 1
    sw.refresh_orders()
    assert store.get("n1").status == "unknown"


def test_cancelled_order_releases_quota(env):
    sw, client, store, clock = env()
    store.add("n1", mode="live", name=CASE.name, product_id=1, price=5.0, status="ok", ts=clock.now)
    client.orders = {"n1": {"orderId": 9, "productId": 1, "status": "11", "failedDesc": "卖家未发货"}}
    sw.refresh_orders()
    assert store.get("n1").status == "cancelled"
    assert store.totals("live") == {}


def test_delivered_order_is_not_polled_again(env):
    sw, client, store, clock = env()
    store.add("n1", mode="live", name=CASE.name, product_id=1, price=5.0, status="ok", ts=clock.now)
    client.orders = {"n1": {"orderId": 9, "productId": 1, "status": "10"}}
    assert sw.refresh_orders(force=True) == 1
    assert sw.refresh_orders(force=True) == 0


# ---------- quick 策略 ----------

def test_quick_buys_until_platform_refuses(env):
    sw, client, store, _ = env(strategy="quick")
    results = iter([{"actualPay": 5.0, "orderId": 1, "payStatus": 1},
                    {"actualPay": 5.1, "orderId": 2, "payStatus": 1},
                    C5Error("没有符合条件的在售")])

    def quick(**kw):
        client.calls.append(("quick_buy", kw))
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r
    client.quick_buy = quick

    sw.run_cycle()
    assert client.count("search_products") == 0
    assert [r.status for r in store.select("live")] == ["ok", "ok", "failed"]
    assert store.totals("live")[CASE.name].spent == pytest.approx(10.1)


def test_quick_reserves_max_price_per_buy(env):
    sw, client, store, _ = env(strategy="quick", max_total_spend=12.0)
    sw.run_cycle()
    # 成交 5.0 两次后剩 2.0，不够 max_price 5.5，停手
    assert client.count("quick_buy") == 2
