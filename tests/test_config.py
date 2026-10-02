from __future__ import annotations

import pytest

from c5bot.config import load_settings, load_watchlist

ENV_KEYS = ("C5_APP_KEY", "C5_MODE", "C5_LIVE_CONFIRM", "C5_TRADE_URL", "C5_MAX_TOTAL_SPEND",
            "C5_STRATEGY", "C5_DATA_DIR", "C5_START_PAUSED", "STEAM_RATE_ITEM")
TRADE_URL = "https://steamcommunity.com/tradeoffer/new/?partner=1&token=x"


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("C5_DATA_DIR", str(tmp_path))

    def load(**values):
        for k, v in values.items():
            monkeypatch.setenv(k, v)
        return load_settings(tmp_path / "missing.env")
    return load


def test_defaults_to_dry(env):
    s = env(C5_APP_KEY="k")
    assert s.mode == "dry" and s.strategy == "listing" and s.start_paused is False
    assert env(C5_APP_KEY="k", C5_START_PAUSED="yes").start_paused is True


def test_app_key_required(env):
    with pytest.raises(SystemExit):
        env()


@pytest.mark.parametrize("missing", ["C5_LIVE_CONFIRM", "C5_TRADE_URL", "C5_MAX_TOTAL_SPEND"])
def test_live_needs_confirm_trade_url_and_budget(env, missing):
    full = {"C5_APP_KEY": "k", "C5_MODE": "live", "C5_LIVE_CONFIRM": "yes",
            "C5_TRADE_URL": TRADE_URL, "C5_MAX_TOTAL_SPEND": "100"}
    assert env(**full).mode == "live"
    full[missing] = ""
    with pytest.raises(SystemExit):
        env(**full)


def test_watchlist_defaults_and_overrides(tmp_path):
    f = tmp_path / "watchlist.toml"
    f.write_text("""
[defaults]
delivery = 2

[[items]]
name = "Kilowatt Case"
max_price = 5
max_qty = 10

[[items]]
name = "Revolution Case"
max_price = 3.2
max_qty = 20
delivery = 0
max_spend = 50
""", encoding="utf-8")
    a, b = load_watchlist(f)
    assert (a.name, a.max_price, a.max_qty, a.delivery, a.app_id) == ("Kilowatt Case", 5.0, 10, 2, 730)
    assert (b.delivery, b.max_spend) == (0, 50.0)


@pytest.mark.parametrize("body", [
    '[[items]]\nname = "A"\nmax_price = 0\nmax_qty = 1',                         # 价格非法
    '[[items]]\nname = "A"\nmax_price = 1\nmax_qty = 1\nprice_max = 2',           # 拼错的字段
    '[[items]]\nname = "A"\nmax_price = 1\nmax_qty = 1\n' * 2,                    # 重复
    '[defaults]\napp_id = 730',                                                   # 没有 items
])
def test_watchlist_rejects_bad_input(tmp_path, body):
    f = tmp_path / "watchlist.toml"
    f.write_text(body, encoding="utf-8")
    with pytest.raises(SystemExit):
        load_watchlist(f)


def test_steam_rate_item_default_and_override(env):
    assert env(C5_APP_KEY="k").steam_rate_item == "AK-47 | Redline (Field-Tested)"
    assert env(C5_APP_KEY="k", STEAM_RATE_ITEM=" Glock-18 | Fade (Factory New) ").steam_rate_item == "Glock-18 | Fade (Factory New)"
