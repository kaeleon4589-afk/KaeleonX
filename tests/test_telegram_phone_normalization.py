from app.auth.service import AuthService, normalize_phone


class FakeDb:
    def __init__(self):
        self.rows = []

    def write(self, collection, document):
        self.rows.append((collection, dict(document)))

    def find_one(self, collection, key):
        for c, row in reversed(self.rows):
            if c == collection and all(row.get(k) == v for k, v in key.items()):
                return dict(row)
        return None

    def upsert(self, collection, key, document):
        for index, (c, row) in enumerate(self.rows):
            if c == collection and all(row.get(k) == v for k, v in key.items()):
                merged = dict(row)
                merged.update(document)
                self.rows[index] = (c, merged)
                return
        merged = dict(key)
        merged.update(document)
        self.rows.append((collection, merged))

    def update_many(self, collection, key, update):
        count = 0
        for index, (c, row) in enumerate(self.rows):
            if c == collection and all(row.get(k) == v for k, v in key.items()):
                merged = dict(row)
                merged.update(update)
                self.rows[index] = (c, merged)
                count += 1
        return count


def test_telegram_digits_only_number_normalizes_to_same_e164():
    assert normalize_phone("59494299", "+53") == "+5359494299"
    assert normalize_phone("+5359494299") == "+5359494299"
    assert normalize_phone("5359494299") == "+5359494299"


def test_telegram_contact_matches_registration_when_telegram_omits_plus():
    db = FakeDb()
    auth = AuthService(db)
    registration = auth.create_registration("59494299", "strong-password", "+53")

    assert auth.mark_telegram_contact(
        registration["challenge"],
        telegram_user_id="12345",
        telegram_phone="5359494299",
    ) is True


def test_pending_registration_can_restart_and_old_challenge_is_invalidated():
    db = FakeDb()
    auth = AuthService(db)
    first = auth.create_registration("59494299", "first-password", "+53")
    second = auth.create_registration("59494299", "second-password", "+53")

    assert first["user_id"] == second["user_id"]
    assert first["challenge"] != second["challenge"]
    assert auth.registration_challenge_info(first["challenge"]) is None
    assert auth.registration_challenge_info(second["challenge"])["phone"] == "+5359494299"
