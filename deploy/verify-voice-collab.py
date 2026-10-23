# -*- coding: utf-8 -*-
"""电话会议模块的「协同办公能力」与协同办公模块的对齐验证。

要验证的对齐点（历史上两处各写了一份，逻辑不一致）：
  1. 同一个文档库：会议里新建/上传的文档落在 media/collab/<项目>/，并且立刻出现在协同办公列表里
  2. 生命周期一致：不再有 voice_demo/ 临时目录、不再写 is_temp_demo / expires_at(7天过期)
  3. 权限一致：管理员/项目经理/项目参与人都能进（原先会议页只认「项目参与人」，
     管理员反而进不去，而且非成员访问会因 Http404 未导入而 500）
  4. 能力一致：会议内可 新建(模板 docx/xlsx/pptx) / 上传 / 演示 / 编辑 / 下载 / 删除
  5. 编辑器配置一致：doc-config 复用同一套 build_editor_config + JWT 签名有效，
     并把「本场会议共享的文档」写入 voice_sessions.collab_document

结束后删除全部测试文档与文件（零残留）。
用法：/var/www/pm-django/venv/bin/python deploy/verify-voice-collab.py [项目ID，默认 1]
"""
import base64
import hashlib
import hmac
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

import django

PROJECT_DIR = os.environ.get('PM_PROJECT_DIR', '/var/www/pm-django')
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


