#!/usr/bin/env bash
# ============================================================================
#  PM Django - 电话会议（语音）一键部署脚本
#
#  前置条件：先按 deploy.sh 完成 PM Django 主系统部署
#  功能：
#    [1] 自签名 SSL 证书
#    [2] 启动 Janus WebRTC 网关（Docker）
#    [3] 启动 coturn TURN/STUN 服务器（Docker）
#    [4] 写入 Django .env 的 VOICE_* 配置
#    [5] 防火墙放行相关端口
#    [6] 健康检查
# ============================================================================

set -euo pipefail

PM_DIR="/var/www/pm-django"
VOICE_SSL_DIR="/etc/pm/voice/ssl"
JANUS_HTTP_PORT=8088
JANUS_WS_PORT=8188
JANUS_ADMIN_PORT=7088
JANUS_RTP_RANGE="20000-40000"

log()  { echo -e "\033[1;34m[INFO]\033[0m $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m $*"; }
err()  { echo -e "\033[1;31m[ERROR]\033[0m $*"; }

[ "$(id -u)" -eq 0 ] || { err "请用 root 运行"; exit 1; }
[ -d "$PM_DIR" ] || { err "$PM_DIR 不存在，请先运行 deploy.sh"; exit 1; }

SERVER_IP=$(hostname -I | awk '{print $1}')
[ -n "$SERVER_IP" ] || SERVER_IP="127.0.0.1"
ENV_FILE="${PM_DIR}/.env"

log "========== PM 电话会议部署开始（IP: $SERVER_IP）=========="

# [1] SSL
log "[1/7] 自签名 SSL..."
mkdir -p "$VOICE_SSL_DIR"
if [ ! -f "$VOICE_SSL_DIR/server.crt" ]; then
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout "$VOICE_SSL_DIR/server.key" \
        -out "$VOICE_SSL_DIR/server.crt" \
        -subj "/CN=$SERVER_IP" \
        -addext "subjectAltName=IP:$SERVER_IP,DNS:localhost" >/dev/null 2>&1
fi
# 把证书发布到 /static：客户机走 80 端口 http 下载（不弹告警），双击 install-cert.bat 装成
# 「受信任的根证书颁发机构」，之后 443 / 8443 / 3002 全部免告警。证书已含 CA:TRUE + SAN=IP。
install -m 644 "$VOICE_SSL_DIR/server.crt" "${PM_DIR}/static/pm-ca.crt" 2>/dev/null || true
[ -f "${PM_DIR}/deploy/install-cert-client.bat" ] \
    && install -m 644 "${PM_DIR}/deploy/install-cert-client.bat" "${PM_DIR}/static/install-cert.bat" 2>/dev/null || true

# [2] Janus
log "[2/7] 启动 Janus 容器..."
if ! docker ps --format '{{.Names}}' | grep -q '^pm-janus$'; then
    docker rm -f pm-janus 2>/dev/null || true
    if docker run -d \
        --name pm-janus \
        --restart=always \
        -p ${JANUS_HTTP_PORT}:8088 \
        -p ${JANUS_WS_PORT}:8188 \
        -p ${JANUS_ADMIN_PORT}:7088 \
        -p ${JANUS_RTP_RANGE}:${JANUS_RTP_RANGE}/udp \
        meetecho/janus-gateway:latest >/dev/null 2>&1; then
        log "Janus 已启动"
    else
        # 镜像拉取/启动失败 → best-effort：纯 WebRTC mesh 模式不依赖 Janus
        warn "Janus 镜像拉取/启动失败（纯 mesh 模式可忽略；若日后改 SFU 模式再处理）"
    fi
else
    log "Janus 已存在，跳过"
fi

# [3] coturn
log "[3/7] 启动 coturn 容器..."
TURN_SECRET=""
if [ -f "$ENV_FILE" ]; then
    TURN_SECRET="$(grep -E '^TURN_SECRET=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
fi
[ -z "$TURN_SECRET" ] && TURN_SECRET=$(openssl rand -hex 16)

if ! docker ps --format '{{.Names}}' | grep -q '^pm-coturn$'; then
    docker rm -f pm-coturn 2>/dev/null || true
    if docker run -d \
        --name pm-coturn \
        --restart=always \
        --network=host \
        coturn/coturn:latest \
        -n --listening-port=3478 --fingerprint --lt-cred-mech \
        --realm=pm-voice --user=pm:${TURN_SECRET} \
        --external-ip=${SERVER_IP} >/dev/null 2>&1; then
        log "coturn 已启动"
    else
        # 同上，best-effort：纯 mesh 模式只用 stun（且当前 JS iceServers 为空），coturn 暂非必需
        warn "coturn 启动失败（NAT 穿透需要时再处理；同局域网/mesh 模式可忽略）"
    fi
else
    log "coturn 已存在，跳过"
fi

# [4] PM 信令服务（替代参考里的 PHP Swoole `pm-signal`）
log "[4/7] 启动 PM 信令服务（pm-signal）..."
PIP="/var/www/pm-django/venv/bin/pip"
# 优先走 PyPI；离线服务器走 /opt/pm-offline/wheels 兜底
$PIP install --quiet "websockets==13.1" 2>/dev/null \
    || $PIP install --quiet --no-index --find-links=/opt/pm-offline/wheels "websockets==13.1" \
    || { err "websockets 安装失败（请检查网络或 /opt/pm-offline/wheels）"; exit 1; }

