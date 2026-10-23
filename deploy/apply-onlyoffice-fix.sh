#!/usr/bin/env bash
# ============================================================================
#  OnlyOffice 协同办公配置修复（Django 迁移版 / openEuler 服务器）
#
#  修复浏览器端「协同编辑器加载失败：DocsAPI is not defined」以及紧随其后的
#  「下载失败 / JWT 校验失败」，共三处配置问题：
#
#    [1] settings.py 把 ONLYOFFICE_* 硬编码 → .env 里的配置根本不生效
#        改为 os.getenv(...)（.env 由 pm-django.service 的 EnvironmentFile 注入）
#    [2] .env 里 PM_INTERNAL_URL 用 host.docker.internal:8000，但 gunicorn 只监听
#        127.0.0.1:8000 → 容器访问不到 PM，DS 取文档/回存必然失败
#        改为经 Nginx 走 80 端口（http://<服务器IP>，不带端口）
#    [3] DS 容器启动时没有传 JWT_SECRET，而 DS 入口脚本在密钥为空时会「每次启动
#        随机生成」一个密钥写进 local.json → Django 侧签名必然不匹配
#        带显式 JWT_SECRET 重建容器（保留容器内已安装的中文字体）
#
#  幂等：可重复执行；改 settings.py 前留 .bak-onlyoffice 备份。
#  用法：bash apply-onlyoffice-fix.sh [服务器IP]     缺省自动探测
# ============================================================================
set -u

PM_DIR=/var/www/pm-django
ENV_FILE="$PM_DIR/.env"
SETTINGS="$PM_DIR/pm/settings.py"
DS_CONTAINER=onlyoffice
DS_PORT=8080
DS_IMAGE="onlyoffice/documentserver:latest"
JWT_SECRET="40c9ffd0643b6ec199ffbb41f1da030fbec4ccf59d0a2479ce1eed85cd7c91fe"
FONT_DIR="/usr/share/fonts/truetype/wqy"
FONT_STAGE="/root/pm-stage/wqy-fonts"
# 离线资产（deploy/prepare-offline-assets.sh 预置）：中文字体优先以只读卷挂进容器
HOST_FONT_DIR="/opt/pm-offline/fonts/wqy"

FAIL=0
info() { echo "[INFO]  $*"; }
ok()   { echo "[OK]    $*"; }
bad()  { echo "[FAIL]  $*"; FAIL=$((FAIL + 1)); }

SERVER_IP="${1:-}"
if [ -z "$SERVER_IP" ]; then
    SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
if [ -z "$SERVER_IP" ]; then
    echo "无法探测本机 IP，请把服务器 IP 作为第一个参数传入"
    exit 1
fi
DS_URL="http://${SERVER_IP}:${DS_PORT}"
PM_INTERNAL="http://${SERVER_IP}"

echo "==============================================================="
echo " PM 目录        : $PM_DIR"
echo " 浏览器访问 DS  : $DS_URL"
echo " 容器回访 PM    : $PM_INTERNAL"
echo "==============================================================="
echo

# ---------------------------------------------------------------- [1/5]
echo "== [1/5] settings.py 改为环境变量驱动"
if grep -q "os.getenv('ONLYOFFICE_SERVER_URL'" "$SETTINGS" 2>/dev/null; then
    ok "已是环境变量驱动，跳过"
elif ! grep -q '^ONLYOFFICE_SERVER_URL = ' "$SETTINGS" 2>/dev/null; then
    bad "$SETTINGS 中找不到 ONLYOFFICE_SERVER_URL 行，跳过（请人工检查）"
else
    cp -a "$SETTINGS" "${SETTINGS}.bak-onlyoffice"
    sed -i "s|^ONLYOFFICE_SERVER_URL = .*|ONLYOFFICE_SERVER_URL = os.getenv('ONLYOFFICE_SERVER_URL', 'http://localhost:8080')|" "$SETTINGS"
    sed -i "s|^ONLYOFFICE_JWT_SECRET = .*|ONLYOFFICE_JWT_SECRET = os.getenv('ONLYOFFICE_JWT_SECRET', '${JWT_SECRET}')|" "$SETTINGS"
    sed -i "s|^ONLYOFFICE_PM_INTERNAL_URL = .*|ONLYOFFICE_PM_INTERNAL_URL = os.getenv('ONLYOFFICE_PM_INTERNAL_URL', 'http://host.docker.internal:8000')|" "$SETTINGS"
    if grep -q "os.getenv('ONLYOFFICE_SERVER_URL'" "$SETTINGS" \
        && python3 -c "import ast;ast.parse(open('${SETTINGS}',encoding='utf-8').read())" 2>/dev/null; then
        ok "已改写（备份：${SETTINGS}.bak-onlyoffice），语法校验通过"
        grep -n 'ONLYOFFICE' "$SETTINGS" | sed 's/^/        /'
    else
        cp -a "${SETTINGS}.bak-onlyoffice" "$SETTINGS"
        bad "改写后校验失败，已回滚"
    fi
fi
echo

