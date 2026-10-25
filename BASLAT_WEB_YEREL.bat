@echo off
cd /d "%~dp0"
echo ============================================================
echo  Isi Kaybi WEB sitesi (YEREL TICARI SURUM v2) baslatiliyor
echo  Adres: http://127.0.0.1:8080   (yalnizca bu bilgisayardan)
echo  Yonetici girisi: ugur.tanyeri@hotmail.com veya admin@yerel.test
echo  Guncel Motor: Otomatik Yon + Plak/Asmolen + Karma Yapi + Hibrit Tavan/Taban
echo  BU PENCEREYI KAPATMAYIN.
echo ============================================================
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8080" ^| findstr "LISTENING"') do (
    echo Eski yerel sunucu oturumu kapatiliyor ^(PID: %%a^)...
    taskkill /F /PID %%a >nul 2>&1
)
set "ADMIN_EPOSTA=admin@yerel.test"
set "ADMIN_SIFRE=Degistir-Bu-Sifreyi-1"
set "VERI_DIZINI=%~dp0yerel_veri"
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m web.app
pause
