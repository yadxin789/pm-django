#!/usr/bin/env bash
# ============================================================================
#  PM 项目管理系统 (Django) - openEuler 一键部署脚本 (deploy.sh)
#
#  适用环境：openEuler 24.03 LTS 社区版（root 执行）
#  前置条件：
#    1. 存储 PM 项目 ZIP（如 pm-openEuler-django.zip）的 U 盘已插入【服务器本地 USB 口】
#       （也可用参数直接指定 ZIP 路径：./deploy.sh /path/to/pm.zip）
#    2. ZIP 解压后的根目录应直接包含 manage.py、requirements.txt、pm/（脚本会自动修正多套一层的情况）
#  功能：
#    [1] 安装 Python3 / MySQL / Redis / unzip / gunicorn
#    [2] 查找并挂载 U 盘，解压项目代码到 /var/www/pm-django
#    [3] 启动 MySQL，创建数据库 pm_db_django（root 密码 root）
#    [4] 创建 venv 并安装 Python 依赖（gunicorn/celery/django 等）
#    [5] 生成 .env，执行 migrate + seed_data + seed_beat + collectstatic
#    [6] 配置防火墙放行 8000
#    [7] 安装并启动 systemd 服务（gunicorn / celery worker / celery beat）
#  可重复执行：脚本对每步做幂等处理，重复运行不会破坏已有部署。
# ============================================================================

set -euo pipefail

# ---------- 基础变量（按需修改） ----------
PM_DIR="/var/www/pm-django"
DB_NAME="pm_db_django"
DB_USER="root"
DB_PASS="root"
ZIP_PATH="${1:-}"                                    # 可选：直接指定 ZIP 绝对路径（命令行第一个参数）
ZIP_NAME_PATTERN="pm-openEuler-django.zip"           # 自动查找 U 盘时的文件名匹配

# ---------- 输出辅助 ----------
log()  { echo -e "\033[1;34m[INFO]\033[0m $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m $*"; }
err()  { echo -e "\033[1;31m[ERROR]\033[0m $*"; }
fail() { err "$*"; echo ""; err "部署失败。日志输出见上方。"; exit 1; }

# ---------- 前置检查 ----------
[ "$(id -u)" -eq 0 ] || fail "请使用 root 用户运行本脚本（sudo -i 或 su - 后执行）"

log "========== PM Django 一键部署开始 =========="

# =========================================================
# [1] 安装系统依赖
# =========================================================
log "[1/7] 安装系统依赖（python3 / MySQL / Redis / unzip）..."

dnf install -y python3 python3-pip python3-devel mysql-server redis unzip gcc >/dev/null 2>&1 || \
    dnf install -y python3 python3-pip python3-devel mariadb-server redis unzip gcc

# 确定数据库服务名
if systemctl list-unit-files --no-legend 2>/dev/null | grep -q '^mysqld.service'; then
    MYSQL_SVC="mysqld"
elif systemctl list-unit-files --no-legend 2>/dev/null | grep -q '^mariadb.service'; then
    MYSQL_SVC="mariadb"
else
    MYSQL_SVC="mysqld"
fi
log "    数据库服务名: ${MYSQL_SVC}"

# =========================================================
# [2] 获取项目代码（U 盘挂载 / 指定路径）
# =========================================================
log "[2/7] 获取 PM 项目代码..."

if [ -f "${PM_DIR}/manage.py" ]; then
    log "    ${PM_DIR} 已存在项目代码，跳过解压（如需强制覆盖请先删除该目录）"
