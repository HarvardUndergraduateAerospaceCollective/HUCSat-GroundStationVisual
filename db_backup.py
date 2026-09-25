"""
db_backup.py — Automated backup for mission_data.db

Creates safe, verified backups of the CubeSAT mission database to:
  1. USB-connected SD card (physical backup)
  2. Google Drive via rclone (cloud backup)

Designed to run every 3 hours via cron or systemd timer on a Raspberry Pi.

Usage:
  python db_backup.py                   # Run both backups once and exit
  python db_backup.py --interval 10800  # Loop: back up every 3 hours (Ctrl+C to stop)
  python db_backup.py --local           # SD card only
  python db_backup.py --cloud           # Google Drive only
  python db_backup.py --dry-run         # Log what would happen
  python db_backup.py --status          # Check backup infrastructure

Setup (on the Raspberry Pi):
  1. Install rclone:
       curl https://rclone.org/install.sh | sudo bash

  2. Configure Google Drive remote (do this on a machine with a browser):
       rclone config
       > n (new remote)
       > name: gdrive
       > storage: drive
       > (follow OAuth prompts)
     Then copy ~/.config/rclone/rclone.conf to the Pi.

  3. Mount the backup USB drive (already formatted exFAT):
       sudo mkdir -p /mnt/cubesat-backup
       sudo mount /dev/sdb1 /mnt/cubesat-backup
       # For auto-mount on boot, get the UUID and add to fstab:
       sudo blkid /dev/sdb1
       echo 'UUID=<uuid>  /mnt/cubesat-backup  exfat  defaults,nofail,noatime  0  0' | sudo tee -a /etc/fstab
       sudo mkdir -p /mnt/cubesat-backup && sudo mount -a

  4. Add to crontab (every 3 hours):
       0 */3 * * *  /usr/bin/python3 /path/to/db_backup.py >> /dev/null 2>&1

  5. (Optional) Set Slack webhook for failure alerts:
       export CUBESAT_SLACK_WEBHOOK="https://hooks.slack.com/services/T.../B.../..."
"""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

# ── Configuration ──────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "mission_data.db"
STAGING_DIR = SCRIPT_DIR / "backups"
LOG_FILE = STAGING_DIR / "backup.log"
LOCK_FILE = STAGING_DIR / "backup.lock"

LOCAL_RETENTION_COUNT = 3

SD_MOUNT_POINT = Path("/mnt/cubesat-backup")
SD_BACKUP_DIR = SD_MOUNT_POINT / "mission_backups"
SD_RETENTION_DAYS = 30
SD_LOW_SPACE_MB = 100

RCLONE_REMOTE = "dbbackup"
RCLONE_DEST = f"{RCLONE_REMOTE}:CubeSAT-Backups"
RCLONE_TIMEOUT = 300
CLOUD_RETENTION_DAYS = 90

SLACK_WEBHOOK_URL = os.environ.get("CUBESAT_SLACK_WEBHOOK", "")

LOCK_STALE_SECONDS = 1800


# ── Logging ────────────────────────────────────

def setup_logging() -> logging.Logger:
    STAGING_DIR.mkdir(exist_ok=True)

    logger = logging.getLogger("db_backup")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)-8s  %(message)s"))
    logger.addHandler(ch)

    return logger


# ── Slack notifications ───────────────────────

def notify_slack(message: str, level: str = "warning"):
    """Post to Slack webhook. No-op when CUBESAT_SLACK_WEBHOOK is unset."""
    if not SLACK_WEBHOOK_URL:
        return
    emoji = {"info": ":white_check_mark:", "warning": ":warning:", "error": ":rotating_light:"}.get(level, ":bell:")
    payload = json.dumps({
        "text": f"{emoji} *CubeSAT Backup* — {message}",
        "username": "MissionStation Backup",
    })
    try:
        req = Request(
            SLACK_WEBHOOK_URL,
            data=payload.encode(),
            headers={"Content-Type": "application/json"},
        )
        urlopen(req, timeout=10)
    except Exception as exc:
        logging.getLogger("db_backup").debug(f"Slack notify failed: {exc}")


# ── Lock file ─────────────────────────────────

