# -*- coding: utf-8 -*-
"""企业微信服务：对齐 Laravel WecomService.php（token/消息/素材/通讯录）。"""
import json
import time

import requests

from core.models import WecomConfig

API_BASE = 'https://qyapi.weixin.qq.com'
_cache = {}


def _active_config():
    return WecomConfig.objects.filter(is_active=True).first()


def is_enabled():
    cfg = _active_config()
    return bool(cfg and cfg.corp_id and cfg.secret)


def get_config():
    return _active_config()


def get_access_token():
    cfg = _active_config()
    if not cfg:
        raise RuntimeError('企业微信未配置')
    cache_key = 'wecom_access_token_%s' % cfg.corp_id
    cached = _cache.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0]
    resp = api_get('/cgi-bin/gettoken', {'corpid': cfg.corp_id, 'corpsecret': cfg.secret}, use_token=False)
    if (resp.get('errcode', -1)) != 0:
        raise RuntimeError('获取企微 access_token 失败: ' + resp.get('errmsg', 'unknown'))
    token = resp['access_token']
    _cache[cache_key] = (token, time.time() + 7000)
    return token


def build_oauth_url(redirect_url, state):
    cfg = _active_config()
    corp_id = cfg.corp_id
    from urllib.parse import quote
    return ('https://open.weixin.qq.com/connect/oauth2/authorize?appid=%s'
            '&redirect_uri=%s&response_type=code&scope=snsapi_base&state=%s#wechat_redirect'
            % (corp_id, quote(redirect_url, safe=''), quote(state)))


def get_user_info_by_code(code):
    return api_get('/cgi-bin/auth/getuserinfo', {'code': code})


def send_text_card(touser, title, description, url, btntxt='查看详情'):
    cfg = _active_config()
    payload = {
        'touser': touser, 'agentid': int(cfg.agent_id), 'msgtype': 'textcard',
        'textcard': {'title': title[:128], 'description': description[:512], 'url': url, 'btntxt': btntxt},
        'enable_id_trans': 1, 'enable_duplicate_check': 1, 'duplicate_check_interval': 1800,
    }
    return api_post('/cgi-bin/message/send', payload)


def send_text(touser, content):
    cfg = _active_config()
    payload = {
        'touser': touser, 'agentid': int(cfg.agent_id), 'msgtype': 'text',
        'text': {'content': content}, 'enable_id_trans': 1, 'enable_duplicate_check': 1,
    }
    return api_post('/cgi-bin/message/send', payload)


def upload_media(media_type, file_path, filename=None):
    if not __import__('os').path.exists(file_path):
        return {'errcode': -1, 'errmsg': '文件不存在: ' + file_path}
    allowed = ['image', 'voice', 'video', 'file']
    if media_type not in allowed:
        return {'errcode': -1, 'errmsg': '不支持的素材类型: ' + media_type}
    filename = filename or __import__('os').path.basename(file_path)
    token = get_access_token()
    with open(file_path, 'rb') as f:
        files = {'media': (filename, f)}
        try:
            r = requests.post('%s/cgi-bin/media/upload' % API_BASE,
                              params={'access_token': token, 'type': media_type},
                              files=files, timeout=30)
            return r.json()
        except Exception as e:
            return {'errcode': -1, 'errmsg': str(e)}


def get_media(media_id, save_path):
    try:
        token = get_access_token()
    except RuntimeError as e:
        return {'success': False, 'error': str(e)}
    import os
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    try:
        r = requests.get('%s/cgi-bin/media/get' % API_BASE,
                         params={'access_token': token, 'media_id': media_id},
                         stream=True, timeout=60)
        ctype = r.headers.get('Content-Type', '')
        if 'json' in ctype or 'text/plain' in ctype:
            body = r.json()
            return {'success': False, 'error': body.get('errmsg', '未知下载错误')}
        with open(save_path, 'wb') as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return {'success': True, 'path': save_path, 'size': os.path.getsize(save_path)}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def send_file_message(touser, media_id):
    cfg = _active_config()
    payload = {
        'touser': touser, 'agentid': int(cfg.agent_id), 'msgtype': 'file',
        'file': {'media_id': media_id}, 'enable_id_trans': 1,
        'enable_duplicate_check': 1, 'duplicate_check_interval': 1800,
    }
    return api_post('/cgi-bin/message/send', payload)


def send_image_message(touser, media_id):
    cfg = _active_config()
    payload = {
        'touser': touser, 'agentid': int(cfg.agent_id), 'msgtype': 'image',
        'image': {'media_id': media_id}, 'enable_id_trans': 1,
        'enable_duplicate_check': 1, 'duplicate_check_interval': 1800,
    }
    return api_post('/cgi-bin/message/send', payload)


def get_department_list(parent_id=None):
    params = {}
    if parent_id is not None:
        params['id'] = parent_id
    return api_get('/cgi-bin/department/list', params)


def get_department_users(department_id, fetch_child=True):
    return api_get('/cgi-bin/user/simplelist', {'department_id': department_id, 'fetch_child': 1 if fetch_child else 0})


def api_get(path, params=None, use_token=True):
    params = dict(params or {})
    if use_token:
        try:
            params['access_token'] = get_access_token()
        except RuntimeError as e:
            return {'errcode': -1, 'errmsg': str(e)}
    try:
        r = requests.get(API_BASE + path, params=params, timeout=30)
        return r.json()
    except Exception as e:
        return {'errcode': -1, 'errmsg': str(e)}


def api_post(path, data):
    try:
        token = get_access_token()
    except RuntimeError as e:
        return {'errcode': -1, 'errmsg': str(e)}
    try:
        r = requests.post(API_BASE + path, params={'access_token': token},
                          json=data, timeout=30)
        return r.json()
    except Exception as e:
        return {'errcode': -1, 'errmsg': str(e)}
