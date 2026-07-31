# クラウド監視(GitHub Actions + Pages)

PCがオフでも、GitHub Actions が定期的に資金調達率を収集し、ダッシュボードに表示します。

## 構成
- `probe.py` … 取引所到達性の判定(米国Azure IPから)。`.github/workflows/probe.yml`(手動)
- `collect.py` … 30分ごとに収集 → `docs/data/latest.json` と月次履歴を更新。`.github/workflows/collect.yml`
- `docs/index.html` … ダッシュボード(`latest.json`を読んで表示・5分ごと自動更新)

## 到達性(実測 2026-08-01, 米国Azure IP)
- ✅ 到達 9/11: HTX, dYdX, Hyperliquid, OKX, Gate, KuCoin, MEXC, Bitget, BingX
- ❌ 遮断: **Binance(451) / Bybit(403)** ← 米国IPブロック。`EXCLUDE_VENUES` で除外中
- → SOL(dYdX→HTX), BTC/ETH(dYdX→Hyperliquid) は全脚監視可。Binance/Bybit脚のペア(ADA/LTC/XRP)は取れない

## Discord通知(任意)
Webhookを登録すると、追跡ペアが閾値超えで通知します:
```
gh secret set DISCORD_WEBHOOK --repo <owner>/funding-arb-monitor --body "https://discord.com/api/webhooks/..."
```
閾値の既定: 追跡ペア グロス≥12%/yr、発掘 グロス≥30%/yr。変更は Variables(WATCH_ALERT / DISCOVER_ALERT)。

## ダッシュボードの公開(GitHub Pages)
Pagesを **main / docs** から配信すると `https://<owner>.github.io/funding-arb-monitor/` で見られます。
- **private リポジトリの Pages は GitHub Pro が必要**。無料で公開したい場合は、秘密情報ゼロ(全て公開API)なので **リポジトリをPublic化**するのが最も簡単(Actionsも無制限無料になる)。
- ローカルで見るだけなら `cd docs && python3 -m http.server` で `http://localhost:8000`。

## 費用/制限メモ
- private の Actions 無料枠 = 2000分/月。30分cron ≒ 1440分/月で収まる。高頻度化するならPublic化(無制限)。
- 秘密鍵は一切不要(公開APIのみ)。Phase2(自動売買)で鍵を持たせる時は必ず Secrets へ、コミット厳禁。
