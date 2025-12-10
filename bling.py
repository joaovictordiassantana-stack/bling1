#!/usr/bin/env python3
"""
bling.py - Sistema completo de automação Bling com design premium (CORRIGIDO)
Implementa OAuth 2.0, API robusta, gerenciamento de estoque/compras e dashboard web.
"""

import os
import sys
import json
import time
import logging
import logging.handlers
import base64
import secrets
import argparse

from pathlib import Path
from datetime import datetime, timedelta
from threading import Lock, Thread
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
from functools import wraps

import requests
from requests.exceptions import RequestException
from flask import Flask, request, render_template_string, jsonify, redirect, url_for
from flask_sock import Sock

# ============================================================================
# 1. LOGS AVANÇADOS
# ============================================================================

class InMemoryLogHandler(logging.Handler):
    """Handler de log que armazena os registros em memória para o WebSocket."""
    def __init__(self, max_logs=500):
        super().__init__()
        self.logs = []
        self.max_logs = max_logs
        self.formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%dT%H:%M:%S'
        )
        
    def emit(self, record):
        try:
            log_entry = {
                'timestamp': self.formatter.formatTime(record),
                'level': record.levelname,
                'message': self.format(record),
                'name': record.name
            }
            self.logs.append(log_entry)
            if len(self.logs) > self.max_logs:
                self.logs.pop(0)
        except Exception:
            self.handleError(record)
    
    def get_logs(self, limit: Optional[int] = None) -> List[Dict[str, str]]:
        if limit:
            return self.logs[-limit:]
        return self.logs.copy()

# Configuração global de diretórios e logs
LOGS_DIR = Path('logs')
LOG_FILE = LOGS_DIR / 'automacao_bling.log'
ERROR_LOG_FILE = LOGS_DIR / 'errors.log'

def setup_logging():
    LOGS_DIR.mkdir(exist_ok=True)
    global memory_handler
    memory_handler = InMemoryLogHandler()
    
    logger = logging.getLogger('bling_automacao')
    logger.setLevel(logging.INFO)
    
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1024*1024*5, backupCount=5, encoding='utf-8'
    )
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    
    # Handler de erro separado
    error_logger = logging.getLogger('error_logger')
    error_logger.setLevel(logging.ERROR)
    error_file_handler = logging.handlers.RotatingFileHandler(
        ERROR_LOG_FILE, maxBytes=1024*1024*5, backupCount=5, encoding='utf-8'
    )
    error_logger.addHandler(error_file_handler)
    
    logger.addHandler(file_handler)
    logger.addHandler(memory_handler)
    
    if not os.environ.get('FLASK_ENV'):
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
        logger.addHandler(console_handler)
        
    return logger, error_logger

logger, error_logger = setup_logging()

# ============================================================================
# 2. CONFIGURAÇÕES
# ============================================================================

class Config:
    """Configurações globais da aplicação."""
    
    # Bling OAuth
    CLIENT_ID: str = os.environ.get('BLING_CLIENT_ID', 'YOUR_CLIENT_ID')
    CLIENT_SECRET: str = os.environ.get('BLING_CLIENT_SECRET', 'YOUR_CLIENT_SECRET')
    REDIRECT_URI: str = os.environ.get('BLING_REDIRECT_URI', 'http://localhost:8000/callback')
    
    # API
    BLING_API_URL: str = 'https://www.bling.com.br/Api/v3'
    TOKEN_URL: str = 'https://www.bling.com.br/Api/v3/oauth/token'
    
    # Retry e Timeout
    REQUEST_TIMEOUT: int = 30
    MAX_RETRIES: int = 3
    BASE_DELAY: float = 1.0
    
    # Automação
    CHECK_MIN_STOCK: bool = True
    MIN_STOCK_THRESHOLD: int = 10
    DEFAULT_BATCH_SIZE: int = 10
    DELAY_BETWEEN_BATCHES: float = 0.5
    
    # Arquivos
    TOKENS_FILE: Path = Path('tokens.json')
    COMPONENT_CONFIG_FILE: Path = Path('component_config.json')

# ============================================================================
# 3. UTILITÁRIOS E AUTH (FUNÇÕES SEGURAS)
# ============================================================================

def load_tokens_safe(path="tokens.json"):
    if not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({}, f)
        except Exception:
            pass
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
            return data
    except Exception as e:
        logger.error(f"Erro lendo tokens.json: {e}")
        return {}

