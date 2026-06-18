#!/usr/bin/env bash
# Launcher for Linux_LLM — activates the venv and runs main.py
cd "$(dirname "$0")"
source .venv/bin/activate
python main.py "$@"
