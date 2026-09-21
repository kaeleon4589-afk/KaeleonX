from app.api import user_api, admin_api
from app.storage.database import Database


def test_user_referral_summary_exposes_code_counts_and_rewards(monkeypatch):
    db = Database()
    db.write('users', {'user_id':'u1','phone':'+5359494299','status':'active','referral_code':'KAELEON-ABC','referral_reward_days_total':7})
    db.write('users', {'user_id':'u2','phone':'+5359000001','status':'active','referred_by_user_id':'u1','referred_by_code':'KAELEON-ABC','referral_rewarded_at':'2026-01-01','referral_reward_days':7})
    monkeypatch.setattr(user_api, 'current', lambda authorization: (db, db.find_one('users', {'user_id':'u1'}), None, None))
    result = user_api.referrals('Bearer x')
    assert result['referral_code'] == 'KAELEON-ABC'
    assert result['referred_count'] == 1
    assert result['rewarded_count'] == 1
    assert result['reward_days_total'] == 7
    assert result['items'][0]['phone_masked'] != '+5359000001'


def test_admin_referral_view_lists_referrer_and_referred(monkeypatch):
    db = Database()
    db.write('users', {'user_id':'a','phone':'+5359494299','status':'active','referral_code':'KAELEON-A'})
    db.write('users', {'user_id':'b','phone':'+5359000002','status':'active','referred_by_user_id':'a','referred_by_code':'KAELEON-A'})
    monkeypatch.setattr(admin_api, 'current_admin', lambda authorization: {'user_id':'a'})
    monkeypatch.setattr(admin_api, 'deps', lambda: (db, None, None))
    result = admin_api.admin_referrals('Bearer x')
    assert result['total'] == 1
    assert result['items'][0]['referrer_phone'] == '+5359494299'
    assert result['items'][0]['referred_phone'] == '+5359000002'
