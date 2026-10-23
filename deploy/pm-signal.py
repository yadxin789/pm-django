#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PM 电话会议信令服务（WebSocket）。

替代参考实现里的 PHP Swoole `pm-signal`：直接对齐 web.views.voice._issue_token
签发的入场 token 和 static/js/voice-room.js 的协议，因此不需要改前端或后端业务代码。

协议摘要
--------
  客户端：ws = new WebSocket((r.signal_ws || SIGNAL_WS) + '?token=<jwt>')
  token   = b64url(json({uid,pid,rid,exp})).b64url(hmac_sha256(SECRET_KEY, body))

  上行：
    {type:'webrtc_offer',  to_uid, sdp}        → 转发给 to_uid
    {type:'webrtc_answer', to_uid, sdp}        → 转发给 to_uid
    {type:'webrtc_ice',    to_uid, candidate}  → 转发给 to_uid
    {type:'presenter_apply'}                   → 转发给本房主持人
    {type:'presenter_assign', to_uid}          → 仅主持人：设置演示者并广播
    {type:'presenter_revoke'}                  → 仅主持人：清除演示者并广播
    {type:'doc_select', doc_id}                → 仅主持人：广播 doc_selected
    {type:'slide_sync', scroll}                → 仅当前演示者：广播给其他成员

  下行：
    {type:'room_state', members:[{uid,name}, ...]}   连接时 / 成员变化时
    {type:'presenter_changed', presenter_uid}
    {type:'presenter_apply', from_uid, from_name}    仅发给主持人
    {type:'doc_selected', doc_id}
    {type:'slide_sync', scroll}
    {type:'webrtc_offer',  from_uid, sdp}
    {type:'webrtc_answer', from_uid, sdp}
    {type:'webrtc_ice',    from_uid, candidate}
    {type:'error', reason}

部署：deploy-voice.sh 会自动装 websockets、写 systemd 单元、配 nginx wss 反代、开通防火墙。
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import signal as os_signal
import sys
import time
from urllib.parse import parse_qs

# 在 import Django 模型前必须设好
PROJECT_DIR = os.environ.get('PM_PROJECT_DIR', '/var/www/pm-django')
sys.path.insert(0, PROJECT_DIR)


def _load_env_file():
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
# 关掉 Django 默认 logger（DEBUG 模式下 SQL / 请求日志会刷屏，跟信号日志混在一起）
logging.getLogger('django.db.backends').disabled = True
logging.getLogger('django.request').disabled = True
logging.getLogger('django.utils.autoreload').disabled = True
logging.getLogger('django.utils.autoreload').propagate = False

import django  # noqa: E402

django.setup()

from asgiref.sync import sync_to_async  # noqa: E402
from django.conf import settings  # noqa: E402

from core.models import User, VoiceRoom  # noqa: E402

HOST = os.environ.get('PM_SIGNAL_HOST', '127.0.0.1')
PORT = int(os.environ.get('PM_SIGNAL_PORT', '3001'))
LOG_LEVEL = os.environ.get('PM_SIGNAL_LOG_LEVEL', 'INFO')

logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger('pm-signal')


def _b64url_decode(s):
    padding = '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def verify_token(token):
    """校验 web.views.voice._issue_token() 签发的入场 token，返回 payload。"""
    if not token or '.' not in token:
        raise ValueError('token 格式错误')
    body, sig = token.rsplit('.', 1)
    secret = str(settings.SECRET_KEY).encode('utf-8')
    expected = base64.urlsafe_b64encode(
        hmac.new(secret, body.encode('utf-8'), hashlib.sha256).digest()
    ).rstrip(b'=').decode('ascii')
    if not hmac.compare_digest(sig, expected):
        raise ValueError('token 签名不匹配')
    try:
        payload = json.loads(_b64url_decode(body))
    except Exception as exc:
        raise ValueError(f'token 解析失败: {exc}') from exc
    if int(payload.get('exp', 0)) < int(time.time()):
        raise ValueError('token 已过期')
    for key in ('uid', 'pid', 'rid'):
        if key not in payload:
            raise ValueError(f'token 缺少 {key}')
    return payload


