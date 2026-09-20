from __future__ import annotations

import httpx
from .authentication import auth_headers, canonical_json


class CoinWRestClient:
    def __init__(self, base_url: str, api_key: str = "", secret: str = "",
                 timeout: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.secret = secret
        self.timeout = timeout

    async def request(self, method: str, path: str, params=None,
                      private: bool = False):
        method = method.upper()
        params = params or {}
        headers = (
            auth_headers(self.api_key, self.secret, method, path, params)
            if private else {}
        )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if method == "GET":
                response = await client.request(
                    method,
                    self.base_url + path,
                    params=params,
                    headers=headers,
                )
            else:
                # Send exactly the JSON bytes that were signed.
                body = canonical_json(params).encode("utf-8")
                response = await client.request(
                    method,
                    self.base_url + path,
                    content=body,
                    headers=headers,
                )

            response.raise_for_status()
            payload = response.json()

            if isinstance(payload, dict) and "code" in payload:
                code = payload.get("code")
                if str(code) not in {"0", "200"}:
                    message = payload.get("msg") or payload.get("message") or "coinw_api_error"
                    raise RuntimeError(f"coinw_api_error:{code}:{message}")
            return payload
