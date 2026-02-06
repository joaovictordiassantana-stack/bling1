#!/usr/bin/env python3

"""
================================================================================
bling.py - Sistema de Automação Bling com OAuth 2.0 e Dashboard Web Premium
================================================================================

Autor: João Victor Dias Santana
Copyright (c) 2025 João Victor Dias Santana

Implementa integração completa com Bling API v3, gerenciamento de componentes,
KPIs de vendas em tempo real via WebSocket e dashboard interativo.

Versão: 5.0 (Customizada - Integração Tray e Lógica de Componentes)
Última atualização: Fevereiro 2026
================================================================================
"""

import os
import sys
import json
import time
import logging
import logging.handlers
import base64
import secrets
import shutil
import hmac
import hashlib

from pathlib import Path
from datetime import datetime, timedelta, timezone
from threading import Lock, Thread, Event
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Dict, Any, Callable
from collections import defaultdict
from dataclasses import dataclass, field
from functools import wraps

import requests
from requests.exceptions import RequestException
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, request, render_template_string, jsonify, redirect, url_for
from flask_sock import Sock
# Importação necessária para tratamento correto do WebSocket
try:
    from simple_websocket import ConnectionClosed
except ImportError:
    class ConnectionClosed(Exception): pass

# ============================================================================ 
# 0. RATE LIMITER GLOBAL (NÍVEL PRODUÇÃO)
# ============================================================================

class RateLimiter:
    """Limitador de taxa centralizado para evitar 429 da API Bling.
    
    Garante intervalo mínimo entre requisições, thread-safe.
    Taxa segura: ~2.5 req/s (min_interval=0.4s)
    """
    def __init__(self, min_interval=0.4):
        self.min_interval = min_interval
        self.lock = Lock()
        self.last_call = 0.0

    def wait(self):
        """Bloqueia até que o intervalo mínimo desde a última chamada tenha passado."""
        with self.lock:
            now = time.time()
            elapsed = now - self.last_call
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self.last_call = time.time()

# ============================================================================ 
# 1. VARIÁVEIS GLOBAIS DE CONTROLE (LOCK)
# ============================================================================
token_exchange_lock = Lock()
kpi_update_callbacks: List[Callable] = []
kpi_update_lock = Lock()

# ============================================================================ 
# 1. LOGS AVANÇADOS
# ============================================================================

class InMemoryLogHandler(logging.Handler):
    """Handler de log que armazena os registros em memória para o WebSocket."""
    def __init__(self, max_logs=50):
        super().__init__()
        self.logs = []
        self.max_logs = max_logs
        self.formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%dT%H:%M:%S'
        )
        self.ws_callbacks = []
        self.ws_lock = Lock()
        
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
            
            with self.ws_lock:
                dead_callbacks = []
                for cb in self.ws_callbacks:
                    try:
                        cb(log_entry)
                    except Exception:
                        dead_callbacks.append(cb)
                
                for cb in dead_callbacks:
                    self.ws_callbacks.remove(cb)

        except Exception:
            self.handleError(record)
    
    def get_logs(self, limit: Optional[int] = None) -> List[Dict[str, str]]:
        if limit:
            return self.logs[-limit:]
        return self.logs.copy()
        
    def add_ws_callback(self, callback):
        with self.ws_lock:
            self.ws_callbacks.append(callback)
    
    def remove_ws_callback(self, callback):
        with self.ws_lock:
            if callback in self.ws_callbacks:
                self.ws_callbacks.remove(callback)

# Configuração global de diretórios e logs
LOGS_DIR = Path('logs')
LOG_FILE = LOGS_DIR / 'automacao_bling.log'
ERROR_LOG_FILE = LOGS_DIR / 'errors.log'

def setup_logging():
    LOGS_DIR.mkdir(exist_ok=True)
    global memory_handler
    memory_handler = InMemoryLogHandler()
    
    logger = logging.getLogger('bling_automacao')
    logger.setLevel(logging.DEBUG)
    
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    logging.getLogger('flask_sock').setLevel(logging.WARNING)
    
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

