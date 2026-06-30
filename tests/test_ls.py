"""test_ls.py — L/S kalabalik-kontrarian bias + tilt saf-mantik testi (API cagrisiz)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader import ls
from cfs_trader.signals import _tilt_from_pull

def approx(a, b, t=1e-6): return abs(a - b) < t
P = F = 0
def ok(c, m):
    global P, F
    if c: P += 1; print("  OK", m)
    else: F += 1; print("  FAIL", m)

# --- ls.bias: kalabalik-kontrarian ---
ok(ls.bias({"global_long": 0.72}, 0.2) == -1.0, "crowd 0.72 -> bias -1.0 (clamp, ayi)")
ok(ls.bias({"global_long": 0.30}, 0.2) == 1.0, "crowd 0.30 -> bias +1.0 (clamp, boga)")
ok(ls.bias({"global_long": 0.50}, 0.2) == 0.0, "crowd 0.50 -> bias 0")
ok(approx(ls.bias({"global_long": 0.55}, 0.2), -0.25), "crowd 0.55 -> bias -0.25")
ok(ls.bias({}, 0.2) is None and ls.bias(None, 0.2) is None, "veri yok -> None")
ok(ls.bias({"global_long": None}, 0.2) is None, "global_long None -> None")

# --- tilt: crowd-long (bias=-1) -> LONG kucul, SHORT tam (fade) ---
ok(approx(_tilt_from_pull(-1.0, "LONG", 0.3, 0.35), 0.7), "crowd-long: LONG=0.7 (kucul, dumb-money)")
ok(approx(_tilt_from_pull(-1.0, "SHORT", 0.3, 0.35), 1.0), "crowd-long: SHORT=1.0 (tam, fade)")
ok(approx(_tilt_from_pull(1.0, "SHORT", 0.3, 0.35), 0.7), "crowd-short: SHORT=0.7 (kucul)")
ok(approx(_tilt_from_pull(1.0, "LONG", 0.3, 0.35), 1.0), "crowd-short: LONG=1.0 (tam)")

# --- dengeli kalabalik (|bias|<min_abs) -> tilt 1.0 ---
ok(approx(_tilt_from_pull(0.25, "LONG", 0.3, 0.35), 1.0), "|bias|0.25<0.35 -> 1.0 (dokunmaz)")
ok(approx(_tilt_from_pull(-0.25, "SHORT", 0.3, 0.35), 1.0), "|bias|0.25 short -> 1.0")

# --- clamp [0.7,1.0] her durumda ---
clamp_ok = all(0.7 <= _tilt_from_pull(b, s, 0.3, 0.35) <= 1.0
               for b in (-1, -0.5, 0, 0.5, 1) for s in ("LONG", "SHORT"))
ok(clamp_ok, "tilt clamp [0.7,1.0] (risk ASLA artmaz)")

print(f"\n=== {P} gecti / {F} kaldi ===")
sys.exit(1 if F else 0)
