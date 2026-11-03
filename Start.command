#!/bin/bash
# Double-click to run. The first run sets up the environment; after that it opens the app in your browser.
cd "$(dirname "$0")" || exit 1

pause_and_exit() {
  echo "$1"
  read -r -p "Press Enter to close this window"
  exit 1
}

if [ ! -x .venv/bin/python ]; then
  echo "First run: setting things up (takes about a minute)…"
  python3 -m venv .venv || pause_and_exit "Couldn't create the Python environment. Make sure Python 3 is installed."
fi
if ! .venv/bin/python -c "import flask, requests, dotenv, openpyxl" 2>/dev/null; then
  .venv/bin/pip install -q -r requirements.txt || pause_and_exit "Couldn't install the dependencies. Check your network and try again."
fi

.venv/bin/python app.py
