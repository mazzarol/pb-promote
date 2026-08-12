"""
Odoo XML-RPC health check client.
Pings each environment, authenticates, and returns status.
"""

import xmlrpc.client
import urllib.request
import ssl
import json
import subprocess
import socket
from dataclasses import dataclass, field
from typing import Optional

# Global socket timeout — prevents XML-RPC calls from hanging the worker
# (xmlrpc.client.ServerProxy has NO default timeout; a slow/hung Odoo endpoint
#  blocks the request forever and, with few workers, the whole dashboard).
socket.setdefaulttimeout(10)


# --- Environment config ---
ENVIRONMENTS = {
    "dev": {
        "url": "https://dev.priorityblinds.com.au",
        "internal_url": "http://127.0.0.1:8073",
        "db": "odoo19dev",
        "username": "pb-promote",
        "port": 8073,
        "code_root": "/usr/lib/python3/dist-packages",
        "service": "odoo19dev",
    },
    "stage": {
        "url": "https://stage.priorityblinds.com.au",
        "internal_url": "http://127.0.0.1:8074",
        "db": "odoo19stage",
        "username": "pb-promote",
        "port": 8074,
        "code_root": "/opt/odoo19stage",
        "service": "odoo19stage",
    },
    "prod": {
        "url": "https://priorityblinds.com.au",
        "internal_url": "http://127.0.0.1:8075",
        "db": "odoo19prod",
        "username": "pb-promote",
        "port": 8075,
        "code_root": "/usr/lib/python3/dist-packages",
        "service": "odoo19prod",
    },
}

# Paths that matter for promotion — relative to code_root
TRACKED_PATHS = [
    "odoo/http.py",
    "odoo/addons/website_sale/",
    "odoo/addons/website/",
    "odoo/addons/web/",
]

CUSTOM_ADDONS = "/opt/odoo19dev/custom-addons/priority_blinds"

API_KEY = None  # Legacy — use get_env_api_key() instead


def get_env_api_key(env: str) -> str:
    """Get the API key for a specific environment. Falls through: DB per-env → DB global → env var → file."""
    try:
        from app.database import SessionLocal
        from app.models import load_api_key as db_load_api_key
        db = SessionLocal()
        try:
            key = db_load_api_key(db, env)
            if key:
                return key
        finally:
            db.close()
    except Exception:
        pass
    # Fall back to legacy global API_KEY (env var or file)
    import os
    global API_KEY
    if API_KEY:
        return API_KEY
    key = os.environ.get("ODOO_API_KEY", "")
    if key:
        API_KEY = key
        return key
    try:
        with open("/opt/pb-promote/odoo_api_key.txt") as f:
            key = f.read().strip()
            API_KEY = key
            return key
    except FileNotFoundError:
        pass
    return ""


def get_env_config(env: str) -> dict:
    """Get environment config, preferring DB over hardcoded defaults."""
    try:
        from app.database import SessionLocal
        from app.models import load_env_config
        db = SessionLocal()
        try:
            db_config = load_env_config(db, env)
            # Merge with hardcoded as fallback for any missing keys
            merged = dict(ENVIRONMENTS.get(env, {}))
            merged.update({k: v for k, v in db_config.items() if v})
            return merged
        finally:
            db.close()
    except Exception:
        pass
    return ENVIRONMENTS.get(env, {})


@dataclass
class OdooHealth:
    env: str
    reachable: bool = False
    xmlrpc_ok: bool = False
    auth_ok: bool = False
    db_ok: bool = False
    modules_loaded: int = 0
    error: Optional[str] = None
    http_status: int = 0
    response_time_ms: float = 0.0


def check_http(url: str, timeout: int = 10) -> tuple[int, float]:
    """Check HTTP reachability, return (status_code, response_time_ms)."""
    import time

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    start = time.monotonic()
    try:
        req = urllib.request.Request(url, method="HEAD")
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        elapsed = (time.monotonic() - start) * 1000
        return resp.status, elapsed
    except Exception:
        elapsed = (time.monotonic() - start) * 1000
        return 0, elapsed


def check_xmlrpc(env: str, url: str, db: str, username: str) -> tuple[bool, str]:
    """Try XML-RPC auth. Returns (ok, error_message)."""
    api_key = get_env_api_key(env)
    if not api_key:
        return False, "No API key configured"

    try:
        common = xmlrpc.client.ServerProxy(
            f"{url}/xmlrpc/2/common", allow_none=True
        )
        uid = common.authenticate(db, username, api_key, {})
        if uid and uid > 0:
            return True, ""
        return False, f"Authentication returned uid={uid}"
    except Exception as e:
        return False, str(e)[:200]