# ✅ FUNÇÕES DE LIMPEZA DE CALLBACKS
def cleanup_kpi_callbacks():
    """Remove callbacks órfãos a cada 5 minutos"""
    global kpi_update_callbacks
    with kpi_update_lock:
        valid = []
        for cb in kpi_update_callbacks:
            try:
                _ = getattr(cb, '__name__', 'lambda_or_partial')
                valid.append(cb)
            except:
                logger.debug("Callback órfão removido.")
                pass
        kpi_update_callbacks = valid
        logger.debug(f"🧹 Callbacks KPI limpos: {len(valid)} ativos")

def start_cleanup_timer():
    """Inicia timer para limpar callbacks órfãos a cada 5 minutos"""
    def cleanup_loop():
        while True:
            time.sleep(300)
            cleanup_kpi_callbacks()
    
    Thread(target=cleanup_loop, daemon=True).start()

# ============================================================================ 
# 2. CONFIGURAÇÕES
# ============================================================================

RECEITA_CADEIRA = {
    "COMPENSADO 50X52X17": 1,
    "SARRAFO 52": 3,
    "SARRAFO 46": 1,
    "SARRAFO 14": 2,
    "MDF 15MM 52X35": 2,
    "MDF 6MM 52X35": 2,
    "SARRAFO 33": 2,
    "SARRAFO 10": 2,
    "MDF 15MM": 1,
    "TECIDO": 3,
    "ESPUMA ACOPLAGEM": 0.5,
    "ESPUMA ASSENTO": 1,
    "ESPUMA ENCOSTO": 1,
    "ESPUMA CABEÇOTE": 1,
    "ESPUMA ASSENTO 52X7,5X1": 1,
    "ESPUMA ASSENTO 54X14X1": 1,
    "ESPUMA BRAÇO 52X21X1": 1,
    "ESPUMA BRAÇO 52X35X1": 1,
    "ESPUMA BRAÇO 35X9,5X1": 4,
    "ESPUMA BRAÇO 54X9,5X2": 2,
    "LINHA": 1,
    "COLA": 1,
    "LAMINA CROMADA": 1,
    "LAMINA DE CABEÇOTE": 1,
    "PARAFUSO 1/4 X 1": 15,
    "PARAFUSO 1/4 X 2.1/4": 8,
    "PARAFUSO 5X25": 6,
    "PORCA GARRA 1/4": 20,
    "GRAMPO 80/10": 1,
    "GRAMPO 14/40": 1,
    "COSTUREIRA": 1,
    "EMBALAGEM": 1,
    "BASE": 1
}

class Config:
    """Configurações globais da aplicação."""
    
    # Bling OAuth
    CLIENT_ID: str = os.environ.get('BLING_CLIENT_ID', 'YOUR_CLIENT_ID')
    CLIENT_SECRET: str = os.environ.get('BLING_CLIENT_SECRET', 'YOUR_CLIENT_SECRET')
    WEBHOOK_SECRET: str = os.environ.get('BLING_WEBHOOK_SECRET', 'YOUR_WEBHOOK_SECRET')
    REDIRECT_URI: str = os.environ.get('BLING_REDIRECT_URI')
    
    # API
    BLING_API_URL: str = 'https://www.bling.com.br/Api/v3'
    TOKEN_URL: str = 'https://www.bling.com.br/Api/v3/oauth/token'
    
    # Configurações de Loja (Tray)
    TRAY_LOJA_ID: int = 803393
    TRAY_API_ID: int = 205929726
    
    # Retry e Timeout
    REQUEST_TIMEOUT: int = 30
    AUTH_TIMEOUT: int = 3
    MAX_RETRIES: int = 3
    BASE_DELAY: float = 1.0
    
    # Rate Limiting
    MAX_PAGES_PER_BATCH: int = 5
    DELAY_BETWEEN_PAGES: float = 0.8
    DELAY_BETWEEN_BATCHES: float = 5.0
    
    # Arquivos
    TOKENS_FILE: Path = Path('tokens.json')
    PRODUCTS_CACHE_FILE: Path = Path('products_cache.json')
    SALES_STATS_FILE: Path = Path('sales_stats.json')
    
    INITIAL_REFRESH_TOKEN: str = os.environ.get('BLING_REFRESH_TOKEN')

# ============================================================================ 
# 3. UTILITÁRIOS
# ============================================================================

def safe_iter(obj):
    if obj is None: return []
    if isinstance(obj, list): return obj
    return []

def safe_get(obj, key, default=None):
    if not isinstance(obj, dict): return default
    return obj.get(key, default)

