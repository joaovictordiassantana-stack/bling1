@echo off
title Iniciando Bling Automação
color 0a

echo ===========================================
echo        Iniciando Servidor Bling
echo ===========================================

:: Navega até a pasta onde está o código
cd /d "C:\Users\S&W\bling"

:: Ativa o ambiente virtual, se existir
if exist venv (
    echo Ativando ambiente virtual...
    call venv\Scripts\activate
)

:: Instala dependências (somente se faltar algo)
echo Verificando dependências...
pip install -r requirements.txt

:: Executa o servidor Flask
echo Iniciando servidor Flask...
python bling.py --serve

pause
