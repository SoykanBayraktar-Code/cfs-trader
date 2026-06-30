#!/usr/bin/env python3
"""scalp paper-demo tek tik — DEMO/dry_run, AYRI DB, canlıya DOKUNMAZ. Cron */5 ile gece koşar."""
import sys; sys.path.insert(0, "/root/cfs-trader")
from cfs_trader.cfg import Config
from cfs_trader.loop import Ctx, scan_tick, poll_tick
cfg = Config(path="/root/cfs-trader/config_scalp_demo.yaml")
assert cfg.dry_run is True, "GUVENLIK: demo dry_run degil!"
ctx = Ctx(cfg=cfg)
try: ctx.notifier.enabled = False
except Exception: pass
poll_tick(ctx)   # paper çıkış (dry_run simüle)
scan_tick(ctx)   # paper giriş
