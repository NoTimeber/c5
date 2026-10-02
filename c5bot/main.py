"""命令行入口：uv run python -m c5bot <命令>"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
import unicodedata
from datetime import datetime

from .client import C5Client, C5Error
from .compare import SteamRate, append_csv, c5_lowest, compare_row, fetch_steam_rate
from .config import Settings, WatchItem, load_settings, load_watchlist
from .steam import SteamError, SteamMarket, SteamPrice
from .store import Store
from .sweeper import Sweeper, parse_listing
from .web import Dashboard, LogBuffer, create_app, serve_in_thread

log = logging.getLogger("c5bot")

ACCOUNT_STATUS = {1: "正常", 2: "暂挂", 3: "不可交易"}


def setup_logging(settings: Settings) -> LogBuffer:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    logfile = logging.FileHandler(settings.data_dir / "c5bot.log", encoding="utf-8")
    buffer = LogBuffer()
    for h in (console, logfile):
        h.setFormatter(fmt)
    logging.basicConfig(level=settings.log_level, handlers=[console, logfile, buffer])
    # urllib3 的 DEBUG 日志会打出带 app-key 的完整 URL
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return buffer


def make_client(s: Settings) -> C5Client:
    return C5Client(s.app_key, proxy=s.proxy, timeout=s.timeout)


def _fetch_stats(client: C5Client, items: list[WatchItem]) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for app_id in sorted({it.app_id for it in items}):
        stats.update(client.item_stats(app_id, [it.name for it in items if it.app_id == app_id]))
    return stats


def cmd_check(s: Settings) -> int:
    client = make_client(s)
    bal = client.balance()
    print(f"接口连通，app-key 有效。可用余额 {bal.get('moneyAmount')}"
          f"（待结算 {bal.get('tradeSettleAmount')}，保证金 {bal.get('depositAmount')}）")
    for acc in client.steam_info().get("steamList") or []:
        status = ACCOUNT_STATUS.get(acc.get("accountStatus"), acc.get("accountStatus"))
        print(f"Steam 账号 {acc.get('steamId')} {acc.get('nickname') or ''} 状态: {status}")
    print(f"模式 {s.mode}，策略 {s.strategy}，交易链接{'已' if s.trade_url else '未'}配置，"
          f"总预算 {s.max_total_spend or '不限'}")
    return 0


def cmd_prices(s: Settings) -> int:
    items = load_watchlist()
    stats = _fetch_stats(make_client(s), items)
    for it in items:
        st = stats.get(it.name)
        if not st:
            print(f"{it.name}: 查不到，检查 name 是否是正确的 marketHashName")
            continue
        price = c5_lowest(st)
        hit = "  <= 已到目标价" if price is not None and price <= it.max_price else ""
        print(f"{it.name}: 最低 {st.get('sellPrice')}（目标 ≤ {it.max_price}） 在售 {st.get('sellCount')} "
              f"求购最高 {st.get('purchaseMaxPrice')}{hit}")
    return 0


def _width(text: str) -> int:
    """终端显示宽度：中文等全角字符占 2 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _row(cells: list, widths: list[int]) -> str:
    out = []
    for i, (cell, w) in enumerate(zip(cells, widths)):
        text = "-" if cell is None else f"{cell:.2f}" if isinstance(cell, float) else str(cell)
        pad = " " * max(0, w - _width(text))
        out.append(text + pad if i == 0 else pad + text)
    return "".join(out)


def _fmt_discount(d: float | None) -> str:
    return f"{d * 10:.2f}折" if d else "-"


