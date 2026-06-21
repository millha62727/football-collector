#!/usr/bin/env bash
# Goal-1 daily report cron wrapper.
#
# Runs at 23:55 local time each day so the daily card is ready when the user
# opens the chat the next morning. Generates the Markdown report for "today"
# (which at 23:55 VN is essentially the day's worth of paper trades).
#
# flock prevents overlap if a previous run is still going.
#
# Installed via: crontab -e, then add:
#   55 23 * * * bash /root/goal1_daily_report_cron.sh
#
# VPS timezone is Asia/Ho_Chi_Minh (= UTC+7), so 23:55 cron = 23:55 VN.
set -euo pipefail
LOCK=/tmp/goal1_daily_report.lock
LOG=/root/goal1_daily_report.log
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) [cron] previous report still running, skip" >> "$LOG"
  exit 0
fi
{
  echo "===== $(date -Is) report start ====="
  docker exec football_collector python3 scripts/goal1_daily_report.py 2>&1 \
    || echo "[cron] report exited non-zero"
  echo "===== $(date -Is) report end ====="
} >> "$LOG" 2>&1
# Bounded log (last 1000 lines is plenty for one entry/day).
tail -n 1000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