# systemd 单元
install -m 644 "${PM_DIR}/deploy/pm-signal.service" /etc/systemd/system/pm-signal.service
systemctl daemon-reload
systemctl enable pm-signal.service >/dev/null 2>&1
systemctl restart pm-signal.service \
    || { err "pm-signal 启动失败，请看 journalctl -u pm-signal"; exit 1; }

# nginx wss 反代（3001 对外 TLS，内部走 127.0.0.1:3001 的明文 WS）
SIGNAL_NGINX=/etc/nginx/conf.d/pm-signal-https.conf
cat > "$SIGNAL_NGINX" <<'NGINX'
map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}
server {
    listen       3002 ssl;
    listen       [::]:3002 ssl;
    ssl_certificate     /etc/pm/voice/ssl/server.crt;
    ssl_certificate_key /etc/pm/voice/ssl/server.key;

    access_log /var/log/nginx/pm-signal-access.log main;
    error_log  /var/log/nginx/pm-signal-error.log warn;

    location / {
        proxy_pass         http://127.0.0.1:3001;
        proxy_http_version 1.1;
        proxy_set_header   Host              $http_host;
        proxy_set_header   Upgrade           $http_upgrade;
        proxy_set_header   Connection        $connection_upgrade;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
NGINX
# 端口冲突兜底：旧的 3001 反代（如果存在）删掉
rm -f /etc/nginx/conf.d/pm-signal-https.conf.bak-sameport 2>/dev/null || true
nginx -t >/dev/null 2>&1 \
    && systemctl reload nginx >/dev/null \
    || { err "nginx 配置校验/重载失败：$SIGNAL_NGINX"; exit 1; }
log "信令服务已起（systemd：pm-signal.service；nginx：wss://${SERVER_IP}:3002/ → ws://127.0.0.1:3001）"

# [5] 写入 .env
log "[5/7] 写入 Django .env VOICE_* 配置..."
upsert_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        echo "${key}=${value}" >> "$ENV_FILE"
    fi
}
upsert_env "TURN_SECRET" "$TURN_SECRET"
upsert_env "VOICE_JANUS_WS" "ws://${SERVER_IP}:${JANUS_WS_PORT}"
upsert_env "VOICE_JANUS_ADMIN" "http://127.0.0.1:${JANUS_ADMIN_PORT}"
upsert_env "VOICE_STUN" "stun:${SERVER_IP}:3478"
upsert_env "VOICE_TURN" "turn:${SERVER_IP}:3478?transport=udp"
upsert_env "VOICE_TURN_USER" "pm"
upsert_env "VOICE_TURN_SECRET" "$TURN_SECRET"
upsert_env "VOICE_PM_INTERNAL_URL" "http://127.0.0.1:8000"
upsert_env "VOICE_SIGNAL_WS" "wss://${SERVER_IP}:3002"

# [5] 防火墙
log "[6/7] 防火墙..."
firewall-cmd --permanent --add-port=${JANUS_WS_PORT}/tcp >/dev/null 2>&1 || true
firewall-cmd --permanent --add-port=${JANUS_HTTP_PORT}/tcp >/dev/null 2>&1 || true
firewall-cmd --permanent --add-port=${JANUS_ADMIN_PORT}/tcp >/dev/null 2>&1 || true
firewall-cmd --permanent --add-port=3478/udp >/dev/null 2>&1 || true
firewall-cmd --permanent --add-port=3478/tcp >/dev/null 2>&1 || true
firewall-cmd --permanent --add-port=3002/tcp >/dev/null 2>&1 || true
firewall-cmd --permanent --add-port=${JANUS_RTP_RANGE}/udp >/dev/null 2>&1 || true
firewall-cmd --reload >/dev/null 2>&1 || true

# [6] 健康检查
log "[7/7] 健康检查..."
curl -s http://127.0.0.1:${JANUS_ADMIN_PORT}/info >/dev/null && log "Janus admin OK" || warn "Janus admin 未响应"
ss -lunup 2>/dev/null | grep -q ':3478' && log "coturn 3478 OK" || warn "coturn 未监听"
systemctl is-active pm-signal.service >/dev/null 2>&1 && log "pm-signal systemd OK" || warn "pm-signal 未运行"
# WebSocket 升级探测：wss://127.0.0.1:3002/ 用普通 HTTP/1.1 GET 应得 426 Upgrade Required
curl -k -s -o /dev/null -w '%{http_code}' "https://127.0.0.1:3002/" 2>/dev/null | grep -q '^426$' \
    && log "wss 3002 OK" || warn "wss 3002 未响应（pm-signal 或 nginx 异常）"

systemctl restart pm-django 2>/dev/null || true

echo ""
echo "========== 完成 =========="
# 必须用 HTTPS：getUserMedia（麦克风）只在安全上下文可用，http 下 navigator.mediaDevices
# 是 undefined，会议页点「加入会议」会报 Cannot read properties of undefined (reading 'getUserMedia')。
# 站点本身的 HTTPS 由 deploy/setup-nginx-https.sh 提供（443 + DS 反代 8443）。
echo "  语音会议入口: https://${SERVER_IP}/project/<ID>/voice   （若还没配 HTTPS，先跑 deploy/setup-nginx-https.sh）"
echo "  Janus: ws://${SERVER_IP}:${JANUS_WS_PORT}"
echo "  TURN: ${SERVER_IP}:3478 (pm / ${TURN_SECRET})"
echo ""
echo "  注意：信令服务（VOICE_SIGNAL_WS，默认 wss://<IP>:3001）在 Django 版尚无对应实现，"
echo "        未部署时会议页仍能进（页面/文档面板正常），但入会会连不上信令。"
