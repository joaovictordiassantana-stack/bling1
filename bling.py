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
from threading import Lock, Thread
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Dict, Any, Callable
from dataclasses import dataclass, field
from functools import wraps

import requests
from requests.exceptions import RequestException
from flask import Flask, request, render_template_string, jsonify, redirect, url_for
from flask_sock import Sock
# Importação necessária para tratamento correto do WebSocket
try:
    from simple_websocket import ConnectionClosed
except ImportError:
    class ConnectionClosed(Exception): pass

# ============================================================================ 
# 0. VARIÁVEIS GLOBAIS DE CONTROLE (LOCK)
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
    
    # Rate Limiting (Configurável)
    MAX_PAGES_PER_BATCH: int = 3 # Máximo de páginas antes da pausa
    DELAY_BETWEEN_PAGES: float = 2.5 # Delay entre requisições de página (em segundos)
    DELAY_BETWEEN_BATCHES: float = 8.0 # Pausa longa após o batch (em segundos)
    
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
        global orchestrator # Acessa a instância global
        auth_manager = orchestrator.auth
        if not auth_manager.is_authenticated():
            return jsonify({"error": "Não autenticado ou token expirado"}), 401
        
        token = auth_manager.get_access_token()
        if not token:
            return jsonify({"error": "Token de acesso não encontrado"}), 401
            
        return f(*args, token=token, **kwargs)
    return decorated

