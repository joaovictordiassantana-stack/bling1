#!/bin/bash
set -e

# O Gunicorn é mais robusto para produção do que o Waitress.
# Ele usa automaticamente a variável de ambiente $PORT fornecida pelo Render.
# "bling:app" aponta para o módulo bling.py e para a variável app criada por create_app().

echo "Iniciando app Bling com Gunicorn..."

# Executa o servidor
exec gunicorn --bind 0.0.0.0:$PORT --workers 1 "bling:app"