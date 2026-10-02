from __future__ import annotations

import pytest
import requests

from c5bot.client import ACCEPT_ENCODING, C5Client, C5Error, C5NetworkError

SECRET = "SECRET-APP-KEY"


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, result):
        self.result = result
        self.headers: dict = {}
        self.sent: list[dict] = []

    def request(self, method, url, **kw):
        self.sent.append({"method": method, "url": url, **kw})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make(result) -> tuple[C5Client, FakeSession]:
    client = C5Client(SECRET)
    headers = client._http.headers
    client._http = FakeSession(result)
    client._http.headers = headers
    return client, client._http


def test_sends_app_key_and_required_header():
    client, http = make(FakeResponse(payload={"success": True, "data": {"moneyAmount": 1.5}}))
    assert client.balance() == {"moneyAmount": 1.5}
    assert http.sent[0]["url"] == "https://openapi.c5game.com/merchant/account/v2/balance"
    assert http.sent[0]["params"] == {"app-key": SECRET}
    assert http.headers["Accept-Encoding"] == ACCEPT_ENCODING


def test_batch_buy_payload_keeps_int64_product_id():
    client, http = make(FakeResponse(payload={"success": True, "data": {"successList": []}}))
    client.batch_buy("https://t", [{"productId": 1444544795338493955, "buyPrice": 2.2, "outTradeNo": "1"}])
    assert http.sent[0]["json"]["productList"][0]["productId"] == 1444544795338493955
    assert http.sent[0]["json"]["tradeUrl"] == "https://t"


def test_business_failure_is_definite():
    client, _ = make(FakeResponse(payload={"success": False, "errorCode": 400001, "errorMsg": "请输入正确的 app-Key"}))
    with pytest.raises(C5Error) as e:
        client.balance()
    assert not isinstance(e.value, C5NetworkError)
    assert e.value.code == 400001


@pytest.mark.parametrize("result, ambiguous", [
    (requests.ConnectTimeout(f"https://x/?app-key={SECRET}"), False),   # 没连上，肯定没生效
    (requests.ReadTimeout(f"https://x/?app-key={SECRET}"), True),       # 发出去了，结果未知
    (requests.ConnectionError(f"https://x/?app-key={SECRET}"), True),
    (FakeResponse(status=502), True),
    (FakeResponse(status=403), False),
])
def test_failure_classification_and_no_key_leak(result, ambiguous):
    client, _ = make(result)
    with pytest.raises(C5Error) as e:
        client.batch_buy("https://t", [])
    assert isinstance(e.value, C5NetworkError) is ambiguous
    assert SECRET not in str(e.value)
    # 原始异常里有带 app-key 的 URL，不能跟着 traceback 打出来
    assert e.value.__cause__ is None
    assert e.value.__context__ is None or e.value.__suppress_context__


def test_order_detail_not_found_returns_none():
    client, _ = make(FakeResponse(payload={"success": False, "errorCode": 1, "errorMsg": "订单不存在"}))
    assert client.order_detail("1") is None
    client, _ = make(FakeResponse(payload={"success": True, "data": None}))
    assert client.order_detail("1") is None
    client, _ = make(FakeResponse(payload={"success": False, "errorCode": 2, "errorMsg": "请求过于频繁"}))
    with pytest.raises(C5Error):
        client.order_detail("1")
