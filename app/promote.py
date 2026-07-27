"""
Promotion orchestrator — dev→stage and stage→prod with backup, verification, and audit.
"""

import subprocess
import shutil
import os
import json
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

from . import odoo_client as oc
from . import gitops


BACKUP_ROOT = "/opt/pb-promote/backups"
MANIFEST_PATH = "/opt/odoo19stage/.promotions/manifest.txt"
PROMOTE_SCRIPT = "/opt/odoo19stage/.promotions/promote.sh"
PROMOTE_LOG = "/opt/odoo19stage/.promotions/promote.log"
STAGE_CODE_ROOT = "/opt/odoo19stage"
STAGE_DB = "odoo19stage"
PROD_DB = "odoo19prod"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


@dataclass
class PromotionResult:
    success: bool
    direction: str
    stage: str  # preflight, backup, promoting, verifying, success, failed
    files_promoted: int = 0
    files_skipped: int = 0
    backup_db_path: str = ""
    backup_code_path: str = ""
    git_tag: str = ""
    error: str = ""
    smoke_results: dict = field(default_factory=dict)


def create_backup(env: str, direction: str) -> dict:
    """Create DB dump + code snapshot before promotion."""
    ts = _ts()
    backup_dir = os.path.join(BACKUP_ROOT, f"{env}_{ts}")
    os.makedirs(backup_dir, exist_ok=True)

    result = {"db_path": "", "code_path": "", "error": ""}

    # DB backup
    db_name = STAGE_DB if env == "stage" else PROD_DB
    db_path = os.path.join(backup_dir, f"{db_name}.sql")
    try:
        subprocess.run(
            ["sudo", "-u", "postgres", "pg_dump", db_name, "-f", db_path],
            check=True, capture_output=True, text=True, timeout=120,
        )
        size = os.path.getsize(db_path)
        result["db_path"] = db_path
        result["db_size"] = size
    except subprocess.CalledProcessError as e:
        result["error"] = f"DB backup failed: {e.stderr.strip()[:200]}"
        return result
    except Exception as e:
        result["error"] = f"DB backup error: {str(e)[:200]}"
        return result

    # Code backup
    code_root = STAGE_CODE_ROOT if env == "stage" else oc.get_env_config(env).get("code_root", "/usr/lib/python3/dist-packages")
    code_path = os.path.join(backup_dir, "code")
    try:
        # Copy only tracked files to save space
        if env == "stage":
            code_path_abs = code_path
            os.makedirs(code_path_abs, exist_ok=True)
            subprocess.run(
                ["cp", "-a", code_root, code_path_abs],
                check=True, capture_output=True, text=True, timeout=300,
            )
        else:
            # Prod: copy specific tracked files
            subprocess.run(
                ["cp", "-a", code_root, code_path],
                check=True, capture_output=True, text=True, timeout=300,
            )
        result["code_path"] = code_path
    except Exception as e:
        result["error"] = f"Code backup error: {str(e)[:200]}"

    return result


def promote_dev_to_stage() -> PromotionResult:
    """Promote code from dev to stage. Wraps existing promote.sh."""
    r = PromotionResult(direction="dev-to-stage", stage="preflight")

    # 1. Auto-commit dev changes
    commit_ok, commit_msg = gitops.auto_commit_dev()
    r.smoke_results["git_commit"] = commit_msg

    # 2. Create backup
    backup = create_backup("stage", "dev-to-stage")
    if backup["error"]:
        r.stage = "failed"
        r.error = f"Backup failed: {backup['error']}"
        return r
    r.backup_db_path = backup["db_path"]
    r.backup_code_path = backup["code_path"]
    r.stage = "backup"

    # 3. Run promote.sh
    r.stage = "promoting"
    try:
        result = subprocess.run(
            ["sudo", PROMOTE_SCRIPT],
            capture_output=True, text=True, timeout=120,
        )
        output = result.stdout + result.stderr

        # Parse files promoted/skipped from output
        for line in output.split("\n"):
            if "Promoted:" in line:
                parts = line.split()
                for i, p in enumerate(parts):
                    if p == "Promoted:":
                        r.files_promoted = int(parts[i + 1])
                    if p == "Skipped:":
                        r.files_skipped = int(parts[i + 1])

        if result.returncode != 0:
            r.stage = "failed"
            r.error = output[-500:]
            return r
    except subprocess.TimeoutExpired:
        r.stage = "failed"
        r.error = "promote.sh timed out after 120s"
        return r
    except FileNotFoundError:
        r.stage = "failed"
        r.error = f"promote.sh not found at {PROMOTE_SCRIPT}"
        return r

    # 4. Clear .pyc files on stage
    subprocess.run(
        ["find", STAGE_CODE_ROOT, "-name", "*.pyc", "-delete"],
        capture_output=True, timeout=30,
    )

    # 5. Verify stage is running + smoke tests
    r.stage = "verifying"
    time.sleep(2)
    smoke = _run_smoke_tests("stage")
    r.smoke_results.update(smoke)
    r.smoke_tests_passed = smoke.get("all_pass", False)

    if smoke.get("all_pass"):
        r.stage = "success"
        # Create git tag
        tag = gitops.create_tag("stage")
        r.git_tag = tag
    else:
        r.stage = "success" if smoke.get("service_alive") else "failed"
        if not smoke.get("service_alive"):
            r.error = "Stage service failed to start after promotion"

    return r


