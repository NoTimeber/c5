"""本地网页看板：FastAPI 提供状态接口，静态页面在 c5bot/static。

扫货循环在主线程，看板在一个守护线程里；两边通过 Dashboard 共享状态。
Steam 价和余额由 Dashboard 自己的后台线程刷新，不占用扫货循环。
看板上设的目标汇率存在 data/dashboard.json，重启不丢。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .client import C5Client, C5Error
from .compare import (
    SteamRate,
    append_csv,
    c5_lowest,
    compare_row,
    fetch_steam_rate,
    rate_discount,
    target_price,
)
from .config import ROOT, Settings, load_watchlist
from .steam import SteamError, SteamMarket, SteamPrice, fmt_wait
from .store import Store
from .sweeper import Sweeper

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
BALANCE_REFRESH_SEC = 60.0
RATE_REFRESH_SEC = 3600.0       # Steam 汇率多久重新量一次：它一天也变不了多少，少打两次 Steam
MANUAL_REFRESH_MIN_SEC = 60.0   # 看板上手动“刷新 Steam 价”的最小间隔
STEAM_STALE_FACTOR = 3          # Steam 价超过这么多个刷新周期没更新，按汇率算目标价时当作没有
SETTINGS_FILE = "dashboard.json"
RATE_MIN, RATE_MAX = 0.1, 100.0  # 目标汇率合法范围（人民币 / 1 美元）


class LogBuffer(logging.Handler):
    """最近 N 条日志，给看板显示。"""

    def __init__(self, maxlen: int = 300):
        super().__init__()
        self.lines: deque[dict] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append({"t": record.created, "level": record.levelname, "msg": record.getMessage()})


class Dashboard:
    def __init__(self, settings: Settings, sweeper: Sweeper, store_path: Path, logs: LogBuffer,
                 *, watchlist_path: Path | None = None):
        self.s = settings
        self.sweeper = sweeper
        self.logs = logs
        self._store_path = store_path
        self._watchlist_path = watchlist_path or ROOT / "watchlist.toml"
        self._settings_path = settings.data_dir / SETTINGS_FILE
        self.started_at = time.time()
        self.cycle_n = 0
        self.cycle_at: float | None = None
        self.cycle_error: str | None = None
        self.balance: float | None = None
        self.balance_at: float | None = None
        self.steam: dict[str, dict] = {}        # 饰品名 -> {price, at, error}
        self.steam_at: float | None = None
        self.steam_busy = False
        self.steam_rate: SteamRate | None = None    # Steam 内部人民币/美元换算率
        self.steam_rate_error: str | None = None
        self._steam = SteamMarket(proxy=settings.steam_proxy, currency=settings.steam_currency,
                                  timeout=settings.timeout)
        self._client = C5Client(settings.app_key, proxy=settings.proxy, timeout=settings.timeout)
        self._refresh_now = threading.Event()
        self._stop = threading.Event()
        self.target_rate: float | None = self._load_target_rate()
        self._apply_targets()

    # ---------- 后台线程 ----------

    def start(self) -> None:
        threading.Thread(target=self._steam_loop, name="steam", daemon=True).start()
        if self.s.mode == "live":
            threading.Thread(target=self._balance_loop, name="balance", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._refresh_now.set()

    def on_cycle(self, error: str | None) -> None:
        self.cycle_n += 1
        self.cycle_at = time.time()
        self.cycle_error = error

    def request_steam_refresh(self) -> None:
        self._refresh_now.set()

    def _steam_loop(self) -> None:
        while not self._stop.is_set():
            self.refresh_steam()
            self._refresh_now.wait(self.s.steam_refresh_sec)
            self._refresh_now.clear()

    def refresh_steam(self) -> None:
        """拉一遍所有饰品的 Steam 价和 Steam 汇率，重算目标价，顺手把对比行追加到 compare.csv。"""
        self.steam_busy = True
        try:
            items = list(self.sweeper.items)
            fresh: list[tuple] = []
            for it in items:
                if self._stop.is_set():
                    return
                try:
                    sp = self._steam.price(it.app_id, it.name)
                    self.steam[it.name] = {"price": sp, "at": time.time(), "error": None}
                    fresh.append((it, sp))
                except SteamError as e:
                    old = self.steam.get(it.name) or {"price": None, "at": None}
                    self.steam[it.name] = {**old, "error": str(e)}
                    log.warning("Steam 价格 %s: %s", it.name, e)
                    if self._steam.blocked_for > 0:
                        log.warning("Steam 限流，本轮剩下的饰品先跳过，沿用上次的价")
                        break
            self._refresh_rate()
            self.steam_at = time.time()
            self._apply_targets()
            stats = self.sweeper.view.get("stats") or {}
            rate = self.steam_rate.rate if self.steam_rate else None
            rows = [compare_row(it, c5_lowest(stats.get(it.name)), sp, target=self.sweeper.target(it), rate=rate)
                    for it, sp in fresh]
            append_csv(self.s.data_dir / "compare.csv", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), rows)
        finally:
            self.steam_busy = False

    @property
    def steam_blocked_for(self) -> float:
        """被 Steam 限流时还要等多少秒，0 = 正常。"""
        return self._steam.blocked_for

    def _refresh_rate(self) -> None:
        """用参照饰品查人民币价和美元价算 Steam 汇率，最多每 RATE_REFRESH_SEC 量一次。失败就沿用上次的值。"""
        if self._stop.is_set() or self._steam.blocked_for > 0:
            return
        if self.steam_rate and time.time() - self.steam_rate.at < RATE_REFRESH_SEC:
            return
        try:
            self.steam_rate = fetch_steam_rate(self._steam, self.s.steam_rate_item)
            self.steam_rate_error = None
        except SteamError as e:
            self.steam_rate_error = str(e)
            log.warning("Steam 汇率: %s", e)

    def _balance_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.balance = float(self._client.balance().get("moneyAmount") or 0.0)
                self.balance_at = time.time()
            except C5Error as e:
                log.warning("查余额失败: %s", e)
            self._stop.wait(BALANCE_REFRESH_SEC)

    # ---------- 目标汇率 ----------

    def _load_target_rate(self) -> float | None:
        try:
            v = json.loads(self._settings_path.read_text(encoding="utf-8")).get("target_rate")
            return float(v) if v is not None and RATE_MIN <= float(v) <= RATE_MAX else None
        except (OSError, ValueError, AttributeError):
            return None

    def set_target_rate(self, rate: float | None) -> None:
        """看板上设置 / 清除目标汇率，写进 data/dashboard.json。"""
        self.target_rate = rate
        self._settings_path.write_text(json.dumps({"target_rate": rate}), encoding="utf-8")
        self._apply_targets()
        if rate is None:
            log.info("看板：清除目标汇率，目标价用 watchlist 的 max_price")
        else:
            disc = self.discount()
            log.info("看板：目标汇率 %.2f 元/USD%s", rate,
                     f"，相当于 {disc * 10:.2f} 折" if disc else "，等拿到 Steam 汇率后生效")

    def discount(self) -> float | None:
        """目标汇率对应的折扣。"""
        return rate_discount(self.target_rate, self.steam_rate.rate if self.steam_rate else None)

    def _apply_targets(self) -> None:
        """按目标汇率给每个饰品算目标价，交给扫货线程。没设汇率就交 None，扫货用 watchlist 的 max_price。"""
        if self.target_rate is None:
            self.sweeper.auto_targets = None
            return
        disc = self.discount()
        now = time.time()
        stale_after = STEAM_STALE_FACTOR * self.s.steam_refresh_sec
        targets: dict[str, float | None] = {}
        for it in self.sweeper.items:
            st = self.steam.get(it.name) or {}
            sp: SteamPrice | None = st.get("price")
            at = st.get("at")
            fresh = sp is not None and at is not None and now - at <= stale_after
            targets[it.name] = target_price(disc, sp.lowest) if fresh and sp else None
        self.sweeper.auto_targets = targets

    # ---------- 状态 ----------

    def reload_watchlist(self) -> int:
        items = load_watchlist(self._watchlist_path)
        self.sweeper.items = items
        self._apply_targets()
        log.info("重新加载 watchlist：%d 个饰品", len(items))
        return len(items)

    def state(self) -> dict[str, Any]:
        sw = self.sweeper
        view = sw.view
        stats = view.get("stats") or {}
        pause_until = view.get("pause_until") or {}
        store = Store(self._store_path)  # sqlite 连接不能跨线程，看板每次自己开
        try:
            totals = store.totals(self.s.mode)
            purchases = store.select(self.s.mode)[-50:]
        finally:
            store.close()
        now = time.time()
        rate = self.steam_rate.rate if self.steam_rate else None
        auto = sw.auto_targets is not None
        items = []
        for it in sw.items:
            st = stats.get(it.name) or {}
            c5 = c5_lowest(st)
            tgt = sw.target(it)
            t = totals.get(it.name)
            qty, spent = (t.qty, t.spent) if t else (0, 0.0)
            steam = self.steam.get(it.name) or {}
            if qty >= it.max_qty or (it.max_spend and spent >= it.max_spend):
                status = "done"
            elif pause_until.get(it.name, 0) > now:
                status = "cooldown"
            elif c5 is None:
                status = "unknown"
            elif tgt is None:
                status = "nosteam"
            elif c5 <= tgt:
                status = "hit"
            else:
                status = "watch"
            items.append({
                **asdict(compare_row(it, c5, steam.get("price"), target=tgt, rate=rate)),
                "status": status, "pause_until": pause_until.get(it.name),
                "sell_count": st.get("sellCount"), "purchase_max": st.get("purchaseMaxPrice"),
                "max_price": it.max_price, "target_auto": auto,
                "max_qty": it.max_qty, "max_spend": it.max_spend, "bought": qty, "spent": spent,
                "steam_at": steam.get("at"), "steam_error": steam.get("error"),
            })
        return {
            "now": now, "started_at": self.started_at, "version": __version__,
            "mode": self.s.mode, "strategy": self.s.strategy, "paused": sw.paused,
            "poll_interval": self.s.poll_interval, "steam_refresh_sec": self.s.steam_refresh_sec,
            "cycle": {"n": self.cycle_n, "at": self.cycle_at, "error": self.cycle_error,
                      "done": bool(view.get("done"))},
            "budget": {"total": self.s.max_total_spend,
                       "spent": sum(t.spent for t in totals.values()),
                       "qty": sum(t.qty for t in totals.values())},
            "balance": {"value": self.balance, "at": self.balance_at},
            "steam": {"at": self.steam_at, "busy": self.steam_busy, "blocked_for": self.steam_blocked_for},
            "rate": {"target": self.target_rate, "discount": self.discount(),
                     "steam": asdict(self.steam_rate) if self.steam_rate else None,
                     "steam_error": self.steam_rate_error},
            "items": items,
            "purchases": [asdict(p) for p in reversed(purchases)],
            "logs": list(self.logs.lines),
        }


def create_app(dash: Dashboard) -> FastAPI:
    app = FastAPI(title="C5 扫货看板", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def no_cache_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    async def state() -> JSONResponse:
        return JSONResponse(dash.state())

    @app.post("/api/pause")
    async def pause() -> JSONResponse:
        dash.sweeper.paused = True
        log.info("看板：暂停扫货")
        return JSONResponse({"ok": True, "paused": True})

    @app.post("/api/resume")
    async def resume() -> JSONResponse:
        dash.sweeper.paused = False
        log.info("看板：继续扫货")
        return JSONResponse({"ok": True, "paused": False})

    @app.post("/api/steam/refresh")
    async def steam_refresh() -> JSONResponse:
        if dash.steam_busy:
            return JSONResponse({"ok": False, "error": "正在刷新"}, status_code=409)
        blocked = dash.steam_blocked_for
        if blocked > 0:
            return JSONResponse({"ok": False, "error": f"Steam 限流中，{fmt_wait(blocked)}后会自动重试"}, status_code=429)
        since = time.time() - (dash.steam_at or 0.0)
        if since < MANUAL_REFRESH_MIN_SEC:
            return JSONResponse({"ok": False, "error": f"刚刷新过，{fmt_wait(MANUAL_REFRESH_MIN_SEC - since)}后再点"},
                                status_code=429)
        dash.request_steam_refresh()
        return JSONResponse({"ok": True})

    @app.post("/api/orders/refresh")
    async def orders_refresh() -> JSONResponse:
        if dash.s.mode != "live":
            return JSONResponse({"ok": False, "error": "dry 模式没有平台订单"}, status_code=400)
        dash.sweeper.refresh_requested = True
        return JSONResponse({"ok": True})

    @app.post("/api/watchlist/reload")
    async def watchlist_reload() -> JSONResponse:
        try:
            n = dash.reload_watchlist()
        except SystemExit as e:  # load_watchlist 用 SystemExit 报配置错误
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "count": n})

    @app.post("/api/rate")
    async def set_rate(request: Request) -> JSONResponse:
        """body: {"rate": 5.2} 设置目标汇率；{"rate": null} 清除，恢复用 watchlist 的 max_price。"""
        try:
            body = await request.json()
        except ValueError:
            body = None
        raw = body.get("rate") if isinstance(body, dict) else None
        if raw in (None, ""):
            dash.set_target_rate(None)
            return JSONResponse({"ok": True, "rate": None})
        try:
            rate = float(raw)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "目标汇率要是数字"}, status_code=400)
        if not (RATE_MIN <= rate <= RATE_MAX):
            return JSONResponse({"ok": False, "error": f"目标汇率范围 {RATE_MIN} ~ {RATE_MAX:.0f}（人民币 / 1 美元）"},
                                status_code=400)
        dash.set_target_rate(rate)
        return JSONResponse({"ok": True, "rate": rate})

    return app


def serve_in_thread(app: FastAPI, host: str, port: int) -> threading.Thread:
    """看板跑在守护线程里，主线程留给扫货循环（Ctrl+C 也由主线程处理）。"""
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, name="web", daemon=True)
    t.start()
    return t