def load_env_file(path='/var/www/pm-django/.env'):
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(os.path.join(PROJECT_DIR, '.env'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pm.settings')
django.setup()

from django.conf import settings                        # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile   # noqa: E402
from django.test import Client                          # noqa: E402

from core.models import CollabDocument, Project, User, VoiceRoom, VoiceSession   # noqa: E402
from web.views import collab as collab_views            # noqa: E402  与页面同一套权限判定

PK = int(sys.argv[1]) if len(sys.argv) > 1 else 1
NAME_A = '【自动化验证】会议新建'
NAME_B = '会议验证上传'   # 上传时未显式传 name，文档名取自文件名（与前端 JS 行为一致）
FAILED = 0
created_ids = []


def check(cond, msg):
    global FAILED
    if cond:
        print(f'  [OK]   {msg}')
    else:
        print(f'  [FAIL] {msg}')
        FAILED += 1
    return bool(cond)


def b64url_decode(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def jwt_valid(token, secret):
    try:
        head, body, sig = token.split('.')
    except ValueError:
        return False
    expect = hmac.new(secret.encode(), f'{head}.{body}'.encode(), hashlib.sha256).digest()
    return hmac.compare_digest(expect, b64url_decode(sig))


def main():
    project = Project.objects.filter(pk=PK).first()
    if not check(project is not None, f'项目 #{PK} 存在'):
        return 1
    admin = next((u for u in User.objects.all()
                  if u.is_admin() and collab_views._can_view_project(u, project)), None)
    if not check(admin is not None, '找到对项目有权限的管理员账号'):
        return 1
    participant_ids = set(project.participants().values_list('id', flat=True))
    # 「普通参与人」= 参与人里既不是项目创建者、也不是管理员/项目经理的人（否则按规则本来就能删）
    member = next((u for u in User.objects.all()
                   if u.id in participant_ids and u.id != project.creator_id
                   and not u.is_admin() and not u.is_project_manager()), None)
    # 非成员 = 既不是参与人、也不是管理员/项目经理（后两者按规则本就有权查看）
    outsider = next((u for u in User.objects.all()
                     if u.id != admin.id and u.id not in participant_ids
                     and not u.is_admin() and not u.is_project_manager()), None)
    print(f'  项目=#{project.id} {project.name}；管理员={admin.username}(id={admin.id})；'
          f'参与人={member.username if member else "无"}；非成员={outsider.username if outsider else "无"}')
    print(f'  管理员是否为项目参与人：{"是（无法据此验证豁免规则）" if admin.id in participant_ids else "否"}')

    # ---- 0) 部署环境：信令服务可达 ----
    print()
    print('== 0) 部署环境检查 ==')
    signal_ws = str(getattr(settings, 'VOICE_SIGNAL_WS', '') or '')
    check(signal_ws.startswith('wss://'),
          f'VOICE_SIGNAL_WS 已是 wss:// 形式（{signal_ws}）—— http 会被 https 页面当混合内容拦')
    # 探测：wss 端口用普通 HTTPS GET 应得 426 Upgrade Required（WebSocket 端点的标准响应）
    # urllib 不认 wss 协议，所以把协议换成 https 再探测
    if signal_ws.startswith('wss://'):
        probe_url = 'https://' + signal_ws[len('wss://'):]
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            resp = urllib.request.urlopen(probe_url, timeout=5, context=ctx)
            http_code = resp.status
        except urllib.error.HTTPError as exc:
            http_code = exc.code
        except Exception as exc:
            http_code = 0
        check(http_code == 426, f'信令端口 {signal_ws} 可达且响应 WebSocket 升级（HTTP {http_code}，期望 426）')
        # 提取 host：不是 127.0.0.1 / localhost（这两者只有本机浏览器能连，别的机器会全断）
        sig_host = signal_ws.split('://', 1)[1].split(':', 1)[0].split('/', 1)[0]
        check(sig_host not in ('127.0.0.1', 'localhost'),
              f'信令地址不是 127.0.0.1：{signal_ws}（127.0.0.1 只有本机浏览器能连，跨机器会议会全断）')

    cli = Client()
    cli.force_login(admin)

    print()
    print('== 1) 权限对齐：管理员（非项目参与人）也能进会议页与协同办公页')
    r_voice = cli.get(f'/project/{PK}/voice')
    r_collab = cli.get(f'/project/{PK}/collab')
    check(r_voice.status_code == 200, f'管理员访问 /voice → HTTP {r_voice.status_code}（此前是 500/403）')
    check(r_collab.status_code == 200, f'管理员访问 /collab → HTTP {r_collab.status_code}')

    if outsider is not None:
        cli2 = Client()
        cli2.force_login(outsider)
        o_voice = cli2.get(f'/project/{PK}/voice')
        o_collab = cli2.get(f'/project/{PK}/collab')
        check(o_voice.status_code == 404, f'非成员访问 /voice → HTTP {o_voice.status_code}（应为 404）')
        check(o_voice.status_code == o_collab.status_code == 404,
              f'非成员在 /voice 与 /collab 得到同样的结果（{o_voice.status_code} / {o_collab.status_code}）')

    print()
    print('== 2) 会议内新建文档：落库、落盘都在统一文档库')
    CollabDocument.objects.filter(project_id=PK, name__in=[NAME_A, NAME_B]).delete()
    resp = cli.post(f'/project/{PK}/voice/doc-store',
                    {'mode': 'template', 'name': NAME_A, 'extension': 'docx'})
    data = resp.json() if resp['Content-Type'].startswith('application/json') else {}
    check(resp.status_code == 200 and data.get('ok'), f'POST voice/doc-store（模板）→ HTTP {resp.status_code} {data}')
    doc_a = CollabDocument.objects.filter(project_id=PK, name=NAME_A).first()
    if check(doc_a is not None, '数据库生成了文档记录'):
        created_ids.append(doc_a.id)
        print(f'  记录 id={doc_a.id} 路径={doc_a.storage_path} 大小={doc_a.size} 版本=v{doc_a.version}')
        check(doc_a.storage_path.startswith(f'collab/{PK}/'),
              f'存放目录与协同办公一致（collab/{PK}/…，不再是 voice_demo/）')
        check(os.path.exists(os.path.join(settings.MEDIA_ROOT, doc_a.storage_path)), '文件已落盘')
        check(not doc_a.is_temp_demo and doc_a.expires_at is None, '不再是「临时演示/7 天过期」标记')

    print()
    print('== 3) 会议内上传文档：与协同办公同一套格式白名单、同一实现')
    upload = SimpleUploadedFile('会议验证上传.docx', b'PK\x03\x04' + b'0' * 64,
                                content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    resp = cli.post(f'/project/{PK}/voice/doc-store', {'mode': 'upload', 'file': upload})
    data = resp.json() if resp['Content-Type'].startswith('application/json') else {}
    check(resp.status_code == 200 and data.get('ok'), f'POST voice/doc-store（上传）→ HTTP {resp.status_code} {data}')
    doc_b = CollabDocument.objects.filter(project_id=PK, name='会议验证上传').first()
    if check(doc_b is not None, '上传生成了文档记录（名字取自文件名）'):
        created_ids.append(doc_b.id)
        check(doc_b.storage_path.startswith(f'collab/{PK}/'), '同样落在统一文档库')
    bad = SimpleUploadedFile('不支持的类型.pdf', b'%PDF-1.4', content_type='application/pdf')
    resp = cli.post(f'/project/{PK}/voice/doc-store', {'mode': 'upload', 'file': bad})
    check(resp.status_code == 422 and 'error' in resp.json(),
          f'非 docx/xlsx/pptx 被拒（HTTP {resp.status_code}：{resp.json().get("error")}）')

    print()
    print('== 4) 同一个文档库：会议新建的文档立刻出现在协同办公列表里')
    html = cli.get(f'/project/{PK}/collab').content.decode('utf-8', 'ignore')
    check(NAME_A in html, f'协同办公列表页里能看到《{NAME_A}》')
    html_voice = cli.get(f'/project/{PK}/voice').content.decode('utf-8', 'ignore')
    check(NAME_A in html_voice and NAME_B in html_voice, '会议页的「会议文档」面板里能看到这两份文档')
    check('js-doc-present' in html_voice and 'js-doc-delete' in html_voice,
          '会议文档面板提供了「演示 / 下载 / 删除」操作（与协同办公同能力）')
    check('btn-doc-create' in html_voice and 'doc-new-ext' in html_voice and 'file-upload' in html_voice,
          '会议文档面板提供了「新建（docx/xlsx/pptx）+ 上传」入口')
    if doc_a is not None:
        check(f'/project/{PK}/collab/{doc_a.id}/delete' in html,
              '协同办公列表页同样给出删除入口（权限标记两处共用同一份计算结果）')

    print()
    print('== 5) 编辑器配置对齐：同一套 build_editor_config + JWT 有效 + 记录会议共享文档')
    if doc_a is not None:
        room = VoiceRoom.for_project(project)
        session = VoiceSession.objects.filter(voice_room_id=room.id, ended_at__isnull=True).order_by('-id').first()
        cfg = cli.get(f'/project/{PK}/voice/doc-config/{doc_a.id}?edit=1').json()
        check('config' in cfg and cfg.get('server_url'), f'doc-config 返回配置（server_url={cfg.get("server_url")}）')
        check(cfg.get('doc', {}).get('name') == NAME_A, f'返回文档元信息（{cfg.get("doc", {}).get("name")}）')
        url = cfg.get('download_url', '')
        check(url.startswith(str(settings.ONLYOFFICE_PM_INTERNAL_URL).rstrip('/')),
              f'下载地址走 PM_INTERNAL_URL（DS 侧可达）：{url}')
        check('k=' + doc_a.doc_key in url, '下载地址带 doc_key')
        token = cfg.get('config', {}).get('token', '')
        check(token and jwt_valid(token, str(settings.ONLYOFFICE_JWT_SECRET)),
              '编辑器 token 签名与 JWT_SECRET 匹配（与协同办公用同一把钥匙）')
        if session is not None:
            session.refresh_from_db()
            check(session.collab_document_id == doc_a.id,
                  f'voice_sessions.collab_document 已记录本场会议的共享文档（session #{session.id}）')

    print()
    print('== 6) 删除对齐：会议内删除走同一权限判定与同一实现')
    if doc_b is not None:
        resp = cli.post(f'/project/{PK}/voice/doc-delete/{doc_b.id}')
        check(resp.status_code == 200 and resp.json().get('ok'), f'管理员删除《{NAME_B}》→ HTTP {resp.status_code}')
        check(CollabDocument.objects.filter(pk=doc_b.id).first() is None, '记录已删除')
        if doc_b.id in created_ids:
            created_ids.remove(doc_b.id)
    if doc_a is not None:
        if member is None:
            print('  [跳过] 该项目里没有「非创建者、非管理员」的普通参与人，无法验证越权删除')
        else:
            cli3 = Client()
            cli3.force_login(member)
            # 先打协同办公、再打电话会议：此时文档都还在，
            # 404（协同办公的拒绝方式）/ 403（会议的拒绝方式）才代表"权限不足"而不是"记录不存在"
            r1 = cli3.post(f'/project/{PK}/collab/{doc_a.id}/delete')
            r2 = cli3.post(f'/project/{PK}/voice/doc-delete/{doc_a.id}')
            check(r1.status_code == 404,
                  f'协同办公：普通参与人（{member.username}）越权删除被拒（HTTP {r1.status_code}）')
            check(r2.status_code == 403,
                  f'电话会议：同一人被同样拒绝（HTTP {r2.status_code}）——两处一致')
            check(CollabDocument.objects.filter(pk=doc_a.id).exists(),
                  '被拒之后文档仍然存在（没有"拒绝但已经删掉"）')

    print()
    print('== 7) 清理')
    # 统一走 TrashService.force_delete：记录、正文、history/ 快照一次性清理。
    # 不要 doc.delete()（软删）+ os.remove 的组合，那会留下死记录。
    from core.services.trash_service import force_delete
    room = VoiceRoom.for_project(project)
    VoiceSession.objects.filter(voice_room_id=room.id, collab_document_id__in=created_ids).update(collab_document=None)
    removed = []
    for d in CollabDocument.objects.filter(project_id=PK, name__in=[NAME_A, NAME_B, '会议验证上传']):
        removed.append(os.path.basename(d.storage_path or ''))
        force_delete(d)
    for did in created_ids:
        left = CollabDocument.objects.filter(pk=did).first()
        if left is not None:
            force_delete(left)
    print(f'  [OK]   已彻底删除测试文档与文件：{", ".join(r for r in removed if r) if removed else "（无）"}')

    print()
    if FAILED == 0:
        print('==== 电话会议「协同办公」能力对齐验证通过（FAIL=0）====')
        return 0
    print(f'==== 存在 {FAILED} 项 FAIL ====')
    return 1


if __name__ == '__main__':
    sys.exit(main())
