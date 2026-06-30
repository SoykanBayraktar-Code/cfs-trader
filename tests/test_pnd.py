#!/usr/bin/env python3
"""pnd_ctx birim testi — saf rush-order/faz mantigi (agsiz, deterministik).
Dogrular: rush_z spike, bs_ratio, 4-faz siniflandirma, DUMP precedence, fail-open."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader import pnd_ctx as P

CFG = {"pnd": {"chunk_s": 15, "recent_chunks": 4, "min_baseline_chunks": 20,
               "rush_z_thresh": 3.0, "price_pos_pump": 0.40, "price_pos_late": 0.70,
               "late_gain_pct": 8.0, "dump_ratio": 0.45, "dump_drop_pct": 1.5, "min_gain_pct": 3.0}}


class Cfg(dict):
    def get(self, k, d=None): return super().get(k, d)


cfg = Cfg(CFG)
n_ok = n_fail = 0
def chk(name, cond):
    global n_ok, n_fail
    print(("✅" if cond else "❌") + " " + name); n_ok += bool(cond); n_fail += (not cond)


# --- classify (saf, crafted metrics) ---
def m(z, pos, gain, bs, off): return {"rush_z": z, "price_pos": pos, "gain_pct": gain, "bs_ratio": bs, "off_high_pct": off}
chk("PUMP_EARLY (spike+dusuk-fiyat+buy)", P.classify(m(4, 0.30, 2, 0.80, 0.5), cfg)[0] == "PUMP_EARLY")
chk("PUMP_LATE (spike+yuksek-fiyat)", P.classify(m(5, 0.85, 12, 0.70, 0.3), cfg)[0] == "PUMP_LATE")
chk("PUMP_LATE (spike+buyuk-gain)", P.classify(m(4, 0.55, 10, 0.65, 0.4), cfg)[0] == "PUMP_LATE")
chk("DUMPING (sell-baskin+tepeden-dusus)", P.classify(m(1, 0.4, 9, 0.30, 3.0), cfg)[0] == "DUMPING")
chk("DUMP precedence (spike olsa bile sell-baskin)", P.classify(m(4, 0.5, 9, 0.30, 2.0), cfg)[0] == "DUMPING")
chk("NONE (spike yok)", P.classify(m(1, 0.5, 1, 0.55, 0.2), cfg)[0] == "NONE")
chk("NONE (spike ama orta-fiyat)", P.classify(m(4, 0.55, 4, 0.60, 0.3), cfg)[0] == "NONE")
chk("classify(None) -> NONE fail-open", P.classify(None, cfg) == ("NONE", 0.0))

# --- compute_metrics (sentetik aggTrades: baseline + buy-spike) ---
def gen():
    cms = 15000; t0 = 1700000000000; tr = []
    for ci in range(22):                          # baseline: dusuk aktivite
        cs = t0 + ci * cms
        for k in range(2):
            tr.append({"T": cs + k * 100, "p": 100.0, "q": 1.0, "m": False})
            tr.append({"T": cs + 5000 + k * 100, "p": 100.0, "q": 1.0, "m": True})
    for ci in range(22, 26):                       # recent: BUY SPIKE + fiyat 100->110
        cs = t0 + ci * cms; px = 100.0 + (ci - 21) * 2.5
        for k in range(25):
            tr.append({"T": cs + k * 50, "p": px, "q": 2.0, "m": False})
        tr.append({"T": cs + 9000, "p": px, "q": 1.0, "m": True})
    return tr

met = P.compute_metrics(gen(), cfg)
chk("compute_metrics calisti", met is not None)
if met:
    chk("rush_z yuksek (>3, spike yakalandi) = %.1f" % met["rush_z"], met["rush_z"] > 3.0)
    chk("bs_ratio buy-baskin (>0.9) = %.2f" % met["bs_ratio"], met["bs_ratio"] > 0.9)
    chk("gain pozitif (~+10%%) = %.1f" % met["gain_pct"], met["gain_pct"] > 5)
    ph, sc = P.classify(met, cfg)
    chk("sentetik pump -> PUMP_LATE/EARLY (=%s)" % ph, ph in ("PUMP_LATE", "PUMP_EARLY"))

# --- fail-open ---
chk("compute_metrics([]) -> None", P.compute_metrics([], cfg) is None)
chk("compute_metrics(az-veri) -> None", P.compute_metrics([{"T": 1, "p": 1, "q": 1, "m": False}] * 5, cfg) is None)
# evaluate fail-open (fetch'i bos dondur)
P._fetch_aggtrades = lambda *a, **k: []
class C: pass
c = C(); c.symbol = "TESTUSDT"
ph, sc, snap = P.evaluate(Cfg(CFG), c)
chk("evaluate fetch-bos -> NONE fail-open", ph == "NONE")

print("\n=== %d gecti / %d kaldi ===" % (n_ok, n_fail))
sys.exit(1 if n_fail else 0)
