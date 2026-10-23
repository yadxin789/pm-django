#!/usr/bin/env bash
# ============================================================================
#  PM Django - 启用 HTTPS（自签证书）：让浏览器开放麦克风（getUserMedia）
#
#  为什么必须做：
#    浏览器的 getUserMedia 只在「安全上下文」可用 —— HTTPS 或 localhost。
#    纯 HTTP 访问时 navigator.mediaDevices 就是 undefined，电话会议点「加入会议」
#    必然报：无法访问麦克风：Cannot read properties of undefined (reading 'getUserMedia')。
#    参考实现（Laravel 版）同样如此：deploy-voice.sh 第 1 步就签发自签证书
#    （CN=服务器IP + SAN=IP），部署完打印的入口是 https://<IP>/project/<ID>/voice，
#    其规划文档也写明「getUserMedia 在 https 下才可用」。
#
#  为什么 OnlyOffice 也要 HTTPS：
#    HTTPS 页面再去加载 http://<IP>:8080/.../api.js 会被浏览器按「混合内容」直接拦掉，
#    编辑器变成白板。参考部署文档给的方式二即：DS 加一层 TLS 反代 + PM 也切 HTTPS。
#
#  端口安排（同一张自签证书，SAN 覆盖本机 IP，两个端口都生效）：
#    https://<IP>/         → PM（nginx 终止 TLS → gunicorn 127.0.0.1:8000）
#    https://<IP>:8443/    → OnlyOffice DS（nginx 反代 → 127.0.0.1:8080）
#  80 端口保持原样、不做跳转：OnlyOffice 容器是「服务器对服务器」回访
#    http://<IP>/collab/callback/...，一旦 301 到 443 会让回存失效。
#
#  幂等：可重复执行。证书已存在且 SAN 覆盖本机 IP 则复用；nginx 已配过则跳过。
#  回滚：删掉 /etc/nginx/conf.d/pm-ds-https.conf、把 pm.conf.bak-https 还原、
#        再把 .env 的 ONLYOFFICE_SERVER_URL 改回 http://<IP>:8080，reload 即可。
#
#  用法：bash deploy/setup-nginx-https.sh [服务器IP]
# ============================================================================
set -euo pipefail

SERVER_IP="${1:-$(hostname -I | awk '{print $1}' 2>/dev/null || true)}"
[ -n "$SERVER_IP" ] || { echo "拿不到服务器 IP，请显式传入：bash $0 192.168.2.102"; exit 1; }

SSL_DIR="/etc/pm/voice/ssl"                      # 与参考实现 / deploy-voice.sh 同路径（Janus 后续复用）
PM_DIR="/var/www/pm-django"
CRT="${SSL_DIR}/server.crt"
KEY="${SSL_DIR}/server.key"
PM_CONF="/etc/nginx/conf.d/pm.conf"
DS_CONF="/etc/nginx/conf.d/pm-ds-https.conf"
DS_HTTPS_PORT=8443
ENV_FILE="/var/www/pm-django/.env"

log()  { echo -e "\033[1;34m[INFO]\033[0m $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m $*"; }
err()  { echo -e "\033[1;31m[ERROR]\033[0m $*"; }
ok()   { echo -e "\033[1;32m[ OK ]\033[0m $*"; }

[ "$(id -u)" -eq 0 ] || { err "请用 root 运行"; exit 1; }
[ -f "$PM_CONF" ] || { err "找不到 $PM_CONF，请先执行 deploy/setup-nginx-80.sh"; exit 1; }

log "========== 启用 HTTPS（自签证书，IP: ${SERVER_IP}）=========="

# ---------------------------------------------------------------- [1/6] 证书
log "[1/6] 自签证书（CN/SAN = ${SERVER_IP}）..."
mkdir -p "$SSL_DIR"
if [ -s "$CRT" ] && [ -s "$KEY" ] && openssl x509 -in "$CRT" -noout -checkip "$SERVER_IP" >/dev/null 2>&1; then
    ok "复用已有证书（SAN 已覆盖 ${SERVER_IP}）：$CRT"
else
    # 必须带 subjectAltName：新版浏览器只看 SAN，不看 CN，缺了 SAN 会直接判定「证书无效」
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout "$KEY" -out "$CRT" \
        -subj "/CN=${SERVER_IP}" \
        -addext "subjectAltName=IP:${SERVER_IP},DNS:localhost" >/dev/null 2>&1
    chmod 600 "$KEY"
    chmod 644 "$CRT"
    ok "已签发自签证书：$CRT"
fi
openssl x509 -in "$CRT" -noout -subject -dates 2>/dev/null | sed 's/^/       /' || true

