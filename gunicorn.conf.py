# 🔥 Aplicar o monkey patch do gevent ANTES de qualquer outra importação
from gevent import monkey
monkey.patch_all()

# 🟩 Tipo de worker ideal para WebSockets + requisições externas lentas
worker_class = "gevent"

# 🟩 Apenas 1 worker — obrigatório no Render (mantém o estado, tokens, WS)
workers = 1

# 🟩 Evitar "WORKER TIMEOUT" em requisições pesadas (e Bling é lento)
timeout = 120
graceful_timeout = 120

# 🟩 Manter as conexões vivas mais tempo (especialmente WebSockets)
keepalive = 120

# 🟩 Opcional: deixa logs mais limpos e úteis
loglevel = "info"
