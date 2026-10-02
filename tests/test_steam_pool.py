from __future__ import annotations

import json

import pytest

from c5bot.steam_login import SteamLoginError, SteamSession
from c5bot.steam_pool import ACCESS_RENEW_BEFORE_SEC, COOLDOWN_SEC, SteamPool
from tests.test_web import jwt

NOW = 1_800_000_000.0


def sess(i: int, *, access_exp: float = NOW + 86400, refresh_exp: float = NOW + 90 * 86400) -> SteamSession:
    return SteamSession(f"7656{i}", f"acc{i}", jwt(refresh_exp), jwt(access_exp))


@pytest.fixture
def pool(tmp_path):
    renewed = []

    def fake_renew(s, *, timeout=15.0):
        if s.account == "acc-dead":
            raise SteamLoginError("Steam 不再接受这个登录态")
        renewed.append(s.account)
        return SteamSession(s.steamid, s.account, s.refresh_token, jwt(NOW + 86400))
    p = SteamPool(tmp_path / "steam_sessions.json", renew=fake_renew, clock=lambda: NOW)
    p.renewed = renewed
    return p


def test_add_replace_remove_and_persist(pool, tmp_path):
    pool.add(sess(1))
    pool.add(sess(2))
    pool.add(SteamSession("76561", "acc1", "r2", "a2"))     # 同一账号重新登录：替换
    assert [a.session.account for a in pool.accounts] == ["acc1", "acc2"]
    assert pool.get("76561").session.access_token == "a2"
    saved = json.loads((tmp_path / "steam_sessions.json").read_text(encoding="utf-8"))
    assert [d["steamid"] for d in saved] == ["76561", "76562"]
    assert pool.remove("76561") and not pool.remove("nope")
    reloaded = SteamPool(tmp_path / "steam_sessions.json", clock=lambda: NOW)
    assert [a.session.account for a in reloaded.accounts] == ["acc2"]
    pool.clear()
    assert not (tmp_path / "steam_sessions.json").exists()


def test_loads_legacy_single_account_file(tmp_path):
    path = tmp_path / "steam_sessions.json"
    path.write_text(json.dumps({"steamid": "1", "account": "old", "refresh_token": "r", "access_token": "a"}), encoding="utf-8")
    p = SteamPool(path, clock=lambda: NOW)
    assert len(p) == 1 and p.accounts[0].session.account == "old"
    path.write_text("garbage", encoding="utf-8")
    assert len(SteamPool(path)) == 0


def test_pick_round_robin_skips_cooldown_and_dead(pool):
    for i in (1, 2, 3):
        pool.add(sess(i))
    assert [pool.pick().session.account for _ in range(4)] == ["acc1", "acc2", "acc3", "acc1"]
    pool.cooldown("76562", "被限流")
    assert [pool.pick().session.account for _ in range(3)] == ["acc3", "acc1", "acc3"]
    assert pool.get("76562").error == "被限流"
    assert [a.session.account for a in pool.usable()] == ["acc1", "acc3"]
    st = {a["account"]: a for a in pool.status()}
    assert st["acc2"]["cooldown_for"] == pytest.approx(COOLDOWN_SEC) and st["acc1"]["requests"] == 3
    assert st["acc3"]["last_used"] == NOW and st["acc2"]["dead"] is False


def test_pick_renews_expiring_token_and_marks_expired(pool):
    pool.add(sess(1, access_exp=NOW + ACCESS_RENEW_BEFORE_SEC - 10))   # 快过期：取用前续
    pool.add(sess(2, refresh_exp=NOW - 1))                             # refresh token 过期：标死
    a = pool.pick()
    assert a.session.account == "acc1" and pool.renewed == ["acc1"] and a.session.access_exp == NOW + 86400
    assert pool.pick().session.account == "acc1"                       # acc2 跳过
    assert pool.get("76562").dead and "过期" in pool.get("76562").error
    assert [a.session.account for a in pool.usable()] == ["acc1"]


def test_pick_keeps_using_account_when_renew_fails_but_token_still_valid(pool):
    pool.add(SteamSession("7656d", "acc-dead", jwt(NOW + 90 * 86400), jwt(NOW + 600)))   # 10 分钟后过期，续不了
    a = pool.pick()
    assert a is not None and "续期失败" in a.error     # 还能用 10 分钟
    pool.accounts[0].session = SteamSession("7656d", "acc-dead", jwt(NOW + 90 * 86400), jwt(NOW - 1))
    assert pool.pick() is None                          # 过期又续不了：没有可用账号


def test_rejected_renews_or_marks_dead(pool):
    pool.add(sess(1))
    pool.add(SteamSession("7656d", "acc-dead", jwt(NOW + 90 * 86400), jwt(NOW + 86400)))
    assert pool.rejected("76561", "登录态失效") is True and pool.renewed == ["acc1"]
    assert pool.rejected("7656d", "登录态失效") is False
    dead = pool.get("7656d")
    assert dead.dead and "登录态失效" in dead.error and "续期失败" in dead.error
    assert [a.session.account for a in pool.usable()] == ["acc1"]
    assert pool.rejected("nope", "x") is False