def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age < LOCK_STALE_SECONDS:
                return False
        except OSError:
            pass
    STAGING_DIR.mkdir(exist_ok=True)
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock():
    LOCK_FILE.unlink(missing_ok=True)


# ── Core backup logic ─────────────────────────

def safe_backup(src_path: Path, dst_path: Path) -> bool:
    """Use sqlite3.backup() for a WAL-safe copy of a live database."""
    log = logging.getLogger("db_backup")
    try:
        src = sqlite3.connect(str(src_path))
        dst = sqlite3.connect(str(dst_path))
        try:
            src.backup(dst, pages=256)
        finally:
            dst.close()
            src.close()
        log.info(f"Backup snapshot created: {dst_path.name} "
                 f"({dst_path.stat().st_size / 1024:.1f} KB)")
        return True
    except Exception as exc:
        log.error(f"sqlite3.backup() failed: {exc}")
        return False


def verify_backup(db_path: Path) -> bool:
    """Run PRAGMA integrity_check on the backup copy."""
    log = logging.getLogger("db_backup")
    try:
        conn = sqlite3.connect(str(db_path))
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        ok = result is not None and result[0] == "ok"
        if ok:
            log.info(f"Integrity verified: {db_path.name}")
        else:
            log.error(f"Integrity FAILED: {db_path.name} — {result}")
        return ok
    except Exception as exc:
        log.error(f"Integrity check error: {exc}")
        return False


def backup_filename() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"mission_{ts}.db"


# ── SD card backup ─────────────────────────────

def backup_to_sd(staging_path: Path) -> bool:
    log = logging.getLogger("db_backup")

    if not os.path.ismount(str(SD_MOUNT_POINT)):
        log.warning(f"SD card not mounted at {SD_MOUNT_POINT} — skipping")
        return False

    try:
        SD_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dest = SD_BACKUP_DIR / staging_path.name
        shutil.copy2(str(staging_path), str(dest))
        log.info(f"SD backup written: {dest}")

        rotate_sd_backups()
        check_sd_space(log)
        return True
    except Exception as exc:
        msg = f"SD backup failed: {exc}"
        log.error(msg)
        notify_slack(msg, "error")
        return False


