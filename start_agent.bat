@echo off
REM Print agent for LMC Card Studio (server mode). Keep this window open while printing.
REM First time, run once:  python agent.py --server http://SERVER-IP:8765 --key AGENT-KEY
cd /d "%~dp0"
python agent.py
pause
