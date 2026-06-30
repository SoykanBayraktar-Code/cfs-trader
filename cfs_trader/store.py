"""store — SQLite kalıcı durum. Reconciliation + öğrenmenin tek doğruluk kaynağı.

Tablolar:
  trades      — her işlemin tam yaşam döngüsü (açılış→kapanış), realize PnL + R
  daily_state — gün-bazlı kill-switch durumu (toplam PnL, ardışık zarar, halted?)
  decisions   — her döngüde verilen karar (girildi/reddedildi + sebep) — denetim izi
  learning    — (rejim×yön×sinyal-tipi) yuvarlanan beklenti (Faz 3)
"""
import sqlite3
import os
import json
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_open       INTEGER NOT NULL,
    ts_close      INTEGER,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,               -- LONG | SHORT
    qty           REAL NOT NULL,
    entry         REAL NOT NULL,
    sl            REAL NOT NULL,
    tp            REAL,
    leverage      INTEGER,
    risk_usdt     REAL,                         -- SL'de planlanan kayıp
    regime        TEXT,
    signal_type   TEXT,
    tape_verdict  TEXT,
    status        TEXT NOT NULL DEFAULT 'OPEN', -- OPEN | CLOSED
    exit_price    REAL,
    exit_reason   TEXT,                         -- SL | TP | MANUAL | KILLSWITCH
    pnl_usdt      REAL,
    r_multiple    REAL,
    fees_usdt     REAL,
    entry_order_id TEXT,
    sl_order_id    TEXT,
    tp_order_id    TEXT,
    mode          TEXT,                         -- testnet | live
    dry_run       INTEGER,
    sl_init       REAL,                         -- ilk SL (R-birimi hesabı için sabit kalır)
    peak_price    REAL,                         -- lehte gidilen en uç fiyat (high-water mark)
    trail_state   TEXT DEFAULT 'INIT'           -- INIT | BE (breakeven) | TRAIL
);
CREATE TABLE IF NOT EXISTS daily_state (
    day           TEXT PRIMARY KEY,             -- YYYY-MM-DD (UTC)
    realized_pnl  REAL NOT NULL DEFAULT 0,
    consec_losses INTEGER NOT NULL DEFAULT 0,
    trades_count  INTEGER NOT NULL DEFAULT 0,
    halted        INTEGER NOT NULL DEFAULT 0,
    halt_reason   TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    symbol    TEXT,
    side      TEXT,
    action    TEXT NOT NULL,                    -- ENTER | REJECT
    reason    TEXT,
    detail    TEXT                              -- JSON
);
CREATE TABLE IF NOT EXISTS learning (
    key       TEXT PRIMARY KEY,                 -- regime|side|signal_type
    n         INTEGER NOT NULL DEFAULT 0,
    sum_r     REAL NOT NULL DEFAULT 0,
    wins      INTEGER NOT NULL DEFAULT 0,
    updated   INTEGER
);
CREATE TABLE IF NOT EXISTS brain_decisions (         -- item 4: pretrade gate shadow-metrik (gate edge ölçümü)
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,
    symbol       TEXT, side TEXT, signal_type TEXT, regime TEXT,
    tape_verdict TEXT, tape_score REAL,
    decision     TEXT,                              -- allow | veto
    confidence   TEXT,
    reason       TEXT,
    trade_id     INTEGER                            -- allow→giren işlem id; veto→NULL (sonuç trades JOIN ile)
);
"""


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # check_same_thread=False: komut dinleyici ayrı thread'den okur; yazımlar ctx.lock ile serialize.
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self):
        """Mevcut DB'ye eksik kolonları ekle (ALTER ADD COLUMN — veri kaybı yok, idempotent)."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(trades)")}
        adds = {
            "sl_init": "REAL",
            "peak_price": "REAL",
            "trail_state": "TEXT DEFAULT 'INIT'",
            "llm_note": "TEXT",            # brain post-mortem notu (kapanışta yazılır)
            "liq_pull": "REAL",            # giriş anı likidasyon-mıknatıs yönü (-1..+1) — bağlam ölçümü
            "context_tilt": "REAL",        # uygulanan bağlam sizing-tilt'i (≤1.0)
            "brain_conviction": "REAL",    # Faz1 M2: Claude pretrade konviksiyonu 0-1 (SHADOW)
            "brain_size_hint": "REAL",     # Faz1 M2: Claude önerilen boyut çarpanı 0.5-1.0 (SHADOW, uygulanmaz)
            "sizing_confidence": "REAL",   # kazanç-ihtimali vekili 0-1 (dinamik boyut)
            "risk_pct_used": "REAL",       # bu işlemde kullanılan dinamik risk% (kasa %'si)
            "cz_snapshot": "TEXT",        # coinalyze capraz-borsa funding/OI snapshot (JSON, SHADOW)
            "ls_bias": "REAL",            # top-trader kalabalik-kontrarian bias (-1..+1)
            "ls_tilt": "REAL",            # uygulanan L/S sizing-tilt (<=1.0)
            "ls_snapshot": "TEXT",        # ham lsratio snapshot (JSON, SHADOW)
            "sq_tilt": "REAL",            # coinalyze squeeze-farkindalik sizing-tilt (<=1.0)
            "notion_page_id": "TEXT",     # Notion İşlem Kayıt Defteri sayfa id'si (kapanışta güncellemek için)
            "cvd15": "REAL",              # Faz 0 SHADOW: kline-türevli net-taker fraksiyonu 15dk
            "cvd30": "REAL",              # ... 30dk
            "cvd60": "REAL",              # ... 60dk
            "funding": "REAL",            # anlık funding oranı
            "basis_bps": "REAL",          # spot-perp basis (bps)
            "flow_regime": "TEXT",        # akış rejimi etiketi (AKIS_UP/.../NOTR)
            "confluence": "INTEGER",      # yön-uyumlu bağımsız eksen sayısı (0-5)
            "squeeze_pct": "REAL",        # Faz 1 SHADOW: BBW yüzdelik (düşük=sıkışık)
            "atr_contraction": "REAL",    # ATR şimdi/ort (<1=daralıyor)
            "oi_trend": "REAL",           # OI % değişim (yüklenme)
            "cvd_divergence": "INTEGER",  # birikim(+1)/dağıtım(-1)/yok(0)
            "book_asym": "REAL",          # defter asimetrisi (-1..+1)
            "vol_surge": "REAL",          # son bar hacmi/ort (>1.5=patlama)
            "range_pos": "REAL",          # fiyatın 20-bar aralığındaki yeri (0=dip,1=tepe) — kırılma kenarı
            "scalp_score": "INTEGER",     # yön-uyumlu bileşik scalp setup skoru (0-6)
            "derivs_bias": "TEXT",        # türev-confluence verdict (CONFIRM/CAUTION/CONFLICT/NEUTRAL) — SHADOW
            "derivs_score": "REAL",       # türev-confluence ağırlıklı skoru (-1..+1, yönle hizalı) — SHADOW
            "derivs_snapshot": "TEXT",    # derivs bileşen detayı + F&G (JSON) — SHADOW
            "pnd_phase": "TEXT",          # Pump&Dump fazı (NONE/PUMP_EARLY/PUMP_LATE/DUMPING) — SHADOW
            "pnd_score": "REAL",          # rush-order spike z-skoru — SHADOW
            "pnd_snapshot": "TEXT",       # P&D ham metrik detayı (JSON) — SHADOW
            "funding_usdt": "REAL",       # AUDIT #4: kapanışta borsadan okunan GERÇEK funding (income FUNDING_FEE); ground-truth ölçüm
            "pnl_src": "TEXT",            # AUDIT #4: PnL kaynağı — 'income' (gerçek net) | 'tahmin' (fiyat-tabanlı fallback)
        }
        for col, typ in adds.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
        # eski açık işlemlerde sl_init/peak boşsa makul varsayılanla doldur (geriye-uyum)
        self.db.execute("UPDATE trades SET sl_init=sl WHERE sl_init IS NULL AND status='OPEN'")
        self.db.execute("UPDATE trades SET peak_price=entry WHERE peak_price IS NULL AND status='OPEN'")
        self.db.execute("UPDATE trades SET trail_state='INIT' WHERE trail_state IS NULL AND status='OPEN'")

    # ---- trades ----
    def open_trade(self, **kw):
        kw.setdefault("ts_open", int(time.time()))
        kw.setdefault("status", "OPEN")
        cols = ",".join(kw.keys())
        ph = ",".join("?" * len(kw))
        cur = self.db.execute(f"INSERT INTO trades ({cols}) VALUES ({ph})", tuple(kw.values()))
        self.db.commit()
        return cur.lastrowid

    def close_trade(self, trade_id, exit_price, exit_reason, pnl_usdt, r_multiple, fees_usdt=0.0,
                    funding_usdt=None, pnl_src=None):
        # AUDIT #4: funding_usdt + pnl_src opsiyonel (geriye-uyumlu) — ground-truth ölçüm için kaydedilir.
        self.db.execute(
            """UPDATE trades SET ts_close=?, status='CLOSED', exit_price=?, exit_reason=?,
               pnl_usdt=?, r_multiple=?, fees_usdt=?, funding_usdt=?, pnl_src=? WHERE id=?""",
            (int(time.time()), exit_price, exit_reason, pnl_usdt, r_multiple, fees_usdt,
             funding_usdt, pnl_src, trade_id),
        )
        self.db.commit()

    def get_trade(self, trade_id):
        """Tek işlemin tam satırı (Row) | None — Notion loglama property'lerini kurmak için."""
        return self.db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()

    def set_notion_page_id(self, trade_id, page_id):
        """Girişte oluşturulan Notion sayfa id'sini sakla (kapanışta aynı satır güncellenir)."""
        self.db.execute("UPDATE trades SET notion_page_id=? WHERE id=?", (str(page_id), trade_id))
        self.db.commit()

    def update_trade_sl(self, trade_id, new_sl, sl_order_id=None, peak_price=None, trail_state=None):
        """Trailing/breakeven: açık işlemin SL'sini (ve isteğe bağlı algo-id/peak/durum) güncelle."""
        sets, vals = ["sl=?"], [new_sl]
        if sl_order_id is not None:
            sets.append("sl_order_id=?"); vals.append(str(sl_order_id))
        if peak_price is not None:
            sets.append("peak_price=?"); vals.append(peak_price)
        if trail_state is not None:
            sets.append("trail_state=?"); vals.append(trail_state)
        vals.append(trade_id)
        self.db.execute(f"UPDATE trades SET {','.join(sets)} WHERE id=?", tuple(vals))
        self.db.commit()

    def update_trade_peak(self, trade_id, peak_price):
        """Sadece high-water mark'ı güncelle (SL taşınmasa bile peak ilerler)."""
        self.db.execute("UPDATE trades SET peak_price=? WHERE id=?", (peak_price, trade_id))
        self.db.commit()

    def set_trade_note(self, trade_id, note):
        """brain post-mortem notunu işleme yaz (llm_note)."""
        self.db.execute("UPDATE trades SET llm_note=? WHERE id=?", (note, trade_id))
        self.db.commit()

    def open_trades(self):
        return self.db.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()

    def open_count(self):
        return self.db.execute("SELECT COUNT(*) c FROM trades WHERE status='OPEN'").fetchone()["c"]

    # ---- daily_state ----
    def day_state(self, day):
        row = self.db.execute("SELECT * FROM daily_state WHERE day=?", (day,)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO daily_state (day) VALUES (?)", (day,))
            self.db.commit()
            row = self.db.execute("SELECT * FROM daily_state WHERE day=?", (day,)).fetchone()
        return row

    def apply_close_to_day(self, day, pnl_usdt):
        st = self.day_state(day)
        consec = 0 if pnl_usdt > 0 else st["consec_losses"] + 1
        self.db.execute(
            """UPDATE daily_state SET realized_pnl=realized_pnl+?, trades_count=trades_count+1,
               consec_losses=? WHERE day=?""",
            (pnl_usdt, consec, day),
        )
        self.db.commit()
        return self.day_state(day)

    def halt_day(self, day, reason):
        self.db.execute("UPDATE daily_state SET halted=1, halt_reason=? WHERE day=?", (reason, day))
        self.db.commit()

    # ---- decisions ----
    def log_decision(self, symbol, side, action, reason, detail=None):
        self.db.execute(
            "INSERT INTO decisions (ts, symbol, side, action, reason, detail) VALUES (?,?,?,?,?,?)",
            (int(time.time()), symbol, side, action, reason, json.dumps(detail or {})),
        )
        self.db.commit()

    # ---- learning ----
    def update_learning(self, regime, side, signal_type, r_multiple):
        key = f"{regime}|{side}|{signal_type}"
        self.db.execute(
            """INSERT INTO learning (key, n, sum_r, wins, updated) VALUES (?,1,?,?,?)
               ON CONFLICT(key) DO UPDATE SET n=n+1, sum_r=sum_r+excluded.sum_r,
               wins=wins+excluded.wins, updated=excluded.updated""",
            (key, r_multiple, 1 if r_multiple > 0 else 0, int(time.time())),
        )
        self.db.commit()

    def expectancy(self, regime, side, signal_type):
        key = f"{regime}|{side}|{signal_type}"
        row = self.db.execute("SELECT n, sum_r FROM learning WHERE key=?", (key,)).fetchone()
        if not row or row["n"] == 0:
            return None, 0
        return row["sum_r"] / row["n"], row["n"]

    # ---- brain shadow-metrik (item 4) ----
    def log_brain_decision(self, decision, confidence, reason, cand, trade_id=None):
        """pretrade gate kararını kaydet. allow→trade_id (giren işlem), veto→None."""
        self.db.execute(
            "INSERT INTO brain_decisions (ts,symbol,side,signal_type,regime,tape_verdict,tape_score,"
            "decision,confidence,reason,trade_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), cand.symbol, cand.side, cand.status, cand.regime,
             cand.tape_verdict, cand.tape_score, decision, confidence, str(reason)[:300], trade_id),
        )
        self.db.commit()

    def brain_gate_stats(self):
        """Gate edge ölçümü: veto/allow sayıları + allow→kapanan işlemlerin ort. R'si ve kazanma oranı."""
        counts = {r["decision"]: r["c"] for r in self.db.execute(
            "SELECT decision, COUNT(*) c FROM brain_decisions GROUP BY decision")}
        a = self.db.execute(
            "SELECT COUNT(*) n, AVG(t.r_multiple) avg_r, "
            "SUM(CASE WHEN t.r_multiple>0 THEN 1 ELSE 0 END) wins "
            "FROM brain_decisions b JOIN trades t ON b.trade_id=t.id "
            "WHERE b.decision='allow' AND t.status='CLOSED'").fetchone()
        return {
            "veto": counts.get("veto", 0),
            "allow": counts.get("allow", 0),
            "allow_closed_n": a["n"] or 0,
            "allow_avg_r": round(a["avg_r"], 3) if a["avg_r"] is not None else None,
            "allow_wins": a["wins"] or 0,
        }

    def context_stats(self):
        """Bağlam (liq_pull) tilt ölçümü: liq_pull adayın yönüyle UYUŞAN vs ÇELİŞEN kapanan işlemler.
        + = yukarı mıknatıs; LONG için liq_pull>0 = uyuşma, SHORT için liq_pull<0 = uyuşma."""
        rows = self.db.execute(
            "SELECT side, liq_pull, r_multiple FROM trades "
            "WHERE status='CLOSED' AND r_multiple IS NOT NULL AND liq_pull IS NOT NULL"
        ).fetchall()
        agree = [r for r in rows if (r["liq_pull"] > 0) == (r["side"] == "LONG") and abs(r["liq_pull"]) >= 0.05]
        dis = [r for r in rows if (r["liq_pull"] > 0) != (r["side"] == "LONG") and abs(r["liq_pull"]) >= 0.05]

        def _wr(s):
            n = len(s); w = sum(1 for x in s if x["r_multiple"] > 0)
            r = sum(x["r_multiple"] for x in s)
            return {"n": n, "wins": w, "winrate": round(100.0 * w / n, 0) if n else None, "sum_r": round(r, 2)}
        return {"agree": _wr(agree), "disagree": _wr(dis), "total_with_liq": len(rows)}

    def brain_conviction_stats(self):
        """Faz1 M2 SHADOW: Claude pretrade konviksiyonu (yüksek≥0.6 vs düşük) → kapanan işlem sonucu.
        Konviksiyon işlem kalitesini öngörüyor mu (yüksek-konv daha mı kazanıyor)?"""
        rows = self.db.execute(
            "SELECT brain_conviction, brain_size_hint, r_multiple FROM trades "
            "WHERE status='CLOSED' AND r_multiple IS NOT NULL AND brain_conviction IS NOT NULL"
        ).fetchall()
        hi = [r for r in rows if r["brain_conviction"] >= 0.6]
        lo = [r for r in rows if r["brain_conviction"] < 0.6]

        def _wr(s):
            n = len(s); w = sum(1 for x in s if x["r_multiple"] > 0)
            return {"n": n, "wins": w, "winrate": round(100.0 * w / n, 0) if n else None,
                    "sum_r": round(sum(x["r_multiple"] for x in s), 2)}
        return {"high_conv": _wr(hi), "low_conv": _wr(lo), "total": len(rows)}

    def close(self):
        self.db.close()
