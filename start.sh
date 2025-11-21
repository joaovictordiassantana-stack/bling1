#!/bin/bash

# Define a porta de bind usando a variável de ambiente PORT
# O Gunicorn usará esta variável
GUNICORN_CMD="gunicorn bling:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120 --log-level debug"

echo "Iniciando com o comando: $GUNICORN_CMD"

# Executa o Gunicorn
exec $GUNICORN_CMD
