@echo off
rem Abre el panel del proxy: ahi se pega la URL y el TOKEN nuevos de la seccion 7 del notebook
rem cuando la sesion de Kaggle cambia. No hay que reiniciar el proxy ni tocar ZCode.
cd /d "%~dp0"
netstat -ano | findstr ":8081 " >nul
if errorlevel 1 (
  echo El proxy no parece estar escuchando en el puerto 8081.
  echo Arrancalo primero con iniciar-proxy.cmd y vuelve a ejecutar este.
  echo.
  pause
  exit /b 1
)
start "" http://127.0.0.1:8081/panel
