@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo.
echo =============================================
echo   シフト自動作成アプリ
echo =============================================
echo.

REM --- 仮想環境が未作成なら初回セットアップ ---
if exist "venv\Scripts\activate.bat" goto :start_app

echo 初回セットアップを実行します...
echo （インターネット接続が必要です）
echo.

REM --- Python検出 ---
echo Pythonを探しています...
set "PY_CMD="

for %%V in (3.13 3.12 3.11 3.10 3.9) do (
    if not defined PY_CMD (
        py -%%V --version >nul 2>&1
        if !errorlevel! == 0 (
            set "PY_CMD=py -%%V"
        )
    )
)

if not defined PY_CMD (
    python --version >nul 2>&1
    if !errorlevel! == 0 set "PY_CMD=python"
)

if not defined PY_CMD (
    py --version >nul 2>&1
    if !errorlevel! == 0 set "PY_CMD=py"
)

if not defined PY_CMD (
    echo.
    echo [エラー] Pythonが見つかりません。
    echo.
    echo 以下の手順でインストールしてください:
    echo   1. https://www.python.org/downloads/ を開く
    echo   2. 「Download Python」ボタンをクリック
    echo   3. ★重要★「Add Python to PATH」にチェックを入れる
    echo   4. 「Install Now」をクリック
    echo   5. PCを再起動してから、このファイルを再度ダブルクリック
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('%PY_CMD% --version 2^>^&1') do echo   %%i を検出しました。

echo 仮想環境を作成中...
%PY_CMD% -m venv venv
if errorlevel 1 (
    echo [エラー] 仮想環境の作成に失敗しました。
    pause
    exit /b 1
)

echo パッケージをインストール中...
echo （数分かかる場合があります。お待ちください）
echo.
call venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul 2>&1
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [エラー] パッケージのインストールに失敗しました。
    echo インターネット接続を確認してください。
    pause
    exit /b 1
)

echo.
echo セットアップ完了！アプリを起動します...
echo.

:start_app
call venv\Scripts\activate.bat

echo =============================================
echo   アプリを起動中です...
echo   ブラウザが自動で開きます。
echo.
echo   開かない場合は以下のURLをブラウザに入力:
echo     http://localhost:5050
echo.
echo   終了 → このウィンドウを閉じる
echo =============================================
echo.

python app.py

echo.
echo アプリが停止しました。
pause
exit /b 0