# ------------------------------------------- [1.5/6] 发布证书 + 客户机安装器
# 必须走 80 端口 http 分发：客户机此刻还没信任这张证书，用 https 下载会先撞上证书告警。
# 证书扩展已含 basicConstraints=CA:TRUE + SAN=IP，可直接被装成「受信任的根证书颁发机构」。
log "[1.5/6] 发布根证书与客户端一键安装器到 /static（客户机免告警下载）..."
install -m 644 "$CRT" "${PM_DIR}/static/pm-ca.crt"
if [ -f "${PM_DIR}/deploy/install-cert-client.bat" ]; then
    install -m 644 "${PM_DIR}/deploy/install-cert-client.bat" "${PM_DIR}/static/install-cert.bat"
    ok "客户机双击运行：http://<IP>/static/install-cert.bat   （证书：/static/pm-ca.crt）"
else
    ok "已发布证书：http://<IP>/static/pm-ca.crt"
fi

# ------------------------------------------------------- [2/6] PM 站点加 443
# 直接给 pm.conf 里既有的 server 块加 443 监听（沿用同一份 location，不做重复配置）
log "[2/6] 给 PM 站点加 443 监听（沿用 pm.conf 的同一份 location）..."
if grep -q 'listen[[:space:]]\+443 ssl' "$PM_CONF"; then
    ok "pm.conf 已含 443 监听，跳过"
else
    cp -a "$PM_CONF" "${PM_CONF}.bak-https"
    sed -i "s|^\([[:space:]]*\)listen[[:space:]]\+80 default_server;|\1listen       80 default_server;\n\1listen       443 ssl;\n\1listen       [::]:443 ssl;\n\1ssl_certificate     ${CRT};\n\1ssl_certificate_key ${KEY};|" "$PM_CONF"
    if grep -q 'listen[[:space:]]\+443 ssl' "$PM_CONF"; then
        ok "已插入 443 监听与证书（原文件备份：${PM_CONF}.bak-https）"
    else
        err "插入失败：pm.conf 结构可能已变，请手工在 server 块内加 listen 443 ssl + ssl_certificate"
        exit 1
    fi
fi

# --------------------------------------------- [3/6] DS 的 HTTPS 反代（8443）
log "[3/6] 生成 OnlyOffice DS 的 HTTPS 反代（:${DS_HTTPS_PORT} → 127.0.0.1:8080）..."
cat > "$DS_CONF" <<EOF
# OnlyOffice Document Server 的 HTTPS 反代：浏览器侧 https://<IP>:${DS_HTTPS_PORT}
# 由 deploy/setup-nginx-https.sh 生成（可重复执行、内容幂等）。
#
# 三个关键点，少一个编辑器就会白板或协同编辑失效：
#   1) proxy_set_header Host \$http_host —— DS 按 Host 头生成绝对地址（api.js 里的 baseUrl、
#      协同编辑用的 websocket 地址）。若只传 \$host（丢掉 :${DS_HTTPS_PORT}），浏览器会去连
#      http://<IP>:8080，在 https 页面里被当成混合内容拦掉；
#   2) Upgrade/Connection 透传 —— 编辑器协同编辑（其他人光标/输入实时同步）走 websocket；
#   3) PM_INTERNAL_URL 仍是 http://<IP> —— DS 容器回访 PM 走 80 端口，是服务端流量，
#      不经过浏览器，保持 http 最省事也最快。
server {
    listen       ${DS_HTTPS_PORT} ssl;
    listen       [::]:${DS_HTTPS_PORT} ssl;
    server_name  pm-ds;

    ssl_certificate     ${CRT};
    ssl_certificate_key ${KEY};
    ssl_protocols       TLSv1.2 TLSv1.3;

    charset utf-8;
    client_max_body_size 200m;

    access_log /var/log/nginx/pm-ds-access.log;
    error_log  /var/log/nginx/pm-ds-error.log warn;

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header   Host              \$http_host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_set_header   Upgrade           \$http_upgrade;
        proxy_set_header   Connection        \$http_connection;
        proxy_redirect     off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering    off;
    }
}
EOF
ok "已写入 $DS_CONF"

# ------------------------------------------------------------ [4/6] 防火墙
log "[4/6] 防火墙放行 443 / ${DS_HTTPS_PORT}..."
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port=443/tcp >/dev/null 2>&1 || true
    firewall-cmd --permanent --add-port=${DS_HTTPS_PORT}/tcp >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
    ok "已放行 TCP 443、${DS_HTTPS_PORT}（当前：$(firewall-cmd --list-ports 2>/dev/null | tr '\n' ' ')）"
else
    warn "firewalld 未运行，跳过（若用 iptables/nftables 请自行放行 443、${DS_HTTPS_PORT}）"