# ---------------------------------------------------------------- [2/5]
echo "== [2/5] 写入 ONLYOFFICE_* 到 .env"
touch "$ENV_FILE"
upsert_env() {   # $1=key $2=value
    local key="$1" value="$2"
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}
upsert_env ONLYOFFICE_SERVER_URL "$DS_URL"
upsert_env ONLYOFFICE_JWT_SECRET "$JWT_SECRET"
upsert_env ONLYOFFICE_PM_INTERNAL_URL "$PM_INTERNAL"
grep -n '^ONLYOFFICE' "$ENV_FILE" | sed 's/^/        /'
ok ".env 已更新"
echo

# ---------------------------------------------------------------- [3/5]
echo "== [3/5] DS 容器的 JWT 密钥"
CUR_SECRET="$(docker inspect "$DS_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | sed -n 's/^JWT_SECRET=//p' | head -n1)"

# 中文字体：有离线资产就用只读卷（重建容器不依赖容器内 apt），否则退回「容器内直拷」模式
FONT_ARGS=()
if [ -n "$(ls -A "$HOST_FONT_DIR" 2>/dev/null)" ]; then
    FONT_ARGS=(-v "${HOST_FONT_DIR}:${FONT_DIR}:ro")
fi
FONT_MOUNT_OK=1
if [ "${#FONT_ARGS[@]}" -gt 0 ]; then
    case "$(docker inspect "$DS_CONTAINER" --format '{{range .Mounts}}{{.Destination}} {{end}}' 2>/dev/null)" in
        *"$FONT_DIR"*) FONT_MOUNT_OK=1 ;;
        *)             FONT_MOUNT_OK=0 ;;
    esac
fi

if [ "${CUR_SECRET}" = "${JWT_SECRET}" ] && [ "$FONT_MOUNT_OK" = "1" ]; then
    ok "容器 JWT_SECRET 与 Django 一致，且中文字体已由只读卷挂载，无需重建"
else
    info "需要重建容器：JWT_SECRET='${CUR_SECRET}'（应为 '${JWT_SECRET}'）、字体只读卷=${FONT_MOUNT_OK}"
    info "→ 按 deploy/deploy-onlyoffice.sh 的参数重建，并保留中文字体"
    if [ "${#FONT_ARGS[@]}" -gt 0 ]; then
        ok "字体改由只读卷提供（${HOST_FONT_DIR}），无需从旧容器暂存"
    elif docker exec "$DS_CONTAINER" test -d "$FONT_DIR" 2>/dev/null; then
        rm -rf "$FONT_STAGE"
        mkdir -p "$FONT_STAGE"
        if docker cp "${DS_CONTAINER}:${FONT_DIR}/." "$FONT_STAGE/" 2>/dev/null; then
            ok "已暂存中文字体：$(du -sh "$FONT_STAGE" 2>/dev/null | cut -f1)"
        else
            info "字体暂存失败（不影响文档编辑，只影响渲染字体）"
        fi
    fi
    NEW_NAME="${DS_CONTAINER}-new"
    HAS_OLD=0
    if docker inspect "$DS_CONTAINER" >/dev/null 2>&1; then
        HAS_OLD=1
        docker stop "$DS_CONTAINER" >/dev/null 2>&1
        info "已暂停旧容器（释放 ${DS_PORT} 端口）"
    fi
    docker rm -f "$NEW_NAME" >/dev/null 2>&1

    # 注意：老版本 Docker（< 20.10）不支持 --add-host host.docker.internal:host-gateway，
    # 本方案 PM_INTERNAL_URL 直接写宿主机 IP，不需要该映射，故不传 --add-host。
    # 顺序上「先起新、再删旧」，新容器失败就回滚旧容器，避免出现服务空窗。
    if docker run -d --name "$NEW_NAME" \
        --restart=always \
        -p "${DS_PORT}:80" \
        ${FONT_ARGS[@]+"${FONT_ARGS[@]}"} \
        -e JWT_ENABLED=true \
        -e JWT_SECRET="$JWT_SECRET" \
        "$DS_IMAGE" >/dev/null 2>&1; then
        ok "新容器已启动（临时名 ${NEW_NAME}）"
        if [ "$HAS_OLD" = "1" ]; then
            docker rm -f "$DS_CONTAINER" >/dev/null 2>&1 && ok "旧容器已删除"
        fi
        if docker rename "$NEW_NAME" "$DS_CONTAINER" >/dev/null 2>&1; then
            ok "容器已接管名称 ${DS_CONTAINER}"
        else
            info "docker rename 不可用，改用最终名称重建"
            docker rm -f "$NEW_NAME" >/dev/null 2>&1
            if docker run -d --name "$DS_CONTAINER" \
                --restart=always \
                -p "${DS_PORT}:80" \
                ${FONT_ARGS[@]+"${FONT_ARGS[@]}"} \
                -e JWT_ENABLED=true \
                -e JWT_SECRET="$JWT_SECRET" \
                "$DS_IMAGE" >/dev/null 2>&1; then
                ok "容器已按最终名称重建"
            else
                bad "重建失败（docker images 检查镜像是否还在）"
            fi
        fi
    else
        bad "新容器创建失败（详见上方 docker 报错）"
        if [ "$HAS_OLD" = "1" ]; then
            docker start "$DS_CONTAINER" >/dev/null 2>&1 && info "已回滚：旧容器重新启动"
        fi
    fi
    info "等待 DS 就绪（最长 3 分钟）..."
    READY=0
    for _ in $(seq 1 36); do
        if curl -sf "http://127.0.0.1:${DS_PORT}/healthcheck" 2>/dev/null | grep -q true; then
            READY=1
            break
        fi
        sleep 5
    done
    if [ "$READY" = "1" ]; then
        ok "DS healthcheck 通过"
    else
        bad "DS healthcheck 未通过（排查：docker logs ${DS_CONTAINER} --tail 50）"
    fi
    if [ "${#FONT_ARGS[@]}" -gt 0 ]; then
        ok "中文字体由只读卷提供：${HOST_FONT_DIR} → ${FONT_DIR}（不依赖容器内 apt）"
    elif [ -d "$FONT_STAGE" ] && [ -n "$(ls -A "$FONT_STAGE" 2>/dev/null)" ]; then
        docker exec "$DS_CONTAINER" mkdir -p "$FONT_DIR" 2>/dev/null
        if docker cp "$FONT_STAGE/." "${DS_CONTAINER}:${FONT_DIR}/" 2>/dev/null; then
            ok "中文字体已回填：$(docker exec "$DS_CONTAINER" ls "$FONT_DIR" 2>/dev/null | tr '\n' ' ')"
        else
            bad "字体回填失败"
        fi
    fi
    # 容器内没有 fc-cache，用 DS 自带的脚本重建字体表（该脚本自身会重启 docservice/converter）
    if docker exec "$DS_CONTAINER" test -x /usr/bin/documentserver-generate-allfonts.sh 2>/dev/null; then
        docker exec "$DS_CONTAINER" documentserver-generate-allfonts.sh >/dev/null 2>&1 \
            && ok "字体表已重建（documentserver-generate-allfonts.sh）" \
            || info "字体表重建异常（只影响字体渲染，不影响文档内容）"
    fi
