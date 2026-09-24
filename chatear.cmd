@echo off
rem Abre la pagina para chatear con tu modelo de Kaggle (chat-local.py).
rem Doble clic aqui. Deja la ventana abierta mientras chateas.
cd /d "%~dp0"
title chat con el modelo de Kaggle

netstat -ano | findstr ":8082 " >nul
if not errorlevel 1 (
  echo Ya hay un chat abierto en el puerto 8082: se abre el navegador.
  start "" http://127.0.0.1:8082
  exit /b 0
)

start "" http://127.0.0.1:8082
python -u chat-local.py
echo.
echo ============================================================
echo  El chat se ha detenido. Vuelve a ejecutar este archivo.
echo ============================================================
pause
