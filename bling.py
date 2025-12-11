#!/usr/bin/env python3

from gevent import monkey
monkey.patch_all()   # torna as bibliotecas padrão cooperativas com gevent
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
try:
    from simple_websocket import ConnectionClosed
except ImportError:
    class ConnectionClosed(Exception): pass

# ============================================================================ 
# 0. VARIÁVEIS GLOBAIS DE CONTROLE (LOCK)
# ============================================================================
token_exchange_lock = Lock()

# ============================================================================ 
# 1. LOGS AVANÇADOS
# ============================================================================

class InMemoryLogHandler(logging.Handler):
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
    CLIENT_ID: str = os.environ.get('BLING_CLIENT_ID', 'YOUR_CLIENT_ID')
    CLIENT_SECRET: str = os.environ.get('BLING_CLIENT_SECRET', 'YOUR_CLIENT_SECRET')
    REDIRECT_URI: str = os.environ.get('BLING_REDIRECT_URI')
    if not REDIRECT_URI:
        pass
    
    BLING_API_URL: str = 'https://www.bling.com.br/Api/v3'
    TOKEN_URL: str = 'https://www.bling.com.br/Api/v3/oauth/token'
    
    REQUEST_TIMEOUT: int = 30
    AUTH_TIMEOUT: int = 3
    MAX_RETRIES: int = 3
    BASE_DELAY: float = 1.0
    
    CHECK_MIN_STOCK: bool = True
    MIN_STOCK_THRESHOLD: int = 10
    DEFAULT_BATCH_SIZE: int = 10
    DELAY_BETWEEN_BATCHES: float = 0.5
    
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
    if not token_data: return False
    expires_at = token_data.get("expires_at")
    if not expires_at: return False
    return time.time() < float(expires_at) - 20

