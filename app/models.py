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
