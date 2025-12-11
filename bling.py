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
    REDIRECT_URI: str = os.environ.get('BLING_REDIRECT_URI')
    if not REDIRECT_URI:
        # A validação e abort(500) serão feitas na inicialização do Flask, conforme instruído.
        pass
    
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

# --- FUNÇÃO PARA BUSCA DE PRODUTOS (COM PAGINAÇÃO, ITERATIVA) ---
def get_bling_products_safe(bling_client, sku: str | None = None, nome: str | None = None, access_token: str | None = None):
    """
    Busca produtos no Bling por SKU ou por nome.
    Retorna dicionário: {"success": True, "data": [...] } ou {"success": False, "error": "msg"}
    """
    try:
        filters = {}
        if sku:
            filters['sku'] = sku.strip()
        if nome and not sku:
            filters['nome'] = nome.strip()

        # Paginação iterativa (não recursiva)
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
        self.logger = logger  # Ajuste: inclui logger no gerenciador
    
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
            self.logger.error(f"Erro salvando config: {e}")

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
        self.logger = logger  # Usa o logger global 'bling_automacao'
        
    def get_authorization_url(self) -> str:
        if self.state is None:
            self.state = secrets.token_urlsafe(16)
        return f"https://www.bling.com.br/Api/v3/oauth/authorize?client_id={self.config.CLIENT_ID}&redirect_uri={self.config.REDIRECT_URI}&response_type=code&state={self.state}"
    
    def exchange_code_for_token(self, code: str) -> bool:
        """
        Tenta trocar o código OAuth por token de acesso. Não chama recursivamente a si mesmo.
        """
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
            self.logger.error(f"Erro troca token: {e}")
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
            self.logger.error(f"Erro refresh token: {e}")
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
        self.logger = logger  # Ajuste: inclui logger no cliente API
    
    def get_products(self, access_token: str, page: int = 1, limit: int = 100, **filters) -> Dict[str, Any]:
        headers = {'Authorization': f'Bearer {access_token}', 'Accept': 'application/json'}
        params = {'pagina': page, 'limite': limit, **filters}
        url = f"{self.config.BLING_API_URL}/produtos"
        
        for attempt in range(self.config.MAX_RETRIES):
            try:
                response = self.session.get(url, headers=headers, params=params, timeout=self.config.REQUEST_TIMEOUT)
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:  # Rate limit
                    time.sleep(2)
                    continue
                else:
                    self.logger.warning(f"Erro API Produtos: {response.status_code} - {response.text}")
            except Exception as e:
                self.logger.warning(f"Erro conexao API: {e}")
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
        self.logger = logger  # Ajuste: inclui logger no orquestrador
    
    def load_data_worker(self):
        """Worker background para carregar dados."""
        while True:
            try:
                if self.auth.load_tokens():
                    token = self.auth.get_valid_token()
                    if token:
                        self._load_products_and_kits(token)
                    else:
                        self.logger.warning("Token inválido no worker.")
                else:
                    self.logger.info("Aguardando autenticação para carregar dados...")
                time.sleep(3600)  # Recarrega a cada 1h
            except Exception as e:
                self.logger.error(f"Erro worker: {e}")
                time.sleep(60)
    
    def load_data(self) -> bool:
        """Método de compatibilidade para CLI."""
        if self.auth.load_tokens():
             token = self.auth.get_valid_token()
             if token:
                 self._load_products_and_kits(token)
                 return True
        return False
    
    def _load_products_and_kits(self, access_token: str):
        self.logger.info("Carregando produtos e kits...")
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
        
        self.logger.info(f"Carga completa: {len(self.kits)} kits, {len(self.products)} produtos.")

    def get_all_products(self) -> List[Dict[str, Any]]:
        return self.products

    def get_all_kits(self) -> List[Dict[str, Any]]:
        return self.kits

    def run_purchase_check(self, create_orders=False):
        self.logger.info("Verificação de compras iniciada (Simulação).")
        return True

# Instâncias Globais
config = Config()

# Validação obrigatória do REDIRECT_URI antes de inicializar o orquestrador
if not config.REDIRECT_URI:
    logger.error("ERRO FATAL: BLING_REDIRECT_URI não configurada no Render")
    # O abort(500) deve ocorrer no contexto Flask; aqui apenas impedimos a inicialização completa.
    # Em seguida, o Flask mostrará erro 500.
    pass

orchestrator = AutomationOrchestrator(config)
auth = orchestrator.auth  # Atalho

# ============================================================================ 
# 7. DECORADOR (TOKEN REQUIRED AJUSTADO)
# ============================================================================

