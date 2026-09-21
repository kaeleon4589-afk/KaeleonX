from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING, DESCENDING

class Database:
    """MongoDB Atlas persistence. Falls back to an in-memory store only for tests."""
    def __init__(self, uri='', database='kaeleon', client=None):
        self.client=client or (MongoClient(uri, serverSelectionTimeoutMS=3000, tz_aware=True) if uri else None)
        self.db=self.client[database] if self.client else None
        self.memory=[]
        if self.db is not None: self.ensure_indexes()

    def ensure_indexes(self):
        self.db.decisions.create_index([('decision_id',ASCENDING)],unique=True)
        self.db.orders.create_index([('order_id',ASCENDING)])
        self.db.positions.create_index([('position_id',ASCENDING)],unique=True)
        self.db.positions.create_index([('user_id',ASCENDING),('mode',ASCENDING),('status',ASCENDING)])
        self.db.events.create_index([('created_at',DESCENDING)])
        self.db.pnl.create_index([('position_id',ASCENDING)])
        self.db.users.create_index([('phone',ASCENDING)], unique=True)
        self.db.users.create_index([('user_id',ASCENDING)], unique=True)
        self.db.user_trading_profiles.create_index([('user_id',ASCENDING)], unique=True)
        self.db.user_engine_state.create_index([('user_id',ASCENDING),('mode',ASCENDING)], unique=True)
        self.db.sessions.create_index([('token_hash',ASCENDING)], unique=True)
        self.db.sessions.create_index([('expires_at',ASCENDING)])
        self.db.sessions.create_index([('user_id',ASCENDING)])
        self.db.payment_orders.create_index([('payment_order_id',ASCENDING)], unique=True)
        self.db.payment_orders.create_index([('tx_hash',ASCENDING)], sparse=True)
        self.db.payment_orders.create_index([('user_id',ASCENDING), ('created_at',DESCENDING)])
        self.db.registration_challenges.create_index([('challenge_hash',ASCENDING)], unique=True)
        self.db.telegram_verifications.create_index([('challenge_hash',ASCENDING)], unique=True)
        self.db.telegram_bot_sessions.create_index([('chat_id',ASCENDING)], unique=True)
        self.db.telegram_bot_sessions.create_index([('expires_at',ASCENDING)])

    def write(self, collection, document):
        document=dict(document); document.setdefault('created_at',datetime.now(timezone.utc))
        if self.db is not None: return self.db[collection].insert_one(document).inserted_id
        self.memory.append((collection,document)); return None

    def upsert(self, collection, key, document):
        document=dict(document); document['updated_at']=datetime.now(timezone.utc)
        if self.db is not None:
            self.db[collection].update_one(key,{'$set':document},upsert=True)
        else:
            for i,(c,d) in enumerate(self.memory):
                if c == collection and all(d.get(k) == v for k,v in key.items()):
                    merged=dict(d); merged.update(document); self.memory[i]=(c,merged); return
            self.memory.append((collection,document))

    def find_one(self, collection, key):
        if self.db is not None: return self.db[collection].find_one(key)
        for c,d in reversed(self.memory):
            if c == collection and all(d.get(k) == v for k,v in key.items()): return dict(d)
        return None
    def find_many(self, collection, key=None, limit=100, sort_field=None, descending=True):
        key = key or {}
        if self.db is not None:
            cursor = self.db[collection].find(key)
            if sort_field:
                cursor = cursor.sort(sort_field, DESCENDING if descending else ASCENDING)
            return list(cursor.limit(limit))
        rows = [dict(d) for c, d in self.memory if c == collection and all(d.get(k) == v for k, v in key.items())]
        if sort_field:
            rows.sort(key=lambda d: d.get(sort_field), reverse=descending)
        return rows[:limit]

    def update_many(self, collection, key, update):
        if self.db is not None:
            return self.db[collection].update_many(key, {"$set": update})
        count=0
        for i,(c,d) in enumerate(self.memory):
            if c == collection and all(d.get(k) == v for k,v in key.items()):
                merged=dict(d); merged.update(update); self.memory[i]=(c,merged); count += 1
        return count

    def count(self, collection, key=None):
        key = key or {}
        if self.db is not None:
            return self.db[collection].count_documents(key)
        return sum(1 for c, d in self.memory if c == collection and all(d.get(k) == v for k, v in key.items()))

