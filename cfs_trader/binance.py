"""binance — Binance USD-M Futures imzalı REST client (elle, sıfır ekstra dep).

Sadece `requests` kullanır. Testnet/live base URL config'ten gelir.
GÜVENLİK (defense-in-depth): dry_run=True iken HİÇBİR emir gönderilmez — mutasyon metodları
sentetik yanıt döndürür. Yani çağıran tarafta bir bug olsa bile gerçek emir kaçmaz.

İmzalama: HMAC-SHA256(secret, query_string), header X-MBX-APIKEY. recvWindow=5000.
"""
import hmac
import hashlib
import time
import math
from urllib.parse import urlencode
import requests


class BinanceError(Exception):
    pass


class Binance:
    LIVE_PUB = "https://fapi.binance.com"

    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg.base_url                       # imzalı/emir venue (mode: testnet|live)
        # public market data: dry_run/paper'da CANLI (motor canlı sembolleri tarar, hepsi mevcut);
        # gerçek emir modunda emir-venue ile aynı (filtreler emrin gideceği yerle eşleşmeli).
        self.pub = self.LIVE_PUB if cfg.dry_run else cfg.base_url
        self.key, self.secret = cfg.api_keys()
        self.dry_run = cfg.dry_run
        self._s = requests.Session()
        if self.key:
            self._s.headers.update({"X-MBX-APIKEY": self.key})
        self._time_offset = 0
        self._filters = {}  # symbol -> {tickSize, stepSize, minNotional, minQty}

    # ---------- alt katman ----------
    def _ts(self):
        return int(time.time() * 1000) + self._time_offset

    def sync_time(self):
        r = self._s.get(self.base + "/fapi/v1/time", timeout=10).json()
        self._time_offset = int(r["serverTime"]) - int(time.time() * 1000)
        return self._time_offset

    def _public(self, path, params=None, timeout=15):
        r = self._s.get(self.pub + path, params=params or {}, timeout=timeout)
        if r.status_code != 200:
            raise BinanceError(f"{path} {r.status_code}: {r.text[:300]}")
        return r.json()

    def _signed(self, method, path, params=None, timeout=15):
        if not self.key or not self.secret:
            raise BinanceError("API anahtarı yok (secrets.env mode'a göre dolu mu?)")
        p = dict(params or {})
        p["timestamp"] = self._ts()
        p["recvWindow"] = 5000
        query = urlencode(p)
        sig = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = f"{self.base}{path}?{query}&signature={sig}"
        r = self._s.request(method, url, timeout=timeout)
        if r.status_code != 200:
            raise BinanceError(f"{method} {path} {r.status_code}: {r.text[:300]}")
        return r.json()

    # ---------- exchange info / yuvarlama ----------
    def load_filters(self, symbol):
        info = self._public("/fapi/v1/exchangeInfo")
        for s in info["symbols"]:
            if s["symbol"] != symbol:
                continue
            f = {"pricePrecision": s["pricePrecision"], "quantityPrecision": s["quantityPrecision"]}
            for filt in s["filters"]:
                if filt["filterType"] == "PRICE_FILTER":
                    f["tickSize"] = float(filt["tickSize"])
                elif filt["filterType"] == "LOT_SIZE":
                    f["stepSize"] = float(filt["stepSize"])
                    f["minQty"] = float(filt["minQty"])
                elif filt["filterType"] == "MIN_NOTIONAL":
                    f["minNotional"] = float(filt["notional"])
            self._filters[symbol] = f
            return f
        raise BinanceError(f"{symbol} exchangeInfo'da yok")

    def filters(self, symbol):
        return self._filters.get(symbol) or self.load_filters(symbol)

    @staticmethod
    def _round_step(value, step):
        if step <= 0:
            return value
        return math.floor(value / step) * step

    def round_qty(self, symbol, qty):
        f = self.filters(symbol)
        q = self._round_step(qty, f["stepSize"])
        return round(q, f["quantityPrecision"])

    def round_price(self, symbol, price):
        f = self.filters(symbol)
        p = self._round_step(price, f["tickSize"])
        return round(p, f["pricePrecision"])

    def min_notional(self, symbol):
        return self.filters(symbol).get("minNotional", self.cfg.risk.get("min_notional_usdt", 5.0))

    # ---------- hesap ----------
    def available_usdt(self):
        bals = self._signed("GET", "/fapi/v2/balance")
        for b in bals:
            if b["asset"] == "USDT":
                return float(b["availableBalance"])
        return 0.0

    def wallet_balance(self):
        """Toplam USDT cüzdan bakiyesi (free DEĞİL — açık-pozisyon margin'i dahil). Bileşik boyutlama için."""
        bals = self._signed("GET", "/fapi/v2/balance")
        for b in bals:
            if b["asset"] == "USDT":
                return float(b["balance"])
        return 0.0

    def positions(self, symbol=None):
        params = {"symbol": symbol} if symbol else None
        rows = self._signed("GET", "/fapi/v2/positionRisk", params)
        return [r for r in rows if float(r["positionAmt"]) != 0.0]

    def mark_price(self, symbol):
        return float(self._public("/fapi/v1/premiumIndex", {"symbol": symbol})["markPrice"])

    def book_ticker(self, symbol):
        """En iyi bid/ask (maker limit girişi için). Public, hafif."""
        return self._public("/fapi/v1/ticker/bookTicker", {"symbol": symbol})

    def set_leverage(self, symbol, leverage):
        if self.dry_run:
            return {"dry_run": True, "leverage": leverage}
        return self._signed("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)})

    def set_margin_type(self, symbol, margin_type="ISOLATED"):
        """Sembolü ISOLATED'a al — cross YASAK. Zaten o tipteyse (-4046) sorun değil.
        Açık pozisyon varken değiştirilemez → o durumda hata yükselir (çağıran işlemi iptal eder)."""
        if self.dry_run:
            return {"dry_run": True, "marginType": margin_type}
        try:
            return self._signed("POST", "/fapi/v1/marginType",
                                 {"symbol": symbol, "marginType": margin_type})
        except BinanceError as e:
            if "-4046" in str(e) or "No need to change" in str(e):
                return {"marginType": margin_type, "already": True}
            raise

    def margin_type_of(self, symbol):
        """Sembolün mevcut margin tipini döndür (positionRisk üstünden): 'isolated' | 'cross' | None."""
        rows = self._signed("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        for r in rows:
            if r["symbol"] == symbol:
                return "isolated" if r.get("isolated") in (True, "true") else r.get("marginType")
        return None

    # ---------- emirler (mutasyon — dry_run kapısı) ----------
    def _send_order(self, params):
        if self.dry_run:
            return {"dry_run": True, "orderId": f"DRY-{int(time.time()*1000)}", "params": params}
        return self._signed("POST", "/fapi/v1/order", params)

    def _send_algo(self, params):
        """Koşullu emir (SL/TP) — 2025-12-09'dan beri AYRI Algo endpoint. dry_run kapısı geçerli."""
        if self.dry_run:
            return {"dry_run": True, "algoId": f"DRY-{int(time.time()*1000)}", "params": params}
        return self._signed("POST", "/fapi/v1/algoOrder", params)

    def place_market(self, symbol, side, qty, reduce_only=False):
        """side: BUY|SELL. LONG giriş=BUY, SHORT giriş=SELL. (Düz emir — /fapi/v1/order.)"""
        p = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty}
        if reduce_only:
            p["reduceOnly"] = "true"
        return self._send_order(p)

    def place_limit(self, symbol, side, qty, price, tif="GTX", reduce_only=False):
        """LIMIT emir. tif=GTX = post-only (maker garantisi; alıcı/satıcı olursa REDDEDİLİR → caller taker'a düşer).
        Düz emir (/fapi/v1/order). dry_run kapısı geçerli."""
        p = {"symbol": symbol, "side": side, "type": "LIMIT", "quantity": qty,
             "price": price, "timeInForce": tif}
        if reduce_only:
            p["reduceOnly"] = "true"
        return self._send_order(p)

    def place_stop_market(self, symbol, side, trigger_price, close_position=True, qty=None):
        """SL — Algo CONDITIONAL. side = pozisyonun TERSİ. closePosition iken qty/reduceOnly GÖNDERİLMEZ."""
        p = {"algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": "STOP_MARKET",
             "triggerPrice": trigger_price, "workingType": "MARK_PRICE"}
        if close_position:
            p["closePosition"] = "true"
        else:
            p["quantity"] = qty
            p["reduceOnly"] = "true"
        return self._send_algo(p)

    def place_take_profit_market(self, symbol, side, trigger_price, close_position=True, qty=None):
        """TP — Algo CONDITIONAL TAKE_PROFIT_MARKET. side = pozisyonun TERSİ."""
        p = {"algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": "TAKE_PROFIT_MARKET",
             "triggerPrice": trigger_price, "workingType": "MARK_PRICE"}
        if close_position:
            p["closePosition"] = "true"
        else:
            p["quantity"] = qty
            p["reduceOnly"] = "true"
        return self._send_algo(p)

    def open_orders(self, symbol=None):
        params = {"symbol": symbol} if symbol else None
        return self._signed("GET", "/fapi/v1/openOrders", params)

    def open_algo_orders(self, symbol=None):
        params = {"symbol": symbol} if symbol else None
        return self._signed("GET", "/fapi/v1/openAlgoOrders", params)

    def cancel_all(self, symbol):
        """Hem düz hem algo (SL/TP) açık emirleri iptal et."""
        if self.dry_run:
            return {"dry_run": True}
        res = {}
        try:
            res["orders"] = self._signed("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        except BinanceError as e:
            res["orders_err"] = str(e)[:120]
        try:
            res["algo"] = self._signed("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})
        except BinanceError as e:
            res["algo_err"] = str(e)[:120]
        return res

    def get_order(self, symbol, order_id):
        return self._signed("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

    def cancel_order(self, symbol, order_id):
        """Tek bir DÜZ emri (limit) iptal et. dry_run'da no-op. (Algo SL/TP için cancel_algo_order.)"""
        if self.dry_run:
            return {"dry_run": True, "orderId": order_id}
        return self._signed("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

    def get_algo_order(self, symbol, algo_id):
        return self._signed("GET", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": algo_id})

    def cancel_algo_order(self, symbol, algo_id):
        """Tek bir algo emrini (SL/TP) iptal et — cancel_all'dan farkı: diğer emirlere DOKUNMAZ.
        Trailing'de eski SL'yi iptal ederken TP'yi korumak için şart. dry_run'da no-op."""
        if self.dry_run:
            return {"dry_run": True, "algoId": algo_id}
        return self._signed("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": algo_id})

    def user_trades(self, symbol, limit=10):
        return self._signed("GET", "/fapi/v1/userTrades", {"symbol": symbol, "limit": limit})

    def income(self, symbol=None, start_ms=None, income_type=None, limit=200):
        """GERÇEK gelir kayıtları (GET /fapi/v1/income) — ground-truth PnL ölçümü için (audit #4).
        incomeType: REALIZED_PNL / COMMISSION / FUNDING_FEE / ... ; her kayıt {incomeType, income, time, symbol}."""
        p = {"limit": limit}
        if symbol:
            p["symbol"] = symbol
        if start_ms:
            p["startTime"] = int(start_ms)
        if income_type:
            p["incomeType"] = income_type
        return self._signed("GET", "/fapi/v1/income", p)
