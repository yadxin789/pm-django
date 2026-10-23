#!/usr/bin/env bash
# ============================================================================
#  PM Django - OnlyOffice Document Server 一键部署脚本
#
#  前置条件：先按 deploy.sh 完成 PM Django 主系统部署
#  功能：
#    [1] 安装并启动 Docker
#    [2] 解析 JWT 密钥
#    [3] 创建并启动 onlyoffice/documentserver 容器（8080 端口）
#    [4] 安装中文字体
#    [5] 防火墙放行 8080
#    [6] 配置 Django settings（写入 .env 的 ONLYOFFICE_* 变量）
#    [7] healthcheck 验证
# ============================================================================

set -euo pipefail

# ---------- 基础变量 ----------
PM_DIR="/var/www/pm-django"
DS_CONTAINER="onlyoffice"
DS_PORT=8080
DS_IMAGE="onlyoffice/documentserver:latest"
# 离线资产目录（由 deploy/prepare-offline-assets.sh 预置到本机）：
# 中文字体以只读卷挂进容器，重建容器不再依赖容器内 apt-get（服务器无外网时 apt 必失败）
HOST_FONT_DIR="/opt/pm-offline/fonts/wqy"
JWT_SECRET=""
DS_PUBLIC_URL=""
PM_INTERNAL_URL=""

log()  { echo -e "\033[1;34m[INFO]\033[0m $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m $*"; }
err()  { echo -e "\033[1;31m[ERROR]\033[0m $*"; }
fail() { err "$*"; echo ""; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "请用 root 运行"
ENV_FILE="${PM_DIR}/.env"

log "========== OnlyOffice Document Server 部署开始 =========="

# [1] Docker
log "[1/7] 检查 Docker..."
if ! command -v docker >/dev/null 2>&1; then
    dnf install -y docker >/dev/null 2>&1 || fail "Docker 安装失败"
fi
command -v curl >/dev/null 2>&1 || dnf install -y curl >/dev/null 2>&1 || true
systemctl enable --now docker >/dev/null 2>&1 || fail "Docker 启动失败"
for i in $(seq 1 15); do docker info >/dev/null 2>&1 && break; sleep 1; done
docker info >/dev/null 2>&1 || fail "Docker daemon 未就绪"

# [2] JWT
log "[2/7] 解析 JWT 密钥..."
if [ -n "$JWT_SECRET" ]; then
    :
elif [ -f "$ENV_FILE" ] && grep -q '^ONLYOFFICE_JWT_SECRET=.\+' "$ENV_FILE"; then
    JWT_SECRET="$(grep '^ONLYOFFICE_JWT_SECRET=' "$ENV_FILE" | head -n1 | cut -d= -f2-)"
else
    # 注意：旧容器很可能「根本没带 JWT_SECRET」（DS 入口脚本在密钥为空时每次启动都会
    # 随机生成一个写进 local.json）→ Django 侧签名永远对不上。只有拿到足够长的非空值
    # 才复用它，否则生成一个新密钥并写入 .env 固化下来。
    JWT_FROM_CONTAINER=""
    if docker inspect "$DS_CONTAINER" >/dev/null 2>&1; then
        JWT_FROM_CONTAINER="$(docker inspect "$DS_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | sed -n 's/^JWT_SECRET=//p' | head -n1)"
    fi
    if [ "${#JWT_FROM_CONTAINER}" -ge 16 ]; then
        JWT_SECRET="$JWT_FROM_CONTAINER"
    else
        JWT_SECRET="$(openssl rand -hex 32)"
    fi
fi
[ "${#JWT_SECRET}" -ge 16 ] || fail "JWT_SECRET 过短（长度 ${#JWT_SECRET}）"

# [3] 容器
log "[3/7] 创建 ${DS_CONTAINER} 容器..."
if docker inspect "$DS_CONTAINER" >/dev/null 2>&1; then
    docker rm -f "$DS_CONTAINER" >/dev/null 2>&1 || true
fi
if ! docker image inspect "$DS_IMAGE" >/dev/null 2>&1; then
    log "    拉取镜像（首次约数百 MB）..."
    docker pull "$DS_IMAGE" || fail "镜像拉取失败"