def cmd_compare(s: Settings, every: float) -> int:
    """C5 最低价 vs Steam 最低挂单价，算 1 元 Steam 余额要花多少钱、1 美元余额花多少人民币。"""
    items = load_watchlist()
    client = make_client(s)
    steam = SteamMarket(proxy=s.steam_proxy, currency=s.steam_currency, timeout=s.timeout)
    widths = [max(28, *(_width(it.name) + 2 for it in items)), 8, 8, 10, 8, 12, 12, 12, 10, 9]
    while True:
        stats = _fetch_stats(client, items)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        priced: list[tuple[WatchItem, float | None, SteamPrice]] = []
        failed: list[tuple[WatchItem, float | None, str]] = []
        for it in items:
            c5 = c5_lowest(stats.get(it.name))
            try:
                priced.append((it, c5, steam.price(it.app_id, it.name)))
            except SteamError as e:
                failed.append((it, c5, str(e)))
        rate: SteamRate | None = None
        try:
            rate = fetch_steam_rate(steam, [(it, sp) for it, _, sp in priced])
        except SteamError as e:
            print(f"Steam 汇率查询失败: {e}")
        print(f"\n{now}  Steam 价为当前最低挂单价，净到手 = 扣 Steam 5% + CS2 10% 手续费后。"
              f"C5 买到的 7 天后才能挂 Steam，折扣按今天价算，仅供参考。")
        if rate:
            print(f"Steam 汇率 {rate.rate:.4f} 元/USD（{rate.name} ¥{rate.cny:.2f} / ${rate.usd:.2f}）。"
                  f"汇率(C5最低) = 按 C5 最低价买入，1 美元 Steam 余额花多少人民币。")
        print(_row(["箱子", "C5最低", "目标价", "Steam最低", "净到手", "折(C5最低)", "汇率(C5最低)", "折(目标价)",
                    "Steam中位", "24h成交"], widths))
        rows = []
        for it, c5, sp in priced:
            r = compare_row(it, c5, sp, rate=rate.rate if rate else None)
            rows.append(r)
            print(_row([r.name, r.c5_lowest, r.c5_target, r.steam_lowest, r.steam_net,
                        _fmt_discount(r.discount_at_lowest), r.rate_at_lowest, _fmt_discount(r.discount_at_target),
                        r.steam_median, r.steam_volume], widths))
        for it, c5, err in failed:
            print(_row([it.name, c5], widths[:2]) + f"  Steam 查询失败: {err}")
        append_csv(s.data_dir / "compare.csv", now, rows)
        if not every:
            return 0
        time.sleep(every * 60)


def cmd_listings(s: Settings, name: str) -> int:
    matches = [it for it in load_watchlist() if it.name == name]
    if not matches:
        raise SystemExit(f"watchlist.toml 里没有 {name}")
    it = matches[0]
    raw = make_client(s).search_products(app_id=it.app_id, name=it.name, price_max=it.max_price,
                                         delivery=it.delivery, asset_type=it.asset_type)
    listings = sorted((l for l in map(parse_listing, raw) if l), key=lambda l: l.price)
    print(f"{it.name} 目标价 {it.max_price} 以内的在售：{len(listings)} 件")
    for l in listings:
        print(f"  {l.price:.2f}  在售 id {l.product_id}")
    return 0


def cmd_run(s: Settings, logs: LogBuffer, start_paused: bool) -> int:
    items = load_watchlist()
    store_path = s.data_dir / "c5bot.sqlite"
    store = Store(store_path)
    if s.mode == "dry":
        store.clear("dry")  # 模拟流水每次启动从零开始
    sweeper = Sweeper(s, items, make_client(s), store)
    start_paused = start_paused or s.start_paused
    sweeper.paused = start_paused
    log.info("启动：模式 %s，策略 %s，轮询 %.1fs，总预算 %s，监控 %d 个饰品%s",
             s.mode, s.strategy, s.poll_interval, s.max_total_spend or "不限", len(items),
             "，先暂停扫货只看行情" if start_paused else "")
    totals = store.totals(s.mode)
    for it in items:
        t = totals.get(it.name)
        log.info("  %s ≤ %.2f × %d（已买 %d，已花 %.2f）", it.name, it.max_price, it.max_qty,
                 t.qty if t else 0, t.spent if t else 0.0)

    dash: Dashboard | None = None
    if s.ui_port:
        dash = Dashboard(s, sweeper, store_path, logs)
        dash.start()
        serve_in_thread(create_app(dash), s.ui_host, s.ui_port)
        log.info("看板地址: http://%s:%s", s.ui_host, s.ui_port)

    def _sigterm(signum, frame):
        raise KeyboardInterrupt  # docker stop 发的是 SIGTERM，走和 Ctrl+C 一样的退出路径

    signal.signal(signal.SIGTERM, _sigterm)

    errors = 0
    done_logged = False
    try:
        while True:
            started = time.monotonic()
            try:
                more = sweeper.run_cycle()
                errors = 0
                if dash:
                    dash.on_cycle(None)
                if not more:
                    if not dash:
                        log.info("所有目标已买满或预算用完，退出")
                        return 0
                    if not done_logged:
                        log.info("所有目标已买满或预算用完，看板继续运行，Ctrl+C 退出")
                        done_logged = True
            except C5Error as e:
                errors += 1
                log.warning("本轮失败（连续第 %d 次）: %s", errors, e)
                if dash:
                    dash.on_cycle(str(e))
            delay = s.poll_interval * min(2 ** errors, 20)  # 连续失败时退避
            time.sleep(max(0.0, delay - (time.monotonic() - started)))
    except KeyboardInterrupt:
        log.info("已停止")
        return 0
    finally:
        if dash:
            dash.stop()


