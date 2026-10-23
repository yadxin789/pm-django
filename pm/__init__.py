import pymysql
pymysql.install_as_MySQLdb()

# 让 Celery 应用在 Django 启动时被加载
from pm.celery import app as celery_app

__all__ = ('celery_app',)
