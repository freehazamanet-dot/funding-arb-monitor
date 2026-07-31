#!/usr/bin/env python3
"""
取引所到達性プローブ（地理ブロック判定）

funding_monitor.FETCHERS を1つずつ叩き、GitHub Actions ランナー（米国Azure IP）
などのクラウド環境から各取引所の公開APIに到達できるかを判定する。
- 実行元の公開IPと国も表示（US Azureか確認用）
- 各取引所: OK/失敗・取得銘柄数・BTC/ETHのfunding・所要秒・エラー種別
- 結果は標準出力 + $GITHUB_STEP_SUMMARY（Actions上ではジョブ要約に出る）

使い方: python3 probe.py
"""
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from funding_monitor import FETCHERS

PER_TIMEOUT = 25  # 1取引所あたりの上限秒（ブロックでハングする所を弾く）
# 本命戦略で使う脚（見えないと監視にならない取引所）
KEY = {"Binance", "HTX", "Bybit", "dYdX", "Hyperliquid", "OKX", "Gate"}


def runner_ip():
    for url in ("https://ipinfo.io/json", "https://ipapi.co/json/"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "probe/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                j = json.load(r)
            ip = j.get("ip", "?")
            country = j.get("country") or j.get("country_name") or "?"
            org = j.get("org", "") or j.get("asn", "")
            return f"{ip} / {country} / {org}"
        except Exception:
            continue
    return "取得不可"


def probe_one(name, fn):
    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            data = ex.submit(fn).result(timeout=PER_TIMEOUT)
        el = time.time() - t0
        n = len(data)
        btc = data.get("BTC", {}).get("rate")
        eth = data.get("ETH", {}).get("rate")
        sample = []
        if btc is not None:
            sample.append(f"BTC {btc*100:+.4f}%")
        if eth is not None:
            sample.append(f"ETH {eth*100:+.4f}%")
        return {"name": name, "ok": n > 0, "n": n, "sec": el,
                "sample": " / ".join(sample) or "-", "err": ""}
    except FutTimeout:
        return {"name": name, "ok": False, "n": 0, "sec": time.time() - t0,
                "sample": "-", "err": f"TIMEOUT(>{PER_TIMEOUT}s) 地理ブロック/遮断の可能性"}
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        return {"name": name, "ok": False, "n": 0, "sec": time.time() - t0,
                "sample": "-", "err": msg[:160]}


def main():
    ip = runner_ip()
    results = []
    with ThreadPoolExecutor(max_workers=len(FETCHERS)) as pool:
        futs = {pool.submit(probe_one, n, fn): n for n, fn in FETCHERS.items()}
        for f in futs:
            results.append(f.result())
    order = list(FETCHERS.keys())
    results.sort(key=lambda r: order.index(r["name"]))

    ok = [r for r in results if r["ok"]]
    ng = [r for r in results if not r["ok"]]
    key_ng = [r["name"] for r in ng if r["name"] in KEY]

    lines = []
    lines.append(f"# 取引所到達性プローブ")
    lines.append(f"- 実行元IP/国/組織: **{ip}**")
    lines.append(f"- 到達 **{len(ok)}/{len(results)}**"
                 + (f" / ⚠️ 本命脚で未到達: **{', '.join(key_ng)}**" if key_ng else " / ✅ 本命脚は全到達"))
    lines.append("")
    lines.append("| 取引所 | 結果 | 銘柄数 | サンプル | 秒 | エラー |")
    lines.append("|---|---|--:|---|--:|---|")
    for r in results:
        mark = "✅OK" if r["ok"] else "❌NG"
        star = "⭐" if r["name"] in KEY else ""
        lines.append(f"| {star}{r['name']} | {mark} | {r['n']} | {r['sample']} | "
                     f"{r['sec']:.1f} | {r['err']} |")
    report = "\n".join(lines)
    print(report)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")


if __name__ == "__main__":
    main()
