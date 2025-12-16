# ============================================================================
# gunicorn.conf.py - Configuração Otimizada para Bling API + WebSockets
# ============================================================================
# Autor: João Victor Dias Santana
# Última atualização: Dezembro 2025
# ============================================================================

# 🔥 Aplicar o monkey patch do gevent ANTES de qualquer outra importação
from gevent import monkey
monkey.patch_all()

# ============================================================================
# WORKER CONFIGURATION
# ============================================================================

# 🟩 Tipo de worker ideal para WebSockets + requisições externas lentas
worker_class = "gevent"

# 🟩 Apenas 1 worker — obrigatório no Render (mantém o estado, tokens, WS)
workers = 1

# 🟩 Threads por worker (gevent gerencia isso automaticamente, mas podemos definir)
threads = 1

# ============================================================================
# TIMEOUT CONFIGURATION
# ============================================================================

# 🟩 Evitar "WORKER TIMEOUT" em requisições pesadas (Bling API é lento)
# ✅ AUMENTADO: 120s → 180s para processar muitas páginas de pedidos
timeout = 180

# 🟩 Tempo para shutdown gracioso (permite finalizar requisições em andamento)
graceful_timeout = 120

# ============================================================================
# CONNECTION CONFIGURATION
# ============================================================================

# 🟩 Manter as conexões vivas mais tempo (especialmente WebSockets)
keepalive = 120

# ✅ NOVO: Limitar conexões simultâneas por worker (previne sobrecarga)
worker_connections = 1000

# ✅ NOVO: Tamanho máximo do backlog de conexões pendentes
backlog = 2048

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================

# 🟩 Deixa logs mais limpos e úteis
loglevel = "info"

# ✅ NOVO: Desabilita logs de acesso HTTP padrão (você já tem logs customizados)
accesslog = None  # Use "-" para STDOUT se quiser ver

# ✅ NOVO: Logs de erro vão para STDERR
errorlog = "-"

# ✅ NOVO: Formato de log mais limpo
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# ============================================================================
# PROCESS NAMING
# ============================================================================

# ✅ NOVO: Nome do processo (útil para monitoramento no Render)
proc_name = "bling_automacao"

# ============================================================================
# PRELOAD & DAEMON
# ============================================================================

# ✅ NOVO: Preload da aplicação (carrega código antes de fazer fork)
# CUIDADO: No Render, isso pode causar problemas com o worker de fundo
# Deixe como False para evitar duplicação do worker
preload_app = False

# ✅ NOVO: Não rodar como daemon (necessário para containers/Render)
daemon = False

# ============================================================================
# WORKER LIFECYCLE HOOKS
# ============================================================================

def on_starting(server):
    """
    Chamado logo antes do master process ser inicializado.
    """
    server.log.info("🚀 Gunicorn iniciando - Bling Automação v4.6")

def on_reload(server):
    """
    Chamado quando o servidor é recarregado (SIGHUP).
    """
    server.log.info("🔄 Recarregando configuração do Gunicorn")

def when_ready(server):
    """
    Chamado logo após o servidor estar pronto para aceitar conexões.
    """
    server.log.info("✅ Gunicorn pronto - Aguardando requisições")

def pre_fork(server, worker):
    """
    Chamado antes de fazer fork de um novo worker.
    """
    server.log.info(f"👶 Preparando para criar worker #{worker.age}")

def post_fork(server, worker):
    """
    Chamado após fork de um novo worker (dentro do worker process).
    """
    server.log.info(f"✅ Worker #{worker.pid} iniciado com sucesso")

def pre_exec(server):
    """
    Chamado antes de executar um novo master process.
    """
    server.log.info("🔄 Preparando para reiniciar o master process")

def worker_int(worker):
    """
    Chamado quando o worker recebe SIGINT ou SIGQUIT.
    """
    worker.log.info(f"⚠️ Worker #{worker.pid} recebeu sinal de interrupção")

def worker_abort(worker):
    """
    Chamado quando o worker é abortado (timeout ou erro crítico).
    """
    worker.log.error(f"❌ Worker #{worker.pid} abortado (timeout ou erro crítico)")

# ============================================================================
# SECURITY & LIMITS
# ============================================================================

# ✅ NOVO: Limite de tamanho de requisição (protege contra uploads gigantes)
limit_request_line = 4096
limit_request_fields = 100
limit_request_field_size = 8190

# ============================================================================
# BIND & PORT
# ============================================================================

# ✅ NOVO: Bind na porta 10000 (padrão do Render)
# Render define a variável PORT automaticamente
import os
bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"