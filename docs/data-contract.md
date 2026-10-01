# データ契約（recorder ⇔ backtest の合意事項）

recorder（書き手）と backtest（読み手）は本契約のみを介して疎結合にする。
変更する場合は両側と本ファイルを同一コミットで更新する。

## パス規約

プロジェクトルート相対。日付は JST の営業日。

- ティック:   `data/ticks/YYYY-MM-DD/{code}.parquet`
- 1分足:      `data/bars1m/YYYY-MM-DD/{code}.parquet`
- L1気配:     `data/quotes/YYYY-MM-DD/{code}.parquet`
- 銘柄選定:   `data/universe/YYYY-MM-DD.yaml`（その日の録画対象と選定理由）

`{code}` は moomoo 形式（例 `JP.7203`, `US.AAPL`）。ファイル名にそのまま使う。

## タイムスタンプ規約

**全テーブル共通: `time` 列は JST の naive datetime64[ns]**（tz 情報なし）。
日本市場専用ツールなので JST 固定とし、変換は datafeed 層（moomoo 返却値は既に JST 文字列）で吸収する。
US 銘柄のアドホック検証時も「moomoo が返すローカル市場時刻」をそのまま入れる（検証用途のみ、混在させない）。

## スキーマ

### ticks
| 列 | 型 | 意味 |
|---|---|---|
| time | datetime64[ns] | 約定時刻（ミリ秒精度） |
| price | float64 | 約定価格 |
| volume | int64 | 出来高（株数） |
| turnover | float64 | 売買代金 |
| direction | str | BUY / SELL / NEUTRAL（moomoo ticker_direction） |
| seq | int64 | moomoo sequence（重複排除キー） |
| tick_type | str | moomoo type（AUTO_MATCH 等） |

同一 (code, seq) は重複排除。time 昇順で保存。

### bars1m
| 列 | 型 |
|---|---|
| time | datetime64[ns]（バー開始時刻） |
| open / high / low / close | float64 |
| volume | int64 |
| turnover | float64 |
| source | str（`ticks` = ティックから合成 / `yfinance` = 遅延フォールバック） |

### quotes（L1）
| 列 | 型 |
|---|---|
| time | datetime64[ns] |
| bid / ask | float64 |
| bid_vol / ask_vol | int64 |

## loader API（scalplab/store/loader.py が提供）

```python
load_ticks(date: str, code: str) -> pd.DataFrame          # 契約スキーマで返す。無ければ FileNotFoundError
load_bars1m(date: str, code: str) -> pd.DataFrame
load_quotes(date: str, code: str) -> pd.DataFrame
list_recorded(date: str, kind: str = "ticks") -> list[str] # その日の録画済み code 一覧
resample_ticks_to_bars(ticks: pd.DataFrame, seconds: int) -> pd.DataFrame  # bars1m と同スキーマ（source='ticks'）
```

backtest 側はこの API と本スキーマだけに依存する（recorder の内部実装に依存しない）。
テストでは本契約どおりの parquet をフィクスチャとして自作してよい。

## 注釈データ（data/annotations.sqlite3）

ユーザーが Web UI 上で「ここでエントリー／入っちゃダメ」を理由つきで記録する教師データ。AI（Claude）が
その時点の相場文脈（RCI 等）と一緒に読んで、ユーザーのエントリー手法を学ぶための一次資料。標準ライブラリ
sqlite3 のファイル1つ（依存追加なし）。**個人の判断データなのでコミットしない（`.gitignore` 済み）**。

テーブル `annotations`:

| 列 | 型 | 意味 |
|---|---|---|
| id | INTEGER PK | 連番 |
| created_at | TEXT | 記録した日時（JST ISO） |
| code | TEXT | 銘柄コード（例 `JP.7203`） |
| bar_time | TEXT | クリックしたバーの時刻（JST ISO・市場ローカル壁時計） |
| kind | TEXT | `entry_long` / `entry_short` / `no_entry` の3値 |
| reason | TEXT | ユーザーが書いた理由（任意） |
| source | TEXT | データソース（`bars1m` / `ticks` / `hist1m`） |
| context | TEXT | 保存時にサーバが採取した指標スナップショット（JSON: `price, rci9, rci14, sar, atr, sma5, sma25, bar_index, n_bars`） |

読み出し API（`scalplab/annotations.py`）:

```python
add_annotation(code, bar_time, kind, reason, source, context, ...) -> dict
list_annotations(code=None, ...) -> list[dict]   # context は dict に復元済み
delete_annotation(id, ...) -> bool
load_annotations(...) -> pd.DataFrame            # AI が手法を学ぶための可読 API（全件）
```

AI 学習時は `load_annotations()` で全件を DataFrame として読み、`kind`（正解ラベル）と `context`
（その時の指標）を突き合わせて、ユーザーがどんな RCI/SAR 状況でエントリー可否を判断しているかを学ぶ。

## 日本株の社名マスタ（config/jp_names.csv）

Web UI の日本株ドロップダウンを「社名 (7203)」形式で表示するためのコード→社名対応表。ヘッダは
`code,name`（例 `JP.7203,トヨタ自動車`。形式は `config/jp_names.sample.csv` を参照）。
**このファイルはリポジトリに含めない**（JPX / J-Quants 由来の全銘柄リストの再配布を避けるため
`.gitignore` 対象）。使いたい場合は J-Quants の上場銘柄情報（listed info）API から自分で生成する:
コードは `JP.` + 東証の 4 桁コード（`285A` のような英数字コードも同様）、社名は API の会社名をそのまま入れる。
ファイルが無い場合 UI は社名なし（コード表示）で動く。
上場銘柄の改廃（新規上場・廃止・社名変更・コード付替え）が起きたら再生成する。マスタに無いコードは
UI 側でコードのまま表示にフォールバックする。
