#!/usr/bin/env python3

# ============================================================================
# GEVENT MONKEY PATCH — DEVE SER A PRIMEIRA COISA A EXECUTAR
# Gunicorn 25.1.0 criou um control server baseado em asyncio.
# Com worker gevent, asyncio.get_event_loop() falha: "no running event loop".
# Solução: monkey_patch antes de tudo + forçar criação do event loop asyncio.
# ============================================================================
try:
    from gevent import monkey as _gm
    _gm.patch_all(thread=True, socket=True, dns=True, time=True,
                  select=True, ssl=True, subprocess=True, signal=True,
                  builtins=False, os=True)
    import asyncio as _aio
    try:
        _aio.get_event_loop()
    except RuntimeError:
        _aio.set_event_loop(_aio.new_event_loop())
    del _gm, _aio
except ImportError:
    pass  # Sem gevent instalado — modo local com threads puras

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
import shutil
import hmac
import hashlib

from pathlib import Path
from datetime import datetime, timedelta, timezone
from threading import Lock, Thread, Event
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
try:
    from simple_websocket import ConnectionClosed
except ImportError:
    class ConnectionClosed(Exception): pass

# Filtro: suprime log de erro residual do gunicorn control server
# Caso o event loop não seja encontrado mesmo após o patch acima
import logging as _log_setup
for _guni_logger in ('gunicorn.arbiter', 'gunicorn.error', 'gunicorn'):
    _log_setup.getLogger(_guni_logger).addFilter(
        type('_SuppressNoLoop', (_log_setup.Filter,), {
            'filter': staticmethod(lambda r: 'no running event loop' not in r.getMessage())
        })()
    )
del _log_setup, _guni_logger

# ============================================================================
# MONGODB — Camada de Persistência Central
# ============================================================================
# Defina MONGODB_URI nas variáveis de ambiente do Render.
# Se não definida, cai para arquivos locais (modo legado).
try:
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError
    _MONGO_URI = os.environ.get('MONGODB_URI', '') or os.environ.get('MONGO_URI', '')
    if _MONGO_URI:
        _mongo_client = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=5000)
        _mongo_db = _mongo_client.get_database('sw_moveis')
        _mongo_client.admin.command('ping')  # testa conexão na inicialização
        MONGO_AVAILABLE = True
    else:
        MONGO_AVAILABLE = False
        _mongo_db = None
except Exception as _mongo_err:
    MONGO_AVAILABLE = False
    _mongo_db = None

class MongoStore:
    """
    Camada de acesso unificada ao MongoDB.
    Cada coleção pode ter múltiplos documentos identificados por _id.
    Usado como backend persistente para timers, consumo, pedidos, tokens e stats.
    """
    @staticmethod
    def get(collection: str, doc_id: str = 'main') -> dict:
        if not MONGO_AVAILABLE:
            return {}
        try:
            doc = _mongo_db[collection].find_one({'_id': doc_id})
            if doc:
                doc.pop('_id', None)
            return doc or {}
        except Exception:
            return {}

    @staticmethod
    def set(collection: str, data: dict, doc_id: str = 'main') -> bool:
        if not MONGO_AVAILABLE:
            return False
        try:
            payload = {k: v for k, v in data.items() if k != '_id'}
            _mongo_db[collection].update_one(
                {'_id': doc_id},
                {'$set': payload},
                upsert=True
            )
            return True
        except Exception:
            return False

    @staticmethod
    def get_all(collection: str) -> dict:
        """Retorna todos os docs da coleção como dict keyed by _id."""
        if not MONGO_AVAILABLE:
            return {}
        try:
            result = {}
            for doc in _mongo_db[collection].find():
                key = str(doc.pop('_id'))
                result[key] = doc
            return result
        except Exception:
            return {}

    @staticmethod
    def upsert(collection: str, doc_id: str, data: dict) -> bool:
        if not MONGO_AVAILABLE:
            return False
        try:
            payload = {k: v for k, v in data.items() if k != '_id'}
            _mongo_db[collection].update_one(
                {'_id': doc_id},
                {'$set': payload},
                upsert=True
            )
            return True
        except Exception:
            return False

    @staticmethod
    def remove(collection: str, doc_id: str) -> bool:
        if not MONGO_AVAILABLE:
            return False
        try:
            _mongo_db[collection].delete_one({'_id': doc_id})
            return True
        except Exception:
            return False

# ============================================================================
# CONFIGURAÇÃO DE DISCO (fallback quando MongoDB não disponível)
# ============================================================================
DATA_DIR = Path(os.environ.get('DATA_DIR', '.'))

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

# ==============================================================================
# CONFIGURAÇÃO DE RECEITA (Adicione logo após os imports)
# ==============================================================================
RECIPE_CADEIRA = [
    {"nome": "COMPENSADO 50X52X17", "qtd": 1, "un": "Peça"},
    {"nome": "SARRAFO 52", "qtd": 3, "un": "Peças"},
    {"nome": "SARRAFO 46", "qtd": 1, "un": "Peça"},
    {"nome": "SARRAFO 14", "qtd": 2, "un": "Peças"},
    {"nome": "MDF 15MM 52X35", "qtd": 2, "un": "Peças"},
    {"nome": "MDF 6MM 52X35", "qtd": 2, "un": "Peças"},
    {"nome": "SARRAFO 33", "qtd": 2, "un": "Peças"},
    {"nome": "SARRAFO 10", "qtd": 2, "un": "Peças"},
    {"nome": "MDF 15MM", "qtd": 1, "un": "Peça"},
    {"nome": "TECIDO", "qtd": 3, "un": "Metros"},
    {"nome": "ESPUMA ACOPLAGEM", "qtd": 0.5, "un": "Metro"},
    {"nome": "ESPUMA ASSENTO", "qtd": 1, "un": "Unid"},
    {"nome": "ESPUMA ENCOSTO", "qtd": 1, "un": "Unid"},
    {"nome": "ESPUMA CABEÇOTE", "qtd": 1, "un": "Unid"},
    {"nome": "ESPUMA ASSENTO 52X7,5X1", "qtd": 1, "un": "Peça"},
    {"nome": "ESPUMA ASSENTO 54X14X1", "qtd": 1, "un": "Peça"},
    {"nome": "ESPUMA BRAÇO 52X21X1", "qtd": 1, "un": "Peça"},
    {"nome": "ESPUMA BRAÇO 52X35X1", "qtd": 1, "un": "Peça"},
    {"nome": "ESPUMA BRAÇO 35X9,5X1", "qtd": 4, "un": "Peças"},
    {"nome": "ESPUMA BRAÇO 54X9,5X2", "qtd": 2, "un": "Peças"},
    {"nome": "LINHA", "qtd": 1, "un": "Unid"},
    {"nome": "COLA", "qtd": 1, "un": "Unid"},
    {"nome": "LAMINA CROMADA", "qtd": 1, "un": "Unid"},
    {"nome": "LAMINA DE CABEÇOTE", "qtd": 1, "un": "Unid"},
    {"nome": "PARAFUSO 1/4 X 1", "qtd": 15, "un": "Peças"},
    {"nome": "PARAFUSO 1/4 X 2.1/4", "qtd": 8, "un": "Peças"},
    {"nome": "PARAFUSO 5X25", "qtd": 6, "un": "Peças"},
    {"nome": "PORCA GARRA 1/4", "qtd": 20, "un": "Peças"},
    {"nome": "GRAMPO 80/10", "qtd": 1, "un": "Unid"},
    {"nome": "GRAMPO 14/40", "qtd": 1, "un": "Unid"},
    {"nome": "COSTUREIRA", "qtd": 1, "un": "Serviço"},
    {"nome": "EMBALAGEM", "qtd": 1, "un": "Unid"},
    {"nome": "BASE", "qtd": 1, "un": "Unid"}
]
# Podeis ajustar os nomes e quantidades conforme a vossa realidade nobre.
# ==============================================================================

# Lock global para impedir múltiplas trocas de token simultâneas (Erro Worker Timeout)
token_exchange_lock = Lock()
kpi_update_callbacks: List[Callable] = []
kpi_update_lock = Lock()
_cleanup_timer_started = False  # garante que cleanup_timer só é iniciado uma vez

# ============================================================================ 
# 1. LOGS AVANÇADOS
# ============================================================================

class InMemoryLogHandler(logging.Handler):
    """Handler de log que armazena os registros em memória para o WebSocket."""
    def __init__(self, max_logs=100):
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
                dead = []
                for cb in self.ws_callbacks:
                    try:
                        cb(log_entry)
                    except Exception:
                        dead.append(cb)
                for cb in dead:
                    self.ws_callbacks.remove(cb)
        except Exception:
            self.handleError(record)

    def get_logs(self, limit=None):
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
    _log_level_str = os.environ.get('BLING_LOG_LEVEL', 'INFO').upper()
    _log_level = getattr(logging, _log_level_str, logging.INFO)
    logger.setLevel(_log_level)
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
    """Log periódico de callbacks KPI ativos (limpeza real ocorre no broadcast)."""
    with kpi_update_lock:
        n = len(kpi_update_callbacks)
    if n > 0:
        logger.debug(f"🔗 WebSocket KPI: {n} conexão(ões) ativa(s)")

def start_cleanup_timer():
    """Inicia timer para limpar callbacks órfãos — idempotente, roda no máximo 1 vez."""
    global _cleanup_timer_started
    if _cleanup_timer_started:
        return
    _cleanup_timer_started = True

    def cleanup_loop():
        while True:
            time.sleep(300)
            cleanup_kpi_callbacks()

    Thread(target=cleanup_loop, daemon=True, name="cleanup_timer").start()

# ============================================================================ 
# 2. CONFIGURAÇÕES
# ============================================================================

class Config:
    """Configurações globais da aplicação."""
    
    # Bling OAuth
    CLIENT_ID: str = os.environ.get('BLING_CLIENT_ID', 'YOUR_CLIENT_ID')
    CLIENT_SECRET: str = os.environ.get('BLING_CLIENT_SECRET', 'YOUR_CLIENT_SECRET')
    WEBHOOK_SECRET: str = os.environ.get('BLING_WEBHOOK_SECRET', 'YOUR_WEBHOOK_SECRET')
    REDIRECT_URI: str = os.environ.get('BLING_REDIRECT_URI')
    if not REDIRECT_URI:
        pass
    
    # API
    BLING_API_URL: str = 'https://www.bling.com.br/Api/v3'
    TOKEN_URL: str = 'https://www.bling.com.br/Api/v3/oauth/token'
    
    # Retry e Timeout
    REQUEST_TIMEOUT: int = 30
    AUTH_TIMEOUT: int = 20  # Timeout para auth (aumentado para cold start no Render)
    MAX_RETRIES: int = 3
    BASE_DELAY: float = 1.0
    
    # Rate Limiting (Configurável) - OTIMIZADO
    MAX_PAGES_PER_BATCH: int = 5  # Pode aumentar um pouco se quiser
    DELAY_BETWEEN_PAGES: float = 0.8  # Reduzido de 5.0 para 0.8 (mais rápido)
    DELAY_BETWEEN_BATCHES: float = 5.0  # Reduzido de 15.0 para 5.0
    
    # Automação
    
    
    # Arquivos
    TOKENS_FILE: Path = DATA_DIR / 'tokens.json'
    
    # Token Inicial (para implantação)
    INITIAL_REFRESH_TOKEN: Optional[str] = os.environ.get('BLING_REFRESH_TOKEN')

    SALES_STATS_FILE: Path = DATA_DIR / 'sales_stats.json'
    PRODUCTS_CACHE_FILE: Path = DATA_DIR / 'products_cache.json'

# ============================================================================ 
# 3. UTILITÁRIOS E AUTH (FUNÇÕES SEGURAS)
# ============================================================================

def atomic_write_json(data: dict, path: Path):
    """Escreve em um arquivo temporário e renomeia (Atômico/Seguro)."""
    temp_path = path.with_suffix('.tmp')
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        # Move o temporário para o original (operação atômica no OS)
        shutil.move(str(temp_path), str(path))
    except Exception as e:
        logger.exception(f"Erro ao salvar arquivo {path} de forma atômica.")
        if temp_path.exists():
            os.remove(temp_path)

def load_tokens_safe(path: Path | str = "tokens.json"):
    # MongoDB primeiro
    if MONGO_AVAILABLE:
        try:
            data = MongoStore.get('auth_tokens', 'tokens')
            if data:
                return data
        except Exception:
            pass
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
    # MongoDB primeiro
    if MONGO_AVAILABLE:
        try:
            MongoStore.set('auth_tokens', data, 'tokens')
            logger.info("Tokens salvos no MongoDB.")
            return
        except Exception as e:
            logger.error(f"Erro ao salvar tokens no MongoDB: {e}")
    if isinstance(path, str): path = Path(path)
    atomic_write_json(data, path)
    logger.info("Tokens salvos em arquivo (fallback).")

def load_stats_safe(path: Path):
    """Carrega as estatísticas de vendas — MongoDB primeiro, arquivo fallback."""
    if MONGO_AVAILABLE:
        try:
            data = MongoStore.get('sales_stats', 'stats')
            if data:
                if 'last_recalculated' in data and isinstance(data['last_recalculated'], str):
                    try:
                        data['last_recalculated'] = datetime.fromisoformat(data['last_recalculated'])
                    except Exception:
                        pass
                return data
        except Exception:
            pass
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if data and 'last_recalculated' in data and isinstance(data['last_recalculated'], str):
                 data['last_recalculated'] = datetime.fromisoformat(data['last_recalculated'])
            return data
    except Exception as e:
        logger.exception(f"Erro lendo {path.name}.")
        return None

