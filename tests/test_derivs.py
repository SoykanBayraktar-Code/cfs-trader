#!/usr/bin/env python3
"""derivs_ctx CONFLUENCE birim testi — saf bias mantığı (ağsız, deterministik).

Doğrular: yön-hizalama (LONG/SHORT), kalibre ağırlıklar, F&G ağırlık=0 (skora katmaz),
yetersiz girdi→NEUTRAL, uç destek→CONFIRM, uç çelişki→CONFLICT, fail-open."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader import derivs_ctx as D

CFG = {"derivs": {
    "enabled": False, "shadow": True, "min_components": 2,
    "confirm_thresh": 0.35, "conflict_thresh": 0.35,
    "oi_collapse_norm": 1.0, "fng_extreme_lo": 25, "fng_extreme_hi": 75,
    "weights": {"ls": 1.0, "oi": 1.0, "funding": 0.4, "liq": 0.4, "fng": 0.0},
}}


class Cfg(dict):
    def get(self, k, d=None): return super().get(k, d)


cfg = Cfg(CFG)
n_ok = n_fail = 0


def chk(name, cond):
    global n_ok, n_fail
    print(("✅" if cond else "❌") + " " + name)
    n_ok += bool(cond); n_fail += (not cond)


# 1) LONG: kalabalık short (ls+0.8) + OI yükseliyor (+0.6) → güçlü destek → CONFIRM
comps, score, n = D.components("LONG", {"ls_bias": 0.8, "oi_trend": 0.6}, cfg)
chk(f"LONG ls+0.8/oi+0.6 → skor>0.35 (={score})", score >= 0.35 and n == 2)

# 2) SHORT: kalabalık long (ls-0.8 → -ls=+0.8 destek) + OI yükseliyor (yeni short, +0.6) → CONFIRM
comps, score, n = D.components("SHORT", {"ls_bias": -0.8, "oi_trend": 0.6}, cfg)
chk(f"SHORT ls-0.8/oi+0.6 → skor>0.35 (={score})", score >= 0.35)

# 3) LONG: kalabalık aşırı long (ls-0.9 → -0.9 çelişki) + OI çöküyor (-1.5→clamp-1.0) → CONFLICT
comps, score, n = D.components("LONG", {"ls_bias": -0.9, "oi_trend": -1.5}, cfg)
chk(f"LONG ls-0.9/oi-1.5 → skor<-0.35 (={score})", score <= -0.35)

# 4) funding all_pos: LONG cezası (-1*0.4), SHORT destek (+1*0.4)
cL, sL, _ = D.components("LONG", {"all_pos": True, "oi_trend": 0.0}, cfg)
cS, sS, _ = D.components("SHORT", {"all_pos": True, "oi_trend": 0.0}, cfg)
chk(f"all_pos LONG<0<SHORT (L={sL} S={sS})", sL < 0 < sS)

# 5) liq_imb>0 (short-squeeze, yukarı): LONG destek, SHORT çelişki
cL, sL, _ = D.components("LONG", {"liq_imb": 0.8, "oi_trend": 0.0}, cfg)
cS, sS, _ = D.components("SHORT", {"liq_imb": 0.8, "oi_trend": 0.0}, cfg)
chk(f"liq_imb+0.8 LONG>0>SHORT (L={sL} S={sS})", sL > 0 > sS)

# 6) F&G AĞIRLIK 0 → skoru DEĞİŞTİRMEZ (aşırı korku 10 koysak bile aynı skor)
_, s_wo, _ = D.components("SHORT", {"ls_bias": -0.6, "oi_trend": 0.4}, cfg)
_, s_w, n_w = D.components("SHORT", {"ls_bias": -0.6, "oi_trend": 0.4, "fng": 10}, cfg)
chk(f"F&G ağırlık 0 → skor değişmez ({s_wo}=={s_w})", abs(s_wo - s_w) < 1e-9)
chk("F&G n_active'e SAYILMAZ (w=0)", n_w == 2)

# 7) yetersiz girdi (yalnız 1 aktif bileşen) → evaluate NEUTRAL
class C:
    pass
c = C(); c.side = "LONG"; c.ls_bias = 0.9; c.oi_trend = None; c.cz_snapshot = None
D._fng = lambda cfg: (None, None)   # ağ yok
b, sc, snap = D.evaluate(Cfg(CFG), c)
chk(f"tek bileşen → NEUTRAL (={b})", b == "NEUTRAL")

# 8) evaluate CONFIRM yolu (2 bileşen, güçlü) + snapshot JSON üretir
c2 = C(); c2.side = "SHORT"; c2.ls_bias = -0.8; c2.oi_trend = 0.6; c2.cz_snapshot = None
b2, sc2, snap2 = D.evaluate(Cfg(CFG), c2)
chk(f"2 güçlü bileşen SHORT → CONFIRM (={b2})", b2 == "CONFIRM")
chk("snapshot JSON üretildi", isinstance(snap2, str) and "score" in snap2)

# 9) fail-open: tamamen boş cand → NEUTRAL, hata yok
c3 = C(); c3.side = "LONG"
b3, sc3, snap3 = D.evaluate(Cfg(CFG), c3)
chk(f"boş cand → NEUTRAL fail-open (={b3})", b3 == "NEUTRAL")

# 10) cz_snapshot parse (all_pos + liq_imb) gerçek JSON'dan okunur
c4 = C(); c4.side = "SHORT"; c4.ls_bias = None; c4.oi_trend = None
c4.cz_snapshot = '{"c":"BSB","coin":{"funding_div":0.0006,"all_pos":true,"liq":{"imb":-0.7}}}'
b4, sc4, snap4 = D.evaluate(Cfg(CFG), c4)
chk(f"cz JSON'dan all_pos+liq okundu, SHORT destek skor>0 (={sc4})", sc4 > 0)

print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
sys.exit(1 if n_fail else 0)
