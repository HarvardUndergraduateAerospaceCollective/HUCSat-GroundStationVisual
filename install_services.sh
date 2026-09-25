#!/usr/bin/env bash
# Install (or refresh) the HUCSat systemd units on the mission-control Pi:
# the dashboard, the AWS sync, and the 3-hourly DB backup timer -- all under
# systemd, so they come back after a reboot or crash (a manually started
# aws_sync or db_backup loop does not).  Run from the repo directory after
# `git pull`:
#
#     ./install_services.sh
#
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

EXPECTED_DIR=/home/huac/Desktop/HUCSat-GroundStationVisual
if [ "$PWD" != "$EXPECTED_DIR" ]; then
    echo "The unit files expect $EXPECTED_DIR but this repo is at $PWD." >&2
    echo "Edit WorkingDirectory= in the .service files first." >&2
    exit 1
fi
for var in AWS_SYNC_URL AWS_SYNC_API_KEY; do
    if ! grep -q "^export $var=." mission.env 2>/dev/null; then
        echo "mission.env has no $var -- aws_sync would not start. Aborting." >&2
        exit 1
    fi
done

sudo install -m 644 hucsat-dashboard.service hucsat-awssync.service \
    hucsat-backup.service hucsat-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Stop any aws_sync / db_backup loop that systemd doesn't own (started by hand,
# under nohup/screen, or as root): it would race the units for the cursor file.
stop_strays() {   # $1 = pgrep -f regex, $2 = unit whose MainPID to keep
    local keep=0 pid
    if [ -n "$2" ]; then
        keep=$(systemctl show -p MainPID --value "$2" 2>/dev/null || echo 0)
    fi
    for pid in $(pgrep -f "$1" || true); do
        if [ "$pid" != "$keep" ]; then
            echo "Stopping stray process $pid: $(ps -o args= -p "$pid" 2>/dev/null || true)"
            sudo kill "$pid" 2>/dev/null || true
        fi
    done
}
stop_strays 'python[0-9.]* .*aws_sync\.py' hucsat-awssync
stop_strays 'python[0-9.]* .*db_backup\.py.*--interval' ''

# The services run as huac; a runtime file left root-owned by an old
# `sudo python3 ...` would make the sync or backup fail on every run.
for f in .aws_sync_state.json aws_sync_quarantine.jsonl \
         mission_data.db mission_data.db-wal mission_data.db-shm backups; do
    if [ -e "$f" ]; then
        sudo chown -R huac:huac "$f"
    fi
done

# The timer replaces the old "every 3 h" cron line from DEPLOYMENT_CHECKLIST.
for who in huac root; do
    if sudo crontab -u "$who" -l 2>/dev/null | grep -v '^[[:space:]]*#' | grep -q 'db_backup\.py'; then
        echo "WARNING: $who's crontab still runs db_backup.py -- remove that line" \
             "(sudo crontab -u $who -e) or backups will run twice." >&2
    fi
done

sudo systemctl enable hucsat-dashboard hucsat-awssync
sudo systemctl enable --now hucsat-backup.timer
sudo systemctl restart hucsat-dashboard hucsat-awssync
echo "Running one backup now to confirm it works..."
sudo systemctl start hucsat-backup.service || echo "BACKUP FAILED - see: journalctl -u hucsat-backup" >&2
sleep 5
systemctl --no-pager --lines=5 status hucsat-dashboard hucsat-awssync hucsat-backup.service || true
systemctl --no-pager list-timers hucsat-backup.timer || true
echo
echo "Follow the sync with:    journalctl -u hucsat-awssync -f"
echo "Backup log:              journalctl -u hucsat-backup   (and backups/backup.log)"
