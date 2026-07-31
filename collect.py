#!/usr/bin/env python3
"""
クラウド収集(GitHub Actions用): 到達可能な取引所の資金調達率を集め、
- 追跡ペア(検証済みの固定キャリー)の現在スプレッドを計算
- 全到達取引所から新規のキャリー機会を発掘
- docs/data/latest.json(ダッシュボード用) と docs/data/history/YYYY-MM.jsonl(履歴) を更新
- 閾値超えなら Discord Webhook に通知(環境変数 DISCORD_WEBHOOK があれば)

米国ランナー(GH Actions)からは Binance(451)/Bybit(403) が遮断されるため除外。
"""
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from funding_monitor import FETCHERS, HOURS_PER_YEAR

# --- 設定(環境変数で上書き可) ---
EXCLUDE = set(os.environ.get("EXCLUDE_VENUES", "Binance,Bybit").split(","))
LEG_MIN_VOL = float(os.environ.get("LEG_MIN_VOL", 3e6))   # 発掘で「脚」に採用する最低24h出来高
THIN_VOL = float(os.environ.get("THIN_VOL", 1e6))         # これ未満は追跡ペアで「薄い」警告
COST_BUFFER = float(os.environ.get("COST_BUFFER", 3.0))  # ネット概算で引く年率コスト%
WATCH_ALERT = float(os.environ.get("WATCH_ALERT", 12.0))   # 追跡ペアの通知閾値(年率%)
DISCOVER_ALERT = float(os.environ.get("DISCOVER_ALERT", 30.0))  # 発掘機会の通知閾値
PER_TIMEOUT = 25

# 発掘対象の主要コイン(流動性のある確立銘柄のみ。新規上場の薄商いノイズを排除)
UNIVERSE = set(os.environ.get("UNIVERSE", "").split(",")) if os.environ.get("UNIVERSE") else {
    "BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LTC", "BCH", "LINK", "AVAX",
    "DOT", "POL", "MATIC", "TRX", "ATOM", "UNI", "ETC", "FIL", "APT", "ARB",
    "OP", "SUI", "NEAR", "INJ", "TIA", "SEI", "AAVE", "MKR", "RUNE", "LDO",
    "ONDO", "ENA", "PEPE", "WIF", "BONK", "SHIB", "TON", "HBAR", "XLM", "ALGO",
}

# 検証済みの固定ペア(long=受取側/最も低い funding、short=支払わせる側)
# 到達不可の脚(Binance/Bybit)を含むものは long_alt に控えを持つ
WATCHED = [
    {"coin": "SOL", "long": "dYdX", "short": "HTX"},
    {"coin": "BTC", "long": "dYdX", "short": "Hyperliquid"},
    {"coin": "ETH", "long": "dYdX", "short": "Hyperliquid"},
    {"coin": "ADA", "long": "Binance", "short": "HTX"},   # long脚は米国遮断→N/A表示
    {"coin": "LTC", "long": "Bybit", "short": "HTX"},
    {"coin": "XRP", "long": "Bybit", "short": "HTX"},
]


def annual_pct(entry):
    """1回あたりrate → 年率%"""
    return entry["rate"] * (HOURS_PER_YEAR / entry["interval_h"]) * 100.0