def check_db(env: str) -> tuple[bool, str]:
    """Check PostgreSQL connectivity for an environment."""
    conf = get_env_config(env)
    db_name = conf["db"]
    try:
        result = subprocess.run(
            [
                "sudo", "-u", "postgres", "psql",
                "-d", db_name, "-c", "SELECT 1;",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and "1 row" in result.stdout:
            return True, ""
        return False, result.stderr.strip() or "No rows returned"
    except FileNotFoundError:
        return False, "psql not found"
    except subprocess.TimeoutExpired:
        return False, "Connection timed out"
    except Exception as e:
        return False, str(e)[:200]


def check_service(env: str) -> tuple[bool, str]:
    """Check if the systemd service is active."""
    conf = get_env_config(env)
    service = conf.get("service", "odoo")
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=5,
        )
        active = result.returncode == 0
        return active, result.stdout.strip()
    except Exception as e:
        return False, str(e)


def check_modules(env: str, url: str, db: str, username: str) -> tuple[int, str]:
    """Count installed modules via XML-RPC. Returns (count, error)."""
    api_key = get_env_api_key(env)
    if not api_key:
        return 0, "No API key"

    try:
        common = xmlrpc.client.ServerProxy(
            f"{url}/xmlrpc/2/common", allow_none=True
        )
        uid = common.authenticate(db, username, api_key, {})
        models = xmlrpc.client.ServerProxy(
            f"{url}/xmlrpc/2/object", allow_none=True
        )
        count = models.execute_kw(
            db, uid, api_key,
            "ir.module.module", "search_count",
            [[["state", "=", "installed"]]],
        )
        return count, ""
    except Exception as e:
        return 0, str(e)[:200]


def full_health_check(env: str) -> OdooHealth:
    """Run all health checks for an environment."""
    conf = get_env_config(env)
    h = OdooHealth(env=env)

    # XML-RPC / module checks use the INTERNAL url (127.0.0.1:port) — the
    # public url routes through nginx/LiteSpeed which may 404 on /xmlrpc/
    # (prod's LiteSpeed edge proxy doesn't pass /xmlrpc/2/* through).
    xmlrpc_url = conf.get("internal_url", conf["url"])

    # HTTP check (public reachability)
    h.http_status, h.response_time_ms = check_http(conf["url"], timeout=10)
    h.reachable = h.http_status in (200, 301, 302, 303, 307, 308)

    if not h.reachable:
        h.error = f"HTTP {h.http_status or 'unreachable'}"
        return h

    # XML-RPC auth
    h.xmlrpc_ok, err = check_xmlrpc(env, xmlrpc_url, conf["db"], conf["username"])
    h.auth_ok = h.xmlrpc_ok
    if not h.xmlrpc_ok:
        h.error = err
        return h

    # DB check
    h.db_ok, db_err = check_db(env)
    if not h.db_ok:
        h.error = f"DB: {db_err}"
        return h

    # Module count
    h.modules_loaded, _ = check_modules(
        env, xmlrpc_url, conf["db"], conf["username"]
    )

    return h


def get_file_diff() -> dict:
    """Show which tracked files differ between dev and stage code roots."""
    import os

    diffs = []
    dev_base = get_env_config("dev").get("code_root", "/usr/lib/python3/dist-packages")
    stage_base = get_env_config("stage").get("code_root", "/opt/odoo19stage")

    for path in TRACKED_PATHS:
        dev_path = os.path.join(dev_base, path)
        stage_path = os.path.join(stage_base, path)

        if os.path.isdir(dev_path) and os.path.isdir(stage_path):
            # Directory — compare with diff -rq
            try:
                result = subprocess.run(
                    ["diff", "-rq", dev_path, stage_path],
                    capture_output=True, text=True, timeout=30,
                )
                for line in result.stdout.strip().split("\n"):
                    if line:
                        diffs.append(line)
            except Exception:
                pass
        elif os.path.isfile(dev_path) and os.path.isfile(stage_path):
            try:
                result = subprocess.run(
                    ["diff", "-q", dev_path, stage_path],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode != 0:
                    diffs.append(f"Files {dev_path} and {stage_path} differ")
            except Exception:
                pass
        elif os.path.isfile(dev_path):
            diffs.append(f"Only in dev: {dev_path}")
        elif os.path.isfile(stage_path):
            diffs.append(f"Only in stage: {stage_path}")

    return {
        "count": len(diffs),
        "diffs": diffs,
        "dev_base": dev_base,
        "stage_base": stage_base,
    }


def check_disk_space() -> tuple[bool, str, int]:
    """Check disk space. Returns (ok, detail, percent_free)."""
    import shutil

    usage = shutil.disk_usage("/")
    percent_free = int((usage.free / usage.total) * 100)
    ok = percent_free >= 20
    detail = (
        f"Free: {usage.free // (1024**3)}GB / "
        f"Total: {usage.total // (1024**3)}GB "
        f"({percent_free}%)"
    )
    return ok, detail, percent_free


def check_csp_headers(url: str) -> tuple[bool, str]:
    """Verify CSP fix is applied (no default-src 'none' on images)."""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=10, context=ctx)
        csp = resp.headers.get("Content-Security-Policy", "")
        if "default-src 'none'" in csp:
            return False, f"CSP still blocks images: {csp}"
        return True, "CSP clean" if not csp else f"CSP OK: {csp[:100]}"
    except Exception as e:
        return False, str(e)
