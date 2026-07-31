#!/usr/bin/env python3
"""
Funding Rate Arbitrage Monitor (Phase 1: 監視のみ・売買しない)

5取引所の無期限先物(USDT建て)の資金調達率を公開APIから取得し、
同一コインを「資金調達率が低い取引所でロング / 高い取引所でショート」
した場合の理論上の年率(APR)を計算して表示する。

APIキー不要。資金も不要。読み取り専用。

使い方:
  python3 funding_monitor.py                  # 1回スキャンして上位を表示
  python3 funding_monitor.py --top 30         # 上位30件
  python3 funding_monitor.py --min-volume 10  # 24h出来高 1000万ドル以上に限定
  python3 funding_monitor.py --loop 300       # 5分ごとに繰り返しスキャン
  python3 funding_monitor.py --json out.json  # 結果をJSON保存
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

TIMEOUT = 20
HEADERS = {"User-Agent": "funding-monitor/1.0"}
HOURS_PER_YEAR = 24 * 365


def http_get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def http_post(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={**HEADERS, "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def norm_coin(base):
    """1000PEPE / kPEPE などの倍率プレフィックスを剥がしてコイン名を揃える。
    資金調達率は割合なので倍率が違っても比較可能。"""
    for p in ("1000000", "100000", "10000", "1000"):
        if base.startswith(p) and len(base) > len(p):
            return base[len(p):]
    if base.startswith("k") and base[1:].isupper():
        return base[1:]
    return base


# ---- 各取引所: coin -> {rate(1回あたり), interval_h, vol(24h USD or None)} ----

def fetch_bybit():
    intervals = {}
    cursor = ""
    while True:
        url = "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000"
        if cursor:
            url += "&cursor=" + urllib.parse.quote(cursor)
        res = http_get(url)["result"]
        for it in res.get("list", []):
            try:
                intervals[it["symbol"]] = int(it.get("fundingInterval") or 480) / 60
            except (ValueError, TypeError):
                pass
        cursor = res.get("nextPageCursor") or ""
        if not cursor:
            break
    out = {}
    tickers = http_get("https://api.bybit.com/v5/market/tickers?category=linear")
    for it in tickers["result"]["list"]:
        s = it["symbol"]
        fr = it.get("fundingRate")
        if not s.endswith("USDT") or fr in (None, ""):
            continue
        out[norm_coin(s[:-4])] = {
            "rate": float(fr),
            "interval_h": intervals.get(s, 8.0),
            "vol": float(it.get("turnover24h") or 0),
        }
    return out


def fetch_binance():
    prem = http_get("https://fapi.binance.com/fapi/v1/premiumIndex")
    vols = {}
    try:
        for t in http_get("https://fapi.binance.com/fapi/v1/ticker/24hr"):
            vols[t["symbol"]] = float(t.get("quoteVolume") or 0)
    except Exception:
        pass
    itv = {}
    try:
        for it in http_get("https://fapi.binance.com/fapi/v1/fundingInfo"):
            itv[it["symbol"]] = float(it.get("fundingIntervalHours") or 8)
    except Exception:
        pass
    out = {}
    for it in prem:
        s = it["symbol"]
        fr = it.get("lastFundingRate")
        if not s.endswith("USDT") or fr in (None, ""):
            continue
        out[norm_coin(s[:-4])] = {
            "rate": float(fr),
            "interval_h": itv.get(s, 8.0),
            "vol": vols.get(s),
        }
    return out


def fetch_bitget():
    itv = {}
    try:
        contracts = http_get(
            "https://api.bitget.com/api/v2/mix/market/contracts?productType=USDT-FUTURES"
        )["data"]
        for c in contracts:
            try:
                itv[c["symbol"]] = float(c.get("fundInterval") or 8)
            except (ValueError, TypeError):
                pass
    except Exception:
        pass
    out = {}
    tickers = http_get(
        "https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES"
    )["data"]
    for it in tickers:
        s = it["symbol"]
        fr = it.get("fundingRate")
        if not s.endswith("USDT") or fr in (None, ""):
            continue
        out[norm_coin(s[:-4])] = {
            "rate": float(fr),
            "interval_h": itv.get(s, 8.0),
            "vol": float(it.get("usdtVolume") or 0),
        }
    return out


def fetch_gate():
    contracts = http_get("https://api.gateio.ws/api/v4/futures/usdt/contracts")
    tick = {}
    try:
        for t in http_get("https://api.gateio.ws/api/v4/futures/usdt/tickers"):
            tick[t["contract"]] = t
    except Exception:
        pass
    out = {}
    for c in contracts:
        name = c.get("name", "")
        fr = c.get("funding_rate")
        if not name.endswith("_USDT") or c.get("in_delisting") or fr in (None, ""):
            continue
        vol = None
        t = tick.get(name)
        if t:
            v = t.get("volume_24h_settle") or t.get("volume_24h_quote")
            if v not in (None, ""):
                vol = float(v)
        out[norm_coin(name[:-5])] = {
            "rate": float(fr),
            "interval_h": float(c.get("funding_interval") or 28800) / 3600,
            "vol": vol,
        }
    return out


def fetch_hyperliquid():
    meta, ctxs = http_post("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs"})
    out = {}
    for u, c in zip(meta["universe"], ctxs):
        fr = c.get("funding")
        if u.get("isDelisted") or fr in (None, ""):
            continue
        out[norm_coin(u["name"])] = {
            "rate": float(fr),
            "interval_h": 1.0,  # Hyperliquidは1時間ごと
            "vol": float(c.get("dayNtlVlm") or 0),
        }
    return out


def fetch_okx():
    # instId=ANY で全SWAPの資金調達率を一括取得できる
    rates = http_get("https://www.okx.com/api/v5/public/funding-rate?instId=ANY")["data"]
    vols = {}
    try:
        for t in http_get("https://www.okx.com/api/v5/market/tickers?instType=SWAP")["data"]:
            try:
                vols[t["instId"]] = float(t["volCcy24h"]) * float(t["last"])
            except (ValueError, KeyError):
                pass
    except Exception:
        pass
    out = {}
    for it in rates:
        inst = it.get("instId", "")
        fr = it.get("fundingRate")
        if not inst.endswith("-USDT-SWAP") or fr in (None, ""):
            continue
        try:
            interval_h = (int(it["nextFundingTime"]) - int(it["fundingTime"])) / 3600000
            if not 0.5 <= interval_h <= 24:
                interval_h = 8.0
        except (ValueError, KeyError):
            interval_h = 8.0
        out[norm_coin(inst[:-10])] = {
            "rate": float(fr),
            "interval_h": interval_h,
            "vol": vols.get(inst),
        }
    return out


def fetch_kucoin():
    contracts = http_get("https://api-futures.kucoin.com/api/v1/contracts/active")["data"]
    out = {}
    for c in contracts:
        s = c.get("symbol", "")
        fr = c.get("fundingFeeRate")
        if not s.endswith("USDTM") or fr in (None, ""):
            continue
        base = s[:-5]
        if base == "XBT":
            base = "BTC"
        gran = c.get("fundingRateGranularity") or 28800000  # ms
        vol = c.get("turnoverOf24h")
        out[norm_coin(base)] = {
            "rate": float(fr),
            "interval_h": float(gran) / 3600000,
            "vol": float(vol) if vol not in (None, "") else None,
        }
    return out


def fetch_mexc():
    tickers = http_get("https://contract.mexc.com/api/v1/contract/ticker")["data"]
    out = {}
    for it in tickers:
        s = it.get("symbol", "")
        fr = it.get("fundingRate")
        if not s.endswith("_USDT") or fr in (None, ""):
            continue
        vol = it.get("amount24")
        out[norm_coin(s[:-5])] = {
            "rate": float(fr),
            "interval_h": 8.0,  # MEXCは間隔の一括取得APIがないため8hと仮定
            "vol": float(vol) if vol not in (None, "") else None,
        }
    return out


def fetch_dydx():
    # dYdXはUSDC建てだが比較対象として扱う。資金調達は1時間ごと
    markets = http_get("https://indexer.dydx.trade/v4/perpetualMarkets")["markets"]
    out = {}
    for name, m in markets.items():
        fr = m.get("nextFundingRate")
        if m.get("status") != "ACTIVE" or fr in (None, ""):
            continue
        vol = m.get("volume24H")
        out[norm_coin(name.split("-")[0])] = {
            "rate": float(fr),
            "interval_h": 1.0,
            "vol": float(vol) if vol not in (None, "") else None,
        }
    return out


def fetch_htx():
    data = http_get("https://api.hbdm.com/linear-swap-api/v1/swap_batch_funding_rate")["data"]
    out = {}
    for it in data:
        code = it.get("contract_code", "")
        fr = it.get("funding_rate")
        if not code.endswith("-USDT") or fr in (None, ""):
            continue
        out[norm_coin(code[:-5])] = {
            "rate": float(fr),
            "interval_h": 8.0,
            "vol": None,
        }
    return out


def fetch_bingx():
    prem = http_get("https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex")["data"]
    vols = {}
    try:
        for t in http_get("https://open-api.bingx.com/openApi/swap/v2/quote/ticker")["data"]:
            v = t.get("quoteVolume")
            if v not in (None, ""):
                vols[t.get("symbol")] = float(v)
    except Exception:
        pass
    out = {}
    for it in prem:
        s = it.get("symbol", "")
        fr = it.get("lastFundingRate")
        if not s.endswith("-USDT") or fr in (None, ""):
            continue
        out[norm_coin(s[:-5])] = {
            "rate": float(fr),
            "interval_h": 8.0,
            "vol": vols.get(s),
        }
    return out


FETCHERS = {
    "Bybit": fetch_bybit,
    "Binance": fetch_binance,
    "Bitget": fetch_bitget,
    "Gate": fetch_gate,
    "Hyperliquid": fetch_hyperliquid,
    "OKX": fetch_okx,
    "KuCoin": fetch_kucoin,
    "MEXC": fetch_mexc,
    "dYdX": fetch_dydx,
    "HTX": fetch_htx,
    "BingX": fetch_bingx,
}


def annualize(entry):
    return entry["rate"] * (HOURS_PER_YEAR / entry["interval_h"]) * 100


def scan(min_volume_usd, taker_fee, max_apr):
    data = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=len(FETCHERS)) as pool:
        futures = {name: pool.submit(fn) for name, fn in FETCHERS.items()}
        for name, fut in futures.items():
            try:
                data[name] = fut.result()
            except Exception as e:
                errors[name] = f"{type(e).__name__}: {e}"

    coins = {}
    for venue, entries in data.items():
        for coin, entry in entries.items():
            if entry["vol"] is not None and entry["vol"] < min_volume_usd:
                continue
            coins.setdefault(coin, {})[venue] = entry

    rows = []
    for coin, venues in coins.items():
        if len(venues) < 2:
            continue
        aprs = {v: annualize(e) for v, e in venues.items()}
        long_v = min(aprs, key=aprs.get)   # 資金調達率が最も低い所でロング
        short_v = max(aprs, key=aprs.get)  # 最も高い所でショート
        net = aprs[short_v] - aprs[long_v]
        if net <= 0 or net > max_apr:
            continue
        # 往復手数料: 建玉+決済 × 2レッグ = テイカー手数料4回分
        fee_pct = taker_fee * 4 * 100
        breakeven_days = fee_pct / (net / 365) if net > 0 else float("inf")
        rows.append({
            "coin": coin,
            "long_venue": long_v,
            "long_apr": aprs[long_v],
            "short_venue": short_v,
            "short_apr": aprs[short_v],
            "net_apr": net,
            "breakeven_days": breakeven_days,
            "n_venues": len(venues),
        })
    rows.sort(key=lambda r: r["net_apr"], reverse=True)
    return rows, data, errors


def print_report(rows, data, errors, top, min_apr):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n=== Funding Rate Arbitrage Monitor  {now} ===")
    ok = ", ".join(f"{k}({len(v)})" for k, v in data.items())
    print(f"取得成功: {ok}")
    for name, err in errors.items():
        print(f"[警告] {name} 取得失敗: {err}")
    if not rows:
        print("条件を満たす機会なし")
        return
    hdr = (f"{'コイン':<10} {'ロング側':<12} {'(APR%)':>8} {'ショート側':<12} "
           f"{'(APR%)':>8} {'差=年率%':>9} {'損益分岐':>8}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows[:top]:
        mark = " ★" if r["net_apr"] >= min_apr else ""
        print(f"{r['coin']:<10} {r['long_venue']:<12} {r['long_apr']:>8.1f} "
              f"{r['short_venue']:<12} {r['short_apr']:>8.1f} "
              f"{r['net_apr']:>9.1f} {r['breakeven_days']:>6.1f}日{mark}")
    print(f"\n★ = 年率 {min_apr}% 以上 / 損益分岐 = 手数料を金利差で回収するのに必要な日数")
    print("注意: これは理論値。実際は約定価格の乖離・金利の変動・清算リスクがある。")


def main():
    ap = argparse.ArgumentParser(description="資金調達率アービトラージ監視bot")
    ap.add_argument("--top", type=int, default=20, help="表示件数 (default: 20)")
    ap.add_argument("--min-volume", type=float, default=5.0,
                    help="24h出来高の下限 (百万USD, default: 5)")
    ap.add_argument("--min-apr", type=float, default=10.0,
                    help="★を付ける年率しきい値%% (default: 10)")
    ap.add_argument("--max-apr", type=float, default=1000.0,
                    help="これを超える異常値は除外 (default: 1000)")
    ap.add_argument("--taker-fee", type=float, default=0.055,
                    help="片道テイカー手数料%% (default: 0.055)")
    ap.add_argument("--loop", type=int, default=0,
                    help="指定秒ごとに繰り返しスキャン (0=1回のみ)")
    ap.add_argument("--json", metavar="PATH", help="結果をJSONで保存")
    args = ap.parse_args()

    while True:
        rows, data, errors = scan(
            min_volume_usd=args.min_volume * 1e6,
            taker_fee=args.taker_fee / 100,
            max_apr=args.max_apr,
        )
        print_report(rows, data, errors, args.top, args.min_apr)
        if args.json:
            snapshot = {"timestamp": datetime.now().isoformat(), "rows": rows}
            with open(args.json, "w") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            print(f"JSON保存: {args.json}")
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
