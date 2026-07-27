"""
PB-Promote SQLAlchemy models — promotions, rollbacks, checks, backups.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, Text, Boolean, ForeignKey
from sqlalchemy.orm import relationship

from .database import Base


def _now():
    return datetime.now(timezone.utc).isoformat()


class Promotion(Base):
    __tablename__ = "promotions"

    id = Column(Integer, primary_key=True, index=True)
    direction = Column(String(20), nullable=False)  # dev-to-stage, stage-to-prod
    status = Column(
        String(20), nullable=False, default="preflight"
    )  # preflight, backup, promoting, verifying, success, failed, rolled_back
    files_changed = Column(Text)  # JSON list
    files_promoted = Column(Integer, default=0)
    files_skipped = Column(Integer, default=0)
    backup_db_path = Column(String(500))
    backup_code_path = Column(String(500))
    git_commit = Column(String(64))  # pre-promotion commit SHA
    git_tag = Column(String(128))
    rollback_available = Column(Boolean, default=True)
    smoke_tests_passed = Column(Boolean, default=False)
    started_at = Column(String(30), nullable=False, default=_now)
    completed_at = Column(String(30))
    error_message = Column(Text)
    created_by = Column(String(64), default="pb-promote")

    rollbacks = relationship("Rollback", back_populates="promotion")


class Rollback(Base):
    __tablename__ = "rollbacks"

    id = Column(Integer, primary_key=True, index=True)
    promotion_id = Column(Integer, ForeignKey("promotions.id"))
    environment = Column(String(10), nullable=False)  # stage, prod
    status = Column(
        String(20), nullable=False, default="initiated"
    )  # initiated, restoring_db, restoring_code, verifying, success, failed
    started_at = Column(String(30), nullable=False, default=_now)
    completed_at = Column(String(30))
    error_message = Column(Text)

    promotion = relationship("Promotion", back_populates="rollbacks")


class Check(Base):
    __tablename__ = "checks"

    id = Column(Integer, primary_key=True, index=True)
    run_at = Column(String(30), nullable=False, default=_now)
    check_name = Column(String(50), nullable=False)
    environment = Column(String(10))  # dev, stage, prod, or NULL
    passed = Column(Boolean, nullable=False)
    critical = Column(Boolean, default=False)
    detail = Column(Text)
    raw_output = Column(Text)


class Backup(Base):
    __tablename__ = "backups"

    id = Column(Integer, primary_key=True, index=True)
    environment = Column(String(10), nullable=False)
    type = Column(String(10), nullable=False)  # db, code
    path = Column(String(500), nullable=False)
    promotion_id = Column(Integer, ForeignKey("promotions.id"))
    created_at = Column(String(30), nullable=False, default=_now)
    size_bytes = Column(Integer)


class Setting(Base):
    """Key-value configuration store for environment params, API keys, etc."""
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False, index=True)
    value = Column(Text, default="")
    description = Column(Text, default="")
    updated_at = Column(String(30), default=_now)

    # Known keys:
    #   api_key                  — Odoo XML-RPC API key (global)
    #   env.<env>.url            — Base URL (https://dev.priorityblinds.com.au)
    #   env.<env>.port           — Port number (8069)
    #   env.<env>.db             — Database name (odoo19dev)
    #   env.<env>.username       — XML-RPC username (admin)
    #   env.<env>.service        — systemd service name (odoo, odoo-stage)
    #   env.<env>.code_root      — Code root path on disk
    #   env.<env>.theme_colour   — UI card accent (#27ae60)
    #   tracked_paths            — JSON list of paths to diff for promotion
    #   custom_addons_path       — Path to custom addons on dev
    #   ssh_host                 — SSH host for remote commands
    #   ssh_user                 — SSH user
    #   promote.backup_root      — Backup directory root
    #   promote.smoke_urls       — JSON list of URLs to hit after promote


# ── Setting helpers ────────────────────────────────────────────────────

import json as _json


def get_setting(db, key: str, default: str = "") -> str:
    """Read a single setting value from the DB."""
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value if row else default


def set_setting(db, key: str, value: str, description: str = ""):
    """Write a setting, creating or updating the row."""
    row = db.query(Setting).filter(Setting.key == key).first()
    if row:
        row.value = value
        if description:
            row.description = description
        row.updated_at = _now()
    else:
        db.add(Setting(key=key, value=value, description=description))


def load_env_config(db, env: str) -> dict:
    """Load all settings for an environment from the DB, falling back to hardcoded."""
    from app.odoo_client import ENVIRONMENTS as HARDCODED
    defaults = HARDCODED.get(env, {})

    config = {}
    for field in ("url", "db", "username", "code_root", "service"):
        config[field] = get_setting(db, f"env.{env}.{field}", default=str(defaults.get(field, "")))
    port_raw = get_setting(db, f"env.{env}.port", default=str(defaults.get("port", 8069)))
    config["port"] = int(port_raw) if port_raw.isdigit() else 8069
    config["theme_colour"] = get_setting(
        db, f"env.{env}.theme_colour",
        default={"dev": "#27ae60", "stage": "#e67e22", "prod": "#17a2b8"}.get(env, "#30363d"),
    )
    # Per-environment API key
    config["api_key"] = get_setting(db, f"env.{env}.api_key", default="")
    return config


def load_api_key(db, env: str = "") -> str:
    """Load the Odoo API key for a specific environment. Falls through DB → env var → file."""
    # Try per-env DB key first
    if env:
        key = get_setting(db, f"env.{env}.api_key")
        if key:
            return key
    # Try global key (legacy, or set via env var)
    key = get_setting(db, "api_key")
    if key:
        return key
    import os
    key = os.environ.get("ODOO_API_KEY", "")
    if key:
        return key
    try:
        with open("/opt/pb-promote/odoo_api_key.txt") as f:
            key = f.read().strip()
    except FileNotFoundError:
        pass
    if key:
        # Migrate into DB as legacy
        set_setting(db, "api_key", key, "Migrated from odoo_api_key.txt")
        db.commit()
    return key
