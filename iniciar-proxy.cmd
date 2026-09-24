@echo off
rem Lanzador del proxy Anthropic -> OpenAI (ZCode <-> tunel de Kaggle).
rem Doble clic en este archivo. Ctrl+C o cerrar la ventana para parar.
rem Si el proceso se cae por lo que sea, se vuelve a arrancar solo (5 s).
rem El registro se guarda en proxy-salida.log, en esta misma carpeta (se añade, no se borra).
cd /d "%~dp0"
title proxy zcode (Anthropic -> Kaggle)

:loop
echo.
echo Arrancando el proxy... (panel: http://127.0.0.1:8081/panel)
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "python -u zcode-anthropic-proxy.py 2>&1 | Tee-Object -FilePath 'proxy-salida.log' -Append"
echo.
echo ============================================================
echo  El proxy se ha detenido. Se reinicia solo en 5 segundos.
echo  (Si quieres pararlo del todo: Ctrl+C aqui, o cierra la ventana.)
echo ============================================================
timeout /t 5 /nobreak >nul
goto loop
