#!/usr/bin/env sh
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "First run: setting up. This takes a minute."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements.txt
fi
( sleep 2; (command -v open >/dev/null && open http://127.0.0.1:8765) || xdg-open http://127.0.0.1:8765 ) >/dev/null 2>&1 &
exec .venv/bin/python app.py
