"""
PB-Promote Monitor — Email gateway, SMS gateway, Odoo issues, and system health.
Queries Odoo XML-RPC for email/SMS activity and system tools for logs/resources.
"""
import subprocess
import os
import json
import time
from dataclasses import dataclass, field
from typing import Optional, List


# ── Data Classes ──────────────────────────────────────────────────────

@dataclass
class EmailGatewayStatus:
    """Postfix + Odoo email gateway health."""
    postfix_active: bool = False
    postfix_status: str = "unknown"
    mail_queue_size: int = 0
    mail_queue_deferred: int = 0
    recent_deliveries: int = 0
    recent_bounces: int = 0
    odoo_mail_servers: list = field(default_factory=list)
    recent_log_entries: list = field(default_factory=list)
    error: str = ""


@dataclass
class SmsGatewayStatus:
    """SMS gateway health — Docker containers, mock services, and Android SMS Gateway."""
    configured: bool = False
    error: str = ""
    # Docker containers
    docker_ok: bool = False
    containers: list = field(default_factory=list)
    # Mock SMS (dev + stage)
    mock_dev: dict = field(default_factory=dict)
    mock_stage: dict = field(default_factory=dict)
    # Real SMS Gateway (prod)
    gateway_server: dict = field(default_factory=dict)
    gateway_worker: dict = field(default_factory=dict)
    gateway_db: dict = field(default_factory=dict)


@dataclass
class OdooErrorLog:
    """Aggregated Odoo errors from journalctl."""
    env: str
    error_count: int = 0
    recent_errors: list = field(default_factory=list)
    last_error_time: str = ""


@dataclass
class SystemHealth:
    """VPS resource health."""
    uptime: str = ""
    load_avg: str = ""
    mem_total: str = ""
    mem_used: str = ""
    mem_pct: float = 0.0
    disk_pct: float = 0.0
    disk_free: str = ""
    cpu_temp: str = ""


@dataclass
class MonitorSnapshot:
    """Complete monitoring snapshot."""
    timestamp: str = ""
    email: EmailGatewayStatus = field(default_factory=EmailGatewayStatus)
    sms: SmsGatewayStatus = field(default_factory=SmsGatewayStatus)
    odoo_errors: dict = field(default_factory=dict)  # {env: OdooErrorLog}
    system: SystemHealth = field(default_factory=SystemHealth)
    env_health: dict = field(default_factory=dict)  # Basic reachability per env


# ── Email Gateway ─────────────────────────────────────────────────────

