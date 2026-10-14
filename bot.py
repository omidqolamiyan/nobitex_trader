name: Nobitex Market Bot

on:
workflow_dispatch:

schedule:
- cron: "*/5 * * * *"

push:
paths-ignore:
- "paper_state_v4.json"

permissions:
contents: write

concurrency:
group: nobitex-paper-trader
cancel-in-progress: false

jobs:
run-bot:
runs-on: ubuntu-latest

steps:
  - name: دریافت مخزن
    uses: actions/checkout@v4
    with:
      fetch-depth: 0

  - name: نصب Python
    uses: actions/setup-python@v5
    with:
      python-version: "3.12"

  - name: اجرای ربات
    run: python bot.py

  - name: ذخیره وضعیت معاملات مجازی
    run: |
      git config user.name "github-actions[bot]"
      git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

      git add paper_state_v4.json

      if ! git diff --cached --quiet; then
        git commit -m "Update paper trading state"
        git push
      else
        echo "No state changes to save."
      fi
