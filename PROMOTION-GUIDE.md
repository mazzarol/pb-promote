# PB-Promote: Odoo 19 Promotion & Rollback Guide

**Priority Blinds Odoo 19 CI/CD Pipeline**
**Server:** BinaryLane VPS — 103.249.238.138
**App URL:** http://127.0.0.1:8080 (internal only — access via SSH tunnel or Tailscale)

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Accessing PB-Promote](#2-accessing-pb-promote)
3. [Environment Reference](#3-environment-reference)
4. [Standard Promotion Workflow](#4-standard-promotion-workflow)
5. [Rollback Procedures](#5-rollback-procedures)
6. [Database Clone (Refresh Stage)](#6-database-clone-refresh-stage)
7. [Manual Operations (CLI)](#7-manual-operations-cli)
8. [Critical Configuration](#8-critical-configuration)
9. [Common Pitfalls](#9-common-pitfalls)
10. [Emergency Recovery](#10-emergency-recovery)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                  BinaryLane VPS                          │
│                  103.249.238.138                         │
│                                                         │
│  ┌─────────┐    ┌──────────┐    ┌──────────┐           │
│  │   DEV   │───▶│  STAGE   │───▶│   PROD   │           │
│  │ :8069   │    │  :8070   │    │  :8069   │           │
│  │ odoo19dev│   │odoo19stage│   │odoo19prod │           │
│  └─────────┘    └──────────┘    └──────────┘           │
│       │              │               │                  │
│  /usr/lib/      /opt/odoo19stage/  /usr/lib/           │
│                                                         │
│  ┌──────────────────────────────────────┐               │
│  │        PB-Promote :8080              │               │
│  │  Checks │ Promote │ Rollback │ Guide  │              │
│  │  SQLite audit trail + git tags       │               │
│  └──────────────────────────────────────┘               │
│                                                         │
│  Backups: /opt/pb-promote/backups/                      │
│  Logs:    /opt/pb-promote/logs/app.log                  │
│  DB:      /opt/pb-promote/promotions.db                 │
└─────────────────────────────────────────────────────────┘
```

### Key Paths

| Path | Purpose |
|------|---------|
| `/opt/pb-promote/` | PB-Promote app root |
| `/usr/lib/python3/dist-packages/` | Dev & Prod Odoo code |
| `/opt/odoo19stage/` | Stage Odoo code (isolated copy) |
| `/opt/odoo19dev/custom-addons/priority_blinds/` | Custom addons (git repo) |
| `/opt/odoo19dev/migration/image_uploader/` | Migration scripts |
| `/opt/odoo19stage/.promotions/manifest.txt` | Files to promote |
| `/opt/odoo19stage/.promotions/promote.sh` | Promotion script |
| `/opt/odoo19stage/.promotions/promote.log` | Promotion log |

---

## 2. Accessing PB-Promote

PB-Promote binds to `127.0.0.1:8080` — **not accessible from the internet**. Use one of these methods:

### Option A: SSH Tunnel (Recommended)

```bash
ssh -L 8080:127.0.0.1:8080 root@103.249.238.138
# Then open: http://localhost:8080
```

### Option B: Tailscale

If Tailscale is installed on the VPS, access directly:
```
http://<tailscale-ip>:8080
```

### Option C: Server Console

If you're already SSH'd into the server:
```bash
curl http://127.0.0.1:8080/api/status
```

---

## 3. Environment Reference

| Env | Port | URL | Database | PG User | Code Root | Navbar Colour |
|-----|------|-----|----------|---------|-----------|---------------|
| DEV | 8069 | dev.priorityblinds.com.au | odoo19dev | odoo19dev | /usr/lib/python3/dist-packages/ | #27ae60 (green) |
| STAGE | 8070 | stage.priorityblinds.com.au | odoo19stage | odoo19stage | /opt/odoo19stage/ | #e67e22 (orange) |
| PROD | 8069 | priorityblinds.com.au | odoo19prod | odoo19prod | /usr/lib/python3/dist-packages/ | #17a2b8 (blue) |

---

## 4. Standard Promotion Workflow

### Phase 1: Make Changes (on DEV)

1. Edit code files on dev:
   ```bash
   sudo vim /usr/lib/python3/dist-packages/odoo/http.py
   ```
   Or edit custom addons:
   ```bash
   cd /opt/odoo19dev/custom-addons/priority_blinds
   vim ...
   ```

2. If you edited a NEW file that wasn't in the manifest before, add it:
   ```bash
   echo "odoo/addons/website/models/ir_http.py" | sudo tee -a /opt/odoo19stage/.promotions/manifest.txt
   ```

3. Restart dev to test changes:
   ```bash
   sudo systemctl restart odoo
   ```

4. Verify changes work on `https://dev.priorityblinds.com.au`

### Phase 2: Run Pre-Flight Checks

1. Open PB-Promote in browser
2. Navigate to **Checks** page
3. Review results:
   - **All critical green** → proceed
   - **Any critical red** → fix before continuing
   - **Amber warnings** → note them, can proceed

### Phase 3: Dev → Stage Promotion

1. Navigate to **Promote** page
2. Review the file diff to confirm expected changes
3. Verify git status shows expected uncommitted files (they'll be auto-committed)
4. Click **Promote to Stage**

**What happens automatically:**
1. ✓ Uncommitted dev changes auto-committed to git
2. ✓ Stage DB dumped to `/opt/pb-promote/backups/stage_YYYYMMDD_HHMMSS/`
3. ✓ Stage code snapshot taken
4. ✓ `promote.sh` copies changed files from dev to stage
5. ✓ `.pyc` cache files deleted on stage
6. ✓ `systemctl restart odoo-stage`
7. ✓ Smoke tests run (HTTP, XML-RPC, /shop)
8. ✓ Git tag `stage-YYYYMMDD-HHMMSS` created and pushed

### Phase 4: Stage Validation

Before promoting to production, manually verify on stage:

1. **Homepage loads:** `https://stage.priorityblinds.com.au`
2. **Shop page:** `https://stage.priorityblinds.com.au/shop`
3. **Product images render:** Check multiple products
4. **CSP headers clean:** Open DevTools → Network → check image responses have no `default-src 'none'`
5. **New features work:** Test the specific changes you made
6. **No error logs:** `sudo journalctl -u odoo-stage --since "5 min ago" | grep -i error`

**If issues found on stage:** Rollback immediately (see Section 5)

### Phase 5: Stage → Production Promotion

1. Navigate to **Promote** page
2. In the Production section, type `PROD` (all caps) in the confirmation field
3. Click **Promote to Production**

**What happens automatically:**
1. ✓ Prod DB backed up (critical — rollback anchor)
2. ✓ File diff calculated between stage and prod
3. ✓ Changed files copied from stage to prod
4. ✓ `.pyc` cache cleared on prod
5. ✓ `systemctl restart odoo` (production restart)
6. ✓ Prod smoke tests run
7. ✓ Git tag `prod-YYYYMMDD-HHMMSS` created

### Phase 6: Post-Promotion Monitoring

Monitor production for at least 5 minutes:

```bash
# Check service health
sudo systemctl status odoo

# Watch logs for errors
sudo journalctl -u odoo -f

# Verify CSP fix survived
curl -skI https://priorityblinds.com.au/web/image/product.template/1/image_1024 | grep -i content-security
# Should return NOTHING (no CSP header blocking images)
```

---

## 5. Rollback Procedures

### Stage Rollback

**Via PB-Promote UI (preferred):**
1. Navigate to **Rollback** page
2. Choose a promotion from the "Recent Promotions" table
3. Click **Rollback Stage**
4. System restores DB + code from that promotion's backup

**Via CLI (if UI unavailable):**
```bash
# Stop stage
sudo systemctl stop odoo-stage

# Restore DB from latest backup
LATEST_BACKUP=$(ls -d /opt/pb-promote/backups/stage_* | tail -1)
sudo -u postgres psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='odoo19stage';"
sudo -u postgres dropdb --if-exists odoo19stage
sudo -u postgres createdb odoo19stage
sudo -u postgres psql -d odoo19stage -f "$LATEST_BACKUP/odoo19stage.sql"
sudo -u postgres psql -c "ALTER DATABASE odoo19stage OWNER TO odoo19stage;"

# Restore code from snapshot
sudo rm -rf /opt/odoo19stage
sudo cp -a "$LATEST_BACKUP/code" /opt/odoo19stage

# Clear .pyc and restart
sudo find /opt/odoo19stage -name '*.pyc' -delete
sudo systemctl start odoo-stage

# Verify
sudo systemctl status odoo-stage
```

### Production Rollback

**Via PB-Promote UI:**
1. Navigate to **Rollback** page
2. Type `PROD` in the confirmation field
3. Click **Rollback Production**
4. System restores prod DB from pre-promotion backup

**Via CLI (EMERGENCY — if UI is down):**
```bash
# STOP PROD IMMEDIATELY
sudo systemctl stop odoo

# Restore DB from latest backup
LATEST_BACKUP=$(ls -d /opt/pb-promote/backups/prod_* | tail -1)
SQL_FILE=$(ls "$LATEST_BACKUP"/*.sql | head -1)

sudo -u postgres psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='odoo19prod';"
sudo -u postgres dropdb --if-exists odoo19prod
sudo -u postgres createdb odoo19prod
sudo -u postgres psql -d odoo19prod -f "$SQL_FILE"
sudo -u postgres psql -c "ALTER DATABASE odoo19prod OWNER TO odoo19prod;"

# Start production
sudo systemctl start odoo

# Monitor for 5 minutes
sudo journalctl -u odoo -f
```

---

## 6. Database Clone (Refresh Stage)

When stage DB drifts too far from production (schema changes, data stale):

### Via PB-Promote UI:
1. Navigate to **Promote** page
2. Scroll to "Database: Clone Prod → Stage"
3. Click **Clone Database**

### Via CLI:
```bash
sudo -u postgres psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='odoo19stage';"
sudo -u postgres psql -c "DROP DATABASE IF EXISTS odoo19stage;"
sudo -u postgres psql -c "CREATE DATABASE odoo19stage TEMPLATE odoo19prod;"
sudo -u postgres psql -c "ALTER DATABASE odoo19stage OWNER TO odoo19stage;"
```

**Important:** After cloning, re-promote code from dev to stage (the clone only copies the database, not the code).

---

## 7. Manual Operations (CLI)

### Promoting Without the UI

If PB-Promote is unavailable but you need to promote:

```bash
# 1. Auto-commit dev changes
cd /opt/odoo19dev/custom-addons/priority_blinds
git add -A
git commit -m "promote: manual $(date -Iseconds)"

# 2. Run promote.sh
sudo /opt/odoo19stage/.promotions/promote.sh

# 3. Clear .pyc
sudo find /opt/odoo19stage -name '*.pyc' -delete

# 4. Restart
sudo systemctl restart odoo-stage

# 5. Verify
sudo systemctl status odoo-stage
curl -skI https://stage.priorityblinds.com.au | head -1
```

### Manual Prod Promotion (CLI only)

```bash
# 1. Backup prod DB
sudo -u postgres pg_dump odoo19prod > /tmp/prod_backup_$(date +%Y%m%d_%H%M%S).sql

# 2. Read manifest and copy each file
while IFS= read -r line; do
    [[ -z "$line" || "$line" =~ ^# ]] && continue
    STAGE_FILE="/opt/odoo19stage/$line"
    PROD_FILE="/usr/lib/python3/dist-packages/$line"
    if ! diff -q "$STAGE_FILE" "$PROD_FILE" > /dev/null 2>&1; then
        echo "Promoting: $line"
        sudo cp "$STAGE_FILE" "$PROD_FILE"
    fi
done < /opt/odoo19stage/.promotions/manifest.txt

# 3. Clear .pyc and restart
sudo find /usr/lib/python3/dist-packages -name '*.pyc' -delete
sudo systemctl restart odoo

# 4. Verify CSP fix survived
grep "content_security_policy=''" /usr/lib/python3/dist-packages/odoo/http.py
# Should show the patched line

# 5. Monitor
sudo journalctl -u odoo -f
```

### Creating Git Tags Manually

```bash
cd /opt/odoo19dev/custom-addons/priority_blinds
TAG="stage-$(date +%Y%m%d-%H%M%S)"
git tag "$TAG"
git push origin "$TAG"
```

---

## 8. Critical Configuration

### CSP Fix (MUST survive promotions)

File: `/usr/lib/python3/dist-packages/odoo/http.py`

**Line ~587:** Change:
```python
content_security_policy="default-src 'none'"
```
To:
```python
content_security_policy=''
```

**Line ~2779:** Comment out or replace CSP assignment with `pass`

**Verify after ANY promotion or `apt upgrade`:**
```bash
grep -n "content_security_policy" /usr/lib/python3/dist-packages/odoo/http.py
curl -skI https://priorityblinds.com.au/web/image/product.template/1/image_1024 | grep -i content-security
# Should return NOTHING
```

### Google Maps API Key

Stored in Odoo `ir.config_parameter`:
```
Key: google_maps.api_key
```
Shared across all environments. If images/geocoding breaks, check this key first.

### Manifest File

`/opt/odoo19stage/.promotions/manifest.txt`

Add new tracked files here:
```bash
echo "odoo/addons/website/controllers/main.py" | sudo tee -a /opt/odoo19stage/.promotions/manifest.txt
```

### Fail2ban Recovery

If you get locked out of SSH:
1. Log into **BinaryLane web console** (browser-based)
2. Check fail2ban status: `sudo fail2ban-client status sshd`
3. Unban your IP: `sudo fail2ban-client set sshd unbanip <your-ip>`
4. Or restart fail2ban: `sudo systemctl restart fail2ban`

---

## 9. Common Pitfalls

### DB Ownership After Clone
**Symptom:** Stage database selector page appears but shows no databases.
**Cause:** `createdb -T odoo19prod odoo19stage` keeps prod's owner.
**Fix:**
```bash
sudo -u postgres psql -c "ALTER DATABASE odoo19stage OWNER TO odoo19stage;"
```

### CSP Fix Reverted
**Symptom:** Product images not rendering on Firefox after `apt upgrade odoo`.
**Cause:** Package upgrade overwrites `/usr/lib/python3/dist-packages/odoo/http.py`.
**Fix:** Re-apply the CSP fix on both dev and prod code roots, then re-promote.

### Package Updates Overwrite Dev
**Symptom:** Dev changes disappear after `apt upgrade odoo`.
**Cause:** `apt upgrade odoo` replaces `/usr/lib/python3/dist-packages/`.
**Fix:** Always re-promote after `apt upgrade`. The manifest tracks which files to promote.

### Stage DB Drift
**Symptom:** Stage behaves differently from prod.
**Cause:** Stage DB was cloned from prod weeks ago — data has diverged.
**Fix:** Clone prod DB to stage (Section 6), then re-promote code.

### .pyc Cache Stale
**Symptom:** Code changes not taking effect despite files being copied.
**Cause:** Python bytecode cache (.pyc) is older than source.
**Fix:**
```bash
sudo find /opt/odoo19stage -name '*.pyc' -delete
sudo systemctl restart odoo-stage
```

### Shared Code Path Confusion
**Symptom:** Changes to dev affect prod or vice versa.
**Cause:** Dev and prod share `/usr/lib/python3/dist-packages/`. Changes to dev code are ALSO prod code until consciously promoted.
**Rule:** Never edit prod-critical paths on dev without promoting them. Test thoroughly on dev first.

---

## 10. Emergency Recovery

### Scenario 1: Production Down After Promotion

```bash
# 1. Check what happened
sudo systemctl status odoo
sudo journalctl -u odoo --since "2 min ago" --no-pager | tail -50

# 2. If service won't start, IMMEDIATE ROLLBACK:
sudo systemctl stop odoo
sudo -u postgres psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='odoo19prod';"
sudo -u postgres dropdb --if-exists odoo19prod

# 3. Restore from latest backup
LATEST=$(ls -d /opt/pb-promote/backups/prod_* | tail -1)
SQL=$(ls "$LATEST"/*.sql | head -1)
sudo -u postgres createdb odoo19prod
sudo -u postgres psql -d odoo19prod -f "$SQL"
sudo -u postgres psql -c "ALTER DATABASE odoo19prod OWNER TO odoo19prod;"

# 4. Start prod
sudo systemctl start odoo
sudo systemctl status odoo
```

### Scenario 2: All Instances Down (Server Rebooted)

```bash
# Check all services
sudo systemctl status odoo odoo-stage pb-promote postgresql

# Start them in order
sudo systemctl start postgresql
sleep 2
sudo systemctl start odoo        # dev (port 8069)
sudo systemctl start odoo-stage  # stage (port 8070)
sudo systemctl start pb-promote
```

### Scenario 3: Disk Full

```bash
# Check usage
df -h /

# Clean up old backups (keep last 5)
ls -d /opt/pb-promote/backups/*_* | sort | head -n -5 | xargs sudo rm -rf

# Clean old logs
sudo journalctl --vacuum-size=500M
```

### Scenario 4: BinaryLane Console Only (SSH Dead)

1. Log in at BinaryLane web console (browser-based VNC/terminal)
2. Check fail2ban: `sudo fail2ban-client status sshd`
3. If your IP is banned: `sudo fail2ban-client set sshd unbanip <your-ip>`
4. If SSH service is dead: `sudo systemctl restart sshd`
5. If port changed: check `/etc/ssh/sshd_config`

---

## Quick Reference Card

```
┌─────────────────────────────────────────────────────────┐
│  PROMOTION CHECKLIST                                     │
├─────────────────────────────────────────────────────────┤
│  □ Pre-flight checks pass (all critical green)           │
│  □ File diff reviewed                                    │
│  □ Git changes committed                                 │
│  □ Stage promotion complete                              │
│  □ Stage validated (shop, images, features)              │
│  □ PROD typed for confirmation                           │
│  □ Post-promotion CSP check: curl -skI ...image...       │
│  □ Monitor prod logs for 5 minutes                       │
│  □ Git tag created                                       │
├─────────────────────────────────────────────────────────┤
│  ROLLBACK CHECKLIST                                      │
├─────────────────────────────────────────────────────────┤
│  □ Stop affected service                                 │
│  □ Restore DB from pre-promotion backup                  │
│  □ Fix DB ownership: ALTER DATABASE ... OWNER TO ...     │
│  □ Restore code from snapshot (if needed)                │
│  □ Clear .pyc files                                      │
│  □ Start service                                         │
│  □ Verify shop and images                                │
│  □ Document what happened                                │
└─────────────────────────────────────────────────────────┘
```