fi
echo

# ---------------------------------------------------------------- [4/5]
echo "== [4/5] 一致性校验"

DS_SECRET="$(docker exec "$DS_CONTAINER" cat /etc/onlyoffice/documentserver/local.json 2>/dev/null \
    | sed -n 's/.*"string": "\([^"]*\)".*/\1/p' | head -n1)"
if [ "${DS_SECRET}" = "${JWT_SECRET}" ]; then
    ok "DS 生效密钥与 Django 一致（${JWT_SECRET:0:8}…）"
else
    bad "DS 生效密钥='${DS_SECRET}' 与 Django='${JWT_SECRET}' 不一致"
fi

API_JS="$(curl -s -o /dev/null -w '%{http_code}' "${DS_URL}/web-apps/apps/api/documents/api.js" 2>/dev/null)"
if [ "${API_JS}" = "200" ]; then
    ok "api.js 可访问：${DS_URL}/web-apps/apps/api/documents/api.js"
else
    bad "api.js 返回 HTTP ${API_JS}（浏览器将报 DocsAPI is not defined）"
fi

FONT_MOUNTED="$(docker inspect "$DS_CONTAINER" --format '{{range .Mounts}}{{.Destination}} {{end}}' 2>/dev/null)"
case "${FONT_MOUNTED}" in
    *"$FONT_DIR"*) ok "中文字体已挂载：${HOST_FONT_DIR} → ${FONT_DIR}（离线重建无需 apt）" ;;
    *)             info "未挂载本机字体（当前为容器内直拷模式）；建议先跑 deploy/prepare-offline-assets.sh" ;;
esac

PM_CODE="$(docker exec "$DS_CONTAINER" curl -s -o /dev/null -w '%{http_code}' "${PM_INTERNAL}/" 2>/dev/null)"
case "${PM_CODE}" in
    200|302|403|404) ok "容器可回访 PM：${PM_INTERNAL} 返回 HTTP ${PM_CODE}" ;;
    *)               bad "容器回访 ${PM_INTERNAL} 返回 HTTP ${PM_CODE}（DS 取文档/回存会失败）" ;;
esac
echo

# ---------------------------------------------------------------- [5/5]
echo "== [5/5] 重启 pm-django 让配置生效"
if systemctl restart pm-django; then
    sleep 3
    ok "pm-django 已重启，当前状态：$(systemctl is-active pm-django)"
else
    bad "pm-django 重启失败（journalctl -u pm-django -n 50）"
fi
echo

echo "==============================================================="
if [ "$FAIL" = "0" ]; then
    echo " 修复完成，FAIL=0"
else
    echo " 修复完成，但有 ${FAIL} 项 FAIL（见上文）"
fi
echo " 清缓存后重试浏览器：Ctrl+Shift+R 打开 项目 → 协同办公 → 文档"
echo " 进一步验证：/var/www/pm-django/venv/bin/python /root/pm-stage/verify-collab.py 1"
echo "==============================================================="
exit 0