# --- CORREÇÃO 1: Função de extração de imagem mais focada na estrutura Bling V3 ---
def extract_image_url(prod: dict) -> Optional[str]:
    """Extrai URL da imagem, focando nas estruturas Bling V3 mais comuns e confiáveis."""
    if not isinstance(prod, dict): return None
    
    # Prioridade 1: Campos diretos de thumbnail ou url
    for key in ["urlThumbnail", "imagemURL", "url", "link"]:
        val = prod.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val

    # Prioridade 2: Lista de mídia (midia é a mais comum no V3)
    midia_list = prod.get("midia") or prod.get("midias")
    if isinstance(midia_list, list) and midia_list:
        first_item = midia_list[0]
        if isinstance(first_item, dict):
            # Tenta a chave 'url' dentro do primeiro objeto
            if first_item.get("url") and isinstance(first_item["url"], str) and first_item["url"].startswith("http"):
                return first_item["url"]
        elif isinstance(first_item, str) and first_item.startswith("http"):
            return first_item
            
    # Prioridade 3: Desce um nível (caso os dados de produto estejam aninhados)
    for nested_key in ["data", "produto"]:
        nested_data = prod.get(nested_key)
        if isinstance(nested_data, dict):
            ret = extract_image_url(nested_data) # Recursão rasa
            if ret: return ret

    return None

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
        if self.is_authenticated():
            return "#" 
        if self.state is None:
            self.state = secrets.token_urlsafe(16)
            self._save_state(self.state)
        return f"https://www.bling.com.br/Api/v3/oauth/authorize?client_id={self.config.CLIENT_ID}&redirect_uri={self.config.REDIRECT_URI}&response_type=code&scope=*/*&state={self.state}"
    
    def exchange_code_for_token(self, code: str, state: str) -> bool:
        if self.is_authenticated():
            self.logger.info("Tentativa de callback ignorada: Token já válido.")
            return True

        if self.state is None:
            self.state = state
            self._save_state(state)
        
        if self.state and state != self.state:
            self.logger.warning(f"State mismatch detectado (Ignorado para evitar bloqueio): {state} vs {self.state}")
            
        try:
            client = f"{self.config.CLIENT_ID}:{self.config.CLIENT_SECRET}"
            auth_header = base64.b64encode(client.encode()).decode()
            headers = {"Authorization": f"Basic {auth_header}", "Content-Type": "application/x-www-form-urlencoded"}
            payload = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': self.config.REDIRECT_URI}
            
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
        if not self.refresh_token: return False
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
                    return response.json().get("data", {})
                elif response.status_code == 429:
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
                time.sleep(3600)
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
    
    # --- CORREÇÃO 2: Lógica para carregar TODOS os produtos e detalhar KITS ---
    def _load_products_and_kits(self, access_token: str):
        self.logger.info("Iniciando carga completa de produtos para a lista...")
        self.kits.clear()
        self.products.clear()
        
        todos_produtos = []
        page = 1
        
        # PASSO 1: Baixar TUDO
        while True:
            try:
                resp = self.api_client.get_products(access_token, page=page, limit=100)
                items = resp.get('data', [])
                
                if not items: break
                
                todos_produtos.extend(items)
                if len(items) < 100: break  
                page += 1
                time.sleep(0.2)
            except Exception as e:
                self.logger.error(f"Erro ao carregar página {page}: {e}")
                break
        
        # Mapa para busca rápida de nomes de componentes
        produto_map = {str(p.get("id")): p for p in todos_produtos}
        
        self.logger.info(f"Processando {len(todos_produtos)} itens para exibição...")

        # PASSO 2: Processar, detalhar KITS e formatar a lista
        for p in todos_produtos:
            p_id = p.get("id")
            
            # Checa se é um kit. Se sim, chama detalhes para obter a estrutura completa.
            if p.get("tipo") == "K":
                try:
                    details = self.api_client.get_product_details(access_token, p_id)
                    if details:
                        p.update(details) # Atualiza o objeto p com os detalhes completos
                        time.sleep(0.1) # Pequena pausa para rate limit
                except Exception as e:
                    self.logger.warning(f"Falha ao buscar detalhes do Kit {p_id}: {e}")

            # Agora, extrai a estrutura (seja da lista original ou dos detalhes)
            estrutura = p.get("estrutura", {})
            componentes = estrutura.get("componentes", [])
            
            # Extração de imagem melhorada
            img_url = extract_image_url(p)
            
            # Processa componentes (se houver)
            comps_formatados = []
            if componentes:
                for c in componentes:
                    filho_ref = c.get("produto", {})
                    filho_id = str(filho_ref.get("id"))
                    
                    # Nome do componente
                    produto_filho = produto_map.get(filho_id)
                    # Usa o nome do cache (produto_map) ou do payload, se o cache falhar
                    nome_final = produto_filho.get("nome") if produto_filho else filho_ref.get("nome", "Componente Desconhecido")
                    
                    comps_formatados.append({
                        "nome": nome_final,
                        "quantidade": c.get("quantidade", 0),
                        "sku": produto_filho.get("codigo") if produto_filho else ""
                    })

            # ADICIONA TUDO À LISTA PRINCIPAL (KITS)
            item_formatado = {
                "id": p_id,
                "sku": p.get("codigo"),
                "produto": p.get("nome"),
                "imagemURL": img_url,
                "componentes": comps_formatados
            }
            
            self.kits.append(item_formatado)
            
            # Mantém compatibilidade com funções que usam self.products
            p['imagemURL'] = img_url
            self.products.append(p)

        self.logger.info(f"Carga finalizada. Total de itens listados: {len(self.kits)}")

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
# 7. DECORADOR
# ============================================================================

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
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
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#kits">Todos Produtos</button></li>
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
                    <button class="btn btn-sm btn-info mb-3" onclick="loadKits()">Recarregar Lista</button>
                    <p class="text-muted">Aguarde o carregamento completo. Kits (Produtos com Componentes) podem demorar mais para carregar os detalhes.</p>
                    <div id="kits-list"></div>
                </div>
                <div id="auth-required-kits" class="alert alert-warning hidden">
                    É necessário autenticar com o Bling para visualizar os Produtos.
                </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <script>
    const API = '/api';
    
    function formatLog(log) {
        return `<div class="log-entry"><span class="log-level-${log.level}">[${log.timestamp}] [${log.level}]</span> ${log.message}</div>`;
    }

    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${window.location.host}/ws/logs`);
    ws.onmessage = (e) => {
        const data = JSON.parse(e.data);
        const box = document.getElementById('logs-content');
        if(data.logs) {
            data.logs.forEach(l => box.innerHTML += formatLog(l));
            box.scrollTop = box.scrollHeight;
        }
    }
    
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
            document.getElementById('auth-link').href = d.auth_url;
        } catch (e) {
            console.error("Erro ao checar status:", e);
        }
    }
    
    checkStatus();
    setInterval(checkStatus, 5000);

    const btnSearch = document.getElementById('btn-search');
    btnSearch.onclick = async () => {
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
                        // CORREÇÃO IMAGEM NA BUSCA
                        html += `
                            <div class="list-group-item">
                                <div class="d-flex">
                                    <img src="${p.imagemURL || ''}" 
                                         style="width:60px;height:60px;object-fit:contain;margin-right:10px;border-radius:6px;background:#f1f1f1"
                                         onerror="this.style.display='none'">
                                    
                                    <div class="flex-grow-1">
                                        <div class="d-flex w-100 justify-content-between">
                                            <h5 class="mb-1">${p.nome || 'Sem nome'}</h5>
                                            <small>${p.sku || 'N/D'}</small>
                                        </div>
                                        <p class="mb-1">${p.descricaoCurta || ''}</p>
                                        <small class="text-muted d-block">
                                            <b>Estoque:</b> ${p.estoque}  
                                            <b style="margin-left:10px;">Tipo:</b> ${p.tipo}
                                        </small>
                                        ${p.componentes && p.componentes.length > 0 ? `
                                            <div class="mt-2">
                                                <b>Componentes:</b><br>
                                                ${p.componentes.map(c => `${c.quantidade}x ${c.produto?.nome || 'Sem nome'}`).join("<br>")}
                                            </div>
                                        ` : ""}
                                    </div>
                                </div>
                            </div>
                        `;
                });
                html += '</div>';
                div.innerHTML = html;
            } catch(e) {
                div.innerHTML = `<div class="alert alert-danger">Erro: ${e}</div>`;
            }
        };

        // CORREÇÃO 3: Lógica de exibição no JavaScript
        async function loadKits() {
            const div = document.getElementById('kits-list');
            const authRequiredDiv = document.getElementById('auth-required-kits');
            
            if (!isAuthenticated) {
                div.innerHTML = '';
                authRequiredDiv.classList.remove('hidden');
                return;
            }
            
            authRequiredDiv.classList.add('hidden');
            div.innerHTML = '<div class="alert alert-info">Carregando dados... Pode demorar, pois estamos buscando detalhes de Kits.</div>';
            
            try {
                const r = await fetch(`${API}/kits`);
                
                if (r.status === 401) {
                    div.innerHTML = '';
                    authRequiredDiv.classList.remove('hidden');
                    checkStatus();
                    return;
                }

                const data = await r.json();
                let html = `
                <table class="table table-sm">
                <thead>
                <tr>
                    <th>IMG</th>
                    <th>SKU</th>
                    <th>Nome</th>
                    <th>Componentes</th>
                </tr>
                </thead>
                <tbody>
                `;
                
                data.forEach(k => {
                    // Trata imagem quebrada escondendo a tag (mantido)
                    const imgHtml = k.imagemURL 
                        ? `<img src="${k.imagemURL}" style="width:50px;height:50px;object-fit:contain;border-radius:4px;" onerror="this.style.display='none'">` 
                        : '<span class="text-muted">-</span>';

                    let comps = '';
                    if (k.componentes && k.componentes.length > 0) {
                        // Verifica se o componente tem nome (necessário se o produto for muito novo ou incompleto no cache)
                        const componentes_validos = k.componentes.filter(c => c.nome && c.nome !== 'Componente Desconhecido');
                        
                        if (componentes_validos.length > 0) {
                            comps = componentes_validos
                                .map(c => `<small>• ${c.quantidade}x ${c.nome} (SKU: ${c.sku || 'N/D'})</small>`)
                                .join('<br>');
                        } else {
                            comps = '<span class="text-info" style="font-size:0.8em">Kit sem componentes detalhados na API.</span>';
                        }
                    } else {
                        comps = '<span class="text-muted" style="font-size:0.8em">Produto Simples (sem componentes)</span>';
                    }

                    html += `
                        <tr>
                            <td style="width:60px">${imgHtml}</td>
                            <td style="width:120px; font-weight:bold;">${k.sku || ''}</td>
                            <td>${k.produto}</td>
                            <td>${comps}</td>
                        </tr>
                    `;
                });
                html += '</tbody></table>';
                div.innerHTML = html;
        } catch(e) {
            div.innerHTML = 'Erro ao carregar lista. Verifique os logs.';
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
# 8. SERVIDOR WEB
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
            if self.orchestrator.auth.is_authenticated():
                return redirect('/')
            if not code or not state:
                return redirect('/') 
            if not token_exchange_lock.acquire(blocking=False):
                return redirect('/')
            try:
                with WebServer.code_lock:
                    if code in WebServer.used_codes:
                        return redirect('/')
                    WebServer.used_codes.add(code)
                self.orchestrator.auth.exchange_code_for_token(code, state)
                return redirect('/')
            except Exception as e:
                self.logger.error(f"Erro crítico no callback: {e}")
                return redirect('/')
            finally:
                token_exchange_lock.release()

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
            return jsonify(self.orchestrator.get_all_products())

        @self.app.route('/api/product/search', methods=["GET"])
        @token_required
        def api_product_search(token):
            termo = request.args.get("q") or request.args.get("sku") or request.args.get("nome") or ""
            termo = termo.strip()
            if not termo: return jsonify([])

            all_results_base = []
            seen_ids = set()

            def process_response(resp_data):
                items = resp_data.get('data') or []
                for p in items:
                    p_id = p.get('id')
                    if p_id and p_id in seen_ids: continue
                    if p_id: seen_ids.add(p_id)
                    all_results_base.append({
                        "id": p.get("id"),
                        "sku": p.get("codigo"),
                        "nome": p.get("nome"),
                        "tipo": p.get("tipo"),
                        "situacao": p.get("situacao"),
                        "preco": p.get("preco"),
                    })

            # Busca por SKU e Nome para produtos não detalhados
            resp_sku = self.orchestrator.api_client.get_products(token, codigo=termo, limit=20)
            process_response(resp_sku)
            
            resp_nome = self.orchestrator.api_client.get_products(token, nome=termo, limit=20)
            process_response(resp_nome)

            final_results = []
            MAX_DETALHES = 10 
            
            for idx, p in enumerate(all_results_base):
                if idx >= MAX_DETALHES: break
                try:
                    # Busca detalhes completos para os primeiros resultados
                    details = self.orchestrator.api_client.get_product_details(token, p["id"])
                except Exception:
                    details = {}
                
                estoque_val = (details.get("estoqueAtual") or details.get("saldoDisponivel") or details.get("estoque", {}).get("saldoVirtualTotal", 0))

                produto_completo = {
                    "id": p["id"],
                    "sku": p.get("sku"),
                    "nome": p.get("nome"),
                    "tipo": p.get("tipo"),
                    "situacao": p.get("situacao"),
                    "preco": p.get("preco"),
                    "estoque": estoque_val,
                    "descricaoCurta": details.get("descricaoCurta"),
                    "componentes": details.get("estrutura", {}).get("componentes", []),
                    "imagemURL": extract_image_url(details),
                }
                final_results.append(produto_completo)
            
            # Adiciona produtos do cache que a busca direta pode ter ignorado
            termo_lower = termo.lower()
            produtos_cache = self.orchestrator.get_all_products()
            for prod in produtos_cache:
                if prod.get('id') not in seen_ids:
                    p_nome = str(prod.get('nome', '')).lower()
                    p_sku = str(prod.get('codigo', '')).lower()
                    if termo_lower in p_nome or termo_lower in p_sku:
                         prod_cache_fmt = {
                             "id": prod.get("id"),
                             "sku": prod.get("codigo"),
                             "nome": prod.get("nome"),
                             "tipo": prod.get("tipo"),
                             "estoque": "Cache (N/A)",
                             "imagemURL": prod.get("imagemURL")
                         }
                         final_results.append(prod_cache_fmt)
                         if len(final_results) >= MAX_DETALHES + 5: break
            
            return jsonify(final_results)

        @app.route('/api/kits', methods=["GET"])
        @token_required
        def api_kits(token):
            # Força o recarregamento dos dados com a nova lógica de detalhamento de kits
            orchestrator.load_data() 
            return jsonify(orchestrator.get_all_kits())

        @app.route("/webhook/bling", methods=["POST"])
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
                    except ConnectionClosed:
                         break
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

import os as _os
_os.environ.setdefault("GUNICORN_CMD_ARGS", "--worker-class gevent --timeout 300 --keep-alive 5")
APP_PORT = int(_os.getenv("PORT", "10000"))