def runner_info():
    for url in ("https://ipinfo.io/json", "https://ipapi.co/json/"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "collect/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                j = json.load(r)
            return f'{j.get("ip","?")} / {j.get("country") or j.get("country_name","?")}'
        except Exception:
            continue
    return "?"


def fetch_all():
    venues, failed = {}, {}
    targets = {n: fn for n, fn in FETCHERS.items() if n not in EXCLUDE}
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futs = {pool.submit(fn): n for n, fn in targets.items()}
        for fut, name in list(futs.items()):
            try:
                venues[name] = fut.result(timeout=PER_TIMEOUT)
            except FutTimeout:
                failed[name] = "timeout"
            except Exception as e:
                failed[name] = f"{type(e).__name__}: {str(e)[:80]}"
    return venues, failed


def build_coin_map(venues):
    """coin -> {venue: {ann, interval_h, vol}}(フィルタなし・全件)"""
    m = {}
    for vname, data in venues.items():
        for coin, e in data.items():
            m.setdefault(coin, {})[vname] = {
                "ann": round(annual_pct(e), 2), "interval_h": e["interval_h"],
                "vol": e.get("vol"),
            }
    return m


def _thin(v):
    return v is not None and v < THIN_VOL


def watched_rows(coin_map):
    """追跡ペアは出来高フィルタを掛けず必ず計算。各脚の出来高と薄さ警告を付す。"""
    rows = []
    for w in WATCHED:
        cm = coin_map.get(w["coin"], {})
        L = cm.get(w["long"]); S = cm.get(w["short"])
        row = {"coin": w["coin"], "long": w["long"], "short": w["short"],
               "long_ann": L["ann"] if L else None,
               "short_ann": S["ann"] if S else None,
               "long_vol": L["vol"] if L else None,
               "short_vol": S["vol"] if S else None}
        if L and S:
            gross = round(S["ann"] - L["ann"], 2)
            row["gross"] = gross
            row["net_est"] = round(gross - COST_BUFFER, 2)
            warn = [f'{w["long"]}薄い' for _ in (1,) if _thin(L["vol"])] + \
                   [f'{w["short"]}薄い' for _ in (1,) if _thin(S["vol"])]
            row["status"] = "ok" if not warn else "注意: " + "/".join(warn)
        else:
            missing = w["long"] if not L else w["short"]
            row["gross"] = None; row["net_est"] = None
            row["status"] = f"{missing}=到達不可" if missing in EXCLUDE else f"{missing}=データなし"
        rows.append(row)
    return rows


def discover(coin_map, top=15):
    """主要コイン(UNIVERSE)限定。脚は出来高LEG_MIN_VOL以上(vol不明の取引所は許容)。"""
    ops = []
    for coin, cm in coin_map.items():
        if coin not in UNIVERSE:
            continue
        cand = {v: e for v, e in cm.items()
                if e["vol"] is None or e["vol"] >= LEG_MIN_VOL}
        if len(cand) < 2:
            continue
        s_v = max(cand, key=lambda v: cand[v]["ann"])
        l_v = min(cand, key=lambda v: cand[v]["ann"])
        if s_v == l_v:
            continue
        gross = round(cand[s_v]["ann"] - cand[l_v]["ann"], 2)
        if gross <= 0:
            continue
        ops.append({"coin": coin, "long": l_v, "short": s_v,
                    "long_ann": cand[l_v]["ann"], "short_ann": cand[s_v]["ann"],
                    "long_vol": cand[l_v]["vol"], "short_vol": cand[s_v]["vol"],
                    "gross": gross, "net_est": round(gross - COST_BUFFER, 2),
                    "n_venues": len(cand)})
    ops.sort(key=lambda o: -o["gross"])
    return ops[:top]


def notify_discord(watched, ops):
    url = os.environ.get("DISCORD_WEBHOOK")
    if not url:
        return False
    hits = [w for w in watched if w.get("gross") is not None and w["gross"] >= WATCH_ALERT]
    disc = [o for o in ops if o["gross"] >= DISCOVER_ALERT]
    if not hits and not disc:
        return False
    lines = ["**📈 資金調達率アービ アラート**"]
    for w in hits:
        lines.append(f'・追跡 {w["coin"]} {w["long"]}→{w["short"]}: '
                     f'グロス{w["gross"]:+.1f}%/yr (ネット概算{w["net_est"]:+.1f}%)')
    for o in disc[:5]:
        lines.append(f'・発掘 {o["coin"]} {o["long"]}→{o["short"]}: グロス{o["gross"]:+.1f}%/yr')
    body = json.dumps({"content": "\n".join(lines)}).encode()
    try:
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f"Discord通知失敗: {e}", flush=True)
        return False


def main():
    ts = int(time.time())
    venues, failed = fetch_all()
    coin_map = build_coin_map(venues)
    watched = watched_rows(coin_map)
    ops = discover(coin_map)

    latest = {
        "generated_at": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
        "unix_ts": ts,
        "runner": runner_info(),
        "venues_ok": sorted(venues.keys()),
        "venues_failed": failed,
        "excluded": sorted(EXCLUDE),
        "params": {"leg_min_vol": LEG_MIN_VOL, "thin_vol": THIN_VOL,
                   "cost_buffer_pct": COST_BUFFER,
                   "watch_alert": WATCH_ALERT, "discover_alert": DISCOVER_ALERT},
        "watched": watched,
        "opportunities": ops,
    }

    data_dir = os.path.join(HERE, "docs", "data")
    hist_dir = os.path.join(data_dir, "history")
    os.makedirs(hist_dir, exist_ok=True)
    with open(os.path.join(data_dir, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1)

    # 履歴は軽量1行(追跡ペアのグロス + 発掘トップ3)
    hist_line = {"ts": ts,
                 "watched": {w["coin"]: w["gross"] for w in watched if w["gross"] is not None},
                 "top": [{"c": o["coin"], "g": o["gross"], "l": o["long"], "s": o["short"]}
                         for o in ops[:3]]}
    month = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")
    with open(os.path.join(hist_dir, f"{month}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(hist_line, ensure_ascii=False) + "\n")

    sent = notify_discord(watched, ops)

    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} 到達{len(venues)}取引所 失敗{len(failed)} "
          f"発掘{len(ops)}件 通知={'送信' if sent else 'なし'}", flush=True)
    for w in watched:
        g = f'{w["gross"]:+.1f}%' if w["gross"] is not None else w["status"]
        print(f'  追跡 {w["coin"]:<4} {w["long"]}→{w["short"]}: {g}', flush=True)
    if ops:
        o = ops[0]
        print(f'  発掘トップ: {o["coin"]} {o["long"]}→{o["short"]} {o["gross"]:+.1f}%/yr', flush=True)


if __name__ == "__main__":
    main()
