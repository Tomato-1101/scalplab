# 寄り前気配の記録を平日 07:55 に自動起動するタスクを登録する（Windows 用）。
#
# 実行: リポジトリルートで（**管理者 PowerShell** で行うこと。最上位特権タスクの登録と
#       powercfg のスリープ解除タイマー設定に必要）
#   powershell -ExecutionPolicy Bypass -File scripts\register_premarket_task.ps1
#
# 登録されるタスク:
#   名前   scalplab-premarket
#   起動   平日（月〜金）07:55。以後 09:05 まで 5 分ごとにリトライ
#   実行   scripts\premarket_task.cmd（リポジトリルートへ cd してから python -m scalplab.premarket）
#
# リトライ設計（2026-08-18 要件。「07:55 に失敗しても、08:00〜09:00 の間に
# PC を起動して kabuステーションを再起動しさえすれば録れる」を成立させる）:
#   - 5 分ごとの繰り返し（07:55 から 70 分間）: kabu 未起動/401 なら premarket が即 exit 1 で
#     終わり、5 分後の繰り返しが再挑戦する。premarket は途中参加でき（now〜09:07 を録る）、
#     09:07 以降の空振りは「何もせず終了」なので無害。
#   - MultipleInstances=IgnoreNew: 収集が走っている間は繰り返しが二重起動しない
#     （premarket 自体に多重起動ガードは無いので、ここで防ぐのが必須）。
#   - StartWhenAvailable: 07:55 に PC が寝ていても、起動後すぐ実行される。
#   - WakeToRun + スリープ解除タイマー（2026-08-21 要件「朝起きられなくても録れるように」）:
#     夜はシャットダウンせず**スリープ/休止**にしておけば、07:55（と 5 分ごとの繰り返し）に
#     PC が自動復帰して無人で録れる。シャットダウンからは起こせない（BIOS の RTC アラームの
#     領分。機種依存のため今回は採用しない＝夜はスリープ運用にする）。
#
# 前提: kabuステーションアプリが起動・ログイン済みであること。前日から起動しっぱなしの
# アプリは日付が変わると API が 401 になる（troubleshooting.md 2026-08-18 実測）が、これは
# premarket_task.cmd が scripts\ensure_kabustation.ps1 を毎回呼んで「今朝起動した状態」に
# 揃えるため自動化済み（タスク定義自体は変わらないので、このスクリプトの再実行は不要）。

$ErrorActionPreference = "Stop"
Import-Module ScheduledTasks
$root = Split-Path -Parent $PSScriptRoot
$cmd = Join-Path $root "scripts\premarket_task.cmd"
if (-not (Test-Path $cmd)) { throw "premarket_task.cmd が見つからない: $cmd" }

$action = New-ScheduledTaskAction -Execute $cmd
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At 07:55
# Weekly トリガーは Repetition を直接指定できないため、Once トリガーから移植する定石を使う
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At 07:55 `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Minutes 70)).Repetition
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)

# 最上位特権で実行（ヘッダのコメント参照）。ログオンセッションが必要なので Interactive のまま。
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName "scalplab-premarket" -Action $action `
    -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

# スリープ解除タイマーを許可（無効の環境だと WakeToRun が効かず、朝 PC が起きない）。
# GUID は電源オプション「スリープ > スリープ解除タイマーの許可」。DC 側は電池の無いデスクトップでも無害。
$wakeGuid = "bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d"
powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP $wakeGuid 1 | Out-Null
powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_SLEEP $wakeGuid 1 | Out-Null
powercfg /SETACTIVE SCHEME_CURRENT | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "スリープ解除タイマーの設定に失敗した。管理者 PowerShell で再実行すること。"
}

Write-Host ""
Write-Host "登録完了。確認:"
$t = Get-ScheduledTask -TaskName "scalplab-premarket"
$tr = $t.Triggers[0]
Write-Host ("  実行     : " + $t.Actions[0].Execute)
Write-Host ("  開始     : " + $tr.StartBoundary + " （" + ($tr.DaysOfWeek) + "）")
Write-Host ("  リトライ : " + $tr.Repetition.Interval + " ごと / " + $tr.Repetition.Duration + " 間")
Write-Host ("  多重起動 : " + $t.Settings.MultipleInstances + " / 起動後即実行: " + $t.Settings.StartWhenAvailable)
Write-Host ("  自動復帰 : WakeToRun=" + $t.Settings.WakeToRun + " / 権限: " + $t.Principal.RunLevel)
Write-Host ""
Write-Host "スリープ復帰の確認: powercfg /waketimers に次回の 07:55 が載っていること"
Write-Host "手動テスト実行: schtasks /Run /TN scalplab-premarket"
Write-Host "削除:           schtasks /Delete /TN scalplab-premarket /F"
