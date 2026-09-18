@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
title XauBreak M5 - Installer
color 0F

set "SRC=%~dp0XauBreak_M5.mq5"
set "BASE=%APPDATA%\MetaQuotes\Terminal"
set /a FOUND=0
set /a COMPILED=0

echo.
echo ================================================================
echo   XauBreak M5  -  MetaTrader 5 Installer
echo   منصّب بوت الذهب لميتاتريدر 5
echo ================================================================
echo.

rem ---------------------------------------------------------------- source
if not exist "%SRC%" (
  echo   [ERROR] XauBreak_M5.mq5 غير موجود بنفس مجلد هذا الملف
  echo   [ERROR] XauBreak_M5.mq5 not found next to this installer
  echo.
  echo   فك ضغط الملف المضغوط بالكامل ثم شغّل هذا الملف من داخله.
  goto :END
)

for %%F in ("%SRC%") do set "SRCSIZE=%%~zF"
echo   المصدر / Source : XauBreak_M5.mq5  ^(!SRCSIZE! bytes^)
if not "!SRCSIZE!"=="39007" (
  echo   [!] تحذير: الحجم المتوقع 39007 بايت. الملف قد يكون ناقصاً.
)
echo.

rem ------------------------------------------------------- terminal folders
if not exist "%BASE%" (
  echo   [ERROR] لم أجد مجلد ميتاتريدر:
  echo           %BASE%
  echo.
  echo   إذا كانت نسختك محمولة ^(Portable^)، انسخ الملف يدوياً إلى:
  echo           ^<مجلد البرنامج^>\MQL5\Experts\
  goto :END
)

echo   أبحث عن نسخ ميتاتريدر المنصّبة...
echo.

for /d %%T in ("%BASE%\*") do (
  if exist "%%~fT\MQL5\Experts" (
    set /a FOUND+=1
    set "TERM=%%~fT"
    set "EXPERTS=%%~fT\MQL5\Experts"

    echo   ----------------------------------------------------------------
    echo   [!FOUND!] %%~nxT

    rem --- remove every earlier attempt, in all three program folders ---
    for %%D in (Experts Scripts Indicators) do (
      if exist "%%~fT\MQL5\%%D" (
        for %%N in (XauBreak_M5) do (
          if exist "%%~fT\MQL5\%%D\%%N.mq5" (
            del /f /q "%%~fT\MQL5\%%D\%%N.mq5" >nul 2>&1
            echo       حذفت القديم / removed : MQL5\%%D\%%N.mq5
          )
          if exist "%%~fT\MQL5\%%D\%%N.ex5" (
            del /f /q "%%~fT\MQL5\%%D\%%N.ex5" >nul 2>&1
            echo       حذفت القديم / removed : MQL5\%%D\%%N.ex5
          )
        )
      )
    )

    rem --- copy the fresh source in ---
    copy /y "%SRC%" "!EXPERTS!\XauBreak_M5.mq5" >nul 2>&1
    if exist "!EXPERTS!\XauBreak_M5.mq5" (
      for %%F in ("!EXPERTS!\XauBreak_M5.mq5") do set "GOTSIZE=%%~zF"
      echo       [OK] نُسخ إلى MQL5\Experts  ^(!GOTSIZE! bytes^)
    ) else (
      echo       [ERROR] فشل النسخ. جرّب تشغيل هذا الملف كمسؤول ^(Run as administrator^).
      goto :NEXTTERM
    )

    rem --- locate MetaEditor for this terminal and compile ---
    set "MEPATH="
    if exist "%%~fT\origin.txt" (
      for /f "usebackq delims=" %%L in ("%%~fT\origin.txt") do (
        if exist "%%L\metaeditor64.exe" set "MEPATH=%%L\metaeditor64.exe"
        if exist "%%L\metaeditor.exe"   set "MEPATH=%%L\metaeditor.exe"
      )
    )
    if not defined MEPATH call :FINDEDITOR
    if defined MEPATH (
      echo       أترجم / compiling ...
      "!MEPATH!" /compile:"!EXPERTS!\XauBreak_M5.mq5" /log:"!EXPERTS!\XauBreak_M5_compile.log" >nul 2>&1
      if exist "!EXPERTS!\XauBreak_M5.ex5" (
        set /a COMPILED+=1
        echo       [OK] تمت الترجمة بنجاح - XauBreak_M5.ex5 جاهز
      ) else (
        echo       [!] الترجمة لم تنتج ملف ex5. افتح MetaEditor واضغط F7.
        if exist "!EXPERTS!\XauBreak_M5_compile.log" echo           السجل: MQL5\Experts\XauBreak_M5_compile.log
      )
    ) else (
      echo       [!] لم أجد MetaEditor. افتح ميتاتريدر واضغط F4 ثم F7.
    )
    :NEXTTERM
  )
)

echo   ----------------------------------------------------------------
echo.
if %FOUND%==0 (
  echo   [ERROR] لم أجد أي نسخة ميتاتريدر 5 فيها مجلد MQL5\Experts
  echo           شغّل ميتاتريدر مرة واحدة على الأقل ثم أعد المحاولة.
  goto :END
)

echo   النتيجة / Result
echo     نسخ ميتاتريدر التي عولجت : %FOUND%
echo     تمت ترجمتها بنجاح        : %COMPILED%
echo.

if %COMPILED% GTR 0 (
  echo   ================================================================
  echo    خلص! الآن بميتاتريدر:
  echo      1^) اضغط Ctrl+N لفتح نافذة Navigator
  echo      2^) كليك يمين على Expert Advisors ثم Refresh
  echo      3^) راح يظهر XauBreak_M5 - اسحبه على شارت XAUUSD إطار M5
  echo      4^) فعّل زر AutoTrading بالشريط العلوي
  echo   ================================================================
) else (
  echo   ================================================================
  echo    الملف انتقل لمكانه الصحيح، بقيت خطوة الترجمة:
  echo      1^) بميتاتريدر اضغط F4  ^(يفتح MetaEditor^)
  echo      2^) بنافذة Navigator اليسرى: Experts ثم دبل-كليك XauBreak_M5
  echo      3^) اضغط F7   ^(لازم تطلع 0 errors, 0 warnings^)
  echo   ================================================================
)

:END
echo.
echo   اضغط أي زر للإغلاق / Press any key to close
pause >nul
endlocal
exit /b

rem ------------------------------------------------------------------------
rem Fallback search for MetaEditor when origin.txt is unreadable or missing.
rem ------------------------------------------------------------------------
:FINDEDITOR
for %%P in ("%ProgramFiles%" "%ProgramFiles(x86)%" "%ProgramW6432%") do (
  if exist %%P (
    for /d %%D in (%%P\*) do (
      if not defined MEPATH (
        if exist "%%~fD\metaeditor64.exe" set "MEPATH=%%~fD\metaeditor64.exe"
      )
      if not defined MEPATH (
        if exist "%%~fD\metaeditor.exe" set "MEPATH=%%~fD\metaeditor.exe"
      )
    )
  )
)
exit /b
