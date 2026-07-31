#!/usr/bin/env python3
"""
Phase 1.5: 資金調達率スナップショット記録

funding_monitor.py の取得ロジックを使い、全取引所の資金調達率を
SQLite (data/funding.db) に追記する。launchd から10分ごとに呼ばれる想定。

使い方:
  python3 recorder.py --snapshot   # 1回記録して終了(launchd用)
  python3 recorder.py --loop 600   # 600秒ごとに記録し続ける(手動実行用)
"""
import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from funding_monitor import FETCHERS

# 記録対象の24h出来高下限。極端に薄い銘柄はノイズなので省く(vol不明は記録する)
MIN_VOL = 1e6

SCHEMA = """
CREATE TABLE IF NOT EXISTS funding(
    ts INTEGER NOT NULL,
    venue TEXT NOT NULL,
    coin TEXT NOT NULL,
    rate REAL NOT NULL,
    interval_h REAL NOT NULL,
    vol REAL
);
CREATE INDEX IF NOT EXISTS idx_coin_ts ON funding(coin, ts);
CREATE INDEX IF NOT EXISTS idx_ts ON funding(ts);
"""


def snapshot(db_path):
    con = sqlite3.connect(db_path, timeout=30)
    con.executescript(SCHEMA)
    ts = int(time.time())
    rows, errors = [], []
    with ThreadPoolExecutor(max_workers=len(FETCHERS)) as pool:
        futures = {name: pool.submit(fn) for name, fn in FETCHERS.items()}
        for name, fut in futures.items():
            try:
                for coin, e in fut.result().items():
                    if e["vol"] is not None and e["vol"] < MIN_VOL:
                        continue
                    rows.append((ts, name, coin, e["rate"], e["interval_h"], e["vol"]))
            except Exception as ex:
                errors.append(f"{name}: {type(ex).__name__}: {ex}")
    con.executemany("INSERT INTO funding VALUES(?,?,?,?,?,?)", rows)
    con.commit()
    n_snapshots = con.execute("SELECT COUNT(DISTINCT ts) FROM funding").fetchone()[0]
    con.close()
    msg = f"{datetime.now():%Y-%m-%d %H:%M:%S} 記録 {len(rows)}行 (累計スナップショット {n_snapshots})"
    if errors:
        msg += " | 失敗: " + "; ".join(errors)
    print(msg, flush=True)


def main():
    ap = argparse.ArgumentParser(description="資金調達率レコーダー")
    default_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "funding.db")
    ap.add_argument("--db", default=default_db)
    ap.add_argument("--snapshot", action="store_true", help="1回記録して終了")
    ap.add_argument("--loop", type=int, default=0, help="指定秒ごとに記録し続ける")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.db), exist_ok=True)
    while True:
        try:
            snapshot(args.db)
        except Exception as e:
            print(f"{datetime.now():%Y-%m-%d %H:%M:%S} エラー: {type(e).__name__}: {e}", flush=True)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
