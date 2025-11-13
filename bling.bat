@echo off
setlocal

REM Define o nome do ambiente virtual
set VENV_NAME=venv

REM --- 1. Criacao do Ambiente Virtual ---
if not exist %VENV_NAME%\ (
    echo Criando ambiente virtual...
    
    REM Tenta usar 'python' ou 'py'
    python -m venv %VENV_NAME%
    if errorlevel 1 (
        py -m venv %VENV_NAME%
    )
    
    if errorlevel 1 (
        echo ERRO: Falha ao criar o ambiente virtual.
        echo Verifique se o Python esta instalado e se o comando 'python' ou 'py' funciona no seu terminal.
        pause
        exit /b 1
    )
)

REM --- 2. Ativacao do Ambiente Virtual ---
echo Ativando ambiente virtual...
if exist %VENV_NAME%\Scripts\activate.bat (
    call %VENV_NAME%\Scripts\activate.bat
) else if exist %VENV_NAME%\bin\activate (
    REM Para sistemas baseados em Unix (WSL, Git Bash)
    call %VENV_NAME%\bin\activate
) else (
    echo ERRO: Nao foi possivel encontrar o script de ativacao.
    pause
    exit /b 1
)

REM --- 3. Instalacao de Dependencias ---
if exist requirements.txt (
    echo Instalando ou atualizando dependencias...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo ERRO: Falha ao instalar as dependencias.
        echo Verifique sua conexao com a internet e o conteudo do requirements.txt.
        pause
        exit /b 1
    )
) else (
    echo Aviso: requirements.txt nao encontrado. Pulando instalacao de dependencias.
)

REM --- 4. Execucao do Servidor Flask ---
echo.
echo ====================================================================
echo INICIANDO SERVIDOR BLING AUTOMACAO
echo ====================================================================
echo.
python bling.py --serve

REM Mantem a janela aberta apos a execucao
echo.
echo Servidor encerrado. Pressione qualquer tecla para fechar...
pause
endlocal
