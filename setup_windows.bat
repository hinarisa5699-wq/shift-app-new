@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo =============================================
echo   シフト自動作成アプリ - 初期セットアップ
echo =============================================
echo.

REM --- Python検出 ---
echo [1/4] Pythonを探しています...

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
    if !errorlevel! == 0 (
        set "PY_CMD=python"
    )
)

if not defined PY_CMD (
    py --version >nul 2>&1
    if !errorlevel! == 0 (
        set "PY_CMD=py"
    )
)

if not defined PY_CMD (
    echo.
    echo [エラー] Pythonが見つかりません。
    echo.
    echo 以下の手順でPythonをインストールしてください:
    echo   1. https://www.python.org/downloads/ を開く
    echo   2. 「Download Python 3.x」ボタンをクリック
    echo   3. インストーラーを実行
    echo   4. ★重要★「Add Python to PATH」にチェックを入れる
    echo   5. 「Install Now」をクリック
    echo   6. インストール後、PCを再起動してからこのファイルを再実行
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('%PY_CMD% --version 2^>^&1') do set "PY_VER=%%i"
echo   %PY_VER% が見つかりました。

REM --- 仮想環境の作成 ---
if exist venv (
    echo [2/4] 古い仮想環境を削除中...
    rmdir /s /q venv
    if exist venv (
        echo [エラー] 仮想環境の削除に失敗しました。
        echo 他のプログラムを閉じてから再実行してください。
        pause
        exit /b 1
    )
)

echo [2/4] 仮想環境を作成中...
%PY_CMD% -m venv venv
if errorlevel 1 (
    echo.
    echo [エラー] 仮想環境の作成に失敗しました。
    echo Pythonが正しくインストールされているか確認してください。
    pause
    exit /b 1
)

REM --- 依存パッケージのインストール ---
echo [3/4] 必要なパッケージをインストール中...
echo   （数分かかる場合があります。しばらくお待ちください）
call venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul 2>&1
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [エラー] パッケージのインストールに失敗しました。
    echo インターネット接続を確認してください。
    pause
    exit /b 1
)

REM --- 完了 ---
echo [4/4] セットアップ完了！
echo.
echo =============================================
echo   セットアップが正常に完了しました。
echo   「start_windows.bat」をダブルクリックして
echo   アプリを起動してください。
echo =============================================
echo.
pause
exit /b 0