def _run(cmd: list, timeout: int = 15) -> tuple[str, str, int]:
    """Run a command, return (stdout, stderr, returncode)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", f"timeout after {timeout}s", -1
    except FileNotFoundError:
        return "", "not found", -2
    except Exception as e:
        return "", str(e), -3


def check_postfix() -> tuple[bool, str]:
    """Check if Postfix service is active."""
    out, _, rc = _run(["systemctl", "is-active", "postfix"], timeout=5)
    return rc == 0, out


def check_mail_queue() -> tuple[int, int]:
    """Return (total_queued, deferred_count) from postqueue/mailq."""
    # Try postqueue first (simpler output)
    out, _, rc = _run(["postqueue", "-p"], timeout=10)
    if rc != 0 or not out:
        # Fall back to mailq
        out, _, rc = _run(["mailq"], timeout=10)

    if rc != 0 or "Mail queue is empty" in out:
        return 0, 0

    lines = out.strip().split("\n")
    # Last line is usually "-- N Kbytes in M Requests." or similar
    total = 0
    deferred = 0
    for line in lines:
        if line and line[0] in "0123456789ABCDEF" and len(line) > 10:
            total += 1
        if "deferred" in line.lower():
            deferred += 1

    # Also parse the summary line if present
    for line in lines:
        if "Requests" in line and "--" in line:
            try:
                parts = line.split()
                total = int(parts[-2]) if len(parts) >= 2 else total
            except (ValueError, IndexError):
                pass

    # Count deferred entries
    deferred = out.count("(connect to") + out.count("(delivery temporarily")

    return total, deferred


def check_mail_log(minutes: int = 60) -> list:
    """Return recent /var/log/mail.log entries as structured dicts."""
    entries = []
    log_path = "/var/log/mail.log"
    if not os.path.exists(log_path):
        return entries

    try:
        out, _, _ = _run(
            ["tail", "-200", log_path],
            timeout=5,
        )
        for line in out.split("\n"):
            line = line.strip()
            if not line:
                continue
            if "status=sent" in line:
                entries.append({"type": "sent", "line": line[-200:]})
            elif "status=bounced" in line:
                entries.append({"type": "bounce", "line": line[-200:]})
            elif "status=deferred" in line:
                entries.append({"type": "deferred", "line": line[-200:]})
            elif "connect to" in line and ("Connection refused" in line or "Connection timed out" in line):
                entries.append({"type": "error", "line": line[-200:]})
    except Exception:
        pass

    # Return last 15 entries, newest last
    return entries[-15:]


def check_odoo_email_activity(env: str, url: str, db: str, username: str, api_key: str) -> dict:
    """Query Odoo for recent email activity via XML-RPC."""
    import xmlrpc.client
    import socket

    # Set socket timeout to prevent hanging (default is no timeout)
    socket.setdefaulttimeout(10)

    result = {
        "recent_sent": 0,
        "recent_failed": 0,
        "mail_servers": [],
        "error": "",
    }

    if not api_key:
        result["error"] = "No API key"
        return result

    try:
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
        uid = common.authenticate(db, username, api_key, {})
        if not uid:
            result["error"] = "Auth failed"
            return result

        models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)

        # Recent sent emails (last 24h)
        try:
            sent_count = models.execute_kw(
                db, uid, api_key,
                "mail.mail", "search_count",
                [[["state", "=", "sent"]]],
            )
            result["recent_sent"] = sent_count
        except Exception:
            pass

        # Recent failed/exception emails
        try:
            failed_ids = models.execute_kw(
                db, uid, api_key,
                "mail.mail", "search",
                [[["state", "=", "exception"]]],
                {"limit": 20},
            )
            result["recent_failed"] = len(failed_ids)
            if failed_ids:
                failed_data = models.execute_kw(
                    db, uid, api_key,
                    "mail.mail", "read",
                    [failed_ids[:5]],
                    {"fields": ["subject", "email_to", "failure_reason", "create_date"]},
                )
                result["failed_samples"] = [
                    {
                        "subject": m.get("subject", "")[:80],
                        "to": m.get("email_to", "")[:60],
                        "reason": (m.get("failure_reason", "") or "")[:120],
                        "date": m.get("create_date", ""),
                    }
                    for m in failed_data
                ]
        except Exception:
            pass

        # Mail server configs
        try:
            server_ids = models.execute_kw(
                db, uid, api_key,
                "ir.mail_server", "search",
                [[]],
            )
            if server_ids:
                servers = models.execute_kw(
                    db, uid, api_key,
                    "ir.mail_server", "read",
                    [server_ids],
                    {"fields": ["name", "smtp_host", "smtp_port", "smtp_user", "active"]},
                )
                result["mail_servers"] = [
                    {
                        "name": s.get("name", ""),
                        "host": s.get("smtp_host", ""),
                        "port": s.get("smtp_port", 0),
                        "user": (s.get("smtp_user", "") or "")[:30],
                        "active": s.get("active", False),
                    }
                    for s in servers
                ]
        except Exception:
            pass

    except Exception as e:
        result["error"] = str(e)[:200]

    return result


# ── SMS Gateway (Docker) ──────────────────────────────────────────────

SMS_CONTAINERS = [
    {"name": "sms-mock-dev", "label": "SMS Mock (Dev)", "port": 3098, "health_url": "http://127.0.0.1:3098/health", "env": "dev"},
    {"name": "sms-mock-stage", "label": "SMS Mock (Stage)", "port": 3099, "health_url": "http://127.0.0.1:3099/health", "env": "stage"},
    {"name": "sms-gateway-server-1", "label": "SMS Gateway Server (Prod)", "port": 3000, "env": "prod"},
    {"name": "sms-gateway-worker-1", "label": "SMS Gateway Worker", "env": "prod"},
    {"name": "sms-gateway-db-1", "label": "SMS Gateway DB (MariaDB)", "env": "prod"},
]


def _docker_ps() -> dict:
    """Parse docker ps into a dict of container_name -> status info."""
    out, _, rc = _run(
        ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}"],
        timeout=10,
    )
    result = {}
    if rc != 0 or not out:
        return result
    for line in out.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            name = parts[0]
            status = parts[1]
            image = parts[2]
            ports = parts[3] if len(parts) > 3 else ""
            result[name] = {"status": status, "image": image, "ports": ports}
    return result


def _http_get_json(url: str, timeout: int = 5) -> tuple[bool, dict, str]:
    """GET a JSON endpoint, return (ok, data_dict, error)."""
    import urllib.request
    import ssl
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        data = json.loads(resp.read().decode())
        return True, data, ""
    except Exception as e:
        return False, {}, str(e)[:150]


def check_sms_docker() -> SmsGatewayStatus:
    """Check all SMS Docker containers and mock/gateway health endpoints."""
    status = SmsGatewayStatus()

    containers = _docker_ps()
    if not containers:
        status.error = "Docker not accessible or no containers"
        return status

    status.docker_ok = True
    status.configured = True  # We found SMS containers

    for cfg in SMS_CONTAINERS:
        name = cfg["name"]
        docker_info = containers.get(name)
        info = {
            "name": name,
            "label": cfg["label"],
            "env": cfg["env"],
            "running": docker_info is not None,
            "status": docker_info["status"] if docker_info else "not found",
            "image": docker_info["image"] if docker_info else "",
            "ports": docker_info["ports"] if docker_info else "",
        }

        # Health check for mock services
        if "health_url" in cfg:
            ok, data, err = _http_get_json(cfg["health_url"])
            info["health_ok"] = ok
            info["health_data"] = data
            info["health_error"] = err

        status.containers.append(info)

        # Populate convenience slots
        if name == "sms-mock-dev":
            status.mock_dev = info
        elif name == "sms-mock-stage":
            status.mock_stage = info
        elif name == "sms-gateway-server-1":
            status.gateway_server = info
            # Additional: check if gateway web UI is reachable
            import urllib.request, ssl
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                req = urllib.request.Request("http://127.0.0.1:3000/")
                resp = urllib.request.urlopen(req, timeout=5, context=ctx)
                info["web_ui_ok"] = resp.status == 200
            except Exception:
                info["web_ui_ok"] = False
        elif name == "sms-gateway-worker-1":
            status.gateway_worker = info
        elif name == "sms-gateway-db-1":
            status.gateway_db = info

    return status


# ── Odoo Error Logs ───────────────────────────────────────────────────

def check_odoo_errors(env: str, service: str, hours: int = 2) -> OdooErrorLog:
    """Query journalctl for recent Odoo errors."""
    log = OdooErrorLog(env=env)

    out, _, _ = _run(
        ["journalctl", "-u", service, "--since", f"{hours}h ago", "-p", "err", "--no-pager"],
        timeout=15,
    )

    if not out:
        return log

    lines = [l for l in out.split("\n") if l.strip()]
    log.error_count = len(lines)

    # Extract last 10 unique-ish errors
    seen = set()
    for line in reversed(lines):
        # Try to get a meaningful snippet
        snippet = line[-200:]
        # Skip timestamps and repetitive prefixes
        if len(snippet) > 30 and snippet not in seen:
            seen.add(snippet)
            log.recent_errors.append(snippet)
        if len(log.recent_errors) >= 10:
            break

    # Reverse to show newest last
    log.recent_errors = list(reversed(log.recent_errors))

    # Get timestamp of last error
    if lines:
        last = lines[-1]
        # journalctl lines start with date/time like "Aug 12 14:30:25"
        try:
            log.last_error_time = " ".join(last.split()[:3])
        except Exception:
            log.last_error_time = "unknown"

    return log


def check_odoo_log_files() -> dict:
    """Check Odoo log files for recent errors (alternative to journalctl)."""
    results = {}
    for env in ("dev", "stage", "prod"):
        log_path = f"/var/log/odoo19{env}/odoo19{env}.log"
        if os.path.exists(log_path):
            try:
                out, _, _ = _run(
                    ["tail", "-500", log_path],
                    timeout=5,
                )
                error_lines = [l for l in out.split("\n") if "ERROR" in l or "CRITICAL" in l or "Traceback" in l]
                results[env] = {
                    "error_count": len(error_lines),
                    "recent": error_lines[-5:] if error_lines else [],
                }
            except Exception:
                results[env] = {"error_count": 0, "recent": [], "error": "Could not read log"}
        else:
            results[env] = {"error_count": 0, "recent": [], "note": "No log file"}
    return results


# ── System Health ──────────────────────────────────────────────────────

def check_system_health() -> SystemHealth:
    """Get basic VPS health metrics."""
    h = SystemHealth()

    # Uptime
    out, _, _ = _run(["uptime", "-p"], timeout=5)
    if out:
        h.uptime = out

    # Load average
    out, _, _ = _run(["uptime"], timeout=5)
    if out:
        # " 12:34:56 up 10 days,  2:30,  3 users,  load average: 0.15, 0.10, 0.05"
        try:
            h.load_avg = out.split("load average:")[-1].strip()
        except Exception:
            pass

    # Memory
    out, _, _ = _run(["free", "-h"], timeout=5)
    if out:
        for line in out.split("\n"):
            if "Mem:" in line:
                parts = line.split()
                if len(parts) >= 4:
                    h.mem_total = parts[1]
                    h.mem_used = parts[2]
                    try:
                        h.mem_pct = (float(parts[2].replace("Gi", "").replace("G", ""))
                                     / float(parts[1].replace("Gi", "").replace("G", ""))) * 100
                    except Exception:
                        pass

    # Disk
    out, _, _ = _run(["df", "-h", "/"], timeout=5)
    if out:
        for line in out.split("\n"):
            if line.endswith("/") or " / " in line:
                parts = line.split()
                if len(parts) >= 5:
                    try:
                        pct = int(parts[4].replace("%", ""))
                        h.disk_pct = pct
                        h.disk_free = parts[3]
                    except Exception:
                        pass

    return h


# ── Full Snapshot ─────────────────────────────────────────────────────

def collect_full_snapshot(db_session=None) -> MonitorSnapshot:
    """Collect all monitoring data into a single snapshot."""
    import socket
    from . import odoo_client as oc

    # Prevent any network call from hanging the worker
    socket.setdefaulttimeout(10)

    snap = MonitorSnapshot()
    snap.timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    start = time.time()
    max_total = 30  # Never take more than 30s total

    def _elapsed() -> float:
        return time.time() - start

    # ── Email gateway ──
    email = EmailGatewayStatus()
    try:
        email.postfix_active, email.postfix_status = check_postfix()
        email.mail_queue_size, email.mail_queue_deferred = check_mail_queue()
        email.recent_log_entries = check_mail_log()
        email.recent_deliveries = sum(1 for e in email.recent_log_entries if e["type"] == "sent")
        email.recent_bounces = sum(1 for e in email.recent_log_entries if e["type"] == "bounce")
    except Exception as e:
        email.error = f"Email check failed: {str(e)[:100]}"

    # Odoo email activity (query dev as canonical) — skip if running out of time
    if _elapsed() < max_total * 0.5:
        try:
            dev_cfg = oc.get_env_config("dev")
            api_key = oc.get_env_api_key("dev")
            odoo_email = check_odoo_email_activity(
                "dev", dev_cfg["url"], dev_cfg["db"], dev_cfg["username"], api_key
            )
            email.odoo_mail_servers = odoo_email.get("mail_servers", [])
            if not email.error:
                email.error = odoo_email.get("error", "")
        except Exception as e:
            if not email.error:
                email.error = f"Odoo email query failed: {str(e)[:100]}"

    snap.email = email

    # ── SMS gateway ──
    try:
        snap.sms = check_sms_docker()
    except Exception as e:
        snap.sms = SmsGatewayStatus(error=f"SMS check failed: {str(e)[:100]}")
        snap.sms.configured = False

    # ── Odoo errors ──
    if _elapsed() < max_total * 0.7:
        try:
            for env in ("dev", "stage", "prod"):
                cfg = oc.get_env_config(env)
                service = cfg.get("service", f"odoo19{env}")
                snap.odoo_errors[env] = check_odoo_errors(env, service)
        except Exception:
            pass

    # ── System ──
    try:
        snap.system = check_system_health()
    except Exception:
        pass

    # ── Env health (quick) — skip if almost out of time
    if _elapsed() < max_total * 0.85:
        try:
            for env in ("dev", "stage", "prod"):
                h = oc.full_health_check(env)
                snap.env_health[env] = {
                    "reachable": h.reachable,
                    "auth_ok": h.auth_ok,
                    "db_ok": h.db_ok,
                    "http_status": h.http_status,
                    "response_ms": round(h.response_time_ms),
                    "error": h.error,
                }
        except Exception:
            pass

    return snap
