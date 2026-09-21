from __future__ import annotations

import hashlib
import secrets
import urllib.request
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from app.referrals import reward_first_paid_plan


TRIAL_DAYS = 5
SUBSCRIPTIONS = {
    "15D": {"days": 15, "price_usdt": Decimal("5")},
    "30D": {"days": 30, "price_usdt": Decimal("10")},
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _payment_key(user_id: str, order_id: str) -> str:
    return hashlib.sha256(f"{user_id}:{order_id}".encode()).hexdigest()


@dataclass(frozen=True)
class Entitlement:
    demo_allowed: bool
    live_allowed: bool
    live_state: str
    live_expires_at: Optional[datetime]
    reason: str


class BillingService:
    """KAELEON commercial access layer.

    DEMO is unlimited and never consumes LIVE trial time.
    LIVE trial starts only on the first explicit LIVE activation.
    LIVE subscriptions are activated only after a confirmed payment.
    """

    def __init__(self, db, trial_days: int = TRIAL_DAYS):
        self.db = db
        self.trial_days = int(trial_days)

    def ensure_account(self, user_id: str) -> dict:
        user = self.db.find_one("users", {"user_id": user_id})
        if not user:
            raise ValueError("user_not_found")
        updates = {}
        if "demo_status" not in user:
            updates["demo_status"] = "active"
        if "live_state" not in user:
            updates["live_state"] = "not_started"
        if updates:
            self.db.upsert("users", {"user_id": user_id}, updates)
            user.update(updates)
        return user

    def entitlement(self, user_id: str, now: Optional[datetime] = None) -> Entitlement:
        now = _as_utc(now or utcnow())
        user = self.ensure_account(user_id)
        live_state = str(user.get("live_state", "not_started"))

        candidates = []
        trial_expiry = user.get("live_trial_expires_at")
        if trial_expiry and _as_utc(trial_expiry) > now:
            candidates.append((_as_utc(trial_expiry), "trial", "live_trial_active"))
        subscription_expiry = user.get("subscription_expires_at")
        if subscription_expiry and _as_utc(subscription_expiry) > now:
            candidates.append((_as_utc(subscription_expiry), "subscribed", "subscription_active"))
        manual_expiry = user.get("live_access_until")
        if manual_expiry and _as_utc(manual_expiry) > now:
            candidates.append((_as_utc(manual_expiry), "granted", "manual_live_access_active"))

        if candidates:
            expires, state, reason = max(candidates, key=lambda item: item[0])
            return Entitlement(True, True, state, expires, reason)

        if live_state in {"trial", "subscribed", "granted"}:
            self.db.upsert("users", {"user_id": user_id}, {"live_state": "expired"})
            live_state = "expired"
        return Entitlement(True, False, live_state, None, "live_not_entitled")

    def activate_live_first_time(self, user_id: str, now: Optional[datetime] = None) -> Entitlement:
        now = _as_utc(now or utcnow())
        user = self.ensure_account(user_id)
        current = self.entitlement(user_id, now)
        if current.live_allowed:
            return current
        if user.get("live_trial_started_at") or user.get("live_trial_expires_at"):
            raise ValueError("live_trial_already_used")

        expires = now + timedelta(days=self.trial_days)
        self.db.upsert("users", {"user_id": user_id}, {
            "live_state": "trial",
            "live_trial_started_at": now,
            "live_trial_expires_at": expires,
        })
        self.db.write("events", {
            "event_type": "LIVE_FIRST_ACTIVATION",
            "user_id": user_id,
            "live_state": "trial",
            "trial_started_at": now,
            "trial_expires_at": expires,
        })
        return Entitlement(True, True, "trial", expires, "live_trial_started")

    def create_payment_order(self, user_id: str, plan_code: str, wallet: str, network: str = "BNB_SMART_CHAIN") -> dict:
        plan = SUBSCRIPTIONS.get(plan_code)
        if not plan:
            raise ValueError("invalid_subscription_plan")
        if not wallet:
            raise ValueError("payment_wallet_not_configured")
        order_id = "PAY-" + secrets.token_hex(12).upper()
        now = utcnow()
        order = {
            "payment_order_id": order_id,
            "payment_key": _payment_key(user_id, order_id),
            "user_id": user_id,
            "plan_code": plan_code,
            "duration_days": plan["days"],
            "amount_usdt": str(plan["price_usdt"]),
            "network": network,
            "destination_wallet": wallet,
            "status": "AWAITING_PAYMENT",
            "created_at": now,
            "expires_at": now + timedelta(minutes=30),
            "tx_hash": None,
        }
        self.db.write("payment_orders", order)
        return order

    def submit_tx_hash(self, user_id: str, payment_order_id: str, tx_hash: str) -> dict:
        tx_hash = (tx_hash or "").strip()
        if not tx_hash or len(tx_hash) < 20:
            raise ValueError("invalid_tx_hash")
        order = self.db.find_one("payment_orders", {"payment_order_id": payment_order_id})
        if not order or order.get("user_id") != user_id:
            raise ValueError("payment_order_not_found")
        if order.get("status") in {"CONFIRMED", "ALREADY_USED", "EXPIRED"}:
            raise ValueError("payment_order_not_verifiable")
        if order.get("expires_at") and _as_utc(order["expires_at"]) <= utcnow():
            self.db.upsert("payment_orders", {"payment_order_id": payment_order_id}, {"status": "EXPIRED"})
            raise ValueError("payment_order_expired")
        existing = self.db.find_one("payment_orders", {"tx_hash": tx_hash})
        if existing and existing.get("payment_order_id") != payment_order_id:
            raise ValueError("tx_already_used")
        self.db.upsert("payment_orders", {"payment_order_id": payment_order_id}, {
            "tx_hash": tx_hash,
            "status": "VERIFYING",
            "submitted_at": utcnow(),
        })
        return self.db.find_one("payment_orders", {"payment_order_id": payment_order_id})

    def confirm_payment(self, payment_order_id: str, tx_hash: str, verifier) -> dict:
        order = self.db.find_one("payment_orders", {"payment_order_id": payment_order_id})
        if not order:
            raise ValueError("payment_order_not_found")
        if order.get("status") == "CONFIRMED":
            if order.get("tx_hash") != tx_hash:
                raise ValueError("tx_hash_mismatch")
            return self.entitlement(order["user_id"])
        if order.get("status") == "EXPIRED" or (order.get("expires_at") and _as_utc(order["expires_at"]) <= utcnow()):
            self.db.upsert("payment_orders", {"payment_order_id": payment_order_id}, {"status": "EXPIRED"})
            raise ValueError("payment_order_expired")
        if order.get("tx_hash") != tx_hash:
            raise ValueError("tx_hash_mismatch")
        existing = self.db.find_one("payment_orders", {"tx_hash": tx_hash, "status": "CONFIRMED"})
        if existing and existing.get("payment_order_id") != payment_order_id:
            raise ValueError("tx_already_used")
        result = verifier.verify(order, tx_hash)
        if not result.get("valid"):
            self.db.upsert("payment_orders", {"payment_order_id": payment_order_id}, {
                "status": result.get("status", "INVALID"),
                "verification_reason": result.get("reason", "payment_not_verified"),
                "verified_at": utcnow(),
            })
            raise ValueError(result.get("reason", "payment_not_verified"))

        now = utcnow()
        days = int(order["duration_days"])
        user = self.ensure_account(order["user_id"])
        current_expiry = user.get("subscription_expires_at")
        base = _as_utc(current_expiry) if current_expiry and _as_utc(current_expiry) > now else now
        expires = base + timedelta(days=days)
        self.db.upsert("payment_orders", {"payment_order_id": payment_order_id}, {
            "status": "CONFIRMED",
            "confirmed_at": now,
            "verified_block_number": result.get("block_number"),
        })
        self.db.upsert("users", {"user_id": order["user_id"]}, {
            "live_state": "subscribed",
            "subscription_started_at": now,
            "subscription_expires_at": expires,
            "subscription_last_payment_order_id": payment_order_id,
        })
        self.db.write("events", {
            "event_type": "SUBSCRIPTION_ACTIVATED",
            "user_id": order["user_id"],
            "payment_order_id": payment_order_id,
            "tx_hash": tx_hash,
            "expires_at": expires,
        })
        reward = reward_first_paid_plan(self.db, user, days)
        self.db.write("events", {
            "event_type": "REFERRAL_REWARD_EVALUATED",
            "user_id": order["user_id"],
            "payment_order_id": payment_order_id,
            "rewarded": reward.get("rewarded", False),
            "reward_days": reward.get("reward_days", 0),
        })
        return self.entitlement(order["user_id"], now)


class BscUsdtVerifier:
    """Minimal BNB Smart Chain USDT ERC-20 transfer verifier via JSON-RPC."""

    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a0dfb0c5e0"

    def __init__(self, rpc_url: str, usdt_contract: str, decimals: int = 18):
        self.rpc_url = rpc_url
        self.usdt_contract = usdt_contract.lower()
        self.decimals = int(decimals)

    def _rpc(self, method: str, params: list):
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.rpc_url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as response:
            body = json.loads(response.read().decode())
        if body.get("error"):
            raise ValueError("bsc_rpc_error")
        return body.get("result")

    def verify(self, order: dict, tx_hash: str) -> dict:
        try:
            if not self.rpc_url or not self.usdt_contract:
                return {"valid": False, "status": "INVALID", "reason": "payment_verifier_not_configured"}
            chain_id = self._rpc("eth_chainId", [])
            if chain_id != "0x38":
                return {"valid": False, "status": "INVALID", "reason": "wrong_network"}
            receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
            if not receipt or receipt.get("status") != "0x1":
                return {"valid": False, "status": "INVALID", "reason": "tx_not_confirmed"}
            destination = order["destination_wallet"].lower().replace("0x", "")
            expected_raw = int(Decimal(order["amount_usdt"]) * (Decimal(10) ** self.decimals))
            contract = self.usdt_contract.replace("0x", "")
            for log in receipt.get("logs", []):
                if str(log.get("address", "")).lower().replace("0x", "") != contract:
                    continue
                topics = log.get("topics", [])
                if len(topics) < 3 or topics[0].lower() != self.TRANSFER_TOPIC:
                    continue
                to_addr = topics[2][-40:].lower()
                value = int(log.get("data", "0x0"), 16)
                if to_addr == destination and value == expected_raw:
                    return {"valid": True, "status": "CONFIRMED", "block_number": int(receipt["blockNumber"], 16)}
            return {"valid": False, "status": "INVALID", "reason": "amount_or_destination_mismatch"}
        except (ValueError, TypeError, OSError, InvalidOperation):
            return {"valid": False, "status": "INVALID", "reason": "bsc_verification_error"}
