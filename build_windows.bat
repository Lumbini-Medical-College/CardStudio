@echo off
REM Builds the desktop app: dist\CardStudio\CardStudio.exe  (run on a Windows PC with Python 3.9+)
cd /d "%~dp0"
python -m pip install --upgrade pip || goto :fail
python run.py --setup || goto :fail
python -m pip install "pyinstaller>=6.3" "pywebview>=5.0" || goto :fail
python -m PyInstaller --noconfirm --clean CardStudio.spec || goto :fail
if exist data\cards.db (
  mkdir dist\CardStudio\data 2>nul
  copy /y data\cards.db dist\CardStudio\data\cards.db >nul
  echo Copied your saved cards into dist\CardStudio\data
)
echo.
echo Done. The app is in dist\CardStudio. Copy that whole folder to the printer PC
echo and double-click CardStudio.exe.
pause
exit /b 0
:fail
echo.
echo Build failed - see the messages above.
pause
exit /b 1
