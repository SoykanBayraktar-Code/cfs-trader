"""lock — süreçler-arası danışsal kilit (flock).

Servis döngüsü (loop.run_forever) ile ctl.py'nin açık-pozisyon/işlem mutasyonlarını
serialize eder ki ikisi aynı anda Binance'e/DB'ye yazıp YARIŞMASIN. flock danışsaldır:
yalnızca aynı dosyayı flock'layan süreçler birbirini bekler. Salt-okunur komutlar
(status/show/scan) kilit ALMAZ — yalnız trade/close/flatten ve döngü kritik bölümü alır.
"""
import os
import time
import fcntl
import contextlib


def _lock_path(cfg):
    return cfg.abspath("data/trader.lock")


@contextlib.contextmanager
def cross_lock(cfg, timeout=180):
    """Kilidi al (meşgulse timeout'a kadar bekle, sonra TimeoutError). Çıkışta bırak."""
    path = _lock_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "w")
    acquired = False
    deadline = time.time() + timeout
    try:
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError(
                        "kilit alınamadı — başka bir işlem/tarama sürüyor olabilir, az sonra tekrar dene")
                time.sleep(0.5)
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        f.close()