else
    WORK_ZIP=""
    if [ -n "$ZIP_PATH" ]; then
        [ -f "$ZIP_PATH" ] || fail "指定的 ZIP 不存在: $ZIP_PATH"
        WORK_ZIP="$ZIP_PATH"
    else
        # 1) 查找已挂载介质中的 pm*.zip
        WORK_ZIP="$(find /mnt /media /run/media -maxdepth 4 -type f -iname "$ZIP_NAME_PATTERN" 2>/dev/null | head -n1 || true)"
        # 2) 未找到则尝试挂载服务器本地 U 盘
        if [ -z "$WORK_ZIP" ]; then
            log "    未发现已挂载的 ZIP，尝试挂载服务器本地 U 盘..."
            USB_MNT="/mnt/usb"
            for dev in $(lsblk -rno NAME,MOUNTPOINT 2>/dev/null | awk '$2 == "" && $1 ~ /^sd[a-z][0-9]*$/ {print $1}'); do
                mkdir -p "$USB_MNT"
                if mount "/dev/${dev}" "$USB_MNT" >/dev/null 2>&1 || \
                   mount -t ntfs-3g "/dev/${dev}" "$USB_MNT" >/dev/null 2>&1; then
                    WORK_ZIP="$(find "$USB_MNT" -maxdepth 2 -type f -iname "$ZIP_NAME_PATTERN" 2>/dev/null | head -n1 || true)"
                    if [ -n "$WORK_ZIP" ]; then
                        log "    已从 U 盘找到: $WORK_ZIP"
                        break
                    fi
                    umount "$USB_MNT" >/dev/null 2>&1 || true
                fi
            done
            [ -n "$WORK_ZIP" ] || fail "未找到 ${ZIP_NAME_PATTERN}。请确认 U 盘已插入服务器本地 USB 口，或用 ./deploy.sh /绝对路径/pm.zip 指定"
        fi
    fi

    EXTRACT_DIR="$(mktemp -d /tmp/pm_extract.XXXXXX)"
    log "    解压: $WORK_ZIP"
    unzip -q "$WORK_ZIP" -d "$EXTRACT_DIR" || fail "ZIP 解压失败（文件可能损坏，或需 dnf install -y unzip）"

    # 修正多套一层目录：若解压后顶层没有 manage.py
    if [ ! -f "$EXTRACT_DIR/manage.py" ]; then
        INNER="$(find "$EXTRACT_DIR" -maxdepth 2 -name manage.py -print -quit 2>/dev/null || true)"
        if [ -n "$INNER" ]; then
            INNER_DIR="$(dirname "$INNER")"
            log "    检测到压缩包多套一层目录，自动修正: $INNER_DIR"
            EXTRACT_DIR="$INNER_DIR"
        else
            fail "ZIP 中未找到 manage.py，请确认上传的是 PM Django 项目压缩包"
        fi
    fi

    mkdir -p "$PM_DIR"
    cp -a "$EXTRACT_DIR/." "$PM_DIR/"
    rm -rf "$(dirname "$EXTRACT_DIR")" 2>/dev/null || true

    # U 盘卸载提示
    if [ -n "${USB_MNT:-}" ] && mountpoint -q "$USB_MNT" 2>/dev/null; then
        umount "$USB_MNT" >/dev/null 2>&1 && log "    U 盘已安全卸载，可拔出" || warn "    U 盘仍在占用，稍后手动 umount $USB_MNT"
    fi
fi

cd "$PM_DIR"

# =========================================================
# [3] 启动数据库并创建 pm_db_django
# =========================================================
log "[3/7] 启动数据库并创建 ${DB_NAME}..."

systemctl enable --now "$MYSQL_SVC" >/dev/null 2>&1 || fail "数据库服务启动失败"

# 等待数据库就绪
for i in $(seq 1 15); do
    if mysqladmin -uroot ping >/dev/null 2>&1; then break; fi
    sleep 1
done

if mysql -uroot -e "SELECT 1" >/dev/null 2>&1; then
    mysql -uroot <<SQL