fi

# ------------------------------------------------- [5/6] 让 api.js 也走 https
log "[5/6] 把浏览器侧加载的 ONLYOFFICE_SERVER_URL 切到 https..."
if [ -f "$ENV_FILE" ]; then
    OLD_URL="$(grep -E '^ONLYOFFICE_SERVER_URL=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
    NEW_URL="https://${SERVER_IP}:${DS_HTTPS_PORT}"
    if [ "$OLD_URL" = "$NEW_URL" ]; then
        ok "已是 ${NEW_URL}，跳过"
    else
        if grep -q '^ONLYOFFICE_SERVER_URL=' "$ENV_FILE"; then
            sed -i "s|^ONLYOFFICE_SERVER_URL=.*|ONLYOFFICE_SERVER_URL=${NEW_URL}|" "$ENV_FILE"
        else
            echo "ONLYOFFICE_SERVER_URL=${NEW_URL}" >> "$ENV_FILE"
        fi
        ok "ONLYOFFICE_SERVER_URL：${OLD_URL:-（未设置）} → ${NEW_URL}"
    fi
    if grep -q '^ONLYOFFICE_PM_INTERNAL_URL=' "$ENV_FILE"; then
        ok "ONLYOFFICE_PM_INTERNAL_URL 保持不动（DS 容器在服务端回访 PM，不经浏览器）：$(grep -E '^ONLYOFFICE_PM_INTERNAL_URL=' "$ENV_FILE" | cut -d= -f2-)"
    fi
else
    warn "没找到 $ENV_FILE，请手工把 ONLYOFFICE_SERVER_URL 设为 https://${SERVER_IP}:${DS_HTTPS_PORT}"
fi

# ------------------------------------------------------- [6/6] 校验并加载
log "[6/6] 校验并加载 nginx 配置..."
nginx -t
systemctl reload nginx
if systemctl restart pm-django 2>/dev/null; then
    ok "pm-django 已重启（加载新的 .env）"
else
    warn "pm-django 重启失败，请手动检查 systemctl status pm-django"
fi
sleep 2

# ------------------------------------------------------------------ 自检
log "========== 自检 =========="
pm_https="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "https://${SERVER_IP}/" || echo 000)"
case "$pm_https" in
    200|301|302) ok "PM HTTPS：https://${SERVER_IP}/ → HTTP ${pm_https}" ;;
    *)           err "PM HTTPS 异常：HTTP ${pm_https}" ;;
esac

pm_http="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://${SERVER_IP}/" || echo 000)"
case "$pm_http" in
    200|301|302) ok "80 端口仍可用（DS 容器回访 PM 走这里，不能跳转）：HTTP ${pm_http}" ;;
    *)           err "80 端口异常：HTTP ${pm_http}" ;;
esac

ds_health="$(curl -sk --max-time 10 "https://${SERVER_IP}:${DS_HTTPS_PORT}/healthcheck" 2>/dev/null || true)"
case "$ds_health" in
    *true*) ok "DS HTTPS healthcheck：${ds_health}" ;;
    *)      err "DS HTTPS healthcheck 异常：${ds_health:-（无响应）} —— 检查 8080 是否在跑、Host 头是否用 \$http_host" ;;
esac

api_code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "https://${SERVER_IP}:${DS_HTTPS_PORT}/web-apps/apps/api/documents/api.js" || echo 000)"
case "$api_code" in
    200) ok "DS api.js（HTTPS）→ HTTP 200" ;;
    *)   err "DS api.js（HTTPS）→ HTTP ${api_code}" ;;
esac

echo
echo "========== 完成：浏览器请改用 HTTPS =========="
echo "  1) 打开 https://${SERVER_IP}/            → 证书页点「高级」→「继续前往」"
echo "  2) 打开 https://${SERVER_IP}:${DS_HTTPS_PORT}/        → 再信任一次（编辑器 api.js 走这个端口）"
echo "  3) 电话会议：https://${SERVER_IP}/project/<项目ID>/voice → 点「加入会议」就会弹麦克风授权"
echo
echo "  想彻底免告警（每台客户机装一次，443 / ${DS_HTTPS_PORT} / 3002 全免）："
echo "    方式一（推荐）：浏览器打开 http://${SERVER_IP}/static/install-cert.bat 下载后双击运行"
echo "                    → 弹 UAC 点「是」→ 完全重开浏览器即可"
echo "    方式二（手动）：下载 http://${SERVER_IP}/static/pm-ca.crt"
echo "                    → 双击 → 安装证书 → 本地计算机 → 受信任的根证书颁发机构"
echo "    注：Firefox 自带证书库，需在 about:config 把 security.enterprise_roots.enabled 设为 true"
