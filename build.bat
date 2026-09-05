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
echo Verificando se os fontes compilam...
python -m compileall -q main.py app_config.py tabs
if errorlevel 1 (
    echo.
    echo [ERRO] Algum arquivo nao compila. Corrija antes de empacotar.
    echo.
    echo O PyInstaller NAO falharia aqui. Ele deixaria o modulo de fora,
    echo geraria o executavel normalmente, e o erro so apareceria ao rodar,
    echo como ModuleNotFoundError. Marcadores de conflito de merge esquecidos
    echo no codigo caem exatamente nesse caso.
    echo.
    pause
    exit /b 1
)

echo.
echo Iniciando PyInstaller...
pyinstaller "ODrive GUI Configurador.spec"
if errorlevel 1 (
    echo.
    echo [ERRO] O PyInstaller falhou.
    pause
    exit /b 1
)

set "WARNFILE=build\ODrive GUI Configurador\warn-ODrive GUI Configurador.txt"
findstr /c:"invalid module" "%WARNFILE%" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo [AVISO] O PyInstaller deixou modulos de fora do executavel:
    echo.
    findstr /c:"invalid module" "%WARNFILE%"
    echo.
    echo O executavel foi gerado, mas vai falhar ao rodar.
    echo.
    echo Corrija o modulo e rode de novo COM CACHE LIMPO:
    echo.
    echo     pyinstaller --clean "ODrive GUI Configurador.spec"
    echo.
    echo O --clean e obrigatorio aqui. Um modulo deixado de fora nunca entra
    echo na lista de arquivos que o PyInstaller vigia, entao corrigi-lo nao
    echo invalida o cache: o build seguinte diz "checking Analysis", pula
    echo tudo, e mantem o executavel quebrado com a mesma data de antes.
    echo.
    echo Detalhes em: %WARNFILE%
    echo.
    pause
    exit /b 1
)

echo.
echo ===================================================
echo Compilacao concluida!
echo O executavel foi gerado dentro da pasta 'dist'.
echo ===================================================
pause
