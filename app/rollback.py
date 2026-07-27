"""
Rollback engine — restore databases and code from pre-promotion backups.
"""

import subprocess
import shutil
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import odoo_client as oc
from . import gitops


BACKUP_ROOT = "/opt/pb-promote/backups"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


@dataclass
class RollbackResult:
    success: bool
    environment: str
    stage: str
    error: str = ""
    output: str = ""


def rollback_stage(backup_db_path: str = "", backup_code_path: str = "") -> RollbackResult:
    """Rollback stage to a previous backup. If paths not provided, uses most recent."""
    r = RollbackResult(environment="stage", stage="initiated")

    # If no specific backup, find the most recent stage backup
    if not backup_db_path:
        backup_db_path = _find_latest_backup("stage", "db")
    if not backup_code_path:
        backup_code_path = _find_latest_backup("stage", "code")

    if not backup_db_path and not backup_code_path:
        r.stage = "failed"
        r.error = "No stage backups found"
        return r

    # 1. Stop stage service
    r.stage = "stopping"
    subprocess.run(
        ["sudo", "systemctl", "stop", "odoo-stage"],
        capture_output=True, timeout=30,
    )
    time.sleep(2)

    # 2. Restore DB
    if backup_db_path and os.path.isfile(backup_db_path):
        r.stage = "restoring_db"
        try:
            # Drop and recreate
            subprocess.run(
                ["sudo", "-u", "postgres", "psql", "-c",
                 "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                 "WHERE datname='odoo19stage';"],
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["sudo", "-u", "postgres", "dropdb", "--if-exists", "odoo19stage"],
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["sudo", "-u", "postgres", "createdb", "odoo19stage"],
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["sudo", "-u", "postgres", "psql", "-d", "odoo19stage",
                 "-f", backup_db_path],
                capture_output=True, text=True, timeout=300,
            )
            subprocess.run(
                ["sudo", "-u", "postgres", "psql", "-c",
                 "ALTER DATABASE odoo19stage OWNER TO odoo19stage;"],
                capture_output=True, timeout=10,
            )
        except subprocess.CalledProcessError as e:
            r.stage = "failed"
            r.error = f"DB restore failed: {e.stderr.strip()[:300]}"
            return r

    # 3. Restore code
    if backup_code_path and os.path.isdir(backup_code_path):
        r.stage = "restoring_code"
        try:
            # Remove current stage code, restore from backup
            stage_root = "/opt/odoo19stage"
            subprocess.run(
                ["sudo", "rm", "-rf", stage_root],
                capture_output=True, timeout=30,
            )
            subprocess.run(
                ["sudo", "cp", "-a", backup_code_path, stage_root],
                capture_output=True, timeout=300,
            )
        except Exception as e:
            r.stage = "failed"
            r.error = f"Code restore failed: {str(e)[:300]}"
            return r

    # 4. Clear .pyc, restart, verify
    r.stage = "verifying"
    subprocess.run(
        ["find", "/opt/odoo19stage", "-name", "*.pyc", "-delete"],
        capture_output=True, timeout=30,
    )
    subprocess.run(
        ["sudo", "systemctl", "start", "odoo-stage"],
        capture_output=True, timeout=30,
    )
    time.sleep(3)

    active, _ = oc.check_service("stage")
    if active:
        r.stage = "success"
        r.success = True
    else:
        r.stage = "failed"
        r.error = "Stage service failed to start after rollback"

    return r


def rollback_prod(backup_db_path: str = "", backup_code_path: str = "") -> RollbackResult:
    """Rollback prod to a previous backup. Handles with extreme care."""
    r = RollbackResult(environment="prod", stage="initiated")

    if not backup_db_path:
        backup_db_path = _find_latest_backup("prod", "db")
    if not backup_code_path:
        backup_code_path = _find_latest_backup("prod", "code")

    if not backup_db_path:
        r.stage = "failed"
        r.error = "No prod DB backup found — refusing rollback without DB backup"
        return r

    # 1. Stop prod
    r.stage = "stopping"
    subprocess.run(
        ["sudo", "systemctl", "stop", "odoo"],
        capture_output=True, timeout=30,
    )
    time.sleep(3)

    # 2. Restore DB
    r.stage = "restoring_db"
    try:
        subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-c",
             "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
             "WHERE datname='odoo19prod';"],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["sudo", "-u", "postgres", "dropdb", "--if-exists", "odoo19prod"],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["sudo", "-u", "postgres", "createdb", "odoo19prod"],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-d", "odoo19prod",
             "-f", backup_db_path],
            capture_output=True, text=True, timeout=300,
        )
        subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-c",
             "ALTER DATABASE odoo19prod OWNER TO odoo19prod;"],
            capture_output=True, timeout=10,
        )
    except subprocess.CalledProcessError as e:
        r.stage = "failed"
        r.error = f"Prod DB restore failed: {e.stderr.strip()[:300]}"
        # Try to restart prod anyway
        subprocess.run(["sudo", "systemctl", "start", "odoo"], capture_output=True)
        return r

    # 3. Restore code
    if backup_code_path and os.path.isdir(backup_code_path):
        r.stage = "restoring_code"
        try:
            # git-based rollback preferred — use gitops
            # Fallback: file copy
            prod_root = "/usr/lib/python3/dist-packages"
            _restore_from_snapshot(backup_code_path, prod_root)
        except Exception as e:
            r.error = (r.error + f" Code restore: {str(e)[:200]}")

    # 4. Clear .pyc, restart, verify
    r.stage = "verifying"
    subprocess.run(
        ["find", "/usr/lib/python3/dist-packages", "-name", "*.pyc", "-delete"],
        capture_output=True, timeout=30,
    )
    subprocess.run(
        ["sudo", "systemctl", "start", "odoo"],
        capture_output=True, timeout=30,
    )
    time.sleep(5)

    active, _ = oc.check_service("prod")
    if active:
        r.stage = "success"
        r.success = True
        r.output = "Prod rollback complete — service is running"
    else:
        r.stage = "failed"
        r.error = "Prod service failed to start after rollback"

    return r


def _find_latest_backup(env: str, backup_type: str) -> str:
    """Find the most recent backup for an environment."""
    if not os.path.isdir(BACKUP_ROOT):
        return ""

    # Look for directories matching <env>_<timestamp>
    candidates = []
    for entry in os.listdir(BACKUP_ROOT):
        if entry.startswith(f"{env}_"):
            full = os.path.join(BACKUP_ROOT, entry)
            if os.path.isdir(full):
                candidates.append((entry, full))

    candidates.sort(reverse=True)  # newest first

    for _, dirpath in candidates:
        if backup_type == "db":
            # Find .sql file
            for f in os.listdir(dirpath):
                if f.endswith(".sql"):
                    return os.path.join(dirpath, f)
        elif backup_type == "code":
            code_dir = os.path.join(dirpath, "code")
            if os.path.isdir(code_dir):
                return code_dir

    return ""


def _restore_from_snapshot(snapshot_path: str, target_root: str):
    """Restore specific files from a code snapshot."""
    # Walk snapshot and copy files that differ
    for root, dirs, files in os.walk(snapshot_path):
        rel = os.path.relpath(root, snapshot_path)
        target_dir = os.path.join(target_root, rel) if rel != "." else target_root
        for f in files:
            src = os.path.join(root, f)
            dst = os.path.join(target_dir, f)
            if os.path.isfile(dst):
                shutil.copy2(src, dst)
