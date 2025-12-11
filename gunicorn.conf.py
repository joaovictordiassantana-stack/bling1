# 🔥 Aplicar monkey patch ANTES de qualquer outra importação
from gevent import monkey
monkey.patch_all()

# 🟢 Classe de worker ideal para WebSockets + Flask
worker_class = "gevent"

# 🟢 Apenas 1 worker para evitar problemas de memória/estado em Render
workers = 1

# 🟢 Evitar crash por timeout
timeout = 300
graceful_timeout = 300

# 🟢 Manter conexões WebSocket vivas por mais tempo (antes era só 5 segundos)
keepalive = 120