def save_tokens(data):
    try:
        with open("tokens.json", "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4, ensure_ascii=False)
        logger.info("Tokens salvos com sucesso.")
    except Exception as e:
        logger.error(f"Erro ao salvar tokens: {e}")

def is_token_valid(token_data):
    if not token_data:
        return False
    expires_at = token_data.get("expires_at")
    if not expires_at:
        return False
    return time.time() < float(expires_at) - 20

def get_bling_products_safe(bling_client, sku: str | None = None, nome: str | None = None, access_token: str | None = None):
    """Busca produtos no Bling por SKU ou por nome com paginação."""
    try:
        filters = {}
        if sku:
            filters['sku'] = sku.strip()
        if nome and not sku:
            filters['nome'] = nome.strip()

        page = 1
        all_items = []
        token = access_token or getattr(bling_client, "access_token", None)
        
        while True:
            resp = bling_client.get_products(token, page=page, limit=100, **filters)
            if not resp: 
                break
                
            items = resp.get('data') or resp.get('produtos') or []
            if isinstance(items, dict) and 'produto' in items:
                items = items.get('produto') or []
            
            if not items:
                break
                
            all_items.extend(items)
            if len(items) < 100:
                break
            page += 1
            
        return {"success": True, "data": all_items}
        
    except Exception as e:
        logger.exception("Erro na busca de produtos no Bling: %s", e)
        return {"success": False, "error": str(e)}

# ============================================================================
# 4. CLASSES DE DADOS E EXCEÇÕES
# ============================================================================

class BlingAuthError(Exception): pass
class BlingAPIError(Exception): pass

@dataclass
class ProcessingStats:
    success: int = 0
    failed: int = 0
    ops_created: int = 0
    pos_created: int = 0
    stock_checks: int = 0
    elapsed_time_seconds: float = 0.0
    
    def reset(self):
        self.success = 0
        self.failed = 0
        self.ops_created = 0
        self.pos_created = 0
        self.stock_checks = 0
        self.elapsed_time_seconds = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'success': self.success,
            'failed': self.failed,
            'ops_created': self.ops_created,
            'pos_created': self.pos_created,
            'stock_checks': self.stock_checks,
            'elapsed_time_seconds': round(self.elapsed_time_seconds, 2)
        }

class ComponentConfigManager:
    """Gerencia as configurações locais de componentes."""
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self._load_or_create_config()
        
    def _load_or_create_config(self) -> Dict[str, Any]:
        if self.file_path.exists():
            try:
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    self.config = json.load(f)
            except Exception:
                self.config = {"components": []}
        else:
            self.config = {"components": []}
            self._save_config()
        return self.config
    
    def _save_config(self):
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4)
        except Exception as e:
            logger.error(f"Erro salvando config: {e}")

# ============================================================================
# 5. CLIENTE BLING API E AUTH
# ============================================================================

