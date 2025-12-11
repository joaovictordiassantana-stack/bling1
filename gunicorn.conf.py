# 🔥 Este arquivo é carregado ANTES do Gunicorn inicializar
from gevent import monkey
monkey.patch_all()

worker_class = "gevent"
workers = 1
timeout = 120
keepalive = 5
