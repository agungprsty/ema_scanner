# Skill: Bitunix Futures API Integration

## Overview
Bitunix Futures REST API implementation guide. Base URL: `https://fapi.bitunix.com`

## Authentication & Signature

### Headers Required (All Private Requests)
| Header | Type | Description |
|--------|------|-------------|
| `api-key` | string | API key |
| `nonce` | string | Random string, 32-bit, caller-generated |
| `timestamp` | string | Current timestamp in milliseconds |
| `sign` | string | Signature string |
| `Content-Type` | string | `application/json` |

### Signature Algorithm (2-pass SHA256)
```python
import hashlib
import time
import uuid

def generate_signature(api_key: str, secret_key: str, body: str, query_params: str = "") -> dict:
    nonce = uuid.uuid4().hex[:32]
    timestamp = str(int(time.time() * 1000))

    # Pass 1: digest
    digest_input = nonce + timestamp + api_key + query_params + body
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()

    # Pass 2: sign
    sign_input = digest + secret_key
    sign = hashlib.sha256(sign_input.encode("utf-8")).hexdigest()

    return {
        "api-key": api_key,
        "nonce": nonce,
        "timestamp": timestamp,
        "sign": sign,
    }
```

### Critical Notes
- Request body format MUST be identical to the signature string (remove all spaces in body)
- For GET requests: query_params are sorted in ascending ASCII order by key
- For POST requests: body is JSON stringified
- `timestamp` is in milliseconds (UTC time)

---

## Trade Endpoints

### 1. Place Order
- **Endpoint:** `POST /api/v1/futures/trade/place_order`
- **Rate Limit:** 10 req/sec/uid

#### Request Parameters
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `symbol` | string | YES | Trading pair (e.g. `BTCUSDT`) |
| `qty` | string | YES | Amount in base coin |
| `price` | string | NO* | Price (*required for LIMIT orders) |
| `side` | string | YES | `BUY` or `SELL` |
| `tradeSide` | string | YES | `OPEN` or `CLOSE` (hedge mode only) |
| `positionId` | string | NO** | Position ID (**required when tradeSide=CLOSE) |
| `orderType` | string | YES | `LIMIT` or `MARKET` |
| `effect` | string | NO*** | ***Required for LIMIT: `IOC`, `FOK`, `GTC` (default), `POST_ONLY` |
| `clientId` | string | NO | Custom order ID |
| `reduceOnly` | boolean | NO | Reduce position only |

#### TP/SL Parameters (Optional)
| Parameter | Type | Description |
|-----------|------|-------------|
| `tpPrice` | string | Take profit trigger price |
| `tpStopType` | string | `MARK_PRICE` or `LAST_PRICE` |
| `tpOrderType` | string | `LIMIT` or `MARKET` |
| `tpOrderPrice` | string | TP limit order price (required if tpOrderType=LIMIT) |
| `slPrice` | string | Stop loss trigger price |
| `slStopType` | string | `MARK_PRICE` or `LAST_PRICE` |
| `slOrderType` | string | `LIMIT` or `MARKET` |
| `slOrderPrice` | string | SL limit order price (required if slOrderType=LIMIT) |

#### tradeSide Rules (Hedge Mode)
| Action | side | tradeSide |
|--------|------|-----------|
| Open Long | `BUY` | `OPEN` |
| Open Short | `SELL` | `OPEN` |
| Close Long | `BUY` | `CLOSE` |
| Close Short | `SELL` | `CLOSE` |

#### Response
```json
{
  "code": 0,
  "data": {
    "orderId": "11111",
    "clientId": "22222"
  },
  "msg": "Success"
}
```

---

### 2. Batch Order
- **Endpoint:** `POST /api/v1/futures/trade/batch_order`
- **Rate Limit:** 1 req/sec/uid
- **Max orders per batch:** 5

#### Request
```json
{
  "symbol": "BTCUSDT",
  "orderList": [
    {
      "side": "BUY",
      "price": "60000",
      "qty": "0.5",
      "orderType": "LIMIT",
      "tradeSide": "OPEN",
      "effect": "GTC",
      "clientId": "c12345",
      "tpPrice": "61000",
      "tpStopType": "MARK_PRICE",
      "tpOrderType": "LIMIT",
      "tpOrderPrice": "61000.1",
      "slPrice": "59000",
      "slStopType": "LAST_PRICE",
      "slOrderType": "MARKET"
    }
  ]
}
```

#### Response
```json
{
  "code": 0,
  "data": {
    "successList": [{"id": "11111", "clientId": "22222"}],
    "failureList": [{"clientId": "22222", "errorMsg": "Insufficient balance", "errorCode": 10012}]
  }
}
```

