# Funding Rate Arbitrage Bot

資金調達率(Funding Rate)の取引所間の差を監視し、デルタニュートラル
(片方でロング・片方でショート)で金利差を取る戦略のためのツール。

## 現在のステータス: Phase 1(監視のみ)

- APIキー不要・資金不要・売買しない(読み取り専用)
- 対象11取引所: Bybit / Binance / Bitget / Gate.io / Hyperliquid / OKX / KuCoin /
  MEXC / dYdX / HTX / BingX の USDT建て無期限先物(dYdXのみUSDC建て)
  - 注意: MEXC / HTX / BingX は資金調達間隔の一括取得APIがないため8時間と仮定している
- 資金調達率の間隔差(1h/4h/8h)を年率換算して正規化

```bash
python3 funding_monitor.py                  # 1回スキャン
python3 funding_monitor.py --top 30         # 上位30件表示
python3 funding_monitor.py --min-volume 20  # 24h出来高2000万ドル以上に限定
python3 funding_monitor.py --loop 300       # 5分ごとに監視し続ける
python3 funding_monitor.py --json out.json  # JSON保存
```

## 結果の読み方

- **ロング側/ショート側**: 資金調達率が最も低い取引所でロング、最も高い取引所でショート
- **差=年率%**: 両ポジションを持ったときの理論上の年率リターン
- **損益分岐**: 往復手数料(テイカー4回分)を金利差で回収するのに必要な日数
- 表の最上位は上場直後・低流動性の銘柄が多く、実際には約定できないことが多い。
  `--min-volume 20〜50` で絞った中位の銘柄のほうが現実的。

## 重要: 「リスク0」ではない

このバナーを消してはいけない。実際に存在するリスク:

1. **清算リスク** — 両建てでも各取引所では片側ポジション。価格が急変すると
   片方の証拠金が不足して清算される(→もう片方が裸のポジションになり大損の可能性)
2. **金利の反転** — 資金調達率は数時間で符号が変わる。エントリー時の差が持続する保証はない
3. **執行コスト** — 2取引所で同時に建てても価格乖離・スリッページで初手からマイナスになりうる
4. **取引所リスク** — 出金停止・破綻(FTXの例)。資金を分散して置くこと自体がリスク
5. **規制** — 海外デリバティブ取引所は日本の金融庁登録外。利用は自己責任。
   税務上、先物の損益は雑所得(総合課税)になる点にも注意

## ロードマップ

- [x] Phase 1: 監視bot
- [x] Phase 1.5: 履歴の記録と「金利差がどれくらい持続するか」の統計分析
  - `recorder.py --snapshot` が10分ごとに `data/funding.db` へ記録(launchd登録)
  - 登録: `launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.fundingarb.recorder.plist`
  - 停止: `launchctl bootout gui/501/com.fundingarb.recorder`
  - 2〜3日溜めてから `python3 analyze.py` で持続性・実現リターンを分析
- [ ] Phase 2: 自動売買(取引所選定・資金額・APIキー管理の確認が必要)
  - APIキーは必ず「出金権限なし」で発行し、IP制限をかける
  - 少額(数万円)からテスト開始
