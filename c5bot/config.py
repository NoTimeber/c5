"""从 .env 和 watchlist.toml 读取配置。"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

MODES = ("dry", "live")
STRATEGIES = ("listing", "quick")
# 量 Steam 人民币/美元换算率用的参照饰品。要贵：Steam 换算后向上取整到分，几十美元的饰品误差 0.03% 以内
STEAM_RATE_ITEM_DEFAULT = "AK-47 | Redline (Field-Tested)"


@dataclass(frozen=True)
class WatchItem:
    name: str               # Steam marketHashName（英文名），如 Kilowatt Case
    max_price: float        # 单价不高于此值才买
    max_qty: int            # 累计最多买多少个，重启不清零
    max_spend: float = 0.0  # 该饰品累计花费上限，0 不限制
    app_id: int = 730       # 730 = CS2
    delivery: int = 0       # 0 不限 / 1 只要人工发货 / 2 只要自动发货
    asset_type: int = 1     # 1 普通在售 / 2 冷却期在售


@dataclass(frozen=True)
class Settings:
    app_key: str
    mode: str = "dry"                   # dry：只看不买，模拟记账；live：真实下单
    strategy: str = "listing"           # listing：查在售列表后批量买；quick：快速购买接口，一次一件
    trade_url: str | None = None        # 收货 Steam 账号的交易链接
    proxy: str | None = None            # http(s)://host:port
    poll_interval: float = 3.0
    timeout: float = 10.0
    max_total_spend: float = 0.0        # 所有饰品累计花费上限，实盘必填；dry 下 0 表示不限制
    max_buy_per_cycle: int = 10         # 每轮最多下几单
    min_balance_reserve: float = 0.0    # 账户余额至少留这么多不动
    error_cooldown: float = 10.0        # 某个饰品下单被拒后暂停多少秒
    data_dir: Path = ROOT / "data"
    log_level: str = "INFO"
    steam_proxy: str | None = None      # 访问 Steam 市场用的代理，跟 C5 分开配
    steam_currency: int = 23            # Steam 钱包币种，23 = 人民币
    steam_refresh_sec: float = 600.0    # 看板多久刷新一次 Steam 价
    steam_rate_item: str = STEAM_RATE_ITEM_DEFAULT  # 量 Steam 汇率用的参照饰品（Steam 市场英文名）
    steam_sell_window_days: float = 3.0  # 登录 Steam 后，挂单价按最近几天的成交历史算
    steam_sell_volume_share: float = 0.3  # 挂单价 = 从高价往下累计成交量达到总量这个比例时的价（0.3 = 30% 的成交在此价或更高）
    steam_user_agent: str = "browser"     # 访问 Steam 用的 UA：browser 真实浏览器 UA + 配套头（默认）/ bot 老实报名字 / 其它 = 固定字符串
    steam_source: str = "page"            # page：抓饰品市场页解析（匿名、不被按 IP 封接口，默认）；api：priceoverview + 登录账号查 pricehistory
    ui_host: str = "127.0.0.1"          # 看板监听地址
    ui_port: int = 8766                 # 0 = 不开看板
    start_paused: bool = False          # 启动时先暂停扫货只看行情，等看板上点“继续”


def _float(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or ROOT / ".env")
    app_key = (os.getenv("C5_APP_KEY") or "").strip()
    if not app_key:
        raise SystemExit("缺少 C5_APP_KEY。请复制 .env.example 为 .env 并填写。")
    mode = (os.getenv("C5_MODE") or "dry").strip().lower()
    if mode not in MODES:
        raise SystemExit(f"C5_MODE 只能是 {'/'.join(MODES)}，收到: {mode}")
    strategy = (os.getenv("C5_STRATEGY") or "listing").strip().lower()
    if strategy not in STRATEGIES:
        raise SystemExit(f"C5_STRATEGY 只能是 {'/'.join(STRATEGIES)}，收到: {strategy}")
    trade_url = (os.getenv("C5_TRADE_URL") or "").strip() or None
    max_total_spend = _float("C5_MAX_TOTAL_SPEND", 0.0)
    if mode == "live":
        if (os.getenv("C5_LIVE_CONFIRM") or "").strip().lower() not in ("yes", "true", "1"):
            raise SystemExit("实盘模式需要在 .env 里显式写 C5_LIVE_CONFIRM=yes")
        if not trade_url or not trade_url.startswith("https://steamcommunity.com/tradeoffer/new/"):
            raise SystemExit("实盘模式需要 C5_TRADE_URL（https://steamcommunity.com/tradeoffer/new/?partner=...&token=...）")
        if max_total_spend <= 0:
            raise SystemExit("实盘模式必须设置 C5_MAX_TOTAL_SPEND（总预算）")
    data_dir = Path(os.getenv("C5_DATA_DIR") or ROOT / "data").expanduser()
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_key=app_key,
        mode=mode,
        strategy=strategy,
        trade_url=trade_url,
        proxy=os.getenv("C5_PROXY") or None,
        poll_interval=_float("C5_POLL_INTERVAL_SEC", 3.0),
        timeout=_float("C5_TIMEOUT_SEC", 10.0),
        max_total_spend=max_total_spend,
        max_buy_per_cycle=int(_float("C5_MAX_BUY_PER_CYCLE", 10)),
        min_balance_reserve=_float("C5_MIN_BALANCE_RESERVE", 0.0),
        error_cooldown=_float("C5_ERROR_COOLDOWN_SEC", 10.0),
        data_dir=data_dir,
        log_level=(os.getenv("C5_LOG_LEVEL") or "INFO").upper(),
        steam_proxy=os.getenv("STEAM_PROXY") or None,
        steam_currency=int(_float("STEAM_CURRENCY", 23)),
        steam_refresh_sec=_float("STEAM_REFRESH_SEC", 600.0),
        steam_rate_item=(os.getenv("STEAM_RATE_ITEM") or "").strip() or STEAM_RATE_ITEM_DEFAULT,
        steam_sell_window_days=_float("STEAM_SELL_WINDOW_DAYS", 3.0),
        steam_sell_volume_share=min(max(_float("STEAM_SELL_VOLUME_SHARE", 0.3), 0.001), 1.0),
        steam_user_agent=(os.getenv("STEAM_USER_AGENT") or "").strip() or "browser",
        steam_source="api" if (os.getenv("STEAM_SOURCE") or "").strip().lower() == "api" else "page",
        ui_host=os.getenv("C5_UI_HOST") or "127.0.0.1",
        ui_port=int(_float("C5_UI_PORT", 8766)),
        start_paused=(os.getenv("C5_START_PAUSED") or "").strip().lower() in ("yes", "true", "1"),
    )


def load_watchlist(path: Path | None = None) -> list[WatchItem]:
    path = path or ROOT / "watchlist.toml"
    if not path.exists():
        raise SystemExit(f"找不到 {path.name}。请复制 watchlist.example.toml 为 watchlist.toml 并填写。")
    with path.open("rb") as f:
        doc = tomllib.load(f)
    defaults = doc.get("defaults") or {}
    items: list[WatchItem] = []
    for i, raw in enumerate(doc.get("items") or [], 1):
        try:
            it = WatchItem(**{**defaults, **raw})
            it = WatchItem(
                name=str(it.name).strip(), max_price=float(it.max_price), max_qty=int(it.max_qty),
                max_spend=float(it.max_spend), app_id=int(it.app_id),
                delivery=int(it.delivery), asset_type=int(it.asset_type),
            )
        except (TypeError, ValueError) as e:
            raise SystemExit(f"{path.name} 第 {i} 项有误: {e}")
        if not it.name or it.max_price <= 0 or it.max_qty <= 0:
            raise SystemExit(f"{path.name} 第 {i} 项有误: name 不能为空，max_price / max_qty 必须大于 0")
        if it.delivery not in (0, 1, 2) or it.asset_type not in (1, 2):
            raise SystemExit(f"{path.name} 第 {i} 项有误: delivery 只能是 0/1/2，asset_type 只能是 1/2")
        if any(it.name == other.name for other in items):
            raise SystemExit(f"{path.name} 里 {it.name} 重复了")
        items.append(it)
    if not items:
        raise SystemExit(f"{path.name} 里没有 [[items]]")
    return items