def cmd_orders(s: Settings, mode: str) -> int:
    store = Store(s.data_dir / "c5bot.sqlite")
    if mode == "live":
        n = Sweeper(s, [], make_client(s), store).refresh_orders(force=True)
        print(f"已向平台同步 {n} 笔未完结订单")
    rows = store.select(mode)
    for p in rows[-30:]:
        when = datetime.fromtimestamp(p.ts).strftime("%m-%d %H:%M:%S")
        print(f"{when} {p.status:<9} {p.name} {p.cost:.2f} 单号 {p.out_trade_no} "
              f"订单 {p.order_id or '-'} 平台状态 {p.order_status or '-'} {p.error or ''}")
    print(f"共 {len(rows)} 笔（只显示最近 30 笔）。占用额度的：")
    for name, t in store.totals(mode).items():
        print(f"  {name}: {t.qty} 个，{t.spent:.2f}")
    unknown = [p for p in rows if p.status == "unknown"]
    if unknown:
        print(f"有 {len(unknown)} 笔结果未知，按已花费占着额度。去官网核对后用 resolve 命令手工标记。")
    return 0


def cmd_resolve(s: Settings, out_trade_no: str, status: str) -> int:
    store = Store(s.data_dir / "c5bot.sqlite")
    p = store.get(out_trade_no)
    if p is None:
        raise SystemExit(f"没有单号 {out_trade_no}")
    store.update(out_trade_no, status=status, error="手工标记")
    print(f"{p.name} 单号 {out_trade_no}: {p.status} -> {status}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Windows 下被管道接走时默认是 GBK：统一 UTF-8，饰品名里的 ™ ★ 等字符也不至于抛异常
        stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="c5bot", description="C5GAME 武器箱监控扫货")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="检查连通性、app-key、余额、Steam 账号状态")
    sub.add_parser("prices", help="查一次监控列表的当前行情")
    p = sub.add_parser("compare", help="C5 价 vs Steam 价，算能拿到几折的 Steam 余额（只读，不下单）")
    p.add_argument("--every", type=float, default=0, metavar="分钟", help="每隔 N 分钟重算一次，结果追加到 data/compare.csv")
    p = sub.add_parser("listings", help="查某个饰品目标价以内的在售（验证在售接口权限和 IP 白名单）")
    p.add_argument("name", help="watchlist.toml 里的 name")
    p = sub.add_parser("run", help="启动监控扫货和网页看板")
    p.add_argument("--paused", action="store_true", help="启动时先暂停扫货，只看行情，在看板上点“继续”才开始买")
    p = sub.add_parser("orders", help="同步订单状态并显示购买流水")
    p.add_argument("--mode", choices=("live", "dry"), default="live")
    p = sub.add_parser("resolve", help="手工标记一笔结果未知的单")
    p.add_argument("out_trade_no")
    p.add_argument("status", choices=("ok", "failed"))
    args = parser.parse_args(argv)

    settings = load_settings()
    logs = setup_logging(settings)
    try:
        match args.cmd:
            case "check":
                return cmd_check(settings)
            case "prices":
                return cmd_prices(settings)
            case "compare":
                return cmd_compare(settings, args.every)
            case "listings":
                return cmd_listings(settings, args.name)
            case "run":
                return cmd_run(settings, logs, args.paused)
            case "orders":
                return cmd_orders(settings, args.mode)
            case "resolve":
                return cmd_resolve(settings, args.out_trade_no, args.status)
    except C5Error as e:
        print(f"失败: {e}", file=sys.stderr)
        return 1
    return 0