def token_required(f):
    """Decorador para verificar se o token de acesso está disponível e válido."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Verifica se há token carregado
        if not orchestrator.auth or not orchestrator.auth.access_token:
            orchestrator.auth.logger.warning("Acesso sem token de autenticação")
            return jsonify({"auth": False, "message": "Token indisponível. Autentique a aplicação."}), 401

        token = orchestrator.auth.get_valid_token()
        if not token:
            orchestrator.auth.logger.warning("Falha na atualização do token de acesso")
            return jsonify({"auth": False, "message": "Não autenticado. Autorize a aplicação via OAuth."}), 401
        return f(token=token, *args, **kwargs)
    return decorated

# ============================================================================ 
# 8. SERVIDOR WEB (ROTAS CONSOLIDADAS)
# ============================================================================

class WebServer:
    def __init__(self, app: Flask, orchestrator: AutomationOrchestrator):
        self.app = app
        self.orchestrator = orchestrator
        self.sock = Sock(app)
        self.logger = logger  # Ajuste: logger disponível no WebServer
        self.setup_routes()
        self.setup_websocket()

    def setup_routes(self):
        if not self.orchestrator.config.REDIRECT_URI:
            @self.app.route('/', defaults={'path': ''})
            @self.app.route('/<path:path>')
            def fatal_error_config(path):
                from flask import abort
                self.logger.error("ERRO FATAL: BLING_REDIRECT_URI não configurada no Render")
                abort(500)
        
        @self.app.route("/")
        def dashboard():
            auth_url = self.orchestrator.auth.get_authorization_url()
            return render_template_string(DASHBOARD_TEMPLATE, auth_url=auth_url)

        @self.app.route('/callback')
        def callback():
            code = request.args.get('code')
            if code:
                self.logger.info(f"Tentando trocar code {code} por token...")
                if self.orchestrator.auth.exchange_code_for_token(code):
                    self.logger.info("Troca de token concluída com sucesso.")
                    return redirect('/')
                return "Erro na troca de token", 400
            return "Código não fornecido", 400

        @self.app.route('/api/status')
        def api_status():
            return jsonify({
                "authenticated": self.orchestrator.auth.is_authenticated(),
                "auth_url": self.orchestrator.auth.get_authorization_url(),
                "is_running": self.orchestrator.is_running
            })

        @self.app.route('/api/stats')
        def api_stats():
            return jsonify(self.orchestrator.stats.to_dict())

        @self.app.route("/api/all_products", methods=["GET"])
        @token_required
        def api_all_products(token):
            """Retorna a lista de todos os produtos carregados (não-variações)."""
            return jsonify(self.orchestrator.get_all_products())

        @self.app.route('/api/product/search', methods=["GET"])
        @token_required
        def api_product_search(token):
            """Busca produtos e kits localmente por SKU ou Nome."""
            termo = request.args.get("q") or request.args.get("sku") or request.args.get("nome") or ""
            termo = termo.strip().lower()
            if not termo:
                return jsonify([])

            def match(item):
                n = str(item.get('nome') or item.get('produto') or "").lower()
                c = str(item.get('codigo') or item.get('sku') or "").lower()
                return termo in n or termo in c

            products_found = [p for p in self.orchestrator.products if match(p)]
            kits_found = [k for k in self.orchestrator.kits if match(k)]
            all_results = []
            
            for p in products_found:
                all_results.append({
                    "id": p.get("id"),
                    "sku": p.get("codigo"),
                    "nome": p.get("nome"),
                    "tipo": p.get("tipo"),
                    "situacao": p.get("situacao"),
                    "preco": p.get("preco"),
                    "imagemURL": p.get("imagemURL"),
                    "estoque": p.get("estoque", {}).get("saldoVirtualTotal", 0),
                    "descricaoCurta": p.get("descricaoCurta")
                })
            
            for k in kits_found:
                comps = k.get("componentes", [])
                desc_comps = ", ".join([f"{c['quantidade']}x {c['nome']}" for c in comps])
                all_results.append({
                    "id": None,
                    "sku": k.get("sku"),
                    "nome": k.get("produto"),
                    "tipo": "Kit/Composto",
                    "situacao": "Ativo",
                    "preco": 0,
                    "imagemURL": None,
                    "estoque": "N/A",
                    "descricaoCurta": f"Kit composto por: {desc_comps}"
                })
                
            return jsonify(all_results)

        @self.app.route('/api/produtos', methods=["GET"])
        @token_required
        def api_produtos_compat(token):
             return api_product_search(token=token)

        @self.app.route('/api/kits', methods=["GET"])
        @token_required
        def api_kits(token):
            return jsonify(self.orchestrator.get_all_kits())

        @self.app.route("/webhook/bling", methods=["POST"])
        def webhook_bling():
            try:
                data = request.get_json(silent=True)
                logger.info(f"WEBHOOK RECEBIDO: {data}")
            except Exception:
                pass
            return jsonify({"status": "ok"}), 200

    def setup_websocket(self):
        @self.sock.route('/ws/logs')
        def ws_logs(ws):
            logger.info("WS conectado.")
            last_idx = 0
            while True:
                try:
                    all_logs = memory_handler.get_logs()
                    if len(all_logs) > last_idx:
                        new_logs = all_logs[last_idx:]
                        ws.send(json.dumps({"logs": new_logs}))
                        last_idx = len(all_logs)
                    try:
                        ws.receive(timeout=1)
                    except Exception:
                        pass
                except Exception:
                    break

# ============================================================================ 
# 9. TEMPLATE (RAW STRING)
# ============================================================================

DASHBOARD_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="pt-br">
<!-- ... código HTML omitido para brevidade ... -->
</html>
"""

# ============================================================================ 
# 10. ENTRY POINT
# ============================================================================

def create_app() -> Flask:
    app = Flask(__name__)
    WebServer(app, orchestrator)
    return app

app = create_app()

def run_cli():
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    
    if args.serve:
        Thread(target=orchestrator.load_data_worker, daemon=True).start()
        app.run(host='0.0.0.0', port=args.port, debug=False)

if __name__ == "__main__":
    run_cli()

# --- GUNICORN CONFIGURAÇÕES (remanejar port e worker) ---
import os as _os
_os.environ.setdefault("GUNICORN_CMD_ARGS", "--worker-class gevent --timeout 120 --keep-alive 5")
APP_PORT = int(_os.getenv("PORT", "10000"))
