from datetime import datetime, timezone, timedelta
import secrets

REWARD_BY_DAYS = {15: 7, 30: 15}


def new_referral_code(db):
    for _ in range(20):
        code = "KAELEON-" + secrets.token_hex(4).upper()
        if not db.find_one("users", {"referral_code": code}):
            return code
    raise RuntimeError("referral_code_generation_failed")


def attach_referral(db, user_id: str, referral_code: str | None):
    if not referral_code:
        return None
    user = db.find_one("users", {"user_id": user_id})
    code = referral_code.strip().upper()
    referrer = db.find_one("users", {"referral_code": code})
    if not referrer or referrer.get("user_id") == user_id:
        raise ValueError("invalid_referral_code")
    if user and user.get("referred_by_user_id"):
        raise ValueError("referral_already_set")
    db.upsert("users", {"user_id": user_id}, {
        "referred_by_user_id": referrer["user_id"],
        "referred_by_code": code,
        "referral_attached_at": datetime.now(timezone.utc),
    })
    return referrer


def reward_first_paid_plan(db, referred_user: dict, plan_days: int, audit=None):
    if plan_days not in REWARD_BY_DAYS:
        raise ValueError("unsupported_referral_plan")
    if referred_user.get("referral_rewarded_at"):
        return {"rewarded": False, "reason": "already_rewarded"}
    referrer_id = referred_user.get("referred_by_user_id")
    if not referrer_id:
        return {"rewarded": False, "reason": "no_referrer"}
    referrer = db.find_one("users", {"user_id": referrer_id})
    if not referrer:
        return {"rewarded": False, "reason": "referrer_not_found"}

    reward_days = REWARD_BY_DAYS[plan_days]
    now = datetime.now(timezone.utc)
    current = referrer.get("subscription_expires_at")
    if isinstance(current, str):
        current = datetime.fromisoformat(current.replace("Z", "+00:00"))
    if not current or current < now:
        current = now
    new_until = current + timedelta(days=reward_days)

    db.upsert("users", {"user_id": referrer_id}, {
        "subscription_expires_at": new_until,
        "live_state": "subscribed",
        "referral_reward_days_total": int(referrer.get("referral_reward_days_total", 0)) + reward_days,
    })
    db.upsert("users", {"user_id": referred_user["user_id"]}, {
        "referral_rewarded_at": now,
        "referral_reward_plan_days": plan_days,
        "referral_reward_days": reward_days,
    })
    db.write("referral_rewards", {
        "referred_user_id": referred_user["user_id"],
        "referrer_user_id": referrer_id,
        "plan_days": plan_days,
        "reward_days": reward_days,
        "created_at": now,
    })
    if audit:
        audit.event("REFERRAL_REWARD_GRANTED", user_id=referrer_id,
                    target_user_id=referred_user["user_id"],
                    plan_days=plan_days, reward_days=reward_days,
                    live_access_until=new_until.isoformat())
    return {"rewarded": True, "reward_days": reward_days, "live_expires_at": new_until}
