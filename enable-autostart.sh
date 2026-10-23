#!/usr/bin/env bash
# ============================================================================
#  PM 项目管理系统 - 生产环境开机自启启用脚本
#
#  功能：
#    [1] 自动检测项目目录、venv、数据库服务（mysqld / mariadb）
#    [2] 按本机实际路径与 DB 服务名重写 .service 文件
#    [3] 安装到 /etc/systemd/system/，daemon-reload
#    [4] 启用依赖服务（数据库、redis）
#    [5] 启用并启动 pm-django / pm-celery / pm-celery-beat
#    [6] 输出状态与开机自启校验
#
#  用法（root 执行）：
#     cd /var/www/pm-django
#     ./enable-autostart.sh
# ============================================================================

set -u

PM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PM_DIR"

DEPLOY_DIR="$PM_DIR/deploy"
SYSTEMD_DIR="/etc/systemd/system"

c_blue='\033[1;34m'; c_yellow='\033[1;33m'; c_red='\033[1;31m'; c_off='\033[0m'
log()  { printf "${c_blue}[INFO]${c_off} %s\n" "$*"; }
warn() { printf "${c_yellow}[WARN]${c_off} %s\n" "$*"; }
err()  { printf "${c_red}[ERROR]${c_off} %s\n" "$*"; }

# ---------- 前置检查 ----------
[ "$(id -u)" -eq 0 ] || { err "请使用 root 执行本脚本"; exit 1; }
[ -f "$PM_DIR/manage.py" ] || { err "未在 $PM_DIR 找到 manage.py，请先 cd 到项目根目录"; exit 1; }
[ -d "$DEPLOY_DIR" ] || { err "缺少 $DEPLOY_DIR（应包含 pm-*.service 模板）"; exit 1; }

# ---------- 检测 venv ----------
if [ -x "$PM_DIR/venv/bin/python" ]; then
    VENV_REL="venv"
elif [ -x "$PM_DIR/.venv/bin/python" ]; then
    VENV_REL=".venv"
else
    err "未找到 venv，请先执行 ./start.sh setup 或手动创建 venv + pip install -r requirements.txt"
    exit 1
fi
log "项目目录 : $PM_DIR"
log "虚拟环境 : $VENV_REL"

# ---------- 检测数据库服务 ----------
DB_SVC=""
if systemctl list-unit-files --no-legend 2>/dev/null | awk '{print $1}' | grep -qx 'mysqld.service'; then
    DB_SVC="mysqld.service"
elif systemctl list-unit-files --no-legend 2>/dev/null | awk '{print $1}' | grep -qx 'mariadb.service'; then
    DB_SVC="mariadb.service"
else
    err "未检测到 mysqld.service 或 mariadb.service，请先安装并启动数据库"
    exit 1
fi
log "数据库服务: $DB_SVC"

# ---------- redis ----------
REDIS_OK=0
if systemctl list-unit-files --no-legend 2>/dev/null | awk '{print $1}' | grep -qx 'redis.service'; then
    REDIS_OK=1
fi
[ "$REDIS_OK" -eq 1 ] && log "redis 服务: 已安装" || warn "未检测到 redis.service，Celery 将无法启动"

# ---------- 准备日志目录 ----------
mkdir -p "$PM_DIR/logs"

# ---------- 重写 .service 文件（路径与依赖） ----------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for svc in pm-django pm-celery pm-celery-beat; do
    [ -f "$DEPLOY_DIR/$svc.service" ] || { err "缺少模板: $DEPLOY_DIR/$svc.service"; exit 1; }
    sed -e "s|/var/www/pm-django|$PM_DIR|g" \
        -e "s|/var/www/pm-django/venv/bin|$PM_DIR/$VENV_REL/bin|g" \
        -e "s|mysqld\\.service|$DB_SVC|g" \
        "$DEPLOY_DIR/$svc.service" > "$TMP/$svc.service"
done
log "已按本机环境重写 service 模板"

# ---------- 安装 ----------
install -m 0644 "$TMP"/pm-*.service "$SYSTEMD_DIR/"
log "已写入 $SYSTEMD_DIR/pm-*.service"

# ---------- 启用依赖 ----------
log "启用并启动依赖服务（DB / Redis）..."
systemctl enable --now "$DB_SVC" >/dev/null 2>&1 \
    || { err "数据库服务 $DB_SVC 启动失败，请手动 systemctl status $DB_SVC"; exit 1; }
[ "$REDIS_OK" -eq 1 ] && systemctl enable --now redis >/dev/null 2>&1 \
    || warn "redis 启动失败（Celery 会受影响）"

# ---------- 启用 PM 服务 ----------
systemctl daemon-reload
log "systemctl daemon-reload 完成"

log "启用并启动 pm-django / pm-celery / pm-celery-beat ..."
systemctl enable pm-django pm-celery pm-celery-beat
systemctl restart pm-django pm-celery pm-celery-beat
sleep 2

# ---------- 校验 ----------
echo
echo "============================================"
echo " 自启状态校验"
echo "============================================"
printf '%-22s %-12s %-12s\n' SERVICE ENABLED ACTIVE
for svc in mysqld mariadb redis pm-django pm-celery pm-celery-beat; do
    en="$(systemctl is-enabled "$svc" 2>/dev/null || echo '-')"
    ac="$(systemctl is-active "$svc" 2>/dev/null || echo '-')"
    printf '%-22s %-12s %-12s\n' "$svc" "$en" "$ac"
done

echo
echo "正在监听的端口:"
(ss -lntp 2>/dev/null || netstat -lntp 2>/dev/null) | grep -E ":(8000|6379|3306|3307)\b" || echo "  （8000/6379/数据库 暂未监听，请稍候或 journalctl -u pm-django -n 50 查看）"

echo
echo "开机自启测试（模拟拉起）："
systemctl start pm-django pm-celery pm-celery-beat
sleep 2
systemctl --no-pager --full status pm-django pm-celery pm-celery-beat | grep -E 'Active:|Loaded:' | head -n 20

echo
log "完成。后续管理命令："
echo "    systemctl status pm-django"
echo "    systemctl restart pm-django pm-celery pm-celery-beat"
echo "    journalctl -u pm-django -f    # 实时跟踪日志"
echo "    模拟开机自启：systemctl reboot"
