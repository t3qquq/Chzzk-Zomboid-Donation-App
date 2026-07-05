@echo off
echo ============================================
echo   Puppet API  ( gui.py )  --^>  exe build
echo ============================================
echo.

echo [1/2] Installing / updating PyInstaller...
py -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo.
    echo [!] pip failed. Is Python installed?  Run:  py --version
    pause
    exit /b 1
)
echo.

echo [2/2] Building exe... ^(may take a few minutes^)
py -m PyInstaller --onefile --noconsole --name PuppetAPI --icon=PuppetAPI_smart.ico --add-data "PuppetAPI_smart.ico;." --add-data "opt_conf;opt_conf" --collect-all chzzkpy --collect-all ahttp_client gui.py
if errorlevel 1 (
    echo.
    echo [!] Build failed. Copy the red error above and ask about it.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   DONE!  Run  dist\PuppetAPI.exe
echo   ^(share that single file - others just double-click^)
echo ============================================
pause
