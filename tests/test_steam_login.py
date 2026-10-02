from __future__ import annotations

import base64
import json

import pytest
import rsa

from c5bot.steam_login import (
    SteamAuth,
    SteamLoginError,
    SteamSession,
    jwt_exp,
    load_session,
    renew,
    save_session,
)


def jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


class Resp:
    def __init__(self, body=None, *, eresult="1", status=200):
        self._body = body
        self.headers = {"x-eresult": eresult}
        self.status_code = status

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


PUB, PRIV = rsa.newkeys(512)


@pytest.fixture
def auth(monkeypatch):
    a = SteamAuth()
    calls = []
    replies = {}

    def request(method, url, *, timeout, **kw):
        endpoint = url.rstrip("/").rsplit("/", 2)[-2]
        calls.append((endpoint, kw.get("params") or kw.get("data") or {}))
        r = replies[endpoint]
        return r() if callable(r) else r
    monkeypatch.setattr(a._http, "request", request)
    a.calls, a.replies = calls, replies
    return a


def rsa_reply():
    return Resp({"response": {"publickey_mod": format(PUB.n, "x"), "publickey_exp": format(PUB.e, "x"), "timestamp": "123"}})


def test_jwt_exp():
    assert jwt_exp(jwt(1700000000)) == 1700000000
    assert jwt_exp("garbage") == 0 and jwt_exp("") == 0


def test_session_cookie_and_expiry():
    s = SteamSession("76561198000000000", "me", jwt(2000000000), jwt(1900000000))
    assert s.cookie == "76561198000000000%7C%7C" + s.access_token
    assert s.access_exp == 1900000000 and s.refresh_exp == 2000000000


def test_begin_encrypts_password_and_reports_guards(auth):
    auth.replies["GetPasswordRSAPublicKey"] = rsa_reply()
    auth.replies["BeginAuthSessionViaCredentials"] = Resp({"response": {
        "client_id": 111, "request_id": "req", "steamid": "7656",
        "allowed_confirmations": [{"confirmation_type": 4}, {"confirmation_type": 3}, {"confirmation_type": 99}]}})
    assert auth.begin("me", "pw-秘密") == ["device_confirmation", "device_code"]
    (_, key_params), (_, begin_data) = auth.calls
    assert key_params == {"account_name": "me"}
    assert rsa.decrypt(base64.b64decode(begin_data["encrypted_password"]), PRIV).decode() == "pw-秘密"
    assert begin_data["encryption_timestamp"] == "123" and begin_data["account_name"] == "me"
    assert auth.client_id == "111" and auth.steamid == "7656" and auth.guards == [4, 3]


def test_begin_wrong_password(auth):
    auth.replies["GetPasswordRSAPublicKey"] = rsa_reply()
    auth.replies["BeginAuthSessionViaCredentials"] = Resp({"response": {}}, eresult="5")
    with pytest.raises(SteamLoginError, match="密码错误"):
        auth.begin("me", "x")
    auth.replies["BeginAuthSessionViaCredentials"] = Resp({"response": {}})
    with pytest.raises(SteamLoginError, match="账号或密码"):
        auth.begin("me", "x")


def test_guard_uses_device_code_type_then_poll(auth):
    auth.replies["GetPasswordRSAPublicKey"] = rsa_reply()
    auth.replies["BeginAuthSessionViaCredentials"] = Resp({"response": {
        "client_id": 1, "request_id": "r", "steamid": "7656", "allowed_confirmations": [{"confirmation_type": 2}]}})
    auth.begin("me", "x")
    auth.replies["UpdateAuthSessionWithSteamGuardCode"] = Resp({"response": {}})
    auth.guard(" ABCDE ")
    assert auth.calls[-1] == ("UpdateAuthSessionWithSteamGuardCode",
                              {"client_id": "1", "steamid": "7656", "code": "ABCDE", "code_type": "2"})
    polls = iter([Resp({"response": {"new_client_id": 2}}),
                  Resp({"response": {"refresh_token": jwt(2e9), "access_token": jwt(1.9e9), "account_name": "Me"}})])
    auth.replies["PollAuthSessionStatus"] = lambda: next(polls)
    assert auth.poll() is None and auth.client_id == "2"
    sess = auth.poll()
    assert sess.account == "Me" and sess.steamid == "7656" and sess.refresh_exp == 2e9
    with pytest.raises(SteamLoginError, match="验证码"):
        auth.guard("   ")


def test_guard_code_error_and_before_begin(auth):
    with pytest.raises(SteamLoginError):
        auth.guard("1")
    with pytest.raises(SteamLoginError):
        auth.poll()
    auth.client_id, auth.steamid, auth.guards = "1", "7656", [3]
    auth.replies["UpdateAuthSessionWithSteamGuardCode"] = Resp({"response": {}}, eresult="88")
    with pytest.raises(SteamLoginError, match="令牌"):
        auth.guard("00000")


def test_renew(monkeypatch):
    sess = SteamSession("7656", "me", jwt(2e9), jwt(1e9))
    seen = {}

    class Http:
        def post(self, url, *, timeout, data):
            seen.update(data)
            return Resp({"response": {"access_token": jwt(1.95e9)}})
    monkeypatch.setattr("c5bot.steam_login._http", lambda proxy: Http())
    new = renew(sess)
    assert seen == {"refresh_token": sess.refresh_token, "steamid": "7656", "renewal_type": "1"}
    assert new.access_exp == 1.95e9 and new.refresh_token == sess.refresh_token

    class Dead:
        def post(self, url, *, timeout, data):
            return Resp({"response": {}})
    monkeypatch.setattr("c5bot.steam_login._http", lambda proxy: Dead())
    with pytest.raises(SteamLoginError, match="重新登录"):
        renew(sess)


def test_save_load_delete_session(tmp_path):
    path = tmp_path / "steam_session.json"
    assert load_session(path) is None
    sess = SteamSession("7656", "me", "r", "a")
    save_session(path, sess)
    assert load_session(path) == sess
    save_session(path, None)
    assert not path.exists()
    path.write_text("not json", encoding="utf-8")
    assert load_session(path) is None
