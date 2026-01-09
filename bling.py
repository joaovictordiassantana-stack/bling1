#!/usr/bin/env python3

"""
================================================================================
bling.py - Sistema de Automação Bling com OAuth 2.0 e Dashboard Web Premium
================================================================================

Autor: João Victor Dias Santana
Copyright (c) 2025 João Victor Dias Santana

Implementa integração completa com Bling API v3, gerenciamento de estoque,
KPIs de vendas em tempo real via WebSocket e dashboard interativo.

Versão: 4.6 (Refatorado - V12 - Fluxo de Worker Pós-OAuth e Proteção de Cache)
Última atualização: Dezembro 2025
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

import hmac
import hashlib

from pathlib import Path
from datetime import datetime, timedelta
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
# Lock global para impedir múltiplas trocas de token simultâneas (Erro Worker Timeout)
token_exchange_lock = Lock()
kpi_update_callbacks: List[Callable] = []
kpi_update_lock = Lock()

# ============================================================================ 
# 1. LOGS AVANÇADOS
# ============================================================================

class InMemoryLogHandler(logging.Handler):
    """Handler de log que armazena os registros em memória para o WebSocket."""
    def __init__(self, max_logs=50):  # ✅ Reduz de 100 para 50
        super().__init__()
        self.logs = []
        self.max_logs = max_logs
        self.formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%dT%H:%M:%S'
        )
        # ✅ ADICIONE: Lista de callbacks ativos
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
            
            # ✅ ADICIONE: Notifica todos os WebSockets ativos
            with self.ws_lock:
                dead_callbacks = []
                for cb in self.ws_callbacks:
                    try:
                        cb(log_entry)
                    except Exception:
                        logger.exception("Erro ao notificar callback WebSocket")
                        dead_callbacks.append(cb)
                
                # Remove callbacks mortos
                for cb in dead_callbacks:
                    self.ws_callbacks.remove(cb)

        except Exception:
            self.handleError(record)
    
    def get_logs(self, limit: Optional[int] = None) -> List[Dict[str, str]]:
        if limit:
            return self.logs[-limit:]
        return self.logs.copy()
        
    # ✅ ADICIONE: Métodos para gerenciar callbacks
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
    
    # Define o log principal para INFO (ou DEBUG se necessário, mas INFO é o padrão)
    logger = logging.getLogger('bling_automacao')
    
    logger.setLevel(logging.INFO) 
    # ✅ Suprime logs repetitivos
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    logging.getLogger('flask_sock').setLevel(logging.WARNING)
    
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

# ✅ FUNÇÕES DE LIMPEZA DE CALLBACKS (Definidas após o logger)
def cleanup_kpi_callbacks():
    """Remove callbacks órfãos a cada 5 minutos"""
    global kpi_update_callbacks
    with kpi_update_lock:
        # Testa cada callback. Se falhar (ex: objeto órfão), remove.
        valid = []
        for cb in kpi_update_callbacks:
            try:
                # Tenta acessar um atributo ou chamar o callback. Se falhar, é órfão.
                _ = getattr(cb, '__name__', 'lambda_or_partial') # Teste robusto
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
            time.sleep(300)  # 5 minutos
            cleanup_kpi_callbacks()
    
    Thread(target=cleanup_loop, daemon=True).start()

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
    
    # Rate Limiting (Configurável) - ✅ OTIMIZADO PARA EVITAR 429
    MAX_PAGES_PER_BATCH: int = 2  # ✅ Reduz de 3 para 2 (mais conservador)
    DELAY_BETWEEN_PAGES: float = 4.0  # ✅ Aumenta de 2.5s para 4s
    DELAY_BETWEEN_BATCHES: float = 15.0  # ✅ Aumenta de 8s para 15s (pausa longa)
    
    # Automação
    
    
    # Arquivos
    TOKENS_FILE: Path = Path('tokens.json')

    SALES_STATS_FILE: Path = Path('sales_stats.json') # Persistência de KPIs
    PRODUCTS_CACHE_FILE: Path = Path('products_cache.json') # Persistência de Produtos e Kits

# ============================================================================ 
# 3. UTILITÁRIOS E AUTH (FUNÇÕES SEGURAS)
# ============================================================================

def load_tokens_safe(path: Path | str = "tokens.json"):
    if isinstance(path, str): path = Path(path)
    if not path.exists():
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
        logger.exception(f"Erro lendo {path.name}.")
        return {}

def save_tokens(data: Dict[str, Any], path: Path | str = "tokens.json"):
    if isinstance(path, str): path = Path(path)
    try:
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4, ensure_ascii=False)
        logger.info("Tokens salvos com sucesso.")
    except Exception as e:
        logger.exception("Erro ao salvar tokens.")

def load_stats_safe(path: Path):
    """Carrega as estatísticas de vendas de forma segura."""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Converte a string ISO de volta para datetime
            if data and 'last_recalculated' in data and isinstance(data['last_recalculated'], str):
                 data['last_recalculated'] = datetime.fromisoformat(data['last_recalculated'])
            return data
    except Exception as e:
        logger.exception(f"Erro lendo {path.name}.")
        return None

def save_stats(data: Dict[str, Any], path: Path):
    """Salva as estatísticas de vendas, convertendo datetime para string ISO."""
    try:
        # Cria uma cópia para evitar modificar o objeto original antes do dump
        data_to_save = data.copy()
        if 'last_recalculated' in data_to_save and isinstance(data_to_save['last_recalculated'], datetime):
            data_to_save['last_recalculated'] = data_to_save['last_recalculated'].isoformat()

        with open(path, "w", encoding="utf-8") as file:
            json.dump(data_to_save, file, indent=4, ensure_ascii=False)
        logger.info("Estatísticas de KPIs salvas com sucesso.")
    except Exception as e:
        logger.exception("Erro ao salvar estatísticas de KPIs.")

def safe_dict(data):
    """
    Garante que o objeto é um dict, tentando carregar de string JSON se necessário.
    """
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            return json.loads(data)
        except:
            return {}
    return {}

def load_products_cache(cache_file):
    """
    Carrega cache de produtos e kits do disco.
    Retorna dict vazio se não existir ou falhar.
    """
    if not cache_file or not os.path.exists(cache_file):
        return {}

    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[WARN] Falha ao carregar cache do disco: {e}")
        return {}


def save_products_cache(cache_file, products, kits):
    """
    Salva cache de produtos e kits no disco.
    """
    total_produtos = len(products or []) + len(kits or [])
    
    # ✅ 3. Nunca salvar cache se produtos == 0
    if total_produtos == 0:
        logger.warning("⛔ Cache vazio ignorado. Não salvando no disco.")
        return
        
    try:
        payload = {
            "updated_at": datetime.now().isoformat(),
            "products": products or [],
            "kits": kits or []
        }
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"Cache de produtos e kits salvo com sucesso. Total: {total_produtos}")
    except Exception as e:
        logger.exception("Erro ao salvar cache de produtos.")

def safe_iter(data):
    """Garante que o dado é iterável (lista ou tupla), senão retorna lista vazia."""
    if isinstance(data, (list, tuple)):
        return data
    return []

def safe_get(data, key, default=None):
    """Acesso seguro a chaves de dicionário."""
    if isinstance(data, dict):
        return data.get(key, default)
    return default

def token_required(f):
    """Decorator para verificar se o token está ativo antes de acessar a rota."""
    @wraps(f)
    def decorated(*args, **kwargs):
        from flask import current_app, jsonify
        
        # Acessa o orchestrator anexado ao objeto Flask
        auth_manager = current_app.orchestrator.auth
        
        if not auth_manager.is_authenticated():
            return jsonify({"error": "Não autenticado ou token expirado"}), 401
        
        token = auth_manager.get_access_token()
        if not token:
            return jsonify({"error": "Token de acesso não encontrado"}), 401
            
        return f(*args, token=token, **kwargs)
    return decorated
# ============================================================================

class MetricsManager:
    """Gerencia métricas básicas de observabilidade."""
    def __init__(self):
        self.requests_total = 0
        self.status_codes = defaultdict(int)
        self.latency_sum = 0.0
        self.latency_count = 0
        self.lock = Lock()

    def record_request(self, status_code: int, latency: float):
        with self.lock:
            self.requests_total += 1
            self.status_codes[status_code] += 1
            self.latency_sum += latency
            self.latency_count += 1

    def get_metrics(self) -> Dict[str, Any]:
        with self.lock:
            avg_latency = self.latency_sum / self.latency_count if self.latency_count > 0 else 0.0
            return {
                "requests_total": self.requests_total,
                "status_codes": dict(self.status_codes),
                "avg_latency_ms": round(avg_latency * 1000, 2),
                "errors_401": self.status_codes[401],
                "errors_429": self.status_codes[429],
            }

class BlingAPIClient:
    """
    Cliente HTTP blindado contra quedas de conexão (Errno 104) e Timeouts.
    """
    
    def __init__(self, config: Config, auth_manager):
        self.config = config
        self.auth = auth_manager
        self.logger = logging.getLogger('bling_automacao')
        self.metrics = MetricsManager()
        self.rate_limiter = RateLimiter(min_interval=0.4)
        
        # Configuração de Sessão com Retry Automático
        self.session = requests.Session()
        
        # Estratégia de Retry: Tenta 3 vezes em caso de falha de conexão, reset ou 50x
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,  # Espera 1s, 2s, 4s
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST", "PUT", "DELETE"],
            raise_on_status=False
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self.session.headers.update({
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': 'SWMoveis/4.6 (Integracao Bling)'  # Boa prática
        })
        
    def _request(self, method: str, endpoint: str, **kwargs) -> Optional[Dict[str, Any]]:
        url = f"{self.config.BLING_API_URL}/{endpoint}"
        token = self.auth.get_access_token()
        
        if not token:
            # Silencia erro se for apenas check de startup
            if endpoint != 'pedidos/vendas':
                self.logger.warning(f"Token ausente para {endpoint}.")
            return None
            
        # Garante header de auth atualizado
        kwargs.setdefault('headers', {})
        kwargs['headers']['Authorization'] = f'Bearer {token}'
        
        # Rate Limiter
        self.rate_limiter.wait()
        
        try:
            start_time = time.time()
            # Timeout aumentado para evitar quedas em queries lentas do Bling
            response = self.session.request(method, url, timeout=45, **kwargs)
            latency = time.time() - start_time
            
            self.metrics.record_request(response.status_code, latency)
            
            # Tratamento de Token Expirado (401)
            if response.status_code == 401:
                self.logger.warning(f"Token 401 em {endpoint}. Tentando refresh...")
                if self.auth.refresh_token():
                    new_token = self.auth.get_access_token()
                    kwargs['headers']['Authorization'] = f'Bearer {new_token}'
                    # Tenta novamente (apenas 1 vez para evitar loop infinito)
                    response = self.session.request(method, url, timeout=45, **kwargs)
                else:
                    return None

            if response.status_code == 429:
                self.logger.warning(f"Rate limit (429) em {endpoint}.")
                raise requests.exceptions.HTTPError(response=response)

            response.raise_for_status()
            
            try:
                return response.json()
            except json.JSONDecodeError:
                return {}

        except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
            self.logger.error(f"Erro de Conexão (Reset/Queda) em {endpoint}: {str(e)}")
            # Força recriação da sessão no próximo uso se a conexão estiver corrompida
            self.session.close()
            self.session = requests.Session()
            return None
            
        except Exception as e:
            self.logger.error(f"Erro genérico em {endpoint}: {str(e)}")
            return None

    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        return self._request('GET', endpoint, params=params)

    def post(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        return self._request('POST', endpoint, json=data)

    def put(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        return self._request('PUT', endpoint, json=data)

    def delete(self, endpoint: str) -> Optional[Dict[str, Any]]:
        return self._request('DELETE', endpoint)

# ============================================================================ 
# 5. AUTH MANAGER
# ============================================================================

class AuthManager:
    """Gerencia o ciclo de vida do token OAuth 2.0 do Bling."""
    
    OAUTH_STATE_FILE: Path = Path('oauth_state.json')

    def _save_oauth_state(self, state: str):
        """Salva o state do OAuth de forma persistente em arquivo."""
        try:
            with open(self.OAUTH_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"state": state}, f)
            self.logger.debug("State OAuth salvo em arquivo.")
        except Exception as e:
            self.logger.exception("Erro ao salvar state OAuth.")

    def _load_oauth_state(self) -> Optional[str]:
        """Carrega o state do OAuth do arquivo."""
        if not self.OAUTH_STATE_FILE.exists():
            return None
        try:
            with open(self.OAUTH_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("state")
        except Exception as e:
            self.logger.exception("Erro ao carregar state OAuth.")
            return None

    def _clean_oauth_state(self):
        """Limpa o state do OAuth do arquivo."""
        if self.OAUTH_STATE_FILE.exists():
            try:
                os.remove(self.OAUTH_STATE_FILE)
                self.logger.debug("State OAuth limpo do arquivo.")
            except Exception as e:
                self.logger.exception("Erro ao limpar state OAuth.")
    
    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger('bling_automacao')
        self._tokens = self._load_tokens()
        self._access_token = self._tokens.get('access_token')
        self._refresh_token = self._tokens.get('refresh_token')
        self._expires_at = self._tokens.get('expires_at', 0)
        self._initial_load_failed = True
        
        if not self._access_token and not self._refresh_token:
            self.logger.warning("⚠️ Nenhum token encontrado no arquivo. Necessário realizar autenticação OAuth.")
        elif not self._access_token and self._refresh_token:
            self.logger.info("Refresh Token encontrado. Tentativa de renovação será feita na primeira requisição.") 
        
    def _load_tokens(self) -> Dict[str, Any]:
        """Carrega tokens do arquivo de forma segura."""
        return load_tokens_safe(self.config.TOKENS_FILE)

    def _save_tokens(self):
        """Salva tokens no arquivo."""
        data = {
            'access_token': self._access_token,
            'refresh_token': self._refresh_token,
            'expires_at': self._expires_at
        }
        save_tokens(data, self.config.TOKENS_FILE)

    def is_authenticated(self) -> bool:
        """Verifica se o token de acesso é válido ou pode ser renovado."""
        if self._access_token and self._expires_at > time.time() + 60: # 60s de buffer
            return True
        
        if self._refresh_token:
            return self.refresh_token()
            
        return False

    def get_access_token(self) -> Optional[str]:
        """Retorna o token de acesso, renovando se necessário."""
        if self._access_token and self._expires_at > time.time() + 60:
            return self._access_token
            
        if self._refresh_token:
            if self.refresh_token():
                return self._access_token
                
        return None
    
    def get_authorization_url(self) -> str:
        """Retorna a URL de autenticação (sem usar url_for fora do contexto)."""
        from flask import has_request_context, url_for
        
        if has_request_context():
            # Se estiver em contexto de request, usa url_for
            return url_for('auth', _external=False)
        else:
            # Se estiver fora do contexto (worker/thread), retorna URL hardcoded
            return '/auth'

    def create_auth_flow(self, state: str) -> str:
        """Cria a URL de autorização do Bling, usando o state gerado na sessão do Flask."""
        from urllib.parse import urlencode
        
        params = {
            'response_type': 'code',
            'client_id': self.config.CLIENT_ID,
            'state': state,
            'redirect_uri': self.config.REDIRECT_URI,
        }
        
        return f"https://www.bling.com.br/Api/v3/oauth/authorize?{urlencode(params)}"
    
    def exchange_code_for_token(self, code: str) -> bool:
        """Troca o código de autorização por tokens de acesso e refresh."""
        
        # A validação do state (CSRF) foi movida para a rota /callback (WebServer)
        
        return self._perform_token_request(
            grant_type='authorization_code',
            code=code,
            redirect_uri=self.config.REDIRECT_URI
        )

    def refresh_token(self) -> bool:
        """Renova o token de acesso usando o refresh token."""
        if not self._refresh_token:
            if not self._initial_load_failed:
                self.logger.warning("Não há refresh token disponível para renovação.")
            self._initial_load_failed = False
            return False
            
        self.logger.info("Tentando renovar o token de acesso...")
        
        # O uso de 'with' garante que o lock será liberado, mesmo em caso de exceção.
        with token_exchange_lock:
            # Re-verifica se o token não foi renovado por outra thread enquanto esperava o lock
            if self._access_token and self._expires_at > time.time() + 60:
                self.logger.info("Token já renovado por outra thread.")
                return True
                
            success = self._perform_token_request(
                grant_type='refresh_token',
                refresh_token=self._refresh_token
            )
            
            if success:
                self.logger.info("Token renovado com sucesso.")
            else:
                self.logger.error("Falha na renovação do token.")
                
            return success

    def _perform_token_request(self, grant_type: str, **kwargs) -> bool:
        """Executa a requisição de troca/renovação de token."""
        
        auth_header = base64.b64encode(
            f"{self.config.CLIENT_ID}:{self.config.CLIENT_SECRET}".encode()
        ).decode()
        
        headers = {
            'Authorization': f'Basic {auth_header}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        # ✅ Definição da variável 'data' (Correção de bug: garante que 'data' está definido)
        data = {
            'grant_type': grant_type,
            **kwargs
        }
        
        try:
            response = requests.post(
                self.config.TOKEN_URL,
                headers=headers,
                data=data,
                timeout=self.config.AUTH_TIMEOUT
            )
            response.raise_for_status()
            
            token_data = response.json()
            
            self._access_token = token_data.get('access_token')
            self._refresh_token = token_data.get('refresh_token', self._refresh_token) # Refresh token pode não vir na resposta
            expires_in = token_data.get('expires_in', 3600) # Padrão 1 hora
            self._expires_at = time.time() + expires_in
            
            self._save_tokens()
            return True
            
        except requests.exceptions.HTTPError as e:
            self.logger.exception(f"Erro HTTP na requisição de token. Resposta: {safe_dict(response.text)}")
        except RequestException as e:
            # Garante que 'response' não é acessado aqui
            self.logger.exception(f"Erro de conexão na requisição de token.")
        except Exception as e:
            self.logger.exception(f"Erro inesperado na requisição de token.")
            
        return False

# ============================================================================ 
# 6. SALES MANAGER (KPIs)
# ============================================================================

@dataclass
class SalesManager:
    config: Config
    logger: logging.Logger
    orchestrator: Any = field(default=None)
    
    # Contadores
    daily_count: int = 0
    weekly_count: int = 0
    historic_count: int = 0
    
    # Dados para o Gráfico (Cache)
    history_data: Dict[str, Any] = field(default_factory=dict)
    
    last_recalculated: datetime = field(default_factory=datetime.now)
    lock: Lock = field(default_factory=Lock)
    recalculation_lock: Lock = field(default_factory=Lock)
    _recalculation_running: bool = False

    def __post_init__(self):
        self._load_stats()

    def _load_stats(self):
        with self.lock:
            data = load_stats_safe(self.config.SALES_STATS_FILE)
            if data:
                self.daily_count = data.get('daily', 0)
                self.weekly_count = data.get('weekly', 0)
                self.historic_count = data.get('historic', 0)
                self.history_data = data.get('history_data', {}) # Carrega dados do gráfico
                
                last_recalc = data.get('last_recalculated')
                if isinstance(last_recalc, str):
                    try:
                        self.last_recalculated = datetime.fromisoformat(last_recalc)
                    except:
                        self.last_recalculated = datetime.now()
                else:
                    self.last_recalculated = datetime.now()

    def get_stats(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "daily": self.daily_count,
                "weekly": self.weekly_count,
                "historic": self.historic_count,
                "history_data": self.history_data,
                "last_update": self.last_recalculated.isoformat()
            }

    def _get_state_for_save(self) -> Dict[str, Any]:
        """Retorna o estado atual para persistência ou transmissão."""
        with self.lock:
            return {
                "daily": self.daily_count,
                "weekly": self.weekly_count,
                "historic": self.historic_count,
                "history_data": self.history_data,
                "last_recalculated": self.last_recalculated.isoformat()
            }

    def recalculate_from_orders(self, orders: List[Dict[str, Any]]):
        """
        Calcula KPIs baseado na QUANTIDADE DE ITENS vendidos.
        Gera também os dados para o gráfico de histórico.
        """
        today = datetime.now().date()
        date_7d_ago = today - timedelta(days=7)
        date_30d_ago = today - timedelta(days=30)
        
        daily_sum = 0
        weekly_sum = 0
        historic_sum = 0
        
        # Dicionário para o gráfico: { '2023-10-01': 15, ... }
        daily_breakdown = defaultdict(int)

        for order in orders:
            # Pega a data do pedido (YYYY-MM-DD)
            data_str = safe_get(order.get('data', {}), 'dataEmissao')
            if not data_str: continue
            
            try:
                # Converte apenas para DATA (sem hora), para evitar janelas móveis confusas
                order_date = datetime.strptime(data_str, '%Y-%m-%d').date()
            except: continue

            # Conta total de itens neste pedido
            total_items = 0
            for item in order.get('itens', []):
                total_items += int(float(item.get('quantidade', 0)))

            # 1. Popula dados do gráfico (Agrupa por dia)
            if order_date >= date_30d_ago:
                daily_breakdown[data_str] += total_items

            # 2. Calcula KPIs
            # Diário: Apenas hoje (00:00 até agora)
            if order_date == today:
                daily_sum += total_items
            
            # Semanal: Últimos 7 dias (incluindo hoje)
            if order_date >= date_7d_ago:
                weekly_sum += total_items
                
            # Histórico: Últimos 30 dias (incluindo hoje)
            if order_date >= date_30d_ago:
                historic_sum += total_items

        # Prepara dados formatados para o Chart.js
        chart_labels = []
        chart_data = []
        
        # Preenche os últimos 30 dias (mesmo os dias zerados)
        for i in range(30):
            d = today - timedelta(days=29-i)
            d_str = d.strftime('%Y-%m-%d')
            chart_labels.append(d.strftime('%d/%m'))
            chart_data.append(daily_breakdown.get(d_str, 0))

        # Salva tudo protegido por Lock
        with self.lock:
            self.daily_count = daily_sum
            self.weekly_count = weekly_sum
            self.historic_count = historic_sum
            
            # Salva estrutura pronta para o gráfico
            self.history_data = {
                "labels": chart_labels,
                "data": chart_data,
                "avg": round(historic_sum / 30, 1) if historic_sum > 0 else 0
            }
            
            self.last_recalculated = datetime.now()
            
            # Persiste no disco
            save_stats({
                "daily": self.daily_count,
                "weekly": self.weekly_count,
                "historic": self.historic_count,
                "history_data": self.history_data,
                "last_recalculated": self.last_recalculated.isoformat()
            }, self.config.SALES_STATS_FILE)

        self.logger.info(f"KPIs recalculados: Hoje={daily_sum}, 7D={weekly_sum}, 30D={historic_sum}")

# ============================================================================ 
# 7. ORCHESTRATOR (WORKER DE FUNDO)
# ============================================================================

class Orchestrator:
    """
    Gerencia o worker de fundo para atualização de dados e o ciclo de vida
    do cache de produtos/kits.
    """
    
    def __init__(self, config: "Config", auth_manager: "AuthManager", api_client: "BlingAPIClient", sales_manager: "SalesManager"):
        self.config = config
        self.auth = auth_manager
        self.api = api_client
        self.sales = sales_manager
        self.logger = logging.getLogger('bling_automacao')
        # Garante que o SalesManager tenha a referência correta
        self.sales.orchestrator = self
        self._running = False
        self._worker_thread = None
        self._products_cache = {}
        self._kits_cache = {}
        self._load_cache()
        self._cache_lock = Lock()
        
        # ✅ ADICIONE ESTAS LINHAS:
        self._component_usage_cache = None  # Inicializa o cache de componentes
        self.logger.debug("Orchestrator inicializado com cache de componentes vazio")
        
        # ✅ CORREÇÃO CRÍTICA: Carrega o cache de produtos no startup
        self.logger.info("📦 Carregando cache inicial de produtos (process_products_cache)")
        self.process_products_cache()

    def _load_cache(self):
        """Carrega o cache de produtos/kits do disco."""
        data = load_products_cache(self.config.PRODUCTS_CACHE_FILE)
        if data:
            with self._cache_lock:
                self._products_cache = {p['sku']: p for p in safe_iter(data.get('products'))}
                self._kits_cache = {k['sku']: k for k in safe_iter(data.get('kits'))}
            self.logger.info(f"Cache carregado: {len(self._products_cache)} produtos, {len(self._kits_cache)} kits.")
        else:
            self.logger.warning("Nenhuma cache de produtos/kits encontrado no disco.")

    def get_all_products(self) -> List[Dict[str, Any]]:
        """Retorna todos os produtos simples em cache."""
        with self._cache_lock:
            return list(self._products_cache.values())

    def get_all_kits(self) -> List[Dict[str, Any]]:
        """Retorna todos os kits em cache."""
        with self._cache_lock:
            return list(self._kits_cache.values())

    def is_cache_loaded(self) -> bool:
        """Verifica se o cache de produtos/kits foi carregado (não está vazio)."""
        with self._cache_lock:
            return len(self._products_cache) > 0 or len(self._kits_cache) > 0

    def get_product_by_sku(self, sku: str) -> Optional[Dict[str, Any]]:
        """Busca um produto ou kit pelo SKU no cache."""
        with self._cache_lock:
            if sku in self._products_cache:
                return self._products_cache[sku]
            if sku in self._kits_cache:
                return self._kits_cache[sku]
            return None

    def start_worker(self):
        """Inicia o worker de fundo para atualização de dados."""
        if not self._running:
            self._running = True
            self._stop_event = Event() # Evento para sinalizar parada
            
            # ✅ ADICIONE: Verifica se é a primeira execução
            products_empty = len(self._products_cache) == 0
            kits_empty = len(self._kits_cache) == 0
            
            # A lógica de carga inicial foi movida para o callback, pois o token não está disponível aqui.
            # O worker principal ainda inicia, mas ele se protege com a verificação de token.
            
            self._worker_thread = Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()
            self.logger.info("Worker de fundo iniciado.")

    def stop_worker(self):
        """Para o worker de fundo."""
        self._running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._stop_event.set() # Sinaliza para o loop parar
            self._worker_thread.join(timeout=5)
            if self._worker_thread.is_alive():
                self.logger.warning("Worker de fundo não parou em 5s. Forçando término.")
            else:
                self.logger.info("Worker de fundo parado com sucesso.")

    def is_running(self) -> bool:
        """Verifica se o worker está ativo."""
        return self._running

    def _initial_load(self):
        """Carrega cache de produtos na primeira execução."""
        try:
            self.logger.info("⏳ Carregando cache inicial de produtos/kits...")
            self.process_products_cache()
            self.logger.info("✅ Cache inicial carregado com sucesso!")
        except Exception as e:
            self.logger.exception("❌ Erro no carregamento inicial.")
            
    def _worker_loop(self):
        cycle_count = 0
        
        while not self._stop_event.is_set():
            cycle_count += 1
            
            # Verifica autenticação antes de tudo
            if not self.auth.is_authenticated():
                self.logger.info("Aguardando autenticação para iniciar ciclos...")
                self._stop_event.wait(60)
                continue

            try:
                # Ciclo de Produtos (Cache Pesado)
                if cycle_count % 3 == 0:
                    self.logger.info(f"🔄 Ciclo #{cycle_count}: Atualizando cache de produtos...")
                    self.process_products_cache()
                
                # Ciclo de Vendas (KPIs)
                self.logger.info(f"🔄 Ciclo #{cycle_count}: Atualizando Pedidos/KPIs...")
                self.process_sales_orders()
                
                # Ciclo de Componentes
                if cycle_count % 4 == 0:
                    self.logger.info(f"🔄 Ciclo #{cycle_count}: Calculando componentes...")
                    usage = self.calculate_component_usage()
                    if usage.get('components'):
                        self._component_usage_cache = usage
                        self.broadcast_kpi_update(component_usage=usage)

            except Exception as e:
                self.logger.exception(f"Erro fatal no ciclo #{cycle_count}")

            self.logger.info("✅ Ciclo finalizado. Dormindo...")
            # Mantém 10 minutos (600s) pois o cache de produtos levou 5 min
            # Se diminuir muito, vai encavalar.
            self._stop_event.wait(600)

    def process_sales_orders(self, force: bool = False):
        """Busca pedidos de venda faturados/em andamento dos últimos 30 dias e ATUALIZA O SALES_MANAGER POR RECALCULO."""
        
        # Verifica e marca o estado de recalculação dentro do lock
        with self.sales.recalculation_lock:
            if self.sales._recalculation_running and not force:
                self.logger.info("Recálculo de pedidos já em andamento. Pulando esta iteração.")
                return
            self.sales._recalculation_running = True
            
        try:
            # ✅ 1. Bloquear qualquer worker sem token
            if not self.auth.is_authenticated():
                self.logger.warning("⛔ Worker abortado: token inexistente.")
                return
                
            self.logger.info("Iniciando busca COMPLETA de pedidos de venda para recalcular os KPIs (Últimos 30 dias)...")
            now = datetime.now()
            params = {
                'dataEmissaoInicial': (now - timedelta(days=30)).strftime('%Y-%m-%d'),
                'situacao': 'atendidos,em_aberto,em_andamento,faturados,em_producao'
            }
            
            all_orders = []
            page = 1
            
            # ✅ ADICIONE: Limite absoluto de páginas
            MAX_TOTAL_PAGES = 50  # Não processa mais que 50 páginas (5000 itens)
            
            # ✅ Limita a 3 páginas por vez para evitar rate limit
            MAX_PAGES_PER_BATCH = self.config.MAX_PAGES_PER_BATCH
            batch_count = 0
            
            while True:
                params['pagina'] = page
                
                # ✅ TRATAMENTO 429: Captura e aborta o loop
                try:
                    response = self.api.get('pedidos/vendas', params=params)
                except requests.exceptions.HTTPError as e:
                    if e.response and e.response.status_code == 429:
                        self.logger.warning("🛑 Rate limit (429) detectado. Abortando busca de pedidos.")
                        break
                    raise
                
                if response is None:
                    self.logger.error("Falha ao buscar pedidos na API. Tentando usar cache anterior.")
                    break
    
                data = safe_get(response, 'data', [])
                
                # Valida se retornou dados
                if not data or len(data) == 0:
                    self.logger.info(f"Página {page} vazia. Fim da paginação.")
                    break
                
                all_orders.extend(data)
                self.logger.info(f"Página {page} processada. Total acumulado: {len(all_orders)} | Taxa: {len(data)} itens/página")
                
                # Se retornou menos que 100, é a última página
                if len(data) < 100:
                    self.logger.info(f"Última página detectada ({len(data)} itens).")
                    break
    
                page += 1
                
                # ✅ ADICIONE: Proteção contra loops infinitos
                if page > MAX_TOTAL_PAGES:
                    self.logger.warning(f"⚠️ Limite de {MAX_TOTAL_PAGES} páginas atingido. Parando busca.")
                    break
                
                # ✅ PAUSA FIXA: Aguarda 1.2s entre páginas (obrigatório para evitar burst)
                time.sleep(1.2)
        
        except Exception as e:
            self.logger.exception("Erro ao processar pedidos de venda.")
        finally:
            with self.sales.recalculation_lock:
                self.sales._recalculation_running = False

    def process_products_cache(self):
        """Busca e armazena em cache todos os produtos e kits."""
        if not self.auth.is_authenticated():
            return
            
        self.logger.info("Iniciando busca e cache de produtos e kits...")
        all_products = []
        all_kits = []
        page = 1
        
        while True:
            # Busca produtos (Tipo P e K)
            try:
                response = self.api.get('produtos', params={'pagina': page, 'tipo': 'P,K', 'limite': 100})
            except Exception as e:
                self.logger.error(f"Erro ao buscar produtos: {e}")
                break
            
            data = safe_get(response, 'data', [])
            if not data: break
            
            for item in data:
                # --- CORREÇÃO DE IMAGEM AQUI ---
                # Tenta pegar da lista 'imagens' (padrão v3) ou 'imagem' (legado)
                img_url = ''
                imagens = item.get('imagens', [])
                if imagens and isinstance(imagens, list) and len(imagens) > 0:
                    img_url = safe_get(imagens[0], 'link', '')
                elif item.get('imagem'):
                    img_url = item.get('imagem')
                # -------------------------------

                produto_normalizado = {
                    'id': item.get('id'),
                    'sku': item.get('codigo'),
                    'nome': item.get('nome') or item.get('descricao'),
                    'tipo': "COMPOSTO" if item.get('tipo') == 'K' else "SIMPLES",
                    'estoqueAtual': safe_get(item.get('estoque', {}), 'saldoVirtualTotal', 0),
                    'imagem': img_url, # Usa a URL corrigida
                    'componentes': []
                }
                
                # Se for Kit, processa componentes
                if item.get('tipo') == 'K':
                    comps = []
                    for c in item.get('componentes', []):
                        comps.append({
                            'sku': safe_get(c.get('produto', {}), 'codigo'),
                            'quantidade': c.get('quantidade', 1)
                        })
                    produto_normalizado['componentes'] = comps
                    all_kits.append(produto_normalizado)
                else:
                    all_products.append(produto_normalizado)

            if len(data) < 100: break
            page += 1
            time.sleep(0.5) # Evita rate limit

        # Salva no cache
        with self._cache_lock:
            self._products_cache = {p['sku']: p for p in all_products}
            self._kits_cache = {k['sku']: k for k in all_kits}
            save_products_cache(self.config.PRODUCTS_CACHE_FILE, all_products, all_kits)
            self.logger.info(f"Cache atualizado: {len(all_products)} produtos, {len(all_kits)} kits.")
            
        self.broadcast_kpi_update(cache_updated=True)

    def calculate_component_usage(self, days: int = 30) -> Dict[str, Any]:
        """
        Calcula uso de componentes com breakdown diário.
        """
        
        # CORREÇÃO: Não tenta calcular se não estiver logado
        if not self.auth.is_authenticated():
            return {"components": [], "daily_breakdown": []}

        now = datetime.now()
        params = {
            'dataEmissaoInicial': (now - timedelta(days=days)).strftime('%Y-%m-%d'),
            'situacao': 'atendidos,em_aberto,em_andamento,faturados,em_producao'
        }
        
        token = self.auth.get_access_token()
        if not token:
            self.logger.warning("Token indisponível para calcular uso de componentes.")
            return {"components": [], "daily_breakdown": []}
            
        all_orders = []
        page = 1
        while True:
            params['pagina'] = page
            
            # ✅ TRATAMENTO 429: Captura e aborta o loop
            try:
                response = self.api.get('pedidos/vendas', params=params)
            except requests.exceptions.HTTPError as e:
                if e.response and e.response.status_code == 429:
                    self.logger.warning("🛑 Rate limit (429) detectado. Abortando cálculo de componentes.")
                    break
                raise
            
            if response is None:
                break
            data = safe_get(response, 'data', [])
            if not data or len(data) == 0:
                break
            all_orders.extend(data)
            if len(data) < 100:
                break
            page += 1
            
            # ✅ PAUSA FIXA: Aguarda 1.2s entre páginas (obrigatório para evitar burst)
            time.sleep(1.2)
            
        # Rastreamento por dia E total
        component_usage = {}  # Total do período
        daily_usage = defaultdict(lambda: defaultdict(int))  # Por dia
        
        for order in all_orders:
            # Extrai data
            data_emissao_str = safe_get(safe_get(order, 'data', {}), 'dataEmissao')
            if not data_emissao_str:
                continue
            
            try:
                order_date = datetime.strptime(data_emissao_str, '%Y-%m-%d')
                day_key = order_date.strftime('%Y-%m-%d')
            except:
                continue
            
            itens = safe_get(order, 'itens', [])
            for item in safe_iter(itens):
                produto_sku = safe_get(item, 'codigo')
                quantidade_vendida = safe_get(item, 'quantidade', 0)
                
                if not produto_sku or quantidade_vendida == 0:
                    continue
                
                produto = self.get_product_by_sku(produto_sku)
                
                # Se é KIT, processa componentes
                if produto and safe_get(produto, 'tipo') == 'K':
                    componentes = safe_get(produto, 'componentes', [])
                    for comp in safe_iter(componentes):
                        comp_sku = safe_get(safe_get(comp, 'produto', {}), 'codigo')
                        comp_nome = safe_get(safe_get(comp, 'produto', {}), 'nome')
                        comp_qtd_por_kit = safe_get(comp, 'quantidade', 0)
                        
                        if not comp_sku:
                            continue
                        
                        qtd_consumida = quantidade_vendida * comp_qtd_por_kit
                        
                        # Atualiza total
                        if comp_sku not in component_usage:
                            component_usage[comp_sku] = {
                                "sku": comp_sku,
                                "nome": comp_nome,
                                "quantidade": 0,
                                "produtos": set()
                            }
                        component_usage[comp_sku]["quantidade"] += qtd_consumida
                        component_usage[comp_sku]["produtos"].add(produto_sku)
                        
                        # Atualiza diário
                        daily_usage[day_key][comp_sku] += qtd_consumida
                
                # Se é PRODUTO SIMPLES, conta também
                else:
                    if produto_sku not in component_usage:
                        component_usage[produto_sku] = {
                            "sku": produto_sku,
                            "nome": safe_get(produto, 'nome', 'Produto'),
                            "quantidade": 0,
                            "produtos": set()
                        }
                    component_usage[produto_sku]["quantidade"] += quantidade_vendida
                    component_usage[produto_sku]["produtos"].add(produto_sku)
                    
                    daily_usage[day_key][produto_sku] += quantidade_vendida
        
        # Formata resultado
        result = []
        for sku, usage in component_usage.items():
            result.append({
                "sku": usage["sku"],
                "nome": usage["nome"],
                "quantidade": usage["quantidade"],
                "produtos": sorted(list(usage["produtos"]))
            })
        
        result.sort(key=lambda x: x['quantidade'], reverse=True)
        
        # Formata consumo diário
        daily_breakdown = []
        for day in sorted(daily_usage.keys(), reverse=True):
            daily_breakdown.append({
                "data": day,
                "componentes": [
                    {"sku": sku, "quantidade": qtd}
                    for sku, qtd in daily_usage[day].items()
                ]
            })
        
        return {
            "components": result,
            "daily_breakdown": daily_breakdown[:7]  # Últimos 7 dias
        }

    def broadcast_kpi_update(self, sales_stats: Optional[Dict[str, Any]] = None, cache_updated: bool = False, component_usage: Optional[Dict[str, Any]] = None):
        """
        Envia uma atualização completa de status via WebSocket para todos os clientes.
        Inclui status de autenticação, KPIs e, se solicitado, uso de componentes.
        """
        global kpi_update_callbacks, kpi_update_lock
        
        # 1. Monta o payload base
        payload = {
            "type": "full_update",
            "authenticated": self.auth.is_authenticated(),
            "is_running": self.is_running(),
            "cache_updated": cache_updated,
            "auth_url": self.auth.get_authorization_url() # Envia a URL de auth para o frontend
        }
        
        # 2. Adiciona KPIs se fornecidos
        if sales_stats:
            # Converte a data de volta para ISO string para o WS
            stats_data = sales_stats.copy()
            stats_data['last_recalculated'] = stats_data['last_recalculated'].isoformat()
            stats_data['last_update'] = stats_data.pop('last_recalculated')
            payload["sales_stats"] = stats_data
            
        # 3. Adiciona o uso de componentes se fornecido
        if component_usage:
            payload["component_usage"] = component_usage
            self.logger.debug("Uso de componentes incluído no broadcast.")
                
        # 4. Envia o broadcast
        with kpi_update_lock:
            for cb in kpi_update_callbacks:
                try:
                    cb(payload)
                except ConnectionClosed:
                    self.logger.debug("Conexão WebSocket fechada ao tentar enviar full_update.")
                except Exception as e:
                    self.logger.exception("Erro ao enviar full_update via callback.")

# ============================================================================ 
# 8. WEB SERVER (FLASK)
# ============================================================================

class WebServer:
    """Configura e executa o servidor Flask com rotas e WebSockets."""
    
    # Locks e estados globais para o servidor
    code_lock = Lock()
    used_codes = set()
    webhook_lock = Lock()
    
    def __init__(self, config: "Config", orchestrator: "Orchestrator", flask_app: Flask):
        self.config = config
        self.orchestrator = orchestrator
        self.logger = logging.getLogger('bling_automacao')
        self.app = flask_app
        self.app.orchestrator = orchestrator # ✅ Anexa o orchestrator ao objeto Flask para acesso global
        self.sock = Sock(self.app)
        self._setup_routes()
        self._setup_websockets()

    # O método run() foi removido para compatibilidade com Gunicorn.
    # A inicialização do worker agora é feita no create_app().
    def _setup_routes(self):
        """Configura todas as rotas HTTP."""
        
        # Rota principal (Dashboard)
        @self.app.route('/')
        def index():
            auth_url = self.orchestrator.auth.get_authorization_url()
            return render_template_string(DASHBOARD_TEMPLATE, auth_url=auth_url)

        # Rota de Autorização OAuth (Gera o state e redireciona para o Bling)
        @self.app.route('/auth')
        def auth():
            from flask import redirect
            import secrets
            
            # 1. GERAÇÃO DO STATE (REGRA DE OURO)
            state = secrets.token_urlsafe(32)
            self.orchestrator.auth._save_oauth_state(state)
            
            # 2. Constrói a URL de autorização usando o AuthManager
            auth_url = self.orchestrator.auth.create_auth_flow(state)
            
            return redirect(auth_url)

        # Rota de Callback OAuth
        @self.app.route('/callback')
        def callback():
            from flask import redirect
            
            code = request.args.get("code")
            received_state = request.args.get("state")
            
            # 1. VALIDAÇÃO DO STATE (CSRF)
            saved_state = self.orchestrator.auth._load_oauth_state()
            
            if not saved_state or saved_state != received_state:
                self.logger.error(
                    f"❌ State inválido detectado! CSRF potencial. "
                    f"Saved: {saved_state}, Received: {received_state}"
                )
                # Limpa o state em caso de falha (boa prática)
                self.orchestrator.auth._clean_oauth_state()
                return redirect("/?error=csrf")
            
            if self.orchestrator.auth.is_authenticated():
                self.logger.info("Callback ignorado: Usuário já autenticado.")
                return redirect('/')
            
            if not code:
                self.logger.error("Callback sem code.")
                return redirect('/') 
            
            # ✅ ADICIONE logging detalhado:
            self.logger.info(f"Callback recebido - Code: {code[:10]}...")
            
            # NOTA: O uso de 'with' padrão bloquearia. A lógica abaixo garante a não-concorrência 
            # e a saída imediata, se o lock já estiver sendo usado.
            if not token_exchange_lock.acquire(blocking=False):
                self.logger.warning("Concorrência detectada no callback. Redirecionando.")
                return redirect('/')
                
            try:
                with WebServer.code_lock:
                    if code in WebServer.used_codes:
                        return redirect('/')
                    WebServer.used_codes.add(code)
                
                self.logger.info(f"Processando callback code...")
                # O state não é mais passado para exchange_code_for_token, pois já foi validado
                success = self.orchestrator.auth.exchange_code_for_token(code)
                
                if not success:
                    self.logger.error("Falha na troca de token (erro de API). Redirecionando.")
                    # Limpa o state em caso de falha (boa prática)
                    self.orchestrator.auth._clean_oauth_state()
                    return redirect('/')
                
                # 2. LIMPEZA DO STATE APÓS SUCESSO
                self.orchestrator.auth._clean_oauth_state()
                
                # Após a autenticação, envia um full_update para o frontend
                if success:
                    # ✅ 2. Após /callback, FORÇAR reload do cache e KPIs
                    self.logger.info("✅ Autenticação bem-sucedida. Forçando carga inicial de dados (KPIs e Cache).")

                    # Executa o recálculo e o cache em threads separadas para não bloquear o callback
                    executor = ThreadPoolExecutor(max_workers=2)
                    executor.submit(self.orchestrator.process_sales_orders)
                    executor.submit(self.orchestrator.process_products_cache)
                    executor.shutdown(wait=False)

                    # O broadcast será feito no final de process_products_cache
                    
                    # CORREÇÃO PROBLEMA 1: Iniciar worker após autenticação
                    if not self.orchestrator.is_running():
                        self.orchestrator.start_worker()
                        start_cleanup_timer()
                        self.logger.info("✅ Worker iniciado após autenticação bem-sucedida.")
                
                return redirect('/')
            except Exception as e:
                self.logger.exception("Erro crítico no callback.")
                return redirect('/')
            finally:
                token_exchange_lock.release()

        @self.app.route('/api/status')
        def api_status():
            return jsonify({
                "authenticated": self.orchestrator.auth.is_authenticated(),
                "auth_url": self.orchestrator.auth.get_authorization_url(),
                "is_running": self.orchestrator.is_running()
            })

        @self.app.route('/api/sales/stats')
        @token_required
        def api_sales_stats(token):
            """Retorna os contadores Diário, Semanal e Histórico."""
            stats = self.orchestrator.sales.get_stats()
            
            
            
            return jsonify(stats)
        
        @self.app.route("/api/metrics")
        @token_required
        def api_metrics(token):
            """Retorna métricas de observabilidade da API."""
            metrics = self.orchestrator.api.metrics.get_metrics()
            return jsonify(metrics)

        @self.app.route("/api/sales/history")
        @token_required
        def api_sales_history(token):
            """Retorna o histórico de vendas já processado pelo Worker."""
            # Pega os dados direto da memória do SalesManager
            history = self.orchestrator.sales.history_data
            
            if not history:
                return jsonify({
                    "labels": [],
                    "daily": [],
                    "moving_avg": [],
                    "growth": 0,
                    "avg_daily": 0
                })

            # Calcula dados derivados (Média Móvel e Crescimento) aqui ou no frontend
            # Para simplificar, retornamos o que já temos
            
            data_values = history.get('data', [])
            
            # Cálculo de média móvel simples (7 dias)
            moving_avg = []
            for i in range(len(data_values)):
                start = max(0, i-6)
                subset = data_values[start:i+1]
                avg = sum(subset) / len(subset) if subset else 0
                moving_avg.append(round(avg, 1))

            # Cálculo de crescimento (Últimos 7 vs 7 anteriores)
            last_7 = sum(data_values[-7:])
            prev_7 = sum(data_values[-14:-7])
            growth = 0
            if prev_7 > 0:
                growth = ((last_7 - prev_7) / prev_7) * 100

            return jsonify({
                "labels": history.get('labels', []),
                "daily": data_values,
                "moving_avg": moving_avg,
                "growth": round(growth, 1),
                "avg_daily": history.get('avg', 0)
            })


        @self.app.route('/api/recalculate', methods=['POST'])
        @token_required
        def api_recalculate(token):
            """Força o recálculo dos KPIs em uma thread separada."""
            
            # Verifica e marca o estado de recalculação dentro do lock
            with self.orchestrator.sales.recalculation_lock:
                if self.orchestrator.sales._recalculation_running:
                    self.logger.warning("Recálculo de KPIs já em andamento. Requisição ignorada.")
                    return jsonify({"status": "already_running", "message": "Recálculo de KPIs já em andamento."}), 202
                
                self.orchestrator.sales._recalculation_running = True

            # Executa o recálculo em uma thread separada para não bloquear a requisição HTTP
            executor = ThreadPoolExecutor(max_workers=1)
            executor.submit(self.orchestrator.process_sales_orders)
            executor.shutdown(wait=False)
            
            return jsonify({"status": "started", "message": "Recálculo de KPIs iniciado em segundo plano."}), 200
        @self.app.route('/api/products/search')
        @token_required
        def api_products_search(token):
            """Busca produtos e kits no cache pelo SKU ou nome."""
            query = request.args.get('q', '').lower()
            if not query:
                return jsonify([])
                
            results = []
            
            # Busca em produtos simples
            # ✅ CORREÇÃO CRÍTICA (Passo 3): Usa o cache completo e padronizado
            all_products = self.orchestrator.get_all_products() + self.orchestrator.get_all_kits()
            
            for p in all_products:
                name = safe_get(p, 'nome', '').lower()
                sku = safe_get(p, 'sku', '').lower()
                
                if query in name or query in sku:
                    # ✅ Usa o modelo padronizado do cache
                    results.append({
                        "id": p.get("id"),
                        "nome": p.get("nome"),
                        "sku": p.get("sku"),
                        "tipo": p.get("tipo"),
                        "imagem": p.get("imagem"), # Já normalizado
                        "componentes": p.get("componentes", []) # Já padronizado
                    })

            self.logger.info(
                f"🔍 Busca | resultados={len(results)} | com_componentes="
                f"{sum(1 for r in results if r.get('componentes') and len(r['componentes']) > 0)}"
            )
            return jsonify(results[:10]) # Limita a 10 resultados

        @self.app.route('/api/kits')
        @token_required
        def api_kits(token):
            """Retorna a lista de todos os kits e produtos simples em cache."""
            kits = self.orchestrator.get_all_kits()
            products = self.orchestrator.get_all_products()
            
            self.logger.info(f"📦 Endpoint /api/kits chamado. Kits: {len(kits)}, Produtos: {len(products)}")
            
            return jsonify(kits + products)


        @self.app.route('/_health')
        def health_check():
            """Endpoint de health check para orquestradores."""
            status = {
                "status": "ok",
                "worker_running": self.orchestrator.is_running(),
                "auth_valid": self.orchestrator.auth.is_authenticated(),
                "cache_loaded": self.orchestrator.is_cache_loaded()
            }
            return jsonify(status), 200

        @self.app.route('/api/force-load', methods=['POST'])
        @token_required
        def api_force_load(token):
            """Força o recarregamento do cache de produtos/kits em uma thread separada."""
            
            # Verifica se o processamento já está em andamento sem alterar o estado do lock
            if not self.orchestrator._cache_lock.acquire(blocking=False):
                self.logger.warning("Recarregamento de cache já em andamento. Requisição ignorada.")
                return jsonify({"message": "Recarregamento de cache já em andamento."}), 202
            self.orchestrator._cache_lock.release() # Libera imediatamente (apenas para testar)

            # Executa o recarregamento em uma thread separada para não bloquear a requisição HTTP
            executor = ThreadPoolExecutor(max_workers=1)
            executor.submit(self.orchestrator.process_products_cache)
            executor.shutdown(wait=False)
            
            return jsonify({"message": "Recarregamento do cache de produtos/kits iniciado em segundo plano."}), 202

        @self.app.route('/api/components/usage')
        @token_required
        def api_component_usage(token):
            """Retorna uso de componentes (do cache do worker)."""
            try:
                # Retorna cache se disponível E não vazio
                cache = getattr(self.orchestrator, '_component_usage_cache', None)
                
                if cache and (cache.get('components') or cache.get('daily_breakdown')):
                    self.logger.info(f"📦 Retornando cache: {len(cache.get('components', []))} componentes")
                    return jsonify(cache)
                
                # Calcula sob demanda
                self.logger.info("🔄 Cache vazio. Calculando componentes sob demanda...")
                usage_data = self.orchestrator.calculate_component_usage()
                
                # Armazena no cache para reutilizar
                self.orchestrator._component_usage_cache = usage_data
                
                return jsonify(usage_data)
                
            except Exception as e:
                self.logger.exception("Erro ao processar /api/components/usage")
                return jsonify({
                    "error": str(e),
                    "components": [],
                    "daily_breakdown": []
                }), 500

        @self.app.route('/webhook', methods=['POST'])
        def webhook():
            """Recebe webhooks do Bling com validação HMAC."""
            
            with WebServer.webhook_lock:
                try:
                    # 1. Valida assinatura
                    signature = request.headers.get('X-Bling-Signature')
                    if not signature:
                        self.logger.error("❌ Webhook sem assinatura X-Bling-Signature")
                        return jsonify({"error": "Assinatura ausente"}), 403
                    
                    # Gera HMAC esperado
                    secret = self.config.CLIENT_SECRET.encode()
                    expected = hmac.new(secret, request.data, hashlib.sha256).hexdigest()
                    
                    if not hmac.compare_digest(signature, expected):
                        self.logger.error(f"❌ Assinatura inválida. Esperado: {expected[:10]}...")
                        return jsonify({"error": "Assinatura inválida"}), 403
                    
                    # 2. Processa webhook
                    data = request.json
                    tipo = safe_get(data, 'tipo')
                    evento = safe_get(data, 'evento')

                    self.logger.info(f"📩 Webhook válido: {tipo}.{evento}")

                    # 3. Força recálculo de KPIs para pedidos
                    if tipo == 'pedidoVenda' and evento in ['criado', 'alterado', 'faturado']:
                        self.logger.info("🔄 Webhook acionou recálculo de KPIs")
                        # ✅ CORREÇÃO CRÍTICA: Força o recálculo de forma síncrona para garantir a persistência imediata
                        self.orchestrator.process_sales_orders(force=True)
                        self.orchestrator.sales.save_stats() # ✅ Persiste imediatamente para o Power BI
                        # NOTA: A chamada anterior `executor.submit(self.orchestrator.process_sales_orders)` foi removida.
                        
                    return jsonify({"status": "ok", "message": f"Webhook {tipo}.{evento} processado"}), 200

                except Exception as e:
                    self.logger.exception("❌ Erro no webhook")
                    return jsonify({"error": "Erro interno"}), 500

    def _setup_websockets(self):
        """Configura os WebSockets para logs e atualizações de KPI."""
        
        @self.sock.route('/ws/logs')
        def ws_logs(ws):
            self.logger.info("📡 WebSocket logs conectado.")
            
            # ✅ Limite de callbacks para evitar DoS acidental
            if len(memory_handler.ws_callbacks) >= 10:
                self.logger.warning("Limite de 10 conexões de log WS atingido. Conexão recusada.")
                return

            # ✅ Callback seguro para este WebSocket específico
            def ws_callback(log_entry):
                try:
                    ws.send(json.dumps({"logs": [log_entry]}))
                except ConnectionClosed:
                    raise  # Propaga para remoção automática
                except Exception as e:
                    self.logger.exception("Erro enviando log via WS.")
                    raise ConnectionClosed() # Força desconexão
            
            try:
                # Envia logs históricos
                ws.send(json.dumps({"logs": memory_handler.get_logs()}))
                
                # ✅ Registra callback
                memory_handler.add_ws_callback(ws_callback)
                
                while True:
                    # Mantém a conexão aberta, esperando por mensagens (pode ser um ping/pong)
                    ws.receive(timeout=60) 
            except ConnectionClosed:
                pass
            finally:
                # ✅ Remove callback ao desconectar
                memory_handler.remove_ws_callback(ws_callback)
                self.logger.debug("WebSocket logs desconectado")

        
        @self.sock.route('/ws/kpi-updates')
        def ws_kpi_updates(ws):
            self.logger.info("📡 WebSocket KPI conectado.")
            
            # ✅ Limite de callbacks para evitar DoS acidental
            global kpi_update_callbacks, kpi_update_lock
            if len(kpi_update_callbacks) >= 10:
                self.logger.warning("Limite de 10 conexões KPI WS atingido. Conexão recusada.")
                return

            # Função de callback para enviar atualizações completas
            def kpi_callback(payload):
                try:
                    ws.send(json.dumps(payload))
                except ConnectionClosed:
                    # ✅ ADICIONE: Sinaliza para remover este callback
                    raise
                except Exception as e:
                    self.logger.exception("Erro enviando via WS.")
                    raise ConnectionClosed()  # Força desconexão
                
            # 1. Envia o estado inicial completo (status, kpis, uso de componentes)
            # 1. Envia o estado inicial completo
            try:
                sales_stats = self.orchestrator.sales._get_state_for_save()
                
                # Tenta usar cache se disponível
                component_usage = getattr(self.orchestrator, '_component_usage_cache', None)
                
                if not component_usage:
                    self.logger.info("🔄 Cache de componentes vazio. Calculando...")
                    try:
                        component_usage = self.orchestrator.calculate_component_usage()
                        self.orchestrator._component_usage_cache = component_usage
                    except Exception as calc_error:
                        self.logger.error(f"Falha ao calcular componentes: {calc_error}")
                        component_usage = {"components": [], "daily_breakdown": []}
                
                self.orchestrator.broadcast_kpi_update(
                    sales_stats=sales_stats,
                    component_usage=component_usage
                )
                self.logger.info("✅ Estado inicial enviado ao WebSocket")
                
            except Exception as e:
                self.logger.exception("Erro ao enviar estado inicial via WS.")
                
            # 2. Adiciona o callback à lista global
            with kpi_update_lock:
                kpi_update_callbacks.append(kpi_callback)
                
            try:
                while True:
                    # Mantém a conexão aberta
                    ws.receive(timeout=60)
            except ConnectionClosed:
                pass
            finally:
                # 3. Remove o callback ao desconectar
                with kpi_update_lock:
                    if kpi_callback in kpi_update_callbacks:
                        kpi_update_callbacks.remove(kpi_callback)
                self.logger.info("WebSocket KPI desconectado.")

# ============================================================================ 
# 9. DASHBOARD TEMPLATE (HTML/JS/CSS)
# ============================================================================

DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel SW Móveis - Gestão de Pedidos</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet">
    <style>
        /* ✅ DESIGN: Paleta Premium */
        :root {
            --primary: #0f172a;
            --primary-light: #1e293b;
            --accent: #6366f1;
            --accent-light: #818cf8;
            --success: #10b981;
            --warning: #f59e0b;
            --error: #ef4444;
            --bg-light: #f8fafc;
            --border-color: #e2e8f0;
            --text-muted: #64748b;
        }

        /* ✅ DESIGN: Tipografia e Base */
        * {
            transition: background-color 0.2s ease, color 0.2s ease, border-color 0.2s ease;
        }

        body {
            background: linear-gradient(135deg, #ffffff 0%, #f8fafc 100%);
            font-family: 'Geist', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            font-weight: 400;
            line-height: 1.6;
            letter-spacing: -0.01em;
            color: var(--primary);
        }

        h1, h2, h3, h4, h5, h6 {
            font-weight: 600;
            letter-spacing: -0.02em;
            line-height: 1.2;
        }

        /* ✅ DESIGN: Navbar com Gradiente */
        .navbar {
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary-light) 100%);
            color: white;
            box-shadow: 0 4px 20px rgba(15, 23, 42, 0.1);
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            animation: slideDown 0.4s ease-out;
        }

        @keyframes slideDown {
            from {
                opacity: 0;
                transform: translateY(-20px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        .navbar-brand {
            font-size: 1.5rem;
            font-weight: 700;
            letter-spacing: -0.02em;
        }

        /* ✅ DESIGN: Status Badge com Animação */
        #status-badge {
            animation: pulse-badge 2s cubic-bezier(0.4, 0, 0.6, 1) infinite;
            font-weight: 600;
            padding: 0.4rem 0.8rem !important;
            border-radius: 50px !important;
        }

        @keyframes pulse-badge {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.8; }
        }

        #status-badge.bg-success {
            background: linear-gradient(135deg, var(--success) 0%, #059669 100%) !important;
        }

        #status-badge.bg-danger {
            background: linear-gradient(135deg, var(--error) 0%, #dc2626 100%) !important;
        }

        /* ✅ DESIGN: Cards Premium */
        .card {
            border: 1px solid var(--border-color);
            border-radius: 12px;
            background: #ffffff;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            animation: fadeInUp 0.4s ease-out;
        }

        @keyframes fadeInUp {
            from {
                opacity: 0;
                transform: translateY(16px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        .card:hover {
            border-color: var(--accent);
            box-shadow: 0 12px 24px rgba(99, 102, 241, 0.15);
            transform: translateY(-4px);
        }

        .card-header {
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary-light) 100%);
            color: white;
            border: none;
            border-radius: 12px 12px 0 0;
            font-weight: 600;
            padding: 1.25rem;
        }

        /* ✅ DESIGN: KPI Cards */
        .kpi-card {
            border-left: 4px solid;
            position: relative;
            overflow: hidden;
        }

        .kpi-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: linear-gradient(135deg, rgba(255, 255, 255, 0.5) 0%, transparent 100%);
            pointer-events: none;
        }

        .kpi-daily {
            border-left-color: var(--accent);
        }

        .kpi-weekly {
            border-left-color: var(--warning);
        }

        .kpi-historic {
            border-left-color: var(--success);
        }

        .kpi-card h5 {
            font-size: 0.875rem;
            font-weight: 600;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 0.75rem;
        }

        .kpi-card h3 {
            font-size: 2rem;
            font-weight: 700;
            margin: 0;
        }

        .kpi-card.updating {
            animation: pulse-update 0.6s ease-out;
        }

        @keyframes pulse-update {
            0% {
                background-color: #e8f5e9;
            }
            100% {
                background-color: transparent;
            }
        }

        /* ✅ DESIGN: Log Box */
        .log-box {
            font-family: 'Fira Code', monospace;
            font-size: 0.8rem;
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
            color: #d4d4d4;
            border-radius: 8px;
            padding: 1rem;
            max-height: 400px;
            overflow-y: auto;
            line-height: 1.5;
        }

        .log-box::-webkit-scrollbar {
            width: 6px;
        }

        .log-box::-webkit-scrollbar-track {
            background: rgba(255, 255, 255, 0.05);
            border-radius: 3px;
        }

        .log-box::-webkit-scrollbar-thumb {
            background: rgba(255, 255, 255, 0.2);
            border-radius: 3px;
        }

        .log-box::-webkit-scrollbar-thumb:hover {
            background: rgba(255, 255, 255, 0.3);
        }

        .log-entry {
            animation: slideInLog 0.3s ease-out;
            padding: 0.25rem 0;
        }

        @keyframes slideInLog {
            from {
                opacity: 0;
                transform: translateX(-12px);
            }
            to {
                opacity: 1;
                transform: translateX(0);
            }
        }

        .log-level-INFO {
            color: #4ec9b0;
        }

        .log-level-WARNING {
            color: #dcdcaa;
        }

        .log-level-ERROR {
            color: #f48771;
        }

        .log-level-DEBUG {
            color: #569cd6;
        }

        /* ✅ DESIGN: Tabs */
        .nav-tabs {
            border-bottom: 2px solid var(--border-color);
            gap: 0.5rem;
        }

        .nav-tabs .nav-link {
            color: var(--text-muted);
            border: none;
            border-bottom: 3px solid transparent;
            font-weight: 500;
            transition: all 0.3s ease;
            position: relative;
        }

        .nav-tabs .nav-link:hover {
            color: var(--accent);
            border-bottom-color: var(--accent);
        }

        .nav-tabs .nav-link.active {
            color: var(--accent);
            background: none;
            border-bottom-color: var(--accent);
        }

        .tab-content {
            animation: fadeInTab 0.3s ease-out;
        }

        @keyframes fadeInTab {
            from {
                opacity: 0;
            }
            to {
                opacity: 1;
            }
        }

        /* ✅ DESIGN: Botões */
        .btn {
            font-weight: 600;
            border-radius: 8px;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            border: none;
        }

        .btn-primary {
            background: linear-gradient(135deg, var(--accent) 0%, var(--accent-light) 100%);
            color: white;
            box-shadow: 0 4px 12px rgba(99, 102, 241, 0.3);
        }

        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 20px rgba(99, 102, 241, 0.4);
            color: white;
        }

        .btn-primary:active {
            transform: translateY(0);
        }

        .btn-outline-light {
            border: 2px solid rgba(255, 255, 255, 0.5);
            color: white;
            font-weight: 600;
        }

        .btn-outline-light:hover {
            background: rgba(255, 255, 255, 0.1);
            border-color: white;
            color: white;
        }

        /* ✅ DESIGN: Metric Box */
        .metric-box {
            background: linear-gradient(135deg, var(--accent) 0%, var(--accent-light) 100%);
            padding: 1.5rem;
            border-radius: 12px;
            color: white;
            text-align: center;
            box-shadow: 0 8px 16px rgba(99, 102, 241, 0.2);
            transition: all 0.3s ease;
        }

        .metric-box:hover {
            transform: translateY(-4px);
            box-shadow: 0 12px 24px rgba(99, 102, 241, 0.3);
        }

        .metric-label {
            font-size: 0.875rem;
            opacity: 0.9;
            margin-bottom: 0.5rem;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .metric-value {
            font-size: 2rem;
            font-weight: 700;
        }

        /* ✅ DESIGN: Input e Search */
        .form-control, .form-select {
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 0.75rem 1rem;
            font-weight: 500;
            transition: all 0.3s ease;
        }

        .form-control:focus, .form-select:focus {
            border-color: var(--accent);
            box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.1);
        }

        /* ✅ DESIGN: List Group */
        .list-group-item {
            border: 1px solid var(--border-color);
            border-radius: 8px;
            margin-bottom: 0.5rem;
            transition: all 0.3s ease;
            animation: fadeInUp 0.3s ease-out;
        }

        .list-group-item:hover {
            border-color: var(--accent);
            background: var(--bg-light);
            transform: translateX(4px);
        }

        /* ✅ DESIGN: Toast */
        .toast {
            animation: slideInToast 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
            border-radius: 12px;
            border: none;
            box-shadow: 0 12px 24px rgba(0, 0, 0, 0.15);
        }

        @keyframes slideInToast {
            from {
                opacity: 0;
                transform: translateX(400px);
            }
            to {
                opacity: 1;
                transform: translateX(0);
            }
        }

        .toast.hide {
            animation: slideOutToast 0.3s cubic-bezier(0.34, 1.56, 0.64, 1) forwards;
        }

        @keyframes slideOutToast {
            from {
                opacity: 1;
                transform: translateX(0);
            }
            to {
                opacity: 0;
                transform: translateX(400px);
            }
        }

        /* ✅ DESIGN: Alerts */
        .alert {
            border: none;
            border-radius: 12px;
            border-left: 4px solid;
            animation: fadeInUp 0.4s ease-out;
        }

        .alert-warning {
            background: linear-gradient(135deg, #fef3c7 0%, #fef08a 100%);
            border-left-color: var(--warning);
            color: #92400e;
        }

        .alert-info {
            background: linear-gradient(135deg, #dbeafe 0%, #bfdbfe 100%);
            border-left-color: var(--accent);
            color: #0c4a6e;
        }

        .alert-danger {
            background: linear-gradient(135deg, #fee2e2 0%, #fecaca 100%);
            border-left-color: var(--error);
            color: #7f1d1d;
        }

        /* ✅ DESIGN: Table */
        .table {
            border-collapse: collapse;
        }

        .table thead th {
            background: var(--bg-light);
            border: none;
            font-weight: 600;
            color: var(--primary);
            padding: 1rem;
            text-transform: uppercase;
            font-size: 0.75rem;
            letter-spacing: 0.05em;
        }

        .table tbody tr {
            border-bottom: 1px solid var(--border-color);
            transition: all 0.2s ease;
        }

        .table tbody tr:hover {
            background: var(--bg-light);
        }

        .table td {
            padding: 1rem;
            vertical-align: middle;
        }

        /* ✅ DESIGN: Badge */
        .badge {
            font-weight: 600;
            padding: 0.4rem 0.8rem;
            border-radius: 50px;
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .badge.bg-success {
            background: linear-gradient(135deg, var(--success) 0%, #059669 100%) !important;
        }

        .badge.bg-info {
            background: linear-gradient(135deg, var(--accent) 0%, var(--accent-light) 100%) !important;
        }

        /* ✅ DESIGN: Accordion */
        .accordion-button {
            font-weight: 600;
            transition: all 0.3s ease;
        }

        .accordion-button:not(.collapsed) {
            background: linear-gradient(135deg, var(--bg-light) 0%, #f1f5f9 100%);
            color: var(--accent);
            box-shadow: none;
        }

        .accordion-button:focus {
            border-color: var(--accent);
            box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.1);
        }

        /* ✅ DESIGN: Hidden */
        .hidden {
            display: none;
        }

        /* ✅ DESIGN: Responsivo */
        @media (max-width: 768px) {
            .kpi-card h3 {
                font-size: 1.5rem;
            }

            .metric-value {
                font-size: 1.5rem;
            }

            .log-box {
                max-height: 300px;
            }
        }
    
        /* ✅ DESIGN: Footer */
        footer {
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary-light) 100%);
            border-top: 1px solid rgba(255, 255, 255, 0.1);
            margin-top: 3rem;
            animation: slideUp 0.5s ease-out;
        }

        @keyframes slideUp {
            from {
                opacity: 0;
                transform: translateY(20px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        footer p {
            margin-bottom: 0.25rem;
        }

        footer small {
            font-size: 0.8rem;
        }

        @media (max-width: 768px) {
            footer .col-md-6:last-child {
                text-align: left !important;
                margin-top: 1rem;
            }
        }

    </style>
</head>
<body>
    <!-- ✅ DESIGN: Navbar Premium -->
    <nav class="navbar navbar-expand-lg">
        <div class="container-fluid px-4">
            <a class="navbar-brand text-white d-flex align-items-center" href="#" style="gap: 0.75rem;">
                <img src="data:image/png;base64,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" alt="SW Móveis" style="height: 40px; width: auto; filter: brightness(1.1);">
                <span style="font-size: 1.25rem; font-weight: 700;">SW Móveis</span>
            </a>
            <div class="d-flex align-items-center gap-3">
                <span id="status-badge" class="badge bg-secondary">Carregando...</span>
                <a id="auth-link" href="{{ auth_url }}" class="btn btn-sm btn-outline-light">Autenticar</a>
            </div>
        </div>
    </nav>

    <!-- ✅ DESIGN: Container Principal -->
    <div class="container-fluid px-4 py-5">
        <div class="row mb-5">
            <div class="col-12">
                <h2 class="mb-1" style="font-weight: 700;">📊 Pedidos de Venda</h2>
                <p class="text-muted mb-4">Acompanhe os pedidos abertos e fechados em tempo real</p>
            </div>
        </div>

        <!-- ✅ DESIGN: KPI Cards -->
        <div class="row mb-5">
            <div class="col-md-4 mb-4">
                <div class="card p-4 kpi-card kpi-daily text-center">
                    <h5>Pedidos Diários</h5>
                    <h3 id="kpi-daily" class="text-primary">0</h3>
                    <small class="text-muted">Últimas 24h</small>
                </div>
            </div>
            <div class="col-md-4 mb-4">
                <div class="card p-4 kpi-card kpi-weekly text-center">
                    <h5>Pedidos Semanais</h5>
                    <h3 id="kpi-weekly" style="color: var(--warning);">0</h3>
                    <small class="text-muted">Últimos 7 dias</small>
                </div>
            </div>
            <div class="col-md-4 mb-4">
                <div class="card p-4 kpi-card kpi-historic text-center">
                    <h5>Pedidos Históricos</h5>
                    <h3 id="kpi-historic" style="color: var(--success);">0</h3>
                    <small class="text-muted">Últimos 30 dias</small>
                </div>
            </div>
        </div>

        <!-- ✅ DESIGN: Último Recalcul -->
        <div class="row mb-5">
            <div class="col-12">
                <small class="text-muted">
                    ⏱️ Último Recálculo: <span id="last-recalculated" style="font-weight: 600;">N/D</span>
                </small>
            </div>
        </div>

        <!-- ✅ DESIGN: Logs em Tempo Real -->
        <div class="row mb-5">
            <div class="col-12">
                <div class="card">
                    <div class="card-header">
                        <h5 class="mb-0">📋 Logs em Tempo Real</h5>
                    </div>
                    <div class="card-body p-0">
                        <div id="logs-content" class="log-box"></div>
                    </div>
                </div>
            </div>
        </div>

        <!-- ✅ DESIGN: Tabs com Navegação -->
        <div class="row">
            <div class="col-12">
                <ul class="nav nav-tabs mb-4" id="myTab" role="tablist">
                    <li class="nav-item" role="presentation">
                        <button class="nav-link active" id="search-tab" data-bs-toggle="tab" data-bs-target="#search" type="button">🔍 Busca</button>
                    </li>
                    <li class="nav-item" role="presentation">
                        <button class="nav-link" id="kits-tab" data-bs-toggle="tab" data-bs-target="#kits" type="button">📦 Produtos</button>
                    </li>
                    <li class="nav-item" role="presentation">
                        <button class="nav-link" id="kpi-chart-tab" data-bs-toggle="tab" data-bs-target="#kpi-chart" type="button">📈 Dashboard</button>
                    </li>
                    <li class="nav-item" role="presentation">
                        <button class="nav-link" id="component-tab" data-bs-toggle="tab" data-bs-target="#component-usage" type="button">🔧 Componentes</button>
                    </li>
                </ul>

                <!-- ✅ DESIGN: Auth Required Message -->
                <div id="auth-required-tabs" class="alert alert-warning hidden mb-4">
                    🔐 É necessário autenticar com o SW Móveis para visualizar o conteúdo.
                </div>

                <!-- ✅ DESIGN: Tab Content -->
                <div id="content-tabs" class="tab-content hidden">
                    <!-- Tab: Busca -->
                    <div class="tab-pane fade show active" id="search" role="tabpanel">
                        <div class="row mb-4">
                            <div class="col-12">
                                <div class="input-group">
                                    <input type="text" class="form-control" id="search-input" placeholder="Digite SKU ou nome do produto..." style="padding: 0.75rem 1rem; font-weight: 500;">
                                    <button class="btn btn-primary" id="btn-search" type="button">Buscar</button>
                                </div>
                            </div>
                        </div>
                        <div id="search-results"></div>
                    </div>

                    <!-- Tab: Produtos -->
                    <div class="tab-pane fade" id="kits" role="tabpanel">
                        <div class="mb-4">
                            <button class="btn btn-primary btn-sm" onclick="forceAndReloadKits(event)">🔄 Recarregar Lista</button>
                            <small class="text-muted d-block mt-2">⚠️ Carregamento pode levar 2-5 minutos. Aguarde a notificação do WebSocket.</small>
                        </div>
                        <div id="kits-list"></div>
                    </div>

                    <!-- Tab: Dashboard KPI -->
                    <div class="tab-pane fade" id="kpi-chart" role="tabpanel">
                        <div class="row">
                            <div class="col-lg-8 mb-4">
                                <div class="card">
                                    <div class="card-header">
                                        <h5 class="mb-0">📈 Evolução de Pedidos (Últimos 30 dias)</h5>
                                    </div>
                                    <div class="card-body" style="height: 400px;">
                                        <canvas id="salesChart"></canvas>
                                    </div>
                                </div>
                            </div>
                            <div class="col-lg-4">
                                <div class="card">
                                    <div class="card-header">
                                        <h5 class="mb-0">🎯 Métricas Rápidas</h5>
                                    </div>
                                    <div class="card-body">
                                        <div class="metric-box mb-3">
                                            <div class="metric-label">Média Diária</div>
                                            <div class="metric-value" id="avg-daily">0</div>
                                        </div>
                                        <div class="metric-box mb-3">
                                            <div class="metric-label">Crescimento Semanal</div>
                                            <div class="metric-value" id="growth-weekly">+0%</div>
                                        </div>
                                        <div class="metric-box">
                                            <div class="metric-label">Tendência</div>
                                            <div class="metric-value" id="trend-indicator">📊 Estável</div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- Tab: Componentes -->
                    <div class="tab-pane fade" id="component-usage" role="tabpanel">
                        <div class="card">
                            <div class="card-header">
                                <h5 class="mb-0">🔧 Consumo de Componentes (Últimos 30 dias)</h5>
                                <small class="text-white-50">Atualizado conforme pedidos são processados</small>
                            </div>
                            <div class="card-body">
                                <div id="component-usage-content">
                                    <p class="text-center text-muted">⏳ Carregando dados...</p>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- ✅ DESIGN: Toast Container -->
    <div class="toast-container position-fixed bottom-0 end-0 p-4"></div>

    <!-- Scripts -->
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    
    <script>
        const API = '/api';
        let isAuthenticated = false;
        let salesChart = null;

        /* ✅ DESIGN: Fetch API com Tratamento */
        async function fetchAPI(url, options = {}) {
            try {
                const response = await fetch(url, options);

                if (response.status === 401) {
                    console.error("Sessão expirada (401). Redirecionando para autenticação.");
                    window.location.href = document.getElementById('auth-link').href;
                    throw new Error("Sessão expirada. Redirecionamento em curso.");
                }

                if (!response.ok) {
                    const errorText = await response.text();
                    throw new Error(`Erro na API (${response.status}): ${errorText}`);
                }

                try {
                    return await response.json();
                } catch (e) {
                    return {};
                }

            } catch (error) {
                console.error("Erro em fetchAPI:", error);
                throw error;
            }
        }

        /* ✅ DESIGN: Toast com Animação */
        function showToast(title, message, type = 'info') {
            const toastContainer = document.querySelector('.toast-container');
            const bgClass = type === 'info' ? 'bg-primary' : type === 'warning' ? 'bg-warning' : type === 'danger' ? 'bg-danger' : 'bg-success';
            const textClass = type === 'warning' ? 'text-dark' : 'text-white';

            const toastHtml = `
                <div class="toast align-items-center ${bgClass} ${textClass} border-0" role="alert" aria-live="assertive" aria-atomic="true" data-bs-delay="5000">
                    <div class="d-flex">
                        <div class="toast-body fw-600">
                            <strong>${title}:</strong> ${message}
                        </div>
                        <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button>
                    </div>
                </div>
            `;

            const tempDiv = document.createElement('div');
            tempDiv.innerHTML = toastHtml;
            const toastElement = tempDiv.firstChild;

            toastContainer.appendChild(toastElement);

            const toast = new bootstrap.Toast(toastElement);
            toast.show();

            toastElement.addEventListener('hidden.bs.toast', () => {
                toastElement.remove();
            });
        }

        /* ✅ DESIGN: Formatação de Logs */
        function formatLog(log) {
            const levelClass = `log-level-${log.level}`;
            return `<div class="log-entry ${levelClass}">[${log.timestamp}] [${log.level}] ${log.message}</div>`;
        }

        /* ✅ DESIGN: Formatação de Data/Hora */
        function formatDateTime(isoString) {
            if (!isoString || isoString === 'N/D') return 'N/D';
            try {
                const date = new Date(isoString);
                const now = new Date();
                const isToday = date.toDateString() === now.toDateString();

                if (isToday) {
                    return date.toLocaleTimeString('pt-BR');
                } else {
                    return date.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' }) + ' ' + date.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
                }
            } catch (e) {
                return 'N/D';
            }
        }

        /* ✅ DESIGN: WebSocket Logs */
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

        /* ✅ DESIGN: Atualizar Status de Autenticação */
        function updateAuthStatus(authenticated, authUrl) {
            const badge = document.getElementById('status-badge');
            isAuthenticated = authenticated;

            if(isAuthenticated) {
                badge.className = 'badge bg-success';
                badge.textContent = '🟢 Online';
                document.getElementById('auth-link').classList.add('d-none');
                document.getElementById('content-tabs').classList.remove('hidden');
                document.getElementById('auth-required-tabs').classList.add('hidden');
            } else {
                badge.className = 'badge bg-danger';
                badge.textContent = '🔴 Offline';
                document.getElementById('auth-link').classList.remove('d-none');
                document.getElementById('content-tabs').classList.add('hidden');
                document.getElementById('auth-required-tabs').classList.remove('hidden');
            }
            document.getElementById('auth-link').href = authUrl;
        }

        /* ✅ DESIGN: Atualizar KPIs com Animação */
        function updateKpis(dSalesStats) {
            const kpiDaily = document.getElementById('kpi-daily');
            const kpiWeekly = document.getElementById('kpi-weekly');
            const kpiHistoric = document.getElementById('kpi-historic');

            kpiDaily.textContent = dSalesStats.daily;
            kpiWeekly.textContent = dSalesStats.weekly;
            kpiHistoric.textContent = dSalesStats.historic;
            document.getElementById('last-recalculated').textContent = formatDateTime(dSalesStats.last_update);

            // Animação de atualização
            const cards = document.querySelectorAll('.kpi-card');
            cards.forEach(card => {
                card.classList.add('updating');
                setTimeout(() => {
                    card.classList.remove('updating');
                }, 600);
            });
        }

        /* ✅ DESIGN: Atualizar Componentes */
        function updateComponentUsage(usageData) {
            const div = document.getElementById('component-usage-content');

            if (!usageData.components || usageData.components.length === 0) {
                div.innerHTML = '<div class="alert alert-info">Nenhum componente utilizado nos últimos 30 dias.</div>';
                return;
            }

            let html = '<h5>📊 Consumo Total (30 dias)</h5>';
            html += '<div class="table-responsive"><table class="table table-sm"><thead><tr><th>Componente</th><th>SKU</th><th>Qtd. Total</th><th>Produtos</th></tr></thead><tbody>';

            let total = 0;
            usageData.components.forEach(comp => {
                total += comp.quantidade;
                html += `<tr><td><strong>${comp.nome}</strong></td><td><code>${comp.sku}</code></td><td><span class="badge bg-success">${comp.quantidade}x</span></td><td><small>${comp.produtos.join(', ')}</small></td></tr>`;
            });

            html += '</tbody></table></div>';
            html += `<div class="mt-3 p-3 bg-light rounded"><h6>Total de Insumos: <span class="badge bg-primary fs-5">${total}</span></h6></div>`;

            if (usageData.daily_breakdown && usageData.daily_breakdown.length > 0) {
                html += '<hr><h5 class="mt-4">📅 Consumo Diário (Últimos 7 dias)</h5>';
                html += '<div class="accordion" id="dailyAccordion">';

                usageData.daily_breakdown.forEach((day, idx) => {
                    const date = new Date(day.data);
                    const dateStr = date.toLocaleDateString('pt-BR');
                    const totalDay = day.componentes.reduce((sum, c) => sum + c.quantidade, 0);

                    html += `
                        <div class="accordion-item">
                            <h2 class="accordion-header">
                                <button class="accordion-button ${idx > 0 ? 'collapsed' : ''}" type="button" data-bs-toggle="collapse" data-bs-target="#day${idx}">
                                    ${dateStr} - <span class="badge bg-info ms-2">${totalDay} itens</span>
                                </button>
                            </h2>
                            <div id="day${idx}" class="accordion-collapse collapse ${idx === 0 ? 'show' : ''}" data-bs-parent="#dailyAccordion">
                                <div class="accordion-body">
                                    <ul class="list-group">
                                        ${day.componentes.map(c => `<li class="list-group-item d-flex justify-content-between"><span>${c.sku}</span><span class="badge bg-secondary">${c.quantidade}x</span></li>`).join('')}
                                    </ul>
                                </div>
                            </div>
                        </div>
                    `;
                });

                html += '</div>';
            }

            div.innerHTML = html;
        }

        /* ✅ DESIGN: WebSocket KPI */
        const protoKpi = window.location.protocol === 'https:' ? 'wss' : 'ws';
        let wsKpi = new WebSocket(`${protoKpi}://${window.location.host}/ws/kpi-updates`);

        function setupKpiWebSocket() {
            wsKpi.onmessage = (e) => {
                const data = JSON.parse(e.data);

                if (data.type === 'full_update') {
                    updateAuthStatus(data.authenticated, data.auth_url);

                    if (data.sales_stats) {
                        updateKpis(data.sales_stats);
                    }

                    if (data.component_usage) {
                        updateComponentUsage(data.component_usage);
                    }

                    const forceLoadButton = document.querySelector('#kits button.btn-primary');
                    if (forceLoadButton && forceLoadButton.disabled && data.cache_updated) {
                        forceLoadButton.disabled = false;
                        forceLoadButton.textContent = '🔄 Recarregar Lista';
                        loadKits();
                        showToast('Sucesso', 'Cache de produtos/kits atualizado.', 'success');
                    }
                }
            };

            wsKpi.onerror = (e) => {
                console.error("Erro WebSocket KPI:", e);
                showToast('Erro', 'Conexão WebSocket perdida. Tentando reconectar...', 'danger');
            };

            wsKpi.onclose = () => {
                console.log("WebSocket KPI desconectado. Reconectando...");
                setTimeout(() => {
                    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
                    wsKpi = new WebSocket(`${proto}://${window.location.host}/ws/kpi-updates`);
                    setupKpiWebSocket();
                }, 3000);
            };
        }

        setupKpiWebSocket();

        /* ✅ DESIGN: Busca de Produtos */
        const btnSearch = document.getElementById('btn-search');
        btnSearch.onclick = async () => {
            if (!isAuthenticated) {
                document.getElementById('search-results').innerHTML = '<div class="alert alert-warning">É necessário autenticar com o SW Móveis para realizar buscas.</div>';
                return;
            }

            const q = document.getElementById('search-input').value;
            const div = document.getElementById('search-results');
            div.innerHTML = '<div class="text-center"><div class="spinner-border spinner-border-sm text-primary" role="status"><span class="visually-hidden">Buscando...</span></div></div>';

            try {
                const data = await fetchAPI(`${API}/products/search?q=${q}`);

                if(!data.length) {
                    div.innerHTML = '<div class="alert alert-warning">Nenhum resultado encontrado.</div>';
                    return;
                }

                let html = '<div class="list-group">';

                data.forEach(p => {
                    const imgHtml = p.imagemURL
                        ? `<img src="${p.imagemURL}" style="width:60px;height:60px;object-fit:contain;margin-right:10px;border-radius:6px;background:#f1f1f1" onerror="this.style.display='none'">`
                        : '<span class="text-muted">-</span>';

                    html += `
                        <div class="list-group-item">
                            <div class="d-flex">
                                ${imgHtml}

                                <div class="flex-grow-1">
                                    <div class="d-flex w-100 justify-content-between">
                                        <h5 class="mb-1">${p.nome || p.produto || 'Sem nome'}</h5>
                                        <small>${p.sku || 'N/D'}</small>
                                    </div>

                                    <p class="mb-1">${p.descricaoCurta || ''}</p>

                                    <small class="text-muted d-block">
                                        <b>Estoque:</b> ${p.estoque}
                                        <b style="margin-left:10px;">Tipo:</b> ${p.tipo}
                                    </small>

                                    ${p.componentes && p.componentes.length > 0 ? `
                                        <div class="componentes mt-2 p-2 bg-light rounded">
                                            <small>Componentes:</small>
                                            <ul>
                                                ${p.componentes.map(c =>
                                                    `<li>${c.nome || 'Sem nome'} (${c.quantidade}x)</li>`
                                                ).join("")}
                                            </ul>
                                        </div>
                                    ` : ""}

                                    ${p.tipo === 'Produto' && p.usado_em && p.usado_em.length > 0 ? `
                                        <div class="mt-2 p-2 bg-warning bg-opacity-10 rounded">
                                            <b>📦 Este componente é usado em:</b><br>
                                            ${p.usado_em.map(u =>
                                                `• ${u.quantidade}x no kit <b>${u.kit_nome}</b> (${u.kit_sku})`
                                            ).join("<br>")}
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
                div.innerHTML = `<div class="alert alert-danger">Erro: ${e.message}</div>`;
            }
        };

        /* ✅ DESIGN: Carregar Kits */
        async function loadKits() {
            const div = document.getElementById('kits-list');
            const authRequiredDiv = document.getElementById('auth-required-tabs');

            if (!isAuthenticated) {
                div.innerHTML = '';
                authRequiredDiv.classList.remove('hidden');
                return;
            }

            authRequiredDiv.classList.add('hidden');
            div.innerHTML = '<div class="alert alert-info">⏳ Carregando dados. O worker em segundo plano atualiza o cache a cada 10 minutos. Se a lista estiver vazia, aguarde até 10 minutos e recarregue a página.</div>';

            try {
                const data = await fetchAPI(`${API}/kits`);

                if (!data || data.length === 0) {
                    div.innerHTML = '<div class="alert alert-warning">⚠️ Nenhum Produto/Kit encontrado no cache. O worker pode estar carregando dados. Aguarde 10 minutos e recarregue a página.</div>';
                    return;
                }

                let html = `
                <div class="table-responsive">
                <table class="table table-sm">
                <thead>
                <tr>
                    <th>IMG</th>
                    <th>SKU</th>
                    <th>Nome</th>
                    <th>Componentes / Tipo</th>
                </tr>
                </thead>
                <tbody>
                `;

                data.forEach(k => {
                    const imgHtml = k.imagemURL
                        ? `<img src="${k.imagemURL}" style="width:50px;height:50px;object-fit:contain;border-radius:4px;" onerror="this.style.display='none'">`
                        : '<span class="text-muted">-</span>';

                    let comps = '';
                    if (k.tipo === 'K' && k.componentes && k.componentes.length > 0) {
                        comps = `<b>KIT (${k.componentes.length} itens):</b><br>` + k.componentes
                            .map(c => `<small>• ${c.quantidade}x ${c.nome || 'Sem nome'} (SKU: ${c.sku || 'N/D'})</small>`)
                            .join('<br>');
                    } else if (k.tipo === 'P') {
                        comps = `<span class="badge bg-info">Produto Simples</span><br><small>Estoque: ${k.estoqueAtual || 0}</small>`;
                    } else {
                        comps = '<span class="badge bg-secondary">Tipo Desconhecido</span>';
                    }

                    html += `
                        <tr>
                            <td style="width:60px">${imgHtml}</td>
                            <td style="width:120px; font-weight:bold;">${k.sku || ''}</td>
                            <td>${k.nome || 'N/D'}</td>
                            <td>${comps}</td>
                        </tr>
                    `;
                });

                html += '</tbody></table></div>';
                div.innerHTML = html;

            } catch(e) {
                div.innerHTML = 'Erro ao carregar lista. Verifique os logs.';
            }
        }

        /* ✅ DESIGN: Forçar Recarregamento */
        async function forceAndReloadKits(event) {
            if (!isAuthenticated) {
                showToast('Aviso', 'Faça login primeiro!', 'warning');
                return;
            }

            const btn = event.target;
            btn.disabled = true;
            btn.innerHTML = '⏳ Carregando cache... (pode levar 2-5 minutos)';

            try {
                const data = await fetchAPI('/api/force-load', { method: 'POST' });
                showToast('Info', 'Cache sendo atualizado. Aguarde a notificação do WebSocket.', 'info');
            } catch(e) {
                showToast('Erro', 'Erro: ' + e.message, 'danger');
                btn.disabled = false;
                btn.innerHTML = '🔄 Recarregar Lista';
            }
        }

        /* ✅ DESIGN: Gráfico KPI */
        async function loadKPIChart() {
            try {
                const data = await fetchAPI('/api/sales/history');

                const ctx = document.getElementById('salesChart').getContext('2d');

                if (salesChart) salesChart.destroy();

                salesChart = new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: data.labels,
                        datasets: [{
                            label: 'Pedidos Diários',
                            data: data.daily,
                            borderColor: '#6366f1',
                            backgroundColor: 'rgba(99, 102, 241, 0.1)',
                            tension: 0.4,
                            fill: true,
                            borderWidth: 2
                        }, {
                            label: 'Média Móvel (7 dias)',
                            data: data.moving_avg,
                            borderColor: '#f59e0b',
                            borderDash: [5, 5],
                            tension: 0.4,
                            borderWidth: 2
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: { position: 'top' },
                            tooltip: {
                                mode: 'index',
                                intersect: false
                            }
                        },
                        scales: {
                            y: { beginAtZero: true }
                        }
                    }
                });

                document.getElementById('avg-daily').textContent = data.avg_daily.toFixed(1);
                document.getElementById('growth-weekly').textContent =
                    (data.growth > 0 ? '+' : '') + data.growth.toFixed(1) + '%';
                document.getElementById('trend-indicator').textContent =
                    data.growth > 10 ? '📈 Crescendo' : data.growth < -10 ? '📉 Caindo' : '📊 Estável';
            } catch(e) {
                console.error('Erro ao carregar gráfico KPI:', e);
            }
        }

        /* ✅ DESIGN: Inicialização */
        document.addEventListener('DOMContentLoaded', () => {
            loadKits();

            const kpiTab = document.querySelector('[data-bs-target="#kpi-chart"]');
            if (kpiTab) {
                kpiTab.addEventListener('shown.bs.tab', loadKPIChart);
            }

            const componentUsageTab = document.querySelector('[data-bs-target="#component-usage"]');
            if (componentUsageTab) {
                componentUsageTab.addEventListener('shown.bs.tab', () => {
                    const contentDiv = document.getElementById('component-usage-content');

                    if (contentDiv.innerHTML.includes('Carregando dados...')) {
                        setTimeout(() => {
                            if (contentDiv.innerHTML.includes('Carregando dados...')) {
                                contentDiv.innerHTML = '<div class="alert alert-danger">⚠️ Tempo limite excedido. O cálculo de componentes pode estar demorando. Verifique os logs.</div>';
                            }
                        }, 30000);
                    }
                });
            }
        });
    </script>

    <!-- ✅ DESIGN: Footer Premium -->
    <footer class="bg-primary text-white mt-5 py-4">
        <div class="container-fluid px-4">
            <div class="row align-items-center">
                <div class="col-md-6">
                    <p class="mb-0">
                        <strong>SW Móveis MDF</strong> - Gestão Inteligente de Pedidos
                    </p>
                    <small class="text-white-50">© 2025 - Desenvolvido com ❤️</small>
                </div>
                <div class="col-md-6 text-md-end">
                    <p class="mb-0">
                        <strong>Desenvolvedor:</strong> João Victor Dias Santana
                    </p>
                    <small class="text-white-50">Versão 1.0 - 2025</small>
                </div>
            </div>
        </div>
    </footer>

</body>
</html>
"""

