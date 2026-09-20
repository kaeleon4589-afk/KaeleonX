from __future__ import annotations
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping


def canonical_json(params: Mapping) -> str:
    # Keep the body serialization identical to the string used for signing.
    return json.dumps(dict(params), ensure_ascii=False)


def sign_rest_request(secret: str, timestamp: str, method: str, path: str,
                      params: Mapping | None = None) -> str:
    params = params or {}
    method = method.upper()
    if method == "GET":
        query = "&".join(
            f"{k}={v}" for k, v in params.items() if v is not None
        )
        raw = f"{timestamp}{method}{path}" + (f"?{query}" if query else "")
    else:
        raw = f"{timestamp}{method}{path}{canonical_json(params)}"
    digest = hmac.new(
        secret.encode(),
        raw.encode(),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def auth_headers(api_key: str, secret: str, method: str, path: str,
                 params=None, timestamp: str | None = None) -> dict[str, str]:
    ts = timestamp or str(int(time.time() * 1000))
    headers = {
        "sign": sign_rest_request(secret, ts, method, path, params),
        "api_key": api_key,
        "timestamp": ts,
    }
    if method.upper() in {"POST", "PUT", "DELETE"}:
        headers["Content-Type"] = "application/json"
    return headers
