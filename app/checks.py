"""
Pre-flight check engine — runs all checks, scores results, blocks or warns.
"""

from dataclasses import dataclass, field
from typing import Optional
import subprocess
import os

from . import odoo_client as oc


@dataclass
class CheckResult:
    name: str
    label: str
    env: Optional[str]
    passed: bool
    critical: bool
    detail: str
    raw_output: str = ""


def run_all_checks() -> list[CheckResult]:
    """Run every pre-flight check. Returns list of CheckResults."""
    results: list[CheckResult] = []

    # --- 1-3. Odoo instance health ---
    for env in ("dev", "stage", "prod"):
        h = oc.full_health_check(env)
        results.append(CheckResult(
            name=f"odoo_{env}_health",
            label=f"Odoo {env.title()} Health",
            env=env,
            passed=h.reachable and h.xmlrpc_ok and h.db_ok,
            critical=True,
            detail=(
                f"HTTP {h.http_status} ({h.response_time_ms:.0f}ms), "
                f"XML-RPC {'OK' if h.xmlrpc_ok else 'FAIL'}, "
                f"DB {'OK' if h.db_ok else 'FAIL'}, "
                f"{h.modules_loaded} modules"
                if h.reachable else h.error or "Unreachable"
            ),
        ))

    # --- 4. Service status ---
    for env in ("dev", "stage"):
        active, status = oc.check_service(env)
        service_name = oc.ENVIRONMENTS[env]["service"]
        results.append(CheckResult(
            name=f"service_{env}",
            label=f"Service: {service_name}",
            env=env,
            passed=active,
            critical=True,
            detail=status,
        ))

    # --- 5. Disk space ---
    ok, detail, pct = oc.check_disk_space()
    results.append(CheckResult(
        name="disk_space",
        label="Disk Space",
        env=None,
        passed=ok,
        critical=True,
        detail=detail,
    ))

    # --- 6. File diff dev → stage ---
    diff = oc.get_file_diff()
    results.append(CheckResult(
        name="file_diff",
        label="Dev → Stage File Diff",
        env=None,
        passed=diff["count"] == 0,  # informational — passes even with diffs
        critical=False,
        detail=f"{diff['count']} files differ" if diff["count"]
               else "No differences",
        raw_output="\n".join(diff["diffs"][:50]),
    ))

    # --- 7. Git status ---
    git_ok, git_detail = _check_git_status()
    results.append(CheckResult(
        name="git_status",
        label="Git Status (dev)",
        env="dev",
        passed=git_ok,
        critical=False,
        detail=git_detail,
    ))

    # --- 8. CSP headers on stage ---
    for env in ("stage", "prod"):
        csp_ok, csp_detail = oc.check_csp_headers(oc.ENVIRONMENTS[env]["url"])
        results.append(CheckResult(
            name=f"csp_{env}",
            label=f"CSP Headers ({env})",
            env=env,
            passed=csp_ok,
            critical=False,  # warning only — won't block promotion
            detail=csp_detail,
        ))

    # --- 9. Custom addons exists ---
    addon_ok = os.path.isdir(oc.CUSTOM_ADDONS)
    results.append(CheckResult(
        name="custom_addons",
        label="Custom Addons Present",
        env="dev",
        passed=addon_ok,
        critical=True,
        detail=oc.CUSTOM_ADDONS if addon_ok
               else f"MISSING: {oc.CUSTOM_ADDONS}",
    ))

    # --- 10. Manifest file exists ---
    manifest_path = "/opt/odoo19stage/.promotions/manifest.txt"
    manifest_ok = os.path.isfile(manifest_path)
    results.append(CheckResult(
        name="manifest",
        label="Promotion Manifest",
        env="stage",
        passed=manifest_ok,
        critical=True,
        detail=manifest_path if manifest_ok else "MISSING",
    ))

    return results


def _check_git_status() -> tuple[bool, str]:
    """Check if dev custom addons have uncommitted changes."""
    git_dir = oc.CUSTOM_ADDONS
    if not os.path.isdir(os.path.join(git_dir, ".git")):
        return False, f"Not a git repo: {git_dir}"

    try:
        result = subprocess.run(
            ["git", "-C", git_dir, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        lines = [l for l in result.stdout.strip().split("\n") if l]
        if not lines:
            return True, "Clean — no uncommitted changes"
        return True, f"{len(lines)} uncommitted file(s) — will be auto-committed before promotion"
    except Exception as e:
        return False, str(e)


def score_checks(results: list[CheckResult]) -> dict:
    """Score check results. Returns {passed, failed, warnings, blocking, all_critical_pass}."""
    critical_fails = [r for r in results if r.critical and not r.passed]
    warnings = [r for r in results if not r.critical and not r.passed]
    passed = [r for r in results if r.passed]

    return {
        "total": len(results),
        "passed": len(passed),
        "failed": len(critical_fails),
        "warnings": len(warnings),
        "blocked": len(critical_fails) > 0,
        "all_critical_pass": len(critical_fails) == 0,
    }