# ============================================================================ 
# 10. EXECUÇÃO
# ============================================================================ 
# 10. EXECUÇÃO
# ============================================================================

def create_app() -> Flask:
    """Função de fábrica para criar e configurar a aplicação Flask."""
    
    # 1. Inicializa as dependências na ordem correta
    config = Config()
    
    # A variável 'logger' é global (definida na linha 160)
    
    auth_manager = AuthManager(config)
    api_client = BlingAPIClient(config, auth_manager)
    sales_manager = SalesManager(config, logger)
    
    # 2. Inicializa o Orchestrator (Worker)
    orchestrator = Orchestrator(
        config=config,
        auth_manager=auth_manager,
        api_client=api_client,
        sales_manager=sales_manager,
    )
    
    # 3. Inicializa o Flask
    flask_app = Flask(__name__)
    
    # 4. Inicializa o WebServer (Rotas e WebSockets)
    WebServer(config, orchestrator, flask_app) 
    
    # 5. LÓGICA DE INÍCIO DO WORKER (REMOVIDA DO STARTUP)
    # O worker não deve iniciar automaticamente no startup.
    # Ele deve ser iniciado apenas após a autenticação ou sob demanda.
    # A chamada para orchestrator.start() e start_cleanup_timer() foi removida daqui.
    
    return flask_app

# Ponto de entrada para Gunicorn/WSGI
app = create_app()

if __name__ == '__main__':
    # Apenas para testes locais
    
    # Lógica de worker para ambiente local (apenas 1 processo)
    # Garante que o worker inicie no ambiente local
    orchestrator = app.orchestrator # Acessa o orchestrator criado em create_app
    if not orchestrator.is_running():
        orchestrator.start_worker()
        start_cleanup_timer()
        logger.info("✅ Worker de fundo iniciado em modo local.")
        
    logger.info("Iniciando servidor Flask em modo local...")
    app.run(host='0.0.0.0', port=5000, debug=False)