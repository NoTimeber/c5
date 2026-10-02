"""本地网页看板：FastAPI 提供状态接口，静态页面在 c5bot/static。

扫货循环在主线程，看板在一个守护线程里；两边通过 Dashboard 共享状态。
Steam 价和余额由 Dashboard 自己的后台线程刷新，不占用扫货循环。
看板上设的目标汇率存在 data/dashboard.json，Steam 登录态存在 data/steam_session.json，重启都不丢。
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
from fastapi.concurrency import run_in_threadpool
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
from .steam import USD, SteamError, SteamLoginRequired, SteamMarket, fmt_wait, sell_price_from_history
from .steam_login import GUARD_NAMES, SteamAuth, SteamLoginError, SteamSession, load_session, renew, save_session
from .store import Store
from .sweeper import Sweeper

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
BALANCE_REFRESH_SEC = 60.0
RATE_REFRESH_SEC = 3600.0       # Steam 汇率多久重新量一次：它一天也变不了多少，少打两次 Steam
MANUAL_REFRESH_MIN_SEC = 60.0   # 看板上手动“刷新 Steam 价”的最小间隔
STEAM_STALE_FACTOR = 3          # Steam 价超过这么多个刷新周期没更新，按汇率算目标价时当作没有
ACCESS_RENEW_BEFORE_SEC = 3600.0  # Steam access token 剩不到 1 小时就用 refresh token 续
SETTINGS_FILE = "dashboard.json"
SESSION_FILE = "steam_session.json"
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
        self._session_path = settings.data_dir / SESSION_FILE
        self.started_at = time.time()
        self.cycle_n = 0
        self.cycle_at: float | None = None
        self.cycle_error: str | None = None
        self.balance: float | None = None
        self.balance_at: float | None = None
        self.steam: dict[str, dict] = {}        # 饰品名 -> {price, at, error}
        self.history: dict[str, dict] = {}      # 饰品名 -> {sell, volume, sell_at, at, error}（登录后的挂单价）
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
        self.steam_session: SteamSession | None = load_session(self._session_path)
        self.steam_login_error: str | None = None
        self._auth: SteamAuth | None = None      # 进行中的 Steam 登录
        if self.steam_session:
            self._steam.set_login(self.steam_session.cookie)
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
        """拉一遍所有饰品的 Steam 价（登录后还有成交历史）和 Steam 汇率，重算目标价，顺手把对比行追加到 compare.csv。"""
        self.steam_busy = True
        try:
            items = list(self.sweeper.items)
            use_history = self._ensure_session()
            self._refresh_rate()  # 先量汇率：美元区账号的成交历史要靠它换算成人民币
            fresh: list[tuple] = []
            if self._steam.blocked_for > 0:
                log.warning("Steam 限流，本轮饰品行情先跳过，沿用上次的价")
                items = []
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
                    continue
                if use_history:
                    use_history = self._refresh_history(it, sp)
                    if self._steam.blocked_for > 0:
                        log.warning("Steam 限流，本轮剩下的饰品先跳过，沿用上次的价")
                        break
            self.steam_at = time.time()
            self._apply_targets()
            stats = self.sweeper.view.get("stats") or {}
            rate = self._rate_for_rows()
            rows = [compare_row(it, c5_lowest(stats.get(it.name)), sp, target=self.sweeper.target(it), rate=rate,
                                sell=self.sell_basis(it)[0])
                    for it, sp in fresh]
            append_csv(self.s.data_dir / "compare.csv", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), rows)
        finally:
            self.steam_busy = False

    def _refresh_history(self, it, sp) -> bool:
        """拉一个饰品的成交历史，算挂单价。返回登录态还能不能继续用。"""
        old = self.history.get(it.name) or {"sell": None, "sell_usd": None, "volume": None, "sell_at": None, "at": None}
        try:
            hist = self._steam.price_history(it.app_id, it.name)
        except SteamLoginRequired as e:
            self.history[it.name] = {**old, "error": str(e)}
            log.warning("Steam 成交历史 %s: %s", it.name, e)
            return self._session_rejected(str(e))
        except SteamError as e:
            self.history[it.name] = {**old, "error": str(e)}
            log.warning("Steam 成交历史 %s: %s", it.name, e)
            return True
        sell = sell_price_from_history(hist.points, self.s.steam_sell_window_days)
        if sell is None:
            self.history[it.name] = {**old, "error": f"最近 {self.s.steam_sell_window_days:g} 天没有成交记录"}
            return True
        price, sell_usd = sell.price, None
        if hist.usd and self.s.steam_currency != USD:
            # 登录的是美元区账号：成交历史是美元价，按 Steam 自己的换算率折成人民币
            if not self.steam_rate:
                self.history[it.name] = {**old, "error": "成交历史是美元价，等拿到 Steam 汇率后换算"}
                return True
            sell_usd = sell.price
            price = round(sell.price * self.steam_rate.rate, 2)
        # 换算后仍和当前中位价差太多，多半是钱包币种对不上（既不是人民币也不是美元），这种价不能用
        if sp.median and not 0.5 <= price / sp.median <= 3:
            self.history[it.name] = {**old, "error": "成交历史的币种和 STEAM_CURRENCY 对不上，检查登录账号的钱包区"}
            log.warning("Steam 成交历史 %s: 历史价 %.2f 和当前中位价 %.2f 对不上，疑似币种不一致", it.name, price, sp.median)
            return True
        self.history[it.name] = {"sell": price, "sell_usd": sell_usd, "volume": sell.volume, "sell_at": sell.ts,
                                 "at": time.time(), "error": None}
        return True

    @property
    def usd_wallet(self) -> bool:
        """卖货账号是美元区：Steam 价、净到手都是美元，C5 人民币价 ÷ 到手美元就是汇率，不用再量 Steam 换算率。"""
        return self.s.steam_currency == USD

    def _refresh_rate(self) -> None:
        """用参照饰品查人民币价和美元价算 Steam 汇率，最多每 RATE_REFRESH_SEC 量一次。失败就沿用上次的值。"""
        if self.usd_wallet or self._stop.is_set() or self._steam.blocked_for > 0:
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

    # ---------- Steam 登录 ----------

    @property
    def steam_blocked_for(self) -> float:
        """被 Steam 限流时还要等多少秒，0 = 正常。"""
        return self._steam.blocked_for

    # 登录和续期直连 Steam，不走 STEAM_PROXY：登录是多步流程，轮转代理每步换一个 IP 容易被 Steam 判成异常；
    # 拿到的令牌本身不绑 IP，之后行情、成交历史走代理没问题。

    def steam_login_begin(self, account: str, password: str) -> dict:
        """提交账号密码。返回 {logged_in, guard}；guard 非空表示还要验证码或手机确认。"""
        auth = SteamAuth(timeout=self.s.timeout)
        guards = auth.begin(account, password)
        self._auth = auth
        self.steam_login_error = None
        if not guards:
            return {"logged_in": self.steam_login_poll(), "guard": []}
        return {"logged_in": False, "guard": guards}

    def steam_login_guard(self, code: str) -> bool:
        auth = self._auth
        if auth is None:
            raise SteamLoginError("先提交账号密码")
        auth.guard(code)
        return self.steam_login_poll()

    def steam_login_poll(self) -> bool:
        """登录完成了返回 True；还在等验证返回 False。"""
        auth = self._auth
        if auth is None:
            raise SteamLoginError("没有进行中的登录")
        sess = auth.poll()
        if sess is None:
            return False
        if not sess.access_token:
            sess = renew(sess, timeout=self.s.timeout)
        self._auth = None
        self._set_session(sess)
        log.info("Steam 已登录：%s，挂单价改按最近 %g 天成交历史算", sess.account, self.s.steam_sell_window_days)
        self.request_steam_refresh()
        return True

    def steam_login_cancel(self) -> None:
        self._auth = None

    def steam_logout(self) -> None:
        self._auth = None
        self._set_session(None)
        self.history = {}
        self._apply_targets()
        log.info("Steam 已退出登录，挂单价改按当前最低价算")

    def _set_session(self, sess: SteamSession | None) -> None:
        self.steam_session = sess
        self.steam_login_error = None
        self._steam.set_login(sess.cookie if sess else None)
        save_session(self._session_path, sess)

    def _ensure_session(self) -> bool:
        """有登录态就确保 access token 没过期，快过期就用 refresh token 续。返回现在能不能用登录态。"""
        sess = self.steam_session
        if not sess:
            return False
        now = time.time()
        if sess.refresh_exp and sess.refresh_exp <= now:
            self._set_session(None)
            self.steam_login_error = "Steam 登录已过期，请重新登录"
            log.warning("Steam 登录态过期，请在看板上重新登录")
            return False
        if sess.access_exp - now > ACCESS_RENEW_BEFORE_SEC:
            return True
        try:
            self._set_session(renew(sess, timeout=self.s.timeout))
            log.info("Steam 登录态已续期（%s）", sess.account)
            return True
        except SteamLoginError as e:
            self.steam_login_error = f"续期失败: {e}"
            log.warning("Steam 登录态续期失败: %s", e)
            return sess.access_exp > now

    def _session_rejected(self, reason: str) -> bool:
        """Steam 不认当前 access token：续一次，不行就清掉登录态。返回续成功没有。"""
        sess = self.steam_session
        if sess:
            try:
                self._set_session(renew(sess, timeout=self.s.timeout))
                log.info("Steam 登录态已续期（%s）", sess.account)
                return True
            except SteamLoginError as e:
                reason = f"{reason}（续期失败: {e}）"
        self._set_session(None)
        self.steam_login_error = reason
        log.warning("Steam 登录态失效: %s", reason)
        return False

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
        """目标汇率对应的折扣。美元区钱包时 Steam 价就是美元，折扣 = 目标汇率本身（目标价 = 到手美元 × 目标汇率）。"""
        if self.usd_wallet:
            return rate_discount(self.target_rate, 1.0)
        return rate_discount(self.target_rate, self.steam_rate.rate if self.steam_rate else None)

    def _rate_for_rows(self) -> float | None:
        """给 compare_row 的换算率：美元区钱包是 1（折 × 1 = 汇率），否则是量出来的 Steam 汇率。"""
        if self.usd_wallet:
            return 1.0
        return self.steam_rate.rate if self.steam_rate else None

    def sell_basis(self, it) -> tuple[float | None, str]:
        """算净到手用的 Steam 卖出价：登录后用成交历史里的挂单价（新鲜的），否则用当前最低挂单价。"""
        stale_after = STEAM_STALE_FACTOR * self.s.steam_refresh_sec
        now = time.time()
        h = self.history.get(it.name) or {}
        if h.get("sell") and h.get("at") and now - h["at"] <= stale_after:
            return h["sell"], "history"
        st = self.steam.get(it.name) or {}
        sp = st.get("price")
        return (sp.lowest if sp else None), "lowest"

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
            basis, src = self.sell_basis(it)
            if src == "lowest":
                at = (self.steam.get(it.name) or {}).get("at")
                if not (at and now - at <= stale_after):
                    basis = None
            targets[it.name] = target_price(disc, basis)
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
        rate = self._rate_for_rows()
        auto = sw.auto_targets is not None
        items = []
        for it in sw.items:
            st = stats.get(it.name) or {}
            c5 = c5_lowest(st)
            tgt = sw.target(it)
            t = totals.get(it.name)
            qty, spent = (t.qty, t.spent) if t else (0, 0.0)
            steam = self.steam.get(it.name) or {}
            h = self.history.get(it.name) or {}
            basis, src = self.sell_basis(it)
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
                **asdict(compare_row(it, c5, steam.get("price"), target=tgt, rate=rate, sell=basis)),
                "status": status, "pause_until": pause_until.get(it.name),
                "sell_count": st.get("sellCount"), "purchase_max": st.get("purchaseMaxPrice"),
                "max_price": it.max_price, "target_auto": auto,
                "sell_src": src, "sell_volume": h.get("volume"), "sell_at": h.get("sell_at"), "sell_usd": h.get("sell_usd"),
                "history_error": h.get("error"),
                "max_qty": it.max_qty, "max_spend": it.max_spend, "bought": qty, "spent": spent,
                "steam_at": steam.get("at"), "steam_error": steam.get("error"),
            })
        sess = self.steam_session
        auth = self._auth
        return {
            "now": now, "started_at": self.started_at, "version": __version__,
            "mode": self.s.mode, "strategy": self.s.strategy, "paused": sw.paused,
            "poll_interval": self.s.poll_interval, "steam_refresh_sec": self.s.steam_refresh_sec,
            "sell_window_days": self.s.steam_sell_window_days,
            "steam_currency": self.s.steam_currency, "usd_wallet": self.usd_wallet,
            "cycle": {"n": self.cycle_n, "at": self.cycle_at, "error": self.cycle_error,
                      "done": bool(view.get("done"))},
            "budget": {"total": self.s.max_total_spend,
                       "spent": sum(t.spent for t in totals.values()),
                       "qty": sum(t.qty for t in totals.values())},
            "balance": {"value": self.balance, "at": self.balance_at},
            "steam": {"at": self.steam_at, "busy": self.steam_busy, "blocked_for": self.steam_blocked_for},
            "steam_login": {"account": sess.account if sess else None, "steamid": sess.steamid if sess else None,
                            "access_exp": sess.access_exp if sess else None,
                            "refresh_exp": sess.refresh_exp if sess else None,
                            "error": self.steam_login_error, "pending": auth is not None,
                            "guard": [GUARD_NAMES[g] for g in auth.guards] if auth else []},
            "rate": {"target": self.target_rate, "discount": self.discount(),
                     "steam": asdict(self.steam_rate) if self.steam_rate else None,
                     "steam_error": self.steam_rate_error},
            "items": items,
            "purchases": [asdict(p) for p in reversed(purchases)],
            "logs": list(self.logs.lines),
        }


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


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
        raw = (await _json_body(request)).get("rate")
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

    # ---- Steam 登录：这几个会去请求 Steam，放线程池里跑，不卡看板 ----

    @app.post("/api/steam/login")
    async def steam_login(request: Request) -> JSONResponse:
        body = await _json_body(request)
        account, password = str(body.get("account") or "").strip(), str(body.get("password") or "")
        if not account or not password:
            return JSONResponse({"ok": False, "error": "账号和密码都要填"}, status_code=400)
        try:
            result = await run_in_threadpool(dash.steam_login_begin, account, password)
        except SteamLoginError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, **result})

    @app.post("/api/steam/guard")
    async def steam_guard(request: Request) -> JSONResponse:
        code = str((await _json_body(request)).get("code") or "")
        try:
            logged_in = await run_in_threadpool(dash.steam_login_guard, code)
        except SteamLoginError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "logged_in": logged_in})

    @app.post("/api/steam/login/poll")
    async def steam_login_poll() -> JSONResponse:
        try:
            logged_in = await run_in_threadpool(dash.steam_login_poll)
        except SteamLoginError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "logged_in": logged_in})

    @app.post("/api/steam/login/cancel")
    async def steam_login_cancel() -> JSONResponse:
        dash.steam_login_cancel()
        return JSONResponse({"ok": True})

    @app.post("/api/steam/logout")
    async def steam_logout() -> JSONResponse:
        dash.steam_logout()
        return JSONResponse({"ok": True})

    return app


def serve_in_thread(app: FastAPI, host: str, port: int) -> threading.Thread:
    """看板跑在守护线程里，主线程留给扫货循环（Ctrl+C 也由主线程处理）。"""
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, name="web", daemon=True)
    t.start()
    return t
