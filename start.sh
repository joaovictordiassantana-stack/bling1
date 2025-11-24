#!/bin/bash

# Define a porta de bind usando a variável de ambiente PORT
# O Waitress usará esta variável
WAITRESS_CMD="waitress-serve --listen=0.0.0.0:$PORT --call bling:create_app"

echo "Iniciando com o comando: $WAITRESS_CMD"

# Executa o Waitress
exec $WAITRESS_CMD