def load_tokens_safe(path: Path) -> Dict[str, Any]:
    if not path.exists(): return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_tokens(data: Dict[str, Any], path: Path):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Erro ao salvar tokens: {e}")

def load_products_cache(path: Path) -> Dict[str, Any]:
    if not path.exists(): return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_products_cache(data: Dict[str, Any], path: Path):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Erro ao salvar cache de produtos: {e}")

def load_stats(path: Path) -> Dict[str, Any]:
    if not path.exists(): return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if 'last_recalculated' in data and data['last_recalculated']:
                data['last_recalculated'] = datetime.fromisoformat(data['last_recalculated'])
            return data
    except Exception:
        return {}

def save_stats(data: Dict[str, Any], path: Path):
    try:
        to_save = data.copy()
        if isinstance(to_save.get('last_recalculated'), datetime):
            to_save['last_recalculated'] = to_save['last_recalculated'].isoformat()
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(to_save, f, indent=4)
    except Exception as e:
        logger.error(f"Erro ao salvar estatísticas: {e}")

# ============================================================================ 
# 4. API CLIENT
# ============================================================================

class BlingAPIClient:
    def __init__(self, config: Config, auth_manager: "AuthManager"):
        self.config = config
        self.auth = auth_manager
        self.logger = logging.getLogger('bling_automacao')
        self.rate_limiter = RateLimiter()
        self.session = requests.Session()
        
        retry_strategy = Retry(
            total=config.MAX_RETRIES,
            backoff_factor=config.BASE_DELAY,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "PUT"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.headers.update({
            'Accept': 'application/json',
            'User-Agent': 'SWMoveis/5.0 (Integracao Bling Tray)'
        })
        
    def _request(self, method: str, endpoint: str, **kwargs) -> Optional[Dict[str, Any]]:
        url = f"{self.config.BLING_API_URL}/{endpoint}"
        token = self.auth.get_access_token()
        
        if not token:
            self.logger.warning(f"Token ausente para {endpoint}.")
            return None
            
        kwargs.setdefault('headers', {})
        kwargs['headers']['Authorization'] = f'Bearer {token}'
        
        self.logger.debug(f"[DEBUG] API REQ -> {method} {url} params={kwargs.get('params')}")
        self.rate_limiter.wait()
        
        try:
            response = self.session.request(method, url, timeout=45, **kwargs)
            self.logger.debug(f"[DEBUG] API RESP <- status={response.status_code}")

            if response.status_code == 401:
                if self.auth.refresh_token():
                    new_token = self.auth.get_access_token()
                    kwargs['headers']['Authorization'] = f'Bearer {new_token}'
                    response = self.session.request(method, url, timeout=45, **kwargs)
                else:
                    return None

            response.raise_for_status()
            return response.json() if response.text else {}

        except Exception as e:
            self.logger.error(f"[DEBUG] Erro em {endpoint}: {str(e)}")
            return None

    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        return self._request('GET', endpoint, params=params)

# ============================================================================ 
# 5. AUTH MANAGER
# ============================================================================

class AuthManager:
    OAUTH_STATE_FILE: Path = Path('oauth_state.json')

    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger('bling_automacao')
        self._tokens = load_tokens_safe(self.config.TOKENS_FILE)
        self._access_token = self._tokens.get('access_token')
        self._refresh_token = self._tokens.get('refresh_token') or self.config.INITIAL_REFRESH_TOKEN
        self._expires_at = self._tokens.get('expires_at', 0)
        
    def _save_tokens(self):
        data = {
            'access_token': self._access_token,
            'refresh_token': self._refresh_token,
            'expires_at': self._expires_at
        }
        save_tokens(data, self.config.TOKENS_FILE)

    def is_authenticated(self) -> bool:
        if self._access_token and self._expires_at > time.time() + 60:
            return True
        return self.refresh_token() if self._refresh_token else False

    def get_access_token(self) -> Optional[str]:
        if self.is_authenticated():
            return self._access_token
        return None

    def refresh_token(self) -> bool:
        if not self._refresh_token: return False
        
        with token_exchange_lock:
            try:
                auth_header = base64.b64encode(f"{self.config.CLIENT_ID}:{self.config.CLIENT_SECRET}".encode()).decode()
                response = requests.post(
                    self.config.TOKEN_URL,
                    data={'grant_type': 'refresh_token', 'refresh_token': self._refresh_token},
                    headers={'Authorization': f'Basic {auth_header}'},
                    timeout=10
                )
                if response.status_code == 200:
                    data = response.json()
                    self._access_token = data['access_token']
                    self._refresh_token = data.get('refresh_token', self._refresh_token)
                    self._expires_at = time.time() + data['expires_in']
                    self._save_tokens()
                    self.logger.info("[DEBUG] Token renovado com sucesso.")
                    return True
                else:
                    self.logger.error(f"[DEBUG] Falha ao renovar token: {response.text}")
                    return False
            except Exception as e:
                self.logger.error(f"[DEBUG] Erro no refresh_token: {e}")
                return False

    def create_auth_flow(self, state: str) -> str:
        from urllib.parse import urlencode
        params = {
            'response_type': 'code',
            'client_id': self.config.CLIENT_ID,
            'state': state,
            'redirect_uri': self.config.REDIRECT_URI,
        }
        return f"https://www.bling.com.br/Api/v3/oauth/authorize?{urlencode(params)}"

    def exchange_code(self, code: str) -> bool:
        try:
            auth_header = base64.b64encode(f"{self.config.CLIENT_ID}:{self.config.CLIENT_SECRET}".encode()).decode()
            response = requests.post(
                self.config.TOKEN_URL,
                data={
                    'grant_type': 'authorization_code',
                    'code': code,
                    'redirect_uri': self.config.REDIRECT_URI
                },
                headers={'Authorization': f'Basic {auth_header}'},
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                self._access_token = data['access_token']
                self._refresh_token = data['refresh_token']
                self._expires_at = time.time() + data['expires_in']
                self._save_tokens()
                return True
            return False
        except Exception as e:
            self.logger.error(f"[DEBUG] Erro no exchange_code: {e}")
            return False

    def get_authorization_url(self) -> str:
        return '/auth'

    def _save_oauth_state(self, state: str):
        try:
            with open(self.OAUTH_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"state": state}, f)
        except Exception: pass

    def _load_oauth_state(self) -> Optional[str]:
        if not self.OAUTH_STATE_FILE.exists(): return None
        try:
            with open(self.OAUTH_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("state")
        except Exception: return None

# ============================================================================ 
# 6. SALES MANAGER
# ============================================================================

class SalesManager:
    def __init__(self, config: Config, logger):
        self.config = config
        self.logger = logger
        self.lock = Lock()
        self.recalculation_lock = Lock()
        self._recalculation_running = False
        
        data = load_stats(config.SALES_STATS_FILE)
        self.daily_count = data.get('daily_count', 0)
        self.weekly_count = data.get('weekly_count', 0)
        self.monthly_count = data.get('monthly_count', 0)
        self.historic_count = data.get('historic_count', 0)
        self.last_recalculated = data.get('last_recalculated')
        self.stats_history = data.get('stats_history', {})
        self.history_data = data.get('history_data', {})
        self._orders_cache = {}

    def update_stats(self, all_orders: List[Dict[str, Any]]):
        now = datetime.now()
        
        daily_orders = [o for o in all_orders if datetime.fromisoformat(o['data']).date() == now.date()]
        weekly_orders = [o for o in all_orders if datetime.fromisoformat(o['data']) > now - timedelta(days=7)]
        monthly_orders = [o for o in all_orders if datetime.fromisoformat(o['data']).month == now.month]
        
        with self.lock:
            self.daily_count = len(daily_orders)
            self.weekly_count = len(weekly_orders)
            self.monthly_count = len(monthly_orders)
            self.historic_count = len(all_orders)
            self.last_recalculated = now
            self._orders_cache = {o.get('id'): o for o in all_orders[-100:]}
            
            # Cálculo de histórico simplificado
            dates = []
            counts = []
            for i in range(29, -1, -1):
                d = (now - timedelta(days=i)).date()
                dates.append(d)
                counts.append(len([o for o in all_orders if datetime.fromisoformat(o['data']).date() == d]))
            
            moving_avg = []
            for i in range(len(counts)):
                start = max(0, i-6)
                window = counts[start:i+1]
                moving_avg.append(round(sum(window)/len(window), 1))
            
            prev_week = sum(counts[16:23])
            curr_week = sum(counts[23:30])
            growth = ((curr_week - prev_week) / prev_week * 100) if prev_week > 0 else 0
            
            self.stats_history = {
                'dates': [d.isoformat() for d in dates],
                'daily': counts,
                'moving_avg': moving_avg,
                'growth': round(growth, 1),
                'avg_daily': round(sum(counts)/30, 1)
            }
            
        save_stats(self._get_state_for_save(), self.config.SALES_STATS_FILE)

    def _get_state_for_save(self):
        return {
            'daily_count': self.daily_count,
            'weekly_count': self.weekly_count,
            'monthly_count': self.monthly_count,
            'historic_count': self.historic_count,
            'last_recalculated': self.last_recalculated,
            'stats_history': self.stats_history,
            'history_data': self.history_data
        }

# ============================================================================ 
# 7. ORCHESTRATOR (WORKER DE FUNDO)
# ============================================================================

class Orchestrator:
    def __init__(self, config: Config, auth_manager: AuthManager, api_client: BlingAPIClient, sales_manager: SalesManager):
        self.config = config
        self.auth = auth_manager
        self.api = api_client
        self.sales = sales_manager
        self.logger = logging.getLogger('bling_automacao')
        self.sales.orchestrator = self
        self._running = False
        self._worker_thread = None
        self._products_cache = {}
        self._kits_cache = {}
        self._load_cache()
        self._cache_lock = Lock()
        self._component_usage_cache = None
        
    def _load_cache(self):
        data = load_products_cache(self.config.PRODUCTS_CACHE_FILE)
        if data:
            with self._cache_lock:
                self._products_cache = {p['id']: p for p in safe_iter(data.get('products'))}
                self._kits_cache = {k['id']: k for k in safe_iter(data.get('kits'))}
                self.logger.info(f"[DEBUG] Cache carregado: {len(self._products_cache)} produtos, {len(self._kits_cache)} kits.")

    def start_worker(self):
        if not self._running:
            self._running = True
            self._stop_event = Event()
            self._worker_thread = Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()
            self.logger.info("[DEBUG] Worker de fundo iniciado.")

    def is_running(self) -> bool:
        return self._running

    def _worker_loop(self):
        cycle_count = 0
        while not self._stop_event.is_set():
            cycle_count += 1
            if not self.auth.is_authenticated():
                self._stop_event.wait(60)
                continue
            try:
                if cycle_count == 1 or cycle_count % 3 == 0:
                    self.process_products_cache()
                
                self.process_sales_orders()
                
                usage = self.calculate_component_usage()
                if usage:
                    self._component_usage_cache = usage
                    self.broadcast_kpi_update(sales_stats=self.sales._get_state_for_save(), component_usage=usage)

            except Exception as e:
                self.logger.exception(f"[DEBUG] Erro no ciclo #{cycle_count}: {e}")

            self._stop_event.wait(600)

    def process_products_cache(self):
        """Busca produtos da loja Tray e atualiza cache."""
        self.logger.info(f"[DEBUG] Atualizando cache de produtos da loja {self.config.TRAY_LOJA_ID}...")
        all_products = []
        page = 1
        
        while True:
            params = {
                'pagina': page,
                'limite': 100,
                'idLoja': self.config.TRAY_LOJA_ID,
                'criterio': 1 # Ativos
            }
            res = self.api.get('produtos', params=params)
            if not res or 'data' not in res or not res['data']:
                break
            
            for p in res['data']:
                # Filtra apenas produto pai (formato v3: 'formato' pode indicar se é variação)
                # Na v3, produtos pai costumam ter 'variacoes' ou formato 'P'
                if p.get('formato') == 'V': # Ignora variações na listagem principal
                    continue
                all_products.append(p)
            
            if len(res['data']) < 100: break
            page += 1
            time.sleep(0.5)

        with self._cache_lock:
            self._products_cache = {str(p['id']): p for p in all_products}
            save_products_cache({'products': list(self._products_cache.values()), 'kits': []}, self.config.PRODUCTS_CACHE_FILE)
        
        self.logger.info(f"[DEBUG] Cache atualizado: {len(self._products_cache)} produtos pai encontrados.")

    def process_sales_orders(self, force: bool = False):
        with self.sales.recalculation_lock:
            if self.sales._recalculation_running and not force: return
            self.sales._recalculation_running = True
        
        try:
            now = datetime.now()
            start_date = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0)
            
            params = {
                'dataEmissaoInicial': start_date.strftime('%Y-%m-%d'),
                'dataEmissaoFinal': now.strftime('%Y-%m-%d %H:%M:%S'),
                'idLoja': self.config.TRAY_LOJA_ID,
                'limite': 100 
            }
            
            all_orders = []
            page = 1
            while True:
                params['pagina'] = page
                res = self.api.get('pedidos/vendas', params=params)
                if not res or 'data' not in res or not res['data']: break
                all_orders.extend(res['data'])
                if len(res['data']) < 100: break
                page += 1
            
            self.sales.update_stats(all_orders)
        finally:
            self.sales._recalculation_running = False

    def calculate_component_usage(self) -> Dict[str, Any]:
        """Calcula uso de componentes baseado nos pedidos de venda."""
        self.logger.info("[DEBUG] Calculando uso de componentes...")
        orders = list(self.sales._orders_cache.values())
        component_totals = defaultdict(int)
        product_sales = defaultdict(int)
        
        for order in orders:
            # Na v3, itens estão em order['itens']
            for item in safe_iter(order.get('itens')):
                nome = item.get('descricao', '').lower()
                qtd = item.get('quantidade', 0)
                product_sales[item.get('descricao', 'Desconhecido')] += qtd
                
                if 'cadeira' in nome:
                    for comp, unit_qtd in RECEITA_CADEIRA.items():
                        component_totals[comp] += qtd * unit_qtd
        
        result = []
        for name, total in component_totals.items():
            result.append({"nome": name, "quantidade": total})
        
        # Formata vendas de produtos para o gráfico/lista
        products_list = [{"nome": k, "quantidade": v} for k, v in product_sales.items()]
        products_list.sort(key=lambda x: x['quantidade'], reverse=True)
        
        return {
            "components": sorted(result, key=lambda x: x['quantidade'], reverse=True),
            "products_sold": products_list[:10]
        }

    def broadcast_kpi_update(self, sales_stats=None, component_usage=None):
        global kpi_update_callbacks, kpi_update_lock
        payload = {
            "type": "full_update",
            "authenticated": self.auth.is_authenticated(),
            "sales_stats": sales_stats,
            "component_usage": component_usage,
            "auth_url": self.auth.get_authorization_url()
        }
        with kpi_update_lock:
            for cb in kpi_update_callbacks:
                try: cb(payload)
                except: pass

# ============================================================================ 
# 8. WEB SERVER (FLASK)
# ============================================================================

class WebServer:
    def __init__(self, config: Config, orchestrator: Orchestrator, flask_app: Flask):
        self.config = config
        self.orchestrator = orchestrator
        self.app = flask_app
        self.app.orchestrator = orchestrator
        self.sock = Sock(self.app)
        self._setup_routes()
        self._setup_websockets()

    def _setup_routes(self):
        @self.app.route('/')
        def index():
            return render_template_string(DASHBOARD_TEMPLATE, auth_url='/auth')

        @self.app.route('/auth')
        def auth():
            state = secrets.token_urlsafe(32)
            self.orchestrator.auth._save_oauth_state(state)
            return redirect(self.orchestrator.auth.create_auth_flow(state))

        @self.app.route('/callback')
        def callback():
            code = request.args.get('code')
            if self.orchestrator.auth.exchange_code(code):
                return redirect(url_for('index'))
            return "Erro na autenticação", 400

        @self.app.route('/api/products/search')
        def search_products():
            q = request.args.get('q', '').lower()
            results = []
            with self.orchestrator._cache_lock:
                for p in self.orchestrator._products_cache.values():
                    if q in p.get('nome', '').lower() or q in p.get('codigo', '').lower():
                        results.append(p)
            return jsonify(results[:20])

        @self.app.route('/api/kits')
        def get_kits():
            with self.orchestrator._cache_lock:
                return jsonify(list(self.orchestrator._products_cache.values()))

        @self.app.route('/api/sales/history')
        def sales_history():
            return jsonify(self.orchestrator.sales.stats_history)

    def _setup_websockets(self):
        @self.sock.route('/ws/kpi-updates')
        def ws_kpi(ws):
            def callback(payload):
                try: ws.send(json.dumps(payload))
                except: raise ConnectionClosed()
            
            with kpi_update_lock:
                kpi_update_callbacks.append(callback)
            
            # Envia estado inicial
            initial = {
                "type": "full_update",
                "authenticated": self.orchestrator.auth.is_authenticated(),
                "sales_stats": self.orchestrator.sales._get_state_for_save(),
                "component_usage": self.orchestrator._component_usage_cache
            }
            ws.send(json.dumps(initial))
            
            try:
                while True: ws.receive(timeout=60)
            finally:
                with kpi_update_lock:
                    if callback in kpi_update_callbacks: kpi_update_callbacks.remove(callback)

# ============================================================================ 
# 9. DASHBOARD TEMPLATE
# ============================================================================

DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel SW Móveis - Gestão Tray</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
    <style>
        :root {
            --primary: #0f172a;
            --accent: #6366f1;
            --bg-light: #f8fafc;
        }
        body { background: var(--bg-light); font-family: sans-serif; }
        .navbar { background: var(--primary); color: white; padding: 1rem; }
        .card { border: none; border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); margin-bottom: 1.5rem; }
        .kpi-card { text-align: center; padding: 1.5rem; }
        .kpi-value { font-size: 2rem; font-weight: bold; color: var(--accent); }
        .nav-tabs .nav-link { color: #64748b; font-weight: 500; border: none; }
        .nav-tabs .nav-link.active { color: var(--accent); border-bottom: 3px solid var(--accent); background: none; }
        .product-img { width: 60px; height: 60px; object-fit: contain; border-radius: 8px; background: #eee; }
        .expansion-area { background: #f1f5f9; border-radius: 8px; padding: 1rem; margin-top: 1rem; }
        .debug-msg { font-family: monospace; font-size: 0.8rem; color: #ef4444; }
    </style>
</head>
<body>
    <nav class="navbar d-flex justify-content-between">
        <span class="fw-bold">SW Móveis - Gestão Tray</span>
        <div id="auth-status">
            <span class="badge bg-secondary me-2" id="status-text">Verificando...</span>
            <a href="{{ auth_url }}" class="btn btn-sm btn-outline-light" id="btn-auth">Autenticar</a>
        </div>
    </nav>

    <div class="container py-4">
        <div class="row">
            <div class="col-md-4">
                <div class="card kpi-card">
                    <h6>Vendas Hoje</h6>
                    <div class="kpi-value" id="kpi-today">0</div>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card kpi-card">
                    <h6>Vendas (7 dias)</h6>
                    <div class="kpi-value" id="kpi-week">0</div>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card kpi-card">
                    <h6>Vendas (Mês)</h6>
                    <div class="kpi-value" id="kpi-month">0</div>
                </div>
            </div>
        </div>

        <ul class="nav nav-tabs mb-4" id="mainTabs">
            <li class="nav-item"><a class="nav-link active" data-bs-toggle="tab" href="#search">🔍 Busca</a></li>
            <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#products">📦 Produtos</a></li>
            <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#components">🔧 Componentes</a></li>
        </ul>

        <div class="tab-content">
            <div class="tab-pane fade show active" id="search">
                <div class="input-group mb-3">
                    <input type="text" id="search-input" class="form-control" placeholder="Buscar por nome ou SKU...">
                    <button class="btn btn-primary" onclick="doSearch()">Buscar</button>
                </div>
                <div id="search-results"></div>
            </div>

            <div class="tab-pane fade" id="products">
                <div id="products-list" class="row"></div>
            </div>

            <div class="tab-pane fade" id="components">
                <div class="card p-3">
                    <h5>Resumo de Insumos (Baseado em Vendas)</h5>
                    <div class="table-responsive">
                        <table class="table">
                            <thead><tr><th>Insumo</th><th>Qtd Total</th></tr></thead>
                            <tbody id="components-body"></tbody>
                        </table>
                    </div>
                </div>
                <div class="card p-3 mt-3">
                    <h5>Produtos Vendidos (Controle de Produção)</h5>
                    <div id="products-sold-list"></div>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        let ws;
        const RECEITA_CADEIRA = {{ RECEITA_CADEIRA | tojson }};

        function connectWS() {
            const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
            ws = new WebSocket(`${proto}://${window.location.host}/ws/kpi-updates`);
            ws.onmessage = (e) => {
                const data = JSON.parse(e.data);
                if (data.type === 'full_update') {
                    updateUI(data);
                }
            };
            ws.onclose = () => setTimeout(connectWS, 3000);
            ws.onerror = (err) => console.error("WS Error:", err);
        }

        function updateUI(data) {
            const statusBadge = document.getElementById('status-text');
            if (data.authenticated) {
                statusBadge.className = 'badge bg-success me-2';
                statusBadge.innerText = 'Autenticado (Loja Tray)';
                document.getElementById('btn-auth').classList.add('d-none');
            } else {
                statusBadge.className = 'badge bg-danger me-2';
                statusBadge.innerText = 'Não Autenticado';
                document.getElementById('btn-auth').classList.remove('d-none');
            }

            if (data.sales_stats) {
                document.getElementById('kpi-today').innerText = data.sales_stats.daily_count || 0;
                document.getElementById('kpi-week').innerText = data.sales_stats.weekly_count || 0;
                document.getElementById('kpi-month').innerText = data.sales_stats.monthly_count || 0;
            }

            if (data.component_usage) {
                const body = document.getElementById('components-body');
                body.innerHTML = data.component_usage.components.map(c => 
                    `<tr><td>${c.nome}</td><td class="fw-bold text-primary">${c.quantidade}</td></tr>`
                ).join('');

                const soldList = document.getElementById('products-sold-list');
                soldList.innerHTML = data.component_usage.products_sold.map(p => 
                    `<div class="d-flex justify-content-between border-bottom py-2">
                        <span>${p.nome}</span>
                        <span class="badge bg-info">${p.quantidade} vendidas</span>
                    </div>`
                ).join('');
            }
        }

        async function doSearch() {
            const q = document.getElementById('search-input').value;
            const res = await fetch(`/api/products/search?q=${q}`);
            const data = await res.json();
            renderProducts(data, 'search-results');
        }

        function renderProducts(products, targetId) {
            const container = document.getElementById(targetId);
            if (!products.length) {
                container.innerHTML = '<div class="alert alert-info">Nenhum produto encontrado.</div>';
                return;
            }
            container.innerHTML = products.map(p => {
                const isCadeira = p.nome.toLowerCase().includes('cadeira');
                return `
                <div class="card p-3 mb-2" style="cursor:pointer" onclick="toggleExpansion(this)">
                    <div class="d-flex align-items-center">
                        <img src="${p.imagemURL || ''}" class="product-img me-3" onerror="this.src='https://via.placeholder.com/60'">
                        <div class="flex-grow-1">
                            <div class="fw-bold">${p.nome}</div>
                            <small class="text-muted">SKU: ${p.codigo || p.id}</small>
                        </div>
                        <span class="badge bg-light text-dark border">Clique p/ Insumos</span>
                    </div>
                    <div class="expansion-area d-none">
                        <h6>📋 Insumos e Componentes</h6>
                        ${isCadeira ? `
                            <table class="table table-sm mt-2">
                                ${Object.entries(RECEITA_CADEIRA).map(([name, qty]) => `
                                    <tr><td>${name}</td><td class="text-end">${qty}x</td></tr>
                                `).join('')}
                            </table>
                        ` : '<p class="text-muted small">Sem componentes cadastrados para este item.</p>'}
                    </div>
                </div>`;
            }).join('');
        }

        function toggleExpansion(card) {
            const area = card.querySelector('.expansion-area');
            area.classList.toggle('d-none');
        }

        connectWS();
        // Carga inicial de produtos
        fetch('/api/kits').then(r => r.json()).then(d => renderProducts(d, 'products-list'));
    </script>
</body>
</html>
"""

# ============================================================================ 
# 10. EXECUÇÃO
# ============================================================================

def create_app() -> Flask:
    config = Config()
    auth_manager = AuthManager(config)
    api_client = BlingAPIClient(config, auth_manager)
    sales_manager = SalesManager(config, logger)
    
    orchestrator = Orchestrator(
        config=config,
        auth_manager=auth_manager,
        api_client=api_client,
        sales_manager=sales_manager,
    )
    
    flask_app = Flask(__name__)
    flask_app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'sw-moveis-tray-2026')
    
    WebServer(config, orchestrator, flask_app) 
    
    # Inicia o worker
    if not orchestrator.is_running():
        orchestrator.start_worker()
        start_cleanup_timer()
    
    return flask_app

app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
