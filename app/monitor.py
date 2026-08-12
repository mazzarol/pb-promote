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
    """SMS gateway health (via Odoo ir_mail_server or custom module)."""
    configured: bool = False
    server_name: str = ""
    smtp_host: str = ""
    recent_sms_count: int = 0
    recent_sms_log: list = field(default_factory=list)
    error: str = ""


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
        return "", "timeout", -1
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


def check_sms_gateway(env: str, url: str, db: str, username: str, api_key: str) -> SmsGatewayStatus:
    """Check SMS gateway configuration via Odoo XML-RPC."""
    import xmlrpc.client

    status = SmsGatewayStatus()

    if not api_key:
        status.error = "No API key"
        return status

    try:
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
        uid = common.authenticate(db, username, api_key, {})
        if not uid:
            status.error = "Auth failed"
            return status

        models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)

        # Look for SMS mail servers
        sms_server_ids = models.execute_kw(
            db, uid, api_key,
            "ir.mail_server", "search",
            [[["name", "ilike", "%sms%"]]],
        )

        if not sms_server_ids:
            # Try looking for SMS by SMTP host
            sms_server_ids = models.execute_kw(
                db, uid, api_key,
                "ir.mail_server", "search",
                [[["smtp_host", "ilike", "%sms%"]]],
            )

        if sms_server_ids:
            servers = models.execute_kw(
                db, uid, api_key,
                "ir.mail_server", "read",
                [sms_server_ids],
                {"fields": ["name", "smtp_host", "smtp_port", "smtp_user", "active"]},
            )
            if servers:
                s = servers[0]
                status.configured = True
                status.server_name = s.get("name", "")
                status.smtp_host = s.get("smtp_host", "")
                status.smtp_port = s.get("smtp_port", 0)
                status.smtp_active = s.get("active", False)

        # Check for SMS-related modules
        sms_module_ids = models.execute_kw(
            db, uid, api_key,
            "ir.module.module", "search",
            [[["name", "ilike", "%sms%"], ["state", "=", "installed"]]],
        )
        if sms_module_ids:
            sms_modules = models.execute_kw(
                db, uid, api_key,
                "ir.module.module", "read",
                [sms_module_ids],
                {"fields": ["name", "shortdesc"]},
            )
            status.sms_modules = [
                {"name": m.get("name", ""), "desc": m.get("shortdesc", "")}
                for m in sms_modules
            ]

        # Check for recent SMS mail.mail records
        if sms_server_ids or sms_module_ids:
            sms_mail_ids = models.execute_kw(
                db, uid, api_key,
                "mail.mail", "search",
                [[["mail_server_id", "in", sms_server_ids]]] if sms_server_ids else [[]],
                {"order": "create_date desc", "limit": 10},
            )
            status.recent_sms_count = len(sms_mail_ids)

            if sms_mail_ids:
                sms_data = models.execute_kw(
                    db, uid, api_key,
                    "mail.mail", "read",
                    [sms_mail_ids[:5]],
                    {"fields": ["subject", "state", "create_date", "email_to"]},
                )
                status.recent_sms_log = [
                    {
                        "subject": m.get("subject", "")[:60],
                        "state": m.get("state", ""),
                        "to": m.get("email_to", "")[:40],
                        "date": m.get("create_date", ""),
                    }
                    for m in sms_data
                ]

    except Exception as e:
        status.error = str(e)[:200]

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
    from . import odoo_client as oc

    snap = MonitorSnapshot()
    snap.timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    # ── Email gateway ──
    email = EmailGatewayStatus()
    email.postfix_active, email.postfix_status = check_postfix()
    email.mail_queue_size, email.mail_queue_deferred = check_mail_queue()
    email.recent_log_entries = check_mail_log()

    # Aggregate delivery/bounce counts from log entries
    email.recent_deliveries = sum(1 for e in email.recent_log_entries if e["type"] == "sent")
    email.recent_bounces = sum(1 for e in email.recent_log_entries if e["type"] == "bounce")

    # Odoo email activity (query dev as canonical)
    dev_cfg = oc.get_env_config("dev")
    api_key = oc.get_env_api_key("dev")
    odoo_email = check_odoo_email_activity(
        "dev", dev_cfg["url"], dev_cfg["db"], dev_cfg["username"], api_key
    )
    email.odoo_mail_servers = odoo_email.get("mail_servers", [])
    email.error = odoo_email.get("error", "")

    snap.email = email

    # ── SMS gateway ──
    snap.sms = check_sms_gateway(
        "dev", dev_cfg["url"], dev_cfg["db"], dev_cfg["username"], api_key
    )

    # ── Odoo errors ──
    for env in ("dev", "stage", "prod"):
        cfg = oc.get_env_config(env)
        service = cfg.get("service", f"odoo19{env}")
        snap.odoo_errors[env] = check_odoo_errors(env, service)

    # ── System ──
    snap.system = check_system_health()

    # ── Env health (quick) ──
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

    return snap
