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

# --- 設定(環境変数で上書き可。未設定/空文字は既定値に落ちる) ---
def envf(name, default):
    return float(os.environ.get(name) or default)

EXCLUDE = set((os.environ.get("EXCLUDE_VENUES") or "Binance,Bybit").split(","))
LEG_MIN_VOL = envf("LEG_MIN_VOL", 3e6)   # 発掘で「脚」に採用する最低24h出来高
THIN_VOL = envf("THIN_VOL", 1e6)         # これ未満は追跡ペアで「薄い」警告
COST_BUFFER = envf("COST_BUFFER", 3.0)   # ネット概算で引く年率コスト%
WATCH_ALERT = envf("WATCH_ALERT", 12.0)   # 追跡ペアの通知閾値(年率%)
DISCOVER_ALERT = envf("DISCOVER_ALERT", 30.0)  # 発掘機会の通知閾値
PER_TIMEOUT = 25

# ペーパー損益(もし実弾で投資していたら) — 到達可能な追跡ペアに均等配分
PAPER_CAPITAL = envf("PAPER_CAPITAL", 1_000_000)   # 総投下資本(円)
PAPER_COINS = [c for c in (os.environ.get("PAPER_COINS") or "SOL,BTC,ETH").split(",") if c]
# 実費モデル(手数料・スリッページ・約定・清算リスク)
FEE_BPS = envf("FEE_BPS", 5.0)            # 片脚1約定あたり手数料(bps, taker想定)
SLIP_BASE_BPS = envf("SLIP_BASE_BPS", 3.0)  # スリッページ基礎(bps/約定)
SLIP_K = envf("SLIP_K", 0.5)             # 板インパクト係数(注文額/日次出来高)
RISK_DRAG_ANN = envf("RISK_DRAG_ANN", 1.5)  # 清算・リバランスのリスク調整(年率%控除)
FX_JPY_USD = envf("FX_JPY_USD", 150.0)   # 出来高(USD)と注文額(円)の換算
FILL_WARN_FRAC = envf("FILL_WARN_FRAC", 0.02)  # 注文が日次出来高のこの割合超で約定警告


def slip_bps(order_usd, vol_usd):
    """1約定あたりスリッページ(bps)。薄い脚ほど板インパクトで増える"""
    if not vol_usd or vol_usd <= 0:
        return SLIP_BASE_BPS + 5.0   # 出来高不明はやや上乗せ
    impact = SLIP_K * (order_usd / vol_usd) * 10000.0
    return min(SLIP_BASE_BPS + impact, 150.0)   # 上限150bps


def roundtrip_cost_bps(long_vol, short_vol, order_usd):
    """建て+仕舞いの往復(各脚2約定=計4約定)の手数料+スリッページ合計(bps)"""
    sl = slip_bps(order_usd, long_vol)
    ss = slip_bps(order_usd, short_vol)
    return (FEE_BPS + sl) * 2 + (FEE_BPS + ss) * 2

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


def _pair_label(coin):
    for w in WATCHED:
        if w["coin"] == coin:
            return f'{w["long"]}→{w["short"]}'
    return coin


def _load_history_points():
    """全history jsonlを時系列で [(ts, {coin: gross})] として返す(backfill用)"""
    pts = []
    hist_dir = os.path.join(HERE, "docs", "data", "history")
    if os.path.isdir(hist_dir):
        for fn in sorted(os.listdir(hist_dir)):
            if not fn.endswith(".jsonl"):
                continue
            with open(os.path.join(hist_dir, fn), encoding="utf-8") as fh:
                for line in fh:
                    try:
                        j = json.loads(line)
                        pts.append((j["ts"], j.get("watched", {})))
                    except Exception:
                        pass
    pts.sort(key=lambda x: x[0])
    return pts


