@echo off
title PM Django Dev Server
cd /d "%~dp0"

echo ============================================
echo  PM Project Mgmt System (Django) - Starting
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. First-time setup required.
    echo.
    echo Please run these steps manually first:
    echo   1. python -m venv .venv
    echo   2. .venv\Scripts\activate
    echo   3. pip install -r requirements.txt
    echo   4. mysql -uroot -p -e "CREATE DATABASE IF NOT EXISTS pm_db_django DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
    echo   5. python manage.py migrate
    echo   6. python manage.py seed_data
    echo.
    echo Then run start.bat again.
    pause
    exit /b 1
)

echo Starting services (each in its own window, close window to stop)...
echo.

start "PM-Web :8000" cmd /k "cd /d %~dp0 && .venv\Scripts\activate.bat && python manage.py runserver 0.0.0.0:8000"

start "PM-Celery-Worker" cmd /k "cd /d %~dp0 && .venv\Scripts\activate.bat && celery -A pm worker -l info -P solo"

start "PM-Celery-Beat" cmd /k "cd /d %~dp0 && .venv\Scripts\activate.bat && celery -A pm beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler"

echo Starting optional Docker containers (OnlyOffice / Janus / coturn)...
docker start onlyoffice >nul 2>&1 && echo   [OK] OnlyOffice (8080)
docker start pm-janus >nul 2>&1 && echo   [OK] Janus WebRTC (8188)
docker start pm-coturn >nul 2>&1 && echo   [OK] coturn TURN (3478)
echo.

echo ============================================
echo  Services started:
echo    Web:    http://127.0.0.1:8000
echo    Admin:  admin / 123456
echo.
echo  Optional:
echo    OnlyOffice: http://127.0.0.1:8080
echo    Janus:      ws://127.0.0.1:8188
echo.
echo  Note: Celery needs Redis at 127.0.0.1:6379
echo  Stop: close the three popup windows.
echo ============================================
echo.
pause