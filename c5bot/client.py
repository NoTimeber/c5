"""C5GAME 开放平台客户端。接口文档：https://opendoc.c5game.com"""
from __future__ import annotations

from typing import Any

import requests

BASE_URL = "https://openapi.c5game.com"
# 官方公告：所有请求必须带这个头，否则无法访问
ACCEPT_ENCODING = "gzip, br, zstd, deflate"


class C5Error(Exception):
    """平台明确拒绝了请求（success=false / HTTP 4xx / 根本没连上），请求没有生效。"""

    def __init__(self, message: str, *, code: int | None = None, code_str: str | None = None):
        super().__init__(message)
        self.code = code
        self.code_str = code_str


class C5NetworkError(C5Error):
    """超时、断连、5xx：请求可能已经生效，结果未知。"""


class C5Client:
    def __init__(self, app_key: str, *, proxy: str | None = None, timeout: float = 10.0,
                 base_url: str = BASE_URL):
        self._app_key = app_key
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._http = requests.Session()
        self._http.headers["Accept-Encoding"] = ACCEPT_ENCODING
        if proxy:
            self._http.proxies = {"http": proxy, "https": proxy}

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None) -> Any:
        # 异常一律 from None 且不带原始文本：requests 的异常里有完整 URL，会把 app-key 打进日志
        try:
            resp = self._http.request(
                method, self._base + path,
                params={**(params or {}), "app-key": self._app_key},
                json=body, timeout=self._timeout,
            )
        except (requests.ConnectTimeout, requests.exceptions.ProxyError, requests.exceptions.SSLError) as e:
            raise C5Error(f"{method} {path} 连不上服务器: {type(e).__name__}") from None
        except requests.RequestException as e:
            raise C5NetworkError(f"{method} {path} 网络错误: {type(e).__name__}") from None

        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or "success" not in payload:
            cls = C5Error if 400 <= resp.status_code < 500 else C5NetworkError
            raise cls(f"{method} {path} HTTP {resp.status_code}，返回内容无法识别", code=resp.status_code)
        if not payload["success"]:
            raise C5Error(
                f"{method} {path} 失败: [{payload.get('errorCode')}] {payload.get('errorMsg')}",
                code=payload.get("errorCode"), code_str=payload.get("errorCodeStr"),
            )
        return payload.get("data")

    # ---------- 账户 ----------

    def balance(self) -> dict:
        """moneyAmount 为可用余额。"""
        return self._request("GET", "/merchant/account/v2/balance") or {}

    def steam_info(self) -> dict:
        return self._request("GET", "/merchant/account/v1/steamInfo") or {}

    # ---------- 行情 ----------

    def item_stats(self, app_id: int, names: list[str]) -> dict[str, dict]:
        """按 marketHashName 批量查在售最低价、在售数、求购最高价。返回 {名称: 统计}。"""
        out: dict[str, dict] = {}
        for i in range(0, len(names), 100):  # 单次最多 100 个
            data = self._request("POST", "/merchant/market/v2/item/stat/hash/name",
                                 body={"appId": app_id, "marketHashNames": names[i:i + 100]})
            out.update(data or {})
        return out

    def search_products(self, *, app_id: int, name: str, price_max: float, delivery: int = 0,
                        asset_type: int = 1, page_size: int = 50) -> list[dict]:
        """查某个饰品 price_max 以内的在售。内测接口，需要在官网把本机 IP 加进白名单。"""
        body: dict[str, Any] = {"appId": app_id, "marketHashName": name, "priceMax": price_max,
                                "assetType": asset_type, "pageSize": page_size}
        if delivery:
            body["delivery"] = delivery
        data = self._request("POST", "/merchant/market/v2/products/search", body=body)
        return (data or {}).get("list") or []

    # ---------- 购买 ----------

    def batch_buy(self, trade_url: str, products: list[dict]) -> dict:
        """按在售 id 批量购买。products: [{productId, buyPrice, outTradeNo}]"""
        return self._request("POST", "/merchant/trade/v1/batch/buy",
                             body={"tradeUrl": trade_url, "productList": products}) or {}

    def quick_buy(self, *, out_trade_no: str, trade_url: str, app_id: int, name: str,
                  max_price: float, delivery: int = 0) -> dict:
        """由平台挑一件不高于 max_price 的最低价在售买入。"""
        body: dict[str, Any] = {"outTradeNo": out_trade_no, "tradeUrl": trade_url, "appId": app_id,
                                "marketHashName": name, "maxPrice": max_price, "lowPrice": 1}
        if delivery:
            body["delivery"] = delivery
        return self._request("POST", "/merchant/trade/v2/quick-buy", body=body) or {}

    # ---------- 订单 ----------

    def order_detail(self, out_trade_no: str) -> dict | None:
        """按商户单号查订单，查不到返回 None。"""
        try:
            return self._request("GET", "/merchant/order/v2/buy/detail",
                                 params={"outTradeNo": out_trade_no}) or None
        except C5NetworkError:
            raise
        except C5Error as e:
            # “订单不存在”的具体错误码文档没写，这里按文案猜；猜不中就原样抛出，调用方按“未知”保守处理
            text = f"{e} {e.code_str or ''}".lower()
            if "不存在" in text or "not_exist" in text or "not_found" in text or "not exist" in text:
                return None
            raise
