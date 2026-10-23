#!/usr/bin/env bash
# ============================================================================
#  OnlyOffice 协同办公「离线资产」预置脚本（在 PM 服务器上执行）
#
#  目的：把这套协同办公所需的一切搬到本机，之后服务器无外网也能部署/重建：
#    [1] 中文字体    → /opt/pm-offline/fonts/wqy
#                      DS 容器以只读卷挂载该目录（-v ...:/usr/share/fonts/truetype/wqy:ro），
#                      重建容器不再需要容器内 apt-get（无外网时 apt 必失败）
#    [2] Python 依赖 → /opt/pm-offline/wheels（pip 离线源，含全部传递依赖）
#                      离线安装：venv/bin/pip install --no-index --find-links=/opt/pm-offline/wheels -r requirements.txt
#    [3] DS 镜像     → /opt/pm-offline/images/onlyoffice-documentserver-latest.tar
#                      离线导入：docker load -i <tar>
#
#  幂等：已有资产默认跳过；要重新生成用 --force
#  用法：bash prepare-offline-assets.sh [--force]
# ============================================================================
set -u

OFFLINE_ROOT=/opt/pm-offline
FONT_DIR="$OFFLINE_ROOT/fonts/wqy"
WHEEL_DIR="$OFFLINE_ROOT/wheels"
IMAGE_DIR="$OFFLINE_ROOT/images"
PM_DIR=/var/www/pm-django
DS_CONTAINER=onlyoffice
DS_IMAGE="onlyoffice/documentserver:latest"
STAGE_FONTS=/root/pm-stage/wqy-fonts
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

FAIL=0
info() { echo "[INFO]  $*"; }
ok()   { echo "[OK]    $*"; }
bad()  { echo "[FAIL]  $*"; FAIL=$((FAIL + 1)); }

mkdir -p "$FONT_DIR" "$WHEEL_DIR" "$IMAGE_DIR"

echo "==============================================================="
echo " 离线资产根目录：$OFFLINE_ROOT"
echo "==============================================================="
echo

# ---------------------------------------------------------------- [1]
echo "== [1/3] 中文字体 → $FONT_DIR"
if [ "$FORCE" = "0" ] && [ -n "$(ls -A "$FONT_DIR" 2>/dev/null)" ]; then
    ok "已存在，跳过"
else
    if docker exec "$DS_CONTAINER" test -d /usr/share/fonts/truetype/wqy 2>/dev/null; then
        docker cp "${DS_CONTAINER}:/usr/share/fonts/truetype/wqy/." "$FONT_DIR/" 2>/dev/null && ok "已从运行中的容器导出"
    elif [ -d "$STAGE_FONTS" ] && [ -n "$(ls -A "$STAGE_FONTS" 2>/dev/null)" ]; then
        cp -a "$STAGE_FONTS/." "$FONT_DIR/" && ok "已从暂存目录复制（$STAGE_FONTS）"
    else
        bad "找不到字体来源（容器内 /usr/share/fonts/truetype/wqy 与 $STAGE_FONTS 都不存在）"
    fi
fi
if [ -n "$(ls -A "$FONT_DIR" 2>/dev/null)" ]; then
    info "字体文件：$(ls -1 "$FONT_DIR" | tr '\n' ' ')"
    info "目录大小：$(du -sh "$FONT_DIR" 2>/dev/null | cut -f1)"
fi

echo
# ---------------------------------------------------------------- [2]
echo "== [2/3] Python 依赖 wheel 包 → $WHEEL_DIR"
WHEEL_COUNT="$(ls -1 "$WHEEL_DIR"/*.whl 2>/dev/null | wc -l)"
if [ "$FORCE" = "0" ] && [ "$WHEEL_COUNT" -ge 30 ]; then
    ok "wheel 已齐（${WHEEL_COUNT} 个），跳过下载"