def save_stats(data: Dict[str, Any], path: Path):
    """Salva as estatísticas de vendas — MongoDB primeiro, arquivo fallback."""
    data_to_save = data.copy()
    if 'last_recalculated' in data_to_save and isinstance(data_to_save['last_recalculated'], datetime):
        data_to_save['last_recalculated'] = data_to_save['last_recalculated'].isoformat()
    if MONGO_AVAILABLE:
        try:
            MongoStore.set('sales_stats', data_to_save, 'stats')
            logger.info("Estatísticas salvas no MongoDB.")
            return
        except Exception as e:
            logger.error(f"Erro ao salvar stats no MongoDB: {e}")
    atomic_write_json(data_to_save, path)
    logger.info("Estatísticas salvas em arquivo (fallback).")

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
    Carrega cache de produtos e kits — MongoDB primeiro, arquivo fallback.
    """
    if MONGO_AVAILABLE:
        try:
            data = MongoStore.get('products_cache', 'cache')
            if data and (data.get('products') or data.get('kits')):
                return data
        except Exception:
            pass
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
    logger.debug(f"save_products_cache chamado. products={len(products or [])} kits={len(kits or [])} total={total_produtos}")
    
    # ✅ 3. Nunca salvar cache se produtos == 0
    if total_produtos == 0:
        logger.warning("⛔ Cache vazio ignorado. Não salvando no disco. Isto indica que a API não retornou produtos ou que o parsing falhou.")
        return
        
    payload = {
        "updated_at": datetime.now().isoformat(),
        "products": products or [],
        "kits": kits or []
    }
    if MONGO_AVAILABLE:
        try:
            MongoStore.set('products_cache', payload, 'cache')
            logger.info(f"Cache de produtos salvo no MongoDB. Total: {total_produtos}")
            return
        except Exception as e:
            logger.error(f"Erro ao salvar cache no MongoDB: {e}")
    try:
        atomic_write_json(payload, cache_file)
        skus = [p.get('sku') for p in (products or [])[:5]] + [k.get('sku') for k in (kits or [])[:5]]
        logger.info(f"Cache salvo em arquivo com sample skus: {skus}. Total: {total_produtos}")
    except Exception as e:
        logger.exception("Erro ao salvar cache de produtos em arquivo.")

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
            response = self.session.request(method, url, timeout=45, **kwargs)
            latency = time.time() - start_time
            self.metrics.record_request(response.status_code, latency)
            self.logger.debug(f"API {method} {endpoint} -> {response.status_code} ({latency*1000:.0f}ms)")

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
            
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                # Silencioso para 404, deixa o chamador decidir
                raise e
            self.logger.error(f"Erro HTTP em {endpoint}: {str(e)}")
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

    def register_webhook(self, event: str, url: str):
        """
        Na API v3 do Bling, o registro de webhooks deve ser feito manualmente
        no painel do desenvolvedor (Cadastro de Aplicativos > Webhooks).
        """
        self.logger.info(f"📢 Configure o webhook '{event}' manualmente no painel do Bling → {url}")
        return {"status": "manual_config_required"}

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

    def _validate_oauth_state(self, state: str) -> bool:
        """Valida o state recebido no callback contra o salvo no arquivo."""
        saved_state = self._load_oauth_state()
        if not saved_state or not state:
            return False
        
        is_valid = (saved_state == state)
        if is_valid:
            # ✅ MELHORIA: Não limpamos imediatamente para permitir retentativas rápidas (F5)
            # O state será limpo naturalmente na próxima geração de URL de auth
            self.logger.info(f"State OAuth validado com sucesso: {state}")
            
        return is_valid

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
        
        if not self.config.REDIRECT_URI:
            raise ValueError("CRÍTICO: BLING_REDIRECT_URI não configurada nas variáveis de ambiente!")
        # ---------------------------------

        self.logger = logging.getLogger('bling_automacao')
        self._tokens = self._load_tokens()
        self._access_token = self._tokens.get('access_token')
        self._refresh_token = self._tokens.get('refresh_token')
        self._expires_at = self._tokens.get('expires_at', 0)
        self._initial_load_failed = True
        
        # Se não houver refresh token no arquivo, mas houver na variável de ambiente, usa o da env
        if not self._refresh_token and self.config.INITIAL_REFRESH_TOKEN:
            self.logger.info("Utilizando BLING_REFRESH_TOKEN da variável de ambiente.")
            self._refresh_token = self.config.INITIAL_REFRESH_TOKEN
            # Salva imediatamente para persistir no arquivo
            self._save_tokens()
        
        if not self._access_token and not self._refresh_token:
            self.logger.warning("⚠️ Nenhum token encontrado no arquivo ou ambiente. Necessário realizar autenticação OAuth.")
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

    def reload_tokens_from_disk(self):
        """Recarrega tokens do storage (MongoDB ou arquivo) para a memória."""
        try:
            disk_tokens = self._load_tokens()
            self._access_token = disk_tokens.get('access_token')
            self._refresh_token = disk_tokens.get('refresh_token')
            self._expires_at = disk_tokens.get('expires_at', 0)
            status = "válido" if (self._access_token and self._expires_at > time.time() + 60) else                      "refresh disponível" if self._refresh_token else "ausente"
            logger.info(f"🔑 Tokens carregados — status: {status}")
            return True
        except Exception as e:
            logger.error(f"Erro ao recarregar tokens: {e}")
            return False

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
        """Renova o token de acesso usando o refresh token com proteção contra Race Condition."""
        if not self._refresh_token:
            if not self._initial_load_failed:
                self.logger.warning("Não há refresh token disponível para renovação.")
            self._initial_load_failed = False
            return False
            
        self.logger.info("Verificando necessidade de renovação do token...")
        
        # O uso de 'with' garante que o lock será liberado
        with token_exchange_lock:
            # 1. VERIFICAÇÃO CRÍTICA: Recarrega do disco antes de tentar renovar
            # Isso impede que um processo tente renovar um token que outro processo já renovou
            disk_data = self._load_tokens()
            disk_access = disk_data.get('access_token')
            disk_expires = disk_data.get('expires_at', 0)
            
            # Se o arquivo já tem um token válido (renovado por outro worker/thread), usa ele!
            if disk_access and disk_expires > time.time() + 60:
                self.logger.info("Token já foi renovado por outro processo. Carregando do disco.")
                self._access_token = disk_access
                self._refresh_token = disk_data.get('refresh_token')
                self._expires_at = disk_expires
                return True

            # 2. Se realmente estiver expirado no disco, faz a requisição ao Bling
            self.logger.info("Iniciando requisição de renovação ao Bling...")
            success = self._perform_token_request(
                grant_type='refresh_token',
                refresh_token=self._refresh_token
            )
            
            if success:
                self.logger.info("Token renovado com sucesso via API.")
            else:
                self.logger.error(f"Falha na renovação do token. Refresh Token atual: {self._refresh_token[:10]}... Necessário reautenticar.")
                # Se falhar totalmente, avise o front via WS se possível
                # (Isso exige passar o orchestrator para o AuthManager ou usar callbacks, 
                # mas como simplificação, apenas certifique-se que o 'is_authenticated' retorne False)
                
                # ✅ LOG CRÍTICO: Se falhar, vamos registrar o estado do arquivo para depuração
                disk_data = self._load_tokens()
                self.logger.debug(f"Estado do tokens.json no momento da falha: {list(disk_data.keys())}")
                
            return success

    def _perform_token_request(self, grant_type: str, **kwargs) -> bool:
        """Executa a requisição de troca/renovação de token."""
        self.logger.debug(f"Iniciando requisição de token: grant_type={grant_type}")
        
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
    monthly_count: int = 0
    historic_count: int = 0
    
    # Dados para o Gráfico (Cache)
    history_data: Dict[str, Any] = field(default_factory=dict)
    
    # Cache de Pedidos
    _orders_cache: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    
    # Histórico de Vendas Estruturado
    _sales_history: List[Dict[str, Any]] = field(default_factory=list)
    
    # Novo: Histórico para Gráfico
    stats_history: Dict[str, Any] = field(default_factory=lambda: {'dates': [], 'daily': [], 'moving_avg': [], 'growth': 0, 'avg_daily': 0})
    
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
                self.monthly_count = data.get('monthly', 0)
                self.historic_count = data.get('historic', 0)
                self.history_data = data.get('history_data', {})
                self.stats_history = data.get('stats_history', {'dates': [], 'daily': [], 'moving_avg': [], 'growth': 0, 'avg_daily': 0})
                self._orders_cache = data.get('orders_cache', {})
                # sales_history agora é salvo separado (ver _save_sales_history)
                inline = data.get('sales_history', [])  # compatibilidade com dados antigos
                if inline:
                    self._sales_history = inline
                
                # Carrega sales_history da coleção separada
                if not self._sales_history and MONGO_AVAILABLE:
                    try:
                        hist_doc = MongoStore.get('sales_history', 'history')
                        loaded = hist_doc.get('orders', [])
                        if loaded:
                            self._sales_history = loaded
                            logger.info(f"✅ sales_history: {len(loaded)} pedidos carregados do MongoDB")
                    except Exception:
                        pass

                last_recalc = data.get('last_recalculated')
                if isinstance(last_recalc, str):
                    try:
                        self.last_recalculated = datetime.fromisoformat(last_recalc)
                    except:
                        self.last_recalculated = datetime.now()
                elif isinstance(last_recalc, datetime):
                    self.last_recalculated = last_recalc
                else:
                    self.last_recalculated = datetime.now()

    def get_stats(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "daily": self.daily_count,
                "weekly": self.weekly_count,
                "monthly": self.monthly_count,
                "historic": self.historic_count,
                "history_data": self.history_data,
                "stats_history": self.stats_history,
                "last_update": self.last_recalculated.isoformat()
            }

    def _get_state_for_save(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "daily": self.daily_count,
                "weekly": self.weekly_count,
                "monthly": self.monthly_count,
                "historic": self.historic_count,
                "history_data": self.history_data,
                "stats_history": self.stats_history,
                # sales_history salvo separadamente (evita doc > 16MB no MongoDB)
                # orders_cache é derivado e não precisa persistir
                "last_recalculated": self.last_recalculated.isoformat()
            }

    def _save_sales_history(self):
        """Salva o histórico de pedidos separadamente para não estourar o doc de stats."""
        try:
            # Salva apenas os campos essenciais de cada pedido (reduz tamanho drasticamente)
            compact = [
                {'id': o.get('id'), 'data': o.get('data'), 'itens': o.get('itens', []),
                 'contato': o.get('contato'), 'numero': o.get('numero')}
                for o in self._sales_history
            ]
            if MONGO_AVAILABLE:
                try:
                    MongoStore.set('sales_history', {'orders': compact}, 'history')
                except Exception as e:
                    logger.error(f"Erro ao salvar sales_history no MongoDB: {e}")
        except Exception as e:
            logger.error(f"Erro ao compactar sales_history: {e}")

    def recalculate_from_orders(self, all_orders):
        """Recalcula métricas e histórico baseado na lista de pedidos."""
        from collections import defaultdict
        self.logger.info(f"Recalculando estatísticas com {len(all_orders)} pedidos.")
        
        tz_br = timezone(timedelta(hours=-3))
        now = datetime.now(tz_br)
        
        # Mantém KPIs de calendário (Hoje, Semana Atual, Mês Atual)
        hoje = now.date()
        inicio_semana = hoje - timedelta(days=hoje.weekday())
        inicio_mes = hoje.replace(day=1)
        
        inicio_grafico = hoje - timedelta(days=29) # Últimos 30 dias
        
        daily_orders = []
        weekly_orders = []
        monthly_orders = []
        
        # Dicionário para gráfico (agora usa janela móvel)
        daily_counts_chart = defaultdict(int) 
        monthly_report = defaultdict(int)

        for o in all_orders:
            try:
                date_str = o.get('data') or o.get('dataEmissao')
                if not date_str: continue
                try:
                    dt = datetime.fromisoformat(date_str.replace(' ', 'T'))
                except:
                    try:
                        dt = datetime.strptime(date_str.split(' ')[0], "%Y-%m-%d")
                    except:
                        continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=tz_br)
                
                dt_pedido = dt.date()
                
                if dt.year == now.year:
                    monthly_report[dt.month] += 1
                
                # KPIs Estáticos
                if dt_pedido == hoje: daily_orders.append(o)
                if dt_pedido >= inicio_semana: weekly_orders.append(o)
                if dt_pedido >= inicio_mes: monthly_orders.append(o)
                
                # Dados para o Gráfico (Últimos 30 dias)
                if dt_pedido >= inicio_grafico:
                    daily_counts_chart[dt_pedido] += 1
            except Exception:
                continue

        # Gera eixo X do gráfico (30 dias corridos)
        dates = [(inicio_grafico + timedelta(days=i)) for i in range(30)]
        counts = [daily_counts_chart.get(d, 0) for d in dates]
        moving_avg = []
        for i in range(len(counts)):
            subset = counts[max(0, i-6):i+1]
            moving_avg.append(sum(subset) / len(subset) if subset else 0)
        last_week = sum(counts[-7:])
        prev_week = sum(counts[-14:-7])
        growth = ((last_week - prev_week) / prev_week * 100) if prev_week else 0

        with self.lock:
            self.daily_count = len(daily_orders)
            self.weekly_count = len(weekly_orders)
            # Atualiza o contador do mês ATUAL para manter o KPI do topo do dashboard
            self.monthly_count = len(monthly_orders)
            self.historic_count = len(all_orders)
            
            # Salva o relatório completo de todos os meses em history_data
            self.history_data['yearly_monthly_report'] = dict(monthly_report)
            
            self.stats_history = {
                'dates': [d.isoformat() for d in dates],
                'daily': counts,
                'moving_avg': moving_avg,
                'growth': round(growth, 1),
                'avg_daily': round(sum(counts[-30:]) / 30, 1) if len(counts) >= 30 else 0
            }
            self.last_recalculated = now
            self._orders_cache = {o.get('id'): o for o in all_orders[-100:]}
            
        save_stats(self._get_state_for_save(), self.config.SALES_STATS_FILE)
        self._save_sales_history()  # salva histórico de pedidos separado
        self.logger.info(f"✅ Estatísticas atualizadas: D:{self.daily_count} W:{self.weekly_count} M:{self.monthly_count}")

class ProductionTimer:
    """Gerencia cronômetros de produção e histórico detalhado."""
    FILE_PATH = DATA_DIR / 'production_timers.json'
    HISTORY_PATH = DATA_DIR / 'production_history.json'

    def __init__(self):
        self.timers = self._load()
        self._active_savers: set = set()  # rastreia nomes com saver ativo
        self._auto_pause_on_restart()
        for nome in list(self.timers.keys()):
            self._launch_background_saver(nome)

    def _load(self):
        """Carrega timers — MongoDB primeiro, arquivo como fallback real."""
        if MONGO_AVAILABLE:
            try:
                data = MongoStore.get('production_timers', 'timers')
                timers = data.get('timers', {})
                if timers:  # só usa MongoDB se retornou dados de fato
                    return timers
                # MongoDB vazio — pode ser falha silenciosa, tenta arquivo
                logger.info("MongoDB retornou timers vazio — verificando arquivo local...")
            except Exception as e:
                logger.warning(f"Falha ao carregar timers do MongoDB: {e}")
        if not self.FILE_PATH.exists():
            return {}
        try:
            with open(self.FILE_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data:
                    logger.info(f"✅ Timers carregados do arquivo local: {len(data)} timers")
                return data
        except Exception as e:
            logger.error(f"Erro ao carregar timers do arquivo: {e}")
            return {}

    def _save(self):
        """Salva timers — MongoDB E arquivo local (dupla redundância)."""
        if MONGO_AVAILABLE:
            try:
                MongoStore.set('production_timers', {'timers': self.timers}, 'timers')
            except Exception as e:
                logger.error(f"Erro ao salvar timers no MongoDB: {e}")
        # Sempre salva no arquivo também (seguro contra falha do MongoDB)
        temp_file = self.FILE_PATH.with_suffix('.tmp')
        try:
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self.timers, f, indent=4, ensure_ascii=False)
            shutil.move(str(temp_file), str(self.FILE_PATH))
        except Exception as e:
            logger.error(f"Erro ao salvar timers em arquivo: {e}")

    def _auto_pause_on_restart(self):
        """
        Ao reiniciar: pausa timers 'running' E soma o tempo que estava rodando.
        O background_saver salva a cada 30s, então perdemos no máximo 30s.
        Garante que o tempo acumulado não seja perdido.
        """
        changed = False
        now = time.time()
        for k, v in self.timers.items():
            if v.get('state') == 'running':
                start_ts = v.get('start_ts', 0)
                if start_ts and start_ts > 0:
                    # Soma o tempo que estava rodando desde o último checkpoint
                    v['accumulated'] = v.get('accumulated', 0) + (now - start_ts)
                v['state'] = 'paused'
                v['start_ts'] = 0
                changed = True
        if changed:
            self._save()
            logger.info(f"⏸ Restart: {sum(1 for v in self.timers.values() if v.get('state')=='paused')} timers pausados com tempo preservado.")

    def start(self, produto_nome):
        now = time.time()
        if produto_nome not in self.timers:
            self.timers[produto_nome] = {
                'start_ts': now,
                'accumulated': 0,
                'state': 'running',
                'created_at': datetime.now().isoformat(),
                'checklist': {}
            }
        else:
            t = self.timers[produto_nome]
            if t['state'] != 'running':
                t['start_ts'] = now
                t['state'] = 'running'
        self._save()
        self._launch_background_saver(produto_nome)
        return self.get_status(produto_nome)

    def _launch_background_saver(self, nome):
        """Thread que faz checkpoint do timer a cada 30s. Garante no máximo 1 thread por timer."""
        if nome in self._active_savers:
            return  # Já existe saver para este timer
        self._active_savers.add(nome)

        def background_saver():
            try:
                while True:
                    time.sleep(30)
                    if nome not in self.timers:
                        break  # Timer removido (concluído/zerado)
                    t = self.timers[nome]
                    if t.get('state') == 'running' and t.get('start_ts', 0) > 0:
                        now_ts = time.time()
                        t['accumulated'] = t.get('accumulated', 0) + (now_ts - t['start_ts'])
                        t['start_ts'] = now_ts
                    try:
                        self._save()
                    except Exception as e:
                        logger.error(f"background_saver erro: {e}")
            finally:
                self._active_savers.discard(nome)  # Libera ao sair

        Thread(target=background_saver, daemon=True, name=f"saver_{nome[:40]}").start()

    def pause(self, produto_nome):
        if produto_nome in self.timers and self.timers[produto_nome]['state'] == 'running':
            t = self.timers[produto_nome]
            # Soma o tempo decorrido desde o start até agora
            t['accumulated'] += (time.time() - t['start_ts'])
            t['start_ts'] = 0
            t['state'] = 'paused'
            self._save()
        return self.get_status(produto_nome)

    def stop_and_log(self, produto_nome):
        """Finaliza produção: pausa timer, registra componentes e salva histórico."""
        nome_real = produto_nome.split('||')[0] if '||' in produto_nome else produto_nome

        # Recupera checklist antes de pausar
        checklist_marcado = {}
        if produto_nome not in self.timers:
            # Tenta recarregar do storage antes de desistir
            reloaded = self._load()
            if produto_nome in reloaded:
                self.timers[produto_nome] = reloaded[produto_nome]
                logger.info(f"🔄 Timer '{produto_nome}' recuperado do storage")

        if produto_nome in self.timers:
            checklist_marcado = self.timers[produto_nome].get('checklist', {})
            status = self.pause(produto_nome)
            total_seconds = status['elapsed']
        else:
            # Timer realmente não existe
            total_seconds = 0
            logger.info(f"⚠️ Timer não encontrado para '{produto_nome}' — registrando com tempo 0")

        # Isso garante que a produção que esqueceu de marcar seja contabilizada
        if 'CADEIRA' in nome_real.upper():
            auto_registrados = 0
            for comp in RECIPE_CADEIRA:
                nome_comp = comp['nome']
                if not checklist_marcado.get(nome_comp, False):
                    try:
                        component_consumption.register_component(
                            nome_comp, comp['qtd'], comp['un'], nome_real
                        )
                        auto_registrados += 1
                    except Exception as e:
                        logger.error(f"Auto-registro componente '{nome_comp}': {e}")
            if auto_registrados > 0:
                logger.info(f"✅ Auto-registrados {auto_registrados} componentes faltantes para '{nome_real}'")
            logger.info(f"✅ Todos componentes processados para '{nome_real}'")

        registro = {
            "produto": nome_real,
            "tempo_segundos": total_seconds,
            "data_conclusao": datetime.now().isoformat(),
            "timestamp": time.time(),
            "checklist": checklist_marcado
        }
        self._add_to_history(registro)

        if produto_nome in self.timers:
            del self.timers[produto_nome]
            self._save()

        return {'elapsed': total_seconds, 'state': 'finished', 'registro': registro}

    def reset(self, produto_nome):
        if produto_nome in self.timers:
            del self.timers[produto_nome]
            self._save()
        return {'elapsed': 0, 'state': 'stopped'}

    def get_status(self, produto_nome):
        if produto_nome not in self.timers:
            return {'elapsed': 0, 'state': 'stopped'}
        t = self.timers[produto_nome]
        total = t['accumulated']
        if t['state'] == 'running':
            total += (time.time() - t['start_ts'])
        return {'elapsed': int(total), 'state': t['state'], 'checklist': t.get('checklist', {})}

    def get_active_timers(self):
        """Retorna timers ativos com tempo ao vivo calculado no servidor."""
        active = []
        for nome, data in self.timers.items():
            current_total = data.get('accumulated', 0)
            if data.get('state') == 'running' and data.get('start_ts', 0) > 0:
                current_total += (time.time() - data['start_ts'])
            active.append({
                "produto": nome,
                "estado": data.get('state', 'paused'),
                "tempo_decorrido": int(current_total),
                "inicio": data.get('created_at', ''),
                "checklist_count": sum(1 for v in data.get('checklist', {}).values() if v),
                "checklist_total": len(data.get('checklist', {})),
            })
        return active

    def _add_to_history(self, registro):
        """Salva no histórico mensal — MongoDB principal, arquivo fallback."""
        mes_chave = datetime.now().strftime('%Y-%m')
        # Garante que o registro é serializável (converte tipos Python para primitivos)
        def _clean(obj):
            if isinstance(obj, dict):
                return {k: _clean(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_clean(i) for i in obj]
            if isinstance(obj, bool):
                return obj
            if isinstance(obj, (int, float)):
                return obj
            return str(obj) if obj is not None else None
        reg_clean = _clean(registro)

        saved = False
        if MONGO_AVAILABLE:
            try:
                _mongo_db['production_history'].update_one(
                    {'_id': mes_chave},
                    {'$push': {'registros': reg_clean}},
                    upsert=True
                )
                saved = True
            except Exception as e:
                logger.error(f"Erro ao salvar histórico no MongoDB: {e}")
        # Sempre salva no arquivo também como backup redundante
        try:
            history = {}
            if self.HISTORY_PATH.exists():
                with open(self.HISTORY_PATH, 'r', encoding='utf-8') as f:
                    history = json.load(f)
            if mes_chave not in history:
                history[mes_chave] = []
            history[mes_chave].append(reg_clean)
            temp = self.HISTORY_PATH.with_suffix('.tmp')
            with open(temp, 'w', encoding='utf-8') as f:
                json.dump(history, f, ensure_ascii=False)
            shutil.move(str(temp), str(self.HISTORY_PATH))
            if not saved:
                logger.info(f"Histórico salvo em arquivo (fallback).")
        except Exception as e:
            logger.error(f"Erro ao salvar histórico em arquivo: {e}")

    def get_monthly_history_details(self):
        """Retorna a lista detalhada do mês atual — MongoDB primeiro, arquivo fallback."""
        mes_chave = datetime.now().strftime('%Y-%m')
        if MONGO_AVAILABLE:
            try:
                doc = _mongo_db['production_history'].find_one({'_id': mes_chave})
                registros = (doc or {}).get('registros', [])
                if registros:
                    return registros
                # MongoDB vazio — tenta arquivo (pode ter dados mais recentes)
            except Exception as e:
                logger.warning(f"Falha ao carregar histórico do MongoDB: {e}")
        if not self.HISTORY_PATH.exists():
            return []
        try:
            with open(self.HISTORY_PATH, 'r', encoding='utf-8') as f:
                history = json.load(f)
            return history.get(mes_chave, [])
        except Exception as e:
            logger.error(f"Erro ao carregar histórico do arquivo: {e}")
            return []

class ComponentConsumptionManager:
    """
    Gerencia o consumo real de insumos/componentes registrado via checklist.
    Reinicia automaticamente todo mês.
    """
    FILE_PATH = DATA_DIR / 'component_consumption.json'

    def __init__(self):
        self.data = self._load()
        self._ensure_current_month()

    def _current_month_key(self):
        return datetime.now().strftime('%Y-%m')

    def _load(self):
        """Carrega consumo — MongoDB primeiro, arquivo como fallback real."""
        if MONGO_AVAILABLE:
            try:
                doc = MongoStore.get('component_consumption', 'main')
                data = doc.get('data', {})
                if data:
                    return data
                logger.info("MongoDB retornou consumo vazio — verificando arquivo local...")
            except Exception as e:
                logger.warning(f"Falha ao carregar consumo do MongoDB: {e}")
        if not self.FILE_PATH.exists():
            return {}
        try:
            with open(self.FILE_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data:
                    logger.info(f"✅ Consumo carregado do arquivo local")
                return data
        except Exception as e:
            logger.error(f"Erro ao carregar consumo do arquivo: {e}")
            return {}

    def _save(self):
        """Salva consumo — MongoDB E arquivo local (dupla redundância)."""
        if MONGO_AVAILABLE:
            try:
                MongoStore.set('component_consumption', {'data': self.data}, 'main')
            except Exception as e:
                logger.error(f"Erro ao salvar consumo no MongoDB: {e}")
        temp = self.FILE_PATH.with_suffix('.tmp')
        try:
            with open(temp, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
            shutil.move(str(temp), str(self.FILE_PATH))
        except Exception as e:
            logger.error(f"Erro ao salvar consumo em arquivo: {e}")

    def _ensure_current_month(self):
        """Garante que existe a estrutura para o mês atual."""
        key = self._current_month_key()
        if key not in self.data:
            self.data[key] = {
                'components': {},   # nome -> {qtd, un, registros: [...]}
                'checklist_logs': []  # Histórico de cada item marcado
            }
            self._save()

    def register_component(self, component_name: str, qty: float, unit: str, product_name: str):
        """Registra uso de um componente via checklist."""
        self._ensure_current_month()
        key = self._current_month_key()
        month_data = self.data[key]

        if component_name not in month_data['components']:
            month_data['components'][component_name] = {'qtd': 0, 'un': unit, 'registros': []}

        comp = month_data['components'][component_name]
        comp['un'] = unit

        # (evita duplicação quando marca-desmarca-remarca)
        existing_idx = next((i for i, r in enumerate(comp['registros']) 
                             if r.get('produto') == product_name), None)
        
        if existing_idx is not None:
            # Já existe: apenas atualiza o timestamp (não soma novamente)
            comp['registros'][existing_idx]['timestamp'] = datetime.now().isoformat()
            # A quantidade já foi somada anteriormente, não somar novamente
        else:
            # Novo registro: soma a quantidade e adiciona
            comp['qtd'] = round(comp['qtd'] + qty, 3)
            registro = {
                'produto': product_name,
                'qtd': qty,
                'timestamp': datetime.now().isoformat()
            }
            comp['registros'].append(registro)
            
            # Log geral (só quando realmente soma)
            month_data['checklist_logs'].append({
                'componente': component_name,
                'produto': product_name,
                'qtd': qty,
                'un': unit,
                'timestamp': datetime.now().isoformat()
            })

        self._save()
        return comp

    def unregister_component(self, component_name: str, qty: float, product_name: str):
        """Remove o consumo de um componente (desmarcou o checkbox)."""
        self._ensure_current_month()
        key = self._current_month_key()
        month_data = self.data[key]

        if component_name in month_data['components']:
            comp = month_data['components'][component_name]
            # (antes removia todos, perdendo histórico de marcações anteriores no mês)
            last_idx = None
            for i in range(len(comp['registros']) - 1, -1, -1):
                if comp['registros'][i].get('produto') == product_name:
                    last_idx = i
                    break
            if last_idx is not None:
                removed_qty = comp['registros'][last_idx].get('qtd', qty)
                comp['registros'].pop(last_idx)
                comp['qtd'] = max(0, round(comp['qtd'] - removed_qty, 3))
            self._save()

    def get_current_month(self):
        """Retorna os dados do mês atual."""
        self._ensure_current_month()
        key = self._current_month_key()
        return self.data[key]

    def get_all_months(self):
        """Retorna histórico de todos os meses."""
        return self.data

    def get_month_summary(self):
        """Resumo formatado para o frontend."""
        month = self.get_current_month()
        summary = []
        for nome, info in month['components'].items():
            todos = info.get('registros', [])
            # Um mesmo produto pode ter múltiplos registros (marca-desmarca-remarca)
            produtos_unicos = len(set(r.get('produto', '') for r in todos))
            summary.append({
                'nome': nome,
                'qtd_total': info['qtd'],
                'un': info['un'],
                'num_registros': max(len(todos), produtos_unicos),
                'registros': todos[-5:]
            })
        return sorted(summary, key=lambda x: x['qtd_total'], reverse=True)

# ── Extração de Base/Cor do nome do produto ──────────────────────────────────

import re as _re_ecb

_BASE_TYPES_ECB = [
    "BASE QUADRADA", "BASE REDONDA", "BASE ESTRELA", "BASE CROMADA",
    "BASE PRETA", "BASE ALUMINIO", "BASE ALUMÍNIO", "BASE FIXA",
    "BASE GIRATORIA", "BASE GIRATÓRIA", "BASE MADEIRA", "BASE INOX",
]
_COR_TYPES_ECB = [
    "COURVIM PRETO","COURVIM BRANCO","COURVIM CARAMELO","COURVIM CINZA",
    "COURVIM AZUL","COURVIM VERDE","COURVIM ROSA","COURVIM VINHO","COURVIM",
    "VELUDO PRETO","VELUDO CINZA","VELUDO AZUL","VELUDO VERDE",
    "VELUDO ROSA","VELUDO BEGE","VELUDO VINHO","VELUDO AMARELO","VELUDO",
    "LINHO BEGE","LINHO CINZA","LINHO PRETO","LINHO BRANCO","LINHO",
    "TECIDO PRETO","TECIDO CINZA","TECIDO BEGE","TECIDO BRANCO","TECIDO",
    "MARSALA","BORDO","BORDÔ","CARAMELO","NUDE","CREME",
    "PRETO","BRANCO","CINZA","BEGE","MARROM",
    "AZUL","VERDE","ROSA","AMARELO","LARANJA","VINHO",
]

def _extract_base_cor(nome: str):
    """Extrai base e cor do nome do produto. Suporta 'Cor:X', ' - ', keywords."""
    if not nome:
        return "", ""
    nome_up = nome.upper()
    base = ""
    cor = ""

    # 1. Padrao "Cor:Marsala" ou "Base:Quadrada" (com ou sem espaco)
    m_cor = _re_ecb.search(r"(?:COR|TECIDO|MATERIAL)\s*:\s*([^\s\-\/,;]+(?:\s+[^\s\-\/,;]+)?)", nome_up)
    if m_cor:
        s = m_cor.start(1)
        cor = nome[s : s + len(m_cor.group(1))].strip()

    m_base = _re_ecb.search(r"BASE\s*:\s*([^\s\-\/,;]+(?:\s+[^\s\-\/,;]+)?)", nome_up)
    if m_base:
        s = m_base.start(1)
        base = nome[s : s + len(m_base.group(1))].strip()

    # 2. Separador " - " ou " / "
    if not base or not cor:
        sep = " - " if " - " in nome else (" / " if " / " in nome else None)
        if sep:
            for parte in [p.strip() for p in nome.split(sep)]:
                pu = parte.upper()
                if not base:
                    for bt in _BASE_TYPES_ECB:
                        if pu.startswith(bt) or pu == bt:
                            base = parte
                            break
                    if not base and pu.startswith("BASE ") and len(parte) < 35:
                        base = parte
                if not cor and parte != base:
                    for ct in _COR_TYPES_ECB:
                        if ct in pu:
                            idx = nome_up.find(ct)
                            cor = nome[idx : idx + len(ct)].strip()
                            break

    # 3. Fallback: busca keywords no nome completo
    if not base:
        for bt in _BASE_TYPES_ECB:
            if bt in nome_up:
                base = bt.title()
                break
    if not cor:
        for ct in _COR_TYPES_ECB:
            if ct in nome_up:
                cor = ct.title()
                break

    return base, cor

class PendingOrdersManager:
    """
    Gerencia pedidos do Bling que chegaram e estão aguardando produção.
    Persiste no MongoDB (principal) ou arquivo (fallback).
    """
    FILE_PATH = DATA_DIR / 'pending_orders.json'

    def __init__(self):
        self.data = self._load()

    def _load(self):
        """Carrega pending_orders — MongoDB primeiro, arquivo fallback.
        Garante que item_key está presente dentro de cada doc."""
        if MONGO_AVAILABLE:
            try:
                data = MongoStore.get_all('pending_orders')
                if data:
                    # Injeta item_key dentro do doc (get_all remove o _id)
                    for key, doc in data.items():
                        if 'item_key' not in doc or not doc['item_key']:
                            doc['item_key'] = key
                    logger.info(f"✅ PendingOrders: {len(data)} itens carregados do MongoDB")
                    return data
                logger.info("MongoDB retornou pending_orders vazio — verificando arquivo local...")
            except Exception as e:
                logger.warning(f"Falha ao carregar pending_orders do MongoDB: {e}")
        if not self.FILE_PATH.exists():
            return {}
        try:
            with open(self.FILE_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data:
                    # Mesma garantia para arquivo
                    for key, doc in data.items():
                        if 'item_key' not in doc or not doc['item_key']:
                            doc['item_key'] = key
                    logger.info(f"✅ PendingOrders: {len(data)} itens carregados do arquivo local")
                return data
        except Exception as e:
            logger.error(f"Erro ao carregar pending_orders do arquivo: {e}")
            return {}

    def _save(self):
        """Salva pending_orders — MongoDB E arquivo local (dupla redundância)."""
        if MONGO_AVAILABLE:
            try:
                for key, val in self.data.items():
                    MongoStore.upsert('pending_orders', key, val)
            except Exception as e:
                logger.error(f"Erro ao salvar pending_orders no MongoDB: {e}")
        temp = self.FILE_PATH.with_suffix('.tmp')
        try:
            with open(temp, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
            shutil.move(str(temp), str(self.FILE_PATH))
        except Exception as e:
            logger.error(f"Erro ao salvar pedidos pendentes em arquivo: {e}")

    def _save_one(self, key: str):
        """Salva apenas um item (mais eficiente que _save completo)."""
        if key not in self.data:
            return
        val = self.data[key]
        if MONGO_AVAILABLE:
            try:
                MongoStore.upsert('pending_orders', key, val)
                return
            except Exception as e:
                logger.error(f"Erro ao salvar item {key} no MongoDB: {e}")
        self._save()

    def add_order_item(self, order_id: str, item_key: str, item_data: dict):
        """Adiciona um item de pedido à fila de espera."""
        key = f"{order_id}_{item_key}"
        if key not in self.data:
            self.data[key] = {
                **item_data,
                'order_id': str(order_id),
                'item_key': item_key,
                'status': 'waiting',
                'added_at': datetime.now().isoformat()
            }
            self._save_one(key)
        return self.data[key]

    def start_production(self, item_key: str):
        """Move item para status 'in_production'."""
        if item_key in self.data:
            self.data[item_key]['status'] = 'in_production'
            self.data[item_key]['started_at'] = datetime.now().isoformat()
            self._save_one(item_key)
        return self.data.get(item_key)

    def finish_production(self, item_key: str):
        """Move item para status 'done' — persiste no MongoDB para não sumir ao reiniciar."""
        if item_key in self.data:
            self.data[item_key]['status'] = 'done'
            self.data[item_key]['finished_at'] = datetime.now().isoformat()
            self.data[item_key]['mes_conclusao'] = datetime.now().strftime('%Y-%m')
            self._save_one(item_key)
        return self.data.get(item_key)

    def dismiss(self, item_key: str):
        """Remove item da fila."""
        if item_key in self.data:
            del self.data[item_key]
            if MONGO_AVAILABLE:
                try:
                    MongoStore.remove('pending_orders', item_key)
                except Exception:
                    pass
            else:
                self._save()

    def get_waiting(self):
        """Retorna todos os itens aguardando produção."""
        return [v for v in self.data.values() if v.get('status') == 'waiting']

    def get_in_production(self):
        """Retorna todos os itens em produção."""
        return [v for v in self.data.values() if v.get('status') == 'in_production']

    def get_done(self):
        """Retorna todos os itens concluídos no mês atual."""
        mes_atual = datetime.now().strftime('%Y-%m')
        result = []
        for v in self.data.values():
            if v.get('status') != 'done':
                continue
            # Filtra pelo mês de conclusão (campo mes_conclusao) ou added_at
            mes = v.get('mes_conclusao') or (v.get('finished_at', '')[:7] if v.get('finished_at') else '')
            if not mes:
                mes = v.get('added_at', '')[:7]
            if mes == mes_atual:
                result.append(v)
        return result

    def get_all(self):
        return list(self.data.values())

    def reset_if_new_month(self):
        """
        Todo início de mês remove itens antigos da fila.
        Regras:
        - 'done': só remove se mes_conclusao (ou finished_at) for de mês anterior
        - 'waiting'/'in_production': remove se added_at for de mês anterior
        Itens 'done' do mês atual são mantidos como histórico visível.
        """
        agora = datetime.now()
        mes_atual = f"{agora.year}-{agora.month:02d}"
        to_remove = []
        for key, item in self.data.items():
            status = item.get('status', 'waiting')
            try:
                if status == 'done':
                    # Para itens concluídos, usa o mês de conclusão
                    mes_ref = item.get('mes_conclusao', '')
                    if not mes_ref:
                        fin = item.get('finished_at', '')
                        mes_ref = fin[:7] if fin else item.get('added_at', '')[:7]
                else:
                    # Para itens em espera/produção, usa quando foi adicionado
                    mes_ref = item.get('added_at', '')[:7]

                if mes_ref and mes_ref != mes_atual:
                    to_remove.append(key)
            except Exception:
                pass

        if to_remove:
            for key in to_remove:
                del self.data[key]
                if MONGO_AVAILABLE:
                    try:
                        MongoStore.remove('pending_orders', key)
                    except Exception:
                        pass
            if not MONGO_AVAILABLE:
                self._save()
            logger.info(f"🗓️ Reset mensal: {len(to_remove)} itens antigos removidos da fila.")
        return len(to_remove)

    def sync_from_orders(self, orders: list, products_cache: dict):
        """
        Sincroniza pedidos do Bling com a fila — apenas pedidos do mês atual.
        Itens concluídos (status=done) são mantidos como histórico mas não re-adicionados.
        """
        added = 0
        agora = datetime.now()
        mes_atual = agora.month
        ano_atual = agora.year

        for pedido in orders:
            order_id = str(pedido.get('id', ''))
            if not order_id:
                continue

            # ── Filtro: apenas pedidos do mês atual ─────────────────────────
            data_str = pedido.get('data') or pedido.get('dataEmissao') or ''
            if data_str:
                try:
                    data_limpa = str(data_str).split(' ')[0].split('T')[0]
                    dt = (datetime.strptime(data_limpa, '%Y-%m-%d') if '-' in data_limpa
                          else datetime.strptime(data_limpa, '%d/%m/%Y'))
                    if dt.month != mes_atual or dt.year != ano_atual:
                        continue  # Ignora pedidos de outros meses
                except Exception:
                    pass  # Se não conseguir parsear a data, deixa passar

            itens = pedido.get('itens', [])
            if not itens:
                continue  # sem itens na listagem — será buscado individualmente

            for idx, item in enumerate(itens):
                nome_raw = (item.get('descricao') or item.get('nome') or '').strip()
                sku_raw = (item.get('codigo') or item.get('sku') or '').strip()
                if not nome_raw and not sku_raw:
                    continue
                qtd = max(1, int(float(item.get('quantidade', 1))))

                # Correlaciona com cache de produtos
                produto_cache = (products_cache.get(sku_raw)
                                 or products_cache.get(sku_raw.upper())
                                 or products_cache.get(nome_raw.upper()))
                nome_produto = produto_cache['nome'] if produto_cache else nome_raw
                _img_raw = (produto_cache or {}).get('imagem', '')
                imagem = '' if (not _img_raw or 'no-image' in str(_img_raw)) else _img_raw

                # Extrai base/cor — tenta nome completo, fallback para nome original do item
                base, cor = _extract_base_cor(nome_produto)
                if not base and not cor:
                    base, cor = _extract_base_cor(nome_raw)

                cliente = ''
                contato = pedido.get('contato')
                if isinstance(contato, dict):
                    cliente = contato.get('nome', '') or contato.get('nomeFantasia', '')

                item_data = {
                    'nome': nome_produto,
                    'nome_original': nome_raw,
                    'sku': sku_raw,
                    'base': base,
                    'cor': cor,
                    'imagem': imagem,
                    'pedido_data': pedido.get('data') or pedido.get('dataEmissao', ''),
                    'pedido_numero': pedido.get('numero', order_id),
                    'cliente': cliente,
                }

                for unit in range(qtd):
                    # Chave estável: order_id + SKU + posição (não índice do loop externo)
                    sku_safe = (sku_raw or nome_raw[:20]).replace(' ', '_').replace('/', '_')
                    sub_key = f"{order_id}_{sku_safe}_{unit}"
                    # Evita duplicar itens já existentes em qualquer status
                    already = any(
                        v.get('order_id') == str(order_id)
                        and v.get('sku') == sku_raw
                        and v.get('qtd_unit_idx', unit) == unit
                        for v in self.data.values()
                    )
                    if sub_key not in self.data and not already:
                        self.data[sub_key] = {
                            **item_data,
                            'qtd': 1,
                            'order_id': order_id,
                            'item_key': sub_key,
                            'qtd_unit_idx': unit,
                            'status': 'waiting',
                            'added_at': datetime.now().isoformat()
                        }
                        self._save_one(sub_key)
                        added += 1

        if added > 0:
            if not MONGO_AVAILABLE:
                self._save()
            logger.info(f"✅ PendingOrders: {added} novos itens adicionados.")
        return added

# Instâncias globais
production_timer = ProductionTimer()
component_consumption = ComponentConsumptionManager()
pending_orders = PendingOrdersManager()
# Reset mensal ao iniciar — remove itens antigos concluídos/em espera
try:
    _removed = pending_orders.reset_if_new_month()
    if _removed:
        logger.info(f"♻️ Início: {_removed} itens antigos removidos da fila de produção.")
except Exception as _e:
    logger.warning(f"reset_if_new_month falhou: {_e}")

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
        self._cache_lock = Lock()          # ← criado ANTES de _load_cache (evita AttributeError)
        self._component_usage_cache = None
        self._load_cache()

        # Carrega cache em background — não bloqueia o boot do Flask
        if self.auth._access_token and self.auth._expires_at > __import__('time').time() + 60:
            self.logger.info("📦 Agendando cache inicial de produtos em background...")
            Thread(target=self.process_products_cache, daemon=True, name="cache_init").start()
        else:
            self.logger.info("⏳ Cache de produtos adiado — aguardando autenticação OAuth")

    def _load_cache(self):
        """Carrega o cache de produtos/kits do disco."""
        data = load_products_cache(self.config.PRODUCTS_CACHE_FILE)
        if data:
            with self._cache_lock:
                self._products_cache = {p['id']: p for p in safe_iter(data.get('products'))}
                self._kits_cache = {k['id']: k for k in safe_iter(data.get('kits'))}
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
            self._stop_event = Event()   # sinaliza parada definitiva
            self._wake_event = Event()   # sinaliza "acorda agora" sem parar
            self._worker_thread = Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()
            self.logger.info("Worker de fundo iniciado.")

    def stop_worker(self):
        """Para o worker de fundo."""
        self._running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._stop_event.set()
            # Acorda o worker se estiver em sleep para processar o stop
            if hasattr(self, '_wake_event'):
                self._wake_event.set()
            self._worker_thread.join(timeout=5)
            if self._worker_thread.is_alive():
                self.logger.warning("Worker de fundo não parou em 5s.")
            else:
                self.logger.info("Worker de fundo parado com sucesso.")

    def wake_worker(self):
        """Acorda o worker imediatamente se estiver dormindo (sem parar o loop)."""
        if self._running and hasattr(self, '_wake_event'):
            self._wake_event.set()
            logger.info("⏰ Worker acordado para processar imediatamente.")
        else:
            logger.debug("⚠️ wake_worker: worker não está rodando.")

    def is_running(self) -> bool:
        """Verifica se o worker está ativo."""
        return self._running

    def _worker_loop(self):
        cycle_count = 0
        logger.info("🔄 Worker loop iniciado.")

        while not self._stop_event.is_set():
            cycle_count += 1

            # ── Verifica autenticação ────────────────────────────────────
            if not (self.auth._access_token and self.auth._expires_at > time.time() + 60):
                # access_token expirou — tenta refresh antes de desistir
                self.auth.reload_tokens_from_disk()
                if not self.auth.is_authenticated():
                    logger.info(f"⏸ Ciclo #{cycle_count}: sem token válido — aguardando...")
                    self._wake_event.wait(60)
                    self._wake_event.clear()
                    continue
                logger.info(f"🔑 Ciclo #{cycle_count}: token renovado — continuando.")

            # ── Processamento ─────────────────────────────────────────────
            try:
                if cycle_count == 1 or cycle_count % 3 == 0:
                    logger.info(f"🔄 Ciclo #{cycle_count}: atualizando cache de produtos...")
                    self.process_products_cache()

                logger.info(f"🔄 Ciclo #{cycle_count}: atualizando pedidos/KPIs...")
                self.process_sales_orders()

                if cycle_count % 2 == 0:
                    logger.info(f"🔄 Ciclo #{cycle_count}: calculando componentes...")
                    usage = self.calculate_component_usage()
                    if usage.get('components'):
                        self._component_usage_cache = usage
                        self.broadcast_kpi_update(component_usage=usage)

            except Exception:
                logger.exception(f"❌ Erro fatal no ciclo #{cycle_count}")

            logger.info(f"✅ Ciclo #{cycle_count} finalizado. Próximo em 10min.")

            # Dorme 600s mas acorda se wake_event for setado
            self._wake_event.wait(600)
            self._wake_event.clear()

    def process_sales_orders(self, force: bool = False):
        """Busca pedidos de venda e atualiza o Sales Manager (Versão Híbrida V2/V3)."""
        self.logger.debug(f"process_sales_orders chamado (force={force})")
        
        # Evita recálculos encavalados
        with self.sales.recalculation_lock:
            if self.sales._recalculation_running and not force:
                self.logger.debug("Recálculo já em execução, ignorando.")
                return
            self.sales._recalculation_running = True
            
        try:
            if not self.auth.is_authenticated():
                self.logger.warning("⛔ Worker: token inexistente. Abortando.")
                return
                
            now = datetime.now()
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            self.logger.info(f"Buscando pedidos: {start_date.strftime('%d/%m/%Y')} → hoje")
            
            # Parâmetros compatíveis
            # Busca Janela Móvel (Últimos 30 dias)
            params = {
                'dataEmissaoInicial': start_date.strftime('%Y-%m-%d'),
                'dataEmissaoFinal': now.strftime('%Y-%m-%d %H:%M:%S'),
                'situacao': 'F', # Faturado. Mude para None ou remova se quiser todos os status.
                'limite': 100 
            }
            
            all_orders = []
            page = 1
            
            while True:
                params['pagina'] = page
                self.logger.debug(f"Buscando página {page} de pedidos...")
                try:
                    response = self.api.get('pedidos/vendas', params=params)
                except Exception as e:
                    self.logger.error(f"Erro na API ao buscar pedidos: {e}")
                    break # Se der erro na API, para o loop mas processa o que já pegou
                
                if response is None:
                    self.logger.debug(f"Resposta da API nula na página {page}")
                    break
    
                data = []
                if isinstance(response, dict):
                    # Formato V3 Padrão
                    if 'data' in response:
                        data = response['data']
                    # Formato Legado / Webhook antigo
                    elif 'retorno' in response and 'pedidos' in response['retorno']:
                        data = response['retorno']['pedidos']
                        # Normaliza lista antiga se necessário
                        if data and isinstance(data[0], dict) and 'pedido' in data[0]:
                            data = [d['pedido'] for d in data]
                elif isinstance(response, list):
                    # Se o Bling retornar a lista direta
                    data = response
                # -------------------------------------
                
                self.logger.debug(f"Página {page} retornou {len(data) if data else 0} pedidos.")
                
                if not data:
                    break
                
                all_orders.extend(data)
                
                # Se vier menos que 100, é a última página
                if len(data) < 100:
                    break
    
                page += 1
                time.sleep(0.5) # Respeita o rate limit do Bling
    
            # Só recalcula se achou pedidos
            if all_orders:
                self.logger.info(f"Processando {len(all_orders)} pedidos para o Dashboard.")
                # Filtra pedidos válidos (tem que ter ID e Data)
                valid_orders = []
                for o in all_orders:
                    # Garante que temos uma data válida, verificando vários campos
                    data_pedido = o.get('data') or o.get('dataEmissao') or o.get('dataSaida')
                    
                    if not data_pedido:
                        continue # Pula pedido sem data
                        
                    o['data'] = data_pedido # Padroniza para 'data'
                    
                    if o.get('id'):
                        valid_orders.append(o)

                self.logger.debug(f"{len(valid_orders)} pedidos válidos após normalização inicial.")
                # 1. Mescla pedidos novos com histórico existente (por ID)
                #    Não substitui para não perder pedidos de ciclos anteriores
                existing_ids = {o.get('id') for o in self.sales._sales_history if o.get('id')}
                for o in valid_orders:
                    if o.get('id') and o['id'] not in existing_ids:
                        self.sales._sales_history.append(o)
                        existing_ids.add(o['id'])
                    elif o.get('id'):
                        # Atualiza o pedido existente (pode ter mudado de situação)
                        for i, ex in enumerate(self.sales._sales_history):
                            if ex.get('id') == o['id']:
                                self.sales._sales_history[i] = o
                                break
                # Limita a 2000 pedidos mais recentes para não crescer infinitamente
                if len(self.sales._sales_history) > 2000:
                    self.sales._sales_history = self.sales._sales_history[-2000:]
                
                # 2. Recalcula as estatísticas
                self.sales.recalculate_from_orders(self.sales._sales_history)
                
                # 3. Sincroniza pedidos com fila de produção pendente
                try:
                    with self._cache_lock:
                        cache_flat = {**self._products_cache, **self._kits_cache}
                    # Tenta sync direto (funciona se itens vierem na listagem)
                    added = pending_orders.sync_from_orders(valid_orders, cache_flat)
                    # Se nenhum item foi adicionado e há pedidos, busca detalhes individuais
                    if added == 0 and valid_orders:
                        self.logger.info("⚠️ Itens não vieram na listagem. Buscando pedidos individualmente...")
                        Thread(target=self._fetch_orders_with_items, args=(valid_orders, cache_flat), daemon=True).start()
                except Exception as e:
                    self.logger.warning(f"Erro ao sincronizar pending_orders: {e}")
                
                # Manda atualização pro Front (Gráfico)
                self.broadcast_kpi_update(sales_stats=self.sales._get_state_for_save(), cache_updated=False)
            else:
                self.logger.warning("Nenhum pedido encontrado na busca.")

        except Exception as e:
            self.logger.exception(f"Erro fatal no processamento de pedidos: {e}")
        finally:
            with self.sales.recalculation_lock:
                self.sales._recalculation_running = False

    def process_products_cache(self):
        """Busca e armazena em cache todos os produtos, variações e kits com tratamento de imagem V3."""
        if not self.auth.is_authenticated():
            return
            
        self.logger.info("Iniciando busca profunda de produtos, variações e kits...")
        all_products = []
        all_kits = []
        page = 1
        
        while True:
            try:
                # Busca produtos incluindo imagens e variações
                response = self.api.get('produtos', params={'pagina': page, 'limite': 100})
                if not response: break
            except Exception as e:
                self.logger.error(f"Erro ao buscar produtos: {e}")
                break
            
            data = safe_get(response, 'data', [])
            if not data: break

            for p in data:
                p_id = p.get("id")
                if not p_id: continue

                img_url = "/static/no-image.png" # Placeholder padrão
                imagens = p.get("imagens", [])
                if isinstance(imagens, list) and len(imagens) > 0:
                    img_url = imagens[0].get("link") # Pega a primeira imagem da lista
                elif p.get("imagemURL"):
                    img_url = p.get("imagemURL")
                # -------------------------------------------------

                sku_val = p.get("codigo") or p.get("sku") or str(p_id)
                
                estoque = p.get("estoque", {})
                saldo = 0
                if isinstance(estoque, dict):
                    saldo = estoque.get("saldoVirtual") or estoque.get("saldo") or 0
                else:
                    saldo = estoque or 0

                produto_normalizado = {
                    "id": p_id,
                    "nome": p.get("nome"),
                    "sku": sku_val,
                    "estoqueAtual": saldo,
                    "imagem": img_url, # Usa a URL tratada
                    "tipo": p.get("tipo", "P"),
                    "componentes": []
                }

                if produto_normalizado["tipo"] == "K":
                    try:
                        detalhe = self.api.get(f'produtos/{p_id}')
                        if detalhe and 'data' in detalhe:
                            comp_data = detalhe['data'].get('estrutura', {}).get('componentes', [])
                            produto_normalizado["componentes"] = comp_data
                    except Exception as _kit_err:
                        logger.warning(f"Erro ao buscar componentes do kit {p_id}: {_kit_err}")
                    all_kits.append(produto_normalizado)
                else:
                    all_products.append(produto_normalizado)

                # Processamento de Variações
                variacoes = p.get("variacoes", [])
                if variacoes:
                    for v in variacoes:
                        v_id = v.get("id")
                        v_sku = v.get("codigo") or v.get("sku")
                        if not v_id or not v_sku: continue
                        
                        v_estoque = v.get("estoque", {})
                        v_saldo = 0
                        if isinstance(v_estoque, dict):
                            v_saldo = v_estoque.get("saldoVirtual") or v_estoque.get("saldo") or 0
                        else:
                            v_saldo = v_estoque or 0

                        # Tenta pegar imagem da variação, se não tiver, usa a do pai
                        v_img_url = produto_normalizado["imagem"]
                        v_imagens = v.get("imagens", [])
                        if isinstance(v_imagens, list) and len(v_imagens) > 0:
                            v_img_url = v_imagens[0].get("link")

                        var_normalizada = {
                            "id": v_id,
                            "nome": f"{p.get('nome')} - {v.get('nome', '')}".strip(),
                            "sku": v_sku,
                            "estoqueAtual": v_saldo,
                            "imagem": v_img_url,
                            "tipo": "P",
                            "pai_id": p_id
                        }
                        all_products.append(var_normalizada)

            if len(data) < 100: break
            page += 1
            time.sleep(0.5)

        with self._cache_lock:
            self._products_cache = {str(p["id"]): p for p in all_products}
            # Adiciona também busca por SKU
            for p in all_products: self._products_cache[str(p["sku"])] = p
            
            self._kits_cache = {str(k["id"]): k for k in all_kits}
            for k in all_kits: self._kits_cache[str(k["sku"])] = k
            
            save_products_cache(self.config.PRODUCTS_CACHE_FILE, all_products, all_kits)
            
        self.logger.info(f"✅ Cache atualizado: {len(all_products)} produtos, {len(all_kits)} kits.")
        self.broadcast_kpi_update(cache_updated=True)

    def _fetch_orders_with_items(self, orders: list, cache_flat: dict):
        """
        Busca detalhes individuais de cada pedido para obter os itens.
        A API Bling V3 na listagem não retorna itens — só no endpoint individual.
        Chamado em thread separada para não bloquear o worker principal.
        """
        # Todos os order_ids já presentes (qualquer status) — evita re-buscar e duplicar
        already_have = {v.get('order_id') for v in pending_orders.data.values()}
        agora_fetch = datetime.now()
        orders_mes = []
        for o in orders:
            data_str = o.get('data') or o.get('dataEmissao') or ''
            if data_str:
                try:
                    dl = str(data_str).split(' ')[0].split('T')[0]
                    dt = (datetime.strptime(dl, '%Y-%m-%d') if '-' in dl
                          else datetime.strptime(dl, '%d/%m/%Y'))
                    if dt.month == agora_fetch.month and dt.year == agora_fetch.year:
                        orders_mes.append(o)
                except Exception:
                    orders_mes.append(o)
            else:
                orders_mes.append(o)
        orders_to_fetch = [o for o in orders_mes if str(o.get('id', '')) not in already_have]

        if not orders_to_fetch:
            self.logger.info("✅ Todos os pedidos já estão na fila de pendentes.")
            return

        self.logger.info(f"🔍 Buscando itens de {len(orders_to_fetch)} pedidos individualmente...")
        enriched = []

        for pedido in orders_to_fetch:
            order_id = str(pedido.get('id', ''))
            if not order_id:
                continue
            try:
                resp = self.api.get(f'pedidos/vendas/{order_id}')
                if not resp:
                    continue
                detail = resp.get('data', resp)
                # Mantém campos do pedido original e adiciona itens do detalhe
                merged = {**pedido, 'itens': detail.get('itens', [])}
                if merged['itens']:
                    enriched.append(merged)
                    self.logger.debug(f"  Pedido {order_id}: {len(merged['itens'])} itens encontrados")
                time.sleep(0.4)  # respeita rate limit
            except Exception as e:
                self.logger.error(f"Erro ao buscar pedido {order_id}: {e}")
                continue

        if enriched:
            added = pending_orders.sync_from_orders(enriched, cache_flat)
            self.logger.info(f"✅ {added} itens adicionados à fila de espera após busca individual.")
        else:
            self.logger.warning("⚠️ Nenhum item encontrado nos pedidos individuais.")

    def calculate_component_usage(self) -> Dict[str, Any]:
        """Calcula insumos com alta performance e logs de diagnóstico."""
        start_calc = time.time()
        try:
            agora = datetime.now()
            mes_atual = agora.month
            ano_atual = agora.year
            
            insumos_teoricos = defaultdict(float)
            insumos_reais = defaultdict(float)
            produtos_vendidos = defaultdict(int)
            produtos_produzidos = defaultdict(int)
            
            # 1. PROCESSAMENTO DE VENDAS (Otimizado)
            todos_pedidos = []
            if hasattr(self, 'sales') and self.sales:
                with self.sales.lock:
                    # Pegamos apenas os últimos 500 pedidos para não travar o sistema
                    todos_pedidos = list(self.sales._sales_history or [])[-500:]

            for pedido in todos_pedidos:
                data_str = pedido.get('data')
                if not data_str: continue

                try:
                    # Robusto: suporta '2025-02-19', '2025-02-19 10:00', '2025-02-19T10:00'
                    data_limpa = str(data_str).split(' ')[0].split('T')[0]

                    if '-' in data_limpa:
                        dt_pedido = datetime.strptime(data_limpa, "%Y-%m-%d")
                    else:
                        dt_pedido = datetime.strptime(data_limpa, "%d/%m/%Y")

                    if dt_pedido.month != mes_atual or dt_pedido.year != ano_atual:
                        continue

                    for item in pedido.get('itens', []):
                        nome = (item.get('descricao') or item.get('nome') or "").upper()
                        qtd = float(item.get('quantidade', 0))

                        if qtd > 0:
                            produtos_vendidos[nome] += int(qtd)  # Somatório por quantidade real
                            if "CADEIRA" in nome:
                                for comp in RECIPE_CADEIRA:
                                    insumos_teoricos[comp['nome']] += (comp['qtd'] * qtd)
                except Exception as e:
                    self.logger.debug(f'Erro ao ler data do pedido: {e} - Dado bruto: {data_str}')
                    continue

            # 2. PROCESSAMENTO DE PRODUÇÃO (TIMER)
            historico_producao = production_timer.get_monthly_history_details()
            tempo_total_mes = 0

            for registro in historico_producao:
                nome_prod = registro.get('produto', '').upper()
                tempo = registro.get('tempo_segundos', 0)
                tempo_total_mes += tempo
                produtos_produzidos[nome_prod] += 1
                
                if "CADEIRA" in nome_prod:
                    for comp in RECIPE_CADEIRA:
                        insumos_reais[comp['nome']] += comp['qtd']

            self.logger.debug(f"⏱️ Cálculo de componentes finalizado em {time.time() - start_calc:.2f}s")

            return {
                "components": self._format_components_list(insumos_teoricos, insumos_reais),
                "produtos_vendidos": dict(produtos_vendidos),
                "produtos_produzidos": dict(produtos_produzidos),
                "active_production": production_timer.get_active_timers(),
                "history_production": historico_producao,
                "total_horas_mes": round(tempo_total_mes / 3600, 2)
            }
        except Exception as e:
            self.logger.error(f"❌ Erro no cálculo: {e}")
            return {"error": str(e)}

    def _format_components_list(self, teoricos, reais):
        """Auxiliar para formatar a lista final de componentes."""
        nomes = set(list(teoricos.keys()) + list(reais.keys()))
        lista = []
        for nome in nomes:
            un = next((r['un'] for r in RECIPE_CADEIRA if r['nome'] == nome), "un")
            lista.append({
                "nome": nome,
                "qtd_teorica": round(teoricos[nome], 2),
                "qtd_real": round(reais[nome], 2),
                "un": un
            })
        return sorted(lista, key=lambda x: x['qtd_real'], reverse=True)

    def broadcast_kpi_update(self, sales_stats: Optional[Dict[str, Any]] = None, cache_updated: bool = False, component_usage: Optional[Dict[str, Any]] = None, auth_error: bool = False):
        """
        Envia uma atualização completa de status via WebSocket para todos os clientes.
        Inclui status de autenticação, KPIs e, se solicitado, uso de componentes.
        """
        global kpi_update_callbacks, kpi_update_lock
        
        # Verifica auth sem disparar refresh (operação lenta que bloquearia o broadcast)
        import time as _t
        auth_ok = bool(self.auth._access_token and self.auth._expires_at > _t.time() + 60)
        payload = {
            "type": "full_update",
            "authenticated": auth_ok and not auth_error,
            "auth_error": auth_error,
            "is_running": self.is_running(),
            "cache_updated": cache_updated,
            "auth_url": self.auth.get_authorization_url()
        }
        
        # 2. Adiciona KPIs se fornecidos (com proteção contra tipos inválidos)
        if sales_stats and isinstance(sales_stats, dict):
            try:
                # Converte a data de volta para ISO string para o WS (se já não for string)
                stats_data = sales_stats.copy()
                last_recalc = stats_data.get('last_recalculated')
                
                if isinstance(last_recalc, datetime):
                    stats_data['last_update'] = last_recalc.isoformat()
                else:
                    stats_data['last_update'] = str(last_recalc)
                    
                if 'last_recalculated' in stats_data:
                    stats_data.pop('last_recalculated')
                    
                payload["sales_stats"] = stats_data
            except Exception as e:
                self.logger.error(f"Erro ao processar sales_stats para broadcast: {e}")
        elif sales_stats:
            self.logger.warning(f"sales_stats recebido em formato inválido ({type(sales_stats)}). Ignorando no broadcast.")
            
        # 3. Adiciona o uso de componentes se fornecido
        if component_usage:
            payload["component_usage"] = component_usage
            self.logger.debug("Uso de componentes incluído no broadcast.")

        # 3.1 Adiciona lista de produtos se o cache foi atualizado
        if cache_updated:
            payload["products"] = self.get_all_products()
            payload["kits"] = self.get_all_kits()
                
        # 4. Copia a lista com lock, envia sem lock (evita segurar lock durante I/O de rede)
        with kpi_update_lock:
            callbacks_snapshot = list(kpi_update_callbacks)

        dead = []
        for cb in callbacks_snapshot:
            try:
                cb(payload)
            except ConnectionClosed:
                dead.append(cb)
            except Exception:
                self.logger.exception("Erro ao enviar full_update via callback.")
                dead.append(cb)

        # Remove callbacks mortos
        if dead:
            with kpi_update_lock:
                for cb in dead:
                    if cb in kpi_update_callbacks:
                        kpi_update_callbacks.remove(cb)

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

        # Rota /api/webhook mantida como alias para /webhook (retrocompatibilidade)
        @self.app.route('/api/webhook', methods=['POST'])
        def api_webhook():
            """Alias de /webhook para retrocompatibilidade."""
            # Redireciona internamente para o handler principal com validação completa
            return redirect('/webhook', code=307)

        @self.app.route("/api/orders")
        @token_required
        def list_orders(token):
            return jsonify(list(self.orchestrator.sales._orders_cache.values()))

        # Novo Endpoint: Histórico de Vendas para Dashboard
        @self.app.route("/api/sales/history")
        @token_required
        def api_sales_history(token):
            stats = self.orchestrator.sales.stats_history
            if not stats or not stats.get('dates'):
                if not self.orchestrator.sales.daily_count:
                     Thread(target=self.orchestrator.process_sales_orders, daemon=True).start()
                return jsonify({"labels": [], "daily": [], "moving_avg": [], "growth": 0, "avg_daily": 0})
            return jsonify({
                "labels": stats.get('dates', []),
                "daily": stats.get('daily', []),
                "moving_avg": stats.get('moving_avg', []),
                "growth": stats.get('growth', 0),
                "avg_daily": stats.get('avg_daily', 0)
            })

        @self.app.route('/api/recalculate', methods=['POST'])
        @token_required
        def api_recalculate(token):
            """Força o recálculo dos KPIs em uma thread separada."""
            
            # Verifica e marca o estado de recalculação dentro do lock
            # Não setar _recalculation_running aqui: process_sales_orders já faz isso
            # Setar aqui causaria deadlock: process_sales_orders veria True e retornaria sem executar
            with self.orchestrator.sales.recalculation_lock:
                if self.orchestrator.sales._recalculation_running:
                    return jsonify({"status": "already_running", "message": "Recálculo já em andamento."}), 202

            Thread(target=self.orchestrator.process_sales_orders, kwargs={'force': True}, daemon=True).start()
            return jsonify({"status": "started", "message": "Recálculo iniciado em segundo plano."}), 202

        @self.app.route('/api/timer/action', methods=['POST'])
        @token_required
        def api_timer_action(token):
            data = request.json or {}
            action  = data.get('action', '').strip()
            produto = data.get('produto', '').strip()

            if not action or not produto:
                return jsonify({'error': 'action e produto são obrigatórios'}), 400
            if action not in ('start', 'pause', 'reset', 'finish', 'get'):
                return jsonify({'error': f'action inválida: {action}'}), 400

            if action == 'start':
                status = production_timer.start(produto)
            elif action == 'pause':
                status = production_timer.pause(produto)
            elif action == 'reset':
                status = production_timer.reset(produto)
            elif action == 'finish':
                status = production_timer.stop_and_log(produto)
            else:
                status = production_timer.get_status(produto)
                
            # Força recálculo e notifica TODOS os usuários via WebSocket
            def update_and_broadcast():
                try:
                    usage = self.orchestrator.calculate_component_usage()
                    self.orchestrator._component_usage_cache = usage
                    self.orchestrator.broadcast_kpi_update(component_usage=usage)
                except Exception as e:
                    self.logger.error(f'Erro no broadcast pós-timer: {e}')
            Thread(target=update_and_broadcast, daemon=True).start()
                
            return jsonify(status)

        @self.app.route('/api/production/board')
        @token_required
        def api_production_board(token):
            """
            Retorna snapshot completo da aba de produção.
            - waiting: pedidos do Bling aguardando alguém clicar em Produzir
            - in_production: pedidos em andamento + tempo ao vivo do timer
            - done: concluídos do mês (para histórico)
            - timers_orphan: timers sem item_key (iniciados manualmente)
            """
            timers = production_timer.timers

            def _timer_info(t):
                total = t.get('accumulated', 0)
                if t.get('state') == 'running' and t.get('start_ts', 0) > 0:
                    total += time.time() - t['start_ts']
                return {
                    'estado': t.get('state', 'paused'),
                    'tempo_decorrido': int(total),
                    'checklist': t.get('checklist', {}),
                    'created_at': t.get('created_at', ''),
                }

            # Mapa timer_key -> timer info
            # Suporta tanto "produto||item_key" (novo) quanto "produto" (legado)
            timer_map_by_key = {}   # item_key -> timer_info (lookup por item_key)
            timer_map_by_nome = {}  # nome -> timer_info (fallback legado)
            for tkey, t in timers.items():
                info = _timer_info(t)
                if '||' in tkey:
                    # Novo formato: "produto_nome||item_key"
                    parts = tkey.split('||', 1)
                    timer_map_by_key[parts[1]] = {**info, 'timer_key': tkey}
                    timer_map_by_nome[parts[0]] = {**info, 'timer_key': tkey}
                else:
                    # Legado: chave é nome do produto
                    timer_map_by_nome[tkey] = {**info, 'timer_key': tkey}

            # Enriquece in_production com dados do timer correto
            in_prod = []
            for item in pending_orders.get_in_production():
                ikey = item.get('item_key', '')
                nome = item.get('nome') or item.get('nome_original', '')
                # Tenta primeiro pelo item_key único, depois por nome (legado)
                t_info = timer_map_by_key.get(ikey) or timer_map_by_nome.get(nome) or {}
                enriched = {**item, **t_info}
                if not enriched.get('cor') and not enriched.get('base'):
                    nome_raw = enriched.get('nome_original') or nome or ''
                    base_r, cor_r = _extract_base_cor(nome)
                    if not base_r and not cor_r:
                        base_r, cor_r = _extract_base_cor(nome_raw)
                    if base_r: enriched['base'] = base_r
                    if cor_r: enriched['cor'] = cor_r
                in_prod.append(enriched)

            # Timers sem pedido vinculado (iniciados via modal diretamente)
            ikeys_com_pedido = {v.get('item_key', '') for v in pending_orders.data.values()}
            nomes_com_pedido = {(v.get('nome') or v.get('nome_original', '')) for v in pending_orders.data.values()}
            orphan = []
            for tkey, t in timers.items():
                # Verifica se está vinculado a algum pedido
                if '||' in tkey:
                    ikey_part = tkey.split('||', 1)[1]
                    if ikey_part in ikeys_com_pedido:
                        continue  # Já está em in_prod
                    nome_display = tkey.split('||', 1)[0]
                else:
                    if tkey in nomes_com_pedido:
                        continue
                    nome_display = tkey
                info = _timer_info(t)
                orphan.append({
                    'nome': nome_display,
                    'estado': info['estado'],
                    'tempo_decorrido': info['tempo_decorrido'],
                    'checklist': info['checklist'],
                    'created_at': info['created_at'],
                    'item_key': None,
                    'timer_key': tkey,
                })

            waiting_enriched = []
            for item in pending_orders.get_waiting():
                enriched = dict(item)
                if not enriched.get('cor') and not enriched.get('base'):
                    nome = enriched.get('nome') or enriched.get('nome_original', '')
                    nome_raw = enriched.get('nome_original', '')
                    base_r, cor_r = _extract_base_cor(nome)
                    if not base_r and not cor_r:
                        base_r, cor_r = _extract_base_cor(nome_raw)
                    if base_r: enriched['base'] = base_r
                    if cor_r: enriched['cor'] = cor_r
                waiting_enriched.append(enriched)

            return jsonify({
                'waiting': waiting_enriched,
                'in_production': in_prod,
                'orphan_timers': orphan,
                'done': pending_orders.get_done(),
                'server_time': time.time(),
            })

        @self.app.route('/api/checklist/state/<path:produto>', methods=['GET'])
        @token_required
        def api_checklist_get(token, produto):
            """Retorna estado salvo da checklist de um produto em produção."""
            t = production_timer.timers.get(produto, {})
            return jsonify({'checklist': t.get('checklist', {})})

        @self.app.route('/api/checklist/state', methods=['POST'])
        @token_required
        def api_checklist_set(token):
            """Salva estado de um item da checklist no servidor (persiste)."""
            data = request.json
            produto = data.get('produto', '')
            componente = data.get('componente', '')
            checked = data.get('checked', False)
            if produto and componente:
                # Antes bloqueava silenciosamente, causando 0 registros de consumo
                if produto not in production_timer.timers:
                    production_timer.timers[produto] = {
                        'start_ts': 0,
                        'accumulated': 0,
                        'state': 'paused',
                        'created_at': datetime.now().isoformat(),
                        'checklist': {}
                    }
                if 'checklist' not in production_timer.timers[produto]:
                    production_timer.timers[produto]['checklist'] = {}
                production_timer.timers[produto]['checklist'][componente] = checked
                production_timer._save()
                logger.debug(f"Checklist salvo: produto={produto} comp={componente} checked={checked}")
            return jsonify({'ok': True})

        @self.app.route('/api/consumption/register', methods=['POST'])
        @token_required
        def api_consumption_register(token):
            """Registra ou remove consumo de componente via checklist."""
            data = request.json
            component_name = data.get('component_name', '')
            qty = float(data.get('qty', 0))
            unit = data.get('unit', 'un')
            product_name = data.get('product_name', '')
            checked = data.get('checked', True)  # True = marcou, False = desmarcou

            if not component_name or not product_name:
                return jsonify({'error': 'component_name e product_name são obrigatórios'}), 400

            if checked:
                result = component_consumption.register_component(component_name, qty, unit, product_name)
            else:
                component_consumption.unregister_component(component_name, qty, product_name)
                result = {'unregistered': True}

            def update_and_broadcast():
                try:
                    usage = self.orchestrator.calculate_component_usage()
                    self.orchestrator._component_usage_cache = usage
                    self.orchestrator.broadcast_kpi_update(component_usage=usage)
                except Exception as e:
                    self.logger.error(f'Erro no broadcast pós-consumo: {e}')
            Thread(target=update_and_broadcast, daemon=True).start()

            return jsonify({'success': True, 'result': result})

        @self.app.route('/api/consumption/summary')
        @token_required
        def api_consumption_summary(token):
            """Retorna o resumo de consumo do mês atual."""
            return jsonify({
                'month': component_consumption._current_month_key(),
                'summary': component_consumption.get_month_summary(),
                'logs': component_consumption.get_current_month().get('checklist_logs', [])[-50:]
            })

        @self.app.route('/api/consumption/history')
        def api_consumption_history():
            """Retorna histórico de todos os meses."""
            all_data = component_consumption.get_all_months()
            result = {}
            for month_key, month_data in all_data.items():
                result[month_key] = {
                    'total_components': len(month_data.get('components', {})),
                    'total_logs': len(month_data.get('checklist_logs', [])),
                    'components': [
                        {'nome': k, 'qtd': v['qtd'], 'un': v['un']}
                        for k, v in month_data.get('components', {}).items()
                    ]
                }
            return jsonify(result)

        # =====================================================================
        # ROTAS: PEDIDOS PENDENTES (FILA DE PRODUÇÃO)
        # =====================================================================

        @self.app.route('/api/pending-orders')
        @token_required
        def api_pending_orders(token):
            """Retorna pedidos: aguardando, em produção e concluídos do mês."""
            return jsonify({
                'waiting': pending_orders.get_waiting(),
                'in_production': pending_orders.get_in_production(),
                'done': pending_orders.get_done(),
                'all': pending_orders.get_all(),
                'counts': {
                    'waiting': len(pending_orders.get_waiting()),
                    'in_production': len(pending_orders.get_in_production()),
                    'done': len(pending_orders.get_done()),
                }
            })

        @self.app.route('/api/pending-orders/start', methods=['POST'])
        @token_required
        def api_pending_orders_start(token):
            """Move pedido de 'Em Espera' para 'Em Produção' e inicia timer."""
            data = request.json
            item_key = data.get('item_key', '')
            produto_nome = data.get('produto_nome', '')
            if not item_key:
                return jsonify({'error': 'item_key obrigatório'}), 400
            item = pending_orders.start_production(item_key)
            timer_key = None
            if produto_nome:
                timer_key = f"{produto_nome}||{item_key}"
                production_timer.start(timer_key)
                if item_key in pending_orders.data:
                    pending_orders.data[item_key]['timer_key'] = timer_key
                    pending_orders._save_one(item_key)
            return jsonify({'success': True, 'item': item, 'timer_key': timer_key})

        @self.app.route('/api/pending-orders/finish', methods=['POST'])
        @token_required
        def api_pending_orders_finish(token):
            """Finaliza produção de um pedido pendente."""
            data = request.json
            item_key = data.get('item_key', '')
            produto_nome = data.get('produto_nome', '')
            if not item_key:
                return jsonify({'error': 'item_key obrigatório'}), 400
            item_data = pending_orders.data.get(item_key, {})
            timer_key = item_data.get('timer_key') or (f"{produto_nome}||{item_key}" if produto_nome else None)
            item = pending_orders.finish_production(item_key)
            # Finaliza o timer usando o timer_key único
            if timer_key:
                production_timer.stop_and_log(timer_key)
            elif produto_nome:
                production_timer.stop_and_log(produto_nome)
            return jsonify({'success': True, 'item': item})

        @self.app.route('/api/pending-orders/dismiss', methods=['POST'])
        @token_required
        def api_pending_orders_dismiss(token):
            """Remove um item da fila de pendentes."""
            data = request.json
            item_key = data.get('item_key', '')
            if not item_key:
                return jsonify({'error': 'item_key obrigatório'}), 400
            pending_orders.dismiss(item_key)
            return jsonify({'success': True})

        @self.app.route('/api/pending-orders/sync', methods=['POST'])
        @token_required
        def api_pending_orders_sync(token):
            """Força sincronização imediata dos pedidos do Bling com a fila pendente."""
            try:
                with self.orchestrator._cache_lock:
                    cache_flat = {**self.orchestrator._products_cache, **self.orchestrator._kits_cache}
                orders = self.orchestrator.sales._sales_history or []
                
                # Tenta sync direto primeiro
                added = pending_orders.sync_from_orders(orders, cache_flat)
                
                # Se não adicionou nada, busca pedidos individualmente em background
                if added == 0 and orders:
                    Thread(
                        target=self.orchestrator._fetch_orders_with_items,
                        args=(orders, cache_flat),
                        daemon=True
                    ).start()
                    return jsonify({
                        'success': True,
                        'added': 0,
                        'message': f'Buscando itens de {len(orders)} pedidos individualmente... Aguarde 30s e atualize a página.',
                        'total_waiting': len(pending_orders.get_waiting())
                    })

                return jsonify({
                    'success': True,
                    'added': added,
                    'total_waiting': len(pending_orders.get_waiting())
                })
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/debug/orders-sample')
        @token_required
        def api_debug_orders_sample(token):
            """Debug: mostra estrutura dos últimos 3 pedidos para diagnóstico."""
            orders = self.orchestrator.sales._sales_history or []
            sample = orders[-3:] if orders else []
            result = []
            for o in sample:
                result.append({
                    'id': o.get('id'),
                    'numero': o.get('numero'),
                    'data': o.get('data'),
                    'situacao': o.get('situacao'),
                    'tem_itens': bool(o.get('itens')),
                    'qtd_itens': len(o.get('itens', [])),
                    'itens_sample': o.get('itens', [])[:2],
                    'campos_disponiveis': list(o.keys())
                })
            return jsonify({'total_pedidos': len(orders), 'sample': result})

        # Rota de Callback OAuth (Recebe o code do Bling)
        @self.app.route('/callback')
        def callback():
            code = request.args.get('code')
            state = request.args.get('state')
            
            logger.info("🔐 Callback OAuth recebido.")
            
            if not code:
                logger.error("Código de autorização OAuth não recebido.")
                return "Erro: Código de autorização não recebido.", 400
                
            if not self.orchestrator.auth._validate_oauth_state(state):
                logger.error("State OAuth inválido ou expirado.")
                return "Erro: State inválido ou expirado.", 403

            success = self.orchestrator.auth.exchange_code_for_token(code)

            if success:
                logger.info("✅ Autenticação OAuth concluída com sucesso.")
                self.orchestrator.auth.reload_tokens_from_disk()

                if not self.orchestrator.is_running():
                    self.orchestrator.start_worker()
                    start_cleanup_timer()
                    logger.info("🚀 Worker iniciado após autenticação.")
                else:
                    self.orchestrator.wake_worker()

                return redirect('/')
            else:
                logger.error("Falha ao trocar código OAuth pelo token.")
                return "Erro ao trocar código pelo token.", 500

        # Rota de Busca com correção de 404 e Imagem
        @self.app.route('/api/products/search')
        @self.app.route('/products/search') # Aceita as duas chamadas
        @token_required
        def api_products_search(token):
            query = request.args.get('q', '').lower().strip()
            results = []
            
            # Pega todos os itens (produtos e kits)
            all_items = self.orchestrator.get_all_products() + self.orchestrator.get_all_kits()
            
            self.logger.info(f"🔍 Busca iniciada: '{query}' em {len(all_items)} itens.")
            
            for p in all_items:
                nome = str(p.get('nome', '')).lower()
                sku = str(p.get('sku', '')).lower()
                
                # Se a query estiver vazia, retorna os primeiros 20 itens
                if not query or (query in nome or query in sku):
                    results.append({
                        "id": p.get("id"),
                        "nome": p.get("nome"),
                        "sku": p.get("sku"),
                        "estoque": p.get("estoqueAtual", 0),
                        "estoqueAtual": p.get("estoqueAtual", 0),
                        "imagemURL": p.get("imagem") or "/static/no-image.png",
                        "imagem": p.get("imagem") or "/static/no-image.png",
                        "tipo": "Kit" if p.get("tipo") == "K" else "Produto",
                        "componentes": p.get("componentes", [])
                    })
            
            self.logger.info(f"✅ Busca finalizada: {len(results)} resultados encontrados.")
            return jsonify(results[:50]) # Aumentado para 50 resultados

        @self.app.route('/api/debug/cache')
        @token_required
        def api_debug_cache(token):
            c = self.orchestrator
            with c._cache_lock:
                sample_products = list(c._products_cache.values())[:5]
                sample_kits = list(c._kits_cache.values())[:5]
                return jsonify({
                    "products_count": len(c._products_cache),
                    "kits_count": len(c._kits_cache),
                    "sample_products": sample_products,
                    "sample_kits": sample_kits
                })

        @self.app.route('/api/kits')
        @token_required
        def api_kits(token):
            """Retorna a lista de todos os kits e produtos simples em cache."""
            kits = self.orchestrator.get_all_kits()
            products = self.orchestrator.get_all_products()
            
            self.logger.info(f"📦 Endpoint /api/kits chamado. Kits: {len(kits)}, Produtos: {len(products)}")
            
            def normalize_for_api(item):
                estoque_val = item.get("estoqueAtual", item.get("estoque", 0))
                tipo = item.get("tipo", "P")
                # Mapeia tipo textual para K/P (compatibilidade)
                if tipo in ["COMPOSTO", "K"]: tipo_out = "K"
                else: tipo_out = "P"

                return {
                    "id": item.get("id"),
                    "nome": item.get("nome"),
                    "sku": item.get("sku"),
                    "estoque": estoque_val,
                    "estoqueAtual": estoque_val,
                    "imagemURL": item.get("imagem") if item.get("imagem") else "/static/no-image.png",
                    "imagem": item.get("imagem") if item.get("imagem") else "/static/no-image.png",
                    "tipo": tipo_out,
                    "componentes": item.get("componentes", [])
                }

            all_list = [normalize_for_api(p) for p in kits + products]
            return jsonify(all_list)

        @self.app.route('/api/mongo-status')
        def api_mongo_status():
            """Retorna status da conexão MongoDB."""
            return jsonify({
                'mongodb_available': MONGO_AVAILABLE,
                'storage_backend': 'MongoDB' if MONGO_AVAILABLE else 'Arquivo Local (efêmero)'
            })

        @self.app.route('/_health')
        def health_check():
            """Endpoint de health check — rápido, sem side effects."""
            import time as _t
            auth = self.orchestrator.auth
            # Verifica token direto, sem chamar refresh_token (operação lenta)
            auth_valid = bool(auth._access_token and auth._expires_at > _t.time() + 60)
            status = {
                "status": "ok",
                "worker_running": self.orchestrator.is_running(),
                "auth_valid": auth_valid,
                "cache_loaded": self.orchestrator.is_cache_loaded(),
                "mongodb": MONGO_AVAILABLE,
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
            Thread(target=self.orchestrator.process_products_cache, daemon=True).start()
            
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
            """Recebe webhooks do Bling - Correção para V3."""
            with WebServer.webhook_lock:
                try:
                    # Log de entrada bruta para diagnóstico
                    self.logger.debug(f"Webhook bruto recebido: {request.data.decode('utf-8')[:500]}")
                    self.logger.debug(f"Headers do Webhook: {dict(request.headers)}")

                    # 1. Validação de Assinatura (Mantenha se configurado no Render)
                    signature = request.headers.get("X-Bling-Signature-256")
                    if self.config.WEBHOOK_SECRET and not signature:
                        self.logger.warning("Webhook rejeitado: WEBHOOK_SECRET configurado mas assinatura ausente.")
                        return jsonify({"status": "forbidden", "reason": "missing signature"}), 403

                    data = request.json
                    if not data:
                        self.logger.debug("Webhook ignorado: JSON vazio ou inválido.")
                        return jsonify({"status": "ignored"}), 200

                    self.logger.info(f"⚡ Webhook recebido: {str(data)[:200]}")

                    # 2. DETECÇÃO ROBUSTA DE EVENTO (V2 e V3)
                    should_update = False

                    # Caso 1: Webhook V3 Padrão (vem "id", "situacao", "tipo" na raiz)
                    if 'situacao' in data and 'id' in data:
                        self.logger.debug(f"Webhook V3 detectado (ID: {data.get('id')}, Situação: {data.get('situacao')})")
                        should_update = True
                    
                    # Caso 2: Tipo explícito
                    elif data.get('tipo') == 'pedidoVenda':
                        self.logger.debug("Webhook tipo pedidoVenda detectado.")
                        should_update = True

                    # Caso 3: Formato antigo (V2)
                    elif 'retorno' in data and 'pedidos' in data['retorno']:
                        self.logger.debug("Webhook V2 detectado.")
                        should_update = True
                    
                    # Caso 4: Callbacks de teste
                    elif data.get('test') == True:
                        self.logger.debug("Webhook de teste recebido.")
                        return jsonify({"status": "ok", "message": "Test received"}), 200

                    if should_update:
                        self.logger.info("🔔 Alteração de pedido detectada via Webhook. Iniciando atualização...")
                        
                        # Dispara atualização em background
                        Thread(target=self.orchestrator.process_sales_orders, kwargs={'force': True}, daemon=True).start()
                        
                        return jsonify({"status": "ok", "message": "Update triggered"}), 200

                    self.logger.info("Webhook ignorado (formato desconhecido ou não é pedido)")
                    return jsonify({"status": "ignored"}), 200

                except Exception as e:
                    self.logger.error(f"Erro processando webhook: {e}")
                    return jsonify({"error": "Internal Error"}), 500

    def _setup_websockets(self):
        """Configura os WebSockets para logs e atualizações de KPI."""
        
        @self.sock.route('/ws/logs')
        def ws_logs(ws):
            self.logger.info("📡 WebSocket logs conectado.")
            if len(memory_handler.ws_callbacks) >= 10:
                self.logger.warning("Limite de conexões de log WS atingido.")
                return

            def ws_callback(log_entry):
                try:
                    ws.send(json.dumps({"logs": [log_entry]}))
                except ConnectionClosed:
                    raise
                except Exception:
                    raise ConnectionClosed()

            try:
                ws.send(json.dumps({"logs": memory_handler.get_logs()}))
                memory_handler.add_ws_callback(ws_callback)
                while True:
                    ws.receive(timeout=60)
            except ConnectionClosed:
                pass
            finally:
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

            # Callback para enviar atualizações para este cliente
            def kpi_callback(payload):
                try:
                    ws.send(json.dumps(payload))
                except ConnectionClosed:
                    raise
                except Exception:
                    raise ConnectionClosed()

            # 1. Registra o callback PRIMEIRO para não perder nenhum broadcast
            with kpi_update_lock:
                kpi_update_callbacks.append(kpi_callback)

            # 2. Envia estado inicial diretamente para este cliente (sem broadcast global)
            #    Usa apenas o que já está em cache — sem cálculos bloqueantes
            try:
                sales_stats = self.orchestrator.sales._get_state_for_save()
                component_usage = getattr(self.orchestrator, '_component_usage_cache', None) or {}
                auth_ok = bool(self.orchestrator.auth._access_token and
                               self.orchestrator.auth._expires_at > __import__('time').time() + 60)
                initial_payload = {
                    "type": "full_update",
                    "authenticated": auth_ok,
                    "auth_error": False,
                    "is_running": self.orchestrator.is_running(),
                    "cache_updated": False,
                    "auth_url": self.orchestrator.auth.get_authorization_url(),
                }
                if sales_stats and isinstance(sales_stats, dict):
                    stats_data = sales_stats.copy()
                    lr = stats_data.pop('last_recalculated', None)
                    stats_data['last_update'] = lr.isoformat() if hasattr(lr, 'isoformat') else str(lr)
                    initial_payload["sales_stats"] = stats_data
                if component_usage:
                    initial_payload["component_usage"] = component_usage
                ws.send(json.dumps(initial_payload))
                self.logger.info("✅ Estado inicial enviado ao cliente WS.")
            except Exception as e:
                self.logger.warning(f"Não foi possível enviar estado inicial ao WS: {e}")
                
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
    <title>SW Móveis MDF — Painel de Gestão</title>
    <link rel="icon" href="https://i.imgur.com/j79HO6n.png" type="image/png">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
    <style>
        /* ══════════════════════════════════════════
           SW MÓVEIS MDF — DESIGN SYSTEM 2025
           Manual de Identidade Visual
        ══════════════════════════════════════════ */
        :root {
            --sw-yellow:       #ffb600;
            --sw-yellow-light: #fede8f;
            --sw-yellow-pale:  #f5f5a0;
            --sw-black:        #01010d;
            --sw-gray:         #807f7f;
            --sw-nurse:        #ecedec;

            --primary:     var(--sw-black);
            --accent:      var(--sw-yellow);
            --accent-light:var(--sw-yellow-light);
            --success:     #10b981;
            --warning:     var(--sw-yellow);
            --error:       #ef4444;
            --bg:          #f9f9f7;
            --bg-card:     #ffffff;
            --border:      rgba(1,1,13,0.09);
            --text-muted:  var(--sw-gray);
            --radius:      12px;
            --radius-sm:   7px;
            --shadow:      0 2px 12px rgba(1,1,13,0.07);
            --shadow-lg:   0 12px 40px rgba(1,1,13,0.13);
        }

        *, *::before, *::after { box-sizing: border-box; }

        html { scroll-behavior: smooth; }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: var(--bg);
            color: var(--primary);
            font-size: 14px;
            line-height: 1.6;
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
            overflow-x: hidden;
        }

        h1,h2,h3,h4,h5,h6 { font-weight: 700; line-height: 1.2; }

        /* ══ PATTERN BAR ══ */
        .sw-pattern-bar {
            height: 5px;
            background: repeating-linear-gradient(
                90deg,
                var(--sw-yellow) 0, var(--sw-yellow) 12px,
                var(--sw-black) 12px, var(--sw-black) 18px
            );
        }

        /* ══ NAVBAR ══ */
        .navbar {
            background: var(--sw-black) !important;
            border-bottom: 3px solid var(--sw-yellow);
            padding: 0 1.5rem;
            min-height: 64px;
            box-shadow: 0 2px 20px rgba(1,1,13,0.3);
            will-change: transform;
        }

        .navbar-brand {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            text-decoration: none;
        }

        .navbar-brand img {
            height: 40px;
            width: auto;
            filter: brightness(1.05);
        }

        .navbar-brand-text {
            display: flex;
            flex-direction: column;
            line-height: 1.1;
        }

        .navbar-brand-name {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 1.35rem;
            color: var(--sw-yellow);
            letter-spacing: 0.07em;
        }

        .navbar-brand-sub {
            font-size: 0.58rem;
            color: rgba(255,255,255,0.45);
            letter-spacing: 0.18em;
            text-transform: uppercase;
            font-weight: 500;
        }

        /* ══ STATUS BADGE ══ */
        #status-badge {
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            padding: 0.35rem 0.9rem !important;
            border-radius: 50px !important;
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: 0.06em;
            text-transform: uppercase;
        }

        #status-badge.bg-success {
            background: #10b981 !important;
            box-shadow: 0 0 14px rgba(16,185,129,0.4);
        }

        #status-badge.bg-danger {
            background: #ef4444 !important;
        }

        #status-badge.bg-secondary {
            background: rgba(255,255,255,0.12) !important;
            color: rgba(255,255,255,0.7);
        }

        @keyframes pulse-badge {
            0%,100% { opacity:1; }
            50% { opacity:0.75; }
        }

        #status-badge { animation: pulse-badge 2.5s ease-in-out infinite; }

        /* ══ AUTH LINK BUTTON ══ */
        #auth-link {
            padding: 0.4rem 1rem;
            border: 1.5px solid var(--sw-yellow);
            color: var(--sw-yellow) !important;
            border-radius: var(--radius-sm);
            font-size: 0.78rem;
            font-weight: 700;
            text-decoration: none;
            letter-spacing: 0.04em;
            transition: all 0.2s ease;
            white-space: nowrap;
        }

        #auth-link:hover {
            background: var(--sw-yellow);
            color: var(--sw-black) !important;
        }

        /* ══ CONTAINER ══ */
        .container-fluid.px-4.py-5 {
            max-width: 1440px;
            margin: 0 auto;
        }

        /* ══ PAGE TITLE ══ */
        .page-title {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 2.2rem;
            letter-spacing: 0.04em;
            line-height: 1;
            color: var(--sw-black);
        }

        .page-title .highlight { color: var(--sw-yellow); }

        /* ══ KPI CARDS ══ */
        .card {
            border: 1px solid var(--border);
            border-radius: var(--radius);
            background: var(--bg-card);
            box-shadow: var(--shadow);
            transition: transform 0.25s ease, box-shadow 0.25s ease;
            will-change: transform;
        }

        .card:hover {
            transform: translateY(-3px);
            box-shadow: var(--shadow-lg);
            border-color: rgba(255,182,0,0.35);
        }

        .kpi-card {
            border-left: 4px solid;
            position: relative;
            overflow: hidden;
        }

        .kpi-card::before {
            content: '';
            position: absolute;
            inset: 0;
            background: linear-gradient(135deg, rgba(255,255,255,0.5) 0%, transparent 100%);
            pointer-events: none;
        }

        .kpi-daily   { border-left-color: var(--sw-yellow); }
        .kpi-weekly  { border-left-color: var(--sw-yellow-light); }
        .kpi-historic{ border-left-color: var(--success); }

        .kpi-card h5 {
            font-size: 0.68rem;
            font-weight: 800;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.1em;
            margin-bottom: 0.6rem;
        }

        .kpi-card h3 {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 3rem;
            line-height: 1;
            margin: 0;
        }

        #kpi-daily   { color: var(--sw-yellow); }
        #kpi-weekly  { color: #c49200; }
        #kpi-historic{ color: var(--success); }

        @keyframes kpi-flash {
            0%   { background: rgba(255,182,0,0.15); }
            100% { background: transparent; }
        }

        .kpi-card.updating { animation: kpi-flash 0.6s ease-out; }

        /* ══ CARD HEADER ══ */
        .card-header {
            background: var(--sw-black) !important;
            color: white;
            border: none;
            border-radius: var(--radius) var(--radius) 0 0 !important;
            font-weight: 600;
            padding: 1rem 1.25rem;
        }

        .card-header h5 {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 1rem;
            letter-spacing: 0.07em;
            margin: 0;
            color: white;
        }

        .card-header small { color: rgba(255,255,255,0.5); font-weight: 400; }

        /* ══ LOG BOX ══ */
        .log-box {
            font-family: 'Fira Code', 'Cascadia Code', 'Consolas', monospace;
            font-size: 0.76rem;
            background: #01010d;
            color: #d4d4d4;
            border-radius: 0 0 var(--radius) var(--radius);
            padding: 1rem;
            max-height: 340px;
            overflow-y: auto;
            line-height: 1.6;
        }

        .log-box::-webkit-scrollbar { width: 4px; }
        .log-box::-webkit-scrollbar-track { background: rgba(255,255,255,0.03); }
        .log-box::-webkit-scrollbar-thumb { background: rgba(255,182,0,0.3); border-radius: 2px; }
        .log-box::-webkit-scrollbar-thumb:hover { background: rgba(255,182,0,0.55); }

        .log-entry { padding: 0.12rem 0; animation: log-slide-in 0.25s ease-out; }

        @keyframes log-slide-in {
            from { opacity:0; transform: translateX(-8px); }
            to   { opacity:1; transform: translateX(0); }
        }

        .log-level-INFO    { color: #4ec9b0; }
        .log-level-WARNING { color: var(--sw-yellow); }
        .log-level-ERROR   { color: #f48771; }
        .log-level-DEBUG   { color: #569cd6; }

        /* ══ TABS ══ */
        .nav-tabs {
            border-bottom: 2px solid var(--border);
            gap: 0.15rem;
            flex-wrap: nowrap;
            overflow-x: auto;
        }

        .nav-tabs::-webkit-scrollbar { display: none; }

        .nav-tabs .nav-link {
            color: var(--text-muted);
            border: none;
            border-bottom: 3px solid transparent;
            font-weight: 600;
            font-size: 0.8rem;
            letter-spacing: 0.02em;
            padding: 0.65rem 1rem;
            margin-bottom: -2px;
            transition: all 0.2s ease;
            white-space: nowrap;
            background: none;
        }

        .nav-tabs .nav-link:hover {
            color: var(--sw-black);
            border-bottom-color: rgba(255,182,0,0.4);
        }

        .nav-tabs .nav-link.active {
            color: var(--sw-black);
            background: none;
            border-bottom-color: var(--sw-yellow);
            font-weight: 700;
        }

        .tab-content { animation: fadeIn 0.3s ease-out; }

        @keyframes fadeIn {
            from { opacity:0; transform: translateY(6px); }
            to   { opacity:1; transform: translateY(0); }
        }

        /* ══ BUTTONS ══ */
        .btn {
            font-weight: 600;
            font-size: 0.8rem;
            letter-spacing: 0.03em;
            border-radius: var(--radius-sm);
            transition: all 0.2s ease;
        }

        .btn-primary {
            background: var(--sw-yellow) !important;
            border-color: var(--sw-yellow) !important;
            color: var(--sw-black) !important;
        }

        .btn-primary:hover {
            background: #e6a400 !important;
            border-color: #e6a400 !important;
            transform: translateY(-1px);
            box-shadow: 0 6px 20px rgba(255,182,0,0.4);
        }

        .btn-primary:active { transform: translateY(0); }

        .btn-outline-light {
            border: 1.5px solid rgba(255,255,255,0.3) !important;
            color: white !important;
        }

        .btn-outline-light:hover {
            background: rgba(255,255,255,0.12) !important;
            border-color: rgba(255,255,255,0.6) !important;
        }

        /* ══ FORM CONTROLS ══ */
        .form-control, .form-select {
            border: 1.5px solid var(--border);
            border-radius: var(--radius-sm);
            padding: 0.7rem 0.95rem;
            font-size: 0.85rem;
            font-weight: 500;
            transition: border-color 0.2s ease, box-shadow 0.2s ease;
        }

        .form-control:focus, .form-select:focus {
            border-color: var(--sw-yellow);
            box-shadow: 0 0 0 3px rgba(255,182,0,0.18);
        }

        /* ══ TABLE ══ */
        .table { font-size: 0.82rem; }

        .table thead th {
            background: var(--bg);
            border: none;
            border-bottom: 2px solid var(--border);
            font-weight: 700;
            color: var(--text-muted);
            font-size: 0.68rem;
            text-transform: uppercase;
            letter-spacing: 0.09em;
            padding: 0.8rem 1rem;
        }

        .table tbody tr {
            border-bottom: 1px solid var(--border);
            transition: background 0.15s ease;
        }

        .table tbody tr:hover { background: rgba(255,182,0,0.04); }
        .table td { padding: 0.75rem 1rem; vertical-align: middle; }

        /* ══ BADGES ══ */
        .badge {
            font-weight: 700;
            font-size: 0.65rem;
            letter-spacing: 0.05em;
            padding: 0.3rem 0.65rem;
            border-radius: 50px;
        }

        .badge.bg-success { background: #10b981 !important; }
        .badge.bg-warning { background: var(--sw-yellow) !important; color: var(--sw-black) !important; }
        .badge.bg-danger  { background: #ef4444 !important; }

        /* ══ ALERTS ══ */
        .alert {
            border: none;
            border-left: 4px solid;
            border-radius: var(--radius-sm);
            font-size: 0.83rem;
            font-weight: 500;
        }

        .alert-warning {
            background: rgba(255,182,0,0.1);
            border-left-color: var(--sw-yellow);
            color: #92400e;
        }

        .alert-info {
            background: rgba(59,130,246,0.08);
            border-left-color: #3b82f6;
            color: #1e3a8a;
        }

        .alert-danger {
            background: rgba(239,68,68,0.08);
            border-left-color: #ef4444;
            color: #7f1d1d;
        }

        /* ══ METRIC BOX ══ */
        .metric-box {
            background: var(--sw-black);
            border-radius: var(--radius);
            padding: 1.3rem;
            color: white;
            text-align: center;
            transition: transform 0.2s ease, box-shadow 0.2s ease;
            margin-bottom: 1rem;
        }

        .metric-box:hover {
            transform: translateY(-3px);
            box-shadow: 0 12px 30px rgba(1,1,13,0.2);
        }

        .metric-label {
            font-size: 0.68rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            color: rgba(255,255,255,0.5);
            margin-bottom: 0.5rem;
        }

        .metric-value {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 2.4rem;
            color: var(--sw-yellow);
            line-height: 1;
        }

        /* ══ LIST GROUP ══ */
        .list-group-item {
            border: 1px solid var(--border);
            border-radius: var(--radius-sm) !important;
            margin-bottom: 0.4rem;
            font-size: 0.83rem;
            transition: all 0.2s ease;
        }

        .list-group-item:hover {
            border-color: var(--sw-yellow);
            background: rgba(255,182,0,0.04);
            transform: translateX(3px);
        }

        /* ══ ACCORDION ══ */
        .accordion-button { font-weight: 600; font-size: 0.85rem; }

        .accordion-button:not(.collapsed) {
            background: rgba(255,182,0,0.08);
            color: var(--sw-black);
            box-shadow: none;
        }

        .accordion-button:focus {
            box-shadow: 0 0 0 3px rgba(255,182,0,0.2);
        }

        /* ══ TOAST ══ */
        .toast-container { z-index: 9999; }

        .toast {
            background: var(--sw-black);
            border: none;
            border-left: 4px solid var(--sw-yellow);
            border-radius: var(--radius);
            box-shadow: 0 12px 40px rgba(1,1,13,0.25);
            animation: toast-in 0.35s cubic-bezier(0.34,1.56,0.64,1);
        }

        @keyframes toast-in {
            from { opacity:0; transform: translateX(50px); }
            to   { opacity:1; transform: translateX(0); }
        }

        .toast.hide { animation: toast-out 0.25s ease forwards; }

        @keyframes toast-out {
            to { opacity:0; transform: translateX(50px); }
        }

        /* ══ MODAL ══ */
        .modal-content {
            border: none;
            border-radius: var(--radius) !important;
            overflow: hidden;
            box-shadow: 0 25px 80px rgba(1,1,13,0.3);
        }

        .modal-header {
            background: var(--sw-black) !important;
            border-bottom: 3px solid var(--sw-yellow) !important;
            color: white;
        }

        .modal-title { font-family: 'Bebas Neue', sans-serif !important; letter-spacing: 0.06em; }

        /* ══ SCROLLBAR GLOBAL ══ */
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: var(--bg); }
        ::-webkit-scrollbar-thumb { background: rgba(1,1,13,0.15); border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: var(--sw-yellow); }

        /* ══ UTILITY ══ */
        .hidden { display: none !important; }
        .stock-badge, .estoque-info, .stock-info-row { display: none !important; }
        .shadow-2xl { box-shadow: 0 25px 50px -12px rgba(0,0,0,0.25); }
        .letter-spacing-2 { letter-spacing: 0.1em; }

        /* ══ ANIMATIONS ══ */
        @keyframes slideDown {
            from { opacity:0; transform: translateY(-12px); }
            to   { opacity:1; transform: translateY(0); }
        }

        @keyframes fadeInUp {
            from { opacity:0; transform: translateY(14px); }
            to   { opacity:1; transform: translateY(0); }
        }

        @keyframes pulse-animation {
            0%,100% { opacity:1; }
            50%      { opacity:0.5; }
        }

        .pulse-animation { animation: pulse-animation 2s infinite; }

        .navbar { animation: slideDown 0.3s ease-out; }
        .card   { animation: fadeInUp 0.35s ease-out; }

        /* ══ FOOTER ══ */
        footer {
            background: var(--sw-black) !important;
            border-top: 3px solid var(--sw-yellow);
        }

        /* ══ RESPONSIVO ══ */
        @media (max-width: 768px) {
            .kpi-card h3 { font-size: 2.2rem; }
            .metric-value { font-size: 1.8rem; }
            .log-box { max-height: 260px; }
        }
    </style>
</head>
<body>

    <!-- PATTERN BAR TOP -->
    <div class="sw-pattern-bar"></div>

    <!-- NAVBAR -->
    <nav class="navbar navbar-expand-lg">
        <div class="container-fluid px-4">
            <a class="navbar-brand text-white d-flex align-items-center" href="#" style="gap: 0.75rem;">
                <img src="https://i.imgur.com/j79HO6n.png" alt="SW Móveis MDF" style="height: 40px; width: auto; filter: brightness(1.1);">
                <div class="navbar-brand-text">
                    <span class="navbar-brand-name">SW Móveis MDF</span>
                    <span class="navbar-brand-sub">Painel de Gestão</span>
                </div>
            </a>
            <div class="d-flex align-items-center gap-3">
                <span id="status-badge" class="badge bg-secondary" title="Aguardando WebSocket...">⏳ Conectando...</span>
                <a id="auth-link" href="{{ auth_url }}" class="btn btn-sm btn-outline-light">Autenticar</a>
            </div>
        </div>
    </nav>

    <!-- CONTAINER PRINCIPAL -->
    <div class="container-fluid px-4 py-5">

        <!-- PAGE HEADER -->
        <div class="row mb-5">
            <div class="col-12">
                <h2 class="mb-1 page-title">Pedidos de <span class="highlight">Venda</span></h2>
                <p class="text-muted mb-4" style="font-size:0.85rem;">Acompanhe os pedidos abertos e fechados em tempo real</p>
            </div>
        </div>

        <!-- KPI CARDS -->
        <div class="row mb-5">
            <div class="col-md-4 mb-4">
                <div class="card p-4 kpi-card kpi-daily text-center">
                    <h5>⚡ Pedidos Diários</h5>
                    <h3 id="kpi-daily" class="text-primary">0</h3>
                    <small class="text-muted">Últimas 24h</small>
                </div>
            </div>
            <div class="col-md-4 mb-4">
                <div class="card p-4 kpi-card kpi-weekly text-center">
                    <h5>📅 Pedidos Semanais</h5>
                    <h3 id="kpi-weekly" style="color: var(--warning);">0</h3>
                    <small class="text-muted">Últimos 7 dias</small>
                </div>
            </div>
            <div class="col-md-4 mb-4">
                <div class="card p-4 kpi-card kpi-historic text-center">
                    <h5>📊 Pedidos Mensais</h5>
                    <h3 id="kpi-historic" style="color: var(--success);">0</h3>
                    <small class="text-muted">Este Mês</small>
                </div>
            </div>
        </div>

        <!-- TIMESTAMP -->
        <div class="row mb-5">
            <div class="col-12">
                <small class="text-muted">
                    ⏱️ Último Recálculo: <span id="last-recalculated" style="font-weight: 600;">N/D</span>
                </small>
            </div>
        </div>

        <!-- LOGS EM TEMPO REAL -->
        <div class="row mb-5">
            <div class="col-12">
                <div class="card">
                    <div class="card-header">
                        <h5 class="mb-0">📋 Logs <span style="color:var(--sw-yellow)">em Tempo Real</span></h5>
                    </div>
                    <div class="card-body p-0">
                        <div id="logs-content" class="log-box"></div>
                    </div>
                </div>
            </div>
        </div>

        <!-- TABS -->
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
                        <button class="nav-link" id="component-tab" data-bs-toggle="tab" data-bs-target="#component-usage" type="button">🔧 Insumos & Produção</button>
                    </li>
                </ul>

                <!-- AUTH REQUIRED -->
                <div id="auth-required-tabs" class="alert alert-warning hidden mb-4">
                    🔐 É necessário autenticar com o SW Móveis para visualizar o conteúdo.
                </div>

                <!-- TAB CONTENT -->
                <div id="content-tabs" class="tab-content hidden">

                    <!-- TAB: BUSCA -->
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

                    <!-- TAB: PRODUTOS -->
                    <div class="tab-pane fade" id="kits" role="tabpanel">
                        <div class="mb-4">
                            <button class="btn btn-primary btn-sm" onclick="forceAndReloadKits(event)">🔄 Recarregar Lista</button>
                            <small class="text-muted d-block mt-2">⚠️ Carregamento pode levar 2-5 minutos. Aguarde a notificação do WebSocket.</small>
                        </div>
                        <div id="kits-list"></div>
                    </div>

                    <!-- TAB: DASHBOARD KPI -->
                    <div class="tab-pane fade" id="kpi-chart" role="tabpanel">
                        <div class="row">
                            <div class="col-lg-8 mb-4">
                                <div class="card">
                                    <div class="card-header">
                                        <h5 class="mb-0">📈 Evolução de Pedidos <span style="color:var(--sw-yellow)">(Últimos 30 dias)</span></h5>
                                    </div>
                                    <div class="card-body" style="height: 400px;">
                                        <canvas id="salesChart"></canvas>
                                    </div>
                                </div>
                            </div>
                            <div class="col-lg-4">
                                <div class="card">
                                    <div class="card-header">
                                        <h5 class="mb-0">🎯 Métricas <span style="color:var(--sw-yellow)">Rápidas</span></h5>
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
                                            <div class="metric-value" id="trend-indicator" style="font-size:1.4rem;">📊 Estável</div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- TAB: INSUMOS & PRODUÇÃO -->
                    <div class="tab-pane fade" id="component-usage" role="tabpanel">

                        <!-- PAINEL DE PRODUÇÃO -->
                        <div class="card mb-4 border-0 shadow-sm">
                            <div class="card-header d-flex justify-content-between align-items-center py-3" style="background: linear-gradient(135deg, #01010d 0%, #1e3a5f 100%) !important;">
                                <div>
                                    <h5 class="mb-0 text-white">🏭 Painel de <span style="color:var(--sw-yellow)">Produção</span></h5>
                                    <small class="text-white-50">
                                        ⏳ Em Espera <span id="waiting-count-badge" class="badge bg-warning text-dark ms-1">0</span>
                                        &nbsp; ⚙️ Produzindo <span id="inprod-count-badge" class="badge bg-success ms-1">0</span>
                                        &nbsp; ✅ Concluídos <span id="done-count-badge" class="badge bg-secondary ms-1">0</span>
                                    </small>
                                </div>
                                <button class="btn btn-sm btn-outline-light" onclick="syncAndRefreshPending()">🔄 Sincronizar Bling</button>
                            </div>
                            <div class="card-body p-0" id="production-board-section">
                                <p class="text-center text-muted py-4">⏳ Carregando...</p>
                            </div>
                        </div>

                        <!-- CONSUMO MENSAL -->
                        <div class="card mb-4 border-0 shadow-sm">
                            <div class="card-header d-flex justify-content-between align-items-center" style="background: linear-gradient(135deg, #065f46 0%, #059669 100%) !important;">
                                <div>
                                    <h5 class="mb-0">📊 Consumo de Insumos & Componentes</h5>
                                    <small class="text-white-50" id="consumption-month-label">Mês atual • Reinicia todo mês</small>
                                </div>
                                <span class="badge bg-light text-dark" id="consumption-total-badge">0 insumos</span>
                            </div>
                            <div class="card-body p-0" id="consumption-table-section">
                                <div class="text-center py-4 text-muted">⏳ Carregando consumo...</div>
                            </div>
                        </div>

                        <!-- HISTÓRICO DE FINALIZAÇÕES -->
                        <div class="card border-0 shadow-sm">
                            <div class="card-header" style="background: linear-gradient(135deg, #3b0764 0%, #7c3aed 100%) !important;">
                                <h5 class="mb-0">📜 Histórico de Finalizações (Mês)</h5>
                                <small class="text-white-50">Registro de cada produto finalizado com tempo de produção</small>
                            </div>
                            <div class="card-body p-0" id="production-history-section">
                                <div class="text-center py-4 text-muted">⏳ Carregando histórico...</div>
                            </div>
                        </div>

                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- TOAST CONTAINER -->
    <div class="toast-container position-fixed bottom-0 end-0 p-4"></div>

    <!-- BOOTSTRAP JS -->
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>

<script>
        const API = '/api';
        let isAuthenticated = false;
        let salesChart = null;

        /* ✅ DESIGN: Fetch API com Tratamento */
        async function fetchAPI(url, options = {}) {
            const response = await fetch(url, options);

            if (response.status === 401) {
                // Token expirado: atualiza UI sem redirecionar (evita loop)
                isAuthenticated = false;
                const badge = document.getElementById('status-badge');
                if (badge) { badge.className = 'badge bg-warning text-dark'; badge.textContent = '🟡 Sessão expirada'; }
                const authLink = document.getElementById('auth-link');
                if (authLink) authLink.classList.remove('d-none');
                throw new Error('401');
            }

            if (!response.ok) {
                const errorText = await response.text().catch(() => response.statusText);
                throw new Error(`HTTP ${response.status}: ${errorText}`);
            }

            return response.json().catch(() => ({}));
        }

        /* Sanitização contra XSS — escapa dados externos antes de inserir no DOM */
        function escapeHtml(str) {
            if (str == null) return '—';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
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

        // WebSocket de logs com reconexão automática e limite de linhas
        const _MAX_LOG_LINES = 300;
        let _wsLogs = null;

        function _connectWsLogs() {
            const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
            _wsLogs = new WebSocket(`${proto}://${window.location.host}/ws/logs`);
            _wsLogs.onmessage = (e) => {
                const data = JSON.parse(e.data);
                const box = document.getElementById('logs-content');
                if (!box || !data.logs) return;
                // Adicionar novas linhas sem acumular memória infinita
                data.logs.forEach(l => box.insertAdjacentHTML('beforeend', formatLog(l)));
                // Limitar linhas visíveis
                const entries = box.querySelectorAll('.log-entry');
                if (entries.length > _MAX_LOG_LINES) {
                    for (let i = 0; i < entries.length - _MAX_LOG_LINES; i++) {
                        entries[i].remove();
                    }
                }
                box.scrollTop = box.scrollHeight;
            };
            _wsLogs.onclose = () => {
                setTimeout(_connectWsLogs, 4000);
            };
            _wsLogs.onerror = () => _wsLogs.close();
        }
        _connectWsLogs();

        /* Atualizar Status de Autenticação */
        function updateAuthStatus(authenticated, authUrl) {
            const badge = document.getElementById('status-badge');
            const wasAuthenticated = isAuthenticated;
            isAuthenticated = authenticated;

            if (isAuthenticated) {
                if (badge) { badge.className = 'badge bg-success'; badge.textContent = '🟢 Online'; }
                const al = document.getElementById('auth-link');
                if (al) al.classList.add('d-none');
                const ct = document.getElementById('content-tabs');
                if (ct) ct.classList.remove('hidden');
                const at = document.getElementById('auth-required-tabs');
                if (at) at.classList.add('hidden');
                _onAuthConfirmed();
            } else {
                if (badge) { badge.className = 'badge bg-danger'; badge.textContent = '🔴 Offline'; }
                const al = document.getElementById('auth-link');
                if (al) al.classList.remove('d-none');
                const ct = document.getElementById('content-tabs');
                if (ct) ct.classList.add('hidden');
                const at = document.getElementById('auth-required-tabs');
                if (at) at.classList.remove('hidden');
                // Reseta flag para recarregar kits quando voltar a autenticar
                if (wasAuthenticated) _kitsLoaded = false;
            }
            const al = document.getElementById('auth-link');
            if (al && authUrl) al.href = authUrl;
        }

        /* ✅ DESIGN: Atualizar KPIs com Animação */
        function updateKpis(dSalesStats) {
            const kpiDaily = document.getElementById('kpi-daily');
            const kpiWeekly = document.getElementById('kpi-weekly');
            const kpiHistoric = document.getElementById('kpi-historic');

            kpiDaily.textContent = dSalesStats.daily;
            kpiWeekly.textContent = dSalesStats.weekly;
            kpiHistoric.textContent = dSalesStats.monthly;
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

        /* ✅ DESIGN: Lista Técnica Hardcoded (Engenharia) */
        const RECIPE_CADEIRA = [
            {"nome": "COMPENSADO 50X52X17", "qtd": 1, "un": "Peça"},
            {"nome": "SARRAFO 52", "qtd": 3, "un": "Peças"},
            {"nome": "SARRAFO 46", "qtd": 1, "un": "Peça"},
            {"nome": "SARRAFO 14", "qtd": 2, "un": "Peças"},
            {"nome": "MDF 15MM 52X35", "qtd": 2, "un": "Peças"},
            {"nome": "MDF 6MM 52X35", "qtd": 2, "un": "Peças"},
            {"nome": "SARRAFO 33", "qtd": 2, "un": "Peças"},
            {"nome": "SARRAFO 10", "qtd": 2, "un": "Peças"},
            {"nome": "MDF 15MM", "qtd": 1, "un": "Peça"},
            {"nome": "TECIDO", "qtd": 3, "un": "Metros"},
            {"nome": "ESPUMA ACOPLAGEM", "qtd": 0.5, "un": "Metro"},
            {"nome": "ESPUMA ASSENTO", "qtd": 1, "un": "Unid"},
            {"nome": "ESPUMA ENCOSTO", "qtd": 1, "un": "Unid"},
            {"nome": "ESPUMA CABEÇOTE", "qtd": 1, "un": "Unid"},
            {"nome": "ESPUMA ASSENTO 52X7,5X1", "qtd": 1, "un": "Peça"},
            {"nome": "ESPUMA ASSENTO 54X14X1", "qtd": 1, "un": "Peça"},
            {"nome": "ESPUMA BRAÇO 52X21X1", "qtd": 1, "un": "Peça"},
            {"nome": "ESPUMA BRAÇO 52X35X1", "qtd": 1, "un": "Peça"},
            {"nome": "ESPUMA BRAÇO 35X9,5X1", "qtd": 4, "un": "Peças"},
            {"nome": "ESPUMA BRAÇO 54X9,5X2", "qtd": 2, "un": "Peças"},
            {"nome": "LINHA", "qtd": 1, "un": "Unid"},
            {"nome": "COLA", "qtd": 1, "un": "Unid"},
            {"nome": "LAMINA CROMADA", "qtd": 1, "un": "Unid"},
            {"nome": "LAMINA DE CABEÇOTE", "qtd": 1, "un": "Unid"},
            {"nome": "PARAFUSO 1/4 X 1", "qtd": 15, "un": "Peças"},
            {"nome": "PARAFUSO 1/4 X 2.1/4", "qtd": 8, "un": "Peças"},
            {"nome": "PARAFUSO 5X25", "qtd": 6, "un": "Peças"},
            {"nome": "PORCA GARRA 1/4", "qtd": 20, "un": "Peças"},
            {"nome": "GRAMPO 80/10", "qtd": 1, "un": "Unid"},
            {"nome": "GRAMPO 14/40", "qtd": 1, "un": "Unid"},
            {"nome": "COSTUREIRA", "qtd": 1, "un": "Serviço"},
            {"nome": "EMBALAGEM", "qtd": 1, "un": "Unid"},
            {"nome": "BASE", "qtd": 1, "un": "Unid"}
        ];

        /* ✅ DESIGN: Abrir Checklist de Produção com Cronômetro */
        let timerInterval = null;

        function openProductionChecklist(productName, timerKey) {
            // timerKey é o identificador único do timer (pode ser "nome||item_key" ou apenas "nome")
            const _timerKey = timerKey || productName;
            const isCadeira = productName.toUpperCase().includes('CADEIRA');
            let checklistHtml = '';

            if (isCadeira) {
                // encodedTimerKey: identifica o timer no servidor (pode ser "nome||item_key")
                // encodedProductName: nome legível para registro de consumo
                const encodedTimerKey   = encodeURIComponent(_timerKey);
                const encodedProductName = encodeURIComponent(productName);
                checklistHtml = `
                    <h6 class="text-muted mb-3">📋 Marque o que foi retirado/usado para esta unidade</h6>
                    <div class="row g-2 mb-4" style="max-height: 320px; overflow-y: auto;">
                        ${RECIPE_CADEIRA.map((item, i) => `
                            <div class="col-md-6">
                                <div class="form-check p-2 border rounded bg-white d-flex align-items-center gap-2 checklist-item" 
                                     id="checklist-row-${i}"
                                     style="cursor:pointer; transition: background 0.2s, border-color 0.2s;">
                                    <input class="form-check-input ms-1" type="checkbox" id="check${i}"
                                        onchange="handleChecklistChange(this, ${i}, '${encodedTimerKey}', '${encodedProductName}')">
                                    <label class="form-check-label flex-grow-1 small fw-bold mb-0" for="check${i}" style="cursor:pointer;">
                                        ${item.nome} 
                                        <span class="badge bg-light text-dark border float-end">${item.qtd} ${item.un}</span>
                                    </label>
                                </div>
                            </div>
                        `).join('')}
                    </div>
                    <div id="checklist-progress" class="alert alert-info py-2 small mb-0">
                        <strong>0 / ${RECIPE_CADEIRA.length}</strong> itens marcados como usados
                    </div>
                `;
            } else {
                checklistHtml = `<div class="alert alert-secondary">Este produto não possui lista técnica automática de insumos.</div>`;
            }

            const modalHtml = `
                <div class="modal fade" id="productionModal" tabindex="-1" data-bs-backdrop="static">
                    <div class="modal-dialog modal-lg modal-dialog-centered">
                        <div class="modal-content border-0 shadow-2xl">
                            <div class="modal-header text-white" style="background: linear-gradient(135deg, #1e293b 0%, #334155 100%);">
                                <h5 class="modal-title">🛠️ Produção: ${productName}</h5>
                                <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" onclick="clearInterval(timerInterval)"></button>
                            </div>
                            <div class="modal-body" style="background: #f8fafc;">
                                <!-- Timer Section -->
                                <div class="card mb-4 border-0" style="background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); color: white;">
                                    <div class="card-body text-center py-4">
                                        <div class="text-uppercase small fw-bold mb-2" style="letter-spacing:.1em; opacity:.7;">⏱ Tempo de Produção</div>
                                        <div id="timer-display" class="fw-bold font-monospace mb-3" style="font-size: 3.5rem; letter-spacing:.05em; text-shadow: 0 0 20px rgba(99,102,241,.6);">
                                            00:00:00
                                        </div>
                                        <div id="timer-status" class="badge mb-3" style="font-size:.85rem; padding:.4rem 1rem;">Parado</div>
                                        <div class="d-flex justify-content-center gap-2" id="timer-btn-group"
                                             data-produto="${encodeURIComponent(_timerKey)}"
                                             data-display="${encodeURIComponent(productName)}">
                                            <button class="btn btn-success px-4 fw-bold" onclick="controlTimer('start', decodeURIComponent(document.getElementById('timer-btn-group').dataset.produto))">
                                                ▶ Iniciar
                                            </button>
                                            <button class="btn btn-warning px-4 fw-bold text-dark" onclick="controlTimer('pause', decodeURIComponent(document.getElementById('timer-btn-group').dataset.produto))">
                                                ⏸ Pausar
                                            </button>
                                            <button class="btn btn-outline-light px-4" onclick="controlTimer('reset', decodeURIComponent(document.getElementById('timer-btn-group').dataset.produto))">
                                                ↺ Zerar
                                            </button>
                                        </div>
                                    </div>
                                </div>

                                <!-- Checklist -->
                                ${checklistHtml}
                            </div>
                            <div class="modal-footer bg-white d-flex justify-content-between">
                                <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal" onclick="clearInterval(timerInterval)">
                                    Fechar
                                </button>
                                <button type="button" class="btn btn-success px-4 fw-bold"
                                    onclick="controlTimer('finish', decodeURIComponent(document.getElementById('timer-btn-group').dataset.produto))">
                                    ✅ CONCLUIR & SALVAR
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            `;

            const oldModal = document.getElementById('productionModal');
            if (oldModal) oldModal.remove();
            document.body.insertAdjacentHTML('beforeend', modalHtml);
            
            const modal = new bootstrap.Modal(document.getElementById('productionModal'));
            modal.show();

            // Carrega estado do timer (usa timer_key) e checklist (usa timer_key para encontrar o estado correto)
            controlTimer('get', _timerKey);
            _loadChecklistState(productName, _timerKey);
        }

        const checklistState = {};

        async function _loadChecklistState(productName, timerKey) {
            // Usa timerKey se disponível (para encontrar checklist salvo no timer correto)
            const keyToLoad = timerKey || productName;
            try {
                const safe = encodeURIComponent(keyToLoad);
                const res = await fetch(`/api/checklist/state/${safe}`);
                const data = await res.json();
                const saved = data.checklist || {};
                // Restaura checkboxes marcados
                RECIPE_CADEIRA.forEach((item, i) => {
                    if (saved[item.nome]) {
                        const cb = document.getElementById(`check${i}`);
                        const row = document.getElementById(`checklist-row-${i}`);
                        if (cb) {
                            cb.checked = true;
                            if (row) {
                                row.style.background = '#d1fae5';
                                row.style.borderColor = '#10b981';
                            }
                        }
                    }
                });
                _updateChecklistProgress();
            } catch(e) { console.error('Erro ao carregar checklist:', e); }
        }

        function _updateChecklistProgress() {
            const total = RECIPE_CADEIRA.length;
            const checked = document.querySelectorAll('#productionModal .form-check-input:checked').length;
            const progressDiv = document.getElementById('checklist-progress');
            if (progressDiv) {
                progressDiv.innerHTML = '<strong>' + checked + ' / ' + total + '</strong> itens marcados' + (checked === total ? ' ✅ Tudo marcado!' : '');
                progressDiv.className = 'alert py-2 small mb-0 ' + (checked === total ? 'alert-success' : 'alert-info');
            }
        }

        function handleChecklistChange(cb, idx, encodedTimerKey, encodedProductName) {
            // encodedTimerKey: chave do timer no servidor (para salvar estado do checklist)
            // encodedProductName: nome legível (para registrar consumo)
            const timerKey   = decodeURIComponent(encodedTimerKey);
            const productName = encodedProductName ? decodeURIComponent(encodedProductName) : timerKey.split('||')[0];
            const isChecked = cb.checked;
            const item = RECIPE_CADEIRA[idx];
            const row = document.getElementById('checklist-row-' + idx);

            if (row) {
                if (isChecked) {
                    row.style.background = '#d1fae5';
                    row.style.borderColor = '#10b981';
                } else {
                    row.style.background = '';
                    row.style.borderColor = '';
                }
            }

            // Salva estado da checklist no servidor usando o timerKey correto
            fetch('/api/checklist/state', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ produto: timerKey, componente: item.nome, checked: isChecked })
            }).catch(e => console.error('Erro ao salvar checklist:', e));

            // Registra consumo usando o nome legível do produto
            registerConsumption(item.nome, item.qtd, item.un, productName, isChecked);
            _updateChecklistProgress();
        }

        function toggleChecklist(container, idx, productName, timerKey) {
            const cb = container.querySelector('input[type=checkbox]');
            const tkey = timerKey || productName;
            if (cb) handleChecklistChange(cb, idx, encodeURIComponent(tkey), encodeURIComponent(productName));
        }

        async function registerConsumption(componentName, qty, unit, productName, checked) {
            try {
                const res = await fetch('/api/consumption/register', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ component_name: componentName, qty: qty, unit: unit, product_name: productName, checked: checked })
                });
                if (!res.ok) throw new Error('HTTP ' + res.status);
                const tab = document.getElementById('component-usage');
                if (tab && tab.classList.contains('active')) {
                    setTimeout(() => fetchAPI('/api/consumption/summary').then(d => renderConsumptionTable(d)).catch(() => {}), 400);
                }
            } catch(e) {
                console.error('Erro ao registrar consumo:', e);
                showToast('Aviso', 'Falha ao registrar insumo', 'warning');
            }
        }

        /* Lógica do Timer Conectada ao Backend */
        async function controlTimer(action, produto) {
            try {
                const res = await fetch('/api/timer/action', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ action: action, produto: produto })
                });
                if (!res.ok) throw new Error('HTTP ' + res.status);
                const data = await res.json();

                if (action === 'finish') {
                    clearInterval(timerInterval);
                    timerInterval = null;
                    const elapsed = (data.registro ? data.registro.tempo_segundos : null) || data.elapsed || 0;

                    // ── Animação de conclusão ──────────────────────────────
                    const modalEl = document.getElementById('productionModal');
                    if (modalEl) {
                        const modalContent = modalEl.querySelector('.modal-content');
                        const modalBody    = modalEl.querySelector('.modal-body');
                        const modalFooter  = modalEl.querySelector('.modal-footer');

                        // Congela botões imediatamente
                        if (modalFooter) modalFooter.style.display = 'none';

                        // Substitui o body pelo painel de sucesso
                        if (modalBody) {
                            modalBody.innerHTML = `
                                <div id="finish-anim" style="
                                    display:flex; flex-direction:column; align-items:center;
                                    justify-content:center; min-height:320px; gap:1rem;
                                    background:linear-gradient(135deg,#064e3b 0%,#065f46 100%);
                                    border-radius:0 0 12px 12px;">

                                    <!-- Ícone animado -->
                                    <div id="fa-icon" style="
                                        width:90px; height:90px; border-radius:50%;
                                        background:rgba(255,255,255,.12);
                                        display:flex; align-items:center; justify-content:center;
                                        font-size:3rem;
                                        animation: fa-pop .4s cubic-bezier(.34,1.56,.64,1) both;">
                                        ✅
                                    </div>

                                    <!-- Título -->
                                    <div style="color:#fff; font-size:1.5rem; font-weight:800;
                                                letter-spacing:.02em; text-align:center;
                                                animation: fa-fadein .3s .2s both;">
                                        Produção Concluída!
                                    </div>

                                    <!-- Produto -->
                                    <div style="color:#6ee7b7; font-size:1rem; font-weight:600;
                                                text-align:center; max-width:280px;
                                                animation: fa-fadein .3s .35s both;">
                                        ${produto.split('||')[0]}
                                    </div>

                                    <!-- Tempo registrado -->
                                    <div style="
                                        background:rgba(255,255,255,.1); border-radius:12px;
                                        padding:.75rem 2rem; text-align:center;
                                        animation: fa-fadein .3s .45s both;">
                                        <div style="color:rgba(255,255,255,.6); font-size:.7rem;
                                                    text-transform:uppercase; letter-spacing:.1em;">
                                            Tempo registrado
                                        </div>
                                        <div style="color:#fff; font-size:2.2rem; font-weight:700;
                                                    font-family:monospace; letter-spacing:.05em;
                                                    text-shadow:0 0 20px rgba(110,231,183,.5);">
                                            ${formatSeconds(elapsed)}
                                        </div>
                                    </div>

                                    <!-- Componentes registrados -->
                                    <div style="color:rgba(255,255,255,.55); font-size:.82rem;
                                                text-align:center;
                                                animation: fa-fadein .3s .55s both;">
                                        📦 Insumos computados automaticamente
                                    </div>
                                </div>

                                <style>
                                @keyframes fa-pop {
                                    0%   { transform: scale(0); opacity:0; }
                                    100% { transform: scale(1); opacity:1; }
                                }
                                @keyframes fa-fadein {
                                    from { opacity:0; transform:translateY(10px); }
                                    to   { opacity:1; transform:translateY(0); }
                                }
                                </style>
                            `;
                        }

                        // Fecha após 2.2s
                        setTimeout(() => {
                            try {
                                const bsModal = bootstrap.Modal.getInstance(modalEl);
                                if (bsModal) bsModal.hide();
                            } catch {}
                            setTimeout(() => {
                                modalEl.remove();
                                document.querySelectorAll('.modal-backdrop').forEach(e => e.remove());
                                document.body.classList.remove('modal-open');
                                document.body.style.removeProperty('overflow');
                                document.body.style.removeProperty('padding-right');
                            }, 300);
                        }, 2200);
                    }

                    await loadProductionBoard();
                    await refreshComponentTab();
                    return;
                }

                updateTimerDisplay(data.elapsed || 0, data.state || 'stopped');
                if (action === 'start' || (action === 'get' && data.state === 'running')) {
                    startLocalCounter(data.elapsed || 0);
                } else {
                    clearInterval(timerInterval);
                    timerInterval = null;
                }
            } catch (e) {
                console.error("Erro no timer:", e);
                showToast('Erro', 'Falha ao comunicar com servidor.', 'danger');
            }
        }

        function formatSeconds(s) {
            s = Math.floor(s || 0);
            const h = Math.floor(s / 3600).toString().padStart(2,'0');
            const m = Math.floor((s % 3600) / 60).toString().padStart(2,'0');
            const sec = (s % 60).toString().padStart(2,'0');
            return `${h}:${m}:${sec}`;
        }

        function startLocalCounter(startSeconds) {
            clearInterval(timerInterval);
            let seconds = Math.floor(startSeconds || 0);
            const display = document.getElementById('timer-display');
            timerInterval = setInterval(() => {
                seconds++;
                if (display) display.textContent = formatSeconds(seconds);
            }, 1000);
        }

        function updateTimerDisplay(seconds, state) {
            const display = document.getElementById('timer-display');
            const badge = document.getElementById('timer-status');
            
            if (display) display.textContent = formatSeconds(seconds);
            
            if(state === 'running') {
                badge.className = 'mt-2 badge bg-success';
                badge.textContent = 'Em Produção...';
                badge.classList.add('pulse-animation');
            } else if (state === 'paused') {
                badge.className = 'mt-2 badge bg-warning text-dark';
                badge.textContent = 'Pausado';
                badge.classList.remove('pulse-animation');
            } else {
                badge.className = 'mt-2 badge bg-secondary';
                badge.textContent = 'Parado';
                badge.classList.remove('pulse-animation');
            }
        }

        /* ════════════════════════════════════════════════════════════
           PAINEL DE PRODUÇÃO UNIFICADO
           - Busca /api/production/board a cada 10s automaticamente
           - Em Espera + Em Produção + Concluídos numa única view
           ════════════════════════════════════════════════════════════ */

        let _boardTick = null;      // setInterval do ticker de tempo
        let _boardPoll = null;      // setInterval do polling de dados
        let _boardTimerState = {};  // snapshot do servidor para ticker local

        async function syncAndRefreshPending() {
            const btn = document.querySelector('[onclick="syncAndRefreshPending()"]');
            if (btn) { btn.disabled = true; btn.textContent = '⏳ Sincronizando...'; }
            try {
                const res = await fetchAPI('/api/pending-orders/sync', { method: 'POST' });
                if (res.message) showToast('Info', res.message, 'info');
                else showToast('Sucesso', `${res.added || 0} novos itens adicionados.`, 'success');
            } catch(e) {
                showToast('Aviso', 'Faça login para sincronizar.', 'warning');
            } finally {
                if (btn) { btn.disabled = false; btn.textContent = '🔄 Sincronizar Bling'; }
            }
            await loadProductionBoard();
        }

        // Mantido como alias para compatibilidade com outros pontos do código
        async function loadPendingOrders() { await loadProductionBoard(); }

        async function loadProductionBoard() {
            const div = document.getElementById('production-board-section');
            if (!div) return;
            try {
                const data = await fetch('/api/production/board').then(r => r.json());
                renderProductionBoard(data);
            } catch(e) {
                div.innerHTML = '<div class="alert alert-danger m-3">Erro ao carregar painel.</div>';
            }
        }

        function renderProductionBoard(data) {
            const div = document.getElementById('production-board-section');
            if (!div) return;

            const waiting    = data.waiting      || [];
            const inProd     = data.in_production || [];
            const orphans    = data.orphan_timers || [];
            const done       = data.done          || [];
            const serverTime = data.server_time   || (Date.now() / 1000);

            // Atualiza badges
            const wb = document.getElementById('waiting-count-badge');
            const ib = document.getElementById('inprod-count-badge');
            const db = document.getElementById('done-count-badge');
            if (wb) wb.textContent = waiting.length;
            if (ib) ib.textContent = inProd.length + orphans.length;
            if (db) db.textContent = done.length;

            // Para ticker anterior
            if (_boardTick) { clearInterval(_boardTick); _boardTick = null; }
            _boardTimerState = {};

            let html = '';

            // ── Em Espera ───────────────────────────────────────────────
            if (waiting.length === 0) {
                html += `<div class="text-center py-3 text-muted"><small>Nenhum pedido em espera. Clique em 🔄 Sincronizar Bling.</small></div>`;
            } else {
                html += `<div class="table-responsive"><table class="table table-hover align-middle mb-0 table-sm">
                    <thead style="background:#fef9c3;"><tr>
                        <th class="ps-3">Produto</th><th>Base</th><th>Cor</th>
                        <th>Pedido Nº</th><th>Cliente</th><th class="text-center">Ação</th>
                    </tr></thead><tbody>`;
                waiting.forEach(item => {
                    const rawNome = item.nome || item.nome_original || 'N/D';
                    const nome = rawNome.replace(/'/g,"&#39;");
                    // Só mostra imagem se for URL real (não placeholder /static/no-image.png)
                    const imgUrl = (item.imagem && item.imagem.startsWith('http') && !item.imagem.includes('no-image')) ? item.imagem : '';
                    const imgTag = imgUrl
                        ? '<img src="' + imgUrl + '" alt="" loading="lazy" style="width:44px;height:44px;object-fit:contain;border-radius:6px;border:1px solid #e2e8f0;margin-right:8px;vertical-align:middle;flex-shrink:0;" onerror="this.remove()">'
                        : '';
                    html += `<tr>
                        <td class="ps-3 fw-bold" style="vertical-align:middle;">${imgTag}<span style="vertical-align:middle;">${nome}</span></td>
                        <td class="text-muted small">${escapeHtml(item.base)}</td>
                        <td class="text-muted small">${escapeHtml(item.cor)}</td>
                        <td class="text-muted small">#${item.pedido_numero || item.order_id}</td>
                        <td class="text-muted small">${escapeHtml(item.cliente)}</td>
                        <td class="text-center">
                            <button class="btn btn-xs btn-success btn-sm fw-bold me-1"
                                data-ikey="${item.item_key}"
                                data-pnome="${(item.nome || item.nome_original || '').replace(/"/g,'')}"
                                onclick="startPendingOrder(this.dataset.ikey, this.dataset.pnome)">▶ Produzir</button>
                            <button class="btn btn-xs btn-outline-secondary btn-sm"
                                data-dkey="${item.item_key}"
                                onclick="dismissPendingOrder(this.dataset.dkey, event)">✕</button>
                        </td>
                    </tr>`;
                });
                html += `</tbody></table></div>`;
            }

            // ── Em Produção ─────────────────────────────────────────────
            const allInProd = [...inProd, ...orphans];
            html += `<div class="border-bottom border-top px-3 py-2 mt-1" style="background:#dcfce7;">
                <small class="fw-bold text-success">⚙️ EM PRODUÇÃO (${allInProd.length})</small>
            </div>`;
            if (allInProd.length === 0) {
                html += `<div class="text-center py-3 text-muted"><small>Nenhuma produção em andamento.</small></div>`;
            } else {
                html += `<div class="table-responsive"><table class="table table-hover align-middle mb-0 table-sm">
                    <thead style="background:#f0fdf4;"><tr>
                        <th class="ps-3">Produto</th><th>Base</th><th>Cor</th>
                        <th class="text-center">⏱ Tempo</th><th class="text-center">Status</th>
                        <th class="text-center">Checklist</th><th class="text-center">Ação</th>
                    </tr></thead><tbody>`;
                allInProd.forEach(item => {
                    const nome    = item.nome || item.nome_original || item.produto || 'N/D';
                    const nomeSafe = nome.replace(/'/g,"&#39;");
                    const safeId  = nome.replace(/[^a-zA-Z0-9]/g,'_');
                    const elapsed = item.tempo_decorrido || 0;
                    const estado  = item.estado || 'paused';
                    const itemKey  = item.item_key || null;
                    const timerKey = item.timer_key || nome;  // chave real do timer (pode ser "nome||item_key")
                    const base    = item.base || '—';
                    const cor     = item.cor  || '—';
                    const chkDone = Object.values(item.checklist || {}).filter(Boolean).length;
                    const chkTotal= RECIPE_CADEIRA.length;

                    // Guarda estado para ticker local com a chave correta
                    _boardTimerState[timerKey] = { base: elapsed, startedAt: Date.now() / 1000, estado, serverTime };

                    const finishBtn = itemKey
                        ? `<button class="btn btn-xs btn-success btn-sm ms-1" data-ikey="${itemKey}" data-pnome="${nomeSafe}" onclick="finishBoardItem(this.dataset.ikey, this.dataset.pnome, event)">✅ Concluir</button>`
                        : `<button class="btn btn-xs btn-success btn-sm ms-1" data-pnome="${nomeSafe}" onclick="controlTimer('finish', this.dataset.pnome)">✅ Concluir</button>`;

                    const imgUrlProd = (item.imagem && item.imagem.startsWith('http') && !item.imagem.includes('no-image')) ? item.imagem : '';
                    const imgTagProd = imgUrlProd ? `<img src="${imgUrlProd}" alt="" loading="lazy" style="width:38px;height:38px;object-fit:contain;border-radius:5px;border:1px solid #e2e8f0;margin-right:6px;vertical-align:middle;flex-shrink:0;" onerror="this.remove()">` : '';
                    html += `<tr>
                        <td class="ps-3 fw-bold" style="vertical-align:middle;">${imgTagProd}<span style="vertical-align:middle;">${nomeSafe}</span></td>
                        <td class="text-muted small">${base}</td>
                        <td class="text-muted small">${cor}</td>
                        <td class="text-center">
                            <span id="btimer_${safeId}" class="font-monospace fw-bold text-primary" style="font-size:1.1rem;">${formatSeconds(elapsed)}</span>
                        </td>
                        <td class="text-center">
                            <span class="badge ${estado === 'running' ? 'bg-success' : 'bg-warning text-dark'}"
                                style="${estado==='running'?'animation:pulse-animation 1.5s infinite;':''}">
                                ${estado === 'running' ? '🟢 PRODUZINDO' : '⏸ PAUSADO'}
                            </span>
                        </td>
                        <td class="text-center">
                            <span class="badge ${chkDone===chkTotal ? 'bg-success' : 'bg-light text-dark border'}">${chkDone}/${chkTotal}</span>
                        </td>
                        <td class="text-center">
                            <button class="btn btn-xs btn-outline-primary btn-sm"
                                data-nome="${nomeSafe}"
                                data-tkey="${encodeURIComponent(timerKey)}"
                                onclick="openProductionChecklist(this.dataset.nome, decodeURIComponent(this.dataset.tkey))">🛠 Abrir</button>
                            ${finishBtn}
                        </td>
                    </tr>`;
                });
                html += `</tbody></table></div>`;
            }

            // ── Concluídos ──────────────────────────────────────────────
            if (done.length > 0) {
                html += `<div class="border-top px-3 py-2 mt-1" style="background:#f0fdf4;">
                    <small class="fw-bold text-success">✅ CONCLUÍDOS ESTE MÊS (${done.length})</small>
                </div>
                <div class="table-responsive"><table class="table table-sm align-middle mb-0">
                    <tbody>`;
                done.slice().reverse().forEach(item => {
                    const nome = (item.nome || item.nome_original || 'N/D').replace(/'/g,'&#39;');
                    const fin  = item.finished_at ? new Date(item.finished_at).toLocaleString('pt-BR') : '—';
                    html += `<tr class="table-success">
                        <td class="ps-3 fw-bold text-success">${nome}</td>
                        <td class="text-muted small">${escapeHtml(item.base)}</td>
                        <td class="text-muted small">${item.cor  || '—'}</td>
                        <td class="text-muted small">#${item.pedido_numero || item.order_id}</td>
                        <td class="text-muted small">${escapeHtml(item.cliente)}</td>
                        <td class="text-center"><span class="badge bg-success">✅ Concluído</span><br><small class="text-muted">${fin}</small></td>
                    </tr>`;
                });
                html += `</tbody></table></div>`;
            }

            div.innerHTML = html;

            // ── Ticker local (1s) ────────────────────────────────────────
            _boardTick = setInterval(() => {
                Object.entries(_boardTimerState).forEach(([tkey, s]) => {
                    if (s.estado !== 'running') return;
                    const elapsed = s.base + (Date.now() / 1000 - s.startedAt);
                    // O safeId do elemento usa o nome do produto (parte antes do ||)
                    const displayNome = tkey.includes('||') ? tkey.split('||')[0] : tkey;
                    const safeId = displayNome.replace(/[^a-zA-Z0-9]/g, '_');
                    const el = document.getElementById('btimer_' + safeId);
                    if (el) el.textContent = formatSeconds(Math.floor(elapsed));
                });
            }, 1000);
        }

        async function startPendingOrder(itemKey, produtoNome) {
            if (!itemKey || !produtoNome) {
                showToast('Erro', 'Dados do pedido inválidos', 'danger');
                return;
            }
            try {
                const res = await fetch('/api/pending-orders/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ item_key: itemKey, produto_nome: produtoNome })
                });
                if (!res.ok) throw new Error('Servidor retornou erro');
                const resData = await res.json();
                // Passa o timer_key real (nome||item_key) para o modal
                const timerKey = resData.timer_key || produtoNome;
                await loadProductionBoard();
                openProductionChecklist(produtoNome, timerKey);
                showToast('✅ Iniciado', `Produção: ${produtoNome}`, 'success');
            } catch(e) {
                console.error('startPendingOrder:', e);
                showToast('Erro', 'Falha ao iniciar produção', 'danger');
            }
        }

        async function finishBoardItem(itemKey, produtoNome, evt) {
            // Confirmação inline sem confirm() bloqueante
            const btn = (evt && evt.target) || (typeof event !== 'undefined' && event && event.target) || null;
            if (btn && btn.dataset.confirming !== 'true') {
                btn.dataset.confirming = 'true';
                const orig = btn.textContent;
                btn.textContent = '❓ Confirmar?';
                btn.classList.replace('btn-success', 'btn-warning');
                setTimeout(() => {
                    if (btn.dataset.confirming === 'true') {
                        btn.dataset.confirming = '';
                        btn.textContent = orig;
                        btn.classList.replace('btn-warning', 'btn-success');
                    }
                }, 3000);
                return;
            }
            if (btn) { btn.dataset.confirming = ''; btn.disabled = true; }
            try {
                await fetch('/api/pending-orders/finish', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ item_key: itemKey, produto_nome: produtoNome })
                });
                showToast('✅ Concluído!', produtoNome, 'success');
                await loadProductionBoard();
                await refreshComponentTab();
            } catch(e) {
                showToast('Erro', 'Falha ao concluir', 'danger');
                if (btn) { btn.disabled = false; }
            }
        }

        async function dismissPendingOrder(itemKey, evt) {
            const btn = (evt && evt.target) || (typeof event !== 'undefined' && event && event.target) || null;
            if (btn && btn.dataset.confirming !== 'true') {
                btn.dataset.confirming = 'true';
                const orig = btn.textContent;
                btn.textContent = '❓ Confirmar?';
                btn.classList.replace('btn-outline-danger', 'btn-danger');
                setTimeout(() => {
                    if (btn.dataset.confirming === 'true') {
                        btn.dataset.confirming = '';
                        btn.textContent = orig;
                        btn.classList.replace('btn-danger', 'btn-outline-danger');
                    }
                }, 3000);
                return;
            }
            if (btn) { btn.dataset.confirming = ''; btn.disabled = true; }
            try {
                await fetch('/api/pending-orders/dismiss', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ item_key: itemKey })
                });
                await loadProductionBoard();
            } catch(e) {
                showToast('Erro', 'Falha ao remover pedido', 'danger');
                if (btn) btn.disabled = false;
            }
        }

        function updateComponentUsage(usageData) {
            if (usageData && usageData.history_production) renderProductionHistory(usageData.history_production);
        }

        async function refreshComponentTab() {
            await loadProductionBoard();
            try {
                const consumptionData = await fetchAPI('/api/consumption/summary');
                renderConsumptionTable(consumptionData);
            } catch(e) {
                document.getElementById('consumption-table-section').innerHTML =
                    '<div class="alert alert-danger m-3">Erro ao carregar consumo.</div>';
            }
            try {
                const usageData = await fetchAPI('/api/components/usage');
                if (usageData.history_production) renderProductionHistory(usageData.history_production);
            } catch(e) { console.error('Erro ao carregar histórico:', e); }
        }

        function renderActiveTimers(activeProduction) {}

        function renderConsumptionTable(data) {
            const tableSection = document.getElementById('consumption-table-section');
            const monthLabel = document.getElementById('consumption-month-label');
            const totalBadge = document.getElementById('consumption-total-badge');
            if (!tableSection) return;
            const monthStr = data.month || '';
            const [year, month] = monthStr.split('-');
            const monthNames = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez'];
            const monthName = month ? `${monthNames[parseInt(month)-1]}/${year}` : monthStr;
            if (monthLabel) monthLabel.textContent = `${monthName} • Reinicia todo mês`;
            const summary = data.summary || [];
            if (totalBadge) totalBadge.textContent = `${summary.length} insumos registrados`;
            if (summary.length === 0) {
                tableSection.innerHTML = `<div class="text-center py-5"><div style="font-size:3rem;opacity:.3;">📦</div><p class="text-muted mt-2">Nenhum insumo registrado ainda este mês.</p><small class="text-muted">Abra um produto e marque os itens na checklist para registrar o consumo.</small></div>`;
                return;
            }
            tableSection.innerHTML = `<div class="table-responsive"><table class="table table-hover align-middle mb-0">
                <thead style="background:#f8fafc;"><tr><th class="ps-3">Insumo / Componente</th><th class="text-center">Qtd Usada (Mês)</th><th class="text-center">Un.</th><th class="text-center">Registros</th></tr></thead>
                <tbody>${summary.map(item => `<tr>
                    <td class="ps-3 fw-bold">${item.nome}</td>
                    <td class="text-center"><span class="badge fs-6" style="background:linear-gradient(135deg,#059669,#10b981);color:white;padding:.4rem .9rem;">${item.qtd_total}</span></td>
                    <td class="text-center text-muted small">${item.un}</td>
                    <td class="text-center"><span class="badge bg-light text-dark border">${item.num_registros}x</span></td>
                </tr>`).join('')}</tbody></table></div>`;
        }

        function renderProductionHistory(history) {
            const div = document.getElementById('production-history-section');
            if (!div) return;
            const reversed = [...(history || [])].reverse();
            if (reversed.length === 0) {
                div.innerHTML = '<div class="text-center py-4 text-muted">Nenhum produto finalizado este mês.</div>';
                return;
            }
            div.innerHTML = `<div class="table-responsive" style="max-height:320px;overflow-y:auto;"><table class="table table-sm table-striped align-middle mb-0">
                <thead class="table-dark sticky-top"><tr><th class="ps-3">Data/Hora</th><th>Produto</th><th class="text-center">Tempo de Produção</th></tr></thead>
                <tbody>${reversed.map(h => `<tr>
                    <td class="ps-3 small text-muted">${new Date(h.data_conclusao).toLocaleString('pt-BR')}</td>
                    <td class="fw-bold">${escapeHtml(h.produto)}</td>
                    <td class="text-center font-monospace fw-bold text-primary">${formatSeconds(h.tempo_segundos)}</td>
                </tr>`).join('')}</tbody></table></div>`;
        }

        /* WebSocket KPI com reconexão e backoff */
        let wsKpi = null;
        let _kpiReconnectDelay = 3000; // declarado fora para persistir entre reconexões

        function _connectKpiWs() {
            const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
            wsKpi = new WebSocket(`${proto}://${window.location.host}/ws/kpi-updates`);
            setupKpiWebSocket();
        }

        function setupKpiWebSocket() {
            wsKpi.onopen = () => {
                _kpiReconnectDelay = 3000; // reset ao conectar com sucesso
            };

            wsKpi.onmessage = (e) => {
                let data;
                try { data = JSON.parse(e.data); } catch { return; }

                if (data.type === 'full_update') {
                    updateAuthStatus(data.authenticated, data.auth_url);

                    if (data.sales_stats) updateKpis(data.sales_stats);
                    if (data.component_usage) updateComponentUsage(data.component_usage);

                    const forceLoadButton = document.querySelector('#kits button.btn-primary');
                    if (forceLoadButton && forceLoadButton.disabled && data.cache_updated) {
                        forceLoadButton.disabled = false;
                        forceLoadButton.textContent = '🔄 Recarregar Lista';
                        loadKits();
                        showToast('Sucesso', 'Cache de produtos/kits atualizado.', 'success');
                    }
                }
            };

            wsKpi.onerror = () => { /* silencioso — onclose vai reconectar */ };

            wsKpi.onclose = () => {
                setTimeout(() => {
                    _kpiReconnectDelay = Math.min(_kpiReconnectDelay * 1.5, 30000);
                    _connectKpiWs();
                }, _kpiReconnectDelay);
            };
        }

        _connectKpiWs();

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
                const data = await fetchAPI(`${API}/products/search?q=${encodeURIComponent(q)}`);

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
                        <div class="list-group-item list-group-item-action" onclick="openProductionChecklist('${p.nome || p.produto}')" style="cursor: pointer;">
                            <div class="d-flex">
                                ${imgHtml}

                                <div class="flex-grow-1">
                                    <div class="d-flex w-100 justify-content-between">
                                        <h5 class="mb-1">${p.nome || p.produto || 'Sem nome'}</h5>
                                        <small>${p.sku || 'N/D'}</small>
                                    </div>

                                    <p class="mb-1">${p.descricaoCurta || ''}</p>

                                    <small class="text-muted d-block">
                                        <b>Tipo:</b> ${p.tipo}
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
                        comps = `<span class="badge bg-light text-dark border">Produto Cadastrado</span>`;
                        if (k.pai_id) {
                            comps += `<br><span class="badge bg-secondary">Variação</span>`;
                        }
                    } else {
                        comps = '<span class="badge bg-secondary">Tipo Desconhecido</span>';
                    }

                    html += `
                        <tr onclick="openProductionChecklist('${k.nome}')" style="cursor: pointer;">
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
                if (e.message === '401') {
                    div.innerHTML = '<div class="alert alert-warning">🔐 Sessão expirada. <a href="/auth">Clique aqui para reautenticar</a>.</div>';
                } else {
                    div.innerHTML = '<div class="alert alert-danger">⚠️ Erro ao carregar lista. Verifique os logs do servidor.</div>';
                }
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

        /* Inicialização — loadKits só após autenticação confirmada via WS */
        let _kitsLoaded = false;

        function _onAuthConfirmed() {
            if (!_kitsLoaded) {
                _kitsLoaded = true;
                loadKits();
            }
        }

        /* Inicializa conexão WS após declarar todas as funções */

        document.addEventListener('DOMContentLoaded', () => {
            const kpiTab = document.querySelector('[data-bs-target="#kpi-chart"]');
            if (kpiTab) kpiTab.addEventListener('shown.bs.tab', loadKPIChart);

            const componentUsageTab = document.querySelector('[data-bs-target="#component-usage"]');
            if (componentUsageTab) {
                componentUsageTab.addEventListener('shown.bs.tab', () => {
                    refreshComponentTab();
                    loadProductionBoard();
                    if (!_boardPoll) {
                        _boardPoll = setInterval(loadProductionBoard, 10000);
                    }
                });
                componentUsageTab.addEventListener('hidden.bs.tab', () => {
                    if (_boardPoll) { clearInterval(_boardPoll); _boardPoll = null; }
                    if (_boardTick) { clearInterval(_boardTick); _boardTick = null; }
                });
            }
        });
    </script>

    <!-- FOOTER -->
    <footer class="bg-primary text-white mt-5 py-4">
        <div class="container-fluid px-4">
            <div class="row align-items-center">
                <div class="col-md-6">
                    <p class="mb-0">
                        <strong style="color:var(--sw-yellow)">SW Móveis MDF</strong> — Gestão Inteligente de Pedidos
                    </p>
                    <small class="text-white-50">© 2025 — Desenvolvido por João Victor Dias Santana</small>
                </div>
                <div class="col-md-6 text-md-end">
                    <p class="mb-0">
                        <strong style="color:var(--sw-yellow)">Versão</strong> 4.6
                    </p>
                    <small class="text-white-50">Sistema Integrado Bling API v3</small>
                </div>
            </div>
        </div>
    </footer>
    <div class="sw-pattern-bar"></div>

</body>
</html>"""

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
    
    # ✅ REGRA DE OURO: Define uma SECRET_KEY estável para persistência de sessão
    # Isso evita que o Flask invalide cookies a cada reinício do servidor.
    flask_app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'sw-moveis-mdf-secure-key-2025')
    
    # 4. Inicializa o WebServer (Rotas e WebSockets)
    WebServer(config, orchestrator, flask_app)

    # 5. Inicia worker automaticamente se já existe token salvo.
    #    Garante que após reinício do servidor (Render, deploy, idle)
    #    o sistema volte ao ar sem pedir reautenticação desnecessária.
    def _try_auto_start():
        try:
            time.sleep(2)  # aguarda Flask terminar de subir
            if orchestrator.is_running():
                return
            orchestrator.auth.reload_tokens_from_disk()
            # Renova via refresh_token se o access_token expirou
            if (orchestrator.auth._refresh_token and not (
                    orchestrator.auth._access_token and
                    orchestrator.auth._expires_at > time.time() + 60)):
                logger.info("🔄 Auto-start: renovando token via refresh_token...")
                orchestrator.auth.refresh_token()
            # Inicia apenas se houver token válido
            if (orchestrator.auth._access_token and
                    orchestrator.auth._expires_at > time.time() + 60):
                orchestrator.start_worker()
                start_cleanup_timer()
                logger.info("✅ Worker iniciado automaticamente — token recuperado do storage.")
            else:
                logger.info("ℹ️  Nenhum token válido — aguardando autenticação OAuth.")
        except Exception as e:
            logger.warning(f"Auto-start: não foi possível iniciar o worker: {e}")

    Thread(target=_try_auto_start, daemon=True, name="auto_start").start()

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