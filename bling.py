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
# 0. VARIÁVEIS GLOBAIS DE CONTROLE (LOCK)
# ============================================================================
# Lock global para impedir múltiplas trocas de token simultâneas (Erro Worker Timeout)
token_exchange_lock = Lock()

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
        pass
    
    # API
    BLING_API_URL: str = 'https://www.bling.com.br/Api/v3'
    TOKEN_URL: str = 'https://www.bling.com.br/Api/v3/oauth/token'
    
    # Retry e Timeout
    REQUEST_TIMEOUT: int = 30
    AUTH_TIMEOUT: int = 3 # Timeout curto para auth
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

# --- FUNÇÃO PARA BUSCA DE PRODUTOS (CORRIGIDO PARA V3) ---
def get_bling_products_safe(bling_client, sku: str | None = None, nome: str | None = None, access_token: str | None = None):
    try:
        filters = {}
        if sku:
            # CORREÇÃO: API v3 usa 'codigo' e não 'sku'
            filters['codigo'] = sku.strip()
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
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self._load_or_create_config()
        self.logger = logger
    
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
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.expires_at: Optional[float] = None
        self.logger = logger
        self.load_tokens()
        self.state: Optional[str] = self._load_state()

    def _load_state(self) -> Optional[str]:
        tokens = load_tokens_safe(self.config.TOKENS_FILE)
        return tokens.get("state")

    def _save_state(self, state: str):
        tokens = load_tokens_safe(self.config.TOKENS_FILE)
        tokens["state"] = state
        save_tokens(tokens)
        
    def get_authorization_url(self) -> str:
        # Só gera novo state se não estiver autenticado E não tiver state salvo
        if self.is_authenticated():
            return "#" # Já autenticado
            
        if self.state is None:
            self.state = secrets.token_urlsafe(16)
            self._save_state(self.state)
            
        return f"https://www.bling.com.br/Api/v3/oauth/authorize?client_id={self.config.CLIENT_ID}&redirect_uri={self.config.REDIRECT_URI}&response_type=code&scope=*/*&state={self.state}"
    
    def exchange_code_for_token(self, code: str, state: str) -> bool:
        """
        Tenta trocar o código OAuth por token. Implementa verificação de Lock e State.
        """
        # Se já estiver autenticado, não faz nada
        if self.is_authenticated():
            self.logger.info("Tentativa de callback ignorada: Token já válido.")
            return True

        # Validação do State - Tolerância a nulo se for primeiro boot
        if self.state is None:
            self.state = state
            self._save_state(state)
        
        if state != self.state:
            self.logger.warning(f"State inválido recebido (Ignorado para evitar loop): {state} vs {self.state}")
            return False
            
        try:
            client = f"{self.config.CLIENT_ID}:{self.config.CLIENT_SECRET}"
            auth_header = base64.b64encode(client.encode()).decode()
            headers = {"Authorization": f"Basic {auth_header}", "Content-Type": "application/x-www-form-urlencoded"}
            payload = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': self.config.REDIRECT_URI}
            
            # Timeout curto para evitar travar worker
            response = requests.post(self.config.TOKEN_URL, data=payload, headers=headers, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                self._update_tokens(data)
                self.state = None
                self._save_state(None)
                return True
            else:
                self.logger.error(f"Bling retornou erro na troca: {response.text}")
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
            response = requests.post(self.config.TOKEN_URL, data=payload, timeout=self.config.AUTH_TIMEOUT)
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
        self.logger = logger
    
    def get_products(self, access_token: str, page: int = 1, limit: int = 100, **filters) -> Dict[str, Any]:
        headers = {'Authorization': f'Bearer {access_token}', 'Accept': 'application/json'}
        # Filters serão passados aqui: e.g. codigo='COI-B' ou nome='...'
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

    def get_product_details(self, access_token: str, product_id: int) -> Dict[str, Any]:
        headers = {'Authorization': f'Bearer {access_token}', 'Accept': 'application/json'}
        url = f"{self.config.BLING_API_URL}/produtos/{product_id}"
        
        for attempt in range(self.config.MAX_RETRIES):
            try:
                response = self.session.get(url, headers=headers, timeout=self.config.REQUEST_TIMEOUT)
                if response.status_code == 200:
                    # O Bling V3 retorna o objeto do produto dentro de 'data'
                    return response.json().get("data", {})
                elif response.status_code == 429:  # Rate limit
                    time.sleep(2)
                    continue
                else:
                    self.logger.warning(f"Erro API Detalhes Produto {product_id}: {response.status_code} - {response.text}")
            except Exception as e:
                self.logger.warning(f"Erro conexao API Detalhes Produto {product_id}: {e}")
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
        self.logger = logger
    
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

if not config.REDIRECT_URI:
    logger.error("ERRO FATAL: BLING_REDIRECT_URI não configurada no Render")
    pass

orchestrator = AutomationOrchestrator(config)
auth = orchestrator.auth

# ============================================================================ 
# 7. DECORADOR (TOKEN REQUIRED AJUSTADO)
# ============================================================================

def token_required(f):
    """Decorador para verificar se o token de acesso está disponível e válido."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Retorna 401 limpo para que o front entenda que precisa reautenticar
        if not orchestrator.auth or not orchestrator.auth.is_authenticated():
            orchestrator.auth.logger.warning("Request sem auth válida: retornando 401 json")
            return jsonify({"needAuth": True, "message": "Token expirado ou inválido"}), 401

        token = orchestrator.auth.get_valid_token()
        if not token:
            return jsonify({"needAuth": True, "message": "Falha no refresh token"}), 401
        return f(token=token, *args, **kwargs)
    return decorated

# ============================================================================ 
# 9. TEMPLATE HTML DO DASHBOARD
# ============================================================================

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel Bling - Sw Moveis</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
    <style>
        body { background: #f8f9fa; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        .navbar { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
        .log-box { font-family: 'Courier New', monospace; font-size: .85em; background: #1e1e1e; color: #d4d4d4; border-radius: .5rem; padding: 1rem; max-height: 400px; overflow-y: auto; }
        .log-level-INFO { color: #4ec9b0; }
        .log-level-WARNING { color: #dcdcaa; }
        .log-level-ERROR { color: #f48771; }
        .hidden { display: none; }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg">
        <div class="container-fluid">
            <a class="navbar-brand text-white" href="#">Bling Automação</a>
            <div class="d-flex">
                <span id="status-badge" class="badge bg-secondary me-2">Carregando...</span>
                <a id="auth-link" href="{{ auth_url }}" class="btn btn-sm btn-outline-light">Autenticar</a>
            </div>
        </div>
    </nav>

    <div class="container mt-4">
        <div class="row mb-4">
             <div class="col"><div class="card p-3 text-center"><h5>Sucesso</h5><h3 id="kpi-success" class="text-success">0</h3></div></div>
             <div class="col"><div class="card p-3 text-center"><h5>Falhas</h5><h3 id="kpi-failed" class="text-danger">0</h3></div></div>
        </div>

        <div class="card mb-4">
            <div class="card-header">Logs em Tempo Real</div>
            <div class="card-body bg-dark p-0">
                <div id="logs-content" class="log-box"></div>
            </div>
        </div>

        <ul class="nav nav-tabs" id="myTab" role="tablist">
            <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#search">Busca</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#kits">Kits</button></li>
        </ul>

            <div id="content-tabs" class="tab-content p-3 bg-white border border-top-0 rounded-bottom hidden">
            <div class="tab-pane fade show active" id="search">
                <div class="input-group mb-3">
                    <input type="text" class="form-control" id="search-input" placeholder="SKU ou Nome...">
                    <button class="btn btn-primary" id="btn-search">Buscar</button>
                </div>
                <div id="search-results"></div>
            </div>

            <div class="tab-pane fade" id="kits">
                    <button class="btn btn-sm btn-info mb-3" onclick="loadKits()">Recarregar Kits</button>
                    <div id="kits-list"></div>
                </div>
                <div id="auth-required-kits" class="alert alert-warning hidden">
                    É necessário autenticar com o Bling para visualizar os Kits.
                </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <script>
    const API = '/api';
    
    // Formatador de logs
    function formatLog(log) {
        return `<div class="log-entry"><span class="log-level-${log.level}">[${log.timestamp}] [${log.level}]</span> ${log.message}</div>`;
    }

    // WebSocket Logs
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${window.location.host}/ws/logs`);
    ws.onmessage = (e) => {
        const data = JSON.parse(e.data);
        const box = document.getElementById('logs-content');
        if(data.logs) {
            data.logs.forEach(l => box.innerHTML += formatLog(l));
            box.scrollTop = box.scrollHeight;
        }
    };

    // Status Polling
    let isAuthenticated = false;
    
    async function checkStatus() {
        try {
            const r = await fetch(API + '/status');
            const d = await r.json();
            const badge = document.getElementById('status-badge');
            
            isAuthenticated = d.authenticated;
            
            if(isAuthenticated) {
                badge.className = 'badge bg-success me-2';
                badge.textContent = 'Online';
                document.getElementById('auth-link').classList.add('d-none');
                document.getElementById('content-tabs').classList.remove('hidden');
            } else {
                badge.className = 'badge bg-danger me-2';
                badge.textContent = 'Offline';
                document.getElementById('auth-link').classList.remove('d-none');
                document.getElementById('content-tabs').classList.add('hidden');
            }
        } catch(e) {
            isAuthenticated = false;
        }
    }
    
    // Inicializa e depois repete
    checkStatus();
    setInterval(checkStatus, 5000);

        // Busca de Produtos
        document.getElementById('btn-search').onclick = async () => {
            if (!isAuthenticated) {
                document.getElementById('search-results').innerHTML = '<div class="alert alert-warning">É necessário autenticar com o Bling para realizar buscas.</div>';
                return;
            }
            
            const q = document.getElementById('search-input').value;
            const div = document.getElementById('search-results');
            div.innerHTML = 'Buscando...';
            
            try {
                const r = await fetch(`${API}/product/search?q=${q}`);
                
                if (r.status === 401) {
                    div.innerHTML = '<div class="alert alert-warning">Sessão expirada. Autentique novamente.</div>';
                    checkStatus();
                    return;
                }

                const data = await r.json();
                
                if(!data.length) {
                    div.innerHTML = '<div class="alert alert-warning">Nenhum resultado.</div>';
                    return;
                }
                
                let html = '<div class="list-group">';
                data.forEach(p => {
                    html += `
                        <div class="list-group-item">
                            <div class="d-flex w-100 justify-content-between">
                                <h5 class="mb-1">${p.nome || 'Sem nome'}</h5>
                                <small>${p.sku || 'N/D'}</small>
                            </div>
                            <p class="mb-1">${p.descricaoCurta || ''}</p>
                            <small class="text-muted">Tipo: ${p.tipo} | Estoque: ${p.estoque}</small>
                        </div>
                    `;
                });
                html += '</div>';
                div.innerHTML = html;
            } catch(e) {
                div.innerHTML = `<div class="alert alert-danger">Erro: ${e}</div>`;
            }
        };

        // Carregar Kits
        async function loadKits() {
            const div = document.getElementById('kits-list');
            const authRequiredDiv = document.getElementById('auth-required-kits');
            
            if (!isAuthenticated) {
                div.innerHTML = '';
                authRequiredDiv.classList.remove('hidden');
                return;
            }
            
            authRequiredDiv.classList.add('hidden');
            div.innerHTML = 'Carregando...';
            
            try {
                const r = await fetch(`${API}/kits`);
                
                if (r.status === 401) {
                    div.innerHTML = '';
                    authRequiredDiv.classList.remove('hidden');
                    checkStatus();
                    return;
                }

                const data = await r.json();
                let html = '<table class="table table-sm"><thead><tr><th>SKU</th><th>Nome</th><th>Componentes</th></tr></thead><tbody>';
                data.forEach(k => {
                    let comps = k.componentes.map(c => `${c.quantidade}x ${c.nome}`).join(', ');
                    html += `<tr><td>${k.sku}</td><td>${k.produto}</td><td>${comps}</td></tr>`;
                });
                html += '</tbody></table>';
                div.innerHTML = html;
        } catch(e) {
            div.innerHTML = 'Erro ao carregar kits.';
        }
    }
    
    document.addEventListener('DOMContentLoaded', () => {
        loadKits();
    });
    </script>
</body>
</html>
"""

# ============================================================================ 
# 8. SERVIDOR WEB (ROTAS CONSOLIDADAS)
# ============================================================================

class WebServer:
    used_codes = set()
    code_lock = Lock()
    
    def __init__(self, app: Flask, orchestrator: AutomationOrchestrator):
        self.app = app
        self.orchestrator = orchestrator
        self.sock = Sock(app)
        self.logger = logger
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
            code = request.args.get("code")
            state = request.args.get("state")
            
            # PROTEÇÃO 1: Se já estiver autenticado, redireciona direto
            if self.orchestrator.auth.is_authenticated():
                self.logger.info("Callback ignorado: Usuário já autenticado.")
                return redirect('/')

            if not code or not state:
                return redirect('/') # Redireciona silent, sem erro 400

            # PROTEÇÃO 2: Lock global de troca de token
            # Se não conseguir pegar o lock imediatamente, significa que outro request está processando
            if not token_exchange_lock.acquire(blocking=False):
                self.logger.warning("Concorrência detectada no callback. Redirecionando para home.")
                return redirect('/')
                
            try:
                # PROTEÇÃO 3: Previne reuso de code localmente
                with WebServer.code_lock:
                    if code in WebServer.used_codes:
                        return redirect('/')
                    WebServer.used_codes.add(code)
                
                self.logger.info(f"Processando callback code...")
                success = self.orchestrator.auth.exchange_code_for_token(code, state)
                
                # Se falhou por state inválido ou outro motivo, apenas redireciona
                return redirect('/')
            except Exception as e:
                self.logger.error(f"Erro crítico no callback: {e}")
                return redirect('/')
            finally:
                token_exchange_lock.release()

        @self.app.route('/api/status')
        def api_status():
            # Rota leve e rápida
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
            return jsonify(self.orchestrator.get_all_products())

        @self.app.route('/api/product/search', methods=["GET"])
        @token_required
        def api_product_search(token):
            termo = request.args.get("q") or request.args.get("sku") or request.args.get("nome") or ""
            termo = termo.strip() # Remove espaços
            if not termo:
                return jsonify([])

            # --- CORREÇÃO IMPORTANTE: BUSCA HÍBRIDA NA API ---
# O Bling v3 exige parâmetros específicos. Não podemos assumir que o usuário
            # está buscando 'nome' ou 'código' (SKU). Vamos buscar AMBOS na API para garantir.
            
            all_results_base = []
            seen_ids = set()

            def process_response(resp_data):
                """Processa resposta da API e adiciona à lista de resultados básicos"""
                items = resp_data.get('data') or []
                for p in items:
                    p_id = p.get('id')
                    # Evita duplicatas se encontrar o mesmo produto por nome e código
                    if p_id and p_id in seen_ids:
                        continue
                    if p_id: seen_ids.add(p_id)
                    
                    # Armazena apenas os dados básicos da busca inicial
                    all_results_base.append({
                        "id": p.get("id"),
                        "sku": p.get("codigo"),
                        "nome": p.get("nome"),
                        "tipo": p.get("tipo"),
                        "situacao": p.get("situacao"),
                        "preco": p.get("preco"),
                    })

            # 1. Tenta buscar por CÓDIGO (SKU)
            self.logger.info(f"Buscando API por CÓDIGO: {termo}")
            resp_sku = self.orchestrator.api_client.get_products(token, codigo=termo, limit=20)
            process_response(resp_sku)

            # 2. Tenta buscar por NOME (Descrição)
            self.logger.info(f"Buscando API por NOME: {termo}")
            resp_nome = self.orchestrator.api_client.get_products(token, nome=termo, limit=20)
            process_response(resp_nome)
            
            # 3. ENRIQUECIMENTO DE DADOS (Busca Detalhada)
            final_results = []
            for p in all_results_base:
                details = self.orchestrator.api_client.get_product_details(token, p["id"])
                
                # Constrói o objeto final com dados básicos e detalhes
                produto_completo = {
                    "id": p["id"],
                    "sku": p.get("sku"),
                    "nome": p.get("nome"),
                    "preco": p.get("preco"),
                    # Dados enriquecidos do GET /produtos/{id}
                    "estoque": details.get("estoque", {}).get("saldoVirtualTotal", 0),
                    "imagemURL": details.get("imagemURL"),
                    "componentes": details.get("estrutura", {}).get("componentes", []),
                    "descricaoCurta": details.get("descricaoCurta"),
                    "tipo": p.get("tipo"), # Mantém o tipo do produto
                }
                
                final_results.append(produto_completo)

            return jsonify(final_results)

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

# --- GUNICORN CONFIGURAÇÕES (TIMEOUT AJUSTADO PARA 300) ---
import os as _os
_os.environ.setdefault("GUNICORN_CMD_ARGS", "--worker-class gevent --timeout 300 --keep-alive 5")
APP_PORT = int(_os.getenv("PORT", "10000"))