def rotate_sd_backups():
    cutoff = time.time() - (SD_RETENTION_DAYS * 86400)
    log = logging.getLogger("db_backup")
    for f in SD_BACKUP_DIR.glob("mission_*.db"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                log.debug(f"Rotated SD backup: {f.name}")
        except OSError:
            pass


def check_sd_space(log: logging.Logger):
    try:
        usage = shutil.disk_usage(str(SD_MOUNT_POINT))
        free_mb = usage.free / (1024 * 1024)
        if free_mb < SD_LOW_SPACE_MB:
            msg = f"SD card low on space: {free_mb:.0f} MB remaining"
            log.warning(msg)
            notify_slack(msg, "warning")
    except OSError:
        pass


# ── Cloud backup (Google Drive via rclone) ────

def backup_to_cloud(staging_path: Path) -> bool:
    log = logging.getLogger("db_backup")

    if shutil.which("rclone") is None:
        log.warning("rclone not found in PATH — skipping cloud backup")
        return False

    try:
        result = subprocess.run(
            ["rclone", "copy", str(staging_path), RCLONE_DEST,
             "--log-level", "ERROR"],
            capture_output=True, text=True, timeout=RCLONE_TIMEOUT,
        )
        if result.returncode == 0:
            log.info(f"Cloud backup uploaded to {RCLONE_DEST}/{staging_path.name}")
            return True

        msg = f"rclone failed (exit {result.returncode}): {result.stderr.strip()}"
        log.error(msg)
        notify_slack(msg, "error")
        return False
    except subprocess.TimeoutExpired:
        msg = f"Cloud backup timed out ({RCLONE_TIMEOUT}s)"
        log.error(msg)
        notify_slack(msg, "error")
        return False
    except Exception as exc:
        msg = f"Cloud backup error: {exc}"
        log.error(msg)
        notify_slack(msg, "error")
        return False


def rotate_cloud_backups():
    log = logging.getLogger("db_backup")
    if shutil.which("rclone") is None:
        return
    try:
        subprocess.run(
            ["rclone", "delete", RCLONE_DEST,
             "--min-age", f"{CLOUD_RETENTION_DAYS}d",
             "--log-level", "ERROR"],
            capture_output=True, text=True, timeout=120,
        )
        log.debug("Cloud backup rotation completed")
    except Exception as exc:
        log.debug(f"Cloud rotation error: {exc}")


# ── Local staging rotation ────────────────────

def rotate_local_staging():
    backups = sorted(STAGING_DIR.glob("mission_*.db"))
    for old in backups[:-LOCAL_RETENTION_COUNT]:
        old.unlink(missing_ok=True)


# ── Status check ──────────────────────────────

def check_status():
    """Print diagnostic info about backup infrastructure."""
    print(f"Database:     {DB_PATH}")
    print(f"  exists:     {DB_PATH.exists()}")
    if DB_PATH.exists():
        print(f"  size:       {DB_PATH.stat().st_size / 1024:.1f} KB")

    print(f"\nSD card:      {SD_MOUNT_POINT}")
    print(f"  mounted:    {os.path.ismount(str(SD_MOUNT_POINT))}")
    if os.path.ismount(str(SD_MOUNT_POINT)):
        usage = shutil.disk_usage(str(SD_MOUNT_POINT))
        print(f"  free:       {usage.free / (1024**2):.0f} MB")
        existing = list(SD_BACKUP_DIR.glob("mission_*.db")) if SD_BACKUP_DIR.exists() else []
        print(f"  backups:    {len(existing)}")

    rclone = shutil.which("rclone")
    print(f"\nrclone:       {'installed' if rclone else 'NOT FOUND'}")
    if rclone:
        try:
            result = subprocess.run(
                ["rclone", "listremotes"],
                capture_output=True, text=True, timeout=10,
            )
            remotes = result.stdout.strip().split("\n") if result.stdout.strip() else []
            target = f"{RCLONE_REMOTE}:"
            configured = target in remotes
            print(f"  remote:     {RCLONE_REMOTE} ({'configured' if configured else 'NOT CONFIGURED'})")
        except Exception:
            print(f"  remote:     could not check")

    print(f"\nSlack:        {'configured' if SLACK_WEBHOOK_URL else 'not configured'}")

    local_backups = list(STAGING_DIR.glob("mission_*.db")) if STAGING_DIR.exists() else []
    print(f"\nLocal staging: {len(local_backups)} backup(s) in {STAGING_DIR}")
    if local_backups:
        newest = max(local_backups, key=lambda f: f.stat().st_mtime)
        age_hrs = (time.time() - newest.stat().st_mtime) / 3600
        print(f"  newest:     {newest.name} ({age_hrs:.1f} hours ago)")


# ── Main ───────────────────────────────────────

def run_backup(*, do_local: bool = True, do_cloud: bool = True,
               dry_run: bool = False, do_sync: bool = True) -> bool:
    """Run one backup. Returns False if it failed or any destination failed."""
    log = setup_logging()
    log.info("=" * 50)
    log.info("Backup run started")

    if not DB_PATH.exists():
        msg = f"Database not found: {DB_PATH}"
        log.error(msg)
        notify_slack(msg, "error")
        return False

    # Pre-backup AWS sync: pull any packets the Pi missed
    if do_sync and os.environ.get("CUBESAT_AWS_URL"):
        log.info("Running AWS sync before backup ...")
        try:
            from compare import sync_once
            stats = sync_once()
            log.info(f"Sync result: {stats['new']} new, "
                     f"{stats['skipped']} skipped, {stats['errors']} errors")
        except Exception as exc:
            log.warning(f"Pre-backup sync failed (continuing with backup): {exc}")

    log.info(f"Source DB: {DB_PATH.stat().st_size / 1024:.1f} KB")

    if dry_run:
        log.info("[DRY RUN] Would create snapshot and back up to: "
                 + ", ".join(filter(None, [
                     "SD card" if do_local else None,
                     "Google Drive" if do_cloud else None,
                 ])))
        return True

    if not acquire_lock():
        log.warning("Another backup is already running — exiting")
        return True
    try:
        return _run_backup_locked(log, do_local, do_cloud)
    finally:
        release_lock()


def _run_backup_locked(log: logging.Logger, do_local: bool, do_cloud: bool) -> bool:
    STAGING_DIR.mkdir(exist_ok=True)
    staging_path = STAGING_DIR / backup_filename()

    if not safe_backup(DB_PATH, staging_path):
        notify_slack("sqlite3.backup() failed — no backup created", "error")
        return False

    if not verify_backup(staging_path):
        staging_path.unlink(missing_ok=True)
        notify_slack("Backup integrity check failed — discarded", "error")
        return False

    results = {}

    if do_local:
        results["SD card"] = backup_to_sd(staging_path)
    if do_cloud:
        results["Google Drive"] = backup_to_cloud(staging_path)
        rotate_cloud_backups()

    rotate_local_staging()

    parts = [f"{k}={'OK' if v else 'FAIL'}" for k, v in results.items()]
    summary = f"Backup complete: {', '.join(parts)}"
    log.info(summary)

    failures = [k for k, v in results.items() if not v]
    if failures:
        notify_slack(summary, "warning")
    return not failures


def run_forever(interval: int, *, do_local: bool = True, do_cloud: bool = True,
                do_sync: bool = True):
    """Run a backup every `interval` seconds until interrupted.

    Each iteration is a full run_backup() (which logs, verifies, rotates and
    Slack-alerts on its own and does not raise for a backup failure); the call
    is still guarded so an unexpected error just skips to the next interval
    rather than killing the loop. Ctrl+C / SIGINT exits cleanly.

    NOTE: prefer cron/systemd for production — those start each run in a fresh
    process, so a crash or leak can't silently stop the cadence. This internal
    loop is a convenience and dies if the process does.
    """
    log = setup_logging()
    log.info("Backup loop started: every %d s (%.1f h). Ctrl+C to stop.",
             interval, interval / 3600.0)
    try:
        while True:
            try:
                run_backup(do_local=do_local, do_cloud=do_cloud, dry_run=False,
                           do_sync=do_sync)
            except Exception:
                log.exception("Backup run raised unexpectedly; continuing to next interval")
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("Backup loop stopped by user")


def main():
    parser = argparse.ArgumentParser(
        description="CubeSAT Mission DB Backup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--local", action="store_true", help="SD card backup only")
    parser.add_argument("--cloud", action="store_true", help="Cloud backup only")
    parser.add_argument("--dry-run", action="store_true", help="Log without executing")
    parser.add_argument("--no-sync", action="store_true",
                        help="Skip AWS sync before backup")
    parser.add_argument("--status", action="store_true", help="Check backup infrastructure")
    parser.add_argument("--test-slack", metavar="MESSAGE", help="Send a test Slack message")
    parser.add_argument("--interval", type=int, metavar="SECONDS",
                        help="Loop: run a backup every SECONDS instead of once "
                             "(minimum 60). E.g. --interval 10800 for every 3 hours.")
    args = parser.parse_args()

    if args.status:
        check_status()
        return

    if args.test_slack:
        log = setup_logging()
        webhook = os.environ.get("CUBESAT_SLACK_WEBHOOK", "")
        if not webhook:
            print("ERROR: CUBESAT_SLACK_WEBHOOK is not set.")
            return
        notify_slack(args.test_slack, "info")
        print("Test message sent - check your Slack channel.")
        return

    do_local = True
    do_cloud = True
    if args.local or args.cloud:
        do_local = args.local
        do_cloud = args.cloud

    if args.dry_run:
        # A dry run is always a single pass, even alongside --interval.
        run_backup(do_local=do_local, do_cloud=do_cloud, dry_run=True,
                   do_sync=not args.no_sync)
    elif args.interval:
        if args.interval < 60:
            parser.error("--interval must be at least 60 seconds")
        run_forever(args.interval, do_local=do_local, do_cloud=do_cloud,
                    do_sync=not args.no_sync)
    else:
        # Non-zero exit so a failed timer run shows as failed in systemd.
        if not run_backup(do_local=do_local, do_cloud=do_cloud, dry_run=False,
                          do_sync=not args.no_sync):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