def update_portfolio(watched, ts):
    """『もし¥PAPER_CAPITALを投資していたら』の実費込み損益を更新して保存。
    P&L = 累積キャリー − 往復トレードコスト(手数料+スリッページ、建て時に一括計上)
          − リスク調整(清算・リバランスを年率で控除)。約定可否も判定。"""
    path = os.path.join(HERE, "docs", "data", "portfolio.json")
    cap_each = PAPER_CAPITAL / len(PAPER_COINS)
    order_usd = cap_each / FX_JPY_USD
    info = {w["coin"]: w for w in watched}
    gross_now = {c: info[c]["gross"] for c in PAPER_COINS
                 if info.get(c) and info[c].get("gross") is not None}

    def acc(cum, rate_ann, dt_h):
        return cum + cap_each * (rate_ann / 100.0) * (dt_h / HOURS_PER_YEAR)

    pf = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            # 新スキーマ(cost_model + carry_pnl)のみ増分継続。旧スキーマは再初期化
            if "cost_model" in loaded and all("carry_pnl" in p for p in loaded.get("positions", [])):
                pf = loaded
        except Exception:
            pf = None

    if pf is not None:
        dt_h = max((ts - pf["last_ts"]) / 3600.0, 0)
        for p in pf["positions"]:
            g = gross_now.get(p["coin"])
            if g is not None:
                p["carry_pnl"] = acc(p["carry_pnl"], g, dt_h)
                p["risk_cost"] = p["risk_cost"] + cap_each * (RISK_DRAG_ANN / 100.0) * (dt_h / HOURS_PER_YEAR)
                p["gross_ann"] = round(g, 2)
                p["net_ann"] = round(g - RISK_DRAG_ANN, 2)
        pf["last_ts"] = ts
    else:
        pts = _load_history_points()
        pos = {}
        for c in PAPER_COINS:
            w = info.get(c, {})
            rt_bps = roundtrip_cost_bps(w.get("long_vol"), w.get("short_vol"), order_usd)
            warn = [nm for nm, v in (("long", w.get("long_vol")), ("short", w.get("short_vol")))
                    if v and order_usd > FILL_WARN_FRAC * v]
            g0 = gross_now.get(c, 0.0)
            pos[c] = {"coin": c, "pair": _pair_label(c), "capital": round(cap_each),
                      "carry_pnl": 0.0,
                      "trade_cost": round(cap_each * rt_bps / 10000.0, 1),
                      "risk_cost": 0.0,
                      "roundtrip_bps": round(rt_bps, 1),
                      "fill_warn": warn,
                      "gross_ann": round(g0, 2), "net_ann": round(g0 - RISK_DRAG_ANN, 2)}
        entry_ts = pts[0][0] if pts else ts
        for (t0, w0), (t1, _w1) in zip(pts, pts[1:]):
            dt_h = (t1 - t0) / 3600.0
            for c in PAPER_COINS:
                g = w0.get(c)
                if g is not None:
                    pos[c]["carry_pnl"] = acc(pos[c]["carry_pnl"], g, dt_h)
                    pos[c]["risk_cost"] += cap_each * (RISK_DRAG_ANN / 100.0) * (dt_h / HOURS_PER_YEAR)
        if pts:
            dt_h = (ts - pts[-1][0]) / 3600.0
            for c in PAPER_COINS:
                g = gross_now.get(c)
                if g is not None:
                    pos[c]["carry_pnl"] = acc(pos[c]["carry_pnl"], g, dt_h)
                    pos[c]["risk_cost"] += cap_each * (RISK_DRAG_ANN / 100.0) * (dt_h / HOURS_PER_YEAR)
        pf = {"capital": PAPER_CAPITAL, "entry_ts": entry_ts, "last_ts": ts,
              "cost_model": {"fee_bps_per_fill": FEE_BPS, "slip_base_bps": SLIP_BASE_BPS,
                             "risk_drag_ann_pct": RISK_DRAG_ANN, "fx_jpy_usd": FX_JPY_USD,
                             "order_usd_per_leg": round(order_usd)},
              "positions": list(pos.values())}

    for p in pf["positions"]:
        p["cum_pnl"] = round(p["carry_pnl"] - p["trade_cost"] - p["risk_cost"], 1)
        p["carry_pnl"] = round(p["carry_pnl"], 1)
        p["risk_cost"] = round(p["risk_cost"], 1)
    tc = sum(p["carry_pnl"] for p in pf["positions"])
    tt = sum(p["trade_cost"] for p in pf["positions"])
    tr = sum(p["risk_cost"] for p in pf["positions"])
    total = tc - tt - tr
    days = max((ts - pf["entry_ts"]) / 86400.0, 1e-9)
    pf["carry_pnl"] = round(tc, 1)
    pf["trade_cost"] = round(tt, 1)
    pf["risk_cost"] = round(tr, 1)
    pf["total_pnl"] = round(total, 1)
    pf["total_value"] = round(PAPER_CAPITAL + total, 1)
    pf["pnl_pct"] = round(total / PAPER_CAPITAL * 100, 3)
    pf["days"] = round(days, 2)
    pf["realized_ann_pct"] = round(total / PAPER_CAPITAL / days * 365 * 100, 1)
    # 巡航年利(一回性コスト除く=コスト回収後の持続レート)とブレークイーブン
    run_rate = sum(cap_each * p["net_ann"] for p in pf["positions"]
                   if p.get("net_ann") is not None) / PAPER_CAPITAL
    pf["run_rate_ann"] = round(run_rate, 1)
    daily_net = PAPER_CAPITAL * run_rate / 100.0 / 365.0
    pf["breakeven_days"] = round(tt / daily_net, 1) if daily_net > 0 else None
    pf["broke_even"] = total >= 0
    pf["fill_warns"] = sorted({p["coin"] for p in pf["positions"] if p.get("fill_warn")})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(pf, fh, ensure_ascii=False, indent=1)
    return pf


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
    pf = update_portfolio(watched, ts)

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
        "portfolio": pf,
    }

    data_dir = os.path.join(HERE, "docs", "data")
    hist_dir = os.path.join(data_dir, "history")
    os.makedirs(hist_dir, exist_ok=True)
    with open(os.path.join(data_dir, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1)

    # 履歴は軽量1行(追跡ペアのグロス + 発掘トップ3)
    hist_line = {"ts": ts,
                 "watched": {w["coin"]: w["gross"] for w in watched if w["gross"] is not None},
                 "pnl": pf["total_pnl"], "value": pf["total_value"],
                 "top": [{"c": o["coin"], "g": o["gross"], "l": o["long"], "s": o["short"]}
                         for o in ops[:3]]}
    month = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")
    with open(os.path.join(hist_dir, f"{month}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(hist_line, ensure_ascii=False) + "\n")

    sent = notify_discord(watched, ops)

    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} 到達{len(venues)}取引所 失敗{len(failed)} "
          f"発掘{len(ops)}件 通知={'送信' if sent else 'なし'}", flush=True)
    print(f'  ペーパー¥{PAPER_CAPITAL:,.0f} → 評価額¥{pf["total_value"]:,.0f} '
          f'(損益 {pf["total_pnl"]:+,.0f}円 / {pf["pnl_pct"]:+.2f}% / 実現年率{pf["realized_ann_pct"]:+.1f}% / {pf["days"]:.1f}日)',
          flush=True)
    print(f'    内訳: キャリー{pf["carry_pnl"]:+,.0f} / 取引コスト{-pf["trade_cost"]:+,.0f} / '
          f'リスク調整{-pf["risk_cost"]:+,.0f}'
          + (f' / 約定警告:{",".join(pf["fill_warns"])}' if pf["fill_warns"] else ''), flush=True)
    for w in watched:
        g = f'{w["gross"]:+.1f}%' if w["gross"] is not None else w["status"]
        print(f'  追跡 {w["coin"]:<4} {w["long"]}→{w["short"]}: {g}', flush=True)
    if ops:
        o = ops[0]
        print(f'  発掘トップ: {o["coin"]} {o["long"]}→{o["short"]} {o["gross"]:+.1f}%/yr', flush=True)


if __name__ == "__main__":
    main()