ALTER USER 'root'@'localhost' IDENTIFIED BY '${DB_PASS}';
CREATE DATABASE IF NOT EXISTS ${DB_NAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
FLUSH PRIVILEGES;
SQL
elif mysql -uroot -p"${DB_PASS}" -e "SELECT 1" >/dev/null 2>&1; then
    mysql -uroot -p"${DB_PASS}" -e "CREATE DATABASE IF NOT EXISTS ${DB_NAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
else
    fail "无法连接 MySQL root（既不是空密码也不是密码 ${DB_PASS}），请手动修正数据库"
fi
log "    数据库 ${DB_NAME} 就绪"

# =========================================================
# [4] 创建 venv 并安装 Python 依赖
# =========================================================
log "[4/7] 创建 venv 并安装 Python 依赖..."

python3 -m venv venv
./venv/bin/pip install -U pip >/dev/null 2>&1
./venv/bin/pip install -r requirements.txt || fail "pip install 失败，请检查 requirements.txt 与网络"
# 确保 gunicorn 已安装
./venv/bin/pip install gunicorn >/dev/null 2>&1 || true

# =========================================================
# [5] 生成 .env + migrate + seed + collectstatic
# =========================================================
log "[5/8] 初始化数据库与静态文件..."

mkdir -p logs media

if [ ! -f ".env" ]; then
    cat > .env <<EOF
DB_NAME=${DB_NAME}
DB_USER=${DB_USER}
DB_PASSWORD=${DB_PASS}
DB_HOST=127.0.0.1
DB_PORT=3306
DEBUG=False
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
EOF
    log "    已生成 .env"
fi

# Django 不自动读 .env（systemd 靠 EnvironmentFile 注入），
# 这里手动导出，保证 migrate / seed / collectstatic 与线上服务的配置完全一致。
set -a
. ./.env
set +a

./venv/bin/python manage.py migrate --noinput || fail "数据库迁移失败"

USER_COUNT="$(mysql -uroot -p"${DB_PASS}" -N -e "SELECT COUNT(*) FROM ${DB_NAME}.core_user;" 2>/dev/null || echo 0)"
if [ "${USER_COUNT:-0}" = "0" ]; then
    ./venv/bin/python manage.py seed_data || warn "seed_data 执行失败（可稍后手动执行）"
    log "    预置数据已写入"
else
    log "    users 表已有 ${USER_COUNT} 条数据，跳过 seed_data"
fi

./venv/bin/python manage.py seed_beat || true
./venv/bin/python manage.py collectstatic --noinput >/dev/null 2>&1 || true

# =========================================================
# [6] 防火墙
# =========================================================
log "[6/8] 配置防火墙..."
if command -v firewall-cmd >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port=8000/tcp >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
    log "    已放行 8000/tcp"
else
    warn "    未检测到 firewall-cmd，跳过防火墙配置"
fi

# =========================================================
# [7] 创建运行账号并修正目录属主（服务不再以 root 运行）
# =========================================================
log "[7/8] 创建服务运行账号 pm 并修正目录权限..."
SERVICE_USER="pm"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --home-dir "${PM_DIR}" --shell /sbin/nologin "${SERVICE_USER}" \
        || warn "创建账号 ${SERVICE_USER} 失败，服务将可能启动不了，请手动处理"
fi
# 需要可写的目录：静态/媒体素材、备份、日志、celery 心跳.pid 文件
mkdir -p "${PM_DIR}/web/backups" "${PM_DIR}/logs" "${PM_DIR}/media" "${PM_DIR}/staticfiles"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${PM_DIR}/logs" "${PM_DIR}/media" \
    "${PM_DIR}/staticfiles" "${PM_DIR}/web/backups" 2>/dev/null || true
chown "${SERVICE_USER}:${SERVICE_USER}" "${PM_DIR}" 2>/dev/null || true
log "    运行账号: ${SERVICE_USER}（如需改回其它账号，请改 deploy/*.service 的 User=）"

# =========================================================
# [8] 安装并启动 systemd 服务
# =========================================================
log "[8/8] 安装并启动 systemd 服务（gunicorn / celery / celery-beat）..."

cp deploy/pm-django.service /etc/systemd/system/
cp deploy/pm-celery.service /etc/systemd/system/
cp deploy/pm-celery-beat.service /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now pm-django pm-celery pm-celery-beat >/dev/null 2>&1 || warn "部分服务启动失败，请手动 systemctl status 排查"

# =========================================================
# 收尾
# =========================================================
echo ""
log "========== PM Django 部署完成 =========="
echo ""
echo "  访问地址 : http://$(hostname -I 2>/dev/null | awk '{print $1}'):8000"
echo "  项目目录 : ${PM_DIR}"
echo "  数据库   : ${DB_NAME}（root / ${DB_PASS}）"
echo "  服务管理 :"
echo "      systemctl status pm-django      # Web (gunicorn)"
echo "      systemctl status pm-celery      # 队列 worker"
echo "      systemctl status pm-celery-beat # 定时调度"
echo ""
warn "提示：如需协同编辑，请另行执行 ./deploy/deploy-onlyoffice.sh"
warn "提示：如需电话会议，请另行执行 ./deploy/deploy-voice.sh"
