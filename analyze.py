#!/usr/bin/env python3
"""
Phase 1.5: 金利差の持続性分析

recorder.py が溜めた data/funding.db を読み、
「エントリー時点の金利差(理論年率)が、その後どれだけ実際に取れたか」を分析する。

ポイント: エントリー時に選んだ取引所ペアは固定して追跡する
(実運用ではペアを無料で乗り換えられないため。これが正直なシミュレーション)。

使い方:
  python3 analyze.py                # 全期間を分析
  python3 analyze.py --entry-apr 30 # エントリー条件: 年率30%以上の差
  python3 analyze.py --top 20
"""
import argparse
import os
import sqlite3
import statistics
import sys
from datetime import datetime

HOURS_PER_YEAR = 24 * 365


def load(db_path, since_ts=0):
    """ts -> coin -> venue -> 年率換算した資金調達率(%)"""
    con = sqlite3.connect(db_path)
    cur = con.execute(
        "SELECT ts, venue, coin, rate, interval_h FROM funding WHERE ts >= ? ORDER BY ts",
        (since_ts,))
    snaps = {}
    for ts, venue, coin, rate, interval_h in cur:
        apr = rate * (HOURS_PER_YEAR / interval_h) * 100
        snaps.setdefault(ts, {}).setdefault(coin, {})[venue] = apr
    con.close()
    return snaps


def best_pair(venues):
    """最も低い所でロング・最も高い所でショート。(long, short, spread%)"""
    long_v = min(venues, key=venues.get)
    short_v = max(venues, key=venues.get)
    return long_v, short_v, venues[short_v] - venues[long_v]


def realized_carry(snaps, ts_list, coin, long_v, short_v, t0, horizon_h):
    """t0からhorizon_h時間、ペア固定で保有した場合の実現リターン(%)。
    各スナップショット間の金利差(年率)を時間積分する。"""
    total = 0.0
    covered_h = 0.0
    prev_ts, prev_diff = None, None
    for ts in ts_list:
        if ts < t0:
            continue
        if ts > t0 + horizon_h * 3600:
            break
        venues = snaps[ts].get(coin, {})
        if long_v not in venues or short_v not in venues:
            continue
        diff = venues[short_v] - venues[long_v]
        if prev_ts is not None:
            dt_h = (ts - prev_ts) / 3600
            if dt_h <= 1.0:  # 記録が途切れた区間は積分しない
                total += prev_diff * dt_h / HOURS_PER_YEAR
                covered_h += dt_h
        prev_ts, prev_diff = ts, diff
    return total, covered_h


def episodes(ts_list, spread_series, threshold, max_gap_s, sample_h):
    """spreadがthreshold以上の連続区間を列挙 → 持続時間(h)のリスト。
    1スナップショットのみの機会もサンプリング間隔1回分として数える。"""
    durations = []
    start, last = None, None
    for ts in ts_list:
        s = spread_series.get(ts)
        active = s is not None and s >= threshold
        if active:
            if start is None or (last is not None and ts - last > max_gap_s):
                if start is not None:
                    durations.append((last - start) / 3600 + sample_h)
                start = ts
            last = ts
        else:
            if start is not None:
                durations.append((last - start) / 3600 + sample_h)
                start = None
    if start is not None:
        durations.append((last - start) / 3600 + sample_h)
    return durations


def fmt_h(h):
    return f"{h:.1f}h" if h < 48 else f"{h/24:.1f}日"


