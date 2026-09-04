@echo off
echo ===================================================
echo     Compilando ODrive GUI Configurator...
echo ===================================================
echo.

if not exist "venv\Scripts\activate.bat" (
    echo [ERRO] Ambiente virtual nao encontrado na pasta 'venv'.
    echo Por favor, certifique-se de que o ambiente virtual foi criado
    echo e as dependencias foram instaladas.
    echo.
    pause
    exit /b 1
)

echo Ativando ambiente virtual...
call venv\Scripts\activate.bat

echo.
echo Iniciando PyInstaller...
pyinstaller "ODrive GUI Configurador.spec"

echo.
echo ===================================================
echo Compilacao concluida! 
echo O executavel foi gerado dentro da pasta 'dist'.
echo ===================================================
pause
