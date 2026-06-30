"""test_squeeze.py — coinalyze squeeze-farkindalik tilt + parse_squeeze saf-mantik testi."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader import cz, signals

def approx(a, b, t=1e-3): return abs(a - b) < t
P = F = 0
def ok(c, m):
    global P, F
    if c: P += 1; print("  OK", m)
    else: F += 1; print("  FAIL", m)

def snap(imb, nbars, oi):
    return json.dumps({"c": "X", "coin": {"oi_total": oi, "liq": {"imb": imb, "n_bars": nbars}}, "macro": {}})

# --- parse_squeeze ---
ps = cz.parse_squeeze(snap(0.72, 15, 9e8))
ok(ps and approx(ps["imb"], 0.72) and ps["n_bars"] == 15 and ps["oi"] == 9e8, "parse_squeeze JSON dogru")
ok(cz.parse_squeeze(None) is None and cz.parse_squeeze("bozuk{") is None, "parse_squeeze fail-safe None")

class Cfg:
    _sq = {"enabled": True, "tilt_strength": 0.3, "min_abs": 0.4, "min_n_bars": 8, "min_oi_usd": 5_000_000}
    def get(self, k, d=None): return {"squeeze": self._sq}.get(k, d)
class Cand:
    def __init__(self, side, s): self.side = side; self.cz_snapshot = s
cfg = Cfg()
def st(side, imb, nbars=15, oi=9e8): return signals.squeeze_tilt(cfg, Cand(side, snap(imb, nbars, oi)))

# --- BLESS-benzeri: likit short-squeeze (imb +0.72) ---
ok(approx(st("SHORT", 0.72), 0.784), "likit short-squeeze: SHORT kucul ~0.784 (BLESS senaryosu)")
ok(approx(st("LONG", 0.72), 1.0), "likit short-squeeze: LONG=1.0 (yukari yardim)")
ok(approx(st("SHORT", -0.72), 1.0), "long-flush: SHORT=1.0 (asagi yardim)")
ok(approx(st("LONG", -0.72), 0.784), "long-flush: LONG kucul ~0.784")
# --- ince coin korumasi (CBRS dersi) ---
ok(approx(st("SHORT", 0.9, nbars=3), 1.0), "ince coin n_bars=3 -> tilt 1.0 (dokunmaz)")
ok(approx(st("SHORT", 0.9, oi=1e6), 1.0), "dusuk OI $1M -> tilt 1.0 (dokunmaz)")
# --- zayif/dengeli + disabled ---
ok(approx(st("SHORT", 0.3), 1.0), "|imb|0.3<min_abs0.4 -> tilt 1.0")
class CfgOff(Cfg):
    _sq = {"enabled": False}
ok(approx(signals.squeeze_tilt(CfgOff(), Cand("SHORT", snap(0.9, 15, 9e8))), 1.0), "disabled -> 1.0")
# --- clamp ---
ok(all(0.7 <= st(s, i) <= 1.0 for i in (-1, -0.5, 0, 0.5, 1) for s in ("LONG", "SHORT")), "clamp [0.7,1.0] (risk artmaz)")

print(f"\n=== {P} gecti / {F} kaldi ==="); sys.exit(1 if F else 0)
