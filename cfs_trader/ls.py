"""top-trader L/S shadow + tilt sinyali — advanced/lsratio.py'yi yeniden kullanir.
Binance /futures/data (KEYLESS, stdlib). SHADOW kayit + yumusak-tilt; KARAR YOLUNU yalniz
boyut olarak (kuculterek) etkiler. Fail-safe: hata/None doner, ASLA raise etmez (dongu bloklanmaz)."""
import os, sys, json, contextlib

_ADVANCED = "/root/crypto-futures-scan/advanced"
_ls = None

@contextlib.contextmanager
def _in_advanced():
    cwd = os.getcwd()
    if _ADVANCED not in sys.path:
        sys.path.insert(0, _ADVANCED)
    try:
        os.chdir(_ADVANCED)
        yield
    finally:
        os.chdir(cwd)

def _mod():
    global _ls
    if _ls is None:
        with _in_advanced():
            import lsratio as lr
            _ls = lr
    return _ls

def live_snapshot(symbol):
    """lsratio.snapshot -> {top_pos_long,top_acct_long,global_long,taker_ratio,crowd,smart_vs_crowd} | None."""
    try:
        lr = _mod()
        with _in_advanced():
            snap = lr.snapshot(symbol)
        return snap if isinstance(snap, dict) else None
    except Exception:
        return None

def bias(snap, scale=0.2):
    """Kalabalik-kontrarian yon sinyali [-1,+1] (backtest L2: tek edge). global_long=perakende
    kalabalik. crowd cok-LONG -> bias NEGATIF (LONG'u kucult, SHORT tam=kalabaligi fade);
    crowd cok-SHORT -> bias POZITIF. + = LONG favori. None=veri yok."""
    if not isinstance(snap, dict):
        return None
    gl = snap.get("global_long")
    if gl is None:
        return None
    try:
        return max(-1.0, min(1.0, (0.5 - float(gl)) / float(scale or 0.2)))
    except Exception:
        return None

def snapshot_json(snap):
    try:
        return json.dumps(snap, separators=(",", ":")) if isinstance(snap, dict) else None
    except Exception:
        return None

if __name__ == "__main__":
    s = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    snap = live_snapshot(s)
    print("snapshot:", snap)
    print("bias(crowd-contrarian 0.5-global_long/0.2):", bias(snap))
