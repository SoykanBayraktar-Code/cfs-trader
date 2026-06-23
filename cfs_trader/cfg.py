"""cfg — config.yaml + secrets.env yükleyici. Tek doğruluk kaynağı; her modül buradan okur.

Gizli anahtarlar secrets.env'den okunur, ASLA loglanmaz/yazdırılmaz (DISCIPLINE).
"""
import os
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_secrets(path):
    """secrets.env (KEY=VALUE satırları) → ortam değişkenleri. Yoksa sessiz geç (testte)."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if v and not os.environ.get(k):
                os.environ[k] = v


class Config:
    def __init__(self, path=None):
        path = path or os.path.join(_ROOT, "config.yaml")
        with open(path) as f:
            self._d = yaml.safe_load(f)
        _load_secrets(os.path.join(_ROOT, "secrets.env"))
        self._apply_analyst_overrides()   # Opus analist'in ayar değişiklikleri (config.yaml'a DOKUNMADAN)
        # engine path: env override > config
        self.engine_path = os.environ.get("CFS_ENGINE_PATH") or self._d["signals"]["engine_path"]

    def _apply_analyst_overrides(self):
        """data/analyst_overrides.json'daki {param: değer} (noktalı yol) ayarlarını config'in ÜSTÜNE uygular.
        Dosya yoksa no-op. config.yaml hiç değişmez; dosyayı silmek → varsayılana dönüş."""
        import json
        p = self.abspath("data/analyst_overrides.json")
        if not os.path.exists(p):
            return
        try:
            ov = json.load(open(p)).get("params", {})
        except Exception:
            return
        for dotted, val in ov.items():
            parts = dotted.split(".")
            d = self._d
            ok = True
            for seg in parts[:-1]:
                if isinstance(d, dict) and seg in d and isinstance(d[seg], dict):
                    d = d[seg]
                else:
                    ok = False
                    break
            if ok and isinstance(d, dict) and parts[-1] in d:
                d[parts[-1]] = val

    # --- ham erişim ---
    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)

    # --- kısa yollar ---
    @property
    def mode(self):
        return self._d["mode"]

    @property
    def is_live(self):
        return self._d["mode"] == "live"

    @property
    def dry_run(self):
        return bool(self._d.get("dry_run", True))

    @property
    def budget(self):
        return float(self._d["budget_usdt"])

    @property
    def risk(self):
        return self._d["risk"]

    @property
    def signals(self):
        return self._d["signals"]

    def abspath(self, rel):
        """config'teki göreli yolu (data/trader.db) proje köküne göre mutlaklaştır."""
        if os.path.isabs(rel):
            return rel
        return os.path.join(_ROOT, rel)

    @property
    def db_path(self):
        # mode'a göre AYRI DB — paper (testnet) ve live state'i ASLA karışmaz (cross-contamination koruması).
        base = self._d["paths"]["db"]
        root, ext = os.path.splitext(base)
        return self.abspath(f"{root}_{self.mode}{ext}")

    # --- gizli anahtarlar (mode'a göre doğru çifti döndürür) ---
    def api_keys(self):
        """(api_key, api_secret) — mode:live ise gerçek, değilse testnet anahtarları."""
        if self.is_live:
            return os.environ.get("BINANCE_API_KEY"), os.environ.get("BINANCE_API_SECRET")
        return os.environ.get("BINANCE_TESTNET_API_KEY"), os.environ.get("BINANCE_TESTNET_API_SECRET")

    def telegram(self):
        return os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")

    @property
    def base_url(self):
        """Binance USD-M Futures base — mode'a göre testnet/live."""
        return "https://fapi.binance.com" if self.is_live else "https://testnet.binancefuture.com"


_cfg = None


def get(path=None):
    global _cfg
    if _cfg is None or path is not None:
        _cfg = Config(path)
    return _cfg
