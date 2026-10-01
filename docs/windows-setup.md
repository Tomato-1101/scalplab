# Windows セットアップ手順（scalp-lab のライブ検知・通知）

日本株のリアルタイム検知・発注は **Windows 機**で運用する方針（発注は Windows の証券アプリに慣れているため）。
**データ源は auカブコム kabu STATION API に確定**（Windows 常駐アプリ必須）。日本株のティックはこれ経由で取る。
このドキュメントはコードの取得・環境構築・通知・kabu STATION 接続までの手順をまとめる。
口座・アプリ準備中でも、実機無しでモック検証まで完了済みなので、**開通日に §8 の手順だけで即つながる**。

コードは全面的に `pathlib`（`Path(...) / "data" / ...` 形式）で書かれており、macOS 固有の絶対パスや
`/` 区切りの直書きは無い（URL パスを除く）。そのため Windows でもパス面の修正なしで動く。

---

## 1. リポジトリの取得

git がある場合:

```powershell
git clone <このリポジトリの URL> scalp-lab
cd scalp-lab
```

git を使わない場合は zip をダウンロードして展開し、そのフォルダで PowerShell を開く（`cd` で移動）。

---

## 2. Python 3.12 の venv 構築

Python は **3.12 固定**（futu-api の実績があるため。3.13/3.14 は使わない）。
[python.org](https://www.python.org/downloads/) から 3.12 系をインストールすると `py` ランチャが入る。

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
```

以後は `.venv\Scripts\python`（PowerShell では `.venv\Scripts\python.exe`）を直接呼ぶか、
`.venv\Scripts\Activate.ps1` で仮想環境を有効化する。
（PowerShell の実行ポリシーで `Activate.ps1` が弾かれる場合は
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned` を実行してから有効化する。
`.venv\Scripts\python` を直接呼ぶ運用なら有効化そのものが不要。）

---

## 3. 依存パッケージのインストール

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
```

`requirements.txt` は futu-api / pandas / numpy / pyarrow / PyYAML / pytest / yfinance。
（futu-api は moomoo OpenD を使う場合のみ必要。別の証券会社に切り替えた際も、当面インストールしておいて問題ない。）

インストール確認としてテストを回す（OpenD 非依存で完結する）:

```powershell
.venv\Scripts\python -m pytest tests\ -q
```

---

## 4. 通知（トースト）のテスト

Windows では通知は **PowerShell の WinRT トースト（`[Windows.UI.Notifications]`）**で出す。
BurntToast 等の外部モジュールには依存しない（Windows 10+ 標準機能を直接呼ぶ）。
トーストが使えない環境では PowerShell 内で自動的に beep + 標準出力へフォールバックし、例外で落ちない。

次のコマンドで実際にトーストが出るか確認する:

```powershell
.venv\Scripts\python -c "from scalplab.live import send_notification; send_notification('scalp-lab テスト通知 LONG @ 100.0', sound='on')"
```

画面右下にトースト（タイトル「scalp-lab」）が出れば成功。`sound='on'` を渡すと通知音（フォールバック時は beep）を鳴らす。
出ない場合でもコマンド自体はエラーにならない（設計上、通知失敗で検知ループを止めない）。原因の切り分けは以下:

- 集中モード（Focus assist）/ 通知の無効化 → Windows の「設定 > システム > 通知」で scalp-lab（PowerShell）を許可。
- 会社管理 PC のポリシーで WinRT トーストが無効 → フォールバックの beep + コンソール出力で代替される。

OS 判定は `platform.system()` で行い、macOS では osascript、その他 OS（Linux 等）ではログ出力のみになる。

---

## 5. kabu STATION の APIPassword を環境変数で設定

kabu STATION API の APIPassword（kabu STATION アプリの「API 設定」で決めるパスワード）は、
**環境変数 `KABU_API_PASSWORD` から読む**（既定の変数名。`config/live.yaml` の `kabu.password_env` で変更可）。
**リポジトリや config に平文で書かない**。設定方法:

現在のセッションだけ有効（テスト用）:

```powershell
$env:KABU_API_PASSWORD = "＜APIパスワード＞"
```

恒久設定（ユーザー環境変数に登録。設定後は新しい PowerShell を開き直す）:

```powershell
setx KABU_API_PASSWORD "＜APIパスワード＞"
```

設定できたか確認（値そのものは表示せず有無だけ見る）:

```powershell
if ($env:KABU_API_PASSWORD) { "OK: 設定あり" } else { "NG: 未設定" }
```

---

## 6. ライブ検知の起動

データ源は `config/live.yaml` の `source` で切り替える（`moomoo`=米国株 / `kabu`=日本株）。

日本株（kabu STATION）で動かすには `source: kabu` と `watch`（`JP.<証券コード>`。例: `JP.7203` トヨタ）を
設定する。**共有ファイルの `config/live.yaml` は編集せず、`config/live.local.yaml` に書く**
（存在すればトップレベルキー単位で live.yaml を上書きする。Git 管理外なので Mac 側と衝突しない）。
kabu STATION アプリを起動・ログインした状態で:

```powershell
:: 接続(token)→登録(register)→10秒受信→統計で終了（アプリ起動が要る。市場外でもクォータ確認可）
.venv\Scripts\python -m scalplab.live --check

:: 監視銘柄を上書きして疎通確認
.venv\Scripts\python -m scalplab.live --check --codes JP.7203,JP.9984

:: 市場時間を無視して 60 秒だけ検知（立会中の手動スモーク用）
.venv\Scripts\python -m scalplab.live --duration 60

:: 通常起動（東証 09:00-11:30 / 12:30-15:30 のみ稼働。寄り前は待機・引け後は即終了）
.venv\Scripts\python -m scalplab.live
```

米国株を moomoo で動かす場合は `source: moomoo`（既定）にして `--codes US.TQQQ,US.SOXL` のように使う
（moomoo OpenD の起動・ログインが必要）。**検知と通知だけ**を行い、自動売買・決済はしない（決済は本人裁量）。

---

## 7. タスクスケジューラでの自動起動（任意）

平日の寄り前に自動起動したい場合の例。ライブ検知は既定でセッション時間内のみ稼働する（寄り前は待機・引け後は即終了）。

1. 「タスク スケジューラ」を開く → 「基本タスクの作成」。
2. トリガー: 平日、寄り前の時刻（kabu=東証なら 08:45 JST 前後）。
3. 操作: 「プログラムの開始」。
   - プログラム/スクリプト: `C:\path\to\scalp-lab\.venv\Scripts\python.exe`
   - 引数の追加: `-m scalplab.live`
   - 開始（作業フォルダ）: `C:\path\to\scalp-lab`   ← **これを必ず設定**（相対パス解決とログ出力のため）
4. 条件・設定: 「タスクを停止するまでの時間」を無効化（引けまで動かすため）、
   「ユーザーがログオンしているときのみ実行」を選ぶ（トースト通知と kabu STATION アプリはログオンセッションが要る）。
5. kabu STATION アプリ自体もログオン時に自動起動する設定にしておく（API はアプリ起動中のみ有効）。

`schtasks` は作業フォルダを直接指定できないため、確実にするならスタート用の `.cmd`
「`cd /d C:\path\to\scalp-lab && .venv\Scripts\python.exe -m scalplab.live`」を作り、それをタスクに登録する。

---

## 8. 開通日にやること（口座・アプリが用意できたら）

コード側は実装・モック検証済み。開通日はこの順で進めれば動く:

1. **kabu STATION アプリを起動しログイン** → 「設定 > API」で **API を有効化**し **APIPassword を設定**
   （本番は 18080 番ポート。検証用アプリを使うなら 18081 で、`config/live.yaml` の `kabu.port` を 18081 に）。
2. **§5 の手順で `KABU_API_PASSWORD` を環境変数に登録**（`setx`）。新しい PowerShell を開き直す。
3. `config/live.yaml` を編集: `source: kabu` にし、`watch` を監視したい `JP.<証券コード>` に差し替える（最大50銘柄）。
4. **疎通確認**: `.venv\Scripts\python -m scalplab.live --check`
   → 「kabu STATION 接続 : OK」「購読成功 : N/N」が出れば接続・認証・登録が通っている（市場外でもここまで確認できる）。
5. **通知確認**: §4 の1行コマンドでトーストが出るか確認。
6. **立会中に本起動**: `.venv\Scripts\python -m scalplab.live`（または §7 のタスクスケジューラで自動起動）。
   シグナル発火時にトースト通知＋`logs\live-signals-YYYY-MM-DD.csv` へ追記される。

うまくいかない時の切り分け:

- 「APIPassword が未設定」→ §5 の環境変数。`config/live.yaml` の `kabu.password_env` と変数名が一致しているか。
- 「トークン取得に失敗」→ kabu STATION アプリが起動・ログイン済みで API が有効か。ポート（本番18080/検証18081）が合っているか。
- 「register に反映されず」→ 証券コード（`JP.7203` の数字部分）と市場（`kabu.exchange` 既定 1=東証）が正しいか。

参考: データ源アダプタの契約は `scalplab/live.py` の `TickSource` / `KabuStationTickSource` を参照。
立花証券 e支店（`TachibanaTickSource`）は将来枠（ダミー）として同じ契約で用意してあるが、データ源は kabu STATION に確定済み。
