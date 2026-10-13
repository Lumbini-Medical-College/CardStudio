#!/bin/bash
# Double-click to start LMC Card Studio on macOS.
cd "$(dirname "$0")"
open -a Docker 2>/dev/null
exec python3 run.py