class Room:
    """单个会议房的状态。成员、演示者、当前共享文档都放内存里（重启就丢，会议是临时场景）。"""

    def __init__(self, room_id, project_id):
        self.room_id = room_id
        self.project_id = project_id
        self.members = {}        # uid -> {'ws','name','joined_at'}
        self.presenter_uid = None
        self.current_doc_id = None

    def add(self, uid, ws, name):
        # 同 uid 重复连入：直接覆盖（旧的 ws 在下一次广播/收消息时会失败被回收），
        # 千万不要在 on_disconnect 里再去 remove，否则会把新连接误踢掉。
        self.members[uid] = {'ws': ws, 'name': name, 'joined_at': time.time()}

    def remove_if_self(self, uid, ws):
        """只在「当前存储的就是这个 ws」时才移除，防止覆盖重连后把新连接误删。"""
        entry = self.members.get(uid)
        if entry is not None and entry['ws'] is ws:
            was_presenter = self.presenter_uid == uid
            self.members.pop(uid, None)
            if was_presenter:
                self.presenter_uid = None
            return True
        return False

    def member_list(self):
        return [{'uid': int(uid), 'name': m['name']} for uid, m in self.members.items()]

    async def send_to(self, uid, msg):
        m = self.members.get(uid)
        if not m:
            return False
        try:
            await m['ws'].send(json.dumps(msg, ensure_ascii=False))
            return True
        except Exception:
            # 发送失败说明对方 ws 已挂，标记为死连接由下次 broadcast 回收
            return False

    async def broadcast(self, msg, exclude_uid=None):
        payload = json.dumps(msg, ensure_ascii=False)
        dead = []
        for uid, m in self.members.items():
            if uid == exclude_uid:
                continue
            try:
                await m['ws'].send(payload)
            except Exception:
                dead.append(uid)
        for uid in dead:
            self.members.pop(uid, None)


