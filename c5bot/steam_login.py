"""Steam 网页登录（2023 年起的 JWT 流程），给看板上的“Steam 登录”用。

流程和 steampy / node-steam-session 一致：
  GetPasswordRSAPublicKey -> BeginAuthSessionViaCredentials -> UpdateAuthSessionWithSteamGuardCode（令牌 / 邮箱验证码，
  手机 App 上点确认则不用）-> PollAuthSessionStatus 拿到 refresh_token + access_token。
steamLoginSecure cookie 就是 "steamid||access_token"。access_token 一天左右过期，用 refresh_token 调
GenerateAccessTokenForApp 续；refresh_token 能用几个月，过期才需要重新登录。只存 token，密码用完即丢。
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import requests
import rsa

API = "https://api.steampowered.com/IAuthenticationService"
COMMUNITY = "https://steamcommunity.com"
# EAuthSessionGuardType：2 邮箱验证码 / 3 手机令牌验证码 / 4 手机 App 确认 / 5 邮件里点确认
GUARD_NAMES = {2: "email", 3: "device_code", 4: "device_confirmation", 5: "email_confirmation"}
CODE_TYPES = (3, 2)     # 提交验证码时优先按手机令牌，其次邮箱
ERESULT = {
    5: "密码错误", 20: "Steam 服务暂时不可用", 63: "需要 Steam 令牌验证", 65: "验证码错误",
    84: "尝试太频繁，过几分钟再试", 85: "需要两步验证", 87: "账号被限制登录", 88: "手机令牌验证码不对",
}


class SteamLoginError(Exception):
    pass


@dataclass(frozen=True)
class SteamSession:
    steamid: str
    account: str
    refresh_token: str
    access_token: str

    @property
    def cookie(self) -> str:
        """steamLoginSecure 的值。"""
        return f"{self.steamid}%7C%7C{self.access_token}"

    @property
    def access_exp(self) -> float:
        return jwt_exp(self.access_token)

    @property
    def refresh_exp(self) -> float:
        return jwt_exp(self.refresh_token)


def jwt_exp(token: str) -> float:
    """JWT 里的过期时间（秒级时间戳），解析不了返回 0。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, ValueError, KeyError, TypeError):
        return 0.0


def _http(proxy: str | None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Referer": COMMUNITY + "/", "Origin": COMMUNITY,
                      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) c5bot"})
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def _check(resp: requests.Response) -> dict:
    """Steam 的错误放在 X-eresult 头里，HTTP 仍是 200。返回 json 里的 response 字段。"""
    code = str(resp.headers.get("x-eresult", "1"))
    if code != "1":
        raise SteamLoginError(ERESULT.get(int(code) if code.isdigit() else 0, f"Steam 返回错误 {code}"))
    if resp.status_code != 200:
        raise SteamLoginError(f"Steam HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise SteamLoginError("Steam 返回内容无法识别") from None
    return (data.get("response") if isinstance(data, dict) else None) or {}


class SteamAuth:
    """一次登录：begin -> guard（需要验证码时）-> poll。"""

    def __init__(self, *, proxy: str | None = None, timeout: float = 15.0):
        self._http = _http(proxy)
        self._timeout = timeout
        self.client_id: str | None = None
        self.request_id: str | None = None
        self.steamid: str | None = None
        self.account: str | None = None
        self.guards: list[int] = []     # Steam 允许的验证方式，见 GUARD_NAMES

    def _call(self, method: str, endpoint: str, **kw) -> dict:
        try:
            return _check(self._http.request(method, f"{API}/{endpoint}/v1/", timeout=self._timeout, **kw))
        except requests.RequestException as e:
            raise SteamLoginError(f"连不上 Steam: {type(e).__name__}") from None

    def begin(self, account: str, password: str) -> list[str]:
        """提交账号密码。返回还需要的验证方式名（空列表 = 不需要验证，直接 poll）。"""
        key = self._call("GET", "GetPasswordRSAPublicKey", params={"account_name": account})
        try:
            pub = rsa.PublicKey(int(key["publickey_mod"], 16), int(key["publickey_exp"], 16))
        except (KeyError, ValueError):
            raise SteamLoginError("Steam 没有返回公钥") from None
        encrypted = base64.b64encode(rsa.encrypt(password.encode("utf-8"), pub)).decode()
        resp = self._call("POST", "BeginAuthSessionViaCredentials", data={
            "account_name": account, "encrypted_password": encrypted, "encryption_timestamp": key["timestamp"],
            "remember_login": "true", "persistence": "1", "platform_type": "2", "website_id": "Community",
            "device_friendly_name": "c5bot 看板",
        })
        if not resp.get("client_id"):
            raise SteamLoginError("账号或密码错误")
        self.client_id = str(resp["client_id"])
        self.request_id = str(resp.get("request_id") or "")
        self.steamid = str(resp.get("steamid") or "")
        self.account = account
        self.guards = [int(c.get("confirmation_type") or 0) for c in resp.get("allowed_confirmations") or []]
        self.guards = [g for g in self.guards if g in GUARD_NAMES]
        return [GUARD_NAMES[g] for g in self.guards]

    def guard(self, code: str) -> None:
        """提交手机令牌或邮箱验证码。"""
        if not self.client_id:
            raise SteamLoginError("先提交账号密码")
        code = code.strip()
        if not code:
            raise SteamLoginError("验证码不能为空")
        code_type = next((t for t in CODE_TYPES if t in self.guards), CODE_TYPES[0])
        self._call("POST", "UpdateAuthSessionWithSteamGuardCode", data={
            "client_id": self.client_id, "steamid": self.steamid, "code": code, "code_type": str(code_type)})

    def poll(self) -> SteamSession | None:
        """问 Steam 登录完成没有。还没完成（没填验证码 / 手机上还没确认）返回 None。"""
        if not self.client_id:
            raise SteamLoginError("先提交账号密码")
        resp = self._call("POST", "PollAuthSessionStatus", data={"client_id": self.client_id, "request_id": self.request_id})
        if resp.get("new_client_id"):
            self.client_id = str(resp["new_client_id"])
        if not resp.get("refresh_token"):
            return None
        return SteamSession(steamid=self.steamid or "", account=str(resp.get("account_name") or self.account),
                            refresh_token=str(resp["refresh_token"]), access_token=str(resp.get("access_token") or ""))


def renew(sess: SteamSession, *, proxy: str | None = None, timeout: float = 15.0) -> SteamSession:
    """用 refresh_token 换新的 access_token。renewal_type=1 表示 Steam 愿意的话顺便给个新的 refresh_token。"""
    try:
        resp = _check(_http(proxy).post(f"{API}/GenerateAccessTokenForApp/v1/", timeout=timeout, data={
            "refresh_token": sess.refresh_token, "steamid": sess.steamid, "renewal_type": "1"}))
    except requests.RequestException as e:
        raise SteamLoginError(f"连不上 Steam: {type(e).__name__}") from None
    if not resp.get("access_token"):
        raise SteamLoginError("Steam 不再接受这个登录态，请重新登录")
    return replace(sess, access_token=str(resp["access_token"]),
                   refresh_token=str(resp.get("refresh_token") or sess.refresh_token))


def load_session(path: Path) -> SteamSession | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return SteamSession(**{k: str(d[k]) for k in ("steamid", "account", "refresh_token", "access_token")})
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_session(path: Path, sess: SteamSession | None) -> None:
    """None = 退出登录，删文件。文件里是长期有效的登录令牌，权限只留给自己。"""
    if sess is None:
        path.unlink(missing_ok=True)
        return
    path.write_text(json.dumps(asdict(sess)), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
