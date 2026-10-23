@echo off
chcp 65001 >nul
REM ============================================================================
REM  PM 电话会议 —— 客户端「装一次、再无告警」根证书安装器（Windows）
REM
REM  为什么需要：
REM    服务器用一张自签证书提供 HTTPS（443 / 8443 / 3002 三个端口共用）。浏览器
REM    默认不信任它，每台客户机第一次访问都会弹「您的连接不是私密连接」；而且
REM    getUserMedia（麦克风）只在「安全上下文」可用，证书不被信任时部分浏览器会
REM    直接不给麦克风权限。把这张自签证书装进「受信任的根证书颁发机构」后，
REM    三个端口全部免告警，装一次即可（证书有效期 10 年）。
REM
REM  用法：
REM    把本文件拷到客户机，双击运行（会弹 UAC，点「是」），然后完全重开浏览器。
REM    命令行可指定服务器：install-cert-client.bat 192.168.2.102
REM ============================================================================
setlocal
set "SERVER=%~1"
if "%SERVER%"=="" set "SERVER=192.168.2.102"

REM --- 必须是管理员（要写「本地计算机」的受信任根证书存储） ---
net session >nul 2>&1
if not "%errorlevel%"=="0" (
    echo 需要管理员权限，正在申请提权...
    >"%TEMP%\pm-ca-elevate.vbs" echo Set UAC = CreateObject^("Shell.Application"^)
    >>"%TEMP%\pm-ca-elevate.vbs" echo UAC.ShellExecute "%~s0", "%SERVER%", "", "runas", 1
    cscript //nologo "%TEMP%\pm-ca-elevate.vbs" >nul
    del "%TEMP%\pm-ca-elevate.vbs" >nul 2>&1
    exit /b
)

set "CRT=%TEMP%\pm-ca-%SERVER%.crt"

echo.
echo [1/3] 下载证书：http://%SERVER%/static/pm-ca.crt
REM 注意走 80 端口 http 下载：此刻证书还没被信任，用 https 下载会先撞上告警。
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://%SERVER%/static/pm-ca.crt' -OutFile '%CRT%' -UseBasicParsing -TimeoutSec 15 } catch { exit 1 }"
if not exist "%CRT%" (
    echo.
    echo [失败] 下载证书失败。请确认：
    echo        1) 这台机器能访问 http://%SERVER%/  ；
    echo        2) 服务器地址填对了（当前：%SERVER%）；
    echo        3) 服务器上已执行过 deploy/setup-nginx-https.sh（证书已发布到 /static/pm-ca.crt）。
    echo.
    pause
    exit /b 1
)

echo [2/3] 安装到「受信任的根证书颁发机构」（本地计算机）...
certutil -addstore -f ROOT "%CRT%" >nul
if not "%errorlevel%"=="0" (
    echo [失败] certutil 安装失败。
    pause
    exit /b 1
)

echo [3/3] 完成。
echo.
echo   已信任 %SERVER% 的自签证书：443 / 8443 / 3002 三个端口都不再告警。
echo   请「完全退出并重开浏览器」后访问：
echo     https://%SERVER%/project/项目ID/voice
echo.
echo   若用 Firefox：它自带证书库，请到 about:config 把
echo     security.enterprise_roots.enabled 设为 true，或手动导入 %CRT%
echo.
pause