class BlingAuth:
    def __init__(self, config: Config):
        self.config = config
        self.state: Optional[str] = None
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.expires_at: Optional[float] = None
        
    def get_authorization_url(self) -> str:
        if self.state is None:
            self.state = secrets.token_urlsafe(16)
        return f"https://www.bling.com.br/Api/v3/oauth/authorize?client_id={self.config.CLIENT_ID}&redirect_uri={self.config.REDIRECT_URI}&response_type=code&state={self.state}"
    
    def exchange_code_for_token(self, code: str) -> bool:
        try:
            client = f"{self.config.CLIENT_ID}:{self.config.CLIENT_SECRET}"
            auth_header = base64.b64encode(client.encode()).decode()
            headers = {"Authorization": f"Basic {auth_header}", "Content-Type": "application/x-www-form-urlencoded"}
            payload = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': self.config.REDIRECT_URI}
            
            response = requests.post(self.config.TOKEN_URL, data=payload, headers=headers, timeout=self.config.REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                data = response.json()
                self._update_tokens(data)
                return True
            return False
        except Exception as e:
            logger.error(f"Erro troca token: {e}")
            return False

    def refresh_access_token(self) -> bool:
        if not self.refresh_token:
            return False
        try:
            payload = {
                'grant_type': 'refresh_token',
                'refresh_token': self.refresh_token,
                'client_id': self.config.CLIENT_ID,
                'client_secret': self.config.CLIENT_SECRET
            }
            response = requests.post(self.config.TOKEN_URL, data=payload, timeout=self.config.REQUEST_TIMEOUT)
            if response.status_code == 200:
                self._update_tokens(response.json())
                return True
            return False
        except Exception as e:
            logger.error(f"Erro refresh token: {e}")
            return False

    def _update_tokens(self, data):
        self.access_token = data.get('access_token')
        if 'refresh_token' in data:
            self.refresh_token = data.get('refresh_token')
        self.expires_at = time.time() + data.get('expires_in', 3600)
        save_tokens({'access_token': self.access_token, 'refresh_token': self.refresh_token, 'expires_at': self.expires_at})

    def load_tokens(self) -> bool:
        data = load_tokens_safe()
        if data and is_token_valid(data):
            self.access_token = data.get('access_token')
            self.refresh_token = data.get('refresh_token')
            self.expires_at = data.get('expires_at')
            return True
        elif data and data.get('refresh_token'):
            self.refresh_token = data.get('refresh_token')
            return self.refresh_access_token()
        return False

    def is_authenticated(self) -> bool:
        return bool(self.access_token and self.expires_at and time.time() < (self.expires_at - 60))

    def get_valid_token(self) -> Optional[str]:
        if self.is_authenticated():
            return self.access_token
        if self.refresh_access_token():
            return self.access_token
        return None

class BlingAPIClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
    
    def get_products(self, access_token: str, page: int = 1, limit: int = 100, **filters) -> Dict[str, Any]:
        headers = {'Authorization': f'Bearer {access_token}', 'Accept': 'application/json'}
        params = {'pagina': page, 'limite': limit, **filters}
        url = f"{self.config.BLING_API_URL}/produtos"
        
        for attempt in range(self.config.MAX_RETRIES):
            try:
                response = self.session.get(url, headers=headers, params=params, timeout=self.config.REQUEST_TIMEOUT)
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:
                    time.sleep(2)
                    continue
                else:
                    logger.warning(f"Erro API Produtos: {response.status_code} - {response.text}")
            except Exception as e:
                logger.warning(f"Erro conexao API: {e}")
            time.sleep(1)
        return {}

# ============================================================================
# 6. ORQUESTRADOR
# ============================================================================

class AutomationOrchestrator:
    def __init__(self, config: Config):
        self.config = config
        self.auth = BlingAuth(config)
        self.api_client = BlingAPIClient(config)
        self.component_config = ComponentConfigManager(config.COMPONENT_CONFIG_FILE)
        self.stats = ProcessingStats()
        
        self.kits: List[Dict[str, Any]] = []
        self.products: List[Dict[str, Any]] = []
        self.is_running: bool = False
        self.lock = Lock()
    
    def load_data_worker(self):
        """Worker background para carregar dados."""
        while True:
            try:
                if self.auth.load_tokens():
                    token = self.auth.get_valid_token()
                    if token:
                        self._load_products_and_kits(token)
                    else:
                        logger.warning("Token inválido no worker.")
                else:
                    logger.info("Aguardando autenticação para carregar dados...")
                time.sleep(3600)
            except Exception as e:
                logger.error(f"Erro worker: {e}")
                time.sleep(60)

    def _load_products_and_kits(self, access_token: str):
        logger.info("Carregando produtos e kits...")
        self.kits.clear()
        self.products.clear()
        page = 1
        
        while True:
            resp = self.api_client.get_products(access_token, page=page)
            products_raw = resp.get('data', [])
            if not products_raw:
                break
            
            for p in products_raw:
                prod = p if isinstance(p, dict) else {}
                if not prod: continue
                estrutura = prod.get('estrutura', {})
                componentes = estrutura.get('componentes', [])
                
                if componentes:
                    kit_obj = {
                        "sku": prod.get('codigo'),
                        "produto": prod.get('nome'),
                        "componentes": [
                            {"nome": c.get('produto', {}).get('nome', 'N/A'), "quantidade": c.get('quantidade', 0)}
                            for c in componentes
                        ]
                    }
                    self.kits.append(kit_obj)
                else:
                    self.products.append(prod)
            
            page += 1
            time.sleep(0.2)
        logger.info(f"Carga completa: {len(self.kits)} kits, {len(self.products)} produtos.")

    def get_all_products(self) -> List[Dict[str, Any]]: return self.products
    def get_all_kits(self) -> List[Dict[str, Any]]: return self.kits

# ============================================================================
# 7. DECORADOR
# ============================================================================

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not orchestrator.auth or not orchestrator.auth.access_token:
            return jsonify({"auth": False, "message": "Token indisponível."}), 401
        if not orchestrator.auth.is_authenticated():
            if not orchestrator.auth.refresh_access_token():
                return jsonify({"status": "not_authenticated"}), 401
        token = orchestrator.auth.get_valid_token()
        return f(token=token, *args, **kwargs)
    return decorated

# ============================================================================
# 8. SERVIDOR WEB (PATCH WEBSOCKET APLICADO)
# ============================================================================

class WebServer:
    def __init__(self, app: Flask, orchestrator: AutomationOrchestrator):
        self.app = app
        self.orchestrator = orchestrator
        self.sock = Sock(app)
        self.setup_routes()
        self.setup_websocket()

    def setup_routes(self):
        @self.app.route("/")
        def dashboard():
            auth_url = self.orchestrator.auth.get_authorization_url()
            return render_template_string(DASHBOARD_TEMPLATE, auth_url=auth_url)

        @self.app.route('/callback')
        def callback():
            code = request.args.get('code')
            if code and self.orchestrator.auth.exchange_code_for_token(code):
                return redirect('/')
            return "Erro ou Código não fornecido", 400

        @self.app.route('/api/status')
        def api_status():
            return jsonify({
                "authenticated": self.orchestrator.auth.is_authenticated(),
                "auth_url": self.orchestrator.auth.get_authorization_url(),
                "is_running": self.orchestrator.is_running
            })

        @self.app.route('/api/product/search', methods=["GET"])
        @token_required
        def api_product_search(token):
            termo = (request.args.get("q") or "").strip().lower()
            if not termo: return jsonify([])
            results = []
            
            for p in self.orchestrator.products:
                if termo in str(p.get('nome','')).lower() or termo in str(p.get('codigo','')).lower():
                    results.append({"sku": p.get("codigo"), "nome": p.get("nome"), "tipo": p.get("tipo"), "estoque": p.get("estoque",{}).get("saldoVirtualTotal",0)})
            
            for k in self.orchestrator.kits:
                if termo in str(k.get('produto','')).lower() or termo in str(k.get('sku','')).lower():
                    results.append({"sku": k.get("sku"), "nome": k.get("produto"), "tipo": "Kit", "estoque": "N/A"})
            return jsonify(results)

        @self.app.route('/api/kits', methods=["GET"])
        @token_required
        def api_kits(token): return jsonify(self.orchestrator.get_all_kits())

    def setup_websocket(self):
        @self.sock.route('/ws/logs')
        def ws_logs(ws):
            logger.info("WS conectado.")
            last_idx = 0
            try:
                while True:
                    all_logs = memory_handler.get_logs()
                    if len(all_logs) > last_idx:
                        new_logs = all_logs[last_idx:]
                        ws.send(json.dumps({"logs": new_logs}))
                        last_idx = len(all_logs)
                    time.sleep(1)
            except Exception:
                logger.info("WS desconectado.")

# ============================================================================
# 9. TEMPLATE PRESERVADO
# ============================================================================

DASHBOARD_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel Bling</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
    <style>
        body { background: #f8f9fa; font-family: sans-serif; }
        .log-box { font-family: monospace; background: #1e1e1e; color: #d4d4d4; padding: 1rem; height: 300px; overflow-y: auto; border-radius: 5px; }
        .log-level-INFO { color: #4ec9b0; }
    </style>
</head>
<body>
    <nav class="navbar navbar-dark bg-dark px-3">
        <a class="navbar-brand" href="#">Bling Automação</a>
        <div class="d-flex align-items-center">
            <span id="status-badge" class="badge bg-secondary me-2">Offline</span>
            <a id="auth-link" href="{{ auth_url }}" class="btn btn-outline-light btn-sm">Autenticar</a>
        </div>
    </nav>
    <div class="container mt-4">
        <div class="card bg-dark text-white p-3 mb-4">
            <h6>Logs em Tempo Real</h6>
            <div id="logs-content" class="log-box"></div>
        </div>
        <div class="input-group mb-3">
            <input type="text" id="search-input" class="form-control" placeholder="Buscar SKU ou Produto...">
            <button class="btn btn-primary" onclick="search()">Buscar</button>
        </div>
        <div id="results" class="list-group"></div>
    </div>
    <script>
        const API = '/api';
        const ws = new WebSocket(`${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/logs`);
        ws.onmessage = (e) => {
            const data = JSON.parse(e.data);
            const box = document.getElementById('logs-content');
            data.logs.forEach(l => box.innerHTML += `<div><span class="log-level-${l.level}">[${l.timestamp}]</span> ${l.message}</div>`);
            box.scrollTop = box.scrollHeight;
        };
        async function checkStatus() {
            const r = await fetch(API + '/status');
            const d = await r.json();
            const badge = document.getElementById('status-badge');
            if(d.authenticated) {
                badge.className = 'badge bg-success me-2'; badge.textContent = 'Online';
                document.getElementById('auth-link').classList.add('d-none');
            }
        }
        async function search() {
            const q = document.getElementById('search-input').value;
            const res = await fetch(`${API}/product/search?q=${q}`);
            const data = await res.json();
            const div = document.getElementById('results');
            div.innerHTML = data.map(p => `<div class="list-group-item"><b>${p.sku}</b> - ${p.nome} <br> <small>Estoque: ${p.estoque}</small></div>`).join('');
        }
        setInterval(checkStatus, 5000);
        checkStatus();
    </script>
</body>
</html>
"""

# ============================================================================
# 10. INICIALIZAÇÃO
# ============================================================================

orchestrator = AutomationOrchestrator(Config())

def create_app() -> Flask:
    app = Flask(__name__)
    WebServer(app, orchestrator)
    return app

app = create_app()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if args.serve:
        Thread(target=orchestrator.load_data_worker, daemon=True).start()
        app.run(host='0.0.0.0', port=args.port)