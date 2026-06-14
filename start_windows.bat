@echo off
setlocal

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo.
    echo [エラー] 初期セットアップがまだ完了していません。
    echo 先に「setup_windows.bat」をダブルクリックして実行してください。
    echo.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

echo.
echo =============================================
echo   シフト自動作成アプリを起動中...
echo   ブラウザが自動で開きます。
echo.
echo   開かない場合はブラウザで以下を入力:
echo     http://localhost:5050
echo.
echo   終了するにはこのウィンドウを閉じてください。
echo =============================================
echo.

python app.py

echo.
echo アプリが停止しました。
pause
exit /b 0
