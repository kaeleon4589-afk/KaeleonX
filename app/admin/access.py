from datetime import datetime, timedelta, timezone

def now_utc(): return datetime.now(timezone.utc)
def find_user(db, phone=None, telegram_user_id=None):
    if phone: return db.find_one('users', {'phone': phone})
    if telegram_user_id: return db.find_one('users', {'telegram_user_id': str(telegram_user_id)})
    return None

def grant_live_days(db, user, days, admin_user_id, audit):
    if days <= 0: raise ValueError('days_must_be_positive')
    now=now_utc(); current=user.get('live_access_until')
    if isinstance(current,str): current=datetime.fromisoformat(current.replace('Z','+00:00'))
    if not current or current < now: current=now
    new_until=current+timedelta(days=days)
    db.upsert('users', {'user_id':user['user_id']}, {'live_access_until':new_until,'live_access_source':'ADMIN_MANUAL_GRANT'})
    audit.event('ADMIN_LIVE_DAYS_GRANTED', user_id=admin_user_id, target_user_id=user['user_id'], phone=user.get('phone'), days=days, live_access_until=new_until.isoformat())
    return new_until

def set_user_status(db,user,status,admin_user_id,audit,reason=None,until=None):
    if status not in {'active','blocked','suspended','deleted'}: raise ValueError('invalid_user_status')
    payload={'status':status,'admin_action_at':now_utc(),'admin_action_by':admin_user_id}
    if reason: payload['admin_action_reason']=reason
    payload['ban_until']=until if until else (None if status=='active' else payload.get('ban_until'))
    db.upsert('users', {'user_id':user['user_id']}, payload)
    if status!='active':
        db.update_many('sessions', {'user_id':user['user_id'], 'revoked':False}, {'revoked':True})
    audit.event('ADMIN_USER_STATUS_CHANGED', user_id=admin_user_id,target_user_id=user['user_id'],phone=user.get('phone'),status=status,reason=reason,ban_until=until.isoformat() if until else None)
