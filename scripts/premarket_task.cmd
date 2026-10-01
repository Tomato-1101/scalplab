@echo off
rem 寄り前気配の記録（scalplab.premarket）をタスクスケジューラから起動するラッパ。
rem %~dp0 = このファイルのあるディレクトリ（scripts\）→ 1つ上がリポジトリルート。
rem schtasks は作業ディレクトリを指定できないため、cd を自前で行う（docs/windows-setup.md §7 の教訓）。
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
rem ログを UTF-8 に揃える（このファイルも BOM 無し UTF-8）。既定 cp932 のままだと
rem PowerShell の Write-Host / python の traceback が premarket-task.log で文字化けし、
rem 障害調査の一次資料が読めない（2026-08-28 の欠測調査で実害）。
rem chcp はコンソール出力（PowerShell）用、PYTHONUTF8 はリダイレクト時の python 用
rem （python はパイプ/ファイルへは ANSI CP で書くため chcp だけでは化けたままになる）。
chcp 65001 >nul
set PYTHONUTF8=1
rem この窓を手で閉じて収集プロセスが死んだ実績が 2 回ある（8/21 09:00:20・9/1 09:00-09:05 疑い）。
rem python の出力はログへリダイレクトされ窓には何も出ない＝「何のための窓か」が見えないのが原因なので、
rem タイトルとバナーで「閉じるな・いつ終わるか」を窓自体に表示する（echo はリダイレクトしない）。
title 【閉じないで】scalp-lab 寄り前収集 - 09:07 ごろ自動で終了します
echo ============================================================
echo.
echo    この黒い窓は閉じないでください！
echo.
echo    寄り前の気配を記録しています。
echo    09:07 ごろに自動で終了し、窓もひとりでに閉じます。
echo    手で閉じると今日のデータが欠けます。
echo.
echo    kabuステーション のアプリも収集が終わるまで閉じないでください。
echo.
echo ============================================================
rem 前日から起動しっぱなしのアプリは日付が変わると token が 401 になる（2026-08-18 実測）。
rem 収集の前にアプリを「今朝起動した状態」へ揃える。失敗しても python は実行する
rem （premarket が接続失敗で exit 1 し、5 分後のタスク繰り返しが再挑戦する）。
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\ensure_kabustation.ps1" >> "logs\premarket-task.log" 2>&1
.venv\Scripts\python.exe -m scalplab.premarket >> "logs\premarket-task.log" 2>&1
