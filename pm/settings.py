"""
Django settings for pm (Laravel -> Django 迁移).
时区/语言/静态资源布局对齐原 pm 系统。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 注意：明文 SECRET_KEY / DEBUG=True 绝不能进生产。
# 两者统一从环境变量读取（systemd 的 EnvironmentFile=-/var/www/pm-django/.env 注入，
# deploy.sh 首次部署时会生成含随机 SECRET_KEY 且 DEBUG=False 的 .env）。
DEBUG = os.getenv('DEBUG', 'False').lower() in ('1', 'true', 'yes', 'on')


def _resolve_secret_key():
    """解析 SECRET_KEY：环境变量优先，其次读取/生成 BASE_DIR/.secret_key。

    源码里不再出现明文密钥；即使部署时漏配 SECRET_KEY，也会在本机生成一份
    随机密钥并落盘（600 权限），而不会退化成所有人共用的固定值。
    """
    env_key = os.getenv('SECRET_KEY', '').strip()
    if env_key:
        return env_key
    key_file = BASE_DIR / '.secret_key'
    try:
        if key_file.exists():
            return key_file.read_text(encoding='utf-8').strip()
        import secrets
        generated = secrets.token_urlsafe(50)
        key_file.write_text(generated, encoding='utf-8')
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
        return generated
    except OSError:
        # 只读环境（极少见）：退回一次性随机值，重启后会话失效但不至于起不来
        import secrets
        return secrets.token_urlsafe(50)


SECRET_KEY = _resolve_secret_key()

# ALLOWED_HOSTS 默认沿用原部署的放行策略；生产环境建议用 ALLOWED_HOSTS 环境变量指定域名/IP，逗号分隔。
ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', '*').split(',') if h.strip()]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_celery_beat',
    # 业务 app
    'core',
    'web',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # 静态文件兜底（线上 Nginx 也直出 /static/，两者不冲突）；依赖 requirements.txt 的 whitenoise
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'pm.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                # 侧边栏角色与计数
                'core.context_processors.sidebar_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'pm.wsgi.application'

# --- 数据库（PyMySQL 驱动，新库 pm_db_django） ---
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.getenv('DB_NAME', 'pm_db_django'),
        'USER': os.getenv('DB_USER', 'root'),
        'PASSWORD': os.getenv('DB_PASSWORD', 'root'),
        'HOST': os.getenv('DB_HOST', '127.0.0.1'),
        'PORT': os.getenv('DB_PORT', '3306'),
        'OPTIONS': {
            'charset': 'utf8mb4',
        },
    }
}

# 自定义认证 User 模型（对应原 users 表）
AUTH_USER_MODEL = 'core.User'

# 认证后端（区分密码错误与未激活）
AUTHENTICATION_BACKENDS = [
    'core.backends.UsernameModelBackend',
    'django.contrib.auth.backends.ModelBackend',
]

# 认证：未登录跳转登录页
LOGIN_URL = '/login'
LOGIN_REDIRECT_URL = '/dashboard'
LOGOUT_REDIRECT_URL = '/login'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# 对齐原系统：本地时间直接入库，Asia/Shanghai
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True

# --- 静态资源（复用参考目录 public/） ---
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- 会话落库（对齐原 database session） ---
SESSION_ENGINE = 'django.contrib.sessions.backends.db'
SESSION_COOKIE_AGE = 120 * 60  # 120 分钟，对齐原 SESSION_LIFETIME

# --- Celery（为将来扩展复杂任务留余地） ---
# 本地 Redis 5.x 不支持 RESP3 的 HELLO 握手，已将 redis-py 锁定为 5.2.1（默认 RESP2）
CELERY_BROKER_URL = 'redis://127.0.0.1:6379/0'
CELERY_RESULT_BACKEND = 'redis://127.0.0.1:6379/1'
CELERY_TIMEZONE = 'Asia/Shanghai'
CELERY_TASK_SERIALIZER = 'json'
CELERY_ACCEPT_CONTENT = ['json']

# --- 项目自定义配置（对齐原 .env PM_*） ---
PM_MAX_UPLOAD_SIZE = 20480          # KB
PM_DEFAULT_PAGINATION = 15
PM_DEFAULT_PROJECT_PREFIX = 'PRJ'
PM_CHAT_POLL_INTERVAL = 10000       # ms，聊天轮询
PM_LOGIN_MAX_ATTEMPTS = 5
PM_LOGIN_DECAY_MINUTES = 15
PM_BACKUP_RETENTION_DAYS = 30
PM_REGISTRATION_OPEN = True
PM_DEFAULT_REGISTER_ROLE = 'task_executor'
PM_EXTENSION_MAX_DAYS = 90

# 回收站保留天数：软删除满该天数的记录会被定时任务自动彻底删除（含物理文件）
PM_TRASH_RETENTION_DAYS = int(os.getenv('PM_TRASH_RETENTION_DAYS', 15))

# 任务文件历史版本保留个数：每次更新文件都会为旧版本生成独立快照，
# 超出该数量后清理最旧的
PM_FILE_VERSION_KEEP = int(os.getenv('PM_FILE_VERSION_KEEP', 3))

# 受管媒体根目录：全系统统一为 MEDIA_ROOT（对齐参考目录 Laravel 的 storage/app 物理根）。
# 数据库中一律保存「相对 MEDIA_ROOT」的路径，TrashService / audit_files 据此定位物理文件。
# 注意：不要再拆成 media/file_repository、media/public 之类的多个物理根 ——
# 一旦物理根与写入方不一致，彻底删除会删不掉文件、巡检会把正常记录误判为死记录。
PM_MEDIA_ROOT = os.getenv('PM_MEDIA_ROOT') or MEDIA_ROOT

# --- 企业微信 ---
WECOM_CORP_ID = ''
WECOM_AGENT_ID = ''
WECOM_SECRET = ''
WECOM_TOKEN = ''
WECOM_AES_KEY = ''

# --- AI 数据 REST API 密钥（/api/ai/*，请求头 X-AI-Key；对齐参考 config/pm.php ai_api_key）---
AI_API_KEY = os.getenv('AI_API_KEY', '')

# --- 反向代理（Nginx 终止 TLS）---
# Nginx 以 http 反代到 gunicorn，必须靠这个头告诉 Django「浏览器那一侧其实是 https」。
# 少了它会有两个连锁故障：
#   1) request.is_secure() 恒为 False → CSRF 的 Origin 校验拿 https://IP 去比 http://IP → 一律 403，
#      HTTPS 下所有表单/登录都提交不了；
#   2) 生成的绝对地址（跳转、回调地址）会退回 http，混在 https 页面里被浏览器拦。
# Nginx 侧已固定 proxy_set_header X-Forwarded-Proto $scheme，客户端伪造的头会被覆盖，安全。
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# --- OnlyOffice ---
# 以下三项由 .env 注入（pm-django.service 的 EnvironmentFile）；未配置时用默认值。
#   SERVER_URL      浏览器加载 api.js 的地址，必须「浏览器可达」（HTTPS 下用 https://192.168.2.102:8443），
#                   填 localhost 时只有本机浏览器能用，别的机器打开编辑器就会报 DocsAPI is not defined；
#                   页面是 https 而这里是 http 时，api.js 会被浏览器按「混合内容」拦掉（编辑器白板）
#   JWT_SECRET      必须与 DS 容器启动时的 JWT_SECRET 一致，否则编辑器报 JWT 校验失败
#   PM_INTERNAL_URL DS（容器内）回访本系统的地址；gunicorn 只监听 127.0.0.1:8000，
#                   故要经 Nginx 走 80 端口（如 http://192.168.2.102，不带端口）。
#                   这一项保持 http 即可：它只被 DS 容器在服务端使用，不经过浏览器
ONLYOFFICE_SERVER_URL = os.getenv('ONLYOFFICE_SERVER_URL', 'http://localhost:8080')
ONLYOFFICE_JWT_SECRET = os.getenv('ONLYOFFICE_JWT_SECRET', '40c9ffd0643b6ec199ffbb41f1da030fbec4ccf59d0a2479ce1eed85cd7c91fe')
ONLYOFFICE_PM_INTERNAL_URL = os.getenv('ONLYOFFICE_PM_INTERNAL_URL', 'http://host.docker.internal:8000')

# ===== 电话会议（WebRTC/Janus 信令服务）=====
# 这几项必须走 .env：deploy/deploy-voice.sh 会把它们写成「服务器 IP」（浏览器要连的是服务器，
# 不是自己的机器）。此前这里硬编码 127.0.0.1，导致脚本写进 .env 的值被无视，
# 前端拿到 wss://127.0.0.1:3001 后只能连自己本机，会议永远进不去。
VOICE_SIGNAL_WS = os.getenv('VOICE_SIGNAL_WS', 'wss://127.0.0.1:3001')   # 信令 WebSocket
VOICE_JANUS_WS = os.getenv('VOICE_JANUS_WS', 'ws://127.0.0.1:8188')      # Janus WebSocket
VOICE_STUN = os.getenv('VOICE_STUN', 'stun:127.0.0.1:3478')
VOICE_TURN = os.getenv('VOICE_TURN', '')
VOICE_TURN_USER = os.getenv('VOICE_TURN_USER', '')
VOICE_TURN_SECRET = os.getenv('VOICE_TURN_SECRET', '')
VOICE_TOKEN_TTL = int(os.getenv('VOICE_TOKEN_TTL', '600'))               # 入场 token 有效期（秒）
VOICE_PM_INTERNAL_URL = os.getenv('VOICE_PM_INTERNAL_URL', 'http://127.0.0.1:8000')