fi
# 注意：不要传 --add-host host.docker.internal:host-gateway。
# openEuler 自带的 Docker 可能低于 20.10，不认识 host-gateway 这个特殊值，
# 会让 docker run 直接报 invalid IP address 并失败（而旧容器已在上面被 rm 掉，
# 于是出现「DS 整个不可用」的空窗）。本方案的 ONLYOFFICE_PM_INTERNAL_URL
# 直接用宿主机 IP，不需要 host.docker.internal 映射。
FONT_ARGS=()
if [ -n "$(ls -A "$HOST_FONT_DIR" 2>/dev/null)" ]; then
    FONT_ARGS=(-v "${HOST_FONT_DIR}:/usr/share/fonts/truetype/wqy:ro")
fi

docker run -d --name "$DS_CONTAINER" \
    --restart=always \
    -p "${DS_PORT}:80" \
    ${FONT_ARGS[@]+"${FONT_ARGS[@]}"} \
    -e JWT_ENABLED=true \
    -e JWT_SECRET="$JWT_SECRET" \
    "$DS_IMAGE" || fail "容器创建失败"

for i in $(seq 1 30); do
    [ "$(docker inspect -f '{{.State.Running}}' "$DS_CONTAINER" 2>/dev/null)" = "true" ] && break
    sleep 2
done

# [4] 中文字体
log "[4/7] 确保中文字体可用..."
if [ "${#FONT_ARGS[@]}" -gt 0 ]; then
    log "    已用只读卷挂载本机字体：${HOST_FONT_DIR}（不依赖容器内 apt/外网）"
else
    warn "本机没有 ${HOST_FONT_DIR}：先执行 deploy/prepare-offline-assets.sh 预置离线资产"
    warn "退回容器内 apt 安装，这需要容器能访问 Debian 源（无外网会失败）"
    docker exec "$DS_CONTAINER" bash -c "fc-list 2>/dev/null | grep -qi wqy" 2>/dev/null || \
        docker exec "$DS_CONTAINER" bash -c "apt-get update >/dev/null 2>&1 && apt-get install -y fonts-wqy-zenhei fonts-wqy-microhei >/dev/null 2>&1 && rm -rf /var/lib/apt/lists/*" || true
fi
# 重建 DS 字体表（容器内没有 fc-cache；该脚本自身会重启 docservice/converter）
if docker exec "$DS_CONTAINER" test -x /usr/bin/documentserver-generate-allfonts.sh 2>/dev/null; then
    docker exec "$DS_CONTAINER" documentserver-generate-allfonts.sh >/dev/null 2>&1 \
        && log "    字体表已重建（documentserver-generate-allfonts.sh）" || true
fi

# [5] 防火墙
log "[5/7] 防火墙放行 ${DS_PORT}/tcp..."
firewall-cmd --permanent --add-port="${DS_PORT}/tcp" >/dev/null 2>&1 || true
firewall-cmd --reload >/dev/null 2>&1 || true

# [6] 写入 .env
log "[6/7] 配置 Django .env..."
SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$SERVER_IP" ] || SERVER_IP="127.0.0.1"

upsert_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        echo "${key}=${value}" >> "$ENV_FILE"
    fi
}

# 浏览器加载 api.js 的地址：必须是「浏览器可达」的 IP:端口；
# 若填 localhost/127.0.0.1，从别的机器打开编辑器会报 DocsAPI is not defined
upsert_env "ONLYOFFICE_SERVER_URL" "http://${SERVER_IP}:${DS_PORT}"
upsert_env "ONLYOFFICE_JWT_SECRET" "$JWT_SECRET"
# DS（容器内）回访 PM 的地址。gunicorn 只监听 127.0.0.1:8000，容器用
# host.docker.internal:8000 是连不上的；必须经 Nginx 走 80 端口（不带端口）。
upsert_env "ONLYOFFICE_PM_INTERNAL_URL" "http://${SERVER_IP}"

# [7] healthcheck
log "[7/7] 验证 healthcheck..."
DS_OK=0
for i in $(seq 1 36); do
    if curl -sf "http://127.0.0.1:${DS_PORT}/healthcheck" 2>/dev/null | grep -q 'true'; then
        DS_OK=1; break
    fi
    sleep 5
done

echo ""
if [ "$DS_OK" = "1" ]; then
    log "healthcheck 通过，Document Server 运行正常"
else
    warn "healthcheck 未通过，执行 docker logs ${DS_CONTAINER} 排查"
fi

# 重启 Django 让配置生效
systemctl restart pm-django 2>/dev/null || true

echo ""
echo "  OnlyOffice: http://${SERVER_IP}:${DS_PORT}/welcome"
echo "  JWT: ${JWT_SECRET}"
