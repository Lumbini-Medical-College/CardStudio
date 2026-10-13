@echo off
REM LMC Card Studio in server mode (logins + print queue). Staff open http://THIS-PC-IP:8765
cd /d "%~dp0"
python run.py --server
pause
