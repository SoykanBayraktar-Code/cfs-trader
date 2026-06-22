import time
from cfs_trader.cfg import get
from cfs_trader.store import Store
cfg = get(); s = Store(cfg.db_path)
day = time.strftime("%Y-%m-%d", time.gmtime())
print("ONCE :", dict(s.day_state(day)))
s.db.execute("UPDATE daily_state SET halted=0, halt_reason=NULL, consec_losses=0 WHERE day=?", (day,))
s.db.commit()
print("SONRA:", dict(s.day_state(day)))
