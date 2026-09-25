"""One trading worker per database, renewed outside the market loop."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4


class WorkerLease:
    def __init__(self, db, ttl_seconds=60):
        self.db = db
        self.owner = uuid4().hex
        self.ttl = ttl_seconds
        self.valid_until = datetime.min.replace(tzinfo=timezone.utc)

    def renew(self):
        now = datetime.now(timezone.utc)
        until = now + timedelta(seconds=self.ttl)
        if self.db.db is not None:
            from pymongo import ReturnDocument
            from pymongo.errors import DuplicateKeyError
            try:
                row = self.db.db.worker_leases.find_one_and_update(
                    {'_id': 'trading', '$or': [{'owner': self.owner}, {'expires_at': {'$lte': now}}]},
                    {'$set': {'owner': self.owner, 'expires_at': until}},
                    upsert=True, return_document=ReturnDocument.AFTER)
            except DuplicateKeyError:
                return False
            if not row or row['owner'] != self.owner:
                return False
        else:
            with self.db._mutex:
                old = self.db.find_one('worker_leases', {'lease_id': 'trading'})
                if old and old['owner'] != self.owner and old['expires_at'] > now:
                    return False
                self.db.upsert('worker_leases', {'lease_id': 'trading'}, {'owner': self.owner, 'expires_at': until})
        self.valid_until = until
        return True

    def valid(self):
        return datetime.now(timezone.utc) < self.valid_until - timedelta(seconds=10)

    async def run(self):
        while True:
            await asyncio.sleep(self.ttl / 3)
            if not await asyncio.to_thread(self.renew):
                raise RuntimeError('trading_worker_lease_lost')

    def release(self):
        if self.db.db is not None:
            self.db.db.worker_leases.delete_one({'_id': 'trading', 'owner': self.owner})
        else:
            with self.db._mutex:
                self.db.memory[:] = [(c, d) for c, d in self.db.memory
                                     if not (c == 'worker_leases' and d.get('owner') == self.owner)]
