from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from decimal import Decimal

try:  # Local/test environments may intentionally run without pymongo installed.
    from pymongo import MongoClient, ASCENDING, DESCENDING
except ImportError:  # pragma: no cover - production requirements install pymongo.
    MongoClient = None
    ASCENDING = 1
    DESCENDING = -1


def bson_safe(value):
    """Convert domain objects into values PyMongo can encode safely.

    Trading objects intentionally use Enums/dataclasses in memory.  Passing those
    objects directly to PyMongo raises InvalidDocument and used to interrupt the
    trade pipeline after a real/simulated fill.  Centralising conversion here
    prevents the same class of bug in positions, orders and engine state.
    """
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return bson_safe(asdict(value))
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(k): bson_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [bson_safe(v) for v in value]
    # Primitive BSON-safe values (datetime included) pass through unchanged.
    return value


class Database:
    """MongoDB Atlas persistence with an in-memory fallback for tests.

    All documents are normalised through ``bson_safe`` before they reach PyMongo.
    That boundary is deliberate: business/domain code may use Enums/dataclasses,
    while persistence must only receive BSON-encodable values.
    """

    def __init__(self, uri='', database='kaeleon', client=None):
        if client is not None:
            self.client = client
        elif uri:
            if MongoClient is None:
                raise RuntimeError('pymongo_required_for_mongodb')
            # Bound both server selection and socket operations so a transient DB
            # problem cannot freeze the trading worker indefinitely.
            self.client = MongoClient(
                uri,
                serverSelectionTimeoutMS=3000,
                connectTimeoutMS=3000,
                socketTimeoutMS=5000,
                tz_aware=True,
            )
        else:
            self.client = None

        self.db = self.client[database] if self.client else None
        self.memory = []
        if self.db is not None:
            self.ensure_indexes()

    def ensure_indexes(self):
        self.db.decisions.create_index([('decision_id', ASCENDING)], unique=True)
        self.db.orders.create_index([('order_id', ASCENDING)])
        self.db.positions.create_index([('position_id', ASCENDING)], unique=True)
        self.db.positions.create_index([('user_id', ASCENDING), ('mode', ASCENDING), ('status', ASCENDING)])
        self.db.pnl.create_index([('position_id', ASCENDING)])
        self.db.users.create_index([('phone', ASCENDING)], unique=True)
        self.db.users.create_index([('user_id', ASCENDING)], unique=True)
        self.db.user_trading_profiles.create_index([('user_id', ASCENDING)], unique=True)
        self.db.user_engine_state.create_index([('user_id', ASCENDING), ('mode', ASCENDING)], unique=True)
        self.db.sessions.create_index([('token_hash', ASCENDING)], unique=True)
        self.db.sessions.create_index([('expires_at', ASCENDING)])
        self.db.sessions.create_index([('user_id', ASCENDING)])
        self.db.payment_orders.create_index([('payment_order_id', ASCENDING)], unique=True)
        self.db.payment_orders.create_index([('tx_hash', ASCENDING)], sparse=True)
        self.db.payment_orders.create_index([('user_id', ASCENDING), ('created_at', DESCENDING)])
        self.db.registration_challenges.create_index([('challenge_hash', ASCENDING)], unique=True)
        self.db.telegram_verifications.create_index([('challenge_hash', ASCENDING)], unique=True)
        self.db.telegram_bot_sessions.create_index([('chat_id', ASCENDING)], unique=True)
        self.db.telegram_bot_sessions.create_index([('expires_at', ASCENDING)])

    def write(self, collection, document):
        document = bson_safe(dict(document))
        document.setdefault('created_at', datetime.now(timezone.utc))
        if self.db is not None:
            return self.db[collection].insert_one(document).inserted_id
        self.memory.append((collection, document))
        return None

    def upsert(self, collection, key, document):
        key = bson_safe(dict(key))
        document = bson_safe(dict(document))
        document['updated_at'] = datetime.now(timezone.utc)
        if self.db is not None:
            return self.db[collection].update_one(key, {'$set': document}, upsert=True)
        for i, (c, d) in enumerate(self.memory):
            if c == collection and all(d.get(k) == v for k, v in key.items()):
                merged = dict(d)
                merged.update(document)
                self.memory[i] = (c, merged)
                return None
        self.memory.append((collection, {**key, **document}))
        return None

    def find_one(self, collection, key):
        key = bson_safe(dict(key))
        if self.db is not None:
            return self.db[collection].find_one(key)
        for c, d in reversed(self.memory):
            if c == collection and all(d.get(k) == v for k, v in key.items()):
                return dict(d)
        return None

    def find_many(self, collection, key=None, limit=100, sort_field=None, descending=True):
        key = bson_safe(dict(key or {}))
        if self.db is not None:
            cursor = self.db[collection].find(key)
            if sort_field:
                cursor = cursor.sort(sort_field, DESCENDING if descending else ASCENDING)
            return list(cursor.limit(limit))
        rows = [
            dict(d) for c, d in self.memory
            if c == collection and all(d.get(k) == v for k, v in key.items())
        ]
        if sort_field:
            rows.sort(key=lambda d: (d.get(sort_field) is not None, d.get(sort_field)), reverse=descending)
        return rows[:limit]

    def update_many(self, collection, key, update):
        key = bson_safe(dict(key))
        update = bson_safe(dict(update))
        if self.db is not None:
            return self.db[collection].update_many(key, {'$set': update})
        count = 0
        for i, (c, d) in enumerate(self.memory):
            if c == collection and all(d.get(k) == v for k, v in key.items()):
                merged = dict(d)
                merged.update(update)
                self.memory[i] = (c, merged)
                count += 1
        return count

    def count(self, collection, key=None):
        key = bson_safe(dict(key or {}))
        if self.db is not None:
            return self.db[collection].count_documents(key)
        return sum(
            1 for c, d in self.memory
            if c == collection and all(d.get(k) == v for k, v in key.items())
        )