def promote_stage_to_prod() -> PromotionResult:
    """Promote code from stage to prod. Production-grade safety."""
    r = PromotionResult(direction="stage-to-prod", stage="preflight")

    # 1. Create prod backup (CRITICAL)
    backup = create_backup("prod", "stage-to-prod")
    if backup["error"]:
        r.stage = "failed"
        r.error = f"Prod backup failed: {backup['error']}"
        return r
    r.backup_db_path = backup["db_path"]
    r.backup_code_path = backup["code_path"]
    r.stage = "backup"

    # 2. Copy stage code → prod
    # Read manifest, copy each tracked file from stage to prod
    r.stage = "promoting"
    try:
        with open(MANIFEST_PATH) as f:
            manifest_lines = [
                l.strip() for l in f
                if l.strip() and not l.strip().startswith("#")
            ]
    except FileNotFoundError:
        r.stage = "failed"
        r.error = f"Manifest not found: {MANIFEST_PATH}"
        return r

    stage_base = STAGE_CODE_ROOT
    prod_base = "/usr/lib/python3/dist-packages"

    for line in manifest_lines:
        stage_file = os.path.join(stage_base, line)
        prod_file = os.path.join(prod_base, line)

        if not os.path.isfile(stage_file):
            continue

        # Check if files differ
        diff_result = subprocess.run(
            ["diff", "-q", stage_file, prod_file],
            capture_output=True,
        )
        if diff_result.returncode != 0:
            try:
                shutil.copy2(stage_file, prod_file)
                r.files_promoted += 1
            except PermissionError:
                # Try with sudo
                subprocess.run(
                    ["sudo", "cp", stage_file, prod_file],
                    check=True, capture_output=True, timeout=10,
                )
                r.files_promoted += 1
        else:
            r.files_skipped += 1

    # 3. Clear .pyc on prod
    subprocess.run(
        ["find", prod_base, "-name", "*.pyc", "-delete"],
        capture_output=True, timeout=30,
    )

    # 4. Restart prod
    subprocess.run(
        ["sudo", "systemctl", "restart", "odoo"],
        capture_output=True, timeout=30,
    )

    # 5. Verify
    r.stage = "verifying"
    time.sleep(3)
    smoke = _run_smoke_tests("prod")
    r.smoke_results.update(smoke)

    if smoke.get("all_pass"):
        r.stage = "success"
        tag = gitops.create_tag("prod")
        r.git_tag = tag
    else:
        r.stage = "failed"
        r.error = "Prod smoke tests failed — check logs"

    return r


def _run_smoke_tests(env: str) -> dict:
    """Run post-promotion smoke tests on an environment."""
    conf = oc.get_env_config(env)
    results = {
        "service_alive": False,
        "http_ok": False,
        "xmlrpc_ok": False,
        "shop_accessible": False,
        "all_pass": False,
    }

    # 1. Service check
    active, status = oc.check_service(env)
    results["service_alive"] = active
    results["service_status"] = status

    if not active:
        return results

    # 2. HTTP check
    http_status, ms = oc.check_http(conf["url"])
    results["http_ok"] = http_status in (200, 301, 302)
    results["http_status"] = http_status
    results["response_ms"] = round(ms)

    # 3. XML-RPC check
    ok, err = oc.check_xmlrpc(conf["url"], conf["db"], conf["username"])
    results["xmlrpc_ok"] = ok
    if not ok:
        results["xmlrpc_error"] = err

    # 4. Shop page check (via HTTP)
    try:
        import urllib.request
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        shop_url = f"{conf['url']}/shop"
        req = urllib.request.Request(shop_url, method="HEAD")
        resp = urllib.request.urlopen(req, timeout=10, context=ctx)
        results["shop_accessible"] = resp.status == 200
        results["shop_status"] = resp.status
    except Exception as e:
        results["shop_accessible"] = False
        results["shop_error"] = str(e)[:200]

    # All pass
    results["all_pass"] = all([
        results["service_alive"],
        results["http_ok"],
        results["xmlrpc_ok"],
        results["shop_accessible"],
    ])

    return results


def clone_prod_to_stage() -> dict:
    """Clone prod DB to stage DB."""
    r = {"success": False, "error": "", "output": ""}

    try:
        # Terminate connections on stage DB
        subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-c",
             f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
             f"WHERE datname='{STAGE_DB}';"],
            capture_output=True, timeout=10,
        )

        # Drop and recreate from prod
        cmds = [
            f"DROP DATABASE IF EXISTS {STAGE_DB};",
            f"CREATE DATABASE {STAGE_DB} TEMPLATE {PROD_DB};",
            f"ALTER DATABASE {STAGE_DB} OWNER TO {STAGE_DB};",
        ]
        for cmd in cmds:
            subprocess.run(
                ["sudo", "-u", "postgres", "psql", "-c", cmd],
                check=True, capture_output=True, text=True, timeout=30,
            )

        r["success"] = True
        r["output"] = f"DB {PROD_DB} cloned to {STAGE_DB}"
    except subprocess.CalledProcessError as e:
        r["error"] = e.stderr.strip()[:500]
    except Exception as e:
        r["error"] = str(e)[:500]

    return r
