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
    dry_run       INTEGER
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
"""


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # check_same_thread=False: komut dinleyici ayrı thread'den okur; yazımlar ctx.lock ile serialize.
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self.db.commit()

    # ---- trades ----
    def open_trade(self, **kw):
        kw.setdefault("ts_open", int(time.time()))
        kw.setdefault("status", "OPEN")
        cols = ",".join(kw.keys())
        ph = ",".join("?" * len(kw))
        cur = self.db.execute(f"INSERT INTO trades ({cols}) VALUES ({ph})", tuple(kw.values()))
        self.db.commit()
        return cur.lastrowid

    def close_trade(self, trade_id, exit_price, exit_reason, pnl_usdt, r_multiple, fees_usdt=0.0):
        self.db.execute(
            """UPDATE trades SET ts_close=?, status='CLOSED', exit_price=?, exit_reason=?,
               pnl_usdt=?, r_multiple=?, fees_usdt=? WHERE id=?""",
            (int(time.time()), exit_price, exit_reason, pnl_usdt, r_multiple, fees_usdt, trade_id),
        )
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

    def close(self):
        self.db.close()
