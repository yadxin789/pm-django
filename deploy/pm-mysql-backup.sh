#!/usr/bin/env bash
# ============================================================================
#  PM 项目 MySQL 每日自动备份
#  部署位置：/etc/cron.daily/pm-mysql-backup （chmod 0755）
#  由 anacron 每天触发一次：
#    /etc/anacrontab: "1 5 cron.daily nice run-parts /etc/cron.daily"
#    START_HOURS_RANGE=3-22，RANDOM_DELAY=45
#    → 实际执行窗口约 03:05 ~ 03:50（不是固定 04:02）
#  注意：文件名不能带 "."（run-parts 会跳过），故本文件名为 pm-mysql-backup
#
#  设计要点：
#   - 数据库名从 .env 读取；账号密码走 /root/.my.cnf（mode 600），
#     命令里不出现密码 → 不再有 "Using a password ... can be insecure" 警告，
#     也不会在 ps -ef 中泄露凭据
#   - 备份文件做大小健全性检查，坏备份记进日志而不是静默成功
#   - 保留 7 天，自动清理
#
#  注意：--defaults-extra-file 必须是命令行第一个选项
# ============================================================================

set -u
set -o pipefail

ENV_FILE="/var/www/pm-django/.env"
MY_CNF="/root/.my.cnf"
BK="/var/backups/pm-mysql"
TS=$(date +%Y%m%d-%H%M%S)
LOG="/var/log/pm-mysql-backup.log"

mkdir -p "$BK"
: >>"$LOG"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" >>"$LOG"; }

log "=== 备份开始 (PID $$) ==="

if [ ! -r "$ENV_FILE" ]; then
    log "FATAL: $ENV_FILE 不可读"
    exit 1
fi

if [ ! -r "$MY_CNF" ]; then
    log "FATAL: $MY_CNF 不可读（需含 [client] 段：host/port/user/password，chmod 600）"
    exit 1
fi

set -a
. "$ENV_FILE" 2>>"$LOG"
set +a

DB_NAME="${DB_NAME:-pm_db_django}"

log "DB_NAME=[$DB_NAME] 凭据来源=$MY_CNF"

OUT="$BK/pm_db_${TS}.sql.gz"

if mysqldump \
        --defaults-extra-file="$MY_CNF" \
        --single-transaction --quick \
        "$DB_NAME" 2>>"$LOG" | gzip -9 >"$OUT"; then

    SIZE=$(stat -c%s "$OUT" 2>/dev/null || echo 0)
    if [ "$SIZE" -ge 1024 ]; then
        log "OK   $OUT ($SIZE bytes)"
        find "$BK" -name 'pm_db_*.sql.gz' -mtime +7 -delete
    else
        log "FAIL 备份文件过小 ($SIZE bytes) → $OUT"
        rm -f "$OUT"
        exit 1
    fi
else
    RC=$?
    log "FAIL mysqldump 退出码=$RC"
    exit $RC
fi
