from __future__ import annotations

import hashlib
import secrets
import urllib.request
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional
from threading import RLock

from app.referrals import reward_first_paid_plan


TRIAL_DAYS = 5
SUBSCRIPTIONS = {
    "15D": {"days": 15, "price_usdt": Decimal("5")},
    "30D": {"days": 30, "price_usdt": Decimal("10")},
}

DEFAULT_BSC_USDT_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"
PAYMENT_REVERIFY_GRACE = timedelta(hours=24)
ACTIVE_PAYMENT_STATUSES = {"AWAITING_PAYMENT", "VERIFYING", "INVALID"}
TERMINAL_PAYMENT_STATUSES = {"CONFIRMED", "ALREADY_USED", "EXPIRED", "CANCELLED_DUPLICATE"}
_TX_HASH_RE = re.compile(r"^(?:0x|0X)[0-9a-fA-F]{64}$")
_ORDER_CREATE_LOCK = RLock()


class PaymentVerificationPending(ValueError):
    """The transfer may be valid but is not verifiable yet."""


def normalize_tx_hash(value: str) -> str:
    value = (value or "").strip()
    if not _TX_HASH_RE.fullmatch(value):
        raise ValueError("invalid_tx_hash")
    return "0x" + value[2:].lower()



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

    def _expire_stale_order(self, order: dict, now: Optional[datetime] = None) -> dict:
        now = _as_utc(now or utcnow())
        status = str(order.get("status", ""))
        expires_at = order.get("expires_at")
        if status in ACTIVE_PAYMENT_STATUSES and expires_at and _as_utc(expires_at) <= now:
            submitted_at = order.get("submitted_at")
            # A hash submitted in time may finish confirming after the 30-minute window.
            expires_utc = _as_utc(expires_at)
            submitted_in_time = bool(status in {"VERIFYING", "INVALID"} and submitted_at and _as_utc(submitted_at) <= expires_utc)
            within_reverify_grace = submitted_in_time and now <= expires_utc + PAYMENT_REVERIFY_GRACE
            if not within_reverify_grace:
                self.db.upsert("payment_orders", {"payment_order_id": order["payment_order_id"]}, {
                    "status": "EXPIRED",
                    "active_order_key": None,
                    "expired_at": now,
                })
                order = {**order, "status": "EXPIRED", "active_order_key": None, "expired_at": now}
        return order

    def reconcile_payment_orders(self, user_id: str, now: Optional[datetime] = None) -> Optional[dict]:
        """Return the single active order and clean historical duplicates safely.

        Older builds could create several orders when a user tapped the plan button more
        than once.  Prefer the order that already contains a transaction hash so a real
        payment is never discarded, then cancel duplicate no-hash orders.
        """
        now = _as_utc(now or utcnow())
        rows = self.db.find_many(
            "payment_orders", {"user_id": user_id}, limit=100, sort_field="created_at"
        )
        active: list[dict] = []
        for row in rows:
            row = self._expire_stale_order(row, now)
            if str(row.get("status", "")) in ACTIVE_PAYMENT_STATUSES:
                active.append(row)
        if not active:
            return None

        def priority(row: dict):
            status = str(row.get("status", ""))
            status_rank = {"VERIFYING": 3, "INVALID": 2, "AWAITING_PAYMENT": 1}.get(status, 0)
            has_hash = 1 if row.get("tx_hash") else 0
            created = row.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)
            if isinstance(created, datetime) and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return (has_hash, status_rank, created)

        chosen = max(active, key=priority)
        for row in active:
            oid = row.get("payment_order_id")
            if not oid or oid == chosen.get("payment_order_id"):
                continue
            self.db.upsert("payment_orders", {"payment_order_id": oid}, {
                "status": "CANCELLED_DUPLICATE",
                "active_order_key": None,
                "cancelled_at": now,
                "cancellation_reason": "duplicate_active_order",
            })

        # The partial unique index makes this a cross-request/cross-worker guard in Mongo.
        self.db.upsert("payment_orders", {"payment_order_id": chosen["payment_order_id"]}, {
            "active_order_key": user_id,
        })
        chosen = self.db.find_one("payment_orders", {"payment_order_id": chosen["payment_order_id"]}) or chosen
        return chosen

    def create_payment_order(self, user_id: str, plan_code: str, wallet: str, network: str = "BNB_SMART_CHAIN") -> dict:
        plan = SUBSCRIPTIONS.get(plan_code)
        if not plan:
            raise ValueError("invalid_subscription_plan")
        if not wallet:
            raise ValueError("payment_wallet_not_configured")

        with _ORDER_CREATE_LOCK:
            existing = self.reconcile_payment_orders(user_id)
            if existing:
                return {**existing, "reused_existing": True}

            order_id = "PAY-" + secrets.token_hex(12).upper()
            now = utcnow()
            order = {
                "payment_order_id": order_id,
                "payment_key": _payment_key(user_id, order_id),
                "active_order_key": user_id,
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
            try:
                self.db.write("payment_orders", order)
            except Exception:
                # A concurrent request may have won the unique active_order_key slot.
                winner = self.db.find_one("payment_orders", {"active_order_key": user_id})
                if winner:
                    return {**winner, "reused_existing": True}
                raise
            return order

    def submit_tx_hash(self, user_id: str, payment_order_id: str, tx_hash: str) -> dict:
        tx_hash = normalize_tx_hash(tx_hash)
        order = self.db.find_one("payment_orders", {"payment_order_id": payment_order_id})
        if not order or order.get("user_id") != user_id:
            raise ValueError("payment_order_not_found")
        if order.get("status") in TERMINAL_PAYMENT_STATUSES:
            raise ValueError("payment_order_not_verifiable")
        if order.get("expires_at") and _as_utc(order["expires_at"]) <= utcnow():
            submitted_at = order.get("submitted_at")
            expires_utc = _as_utc(order["expires_at"])
            submitted_in_time = bool(order.get("status") in {"VERIFYING", "INVALID"} and submitted_at and _as_utc(submitted_at) <= expires_utc)
            within_reverify_grace = submitted_in_time and utcnow() <= expires_utc + PAYMENT_REVERIFY_GRACE
            if not within_reverify_grace:
                self.db.upsert("payment_orders", {"payment_order_id": payment_order_id}, {
                    "status": "EXPIRED", "active_order_key": None, "expired_at": utcnow()
                })
                raise ValueError("payment_order_expired")
        existing = self.db.find_one("payment_orders", {"tx_hash": tx_hash})
        if existing and existing.get("payment_order_id") != payment_order_id:
            raise ValueError("tx_already_used")
        submitted_at = order.get("submitted_at") or utcnow()
        self.db.upsert("payment_orders", {"payment_order_id": payment_order_id}, {
            "tx_hash": tx_hash,
            "status": "VERIFYING",
            "submitted_at": submitted_at,
            "verification_reason": None,
            "active_order_key": user_id,
        })
        return self.db.find_one("payment_orders", {"payment_order_id": payment_order_id})

    def confirm_payment(self, payment_order_id: str, tx_hash: str, verifier) -> dict:
        tx_hash = normalize_tx_hash(tx_hash)
        order = self.db.find_one("payment_orders", {"payment_order_id": payment_order_id})
        if not order:
            raise ValueError("payment_order_not_found")
        if order.get("status") == "CONFIRMED":
            if order.get("tx_hash") != tx_hash:
                raise ValueError("tx_hash_mismatch")
            return self.entitlement(order["user_id"])

        expires_at = order.get("expires_at")
        submitted_at = order.get("submitted_at")
        if expires_at and _as_utc(expires_at) <= utcnow():
            expires_utc = _as_utc(expires_at)
            submitted_in_time = bool(order.get("status") in {"VERIFYING", "INVALID"} and submitted_at and _as_utc(submitted_at) <= expires_utc)
            within_reverify_grace = submitted_in_time and utcnow() <= expires_utc + PAYMENT_REVERIFY_GRACE
            if not within_reverify_grace:
                self.db.upsert("payment_orders", {"payment_order_id": payment_order_id}, {
                    "status": "EXPIRED", "active_order_key": None, "expired_at": utcnow()
                })
                raise ValueError("payment_order_expired")
        if order.get("tx_hash") != tx_hash:
            raise ValueError("tx_hash_mismatch")
        existing = self.db.find_one("payment_orders", {"tx_hash": tx_hash, "status": "CONFIRMED"})
        if existing and existing.get("payment_order_id") != payment_order_id:
            raise ValueError("tx_already_used")

        result = verifier.verify(order, tx_hash)
        if not result.get("valid"):
            status = str(result.get("status", "INVALID"))
            reason = str(result.get("reason", "payment_not_verified"))
            if status in {"VERIFYING", "PENDING", "TEMPORARY_ERROR"}:
                self.db.upsert("payment_orders", {"payment_order_id": payment_order_id}, {
                    "status": "VERIFYING",
                    "verification_reason": reason,
                    "last_verification_at": utcnow(),
                    "active_order_key": order["user_id"],
                })
                raise PaymentVerificationPending(reason)
            self.db.upsert("payment_orders", {"payment_order_id": payment_order_id}, {
                "status": "INVALID",
                "verification_reason": reason,
                "verified_at": utcnow(),
                "active_order_key": order["user_id"],
            })
            raise ValueError(reason)

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
            "verification_reason": None,
            "active_order_key": None,
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
        self.usdt_contract = (usdt_contract or DEFAULT_BSC_USDT_CONTRACT).lower()
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
            tx_hash = normalize_tx_hash(tx_hash)
        except ValueError:
            return {"valid": False, "status": "INVALID", "reason": "invalid_tx_hash"}

        if not self.rpc_url or not self.usdt_contract:
            return {"valid": False, "status": "TEMPORARY_ERROR", "reason": "payment_verifier_not_configured"}

        try:
            chain_id = self._rpc("eth_chainId", [])
            if str(chain_id).lower() != "0x38":
                return {"valid": False, "status": "TEMPORARY_ERROR", "reason": "wrong_network"}

            receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
            if not receipt:
                return {"valid": False, "status": "VERIFYING", "reason": "tx_not_confirmed"}
            receipt_status = str(receipt.get("status", "")).lower()
            if receipt_status == "0x0":
                return {"valid": False, "status": "INVALID", "reason": "tx_reverted"}
            if receipt_status != "0x1":
                return {"valid": False, "status": "VERIFYING", "reason": "tx_not_confirmed"}

            # Prevent replaying an old transfer against a freshly created order.
            block_number_hex = receipt.get("blockNumber")
            created_at = order.get("created_at")
            if block_number_hex and created_at:
                block = self._rpc("eth_getBlockByNumber", [block_number_hex, False])
                if block and block.get("timestamp"):
                    block_ts = int(block["timestamp"], 16)
                    if isinstance(created_at, datetime):
                        created_ts = _as_utc(created_at).timestamp()
                    else:
                        parsed = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                        created_ts = _as_utc(parsed).timestamp()
                    if block_ts + 120 < created_ts:
                        return {"valid": False, "status": "INVALID", "reason": "tx_before_order"}

            destination = str(order["destination_wallet"]).lower().replace("0x", "")
            expected_raw = int(Decimal(str(order["amount_usdt"])) * (Decimal(10) ** self.decimals))
            contract = self.usdt_contract.lower().replace("0x", "")
            for log in receipt.get("logs", []):
                if str(log.get("address", "")).lower().replace("0x", "") != contract:
                    continue
                topics = log.get("topics", [])
                if len(topics) < 3 or str(topics[0]).lower() != self.TRANSFER_TOPIC:
                    continue
                to_addr = str(topics[2])[-40:].lower()
                try:
                    value = int(log.get("data", "0x0"), 16)
                except (TypeError, ValueError):
                    continue
                if to_addr == destination and value == expected_raw:
                    block_number = receipt.get("blockNumber")
                    return {
                        "valid": True,
                        "status": "CONFIRMED",
                        "block_number": int(block_number, 16) if block_number else None,
                    }
            return {"valid": False, "status": "INVALID", "reason": "amount_or_destination_mismatch"}
        except (OSError, TimeoutError, json.JSONDecodeError):
            return {"valid": False, "status": "TEMPORARY_ERROR", "reason": "bsc_rpc_temporarily_unavailable"}
        except (ValueError, TypeError, InvalidOperation):
            return {"valid": False, "status": "TEMPORARY_ERROR", "reason": "bsc_verification_error"}

