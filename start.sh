#!/bin/bash
set -a
source .env
set +a
cd "$(dirname "$0")"
python3 bot/main.py


