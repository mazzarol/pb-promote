"""
PB-Promote — Odoo 19 CI/CD Pipeline Web Dashboard
FastAPI application with Jinja2 templates.
"""

import json
import os
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from .database import init_db, get_db, engine
from .models import Base, Promotion, Rollback, Check, Backup
from . import odoo_client as oc
from . import checks as check_engine
from . import promote as promote_engine
from . import rollback as rollback_engine
from . import gitops


# --- App Factory ---
def create_app() -> FastAPI:
    app = FastAPI(
        title="PB-Promote",
        description="Priority Blinds Odoo 19 CI/CD Pipeline",
        version="1.0.0",
    )

    # Templates — use Jinja2 directly (Starlette Jinja2Templates broken with Jinja2 3.1.4+)
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    templates_dir = "/opt/pb-promote/app/templates"
    jinja_env = Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=select_autoescape(["html"]),
    )

    def render_template(name: str, context: dict) -> HTMLResponse:
        """Render a Jinja2 template directly, bypassing Starlette wrapper."""
        template = jinja_env.get_template(name)
        return HTMLResponse(template.render(**context))

    # Context helpers
    def base_context(request: Request) -> dict:
        return {
            "request": request,
            "now": datetime.now(timezone.utc).isoformat(),
            "app_title": "PB-Promote",
            "subtitle": "Odoo 19 CI/CD Pipeline",
        }

    # --- Pages ---

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, db: Session = Depends(get_db)):
        ctx = base_context(request)
        ctx["active_page"] = "dashboard"

        # Load environment statuses
        env_status = {}
        for env in ("dev", "stage", "prod"):
            h = oc.full_health_check(env)
            service_ok, service_status = oc.check_service(env)
            env_status[env] = {
                "reachable": h.reachable,
                "auth_ok": h.auth_ok,
                "db_ok": h.db_ok,
                "http_status": h.http_status,
                "response_ms": round(h.response_time_ms),
                "modules": h.modules_loaded,
                "service_active": service_ok,
                "service_status": service_status,
                "error": h.error,
                "config": oc.ENVIRONMENTS[env],
            }
        ctx["env_status"] = env_status

        # Recent promotions
        recent = (
            db.query(Promotion)
            .order_by(Promotion.id.desc())
            .limit(10)
            .all()
        )
        ctx["recent_promotions"] = [
            {
                "id": p.id,
                "direction": p.direction,
                "status": p.status,
                "files": f"{p.files_promoted} promoted, {p.files_skipped} skipped",
                "tag": p.git_tag or "-",
                "started": p.started_at,
                "error": p.error_message,
            }
            for p in recent
        ]

        # Last check results
        last_checks = (
            db.query(Check)
            .order_by(Check.id.desc())
            .limit(20)
            .all()
        )
        ctx["last_checks"] = [
            {
                "name": c.check_name,
                "env": c.environment,
                "passed": c.passed,
                "critical": c.critical,
                "detail": c.detail,
            }
            for c in last_checks
        ]

        # Git status
        if gitops.is_git_repo():
            ctx["git_status"] = gitops.get_status()
        else:
            ctx["git_status"] = {"is_repo": False}

        return render_template("dashboard.html", ctx)

    @app.get("/promote", response_class=HTMLResponse)
    async def promote_page(request: Request, db: Session = Depends(get_db)):
        ctx = base_context(request)
        ctx["active_page"] = "promote"

        # Run checks for display
        check_results = check_engine.run_all_checks()
        score = check_engine.score_checks(check_results)
        ctx["check_results"] = check_results
        ctx["score"] = score

        # File diff
        ctx["diff"] = oc.get_file_diff()

        # Git status
        if gitops.is_git_repo():
            ctx["git_status"] = gitops.get_status()
        else:
            ctx["git_status"] = {"is_repo": False}

        return render_template("promote.html", ctx)

    @app.get("/rollback", response_class=HTMLResponse)
    async def rollback_page(request: Request, db: Session = Depends(get_db)):
        ctx = base_context(request)
        ctx["active_page"] = "rollback"

        # List available backups
        if os.path.isdir(promote_engine.BACKUP_ROOT):
            backups = []
            for entry in sorted(
                os.listdir(promote_engine.BACKUP_ROOT), reverse=True
            )[:20]:
                full = os.path.join(promote_engine.BACKUP_ROOT, entry)
                if os.path.isdir(full):
                    contents = os.listdir(full)
                    backups.append({
                        "name": entry,
                        "files": contents,
                        "path": full,
                    })
            ctx["backups"] = backups
        else:
            ctx["backups"] = []

        # Recent promotions (rollback targets)
        recent = (
            db.query(Promotion)
            .filter(Promotion.rollback_available == True)
            .order_by(Promotion.id.desc())
            .limit(10)
            .all()
        )
        ctx["rollback_targets"] = [
            {
                "id": p.id,
                "direction": p.direction,
                "status": p.status,
                "tag": p.git_tag or "-",
                "started": p.started_at,
                "backup_db": p.backup_db_path,
                "backup_code": p.backup_code_path,
            }
            for p in recent
        ]

        # Git tags
        if gitops.is_git_repo():
            ctx["git_tags"] = gitops.get_tags(20)
        else:
            ctx["git_tags"] = []

        return render_template("rollback.html", ctx)

    @app.get("/checks", response_class=HTMLResponse)
    async def checks_page(request: Request, db: Session = Depends(get_db)):
        ctx = base_context(request)
        ctx["active_page"] = "checks"

        results = check_engine.run_all_checks()
        score = check_engine.score_checks(results)
        ctx["results"] = results
        ctx["score"] = score

        # Save to DB
        for r in results:
            db.add(Check(
                check_name=r.name,
                environment=r.env,
                passed=r.passed,
                critical=r.critical,
                detail=r.detail,
                raw_output=r.raw_output,
            ))
        db.commit()

        return render_template("checks.html", ctx)

    @app.get("/history", response_class=HTMLResponse)
    async def history_page(request: Request, db: Session = Depends(get_db)):
        ctx = base_context(request)
        ctx["active_page"] = "history"

        promotions = (
            db.query(Promotion)
            .order_by(Promotion.id.desc())
            .limit(50)
            .all()
        )
        ctx["promotions"] = promotions

        rollbacks = (
            db.query(Rollback)
            .order_by(Rollback.id.desc())
            .limit(50)
            .all()
        )
        ctx["rollbacks"] = rollbacks

        return render_template("history.html", ctx)

    @app.get("/guide", response_class=HTMLResponse)
    async def guide_page(request: Request):
        ctx = base_context(request)
        ctx["active_page"] = "guide"
        return render_template("guide.html", ctx)

    # --- API: Status ---

    @app.get("/api/status")
    async def api_status():
        envs = {}
        for env in ("dev", "stage", "prod"):
            h = oc.full_health_check(env)
            service_ok, svc = oc.check_service(env)
            envs[env] = {
                "reachable": h.reachable,
                "http_status": h.http_status,
                "response_ms": round(h.response_time_ms),
                "xmlrpc_ok": h.xmlrpc_ok,
                "db_ok": h.db_ok,
                "modules": h.modules_loaded,
                "service_active": service_ok,
                "service_status": svc,
                "error": h.error,
            }
        return envs

    @app.get("/api/status/{env}")
    async def api_env_status(env: str):
        if env not in ("dev", "stage", "prod"):
            raise HTTPException(400, "Invalid environment")
        h = oc.full_health_check(env)
        service_ok, svc = oc.check_service(env)
        return {
            "env": env,
            "reachable": h.reachable,
            "http_status": h.http_status,
            "response_ms": round(h.response_time_ms),
            "xmlrpc_ok": h.xmlrpc_ok,
            "db_ok": h.db_ok,
            "modules": h.modules_loaded,
            "service_active": service_ok,
            "service_status": svc,
            "error": h.error,
        }

    @app.get("/api/diff")
    async def api_diff():
        return oc.get_file_diff()

    # --- API: Actions ---

    @app.post("/api/checks/run")
    async def api_run_checks(db: Session = Depends(get_db)):
        results = check_engine.run_all_checks()
        score = check_engine.score_checks(results)

        for r in results:
            db.add(Check(
                check_name=r.name,
                environment=r.env,
                passed=r.passed,
                critical=r.critical,
                detail=r.detail,
                raw_output=r.raw_output,
            ))
        db.commit()

        return {
            "score": score,
            "results": [
                {
                    "name": r.name,
                    "label": r.label,
                    "env": r.env,
                    "passed": r.passed,
                    "critical": r.critical,
                    "detail": r.detail,
                }
                for r in results
            ],
        }

    @app.post("/api/promote/dev-to-stage")
    async def api_promote_dev_stage(db: Session = Depends(get_db)):
        # Pre-flight check
        checks = check_engine.run_all_checks()
        score = check_engine.score_checks(checks)
        if score["blocked"]:
            fails = [r for r in checks if r.critical and not r.passed]
            raise HTTPException(
                400,
                f"Pre-flight blocked: {len(fails)} critical failures — "
                + "; ".join(f"{r.label}: {r.detail}" for r in fails[:3])
            )

        # Run promotion
        result = promote_engine.promote_dev_to_stage()

        # Record in DB
        promo = Promotion(
            direction="dev-to-stage",
            status=result.stage,
            files_promoted=result.files_promoted,
            files_skipped=result.files_skipped,
            backup_db_path=result.backup_db_path,
            backup_code_path=result.backup_code_path,
            git_tag=result.git_tag,
            error_message=result.error,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(promo)

        if result.backup_db_path:
            db.add(Backup(
                environment="stage",
                type="db",
                path=result.backup_db_path,
                promotion_id=promo.id,
            ))
        if result.backup_code_path:
            db.add(Backup(
                environment="stage",
                type="code",
                path=result.backup_code_path,
                promotion_id=promo.id,
            ))

        db.commit()

        return {
            "success": result.stage == "success",
            "stage": result.stage,
            "files_promoted": result.files_promoted,
            "files_skipped": result.files_skipped,
            "git_tag": result.git_tag,
            "smoke_tests": result.smoke_results,
            "error": result.error,
        }

    @app.post("/api/promote/stage-to-prod")
    async def api_promote_stage_prod(
        request: Request,
        confirm: str = Form(""),
        db: Session = Depends(get_db),
    ):
        # Production safeguard
        if confirm.strip().upper() != "PROD":
            raise HTTPException(
                400,
                "Production promotion requires confirmation. "
                "Send 'confirm=PROD' to proceed."
            )

        # Pre-flight: stage must be healthy
        stage_health = oc.full_health_check("stage")
        if not stage_health.reachable:
            raise HTTPException(400, "Stage is not healthy — cannot promote to prod")

        # Run promotion
        result = promote_engine.promote_stage_to_prod()

        promo = Promotion(
            direction="stage-to-prod",
            status=result.stage,
            files_promoted=result.files_promoted,
            files_skipped=result.files_skipped,
            backup_db_path=result.backup_db_path,
            backup_code_path=result.backup_code_path,
            git_tag=result.git_tag,
            error_message=result.error,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(promo)

        if result.backup_db_path:
            db.add(Backup(
                environment="prod",
                type="db",
                path=result.backup_db_path,
                promotion_id=promo.id,
            ))

        db.commit()

        return {
            "success": result.stage == "success",
            "stage": result.stage,
            "files_promoted": result.files_promoted,
            "files_skipped": result.files_skipped,
            "git_tag": result.git_tag,
            "smoke_tests": result.smoke_results,
            "error": result.error,
        }

    @app.post("/api/rollback/stage")
    async def api_rollback_stage(
        promotion_id: int = Form(0),
        db: Session = Depends(get_db),
    ):
        backup_db = ""
        backup_code = ""

        if promotion_id > 0:
            promo = db.query(Promotion).filter(Promotion.id == promotion_id).first()
            if promo:
                backup_db = promo.backup_db_path or ""
                backup_code = promo.backup_code_path or ""

        result = rollback_engine.rollback_stage(backup_db, backup_code)

        if promotion_id > 0:
            # Mark the promotion as rolled back
            promo = db.query(Promotion).filter(Promotion.id == promotion_id).first()
            if promo:
                promo.rollback_available = False

        rb = Rollback(
            promotion_id=promotion_id if promotion_id > 0 else None,
            environment="stage",
            status=result.stage,
            error_message=result.error,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(rb)
        db.commit()

        return {
            "success": result.success,
            "stage": result.stage,
            "error": result.error,
        }

    @app.post("/api/rollback/prod")
    async def api_rollback_prod(
        promotion_id: int = Form(0),
        confirm: str = Form(""),
        db: Session = Depends(get_db),
    ):
        # Production safeguard
        if confirm.strip().upper() != "PROD":
            raise HTTPException(
                400,
                "Production rollback requires confirmation. "
                "Send 'confirm=PROD' to proceed."
            )

        backup_db = ""
        backup_code = ""

        if promotion_id > 0:
            promo = db.query(Promotion).filter(Promotion.id == promotion_id).first()
            if promo:
                backup_db = promo.backup_db_path or ""
                backup_code = promo.backup_code_path or ""

        result = rollback_engine.rollback_prod(backup_db, backup_code)

        rb = Rollback(
            promotion_id=promotion_id if promotion_id > 0 else None,
            environment="prod",
            status=result.stage,
            error_message=result.error,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(rb)
        db.commit()

        return {
            "success": result.success,
            "stage": result.stage,
            "error": result.error,
        }

    @app.post("/api/clone-db")
    async def api_clone_db(db: Session = Depends(get_db)):
        result = promote_engine.clone_prod_to_stage()
        return result

    @app.post("/api/tag-release")
    async def api_tag_release(env: str = Form("stage")):
        if env not in ("stage", "prod"):
            raise HTTPException(400, "Tag env must be 'stage' or 'prod'")
        tag = gitops.create_tag(env)
        return {"tag": tag}

    # --- Startup ---
    @app.on_event("startup")
    async def startup():
        init_db()

    return app


# Module-level app instance
app = create_app()
