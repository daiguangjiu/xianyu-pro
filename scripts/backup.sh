#!/usr/bin/env bash
# 数据备份：数据库 + 订单截图。建议放进 crontab，每天跑一次并同步到对象存储。
#   0 3 * * * /srv/xianyu-order-hub/scripts/backup.sh >> /var/log/orderhub-backup.log 2>&1
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
TS="$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

echo "[$(date '+%F %T')] 开始备份 -> $BACKUP_DIR"

# ---------- 1. 数据库 ----------
# 优先按 docker compose 里的 PostgreSQL 备份
if command -v docker >/dev/null 2>&1 \
   && docker compose ps db --quiet >/dev/null 2>&1 \
   && docker compose exec -T db pg_isready -U orderhub >/dev/null 2>&1; then
    echo "备份 PostgreSQL (docker compose)"
    docker compose exec -T db pg_dump -U orderhub -d orderhub --format=custom \
        > "$BACKUP_DIR/db_$TS.dump"
else
    # SQLite 部署：用 .backup 保证 WAL 也一起落盘（直接 cp 可能丢数据）
    SQLITE_DB="${DATABASE_URL:-sqlite:///data/app.db}"
    SQLITE_PATH="${SQLITE_DB#sqlite:///}"
    [[ "$SQLITE_PATH" != /* ]] && SQLITE_PATH="$ROOT/$SQLITE_PATH"
    if [[ -f "$SQLITE_PATH" ]]; then
        echo "备份 SQLite: $SQLITE_PATH"
        if command -v sqlite3 >/dev/null 2>&1; then
            sqlite3 "$SQLITE_PATH" ".backup '$BACKUP_DIR/db_$TS.sqlite'"
        else
            cp "$SQLITE_PATH" "$BACKUP_DIR/db_$TS.sqlite"
        fi
    else
        echo "未找到可备份的数据库，跳过" >&2
    fi
fi

# ---------- 2. 订单截图 ----------
MEDIA_DIR="${MEDIA_DIR:-$ROOT/data/media}"
if [[ -d "$MEDIA_DIR" ]]; then
    echo "备份订单截图: $MEDIA_DIR"
    tar -czf "$BACKUP_DIR/media_$TS.tar.gz" -C "$(dirname "$MEDIA_DIR")" "$(basename "$MEDIA_DIR")"
fi

# ---------- 3. 清理过期备份 ----------
find "$BACKUP_DIR" -type f -mtime +"$KEEP_DAYS" -delete
echo "[$(date '+%F %T')] 备份完成，保留 $KEEP_DAYS 天"
ls -1t "$BACKUP_DIR" | head -5

# ---------- 4. 可选：同步到对象存储 ----------
# if command -v rclone >/dev/null 2>&1; then
#     rclone sync "$BACKUP_DIR" "$RCLONE_REMOTE:orderhub-backups"
# fi