---

### 3. Cancel Orders
- **Endpoint:** `POST /api/v1/futures/trade/cancel_orders`
- **Rate Limit:** 5 req/sec/uid

#### Request
```json
{
  "symbol": "BTCUSDT",
  "orderList": [
    {"orderId": "11111"},
    {"clientId": "22223"}
  ]
}
```

#### Response
```json
{
  "code": 0,
  "data": {
    "successList": [{"orderId": "11111", "clientId": "22222"}],
    "failureList": [{"orderId": "11112", "clientId": "22223", "errorMsg": "Order status error", "errorCode": 10013}]
  }
}
```

---

### 4. Modify Order
- **Endpoint:** `POST /api/v1/futures/trade/modify_order`
- **Rate Limit:** 10 req/sec/uid

#### Request Parameters
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `orderId` | string | NO* | *Either orderId or clientId required |
| `clientId` | string | NO* | *Either orderId or clientId required |
| `qty` | string | YES | New quantity |
| `price` | string | YES | New price |

#### Response
```json
{
  "code": 0,
  "data": {"orderId": "11111", "clientId": "22222"},
  "msg": "Success"
}
```

---

### 5. Get Pending Orders
- **Endpoint:** `GET /api/v1/futures/trade/get_pending_orders`
- **Rate Limit:** 10 req/sec/uid

#### Query Parameters
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `symbol` | string | NO | Trading pair |
| `orderId` | string | NO | Order ID |
| `clientId` | string | NO | Client ID |
| `status` | string | NO | `NEW` or `PART_FILLED` |
| `startTime` | int64 | NO | Start timestamp (ms) |
| `endTime` | int64 | NO | End timestamp (ms) |
| `skip` | int64 | NO | Skip count (default 0) |
| `limit` | int64 | NO | Max 100, default 10 |

#### Response Fields (per order)
`orderId`, `symbol`, `qty`, `tradeQty`, `positionMode` (ONE_WAY/HEDGE), `marginMode` (ISOLATION/CROSS), `leverage`, `price`, `side`, `orderType`, `effect`, `clientId`, `reduceOnly`, `status` (INIT/NEW/PART_FILLED/CANCELED/FILLED), `fee`, `realizedPNL`, `tpPrice`, `tpStopType`, `tpOrderType`, `tpOrderPrice`, `slPrice`, `slStopType`, `slOrderType`, `slOrderPrice`, `ctime`, `mtime`, `total`

---

### 6. Get Order Detail
- **Endpoint:** `GET /api/v1/futures/trade/get_order_detail`
- **Rate Limit:** 10 req/sec/uid

#### Query Parameters
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `orderId` | string | NO* | *At least one of orderId or clientId required |
| `clientId` | string | NO* | *At least one of orderId or clientId required |

#### Response
Same fields as pending orders order object.

---

### 7. Get History Orders
- **Endpoint:** `GET /api/v1/futures/trade/get_history_orders`
- **Rate Limit:** 10 req/sec/uid

#### Query Parameters
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `symbol` | string | NO | Trading pair |
| `orderId` | string | NO | Order ID |
| `clientId` | string | NO | Client ID |
| `status` | string | NO | `FILLED`, `CANCELED`, `PART_FILLED_CANCELED`, `EXPIRED` |
| `type` | string | NO | `LIMIT` or `MARKET` |
| `startTime` | int64 | NO | Start timestamp (ms) |
| `endTime` | int64 | NO | End timestamp (ms) |
| `skip` | int64 | NO | Skip count (default 0) |
| `limit` | int64 | NO | Max 100, default 10 |
| `subAccountId` | int64 | NO | Sub-account filter |
| `queryCanceled` | boolean | NO | true=last 3 days canceled only, false=last 90 days non-canceled |

---

## Common Error Codes
| Code | Description |
|------|-------------|
| 0 | Success |
| 403 | API key does not support this operation |
| 10003 | api-key empty |
| 10004 | IP not in whitelist |
| 10005 | Too many requests |
| 10007 | Sign signature error |
| 10008 | Parameter value non-compliant |
| 20001 | Market not exists |
| 20002 | Max open positions exceeded |
| 20003 | Insufficient balance |
| 20007 | Order not found |
| 20008 | Insufficient amount |
| 20012 | Futures not allowed trading |
| 30001 | Order price/leverage causes immediate liquidation |
| 30013 | Exceeded max order quantity |
| 30016 | Qty should be larger than minimum |
| 30042 | Client ID duplicate |