else
    info "从 PyPI 下载（需要外网，含全部传递依赖）..."
    if "$PM_DIR/venv/bin/pip" download --disable-pip-version-check \
        -r "$PM_DIR/requirements.txt" -d "$WHEEL_DIR" >/tmp/pm-wheel-download.log 2>&1; then
        ok "下载完成：$(ls -1 "$WHEEL_DIR"/*.whl 2>/dev/null | wc -l) 个 whl，$(du -sh "$WHEEL_DIR" 2>/dev/null | cut -f1)"
    else
        bad "下载失败，日志尾部："
        tail -n 8 /tmp/pm-wheel-download.log | sed 's/^/        /'
    fi
fi

echo
echo "== [2/3b] 离线安装自检（临时 venv，全程 --no-index 不访问网络）"
if [ -n "$(ls -A "$WHEEL_DIR" 2>/dev/null)" ]; then
    rm -rf /tmp/pm-offline-verify
    if python3 -m venv /tmp/pm-offline-verify >/dev/null 2>&1; then
        if /tmp/pm-offline-verify/bin/pip install --no-index --find-links="$WHEEL_DIR" \
            --disable-pip-version-check -r "$PM_DIR/requirements.txt" >/tmp/pm-offline-install.log 2>&1; then
            ok "离线安装通过（requirements.txt 全部可离线装上）"
        else
            bad "离线安装失败，日志尾部："
            tail -n 8 /tmp/pm-offline-install.log | sed 's/^/        /'
        fi
        rm -rf /tmp/pm-offline-verify
    else
        bad "python3 -m venv 不可用（是否缺 python3-venv？）"
    fi
fi

echo
# ---------------------------------------------------------------- [3]
echo "== [3/3] DS 镜像离线包 → $IMAGE_DIR"
IMAGE_TAR="$IMAGE_DIR/onlyoffice-documentserver-latest.tar"
if [ "$FORCE" = "0" ] && [ -s "$IMAGE_TAR" ]; then
    ok "已存在，跳过：$(du -sh "$IMAGE_TAR" 2>/dev/null | cut -f1)"
else
    if docker image inspect "$DS_IMAGE" >/dev/null 2>&1; then
        info "docker save 导出镜像（1~3GB，约 1-3 分钟）..."
        if docker save "$DS_IMAGE" -o "$IMAGE_TAR"; then
            ok "已导出：$(du -sh "$IMAGE_TAR" 2>/dev/null | cut -f1)"
            docker image inspect "$DS_IMAGE" --format '镜像 ID: {{.Id}}' 2>/dev/null | sed 's/^/        /'
        else
            bad "docker save 失败"
        fi
    else
        bad "本机没有镜像 $DS_IMAGE（先 docker pull，或导入别处拷来的离线 tar）"
    fi
fi

echo
echo "== 资产清单（sha256，便于校验/拷到别的机器）"
if ( cd "$OFFLINE_ROOT" && find . -type f -size +1k -exec sha256sum {} + >"$OFFLINE_ROOT/MANIFEST.sha256" 2>/dev/null ); then
    info "已写出 $OFFLINE_ROOT/MANIFEST.sha256（$(wc -l <"$OFFLINE_ROOT/MANIFEST.sha256" 2>/dev/null || echo 0) 行）"
fi
echo
echo "目录占用："
du -sh "$OFFLINE_ROOT"/* 2>/dev/null | sed 's/^/        /'
echo
echo "==============================================================="
if [ "$FAIL" = "0" ]; then
    echo " 离线资产预置完成，FAIL=0"
else
    echo " 离线资产预置完成，有 ${FAIL} 项 FAIL（见上文）"
fi
echo " 离线重装协同办公的顺序："
echo "   1) docker load -i /opt/pm-offline/images/onlyoffice-documentserver-latest.tar"
echo "   2) bash /var/www/pm-django/deploy/deploy-onlyoffice.sh   # 已改为挂载本机字体，不再依赖外网"
echo "   3) venv/bin/pip install --no-index --find-links=/opt/pm-offline/wheels -r requirements.txt"
echo "==============================================================="
exit 0
