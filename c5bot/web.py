"""本地网页看板：FastAPI 提供状态接口，静态页面在 c5bot/static。

扫货循环在主线程，看板在一个守护线程里；两边通过 Dashboard 共享状态。
Steam 价和余额由 Dashboard 自己的后台线程刷新，不占用扫货循环。
"""
from __future__ import annotations

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

from .client import C5Client, C5Error
from .compare import append_csv, c5_lowest, compare_row
from .config import ROOT, Settings, load_watchlist
from .steam import SteamError, SteamMarket
from .store import Store
from .sweeper import Sweeper

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
BALANCE_REFRESH_SEC = 60.0


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
        self.started_at = time.time()
        self.cycle_n = 0
        self.cycle_at: float | None = None
        self.cycle_error: str | None = None
        self.balance: float | None = None
        self.balance_at: float | None = None
        self.steam: dict[str, dict] = {}        # 饰品名 -> {price, at, error}
        self.steam_at: float | None = None
        self.steam_busy = False
        self._steam = SteamMarket(proxy=settings.steam_proxy, currency=settings.steam_currency,
                                  timeout=settings.timeout)
        self._client = C5Client(settings.app_key, proxy=settings.proxy, timeout=settings.timeout)
        self._refresh_now = threading.Event()
        self._stop = threading.Event()

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
        """拉一遍所有饰品的 Steam 价，顺手把对比行追加到 compare.csv。"""
        self.steam_busy = True
        try:
            rows = []
            stats = self.sweeper.view.get("stats") or {}
            for it in list(self.sweeper.items):
                if self._stop.is_set():
                    return
                try:
                    sp = self._steam.price(it.app_id, it.name)
                    self.steam[it.name] = {"price": sp, "at": time.time(), "error": None}
                    rows.append(compare_row(it, c5_lowest(stats.get(it.name)), sp))
                except SteamError as e:
                    old = self.steam.get(it.name) or {"price": None, "at": None}
                    self.steam[it.name] = {**old, "error": str(e)}
                    log.warning("Steam 价格 %s: %s", it.name, e)
            self.steam_at = time.time()
            append_csv(self.s.data_dir / "compare.csv", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), rows)
        finally:
            self.steam_busy = False

    def _balance_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.balance = float(self._client.balance().get("moneyAmount") or 0.0)
                self.balance_at = time.time()
            except C5Error as e:
                log.warning("查余额失败: %s", e)
            self._stop.wait(BALANCE_REFRESH_SEC)

    # ---------- 状态 ----------

    def reload_watchlist(self) -> int:
        items = load_watchlist(self._watchlist_path)
        self.sweeper.items = items
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
        items = []
        for it in sw.items:
            st = stats.get(it.name) or {}
            c5 = c5_lowest(st)
            t = totals.get(it.name)
            qty, spent = (t.qty, t.spent) if t else (0, 0.0)
            steam = self.steam.get(it.name) or {}
            if qty >= it.max_qty or (it.max_spend and spent >= it.max_spend):
                status = "done"
            elif pause_until.get(it.name, 0) > now:
                status = "cooldown"
            elif c5 is None:
                status = "unknown"
            elif c5 <= it.max_price:
                status = "hit"
            else:
                status = "watch"
            items.append({
                **asdict(compare_row(it, c5, steam.get("price"))),
                "status": status, "pause_until": pause_until.get(it.name),
                "sell_count": st.get("sellCount"), "purchase_max": st.get("purchaseMaxPrice"),
                "max_qty": it.max_qty, "max_spend": it.max_spend, "bought": qty, "spent": spent,
                "steam_at": steam.get("at"), "steam_error": steam.get("error"),
            })
        return {
            "now": now, "started_at": self.started_at,
            "mode": self.s.mode, "strategy": self.s.strategy, "paused": sw.paused,
            "poll_interval": self.s.poll_interval, "steam_refresh_sec": self.s.steam_refresh_sec,
            "cycle": {"n": self.cycle_n, "at": self.cycle_at, "error": self.cycle_error,
                      "done": bool(view.get("done"))},
            "budget": {"total": self.s.max_total_spend,
                       "spent": sum(t.spent for t in totals.values()),
                       "qty": sum(t.qty for t in totals.values())},
            "balance": {"value": self.balance, "at": self.balance_at},
            "steam": {"at": self.steam_at, "busy": self.steam_busy},
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

    return app


def serve_in_thread(app: FastAPI, host: str, port: int) -> threading.Thread:
    """看板跑在守护线程里，主线程留给扫货循环（Ctrl+C 也由主线程处理）。"""
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, name="web", daemon=True)
    t.start()
    return t
