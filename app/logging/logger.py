import json, logging
from datetime import datetime, timezone

def get_logger(name):
    logger=logging.getLogger(name)
    if not logger.handlers:
        h=logging.StreamHandler(); h.setFormatter(logging.Formatter('%(message)s')); logger.addHandler(h); logger.setLevel(logging.INFO)
    return logger

class AuditLogger:
    def __init__(self, db=None): self.db=db; self.logger=get_logger('kaeleon.audit')
    def event(self,event,decision_id=None,**data):
        record={'ts':datetime.now(timezone.utc).isoformat(),'event':event,'decision_id':decision_id,**data}
        self.logger.info(json.dumps(record,default=str,separators=(',',':')))
        if self.db: self.db.write('events',record)