class Server:
    def __init__(self):
        self.rooms = {}        # room_id -> Room
        self.host_uid = {}     # room_id -> host_uid（一次性查表缓存）
        self.user_name = {}    # uid -> name（缓存）

    def get_room(self, room_id, project_id):
        room = self.rooms.get(room_id)
        if room is None:
            room = Room(room_id, project_id)
            self.rooms[room_id] = room
        return room

    @sync_to_async
    def _lookup_user(self, uid):
        try:
            u = User.objects.get(pk=uid)
            return u.name or u.username or f'用户{uid}'
        except User.DoesNotExist:
            return f'用户{uid}'

    @sync_to_async
    def _lookup_host(self, room_id):
        try:
            vr = VoiceRoom.objects.get(pk=room_id)
            return int(vr.host_id) if vr.host_id else None
        except VoiceRoom.DoesNotExist:
            return None

    async def user_name_of(self, uid):
        if uid not in self.user_name:
            self.user_name[uid] = await self._lookup_user(uid)
        return self.user_name[uid]

    async def host_of(self, room_id):
        if room_id not in self.host_uid:
            self.host_uid[room_id] = await self._lookup_host(room_id)
        return self.host_uid[room_id]

    async def on_connect(self, ws, payload):
        uid = int(payload['uid'])
        project_id = int(payload['pid'])
        room_id = int(payload['rid'])
        name = await self.user_name_of(uid)
        room = self.get_room(room_id, project_id)
        room.add(uid, ws, name)
        log.info('connect uid=%s room=%s name=%s members=%d',
                 uid, room_id, name, len(room.members))
        # 给新人下发当前房间状态
        await ws.send(json.dumps({'type': 'room_state', 'members': room.member_list()},
                                 ensure_ascii=False))
        # 给其他人广播成员变化
        await room.broadcast({'type': 'room_state', 'members': room.member_list()},
                             exclude_uid=uid)

    async def on_message(self, ws, payload, msg):
        uid = int(payload['uid'])
        room_id = int(payload['rid'])
        room = self.rooms.get(room_id)
        if room is None or uid not in room.members:
            await ws.send(json.dumps({'type': 'error', 'reason': '未入会或已被踢'}))
            return
        t = msg.get('type')

        if t in ('webrtc_offer', 'webrtc_answer', 'webrtc_ice'):
            try:
                to_uid = int(msg.get('to_uid'))
            except (TypeError, ValueError):
                return
            relayed = dict(msg, from_uid=uid)
            relayed.pop('to_uid', None)
            await room.send_to(to_uid, relayed)

        elif t == 'presenter_apply':
            host = await self.host_of(room_id)
            if host is None:
                return
            await room.send_to(host, {
                'type': 'presenter_apply',
                'from_uid': uid,
                'from_name': room.members[uid]['name'],
            })

        elif t == 'presenter_assign':
            host = await self.host_of(room_id)
            if uid != host:
                await ws.send(json.dumps({'type': 'error', 'reason': '仅主持人可指派演示者'}))
                return
            try:
                new_uid = int(msg.get('to_uid'))
            except (TypeError, ValueError):
                return
            if new_uid not in room.members:
                return
            room.presenter_uid = new_uid
            await room.broadcast({'type': 'presenter_changed', 'presenter_uid': new_uid})

        elif t == 'presenter_revoke':
            host = await self.host_of(room_id)
            if uid != host:
                await ws.send(json.dumps({'type': 'error', 'reason': '仅主持人可收回演示权'}))
                return
            room.presenter_uid = None
            await room.broadcast({'type': 'presenter_changed', 'presenter_uid': None})

        elif t == 'doc_select':
            host = await self.host_of(room_id)
            if uid != host:
                await ws.send(json.dumps({'type': 'error', 'reason': '仅主持人可选择文档'}))
                return
            try:
                doc_id = int(msg.get('doc_id', 0))
            except (TypeError, ValueError):
                doc_id = 0
            room.current_doc_id = doc_id
            await room.broadcast({'type': 'doc_selected', 'doc_id': doc_id})

        elif t == 'slide_sync':
            # 仅当前演示者能广播滚动；其他人发来静默丢弃
            if room.presenter_uid != uid:
                return
            try:
                scroll = float(msg.get('scroll', 0))
            except (TypeError, ValueError):
                scroll = 0.0
            await room.broadcast({'type': 'slide_sync', 'scroll': scroll}, exclude_uid=uid)

        else:
            await ws.send(json.dumps({'type': 'error', 'reason': f'未知消息类型 {t!r}'}))

    async def on_disconnect(self, payload, ws):
        try:
            uid = int(payload['uid'])
            room_id = int(payload['rid'])
        except Exception:
            return
        room = self.rooms.get(room_id)
        if not room:
            return
        was_presenter = room.presenter_uid == uid
        removed = room.remove_if_self(uid, ws)
        if not removed:
            return  # 这个 ws 已经不是当前连接（被新的覆盖了）
        log.info('disconnect uid=%s room=%s members=%d',
                 uid, room_id, len(room.members))
        await room.broadcast({'type': 'room_state', 'members': room.member_list()})
        if was_presenter:
            await room.broadcast({'type': 'presenter_changed', 'presenter_uid': None})


SERVER = Server()


async def handler(ws):
    """WebSocket 连接入口。"""
    token = None
    payload = None
    try:
        # websockets 13+：ws.request 是 HTTP 请求对象；老版本兜底用 ws.path
        req = getattr(ws, 'request', None)
        if req is not None:
            q = req.query
            query_str = q.decode('utf-8') if isinstance(q, (bytes, bytearray)) else (q or '')
        else:
            query_str = ws.path.split('?', 1)[1] if '?' in getattr(ws, 'path', '') else ''
        params = parse_qs(query_str)
        token = params.get('token', [None])[0]
        payload = verify_token(token)
    except Exception as exc:
        log.warning('reject handshake: %s token=%r', exc, (token or '')[:24])
        try:
            await ws.send(json.dumps({'type': 'error', 'reason': '鉴权失败'}))
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
        return

    await SERVER.on_connect(ws, payload)
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                await ws.send(json.dumps({'type': 'error', 'reason': '非法 JSON'}))
                continue
            await SERVER.on_message(ws, payload, msg)
    except Exception as exc:
        # 客户端断网/连接异常，不致命
        log.info('ws loop exit uid=%s: %s', payload.get('uid'), exc)
    finally:
        await SERVER.on_disconnect(payload, ws)


async def main():
    import websockets
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (os_signal.SIGTERM, os_signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info('listening on ws://%s:%d', HOST, PORT)
    async with websockets.serve(handler, HOST, PORT, max_size=2 ** 20, ping_interval=30):
        await stop.wait()
    log.info('bye')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