def main():
    ap = argparse.ArgumentParser(description="金利差の持続性分析")
    default_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "funding.db")
    ap.add_argument("--db", default=default_db)
    ap.add_argument("--entry-apr", type=float, default=20.0,
                    help="エントリー条件: 金利差の年率%%がこれ以上 (default: 20)")
    ap.add_argument("--fee", type=float, default=0.22,
                    help="往復手数料%%(テイカー4回分, default: 0.22)")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--confirm-h", type=float, default=0.0,
                    help="エントリー前に金利差の持続を確認する時間h (default: 0=即エントリー)")
    ap.add_argument("--avg-h", type=float, default=0.0,
                    help="過去N時間の平均金利差でエントリー判定 (default: 0=瞬間値)")
    ap.add_argument("--since", help="この日時以降のデータのみ分析 (例: 2026-07-08 17:00)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"データがありません: {args.db}\n先に recorder.py を動かしてください。")
        sys.exit(1)

    since_ts = 0
    if args.since:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                since_ts = int(datetime.strptime(args.since, fmt).timestamp())
                break
            except ValueError:
                continue
        else:
            print(f"--since の形式が不正です: {args.since}")
            sys.exit(1)

    snaps = load(args.db, since_ts)
    ts_list = sorted(snaps)
    if not ts_list:
        print("データが空です。")
        sys.exit(1)

    span_h = (ts_list[-1] - ts_list[0]) / 3600
    print(f"=== 金利差の持続性分析 ===")
    print(f"データ期間: {datetime.fromtimestamp(ts_list[0]):%m/%d %H:%M} 〜 "
          f"{datetime.fromtimestamp(ts_list[-1]):%m/%d %H:%M} "
          f"({fmt_h(span_h)}, スナップショット{len(ts_list)}回)")

    if len(ts_list) < 2:
        print("\nまだスナップショットが1回分しかありません。"
              "recorder が10分ごとに記録するので、2〜3日後に再実行してください。")
        sys.exit(0)

    gaps = [b - a for a, b in zip(ts_list, ts_list[1:])]
    max_gap_s = statistics.median(gaps) * 2.5

    # コインごとの spread 時系列(ペアは各時点のベスト)
    all_coins = set()
    for d in snaps.values():
        all_coins.update(c for c, v in d.items() if len(v) >= 2)
    spread_by_coin = {}
    for coin in all_coins:
        series = {}
        for ts in ts_list:
            venues = snaps[ts].get(coin, {})
            if len(venues) >= 2:
                series[ts] = best_pair(venues)[2]
        if series:
            spread_by_coin[coin] = series

    # --- 1) 閾値別の持続時間 ---
    print(f"\n[1] 金利差の持続時間(全{len(spread_by_coin)}コイン)")
    print(f"{'閾値(年率)':>10} {'機会数':>6} {'持続中央値':>10} {'持続p75':>8}")
    sample_h = statistics.median(gaps) / 3600
    for thr in (10, 20, 50, 100):
        durs = []
        for series in spread_by_coin.values():
            durs += episodes(ts_list, series, thr, max_gap_s, sample_h)
        if durs:
            med = statistics.median(durs)
            p75 = sorted(durs)[int(len(durs) * 0.75)]
            print(f"{thr:>9}% {len(durs):>6} {fmt_h(med):>10} {fmt_h(p75):>8}")
        else:
            print(f"{thr:>9}% {'0':>6} {'-':>10} {'-':>8}")

    # --- 2) ペア固定シミュレーション ---
    # 各コインについて「最初に entry-apr を超えた時点」でエントリーしたと仮定
    results = []
    for coin, series in spread_by_coin.items():
        entry_ts = None
        chosen = None
        if args.avg_h > 0:
            # 過去avg_h時間の「取引所ごとの平均金利」で判定し、ペアも平均で選ぶ
            # (瞬間スパイクのペアを掴まないため)
            vhist = {}  # venue -> [(ts, apr)]
            for ts in ts_list:
                venues = snaps[ts].get(coin, {})
                for v, apr in venues.items():
                    vhist.setdefault(v, []).append((ts, apr))
                cutoff = ts - args.avg_h * 3600
                for lst in vhist.values():
                    while lst and lst[0][0] < cutoff:
                        lst.pop(0)
                if ts - ts_list[0] < args.avg_h * 3600 * 0.8:
                    continue
                # サンプル数でなく「窓の何割の時間帯をカバーしているか」で判定
                # (Macスリープ等で記録が疎でも分析できるように)
                avgs = {v: sum(x[1] for x in lst) / len(lst)
                        for v, lst in vhist.items()
                        if len(lst) >= 5
                        and lst[-1][0] - lst[0][0] >= args.avg_h * 3600 * 0.5}
                if len(avgs) < 2:
                    continue
                lv = min(avgs, key=avgs.get)
                sv = max(avgs, key=avgs.get)
                spread = avgs[sv] - avgs[lv]
                # 両取引所に現時点でも板があることを条件にエントリー
                if spread >= args.entry_apr and lv in venues and sv in venues:
                    entry_ts = ts
                    chosen = (lv, sv, spread)
                    break
        else:
            # entry_apr以上の差が confirm_h 時間続いたのを確認した時点でエントリー
            run_start, last_ok = None, None
            for ts in ts_list:
                s = series.get(ts)
                if s is not None and s >= args.entry_apr:
                    if run_start is None or ts - last_ok > max_gap_s:
                        run_start = ts
                    last_ok = ts
                    if ts - run_start >= args.confirm_h * 3600:
                        entry_ts = ts
                        break
                else:
                    run_start = None
        if entry_ts is None:
            continue
        if chosen:
            long_v, short_v, entry_spread = chosen
        else:
            long_v, short_v, entry_spread = best_pair(snaps[entry_ts][coin])
        realized, covered = realized_carry(
            snaps, ts_list, coin, long_v, short_v, entry_ts, 72)
        results.append({
            "coin": coin, "pair": f"{long_v}→{short_v}",
            "entry": entry_spread,
            "realized": realized, "covered": covered,
        })
    results.sort(key=lambda r: r["entry"], reverse=True)

    if args.avg_h > 0:
        cond = f"過去{args.avg_h:g}h平均が年率{args.entry_apr}%以上"
    else:
        cond = f"年率{args.entry_apr}%以上"
        if args.confirm_h > 0:
            cond += f"が{args.confirm_h}h持続"
    print(f"\n[2] ペア固定シミュレーション(エントリー条件: {cond}, "
          f"手数料{args.fee}%控除後)")
    if not results:
        print("エントリー条件を満たしたコインなし。--entry-apr を下げて再実行してください。")
        return
    hdr = (f"{'コイン':<10} {'ペア(L→S)':<22} {'入時年率%':>8} "
           f"{'実測時間':>8} {'実現%(手数料後)':>13} {'実現年率%':>9}")
    print(hdr)
    print("-" * len(hdr))
    MIN_COVERED_H = 6
    ann_list = []
    for r in results:
        if r["covered"] >= MIN_COVERED_H:
            r["net"] = r["realized"] - args.fee
            r["ann"] = r["net"] / (r["covered"] / HOURS_PER_YEAR)
            ann_list.append(r["ann"])
    for r in results[:args.top]:
        if "ann" in r:
            print(f"{r['coin']:<10} {r['pair']:<22} {r['entry']:>8.1f} "
                  f"{fmt_h(r['covered']):>8} {r['net']:>13.3f} {r['ann']:>9.1f}")
        else:
            print(f"{r['coin']:<10} {r['pair']:<22} {r['entry']:>8.1f} "
                  f"{fmt_h(r['covered']):>8} {'(実測不足)':>10} {'-':>9}")

    if ann_list:
        entry_med = statistics.median(r["entry"] for r in results)
        print(f"\n入時年率の中央値 {entry_med:.1f}% に対し、"
              f"実現年率(手数料控除後)の中央値: {statistics.median(ann_list):.1f}% "
              f"(実測{MIN_COVERED_H}h以上の{len(ann_list)}件)")
    print(f"\n(実測不足) = 記録の途切れが多く実測{MIN_COVERED_H}時間未満")
    print("注意: 約定スリッページ・価格乖離は含まない。実際のリターンはこれより低くなる。")


if __name__ == "__main__":
    main()