---

## Implementation Pattern (Python)

```python
import httpx
import hashlib
import uuid
import time
import json

class BitunixClient:
    BASE_URL = "https://fapi.bitunix.com"
    PREFIX = "/api/v1/futures"

    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self.client = httpx.AsyncClient(base_url=self.BASE_URL, timeout=10.0)

    def _sign(self, body: str, query_params: str = "") -> dict:
        nonce = uuid.uuid4().hex[:32]
        timestamp = str(int(time.time() * 1000))
        digest_input = nonce + timestamp + self.api_key + query_params + body
        digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
        sign_input = digest + self.secret_key
        sign = hashlib.sha256(sign_input.encode("utf-8")).hexdigest()
        return {
            "api-key": self.api_key,
            "nonce": nonce,
            "timestamp": timestamp,
            "sign": sign,
        }

    async def place_order(
        self,
        symbol: str,
        side: str,
        trade_side: str,
        order_type: str,
        qty: str,
        price: str | None = None,
        effect: str | None = None,
        client_id: str | None = None,
        reduce_only: bool = False,
        tp_price: str | None = None,
        tp_stop_type: str | None = None,
        tp_order_type: str | None = None,
        tp_order_price: str | None = None,
        sl_price: str | None = None,
        sl_stop_type: str | None = None,
        sl_order_type: str | None = None,
        sl_order_price: str | None = None,
    ) -> dict:
        body = {"symbol": symbol, "side": side, "tradeSide": trade_side,
                "orderType": order_type, "qty": qty, "reduceOnly": reduce_only}
        if price: body["price"] = price
        if effect: body["effect"] = effect
        if client_id: body["clientId"] = client_id
        if tp_price: body["tpPrice"] = tp_price
        if tp_stop_type: body["tpStopType"] = tp_stop_type
        if tp_order_type: body["tpOrderType"] = tp_order_type
        if tp_order_price: body["tpOrderPrice"] = tp_order_price
        if sl_price: body["slPrice"] = sl_price
        if sl_stop_type: body["slStopType"] = sl_stop_type
        if sl_order_type: body["slOrderType"] = sl_order_type
        if sl_order_price: body["slOrderPrice"] = sl_order_price

        body_str = json.dumps(body, separators=(",", ":"))
        headers = self._sign(body_str)
        headers["Content-Type"] = "application/json"
        resp = await self.client.post(f"{self.PREFIX}/trade/place_order", content=body_str, headers=headers)
        return resp.json()

    async def cancel_orders(self, symbol: str, order_list: list[dict]) -> dict:
        body = {"symbol": symbol, "orderList": order_list}
        body_str = json.dumps(body, separators=(",", ":"))
        headers = self._sign(body_str)
        headers["Content-Type"] = "application/json"
        resp = await self.client.post(f"{self.PREFIX}/trade/cancel_orders", content=body_str, headers=headers)
        return resp.json()

    async def get_pending_orders(self, symbol: str = None, limit: int = 10) -> dict:
        params = {}
        if symbol: params["symbol"] = symbol
        params["limit"] = str(limit)
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        headers = self._sign("", query)
        resp = await self.client.get(f"{self.PREFIX}/trade/get_pending_orders", params=params, headers=headers)
        return resp.json()

    async def get_order_detail(self, order_id: str = None, client_id: str = None) -> dict:
        params = {}
        if order_id: params["orderId"] = order_id
        if client_id: params["clientId"] = client_id
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        headers = self._sign("", query)
        resp = await self.client.get(f"{self.PREFIX}/trade/get_order_detail", params=params, headers=headers)
        return resp.json()

    async def close(self):
        await self.client.aclose()
```

---

## Important Implementation Notes

1. **Body string must be identical in request and signature** - use `json.dumps(body, separators=(",", ":"))` (no spaces)
2. **Signature uses `nonce + timestamp + api-key + query_params + body`** - concatenation order matters
3. **Timestamp in milliseconds** - `int(time.time() * 1000)`
4. **All amounts are strings** - qty, price, tp/sl prices are all string type
5. **Hedge mode tradeSide** - When closing, you need the `positionId` from the position
6. **Limit order requires effect** - Default is `GTC`; use `IOC`/`FOK`/`POST_ONLY` for other behaviors
7. **cancel_orders response success != operation success** - Use WebSocket push to confirm actual fill
8. **Batch order limit** - Max 5 orders per batch request
9. **Rate limits** - 10 req/sec for most endpoints, 1 req/sec for batch orders, 5 req/sec for cancel orders
