# 日本版GDPNow（jp-gdpnow）

アトランタ連銀 GDPNow の考え方で作った、日本の実質GDP（QE1次速報・前期比年率）のナウキャスト。

![最新のレポート](output/latest.png)

## 2つのエンジン
- **① QE1予想**：需要項目別のブリッジ方程式の積み上げと、供給側（鉱工業生産＋労働投入）を 50:50 で合成（モデル v3）
- **② 景気因子**：月次16系列と四半期GDPの混合頻度ダイナミック・ファクター・モデル

それぞれ90%予測区間（過去の公表カレンダー再現バックテストの誤差から作成）と、前回からの「ニュース分解」を出す。

## 自動実行（GitHub Actions）
`.github/workflows/nowcast.yml` が平日 18:30（日本時間）に動く。

- 各統計の最新値を取得し、`data/vintages.sqlite` に差分だけ保存（ヴィンテージ）
- 前回から新しい・改定されたデータがあった日だけ予想を記録し、`data/runs.csv` と `output/` をコミット
- 手動実行：Actions タブ → nowcast → Run workflow（`force` にチェックすると新データが無くても実行）

必要な Secret：`ESTAT_APP_ID`（e-Stat API のアプリケーションID）

## 手元で実行
```bash
pip install -r requirements.txt
export ESTAT_APP_ID=...            # Windows PowerShell: $env:ESTAT_APP_ID="..."
export JP_GDPNOW_HOME=$PWD/data    # Windows PowerShell: $env:JP_GDPNOW_HOME="$PWD\data"
python -m scripts.cli nowcast --out output          # 予想・記録・レポート画像
python -m scripts.cli backtest --mode dates         # 公表カレンダー再現バックテスト
python -m scripts.cli history                       # 記録した予想の推移
```

## フォルダ
| パス | 中身 |
|---|---|
| `scripts/` | モデル本体（data, model, factor, release, news, bands, vintage, plots, cli ほか） |
| `scripts/schedule.csv` | 今後の公表予定（手で更新する。四半期ごとに次の期の日程を追加） |
| `vendor/` | データ取得に使う esri-data・boj-api・estat-api スキルのモジュール（同梱コピー） |
| `data/` | ヴィンテージDBと予想の記録 `runs.csv` |
| `output/` | レポート画像（`latest.png` が最新）と内訳CSV |
| `SKILL.md` | 設計・検証結果・不採用案の記録 |

## データ出所
内閣府ESRI（QE・景気動向指数・機械受注）、日本銀行（実質輸出入・消費活動指数・国際収支・物価指数）、総務省（消費動向指数）、経済産業省（鉱工業指数・製造工業生産予測指数・第3次産業活動指数）、国土交通省（建設総合統計, e-Stat 経由）。

モデルによる推計であり、公式の見通しではありません。