# ============================================================================ 
# 4. BLING# 4. API CLIENT
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
    Cliente HTTP para a API Bling v3 com retry, rate limiting e refresh de token.
    """
    
    def __init__(self, config: Config, auth_manager):
        self.config = config
        self.auth = auth_manager
        self.logger = logging.getLogger('bling_automacao')
        self.metrics = MetricsManager() # Inicializa o gerenciador de métricas
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        })
        
    def _request(self, method: str, endpoint: str, **kwargs) -> Optional[Dict[str, Any]]:
        """
        Executa uma requisição HTTP com retry e tratamento de token.
        """
        url = f"{self.config.BLING_API_URL}/{endpoint}"
        token = self.auth.get_access_token()
        
        if not token:
            self.logger.error(f"⛔ Token de acesso ausente para {endpoint}. Abortando requisição.")
            return None
            
        headers = {'Authorization': f'Bearer {token}'}
        
        for attempt in range(self.config.MAX_RETRIES):
            start_time = time.time()
            try:
                response = self.session.request(method, url, headers=headers, timeout=self.config.REQUEST_TIMEOUT, **kwargs)
                latency = time.time() - start_time
                self.metrics.record_request(response.status_code, latency)
                
                if response.status_code == 401:
                    self.logger.warning(f"Token expirado ou inválido ao acessar {endpoint}. Tentando refresh...")
                    if self.auth.refresh_token():
                        token = self.auth.get_access_token()
                        headers['Authorization'] = f'Bearer {token}'
                        self.logger.info("Token renovado com sucesso. Tentando novamente a requisição.")
                        continue # Tenta novamente com o novo token
                    else:
                        self.logger.error("Falha ao renovar o token. Requer autenticação manual.")
                        return None
                
                response.raise_for_status()
                
                # Tenta retornar JSON, se falhar, retorna um objeto vazio ou um indicador de sucesso
                try:
                    return response.json()
                except requests.exceptions.JSONDecodeError:
                    self.logger.debug(f"Resposta não é JSON para {endpoint}. Status: {response.status_code}")
                    return {} # Retorna vazio para 204 No Content, por exemplo
                
            except requests.exceptions.HTTPError as e:
                self.logger.exception(f"Erro HTTP ao acessar {endpoint}.")
                if response.status_code in [429, 500, 502, 503, 504] and attempt < self.config.MAX_RETRIES - 1:
# ✅ Rate limit especial: aguarda mais tempo
                    if response.status_code == 429:
                        # Prioriza Retry-After, senão usa backoff exponencial (5s, 10s, 20s...)
                        retry_after = response.headers.get('Retry-After')
                        wait_time = 5.0 * (2 ** attempt)
                        if retry_after:
                            try:
                                wait_time = max(wait_time, float(retry_after))
                                self.logger.warning(f"⚠️ Status 429. Bling sugeriu Retry-After: {wait_time:.2f}s. Aguardando...")
                            except ValueError:
                                self.logger.warning(f"⚠️ Status 429. Retry-After inválido. Usando backoff: {wait_time:.2f}s.")
                        else:
                            self.logger.warning(f"⚠️ Status 429. Retry-After ausente. Usando backoff: {wait_time:.2f}s.")
                        delay = wait_time
                    else:
                        delay = self.config.BASE_DELAY * (2 ** attempt)

                    if response.status_code != 429:
                        self.logger.warning(f"⚠️ Status {response.status_code}. Aguardando {delay:.2f}s antes de retry {attempt + 2}/{self.config.MAX_RETRIES}")
                    time.sleep(delay)
                else:
                    return None
            except RequestException as e:
                self.logger.exception(f"Erro de conexão ao acessar {endpoint}.")
                return None
                
        self.logger.error(f"Falha na requisição após {self.config.MAX_RETRIES} tentativas para {endpoint}.")
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
        """Retorna a URL local para iniciar o fluxo de autorização."""
        # A URL real do Bling será construída na rota /auth
        from flask import url_for
        return url_for('auth')

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
    """Gerencia e calcula os KPIs de vendas."""
    
    config: Config
    logger: logging.Logger
    orchestrator: Any = field(default=None) # Referência ao Orchestrator
    
    # Contadores
    daily_count: int = 0
    weekly_count: int = 0
    historic_count: int = 0
    
    # Data da última atualização dos dados
    last_recalculated: datetime = field(default_factory=datetime.now)
    
    _initial_load_failed: bool = True 

    def __post_init__(self):
        # Carrega o estado persistido na inicialização
        self.lock = Lock()
        self.recalculation_lock = Lock()
        self._recalculation_running = False  # Flag de estado para controle de concorrência
        self._load_stats()

    def _load_stats(self):
        """Carrega as estatísticas persistidas do disco."""
        with self.lock:
            data = load_stats_safe(self.config.SALES_STATS_FILE)
            if data:
                self.daily_count = data.get('daily', 0)
                self.weekly_count = data.get('weekly', 0)
                self.historic_count = data.get('historic', 0)
                self.last_recalculated = data.get('last_recalculated', datetime.now())
                self.logger.info("Estatísticas de KPIs carregadas do disco.")
            else:
                self.logger.warning("Nenhuma estatística de KPI encontrada no disco.")

    def get_stats(self) -> Dict[str, Any]:
        """Retorna os KPIs atuais."""
        with self.lock:
            return {
                "daily": self.daily_count,
                "weekly": self.weekly_count,
                "historic": self.historic_count,
                # Retorna o timestamp de quando o worker processou por último
                "last_update": self.last_recalculated.isoformat() 
            }

    def _get_state_for_save(self) -> Dict[str, Any]:
        """Retorna o estado atual para persistência."""
        return {
            "daily": self.daily_count,
            "weekly": self.weekly_count,
            "historic": self.historic_count,
            "last_recalculated": self.last_recalculated
        }

    
    def recalculate_from_orders(self, orders: List[Dict[str, Any]]):
        """Calcula KPIs baseando-se na data/hora de emissão dos pedidos."""
        now = datetime.now()
        yesterday = now - timedelta(hours=24) 
        last_week = now - timedelta(days=7)
        last_month = now - timedelta(days=30)
        
        daily = 0
        weekly = 0
        historic = 0
        
        # O cálculo é feito fora do lock.
        for order in orders:
            if not isinstance(order, dict):
                self.logger.warning(f"Item inesperado encontrado na lista de pedidos de venda, ignorando: {order}")
                continue
            
            data_emissao_str = None

            data_obj = order.get('data')
            if isinstance(data_obj, dict):
                data_emissao_str = data_obj.get('dataEmissao')
                hora_emissao = data_obj.get('horaEmissao')
            elif isinstance(data_obj, str):
                data_emissao_str = data_obj
                hora_emissao = None

            if not data_emissao_str:
                # Caminho 3: Tenta estrutura alternativa
                data_emissao_str = order.get('dataEmissao') or order.get('dataHora', '').split('T')[0]
                if not data_emissao_str:
                    self.logger.debug(f"Pedido {order.get('id')} sem dataEmissao. Estrutura: {order.keys()}")
                    continue

            try:
                order_date = datetime.strptime(data_emissao_str, '%Y-%m-%d')
        
                if hora_emissao and isinstance(hora_emissao, str):
                    try:
                        parts = hora_emissao.split(':')
                        if len(parts) == 3:
                            h, m, s = map(int, parts)
                            order_date = order_date.replace(hour=h, minute=m, second=s)
                    except (ValueError, AttributeError):
                        pass
            except Exception as e:
                self.logger.warning(f"Erro ao parsear data '{data_emissao_str}' do pedido {order.get('id')}: {e}")
                continue

            if order_date >= last_month:
                historic += 1 

            if order_date >= last_week:
                weekly += 1

            if order_date >= yesterday:
                daily += 1 

        # ATUALIZAÇÃO E PERSISTÊNCIA DENTRO DO LOCK
        with self.lock:
            # Atualiza todos os contadores de uma vez
            self.daily_count = daily
            self.weekly_count = weekly
            self.historic_count = historic
            self.last_recalculated = now # Atualiza o tempo de processamento
            
            # PERSISTE O ESTADO ATUAL
            save_stats(self._get_state_for_save(), self.config.SALES_STATS_FILE)
            
            # Notifica subscribers sobre a mudança
            if self.orchestrator:
                self.orchestrator.broadcast_kpi_update(sales_stats=self._get_state_for_save())
            else:
                self.logger.warning("Orchestrator não configurado no SalesManager. Não foi possível notificar via WS.")

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
        """Loop principal do worker de fundo."""
        while not self._stop_event.is_set():
            
            self.process_sales_orders()
            self.process_products_cache()
            
            self.logger.info("Worker finalizado. Próxima execução em 10 minutos.")
            self._stop_event.wait(600) # 10 minutos (600 segundos) - Permite parada imediata

    
    def process_sales_orders(self):
        """Busca pedidos de venda faturados/em andamento dos últimos 30 dias e ATUALIZA O SALES_MANAGER POR RECALCULO."""
        
        # Verifica e marca o estado de recalculação dentro do lock
        with self.sales.recalculation_lock:
            if self.sales._recalculation_running:
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
            
            # ✅ Limita a 3 páginas por vez para evitar rate limit
            MAX_PAGES_PER_BATCH = self.config.MAX_PAGES_PER_BATCH
            batch_count = 0
            
            while True:
                params['pagina'] = page
                response = self.api.get('pedidos/vendas', params=params)
                
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
                time.sleep(self.config.DELAY_BETWEEN_PAGES) # Delay configurável entre páginas
                
                batch_count += 1
                if batch_count >= MAX_PAGES_PER_BATCH:
                    self.logger.info(f"⏸️ Pausa de {self.config.DELAY_BETWEEN_BATCHES}s após {batch_count} páginas (rate limit)")
                    time.sleep(self.config.DELAY_BETWEEN_BATCHES) # Pausa configurável após o batch
                    batch_count = 0
                
            self.logger.info(f"Busca de pedidos finalizada. Total de pedidos: {len(all_orders)}")
            
            # Recalcula os KPIs
            self.sales.recalculate_from_orders(all_orders)
            
        except Exception as e:
            self.logger.exception(f"Erro durante recálculo de pedidos: {e}")
        finally:
            # Libera a flag de estado dentro do lock
            with self.sales.recalculation_lock:
                self.sales._recalculation_running = False

    def process_products_cache(self):
        """Busca e armazena em cache todos os produtos e kits."""
        
        # ✅ 1. Bloquear qualquer worker sem token
        if not self.auth.is_authenticated():
            self.logger.error("⛔ Worker abortado: token inexistente ou falha na renovação.")
            return
            
        self.logger.info("Iniciando busca e cache de produtos e kits...")
        
        all_products = []
        all_kits = []
        page = 1
        
        # ✅ Limita a 3 páginas por vez para evitar rate limit
        MAX_PAGES_PER_BATCH = self.config.MAX_PAGES_PER_BATCH
        batch_count = 0
        
        while True:
            params = {
                'pagina': page,
                'tipo': 'produto,kit'
            }
            response = self.api.get('produtos', params=params)
            
            if response is None:
                self.logger.error("Falha ao buscar produtos na API. Tentando usar cache anterior.")
                # Se falhar, o loop é interrompido e o cache anterior será usado (pois não há save_products_cache)
                break
    
            data = safe_get(response, 'data', [])
            
            # Valida se retornou dados
            if not data or len(data) == 0:
                self.logger.info(f"Página {page} vazia. Fim da paginação.")
                break
                
            for item in data:
                # A API retorna {"produto": {...}}
                produto = item.get('produto') if isinstance(item, dict) else item
                if not produto or not isinstance(produto, dict):
                    continue
                
                tipo = produto.get('tipo')
                sku = produto.get('sku') or produto.get('codigo')
                
                # Valida que tem SKU e tipo válido
                if not sku or not tipo:
                    continue
                
                if tipo == 'P':  # Produto simples
                    all_products.append(produto)
                elif tipo == 'K':  # Kit
                    all_kits.append(produto)

            self.logger.info(f"Página {page} processada. Produtos: {len(all_products)}, Kits: {len(all_kits)} | Taxa: {len(data)} itens/página")
            
            # Se retornou menos que 100, é a última página
            if len(data) < 100:
                self.logger.info(f"Última página detectada ({len(data)} itens).")
                break

                
            page += 1
            time.sleep(self.config.DELAY_BETWEEN_PAGES) # Delay configurável entre páginas
            
            batch_count += 1
            if batch_count >= MAX_PAGES_PER_BATCH:
                self.logger.info(f"⏸️ Pausa de {self.config.DELAY_BETWEEN_BATCHES}s após {batch_count} páginas (rate limit)")
                time.sleep(self.config.DELAY_BETWEEN_BATCHES) # Pausa configurável após o batch
                batch_count = 0
            
        self.logger.info(f"Busca de produtos finalizada. Total de produtos: {len(all_products)}, Total de kits: {len(all_kits)}")
        
        # Salva o cache
        with self._cache_lock:
            self._products_cache = {p['sku']: p for p in all_products}
            self._kits_cache = {k['sku']: k for k in all_kits}
            # ✅ 3. Nunca salvar cache se produtos == 0 (proteção já implementada em save_products_cache)
            save_products_cache(self.config.PRODUCTS_CACHE_FILE, all_products, all_kits)
            
        # Notifica o frontend que o cache foi atualizado (e, por tabela, que o worker terminou)
        self.broadcast_kpi_update(cache_updated=True)

    def calculate_component_usage(self) -> Dict[str, Any]:
        """
        Calcula o uso de componentes com base nos pedidos dos últimos 30 dias.
        """
        
        # 1. Obter a lista de pedidos dos últimos 30 dias (já processados pelo SalesManager)
        # Como o SalesManager não armazena os pedidos, vamos re-buscar (ou idealmente, o SalesManager
        # deveria ter um cache de pedidos, mas seguindo a estrutura atual, re-buscamos).
        
        # Simplificação: Usar a lista de pedidos do último recalculo do SalesManager.
        # Como não temos acesso direto, vamos re-buscar (com a ressalva de que é ineficiente).
        
        token = self.auth.get_access_token()
        if not token:
            self.logger.warning("Token indisponível para calcular uso de componentes.")
            return {"components": []}
            
        now = datetime.now()
        params = {
            'dataEmissaoInicial': (now - timedelta(days=30)).strftime('%Y-%m-%d'),
            'situacao': 'atendidos,em_aberto,em_andamento,faturados,em_producao'
        }
        
        all_orders = []
        page = 1
        while True:
            params['pagina'] = page
            response = self.api.get('pedidos/vendas', params=params)
            
            if response is None:
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
            time.sleep(0.1) # Pequeno delay
            
        # 2. Processar os pedidos para calcular o uso de componentes
        component_usage = {}
        
        for order in all_orders:
            itens = safe_get(order, 'itens', [])
            for item in safe_iter(itens):
                produto_sku = safe_get(item, 'codigo')
                quantidade_vendida = safe_get(item, 'quantidade', 0)
                
                if not produto_sku or quantidade_vendida == 0:
                    continue

                # Verificar se é um kit
                kit = self.get_product_by_sku(produto_sku)
                if kit and safe_get(kit, 'tipo') == 'K':
                    componentes = safe_get(kit, 'componentes', [])
                    for comp in safe_iter(componentes):
                        comp_produto = safe_get(comp, 'produto', {})
                        comp_sku = safe_get(comp_produto, 'codigo')
                        comp_nome = safe_get(comp_produto, 'nome')
                        comp_quantidade_por_kit = safe_get(comp, 'quantidade', 0)
                        
                        if not comp_sku or comp_quantidade_por_kit == 0:
                            continue

                        quantidade_total_consumida = quantidade_vendida * comp_quantidade_por_kit
                        
                        if comp_sku not in component_usage:
                            component_usage[comp_sku] = {
                                "sku": comp_sku,
                                "nome": comp_nome,
                                "quantidade": 0,
                                "produtos": set() # Usar set para evitar duplicatas
                            }

                        component_usage[comp_sku]["quantidade"] += quantidade_total_consumida
                        component_usage[comp_sku]["produtos"].add(produto_sku)
    
        # 3. Formatar a saída
        result = []
        for sku, usage in component_usage.items():
            result.append({
                "sku": usage["sku"],
                "nome": usage["nome"],
                "quantidade": usage["quantidade"],
                "produtos": sorted(list(usage["produtos"]))
            })
            
        # Ordenar por quantidade consumida
        result.sort(key=lambda x: x['quantidade'], reverse=True)
        
        return {"components": result}

    def broadcast_kpi_update(self, sales_stats: Optional[Dict[str, Any]] = None, cache_updated: bool = False):
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
            
            # 3. Adiciona o uso de componentes (Calculado sob demanda via API /api/components/usage)
            # NOTA: O cálculo é pesado e foi removido do fluxo de broadcast para evitar latência/rate limit.
            self.logger.debug("Cálculo de uso de componentes omitido do broadcast para otimização.")
                
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
            """Retorna o histórico de pedidos dos últimos 30 dias para o gráfico."""
            
            # A lógica de histórico de vendas está dentro do SalesManager, mas não exposta.
            # Para fins de demonstração, vamos retornar dados mockados ou a lógica de cálculo
            # precisaria ser refatorada para retornar o histórico diário.
            
            # Simplificação: Retorna dados mockados para o gráfico.
            now = datetime.now()
            labels = [(now - timedelta(days=i)).strftime('%d/%m') for i in range(30)][::-1]
            daily = [20, 22, 18, 25, 30, 28, 24, 21, 23, 26, 29, 31, 27, 25, 22, 20, 19, 21, 24, 27, 30, 32, 35, 33, 31, 28, 25, 23, 20, 18][::-1]
            
            # Cálculo de média móvel simples (7 dias)
            moving_avg = []
            for i in range(len(daily)):
                if i < 6:
                    moving_avg.append(None)
                else:
                    avg = sum(daily[i-6:i+1]) / 7
                    moving_avg.append(round(avg, 1))

            # Cálculo de crescimento (últimos 7 dias vs 7 dias anteriores)
            last_week_sum = sum(daily[-7:])
            prev_week_sum = sum(daily[-14:-7])
            
            if prev_week_sum > 0:
                growth = ((last_week_sum - prev_week_sum) / prev_week_sum) * 100
            else:
                growth = 0
                
            avg_daily = sum(daily) / len(daily)
            
            return jsonify({
                "labels": labels,
                "daily": daily,
                "moving_avg": moving_avg,
                "growth": round(growth, 1),
                "avg_daily": round(avg_daily, 1)
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
        def api_product_search(token):
            """Busca produtos e kits no cache pelo SKU ou nome."""
            query = request.args.get('q', '').lower()
            if not query:
                return jsonify([])
                
            results = []
            
            # Busca em produtos simples
            for product in self.orchestrator.get_all_products():
                name = safe_get(product, 'nome', '').lower()
                sku = safe_get(product, 'sku', '').lower()
                if query in name or query in sku:
                    results.append({
                        "sku": product.get('sku'),
                        "nome": product.get('nome'),
                        "estoque": product.get('estoqueAtual'),
                        "tipo": "Produto",
                        "imagemURL": safe_get(product, 'imagem', {}).get('link')
                    })

            # Busca em kits
            for kit in self.orchestrator.get_all_kits():
                name = safe_get(kit, 'nome', '').lower()
                sku = safe_get(kit, 'sku', '').lower()
                if query in name or query in sku:
                    components = []
                    for comp in safe_iter(safe_get(kit, 'componentes')):
                        comp_produto = safe_get(comp, 'produto', {})
                        components.append({
                            "sku": safe_get(comp_produto, 'codigo'),
                            "nome": safe_get(comp_produto, 'nome'),
                            "quantidade": safe_get(comp, 'quantidade')
                        })
                        
                    results.append({
                        "sku": kit.get('sku'),
                        "nome": kit.get('nome'),
                        "estoque": kit.get('estoqueAtual'),
                        "tipo": "Kit",
                        "imagemURL": safe_get(kit, 'imagem', {}).get('link'),
                        "componentes": components
                    })

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
            """Retorna o uso de componentes nos últimos 30 dias."""
            
            usage_data = self.orchestrator.calculate_component_usage()
            return jsonify(usage_data)

        @self.app.route('/webhook', methods=['POST'])
        def webhook():
            """Recebe webhooks do Bling (ex: atualização de estoque)."""
            
            # A implementação completa de webhook requer validação de assinatura (HMAC)
            # e processamento assíncrono. Aqui, apenas um esqueleto.
            
            with WebServer.webhook_lock:
                try:
                    # 1. Validação da Assinatura (Recomendado)
                    # signature = request.headers.get('X-Bling-Signature')
                    # if not self._validate_signature(request.data, signature):
                    #     self.logger.error("Webhook com assinatura inválida.")
                    #     return jsonify({"error": "Assinatura inválida"}), 403
                    
                    data = request.json
                    tipo = safe_get(data, 'tipo')

                    self.logger.info(f"🔔 Webhook recebido: Tipo={tipo}")

                    # Exemplo de processamento: Forçar recálculo de KPIs em caso de novo pedido
                    if tipo == 'pedidoVenda':
                        self.logger.info("Webhook de Pedido de Venda recebido. Forçando recálculo de KPIs.")
                        # Executa o recálculo em uma thread separada
                        executor = ThreadPoolExecutor(max_workers=1)
                        executor.submit(self.orchestrator.process_sales_orders)
                        executor.shutdown(wait=False)
                        
                    return jsonify({"status": "ok", "message": f"Webhook {tipo} recebido e processado."}), 200

                except Exception as e:
                    self.logger.exception("Erro no processamento do webhook.")
                    return jsonify({"error": "Erro interno do servidor"}), 500

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
            try:
                # O broadcast_kpi_update é usado para enviar o estado inicial
                # O sales_stats é passado para garantir que os KPIs e o uso de componentes sejam calculados e enviados
                self.orchestrator.broadcast_kpi_update(sales_stats=self.orchestrator.sales._get_state_for_save())
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

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel Bling - Sw Móveis</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
    <style>body{background:#f8f9fa;font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif}.navbar{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white}.log-box{font-family:'Courier New',monospace;font-size:.85em;background:#1e1e1e;color:#d4d4d4;border-radius:.5rem;padding:1rem;max-height:400px;overflow-y:auto}.log-level-INFO{color:#4ec9b0}.log-level-WARNING{color:#dcdcaa}.log-level-ERROR{color:#f48771}.log-level-DEBUG{color:#569cd6}.hidden{display:none}.kpi-card{border-left:5px solid;transition:background-color .5s ease}.kpi-daily{border-left-color:#0d6efd}.kpi-weekly{border-left-color:#ffc107}.kpi-historic{border-left-color:#198754}footer{box-shadow:0 -1px 3px rgba(0,0,0,0.05)}footer strong{color:#495057}footer p{margin-bottom:0}.metric-box{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);padding:20px;border-radius:10px;color:white;text-align:center}.metric-label{font-size:.9em;opacity:.9;margin-bottom:5px}.metric-value{font-size:2em;font-weight:bold}.toast-container{z-index:1090}</style>
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
        <h2>📊 Pedidos de Venda (Abertos e Fechados)</h2>
        <div class="row mb-4">
             <div class="col-md-4">
                 <div class="card p-3 text-center kpi-card kpi-daily">
 <h5>Pedidos Diários (Últimas 24h)</h5>
 <h3 id="kpi-daily" class="text-primary">0</h3>
                 </div>
             </div>
             <div class="col-md-4">
                 <div class="card p-3 text-center kpi-card kpi-weekly">
 <h5>Pedidos Semanais (Últimos 7 dias)</h5>
 <h3 id="kpi-weekly" class="text-warning">0</h3>
                 </div>
             </div>
             <div class="col-md-4">
                 <div class="card p-3 text-center kpi-card kpi-historic">
 <h5>Pedidos Históricos (Últimos 30 dias)</h5>
 <h3 id="kpi-historic" class="text-success">0</h3>
                 </div>
             </div>
             <small class="text-muted mt-2">
                Último Recalculo de KPIs: <span id="last-recalculated">N/D</span>
            </small>
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
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#kpi-chart">📊 Dashboard KPI</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#component-usage">🔧 Componentes</button></li>
        </ul>

        <div id="auth-required-tabs" class="alert alert-warning hidden">
            É necessário autenticar com o Bling para visualizar o conteúdo.
        </div>

        <div id="content-tabs" class="tab-content p-3 bg-white border border-top-0 rounded-bottom hidden">
            <div class="tab-pane fade show active" id="search">
                <div class="input-group mb-3">
<input type="text" class="form-control" id="search-input" placeholder="SKU ou Nome...">
<button class="btn btn-primary" id="btn-search">Buscar</button>
                </div>
                <div id="search-results"></div>
            </div>

            <div class="tab-pane fade" id="kits">
                <button class="btn btn-sm btn-warning mb-3" onclick="forceAndReloadKits(event)">🔄 Forçar Recarregamento</button>
                <p class="text-muted">Aguarde o carregamento completo. Kits (Produtos com Componentes) podem demorar mais para carregar os detalhes.</p>
                <div id="kits-list"></div>
            </div>

            <div class="tab-pane fade" id="kpi-chart">
                <div class="row">
<div class="col-md-8">
    <div class="card">
<div class="card-header bg-primary text-white">
    <h5>📈 Evolução de Pedidos (Últimos 30 dias)</h5>
</div>
<div class="card-body" style="height: 400px;">
    <canvas id="salesChart"></canvas>
</div>
    </div>
</div>
<div class="col-md-4">
    <div class="card">
<div class="card-header bg-success">
    <h5>🎯 Métricas Rápidas</h5>
</div>
<div class="card-body">
    <div class="metric-box mb-3">
        <div class="metric-label">Média Diária</div>
        <div class="metric-value" id="avg-daily">0</div>
    </div>
    <div class="metric-box mb-3">
        <div class="metric-label">Crescimento Semanal</div>
        <div class="metric-value text-success" id="growth-weekly">+0%</div>
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
            
            <div class="tab-pane fade" id="component-usage">
                <div class="card">
<div class="card-header bg-warning">
    <h5>🔧 Consumo de Componentes por Vendas (Últimos 30 dias)</h5>
    <small>Atualizado conforme pedidos são processados</small>
</div>
<div class="card-body">
    <div id="component-usage-content">
<p class="text-center text-muted">Carregando dados...</p>
    </div>
</div>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    
    <!-- Container para Toast Notifications -->
    <div class="toast-container position-fixed bottom-0 end-0 p-3">
        <!-- Toasts serão injetados aqui -->
    </div>
    <script>
    const API = '/api';
    
    /**
     * Função auxiliar para centralizar chamadas de API, tratamento de erros e verificação de status.
     * @param {string} url - URL da API.
     * @param {object} [options={}] - Opções para a chamada fetch.
     * @returns {Promise<object>} - O corpo da resposta JSON.
     * @throws {Error} - Se a resposta não for OK ou se houver um erro de rede.
     */
    async function fetchAPI(url, options = {}) {
        try {
            const response = await fetch(url, options);

            if (response.status === 401) {
                // Sessão expirada ou não autenticado. Força redirecionamento para reautenticar.
                console.error("Sessão expirada (401). Redirecionando para autenticação.");
                window.location.href = document.getElementById('auth-link').href;
                throw new Error("Sessão expirada. Redirecionamento em curso.");
            }

            if (!response.ok) {
                const errorText = await response.text();
                throw new Error(`Erro na API (${response.status}): ${errorText}`);
            }

            // Tenta retornar JSON, se falhar, retorna um objeto vazio ou um indicador de sucesso
            try {
                return await response.json();
            } catch (e) {
                // Pode ser uma resposta 204 No Content ou um JSON vazio
                return {}; 
            }

        } catch (error) {
            console.error("Erro em fetchAPI:", error);
            throw error; // Re-lança o erro para ser tratado pela função chamadora
        }
    }

    async function forceRecalculate(event) {
        if (!isAuthenticated) {
            showToast('Aviso', 'Faça login primeiro!', 'warning');
            return;
        }
        
        const btn = event.target;
        btn.disabled = true;
        btn.textContent = '⏳ Recalculando... Aguarde o WebSocket';
        
        try {
            // 1. Força o recálculo no servidor
            const data = await fetchAPI('/api/recalculate', { method: 'POST' });
            
            // 2. Não usa alert bloqueante, aguarda o WebSocket
            showToast('Info', data.message, 'info');
            
        } catch(e) {
            showToast('Erro', 'Erro ao forçar recálculo: ' + e.message, 'danger');
            btn.disabled = false;
            btn.textContent = '🔄 Forçar Recálculo';
        }
        // O botão é reabilitado pelo WebSocket (wsKpi.onmessage)
        
    }

    function formatLog(log) {
        const levelClass = `log-level-${log.level}`;
        return `<div class="${levelClass}">[${log.timestamp}] [${log.level}] ${log.message}</div>`;
    }
    
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
    
    function showToast(title, message, type = 'info') {
        const toastContainer = document.querySelector('.toast-container');
        const bgClass = type === 'info' ? 'bg-primary' : type === 'warning' ? 'bg-warning' : type === 'danger' ? 'bg-danger' : 'bg-success';
        const textClass = type === 'warning' ? 'text-dark' : 'text-white';
        
        const toastHtml = `
            <div class="toast align-items-center ${bgClass} ${textClass} border-0" role="alert" aria-live="assertive" aria-atomic="true" data-bs-delay="5000">
                <div class="d-flex">
<div class="toast-body">
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
    
    function updateAuthStatus(authenticated, authUrl) {
        const badge = document.getElementById('status-badge');
        isAuthenticated = authenticated;
        
        if(isAuthenticated) {
            badge.className = 'badge bg-success me-2';
            badge.textContent = 'Online';
            document.getElementById('auth-link').classList.add('d-none');
            document.getElementById('content-tabs').classList.remove('hidden');
            document.getElementById('auth-required-tabs').classList.add('hidden');
        } else {
            badge.className = 'badge bg-danger me-2';
            badge.textContent = 'Offline';
            document.getElementById('auth-link').classList.remove('d-none');
            document.getElementById('content-tabs').classList.add('hidden');
            document.getElementById('auth-required-tabs').classList.remove('hidden');
        }
        document.getElementById('auth-link').href = authUrl;
    }
    
    function updateKpis(dSalesStats) {
        document.getElementById('kpi-daily').textContent = dSalesStats.daily;
        document.getElementById('kpi-weekly').textContent = dSalesStats.weekly;
        document.getElementById('kpi-historic').textContent = dSalesStats.historic;
        document.getElementById('last-recalculated').textContent = formatDateTime(dSalesStats.last_update);
    }
    
    function updateComponentUsage(usageData) {
        const div = document.getElementById('component-usage-content');
        
        if (!usageData.components || usageData.components.length === 0) {
            div.innerHTML = '<div class="alert alert-info">Nenhum componente utilizado nos últimos 30 dias.</div>';
            return;
        }
        
        let html = '<table class="table table-striped"><thead><tr><th>Componente</th><th>SKU</th><th>Qtd. Utilizada</th><th>Produtos que Usam</th></tr></thead><tbody>';
        
        let total = 0;
        usageData.components.forEach(comp => {
            total += comp.quantidade;
            html += '<tr><td><strong>' + comp.nome + '</strong></td><td><code>' + comp.sku + '</code></td><td><span class="badge bg-success">' + comp.quantidade + 'x</span></td><td><small>' + comp.produtos.join(', ') + '</small></td></tr>';
        });
        
        html += '</tbody></table><div class="mt-3 p-3 bg-light rounded"><h6>Total de Insumos Consumidos: <span class="badge bg-primary fs-5">' + total + '</span></h6></div>';
        
        div.innerHTML = html;
    }
    
    const protoKpi = window.location.protocol === 'https:' ? 'wss' : 'ws';
    let wsKpi = new WebSocket(`${protoKpi}://${window.location.host}/ws/kpi-updates`);
    
    function setupKpiWebSocket() {
        wsKpi.onmessage = (e) => {
            const data = JSON.parse(e.data);
            
            if (data.type === 'full_update') {
                // 1. Atualiza Status de Autenticação
                updateAuthStatus(data.authenticated, data.auth_url);
                
                // 2. Atualiza KPIs
                if (data.sales_stats) {
updateKpis(data.sales_stats);

// Animação de atualização
const cards = document.querySelectorAll('.kpi-card');
cards.forEach(card => {
    card.style.backgroundColor = '#e8f5e9';
    setTimeout(() => {
card.style.backgroundColor = '';
    }, 500);
});
                }
                
                // 3. Atualiza Uso de Componentes
                if (data.component_usage) {
updateComponentUsage(data.component_usage);
                }
                
                // 4. Trata botões de recálculo
                const recalculateButton = document.getElementById('recalculate-button');
                if (recalculateButton && recalculateButton.disabled) {
recalculateButton.disabled = false;
recalculateButton.textContent = '🔄 Forçar Recálculo';
showToast('Sucesso', 'Recálculo de KPIs concluído.', 'success');
                }
                
                const forceLoadButton = document.querySelector('#kits button.btn-warning');
                if (forceLoadButton && forceLoadButton.disabled && data.cache_updated) {
forceLoadButton.disabled = false;
forceLoadButton.textContent = '🔄 Forçar Recarregamento';
loadKits(); // Recarrega a lista de kits após a confirmação do WS
showToast('Sucesso', 'Cache de produtos/kits atualizado.', 'success');
                }
            }
        };
        
        wsKpi.onerror = (e) => {
            console.error("Erro WebSocket KPI:", e);
            showToast('Erro', 'Conexão WebSocket perdida. Tentando reconectar...', 'danger');
        };
        
        wsKpi.onclose = () => {
            console.log("🔌 WebSocket KPI desconectado. Reconectando...");
            setTimeout(() => {
                const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
                wsKpi = new WebSocket(`${proto}://${window.location.host}/ws/kpi-updates`);
                setupKpiWebSocket();
            }, 3000);
        };
    }
    
    setupKpiWebSocket();

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
                const data = await fetchAPI(`${API}/product/search?q=${q}`);
                
                if(!data.length) {
div.innerHTML = '<div class="alert alert-warning">Nenhum resultado.</div>';
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
            <div class="mt-2">
                <b>Componentes:</b><br>
                ${p.componentes.map(c => 
`${c.quantidade}x ${c.nome || 'Sem nome'} (SKU: ${c.sku || 'N/D'})`
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
                
                // ADICIONE ESTA VALIDAÇÃO
                // ✅ 4. Frontend: ignorar “Produto simples / estoque”
                const kitsOnly = data.filter(p => p.tipo === 'Kit' || (p.componentes && p.componentes.length > 0));
                
                if (!kitsOnly || kitsOnly.length === 0) {
div.innerHTML = '<div class="alert alert-warning">⚠️ Nenhum Kit encontrado no cache. O worker pode estar carregando dados. Aguarde 10 minutos e recarregue a página.</div>';
return;
                }
                let html = `
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

                kitsOnly.forEach(k => {
const imgHtml = k.imagemURL 
    ? `<img src="${k.imagemURL}" style="width:50px;height:50px;object-fit:contain;border-radius:4px;" onerror="this.style.display='none'">` 
    : '<span class="text-muted">-</span>';

let comps = '';
if (k.componentes && k.componentes.length > 0) {
    const componentes_validos = k.componentes;
    
    if (componentes_validos.length > 0) {
comps = `<b>KIT (${componentes_validos.length} itens):</b><br>` + componentes_validos
    .map(c => `<small>• ${c.quantidade}x ${c.nome || 'Sem nome'} (SKU: ${c.sku || 'N/D'})</small>`)
    .join('<br>');
    } else {
comps = '<span class="text-info" style="font-size:0.8em">KIT sem componentes detalhados.</span>';
    }
} else {
    // ✅ 4. Frontend: ignorar “Produto simples / estoque”
    // Se não é kit, não renderiza a linha (o filter já removeu, mas para segurança)
    return; 
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

                html += '</tbody></table>';
                div.innerHTML = html;

            } catch(e) {
                div.innerHTML = 'Erro ao carregar lista. Verifique os logs.';
            }
        }

        async function forceAndReloadKits(event) {
            if (!isAuthenticated) {
                showToast('Aviso', 'Faça login primeiro!', 'warning');
                return;
            }
            
            const btn = event.target;
            btn.disabled = true;
            btn.textContent = '⏳ Forçando Recarregamento...';
            
            try {
                const data = await fetchAPI('/api/force-load', { method: 'POST' });
     showToast('Info', data.message || 'Recarregamento forçado com sucesso. Aguarde a atualização via WebSocket.', 'info');
     // Não precisa chamar loadKits() aqui, o WS fará isso
                
            } catch(e) {
                showToast('Erro', 'Erro ao forçar recarregamento: ' + e.message, 'danger');
            } finally {
                btn.disabled = false;
                btn.textContent = '🔄 Forçar Recarregamento';
            }
        }
    
    // Função para carregar o gráfico KPI
    let salesChart = null;
    
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
    borderColor: '#0d6efd',
    backgroundColor: 'rgba(13, 110, 253, 0.1)',
    tension: 0.4,
    fill: true
}, {
    label: 'Média Móvel (7 dias)',
    data: data.moving_avg,
    borderColor: '#ffc107',
    borderDash: [5, 5],
    tension: 0.4
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
            
            // Atualizar métricas
            document.getElementById('avg-daily').textContent = data.avg_daily.toFixed(1);
            document.getElementById('growth-weekly').textContent = 
                (data.growth > 0 ? '+' : '') + data.growth.toFixed(1) + '%';
            document.getElementById('trend-indicator').textContent = 
                data.growth > 10 ? '📈 Crescendo' : data.growth < -10 ? '📉 Caindo' : '📊 Estável';
        } catch(e) {
            console.error('Erro ao carregar gráfico KPI:', e);
        }
    }
    
    // A função loadComponentUsage não é mais necessária, pois os dados vêm do WS.
    // A função updateComponentUsage foi criada para receber os dados do WS.
    
    document.addEventListener('DOMContentLoaded', () => {
        loadKits();
        
        // Adicionar event listener para carregar o gráfico quando a aba for ativada
        const kpiTab = document.querySelector('[data-bs-target="#kpi-chart"]');
        if (kpiTab) {
            kpiTab.addEventListener('shown.bs.tab', loadKPIChart);
        }
        
        // ✅ 5. Componentes: timeout + erro visível
        const componentUsageTab = document.querySelector('[data-bs-target="#component-usage"]');
        if (componentUsageTab) {
            componentUsageTab.addEventListener('shown.bs.tab', () => {
                const contentDiv = document.getElementById('component-usage-content');
                
                // Se o conteúdo ainda for o "Carregando dados...", inicia o timeout
                if (contentDiv.innerHTML.includes('Carregando dados...')) {
setTimeout(() => {
    if (contentDiv.innerHTML.includes('Carregando dados...')) {
contentDiv.innerHTML = '<div class="alert alert-danger">❌ Não foi possível carregar o uso de componentes em tempo hábil. Verifique a autenticação e os logs.</div>';
    }
}, 8000); // 8 segundos de timeout
                }
            });
        }
    });
    </script>
    
    <!-- Assinatura do Desenvolvedor -->
    <footer style="background-color: #f8f9fa; border-top: 1px solid #dee2e6; margin-top: 3rem; padding: 1.5rem 0; text-align: center; color: #6c757d; font-size: 0.85rem;">
        <div class="container">
            <p style="margin: 0; font-weight: 500;">
                <span style="opacity: 0.7;">Desenvolvido por</span> 
                <strong>João Victor Dias Santana</strong>
            </p>
            <p style="margin: 0.25rem 0 0 0; opacity: 0.6; font-size: 0.8rem;">
                &copy; 2025 • Bling Automação Dashboard
            </p>
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
        orchestrator.start()
        start_cleanup_timer()
        logger.info("✅ Worker de fundo iniciado em modo local.")
        
    logger.info("Iniciando servidor Flask em modo local...")
    app.run(host='0.0.0.0', port=5000, debug=False)