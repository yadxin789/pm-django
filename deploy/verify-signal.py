# -*- coding: utf-8 -*-
"""PM 信令服务 E2E 验证。

覆盖：
  1) Django 端 /voice/token 视图能签出 web.views.voice._issue_token 同款 JWT
  2) 两个真实 WS 客户端（主持 + 成员）连到 wss://<IP>:3001，按 voice-room.js 的
     协议发送/接收消息，验证全部点对点转发、主持人鉴权、广播、断开清理

跑法（服务器上）：
    /var/www/pm-django/venv/bin/python /var/www/pm-django/deploy/verify-signal.py [服务器IP]
"""
import asyncio
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

import django

PROJECT_DIR = os.environ.get('PM_PROJECT_DIR', '/var/www/pm-django')
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


def _load_env_file():
    """模拟 systemd 的 EnvironmentFile：把 .env 注入 os.environ。
    否则 settings.py 里 os.getenv('VOICE_SIGNAL_WS', ...) 拿不到 .env 里的值，"""
    env = PROJECT_DIR + '/.env'
    if not os.path.exists(env):
        return
    with open(env, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pm.settings')
django.setup()

from asgiref.sync import sync_to_async                  # noqa: E402
from django.contrib.auth import get_user_model          # noqa: E402
from django.test import Client                           # noqa: E402

from core.models import Project, VoiceRoom, User         # noqa: E402
from web.views.voice import _issue_token                 # noqa: E402

HOST = sys.argv[1] if len(sys.argv) > 1 else '127.0.0.1'
SIGNAL_WS = f'wss://{HOST}:3002'
PM_HTTP = f'http://{HOST}'
FAILED = 0


def check(cond, msg):
    global FAILED
    if cond:
        print(f'  [OK]   {msg}')
    else:
        print(f'  [FAIL] {msg}')
        FAILED += 1
    return bool(cond)


@sync_to_async
def get_test_data():
    p = Project.objects.filter(pk=1).first()
    if p is None:
        return None
    User = get_user_model()
    host = User.objects.get(pk=p.creator_id)
    members = list(p.participants())
    if host in members:
        members.remove(host)
    room = VoiceRoom.for_project(p)
    return p, host, members, room


async def expect_message(ws, predicate, timeout=5, label=''):
    """等一条满足 predicate(msg) 的消息（其他消息可丢弃，便于忽略时序噪声）。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return None
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if predicate(msg):
            return msg


async def fetch_token_via_http(username, password, project_id):
    """真实走 http 拿入场 token：覆盖 Nginx + Django + token 视图。"""
    jar = http.cookiejar.CookieJar()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=ctx),
    )
    # 1) GET 登录页拿 csrftoken
    login_url = f'{PM_HTTP}/login'
    try:
        opener.open(login_url, timeout=10).read()
    except urllib.error.HTTPError as exc:
        if exc.code not in (200, 405):
            return None
    csrf = next((c.value for c in jar if c.name == 'csrftoken'), None)
    if not csrf:
        return None
    # 2) POST 登录
    data = urllib.parse.urlencode({
        'username': username, 'password': password, 'csrfmiddlewaretoken': csrf,
    }).encode()
    req = urllib.request.Request(login_url, data=data, method='POST',
                                 headers={'Referer': login_url, 'X-CSRFToken': csrf})
    try:
        opener.open(req, timeout=10)
    except urllib.error.HTTPError as exc:
        return {'error': f'login HTTP {exc.code}'}
    except Exception as exc:
        return {'error': str(exc)}
    # 3) 拿 token
    token_url = f'{PM_HTTP}/project/{project_id}/voice/token'
    try:
        resp = opener.open(token_url, timeout=10)
        return json.loads(resp.read().decode('utf-8', 'ignore'))
    except urllib.error.HTTPError as exc:
        return {'error': f'token HTTP {exc.code}'}
    except Exception as exc:
        return {'error': str(exc)}


async def main():
    import websockets
    print(f'目标：signal={SIGNAL_WS}  PM={PM_HTTP}')

    data = await get_test_data()
    if not data:
        print('  [FAIL] 找不到项目 #1 或其创建者'); return 1
    project, host_user, members, room = data
    check(len(members) > 0, f'项目 #1 至少 1 个非主持人参与人（实际 {len(members)}）')
    member_user = members[0] if members else None

    # ---- 1) Django 端：token 视图 ----
    # Client.force_login / session 触 DB，必须放在 async 上下文之外
    print()
    print('== 1) /voice/token 端点（用 Django test client，覆盖 token 视图）')

    def _check_token_endpoint():
        test_client = Client()
        test_client.force_login(host_user)
        resp = test_client.get(f'/project/{project.id}/voice/token')
        return resp.status_code, (resp.json() if resp.headers.get('Content-Type', '').startswith('application/json') else {})

    status_code, body = await sync_to_async(_check_token_endpoint)()
    check(status_code == 200, f'GET /project/{project.id}/voice/token → HTTP {status_code}')
    check('token' in body, f'返回含 token：token={body.get("token", "")[:30]}...')
    check(body.get('room', {}).get('host_id') == host_user.id,
          f'返回的 room.host_id == 主持人 uid（{body.get("room", {}).get("host_id")} == {host_user.id}）')
    check(str(body.get('signal_ws', '')).startswith('wss://'),
          f'signal_ws 已是 wss:// 形式：{body.get("signal_ws")}')

    # ---- 2) HTTP 真实链路 ----
    # 注意：HTTP 登录拿 token 的链路已由 verify-https-voice.py / verify-collab.py
    # 覆盖（cookie + CSRF + GET 端点都验过）。本脚本专注「WS 协议本身」，
    # 不重复测登录态。
    print()
    print('== 2) HTTP 链路：跳过（与 verify-https-voice.py 重叠；本脚本专注 WS 协议）')

    # ---- 3) WS 协议 ----
    print()
    print(f'== 3) WS 协议：两个真实客户端连 {SIGNAL_WS}')
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    @sync_to_async
    def issue(uid):
        return _issue_token(uid, project.id, room.id)

    host_token = await issue(host_user.id)
    member_token = await issue(member_user.id) if member_user else None

    host_ws = await websockets.connect(SIGNAL_WS + '?token=' + host_token,
                                       ssl=ctx, open_timeout=10)
    check(host_ws.state.name == 'OPEN', f'主持人 WS 已连接（state={host_ws.state.name}）')

    rs = await expect_message(host_ws, lambda m: m.get('type') == 'room_state', label='主持人 room_state')
    check(rs is not None and isinstance(rs.get('members'), list),
          f'主持人收到 room_state：members={len(rs.get("members", [])) if rs else 0}')

    member_ws = None
    if member_token:
        member_ws = await websockets.connect(SIGNAL_WS + '?token=' + member_token,
                                              ssl=ctx, open_timeout=10)
        check(member_ws.state.name == 'OPEN', '成员 WS 已连接')
        rs2 = await expect_message(member_ws, lambda m: m.get('type') == 'room_state', label='成员 room_state')
        check(rs2 is not None and len(rs2.get('members', [])) >= 1,
              f'成员收到 room_state：members={len(rs2.get("members", [])) if rs2 else 0}')
        # 主持人这边应当收到「成员 +1」的 room_state 广播
        rs3 = await expect_message(host_ws, lambda m: m.get('type') == 'room_state' and len(m.get('members', [])) >= 2)
        check(rs3 is not None, f'主持人收到 room_state 广播（含新成员）：members={len(rs3.get("members", [])) if rs3 else 0}')

    if member_ws:
        # 成员申请演示 → 主持人收到
        await member_ws.send(json.dumps({'type': 'presenter_apply'}))
        apply = await expect_message(host_ws, lambda m: m.get('type') == 'presenter_apply')
        check(apply is not None and apply.get('from_uid') == member_user.id,
              f'主持人收到 presenter_apply：from_uid={apply.get("from_uid") if apply else None} from_name={apply.get("from_name") if apply else None}')

        # 主持人指派 → 双方都收到 presenter_changed
        await host_ws.send(json.dumps({'type': 'presenter_assign', 'to_uid': member_user.id}))
        ev_m = await expect_message(member_ws, lambda m: m.get('type') == 'presenter_changed')
        ev_h = await expect_message(host_ws, lambda m: m.get('type') == 'presenter_changed')
        check(ev_m is not None and ev_m.get('presenter_uid') == member_user.id,
              f'成员收到 presenter_changed：{ev_m}')
        check(ev_h is not None and ev_h.get('presenter_uid') == member_user.id,
              f'主持人收到 presenter_changed：{ev_h}')

        # 非主持人发 doc_select → 收到 error
        await member_ws.send(json.dumps({'type': 'doc_select', 'doc_id': 1}))
        err = await expect_message(member_ws, lambda m: m.get('type') == 'error')
        check(err is not None and '仅主持人' in err.get('reason', ''),
              f'非主持人 doc_select 被拒：{err}')

        # 主持人选文档 → 成员收到 doc_selected
        await host_ws.send(json.dumps({'type': 'doc_select', 'doc_id': 99}))
        ds = await expect_message(member_ws, lambda m: m.get('type') == 'doc_selected')
        check(ds is not None and ds.get('doc_id') == 99, f'成员收到 doc_selected：{ds}')

        # P2P 转发：host → member 的 webrtc_offer
        await host_ws.send(json.dumps({'type': 'webrtc_offer', 'to_uid': member_user.id,
                                        'sdp': {'type': 'offer', 'sdp': 'v=0\r\n...'}}))
        offer = await expect_message(member_ws, lambda m: m.get('type') == 'webrtc_offer')
        check(offer is not None and offer.get('from_uid') == host_user.id,
              f'P2P 转发：成员收到 host 的 webrtc_offer，from_uid={offer.get("from_uid") if offer else None}')

        # 演示者发 slide_sync → 主持人收到，演示者自己收不到
        await member_ws.send(json.dumps({'type': 'slide_sync', 'scroll': 0.42}))
        slide = await expect_message(host_ws, lambda m: m.get('type') == 'slide_sync')
        check(slide is not None and abs(slide.get('scroll', -1) - 0.42) < 1e-6,
              f'主持人收到 slide_sync：scroll={slide.get("scroll") if slide else None}')
        # 同时演示者（成员）不应该收到自己的 slide_sync
        try:
            self_echo = await asyncio.wait_for(member_ws.recv(), timeout=1.5)
            try:
                parsed = json.loads(self_echo)
                check(parsed.get('type') != 'slide_sync', f'演示者不应收到自己的 slide_sync（实际收到 {parsed.get("type")}）')
            except Exception:
                check(True, '演示者未收到自己的 slide_sync（收到非 JSON 帧，忽略）')
        except asyncio.TimeoutError:
            check(True, '演示者未收到自己的 slide_sync（超时，符合预期）')

        # 演示者收回 → 双方收到 presenter_changed(null)
        await host_ws.send(json.dumps({'type': 'presenter_revoke'}))
        ev_m = await expect_message(member_ws, lambda m: m.get('type') == 'presenter_changed')
        ev_h = await expect_message(host_ws, lambda m: m.get('type') == 'presenter_changed')
        check(ev_m is not None and ev_m.get('presenter_uid') is None,
              f'主持人 revoke → 成员收到 presenter_changed(presenter_uid=null)：{ev_m}')
        check(ev_h is not None and ev_h.get('presenter_uid') is None,
              f'主持人 revoke → 主持人自己收到 presenter_changed(presenter_uid=null)：{ev_h}')

        # 成员断开 → 主持人收到 room_state（成员 -1）
        await member_ws.close()
        ev = await expect_message(host_ws, lambda m: m.get('type') == 'room_state' and len(m.get('members', [])) == 1)
        check(ev is not None, f'成员断开后主持人收到 room_state：members={len(ev.get("members", [])) if ev else 0}')

    await host_ws.close()

    print()
    if FAILED == 0:
        print('==== 信令服务 E2E 验证通过（FAIL=0）====')
        return 0
    print(f'==== 存在 {FAILED} 项 FAIL ====')
    return 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
