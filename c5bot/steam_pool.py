"""Steam 账号池：看板上登录多个账号，查成交历史时轮着用。

配合轮转代理（每个请求换出口 IP），每个账号每小时只发一两个请求，谁都不会被 Steam 盯上。
某个账号被 429 就让它歇一会儿换下一个；access token 快过期时在取用前顺手续掉；
refresh token 过期或 Steam 不再认的账号标成“需要重新登录”，留在列表里给用户看，不再使用。
账号列表存 data/steam_sessions.json（权限 600），只有令牌没有密码。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import steam_login
from .steam_login import SteamLoginError, SteamSession

log = logging.getLogger(__name__)

ACCESS_RENEW_BEFORE_SEC = 3600.0    # access token 剩不到 1 小时就续
COOLDOWN_SEC = 600.0                # 查成交历史被 429 的账号歇多久


@dataclass
class Account:
    session: SteamSession
    error: str | None = None        # 最近一次问题（续期失败 / 被限流 / 需要重新登录）
    cooldown_until: float = 0.0     # 这个时间之前不用它
    dead: bool = False              # 登录态彻底失效，等用户重新登录
    requests: int = 0               # 用它发了多少次成交历史请求
    last_used: float = 0.0


class SteamPool:
    def __init__(self, path: Path, *, timeout: float = 15.0, renew=None, clock=time.time):
        self._path = path
        self._timeout = timeout
        self._renew_fn = renew          # 测试可注入；默认用 steam_login.renew（运行时取，便于打补丁）
        self._now = clock
        self.accounts: list[Account] = []
        self._cursor = 0
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):     # 旧版单账号文件
            data = [data]
        if not isinstance(data, list):
            return
        for d in data:
            try:
                sess = SteamSession(**{k: str(d[k]) for k in ("steamid", "account", "refresh_token", "access_token")})
            except (KeyError, TypeError):
                continue
            if not self.get(sess.steamid):
                self.accounts.append(Account(sess))

    def save(self) -> None:
        if not self.accounts:
            self._path.unlink(missing_ok=True)
            return
        self._path.write_text(json.dumps([asdict(a.session) for a in self.accounts]), encoding="utf-8")
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    # ---------- 增删 ----------

    def __len__(self) -> int:
        return len(self.accounts)

    def get(self, steamid: str) -> Account | None:
        return next((a for a in self.accounts if a.session.steamid == steamid), None)

    def add(self, sess: SteamSession) -> Account:
        """登录成功后加进来；同一个账号重新登录就替换旧登录态。"""
        acc = self.get(sess.steamid)
        if acc:
            acc.session, acc.error, acc.cooldown_until, acc.dead = sess, None, 0.0, False
        else:
            acc = Account(sess)
            self.accounts.append(acc)
        self.save()
        return acc

    def remove(self, steamid: str) -> bool:
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if a.session.steamid != steamid]
        if len(self.accounts) != before:
            self.save()
            return True
        return False

    def clear(self) -> None:
        self.accounts = []
        self.save()

    # ---------- 取用 ----------

    def _renew(self, sess: SteamSession, *, timeout: float) -> SteamSession:
        return (self._renew_fn or steam_login.renew)(sess, timeout=timeout)

    def usable(self) -> list[Account]:
        now = self._now()
        return [a for a in self.accounts if not a.dead and a.cooldown_until <= now
                and not (a.session.refresh_exp and a.session.refresh_exp <= now)]

    def pick(self) -> Account | None:
        """轮流取下一个能用的账号，取用前把快过期的 access token 续掉。没有能用的返回 None。"""
        n = len(self.accounts)
        now = self._now()
        for _ in range(n):
            acc = self.accounts[self._cursor % n]
            self._cursor += 1
            if acc.dead or acc.cooldown_until > now:
                continue
            sess = acc.session
            if sess.refresh_exp and sess.refresh_exp <= now:
                acc.dead, acc.error = True, "登录已过期，请重新登录"
                log.warning("Steam 账号 %s 登录已过期，请在看板上重新登录", sess.account)
                continue
            if sess.access_exp - now <= ACCESS_RENEW_BEFORE_SEC:
                try:
                    acc.session = self._renew(sess, timeout=self._timeout)
                    acc.error = None
                    self.save()
                    log.info("Steam 账号 %s 登录态已续期", sess.account)
                except SteamLoginError as e:
                    acc.error = f"续期失败: {e}"
                    log.warning("Steam 账号 %s 续期失败: %s", sess.account, e)
                    if sess.access_exp <= now:
                        continue
            acc.requests += 1
            acc.last_used = now
            return acc
        return None

    def cooldown(self, steamid: str, reason: str, sec: float = COOLDOWN_SEC) -> None:
        acc = self.get(steamid)
        if acc:
            acc.cooldown_until = self._now() + sec
            acc.error = reason

    def rejected(self, steamid: str, reason: str) -> bool:
        """Steam 不认这个账号的 access token：续一次，续不了就标成需要重新登录。返回续成功没有。"""
        acc = self.get(steamid)
        if not acc:
            return False
        try:
            acc.session = self._renew(acc.session, timeout=self._timeout)
            acc.error = None
            self.save()
            log.info("Steam 账号 %s 登录态已续期", acc.session.account)
            return True
        except SteamLoginError as e:
            acc.dead, acc.error = True, f"{reason}（续期失败: {e}）"
            log.warning("Steam 账号 %s 登录态失效: %s", acc.session.account, acc.error)
            return False

    def status(self) -> list[dict]:
        now = self._now()
        return [{
            "steamid": a.session.steamid, "account": a.session.account,
            "access_exp": a.session.access_exp, "refresh_exp": a.session.refresh_exp,
            "error": a.error, "dead": a.dead, "cooldown_for": max(0.0, a.cooldown_until - now),
            "requests": a.requests, "last_used": a.last_used or None,
        } for a in self.accounts]
