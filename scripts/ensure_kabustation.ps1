# kabuステーションアプリを「今朝起動した状態」に揃える（premarket_task.cmd から毎回呼ばれる）。
#
# なぜ必要か: 前日から起動しっぱなしのアプリは日付が変わると token 取得が
# status=401 / Code 4001007（ログイン認証エラー）になり、07:55 の自動収集が全滅する
# （2026-08-18 実機実測。docs/troubleshooting.md のエラー表）。失効は朝 7 時台（2026-09-03
# 実測で 07:59）に起きるので、収集直前に**アプリを再起動してクリーンなログイン画面を出し**、
# 人が Chrome Remote Desktop で手動ログインする（Windows Hello 自動入力は不可。A 案）。
#
# 5 分ごとのリトライ（scripts/register_premarket_task.ps1）から繰り返し呼ばれるため、
# **今日起動済みなら何もしない**のが要件。毎回再起動すると収集中のアプリを落としてしまう。
#
# 終了コード: 0=揃った（何もしなかった場合も含む） / 1=アプリが見つからず起動もできなかった。
# 1 のときも premarket_task.cmd は python を実行する（premarket が接続失敗で exit 1 し、
# 5 分後のタスク繰り返しが再挑戦する設計）。
#
# 実機確認済み（2026-08-21 Windows 側 AI）: Get-Process で Name=KabuS /
# Path=C:\Users\<user>\AppData\Local\kabuStation\KabuS.exe を確認（Program Files ではなく
# ユーザーローカルにインストールされる）。
$ProcName = "KabuS"
$ExePath = Join-Path $env:LOCALAPPDATA "kabuStation\KabuS.exe"   # 実機では C:\Users\<user>\AppData\Local\kabuStation
$WaitSec = 60   # 起動→自動ログイン完了までの待ち時間

$ErrorActionPreference = "Stop"

$proc = Get-Process -Name $ProcName -ErrorAction SilentlyContinue | Select-Object -First 1
$exe = $ExePath

if ($proc) {
    if ($proc.Path) { $exe = $proc.Path }   # 実プロセスのパスが既知パスより確実
    $started = $null
    try { $started = $proc.StartTime } catch { $started = $null }
    if ($null -eq $started) {
        # 起動時刻が取れない（権限等）。再起動を繰り返す方が危険なので触らない。
        Write-Warning "$ProcName の StartTime が取得できない。再起動の判定ができないため何もしない。"
        exit 0
    }
    # 「今日起動済み」ではなく「今朝 07:00 以降に起動済み」で判定する（2026-08-28 変更）。
    # 深夜（0〜7時）に起動したアプリはログインサーバのメンテ時間帯にかかりログインできて
    # いないことがある（2026-08-25 実測: 深夜 2:15 ブートで起動→朝まで未ログインのまま
    # 収集が全滅）。日付変更 401 と同じく「再起動→自動ログイン」で揃え直すのが安全。
    if ($started -ge (Get-Date).Date.AddHours(7)) {
        Write-Host ("kabuステーション は今朝 " + $started.ToString("HH:mm:ss") + " に起動済み。再起動しない。")
        exit 0
    }
    # 日付をまたいだセッションは朝 7 時台に必ず失効する（2026-09-03 実測: 07:57 取得可→
    # 07:59 失効。0 時ではなく kabu 側の朝の日次リセット）。トークン再取得だけでの復帰は
    # 反証されたので、再起動してクリーンなログイン画面を出す。ログインは人が Chrome Remote
    # Desktop で手動で行う（Windows Hello はセキュアデスクトップ＝自動入力が Windows の
    # 仕様上不可能。2026-09-04 A 案確定）。
    Write-Host ("kabuステーション が今朝 07:00 より前から起動している（起動 " + $started.ToString("yyyy-MM-dd HH:mm:ss") + "）。再起動してログイン画面を出す。")
    Stop-Process -Id $proc.Id -Force
    Start-Sleep -Seconds 5
}

if (-not (Test-Path $exe)) {
    Write-Warning "kabuステーション の実行ファイルが見つからない: $exe（このスクリプト冒頭の TODO を参照して実機のパスを埋めること）"
    exit 1
}

Write-Host "kabuステーション を起動する: $exe"
Start-Process -FilePath $exe | Out-Null

# ログイン画面が出るのを待つだけ。自動ログインはしない（人が Chrome Remote Desktop で
# 手動ログインする）。premarket 本体は接続リトライしながら待つので、人がログインした
# 時点で収集が始まる。
Write-Host "ログイン画面が出るまで $WaitSec 秒待つ（ログインは人が CRD で行う）。"
Start-Sleep -Seconds $WaitSec
exit 0
