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
    
    # Define o log principal para INFO (ou DEBUG se necessário, mas INFO é o padrão)
    logger = logging.getLogger('bling_automacao')
    
    logger.setLevel(logging.DEBUG)  # DEBUG temporário para investigação
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
    WEBHOOK_SECRET: str = os.environ.get('BLING_WEBHOOK_SECRET', 'YOUR_WEBHOOK_SECRET')
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
        
        # --- DEBUG: log de entrada da requisição ---
        self.logger.debug(f"API REQ -> {method} {url} params={kwargs.get('params')} json_keys={list(kwargs.get('json', {}).keys()) if kwargs.get('json') else None}")

        # Rate Limiter
        self.rate_limiter.wait()
        
        try:
            start_time = time.time()
            # Timeout aumentado para evitar quedas em queries lentas do Bling
            response = self.session.request(method, url, timeout=45, **kwargs)
            latency = time.time() - start_time
            
            # DEBUG: log de status e tamanho do body
            text_len = len(response.text) if response.text else 0
            self.logger.debug(f"API RESP <- {method} {url} status={response.status_code} text_len={text_len}")

            self.metrics.record_request(response.status_code, latency)
            
            # tenta parse do JSON e logar keys top-level (para entender formato)
            try:
                resp_json = response.json()
                if isinstance(resp_json, dict):
                    self.logger.debug(f"API JSON KEYS: {list(resp_json.keys())}")
                else:
                    self.logger.debug(f"API JSON TYPE: {type(resp_json)}")
            except Exception as e:
                self.logger.debug(f"API JSON parse failed: {e}")

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

# ============================================================================ 
# 5. AUTH MANAGER
# ============================================================================

    def register_webhook(self, event: str, url: str):
        """
        Nota: Na API v3 do Bling, o registro de webhooks deve ser feito manualmente 
        no painel do desenvolvedor (Cadastro de Aplicativos > Webhooks).
        Esta função foi mantida para compatibilidade, mas agora apenas loga a instrução.
        """
        self.logger.info(f"📢 Lembrete: Configure o webhook para '{event}' manualmente no painel do Bling apontando para: {url}")
        return {"status": "manual_config_required"}

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
        
        # --- ADICIONE ESTA VERIFICAÇÃO ---
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
        """
        Recarrega os tokens do disco para a memória.
        Útil após OAuth ou quando outro processo atualizou os tokens.
        """
        logger.debug("🔄 [DEBUG-AUTH] Recarregando tokens do disco...")
        
        try:
            disk_tokens = self._load_tokens()
            
            self._access_token = disk_tokens.get('access_token')
            self._refresh_token = disk_tokens.get('refresh_token')
            self._expires_at = disk_tokens.get('expires_at', 0)
            
            logger.debug(f"✅ [DEBUG-AUTH] Tokens recarregados:")
            logger.debug(f"   • Access Token: {'Presente' if self._access_token else 'Ausente'}")
            logger.debug(f"   • Refresh Token: {'Presente' if self._refresh_token else 'Ausente'}")
            logger.debug(f"   • Expira em: {self._expires_at - time.time():.0f}s")
            
            logger.info("✅ Tokens recarregados do disco com sucesso!")
            return True
            
        except Exception as e:
            logger.error(f"❌ [DEBUG-AUTH] Erro ao recarregar tokens: {str(e)}", exc_info=True)
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
    stats_history: Dict[str, Any] = field(default_factory=lambda: {'dates': [], 'daily_counts': [], 'moving_avg': [], 'growth': 0})
    
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
                self.stats_history = data.get('stats_history', {'dates': [], 'daily_counts': [], 'moving_avg': [], 'growth': 0})
                self._orders_cache = data.get('orders_cache', {})
                self._sales_history = data.get('sales_history', [])
                
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
                "orders_cache": self._orders_cache,
                "sales_history": self._sales_history,
                "last_recalculated": self.last_recalculated.isoformat()
            }

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
        
        # --- MUDANÇA AQUI: Janela móvel de 30 dias para o Gráfico ---
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
            except:
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
        self.logger.info(f"✅ Estatísticas atualizadas: D:{self.daily_count} W:{self.weekly_count} M:{self.monthly_count}")

class ProductionTimer:
    """Gerencia cronômetros de produção e histórico detalhado."""
    FILE_PATH = DATA_DIR / 'production_timers.json'
    HISTORY_PATH = DATA_DIR / 'production_history.json'

    def __init__(self):
        self.timers = self._load()
        self._auto_pause_on_restart()
        # Lança savers para TODOS os timers existentes (running ou paused)
        for nome in list(self.timers.keys()):
            self._launch_background_saver(nome)

    def _load(self):
        # Tenta MongoDB primeiro, fallback para arquivo
        if MONGO_AVAILABLE:
            try:
                data = MongoStore.get('production_timers', 'timers')
                return data.get('timers', {})
            except Exception:
                pass
        if not self.FILE_PATH.exists(): return {}
        try:
            with open(self.FILE_PATH, 'r') as f: return json.load(f)
        except: return {}

    def _save(self):
        """Salva no MongoDB (principal) e arquivo (fallback)."""
        if MONGO_AVAILABLE:
            try:
                MongoStore.set('production_timers', {'timers': self.timers}, 'timers')
                return
            except Exception as e:
                logger.error(f"Erro ao salvar timers no MongoDB: {e}")
        # Fallback arquivo
        temp_file = self.FILE_PATH.with_suffix('.tmp')
        try:
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self.timers, f, indent=4)
            shutil.move(str(temp_file), str(self.FILE_PATH))
        except Exception as e:
            logger.error(f"Erro ao salvar timers em arquivo: {e}")

    def _auto_pause_on_restart(self):
        changed = False
        now = time.time()
        for k, v in self.timers.items():
            # Migração: timers antigos não tinham campo 'produto'
            if 'produto' not in v:
                v['produto'] = k
                changed = True
            if v.get('state') == 'running':
                start_ts = v.get('start_ts', 0)
                if start_ts and start_ts > 0:
                    v['accumulated'] = v.get('accumulated', 0) + (now - start_ts)
                v['state'] = 'paused'
                v['start_ts'] = 0
                changed = True
        if changed:
            self._save()
            n = sum(1 for v in self.timers.values() if v.get('state') == 'paused')
            logger.info(f"Restart: {n} timers pausados com tempo preservado.")

    def start(self, timer_key, produto_nome=None):
        """Cria/retoma timer. timer_key=item_key para pedidos, produto_nome para timers manuais."""
        now = time.time()
        if not produto_nome:
            produto_nome = timer_key
        if timer_key not in self.timers:
            self.timers[timer_key] = {
                'produto': produto_nome,
                'start_ts': now,
                'accumulated': 0,
                'state': 'running',
                'created_at': datetime.now().isoformat(),
                'checklist': {}
            }
        else:
            t = self.timers[timer_key]
            t['produto'] = produto_nome
            if t['state'] != 'running':
                t['start_ts'] = now
                t['state'] = 'running'
        self._save()
        self._launch_background_saver(timer_key)
        return self.get_status(timer_key)

    def _launch_background_saver(self, nome):
        """Thread que faz checkpoint do timer a cada 30s enquanto existir (running ou paused)."""
        def background_saver():
            while True:
                time.sleep(30)
                if nome not in self.timers:
                    break  # Timer foi removido (concluído/zerado)
                t = self.timers[nome]
                if t.get('state') == 'running' and t.get('start_ts', 0) > 0:
                    now_ts = time.time()
                    t['accumulated'] = t.get('accumulated', 0) + (now_ts - t['start_ts'])
                    t['start_ts'] = now_ts
                try:
                    self._save()
                except Exception as e:
                    logger.error(f"background_saver erro: {e}")
        Thread(target=background_saver, daemon=True, name=f"saver_{nome}").start()

    def pause(self, timer_key):
        if timer_key in self.timers and self.timers[timer_key].get('state') == 'running':
            t = self.timers[timer_key]
            t['accumulated'] = t.get('accumulated', 0) + (time.time() - t.get('start_ts', 0))
            t['start_ts'] = 0
            t['state'] = 'paused'
            self._save()
        return self.get_status(timer_key)

    def stop_and_log(self, timer_key, produto_nome=None):
        """Finaliza producao. timer_key=item_key (pedido) ou produto_nome (manual)."""
        checklist_marcado = {}
        if timer_key in self.timers:
            t = self.timers[timer_key]
            if not produto_nome:
                produto_nome = t.get('produto', timer_key)
            checklist_marcado = t.get('checklist', {})
            status = self.pause(timer_key)
            total_seconds = status['elapsed']
        else:
            if not produto_nome:
                produto_nome = timer_key
            total_seconds = 0

        # Registra componentes nao marcados manualmente
        if 'CADEIRA' in produto_nome.upper():
            for comp in RECIPE_CADEIRA:
                nome_comp = comp['nome']
                if not checklist_marcado.get(nome_comp, False):
                    try:
                        component_consumption.register_component(
                            nome_comp, comp['qtd'], comp['un'], produto_nome
                        )
                    except Exception as e:
                        logger.error(f"Auto-registro componente '{nome_comp}': {e}")
            logger.info(f"Componentes registrados para '{produto_nome}'")

        registro = {
            "produto": produto_nome,
            "timer_key": timer_key,
            "tempo_segundos": total_seconds,
            "data_conclusao": datetime.now().isoformat(),
            "timestamp": time.time(),
            "checklist": checklist_marcado
        }
        self._add_to_history(registro)

        if timer_key in self.timers:
            del self.timers[timer_key]
            self._save()

        return {'elapsed': total_seconds, 'state': 'finished', 'registro': registro}

    def reset(self, timer_key):
        if timer_key in self.timers:
            del self.timers[timer_key]
            self._save()
        return {'elapsed': 0, 'state': 'stopped'}

    def get_status(self, timer_key):
        if timer_key not in self.timers:
            return {'elapsed': 0, 'state': 'stopped'}
        t = self.timers[timer_key]
        total = t.get('accumulated', 0)
        if t.get('state') == 'running' and t.get('start_ts', 0) > 0:
            total += (time.time() - t['start_ts'])
        return {
            'elapsed': int(total),
            'state': t.get('state', 'paused'),
            'produto': t.get('produto', timer_key),
            'checklist': t.get('checklist', {})
        }

    def get_active_timers(self):
        """Retorna timers. Cada timer_key é único por unidade de produção."""
        active = []
        for key, t in self.timers.items():
            total = t.get('accumulated', 0)
            if t.get('state') == 'running' and t.get('start_ts', 0) > 0:
                total += (time.time() - t['start_ts'])
            active.append({
                "timer_key": key,
                "produto": t.get('produto', key),
                "estado": t.get('state', 'paused'),
                "tempo_decorrido": int(total),
                "inicio": t.get('created_at', ''),
                "checklist": t.get('checklist', {}),
                "checklist_count": sum(1 for v in t.get('checklist', {}).values() if v),
                "checklist_total": len(t.get('checklist', {})),
            })
        return active

    def _add_to_history(self, registro):
        """Salva no histórico mensal — MongoDB principal, arquivo fallback."""
        mes_chave = datetime.now().strftime('%Y-%m')
        if MONGO_AVAILABLE:
            try:
                _mongo_db['production_history'].update_one(
                    {'_id': mes_chave},
                    {'$push': {'registros': registro}},
                    upsert=True
                )
                return
            except Exception as e:
                logger.error(f"Erro ao salvar histórico no MongoDB: {e}")
        # Fallback arquivo
        try:
            if self.HISTORY_PATH.exists():
                with open(self.HISTORY_PATH, 'r') as f: history = json.load(f)
            else: history = {}
            if mes_chave not in history: history[mes_chave] = []
            history[mes_chave].append(registro)
            temp = self.HISTORY_PATH.with_suffix('.tmp')
            with open(temp, 'w') as f: json.dump(history, f)
            shutil.move(str(temp), str(self.HISTORY_PATH))
        except Exception as e:
            logger.error(f"Erro ao salvar histórico em arquivo: {e}")

    def get_monthly_history_details(self):
        """Retorna a lista detalhada do mês atual — MongoDB ou arquivo."""
        mes_chave = datetime.now().strftime('%Y-%m')
        if MONGO_AVAILABLE:
            try:
                doc = _mongo_db['production_history'].find_one({'_id': mes_chave})
                return (doc or {}).get('registros', [])
            except Exception:
                pass
        if not self.HISTORY_PATH.exists(): return []
        try:
            with open(self.HISTORY_PATH, 'r') as f: history = json.load(f)
            return history.get(mes_chave, [])
        except: return []

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
        if MONGO_AVAILABLE:
            try:
                doc = MongoStore.get('component_consumption', 'main')
                return doc.get('data', {})
            except Exception:
                pass
        if not self.FILE_PATH.exists():
            return {}
        try:
            with open(self.FILE_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}

    def _save(self):
        if MONGO_AVAILABLE:
            try:
                MongoStore.set('component_consumption', {'data': self.data}, 'main')
                return
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
        comp['qtd'] = round(comp['qtd'] + qty, 3)
        comp['un'] = unit

        registro = {
            'produto': product_name,
            'qtd': qty,
            'timestamp': datetime.now().isoformat()
        }
        comp['registros'].append(registro)

        # Log geral
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
        """Remove o registro de um componente (desmarcou o checkbox)."""
        self._ensure_current_month()
        key = self._current_month_key()
        month_data = self.data[key]

        if component_name in month_data['components']:
            comp = month_data['components'][component_name]
            comp['qtd'] = max(0, round(comp['qtd'] - qty, 3))
            # Remove o último registro deste produto
            comp['registros'] = [r for r in comp['registros'] if r['produto'] != product_name]
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
            summary.append({
                'nome': nome,
                'qtd_total': info['qtd'],
                'un': info['un'],
                'num_registros': len(todos),
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
        if MONGO_AVAILABLE:
            try:
                return MongoStore.get_all('pending_orders')
            except Exception:
                pass
        if not self.FILE_PATH.exists():
            return {}
        try:
            with open(self.FILE_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}

    def _save(self):
        if MONGO_AVAILABLE:
            try:
                for key, val in self.data.items():
                    MongoStore.upsert('pending_orders', key, val)
                return
            except Exception as e:
                logger.error(f"Erro ao salvar pending_orders no MongoDB: {e}")
        # Fallback arquivo
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
        """Retorna todos os itens concluídos este mês."""
        return [v for v in self.data.values() if v.get('status') == 'done']

    def get_all(self):
        return list(self.data.values())

    def reset_if_new_month(self):
        """
        Todo início de mês remove itens 'done' e 'waiting' do mês anterior.
        Itens em produção (in_production) são pausados para não se perder.
        """
        agora = datetime.now()
        mes_atual = f"{agora.year}-{agora.month:02d}"
        to_remove = []
        for key, item in self.data.items():
            added = item.get('added_at', '')
            if not added:
                continue
            try:
                dt = datetime.fromisoformat(added)
                item_mes = f"{dt.year}-{dt.month:02d}"
                if item_mes != mes_atual:  # Limpa tudo do mês anterior
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
            if to_remove and not MONGO_AVAILABLE:
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
                    sub_key = f"{order_id}_{idx}_{unit}"
                    if sub_key not in self.data:
                        self.data[sub_key] = {
                            **item_data,
                            'qtd': 1,
                            'order_id': order_id,
                            'item_key': sub_key,
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
        self._load_cache()
        self._cache_lock = Lock()
        
        # ✅ ADICIONE ESTAS LINHAS:
        self._component_usage_cache = None  # Inicializa o cache de componentes
        self.logger.debug("Orchestrator inicializado com cache de componentes vazio")
        
        # ✅ CORREÇÃO CRÍTICA: Carrega o cache de produtos no startup
        if self.auth.is_authenticated():
            self.logger.info("📦 Carregando cache inicial de produtos (process_products_cache)")
            self.process_products_cache()
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
            self._stop_event = Event() # Evento para sinalizar parada
            
            # ✅ ADICIONE: Verifica se é a primeira execução
            products_empty = len(self._products_cache) == 0
            kits_empty = len(self._kits_cache) == 0
            
            # A lógica de carga inicial foi movida para o callback, pois o token não está disponível aqui.
            # O worker principal ainda inicia, mas ele se protege com a verificação de token.
            
                        # ✅ REMOVIDO: Registro de Webhook (API v3 requer registro manual no painel)
            # A chamada para self.api.register_webhook foi removida daqui, pois a função agora apenas loga a instrução.
            # O registro deve ser feito manualmente no painel do Bling.
            
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

    def wake_worker(self):
        """
        Acorda o worker imediatamente se estiver dormindo.
        
        Útil após OAuth para forçar início imediato do processamento
        sem esperar os 60 segundos de sleep.
        """
        logger.debug("⏰ [DEBUG-WORKER] wake_worker() chamado")
        
        if self._running and self._stop_event:
            logger.info("⏰ Acordando worker (interrompendo sleep)...")
            self._stop_event.set()  # Interrompe o sleep
            
            # Recria o evento para o próximo ciclo
            import time
            time.sleep(0.1)  # Pequena pausa para garantir que o worker processou
            self._stop_event.clear()
            
            logger.info("✅ Worker acordado com sucesso!")
        else:
            logger.debug("⚠️ Worker não está rodando ou evento não existe")

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
        
        logger.debug("🔄 [DEBUG-WORKER] Worker loop iniciado")
        
        while not self._stop_event.is_set():
            cycle_count += 1
            
            logger.debug(f"")
            logger.debug(f"🔄 [DEBUG-WORKER] ==================== CICLO #{cycle_count} ====================")
            
            # Verifica autenticação antes de tudo
            logger.debug(f"🔍 [DEBUG-WORKER] Verificando autenticação...")
            is_auth = self.auth.is_authenticated()
            logger.debug(f"   • is_authenticated() = {is_auth}")
            
            if not is_auth:
                logger.info(f"⏸️ [DEBUG-WORKER] Ciclo #{cycle_count}: Aguardando autenticação...")
                logger.debug(f"   • Access Token: {'Presente' if self.auth._access_token else 'Ausente'}")
                logger.debug(f"   • Refresh Token: {'Presente' if self.auth._refresh_token else 'Ausente'}")
                
                # Tenta recarregar tokens do disco antes de esperar
                logger.debug("🔄 [DEBUG-WORKER] Tentando recarregar tokens do disco...")
                self.auth.reload_tokens_from_disk()
                
                # Verifica novamente
                is_auth_after_reload = self.auth.is_authenticated()
                logger.debug(f"   • is_authenticated() após reload = {is_auth_after_reload}")
                
                if not is_auth_after_reload:
                    logger.info("⏳ [DEBUG-WORKER] Aguardando 60s para próxima tentativa...")
                    self._stop_event.wait(60)
                    continue
                else:
                    logger.info("✅ [DEBUG-WORKER] Autenticação OK após reload! Continuando ciclo...")

            logger.debug(f"✅ [DEBUG-WORKER] Autenticação confirmada! Iniciando processamento...")
            
            try:
                # Ciclo de Produtos (Cache Pesado)
                # Força no primeiro ciclo (cycle_count=1) ou a cada 3 ciclos
                if cycle_count == 1 or cycle_count % 3 == 0:
                    logger.info(f"🔄 Ciclo #{cycle_count}: Atualizando cache de produtos...")
                    self.process_products_cache()
                
                # Ciclo de Vendas (KPIs)
                logger.info(f"🔄 Ciclo #{cycle_count}: Atualizando Pedidos/KPIs...")
                self.process_sales_orders()
                
                # Ciclo de Componentes
                if cycle_count % 2 == 0:
                    logger.info(f"🔄 Ciclo #{cycle_count}: Calculando componentes...")
                    usage = self.calculate_component_usage()
                    if usage.get('components'):
                        self._component_usage_cache = usage
                        self.broadcast_kpi_update(component_usage=usage)

            except Exception as e:
                logger.exception(f"❌ [DEBUG-WORKER] Erro fatal no ciclo #{cycle_count}")

            logger.info(f"✅ [DEBUG-WORKER] Ciclo #{cycle_count} finalizado. Dormindo 10min...")
            logger.debug(f"🔄 [DEBUG-WORKER] ==================== FIM CICLO #{cycle_count} ====================")
            logger.debug(f"")
            
            # Mantém 10 minutos (600s), mas pode ser interrompido por wake_worker()
            logger.debug("💤 [DEBUG-WORKER] Entrando em sleep de 600s (ou até ser acordado)...")
            interrupted = self._stop_event.wait(600)
            
            if interrupted:
                logger.info("⏰ [DEBUG-WORKER] Sleep interrompido! Iniciando próximo ciclo imediatamente...")
                self._stop_event.clear()  # Limpa o evento para não interromper próximos ciclos
            else:
                logger.debug("⏰ [DEBUG-WORKER] Sleep de 600s completado naturalmente")

    def process_sales_orders(self, force: bool = False):
        """Busca pedidos de venda e atualiza o Sales Manager (Versão Híbrida V2/V3)."""
        self.logger.debug(f"DEBUG: process_sales_orders chamado (force={force})")
        
        # Evita recálculos encavalados
        with self.sales.recalculation_lock:
            if self.sales._recalculation_running and not force:
                self.logger.debug("DEBUG: Recálculo já em execução, ignorando.")
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
                self.logger.debug(f"DEBUG: Buscando página {page} de pedidos...")
                try:
                    response = self.api.get('pedidos/vendas', params=params)
                except Exception as e:
                    self.logger.error(f"DEBUG: Erro na API ao buscar pedidos: {e}")
                    break # Se der erro na API, para o loop mas processa o que já pegou
                
                if response is None:
                    self.logger.debug(f"DEBUG: Resposta da API nula na página {page}")
                    break
    
                # --- CORREÇÃO DE LEITURA (PARSING) ---
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
                
                self.logger.debug(f"DEBUG: Página {page} retornou {len(data) if data else 0} pedidos.")
                
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
                    # --- MELHORIA DE NORMALIZAÇÃO ---
                    # Garante que temos uma data válida, verificando vários campos
                    data_pedido = o.get('data') or o.get('dataEmissao') or o.get('dataSaida')
                    
                    if not data_pedido:
                        continue # Pula pedido sem data
                        
                    o['data'] = data_pedido # Padroniza para 'data'
                    
                    if o.get('id'):
                        valid_orders.append(o)

                self.logger.debug(f"DEBUG: {len(valid_orders)} pedidos válidos após normalização inicial.")
                # 1. Substitui o histórico de vendas pelo resultado da busca (Reset Mensal)
                self.sales._sales_history = valid_orders
                
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

                # --- LÓGICA DE EXTRAÇÃO DE IMAGEM ROBUSTA (V3) ---
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
                    except:
                        pass
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
        
        # 1. Monta o payload base
        payload = {
            "type": "full_update",
            "authenticated": self.auth.is_authenticated() and not auth_error,
            "auth_error": auth_error,
            "is_running": self.is_running(),
            "cache_updated": cache_updated,
            "auth_url": self.auth.get_authorization_url() # Envia a URL de auth para o frontend
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

        # Novo Endpoint: Listagem de Pedidos em Cache
        @self.app.route('/api/webhook', methods=['POST'])
        def api_webhook():
            signature = request.headers.get('X-Bling-Signature')
            payload = request.data
            if self.config.WEBHOOK_SECRET != 'YOUR_WEBHOOK_SECRET' and signature:
                expected = hmac.new(self.config.WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(signature, expected):
                    return 'Invalid signature', 403
            try:
                data = json.loads(payload)
                event = data.get('evento')
                if event in ['pedidoCriado', 'pedidoAlterado', 'pedido']:
                    executor = ThreadPoolExecutor(max_workers=1)
                    executor.submit(self.orchestrator.process_sales_orders, force=True)
                    executor.shutdown(wait=False)
                return 'OK', 200
            except Exception as e:
                return 'Error', 500

        @self.app.route("/api/orders")
        def list_orders():
            return jsonify(list(self.orchestrator.sales._orders_cache.values()))

        # Novo Endpoint: Histórico de Vendas para Dashboard
        @self.app.route("/api/sales/history")
        @token_required
        def api_sales_history(token):
            stats = self.orchestrator.sales.stats_history
            if not stats or not stats.get('dates'):
                if not self.orchestrator.sales.daily_count:
                     executor = ThreadPoolExecutor(max_workers=1)
                     executor.submit(self.orchestrator.process_sales_orders)
                     executor.shutdown(wait=False)
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
            with self.orchestrator.sales.recalculation_lock:
                if self.orchestrator.sales._recalculation_running:
                    self.logger.warning("Recálculo de KPIs já em andamento. Requisição ignorada.")
                    return jsonify({"status": "already_running", "message": "Recálculo de KPIs já em andamento."}), 202
                
                self.orchestrator.sales._recalculation_running = True

            # Executa o recálculo em uma thread separada para não bloquear a requisição HTTP
            executor = ThreadPoolExecutor(max_workers=1)
            executor.submit(self.orchestrator.process_sales_orders)
            executor.shutdown(wait=False)
            
            return jsonify({"status": "started", "message": "Recálculo de KPIs iniciado em segundo plano."}), 202

        @self.app.route('/api/timer/action', methods=['POST'])
        def api_timer_action():
            data = request.json
            action       = data.get('action', 'get')
            # timer_key: item_key para pedidos vinculados, produto_nome para timer manual
            timer_key    = data.get('timer_key') or data.get('produto', '')
            produto_nome = data.get('produto_nome') or data.get('produto', '')
            if not timer_key:
                return jsonify({'error': 'timer_key obrigatorio'}), 400
            if action == 'start':
                status = production_timer.start(timer_key, produto_nome or None)
            elif action == 'pause':
                status = production_timer.pause(timer_key)
            elif action == 'reset':
                status = production_timer.reset(timer_key)
            elif action == 'finish':
                status = production_timer.stop_and_log(timer_key, produto_nome or None)
            else:
                status = production_timer.get_status(timer_key)
            return jsonify(status)

        @self.app.route('/api/production/board')
        def api_production_board():
            """
            Retorna snapshot completo da aba de produção.
            - waiting: pedidos do Bling aguardando alguém clicar em Produzir
            - in_production: pedidos em andamento + tempo ao vivo do timer
            - done: concluídos do mês (para histórico)
            - timers_orphan: timers sem item_key (iniciados manualmente)
            """
            # Mapa timer_key -> info do timer (chave unica por unidade)
            timers = production_timer.timers
            timer_map = {}
            for key, t in timers.items():
                total = t.get('accumulated', 0)
                if t.get('state') == 'running' and t.get('start_ts', 0) > 0:
                    total += time.time() - t['start_ts']
                timer_map[key] = {
                    'estado': t.get('state', 'paused'),
                    'tempo_decorrido': int(total),
                    'checklist': t.get('checklist', {}),
                    'created_at': t.get('created_at', ''),
                }

            # Vincula cada item ao seu timer pelo item_key (unico por unidade de pedido)
            in_prod = []
            for item in pending_orders.get_in_production():
                key = item.get('item_key', '')
                t_info = timer_map.get(key, {})
                in_prod.append({**item, **t_info})

            # Orphan: timers cujo timer_key nao e item_key de nenhum pedido
            all_item_keys = set(pending_orders.data.keys())
            orphan = []
            for key, t in timers.items():
                if key not in all_item_keys:
                    total = t.get('accumulated', 0)
                    if t.get('state') == 'running' and t.get('start_ts', 0) > 0:
                        total += time.time() - t['start_ts']
                    orphan.append({
                        'nome': t.get('produto', key),
                        'timer_key': key,
                        'estado': t.get('state', 'paused'),
                        'tempo_decorrido': int(total),
                        'checklist': t.get('checklist', {}),
                        'created_at': t.get('created_at', ''),
                        'item_key': None,
                    })

            return jsonify({
                'waiting': pending_orders.get_waiting(),
                'in_production': in_prod,
                'orphan_timers': orphan,
                'done': pending_orders.get_done(),
                'server_time': time.time(),
            })

        @self.app.route('/api/checklist/state/<path:timer_key>', methods=['GET'])
        def api_checklist_get(timer_key):
            """Retorna checklist do timer identificado pelo timer_key."""
            t = production_timer.timers.get(timer_key, {})
            return jsonify({'checklist': t.get('checklist', {}),
                            'produto': t.get('produto', timer_key)})

        @self.app.route('/api/checklist/state', methods=['POST'])
        def api_checklist_set():
            """Salva item da checklist no timer identificado pelo timer_key."""
            data = request.json
            timer_key  = data.get('timer_key') or data.get('produto', '')
            componente = data.get('componente', '')
            checked    = data.get('checked', False)
            if timer_key and componente and timer_key in production_timer.timers:
                t = production_timer.timers[timer_key]
                if 'checklist' not in t:
                    t['checklist'] = {}
                t['checklist'][componente] = checked
                production_timer._save()
            return jsonify({'ok': True})

        @self.app.route('/api/consumption/register', methods=['POST'])
        def api_consumption_register():
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

            return jsonify({'success': True, 'result': result})

        @self.app.route('/api/consumption/summary')
        def api_consumption_summary():
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
        def api_pending_orders():
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
        def api_pending_orders_start():
            """Inicia producao. Timer chaveado por item_key — cada unidade tem timer proprio."""
            data = request.json
            item_key     = data.get('item_key', '')
            produto_nome = data.get('produto_nome', '')
            if not item_key:
                return jsonify({'error': 'item_key obrigatorio'}), 400
            item = pending_orders.start_production(item_key)
            # item_key como timer_key garante timer independente por unidade
            production_timer.start(item_key, produto_nome or item_key)
            return jsonify({'success': True, 'item': item})

        @self.app.route('/api/pending-orders/finish', methods=['POST'])
        def api_pending_orders_finish():
            """Finaliza producao. Timer finalizado pelo timer_key (= item_key por padrao)."""
            data = request.json
            item_key     = data.get('item_key', '')
            produto_nome = data.get('produto_nome', '')
            timer_key    = data.get('timer_key') or item_key
            if not item_key:
                return jsonify({'error': 'item_key obrigatorio'}), 400
            item = pending_orders.finish_production(item_key)
            production_timer.stop_and_log(timer_key, produto_nome or None)
            return jsonify({'success': True, 'item': item})

        @self.app.route('/api/pending-orders/dismiss', methods=['POST'])
        def api_pending_orders_dismiss():
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
                # Reset mensal antes de sincronizar
                pending_orders.reset_if_new_month()

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
            
            logger.debug("🔐 [DEBUG-CALLBACK] Callback OAuth recebido")
            logger.debug(f"   • Code presente: {'Sim' if code else 'Não'}")
            logger.debug(f"   • State: {state[:20]}..." if state else "   • State: Ausente")
            
            if not code:
                logger.error("❌ [DEBUG-CALLBACK] Código de autorização não recebido!")
                return "Erro: Código de autorização não recebido.", 400
                
            # Validação do State (CSRF)
            logger.debug("🔍 [DEBUG-CALLBACK] Validando state OAuth...")
            if not self.orchestrator.auth._validate_oauth_state(state):
                logger.error("❌ [DEBUG-CALLBACK] State inválido ou expirado!")
                return "Erro: State inválido ou expirado.", 403
            
            logger.debug("✅ [DEBUG-CALLBACK] State validado com sucesso")
            
            # Troca o código pelo token
            logger.debug("🔄 [DEBUG-CALLBACK] Trocando code por tokens...")
            success = self.orchestrator.auth.exchange_code_for_token(code)
            
            if success:
                logger.info("✅ [DEBUG-CALLBACK] Tokens obtidos com sucesso!")
                
                # 🔧 CORREÇÃO CRÍTICA: Recarrega tokens na memória
                logger.debug("🔄 [DEBUG-CALLBACK] Recarregando tokens na memória...")
                self.orchestrator.auth.reload_tokens_from_disk()
                
                # Verifica autenticação após reload
                is_auth = self.orchestrator.auth.is_authenticated()
                logger.debug(f"🔍 [DEBUG-CALLBACK] is_authenticated() = {is_auth}")
                
                # Inicia o worker após autenticação bem-sucedida
                if not self.orchestrator.is_running():
                    logger.info("🚀 [DEBUG-CALLBACK] Iniciando worker...")
                    self.orchestrator.start_worker()
                    start_cleanup_timer()
                    logger.info("✅ [DEBUG-CALLBACK] Worker iniciado com sucesso!")
                else:
                    logger.debug("ℹ️ [DEBUG-CALLBACK] Worker já está rodando")
                    # 🔧 NOVO: Acorda o worker imediatamente
                    logger.debug("⏰ [DEBUG-CALLBACK] Acordando worker para processar imediatamente...")
                    self.orchestrator.wake_worker()
                
                logger.info("🔄 [DEBUG-CALLBACK] Redirecionando para dashboard...")
                return redirect('/')
            else:
                logger.error("❌ [DEBUG-CALLBACK] Erro ao trocar código pelo token!")
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
                    self.logger.debug(f"DEBUG: Webhook bruto recebido: {request.data.decode('utf-8')[:500]}")
                    self.logger.debug(f"DEBUG: Headers do Webhook: {dict(request.headers)}")

                    # 1. Validação de Assinatura (Mantenha se configurado no Render)
                    signature = request.headers.get("X-Bling-Signature-256")
                    if self.config.WEBHOOK_SECRET and not signature:
                        self.logger.warning("DEBUG: Webhook rejeitado: WEBHOOK_SECRET configurado mas assinatura ausente.")
                        return jsonify({"status": "forbidden", "reason": "missing signature"}), 403

                    data = request.json
                    if not data:
                        self.logger.debug("DEBUG: Webhook ignorado: JSON vazio ou inválido.")
                        return jsonify({"status": "ignored"}), 200

                    self.logger.info(f"⚡ Webhook recebido: {str(data)[:200]}")

                    # 2. DETECÇÃO ROBUSTA DE EVENTO (V2 e V3)
                    should_update = False

                    # Caso 1: Webhook V3 Padrão (vem "id", "situacao", "tipo" na raiz)
                    if 'situacao' in data and 'id' in data:
                        self.logger.debug(f"DEBUG: Webhook V3 detectado (ID: {data.get('id')}, Situação: {data.get('situacao')})")
                        should_update = True
                    
                    # Caso 2: Tipo explícito
                    elif data.get('tipo') == 'pedidoVenda':
                        self.logger.debug("DEBUG: Webhook tipo pedidoVenda detectado.")
                        should_update = True

                    # Caso 3: Formato antigo (V2)
                    elif 'retorno' in data and 'pedidos' in data['retorno']:
                        self.logger.debug("DEBUG: Webhook V2 detectado.")
                        should_update = True
                    
                    # Caso 4: Callbacks de teste
                    elif data.get('test') == True:
                        self.logger.debug("DEBUG: Webhook de teste recebido.")
                        return jsonify({"status": "ok", "message": "Test received"}), 200

                    if should_update:
                        self.logger.info("🔔 Alteração de pedido detectada via Webhook. Iniciando atualização...")
                        
                        # Dispara atualização em background
                        executor = ThreadPoolExecutor(max_workers=1)
                        # Força 'force=True' para ignorar o lock de tempo e atualizar na hora
                        executor.submit(self.orchestrator.process_sales_orders, force=True)
                        executor.shutdown(wait=False)
                        
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
    <title>SW Móveis MDF — Gestão de Produção</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Noto+Sans:ital,wght@0,300;0,400;0,500;0,600;0,700;1,400&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet">
    <style>
        /* ══════════════════════════════════════════════════════════
           SW MÓVEIS MDF — IDENTIDADE VISUAL 2025
           Paleta: Jaguar | Amarelo Seletivo | Salomie | Mindaro | Gray | Nurse
           Tipografia: Bebas Neue (títulos) + Noto Sans (corpo)
        ══════════════════════════════════════════════════════════ */

        :root {
            --jaguar:        #01010d;
            --amarelo:       #ffb600;
            --amarelo-dark:  #e6a400;
            --salomie:       #fede8f;
            --mindaro:       #f5f883;
            --gray:          #807f7f;
            --nurse:         #ecedec;
            /* funcionais */
            --success:       #22c55e;
            --error:         #ef4444;
            --border:        #ddddd8;
            --bg:            #f5f5f0;
        }

        /* ── Reset & Base ─────────────────────────────────────── */
        *, *::before, *::after { box-sizing: border-box; }

        body {
            background: var(--bg);
            font-family: 'Noto Sans', -apple-system, BlinkMacSystemFont, sans-serif;
            font-size: 0.92rem;
            line-height: 1.65;
            color: var(--jaguar);
            min-height: 100vh;
        }

        h1,h2,h3,h4,h5,h6 { font-weight: 700; color: var(--jaguar); margin-bottom: 0.4rem; }

        /* ── Bebas Neue para destaques ─────────────────────────── */
        .sw-title {
            font-family: 'Bebas Neue', sans-serif;
            letter-spacing: 0.06em;
        }

        /* ── Scrollbar ─────────────────────────────────────────── */
        ::-webkit-scrollbar { width: 7px; height: 7px; }
        ::-webkit-scrollbar-track { background: var(--nurse); }
        ::-webkit-scrollbar-thumb { background: var(--salomie); border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: var(--amarelo); }

        /* ── Navbar ─────────────────────────────────────────────── */
        .navbar {
            background: var(--jaguar);
            border-bottom: 3px solid var(--amarelo);
            padding: 0;
            min-height: 62px;
        }

        .navbar-inner {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 1.5rem;
            height: 62px;
            gap: 1rem;
        }

        .navbar-brand-wrap {
            display: flex;
            align-items: center;
            gap: 0.9rem;
            text-decoration: none;
        }

        .navbar-brand-wrap img {
            height: 40px;
            width: auto;
            filter: drop-shadow(0 0 8px rgba(255,182,0,0.5));
        }

        .brand-text-top {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 1.45rem;
            letter-spacing: 0.1em;
            color: var(--amarelo);
            line-height: 1;
        }

        .brand-text-sub {
            font-size: 0.6rem;
            font-weight: 600;
            letter-spacing: 0.18em;
            text-transform: uppercase;
            color: rgba(255,255,255,0.4);
            line-height: 1;
            margin-top: 2px;
        }

        .navbar-right {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        /* ── Status Badge ─────────────────────────────────────── */
        #status-badge {
            font-family: 'Noto Sans', sans-serif;
            font-size: 0.7rem;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            padding: 0.38rem 1rem;
            border-radius: 50px;
        }

        #status-badge.bg-secondary { background: var(--gray) !important; }
        #status-badge.bg-success   { background: var(--success) !important; }
        #status-badge.bg-danger    { background: var(--error) !important; }

        /* ── Auth Button ──────────────────────────────────────── */
        .btn-auth {
            font-family: 'Noto Sans', sans-serif;
            font-size: 0.75rem;
            font-weight: 700;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            color: var(--jaguar) !important;
            background: var(--amarelo);
            border: none;
            padding: 0.4rem 1.1rem;
            border-radius: 6px;
            cursor: pointer;
            transition: background 0.2s;
            text-decoration: none;
        }

        .btn-auth:hover { background: var(--amarelo-dark); }

        /* ── Page Header ──────────────────────────────────────── */
        .page-header {
            padding: 2.2rem 1.5rem 0;
        }

        .page-header-inner {
            display: flex;
            align-items: center;
            gap: 1rem;
            border-left: 5px solid var(--amarelo);
            padding-left: 1rem;
        }

        .page-header-title {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 1.9rem;
            letter-spacing: 0.05em;
            color: var(--jaguar);
            margin: 0;
        }

        .page-header-sub {
            font-size: 0.8rem;
            color: var(--gray);
            font-weight: 500;
            margin: 0;
        }

        /* ── Main Container ───────────────────────────────────── */
        .main-wrap {
            padding: 1.75rem 1.5rem 3rem;
        }

        /* ── Cards ────────────────────────────────────────────── */
        .card {
            background: #fff;
            border: 1px solid var(--border);
            border-radius: 10px;
            box-shadow: 0 2px 8px rgba(1,1,13,0.05);
            transition: border-color 0.2s, box-shadow 0.2s, transform 0.2s;
        }

        .card:hover {
            border-color: var(--amarelo);
            box-shadow: 0 6px 20px rgba(255,182,0,0.15);
            transform: translateY(-2px);
        }

        .card-header {
            background: var(--jaguar);
            color: #fff;
            border-radius: 10px 10px 0 0 !important;
            border-bottom: 2px solid var(--amarelo);
            padding: 0.9rem 1.2rem;
            font-weight: 700;
        }

        .card-header h5, .card-header .h5 {
            color: #fff;
            margin: 0;
            font-size: 0.9rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        /* ── KPI Cards ────────────────────────────────────────── */
        .kpi-card {
            border-left: 4px solid var(--amarelo);
            position: relative;
            overflow: hidden;
            padding: 1.4rem !important;
        }

        .kpi-card::after {
            content: '';
            position: absolute;
            top: -20px; right: -20px;
            width: 80px; height: 80px;
            border-radius: 50%;
            background: var(--salomie);
            opacity: 0.3;
        }

        .kpi-daily   { border-left-color: var(--amarelo); }
        .kpi-weekly  { border-left-color: var(--jaguar); }
        .kpi-historic{ border-left-color: var(--success); }

        .kpi-label {
            font-size: 0.68rem;
            font-weight: 700;
            letter-spacing: 0.1em;
            text-transform: uppercase;
            color: var(--gray);
            margin-bottom: 0.5rem;
        }

        .kpi-value {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 3rem;
            letter-spacing: 0.04em;
            line-height: 1;
            color: var(--jaguar);
        }

        .kpi-sub {
            font-size: 0.72rem;
            color: var(--gray);
            margin-top: 0.35rem;
        }

        .kpi-card.updating { animation: kpi-flash 0.5s ease; }

        @keyframes kpi-flash {
            0%   { background: #fffbee; }
            100% { background: #fff; }
        }

        /* ── Section Accent Title ─────────────────────────────── */
        .section-title {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 1.1rem;
            letter-spacing: 0.06em;
            color: var(--jaguar);
            display: flex;
            align-items: center;
            gap: 0.6rem;
            margin-bottom: 1rem;
        }

        .section-title::before {
            content: '';
            width: 4px;
            height: 22px;
            background: var(--amarelo);
            border-radius: 3px;
            flex-shrink: 0;
        }

        /* ── Log Box ──────────────────────────────────────────── */
        .log-box {
            font-family: 'Fira Code', monospace;
            font-size: 0.78rem;
            background: #0d0d1a;
            color: #d4d4d4;
            border-radius: 0 0 10px 10px;
            padding: 1rem;
            max-height: 320px;
            overflow-y: auto;
            line-height: 1.6;
        }

        .log-entry { padding: 0.1rem 0; animation: fadeIn 0.2s ease; }

        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

        .log-level-INFO    { color: #4ec9b0; }
        .log-level-WARNING { color: #ffd700; }
        .log-level-ERROR   { color: #f48771; }
        .log-level-DEBUG   { color: #569cd6; }

        /* ── Tabs ─────────────────────────────────────────────── */
        .nav-tabs {
            border-bottom: 2px solid var(--border);
            gap: 0.2rem;
            flex-wrap: nowrap;
            overflow-x: auto;
        }

        .nav-tabs .nav-link {
            font-size: 0.75rem;
            font-weight: 700;
            letter-spacing: 0.07em;
            text-transform: uppercase;
            color: var(--gray);
            border: none;
            border-bottom: 3px solid transparent;
            padding: 0.6rem 0.9rem;
            white-space: nowrap;
            transition: all 0.2s;
        }

        .nav-tabs .nav-link:hover { color: var(--jaguar); border-bottom-color: var(--salomie); }

        .nav-tabs .nav-link.active {
            color: var(--jaguar);
            background: none;
            border-bottom: 3px solid var(--amarelo);
        }

        /* ── Buttons ──────────────────────────────────────────── */
        .btn {
            font-weight: 700;
            font-size: 0.78rem;
            letter-spacing: 0.05em;
            text-transform: uppercase;
            border-radius: 7px;
            border: none;
            transition: all 0.2s;
        }

        .btn-primary {
            background: var(--amarelo);
            color: var(--jaguar) !important;
            box-shadow: 0 3px 10px rgba(255,182,0,0.28);
        }

        .btn-primary:hover, .btn-primary:focus {
            background: var(--amarelo-dark);
            color: var(--jaguar) !important;
            transform: translateY(-1px);
        }

        .btn-success { background: var(--success); color: #fff !important; }
        .btn-success:hover { background: #16a34a; color: #fff !important; }

        .btn-danger { background: var(--error); color: #fff !important; }

        .btn-outline-light {
            border: 2px solid var(--amarelo);
            color: var(--amarelo) !important;
            background: transparent;
        }

        .btn-outline-light:hover {
            background: var(--amarelo);
            color: var(--jaguar) !important;
        }

        .btn-outline-secondary {
            border: 1.5px solid var(--border);
            color: var(--jaguar) !important;
            background: transparent;
        }

        .btn-outline-secondary:hover {
            background: var(--nurse);
            border-color: var(--gray);
        }

        .btn-sm { font-size: 0.72rem; padding: 0.3rem 0.7rem; }

        .btn-warning { background: var(--amarelo); color: var(--jaguar) !important; border-color: var(--amarelo); }
        .btn-warning:hover { background: var(--amarelo-dark); border-color: var(--amarelo-dark); }

        /* ── Forms ────────────────────────────────────────────── */
        .form-control, .form-select {
            border: 1.5px solid var(--border);
            border-radius: 7px;
            padding: 0.65rem 1rem;
            font-size: 0.88rem;
            transition: border-color 0.2s, box-shadow 0.2s;
        }

        .form-control:focus, .form-select:focus {
            border-color: var(--amarelo);
            box-shadow: 0 0 0 3px rgba(255,182,0,0.15);
            outline: none;
        }

        /* ── Tables ───────────────────────────────────────────── */
        .table { margin: 0; }

        .table thead th {
            background: var(--jaguar);
            color: var(--amarelo);
            font-size: 0.68rem;
            font-weight: 700;
            letter-spacing: 0.1em;
            text-transform: uppercase;
            padding: 0.8rem 1rem;
            border: none;
        }

        .table tbody tr { border-bottom: 1px solid var(--border); transition: background 0.15s; }
        .table tbody tr:hover { background: #fffbee; }
        .table td { padding: 0.8rem 1rem; vertical-align: middle; }

        /* ── Badges ───────────────────────────────────────────── */
        .badge {
            font-size: 0.68rem;
            font-weight: 700;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            padding: 0.35rem 0.75rem;
            border-radius: 50px;
        }

        .badge.bg-success { background: var(--success) !important; }
        .badge.bg-warning { background: var(--amarelo) !important; color: var(--jaguar) !important; }
        .badge.bg-info    { background: var(--jaguar) !important; color: var(--amarelo) !important; }
        .badge.bg-secondary { background: var(--gray) !important; }
        .badge.bg-light   { background: var(--nurse) !important; color: var(--jaguar) !important; }

        /* ── Alerts ───────────────────────────────────────────── */
        .alert { border: none; border-radius: 8px; border-left: 4px solid; }
        .alert-warning { background: #fffbee; border-left-color: var(--amarelo); color: #78500e; }
        .alert-info    { background: #eef6ff; border-left-color: #3b82f6; color: #1e3a8a; }
        .alert-danger  { background: #fee2e2; border-left-color: var(--error); color: #7f1d1d; }
        .alert-success { background: #dcfce7; border-left-color: var(--success); color: #14532d; }
        .alert-secondary { background: var(--nurse); border-left-color: var(--gray); color: var(--jaguar); }

        /* ── List Group ───────────────────────────────────────── */
        .list-group-item {
            border: 1px solid var(--border);
            border-radius: 8px !important;
            margin-bottom: 0.45rem;
            transition: all 0.2s;
        }

        .list-group-item:hover {
            border-color: var(--amarelo);
            background: #fffbee;
            transform: translateX(3px);
        }

        /* ── Accordion ────────────────────────────────────────── */
        .accordion-button {
            font-weight: 700;
            font-size: 0.82rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .accordion-button:not(.collapsed) {
            background: #fffbee;
            color: var(--jaguar);
            box-shadow: none;
            border-bottom: 2px solid var(--amarelo);
        }

        .accordion-button:focus {
            box-shadow: 0 0 0 3px rgba(255,182,0,0.15);
        }

        /* ── Metric Box ───────────────────────────────────────── */
        .metric-box {
            background: var(--jaguar);
            border-radius: 10px;
            padding: 1.3rem 1rem;
            text-align: center;
            border-bottom: 3px solid var(--amarelo);
            box-shadow: 0 4px 14px rgba(1,1,13,0.18);
            transition: transform 0.2s, box-shadow 0.2s;
        }

        .metric-box:hover { transform: translateY(-3px); box-shadow: 0 8px 22px rgba(1,1,13,0.25); }

        .metric-label {
            font-size: 0.65rem;
            font-weight: 700;
            letter-spacing: 0.12em;
            text-transform: uppercase;
            color: rgba(255,255,255,0.5);
            margin-bottom: 0.4rem;
        }

        .metric-value {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 2.4rem;
            letter-spacing: 0.04em;
            color: var(--amarelo);
            line-height: 1;
        }

        /* ── Toast ────────────────────────────────────────────── */
        .toast {
            border-radius: 10px;
            border: none;
            box-shadow: 0 8px 24px rgba(0,0,0,0.18);
        }

        /* ── Spinner personalizado ────────────────────────────── */
        .spinner-border { color: var(--amarelo) !important; }

        /* ── Modal production header ──────────────────────────── */
        .modal-prod-header {
            background: var(--jaguar);
            border-bottom: 2px solid var(--amarelo);
        }

        /* ── Production board header colors ──────────────────── */
        .board-header-yellow {
            background: var(--jaguar) !important;
            border-bottom: 2px solid var(--amarelo) !important;
        }

        .board-header-green {
            background: var(--jaguar) !important;
            border-bottom: 2px solid var(--success) !important;
        }

        .board-header-purple {
            background: var(--jaguar) !important;
            border-bottom: 2px solid #a855f7 !important;
        }

        /* ── Waiting table row accent ─────────────────────────── */
        .waiting-header { background: #fffbee !important; }
        .waiting-header th { color: var(--jaguar) !important; background: #fffbee !important; }

        .inprod-header { background: #f0fdf4 !important; }
        .inprod-header th { color: var(--jaguar) !important; background: #f0fdf4 !important; }

        /* ── Timer display ────────────────────────────────────── */
        .timer-display {
            font-family: 'Fira Code', monospace;
            font-size: 3.5rem;
            font-weight: 500;
            color: var(--amarelo);
            letter-spacing: 0.05em;
            text-shadow: 0 0 20px rgba(255,182,0,0.4);
        }

        /* ── Hidden ───────────────────────────────────────────── */
        .hidden { display: none !important; }

        .stock-badge, .estoque-info, .stock-info-row { display: none !important; }

        /* ── Animations ───────────────────────────────────────── */
        @keyframes fadeInUp {
            from { opacity: 0; transform: translateY(14px); }
            to   { opacity: 1; transform: translateY(0); }
        }

        @keyframes slideDown {
            from { opacity: 0; transform: translateY(-20px); }
            to   { opacity: 1; transform: translateY(0); }
        }

        @keyframes pulse-animation {
            0%, 100% { opacity: 1; }
            50%       { opacity: 0.5; }
        }

        .pulse-animation { animation: pulse-animation 1.5s infinite; }

        /* ── Responsive ───────────────────────────────────────── */
        @media (max-width: 768px) {
            .kpi-value  { font-size: 2.2rem; }
            .metric-value { font-size: 1.8rem; }
            .log-box    { max-height: 240px; }
            .navbar-inner { padding: 0 1rem; }
        }

        /* ── Footer ───────────────────────────────────────────── */
        .sw-footer {
            background: var(--jaguar);
            border-top: 3px solid var(--amarelo);
            padding: 1.3rem 1.5rem;
            margin-top: 2rem;
            color: rgba(255,255,255,0.7);
            font-size: 0.8rem;
        }

        .sw-footer-brand {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 1.25rem;
            letter-spacing: 0.1em;
            color: var(--amarelo);
        }

        /* ── Last recalculated bar ────────────────────────────── */
        .recalc-bar {
            background: var(--nurse);
            border-radius: 8px;
            padding: 0.5rem 1rem;
            font-size: 0.78rem;
            color: var(--gray);
            display: flex;
            align-items: center;
            gap: 0.5rem;
            border-left: 3px solid var(--salomie);
        }

        .recalc-bar strong { color: var(--jaguar); }

    </style>
</head>
<body>
    <!-- ✅ Navbar SW Móveis MDF -->
    <nav class="navbar">
        <div class="navbar-inner">
            <a class="navbar-brand-wrap" href="#">
                <img src="https://i.imgur.com/j79HO6n.png" alt="SW">
                <div>
                    <div class="brand-text-top">SW Móveis MDF</div>
                    <div class="brand-text-sub">Gestão de Produção</div>
                </div>
            </a>
            <div class="navbar-right">
                <span id="status-badge" class="badge bg-secondary">⏳ Carregando</span>
                <a id="auth-link" href="{{ auth_url }}" class="btn-auth">Autenticar</a>
            </div>
        </div>
    </nav>

    <!-- ✅ Page Header -->
    <div class="page-header">
        <div class="page-header-inner">
            <div>
                <h1 class="page-header-title">Pedidos de Venda</h1>
                <p class="page-header-sub">Acompanhe os pedidos abertos e fechados em tempo real</p>
            </div>
        </div>
    </div>

    <!-- ✅ Main Wrap -->
    <div class="main-wrap">

        <!-- KPI Cards -->
        <div class="row mb-4 g-3">
            <div class="col-md-4">
                <div class="card kpi-card kpi-daily">
                    <div class="kpi-label">Pedidos Diários</div>
                    <div class="kpi-value" id="kpi-daily">0</div>
                    <div class="kpi-sub">Últimas 24h</div>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card kpi-card kpi-weekly">
                    <div class="kpi-label">Pedidos Semanais</div>
                    <div class="kpi-value" id="kpi-weekly">0</div>
                    <div class="kpi-sub">Últimos 7 dias</div>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card kpi-card kpi-historic">
                    <div class="kpi-label">Pedidos Mensais</div>
                    <div class="kpi-value" id="kpi-historic">0</div>
                    <div class="kpi-sub">Este Mês</div>
                </div>
            </div>
        </div>

        <!-- Last recalc bar -->
        <div class="recalc-bar mb-4">
            <span>⏱ Último recálculo:</span>
            <strong id="last-recalculated">N/D</strong>
        </div>

        <!-- Logs em Tempo Real -->
        <div class="card mb-4">
            <div class="card-header d-flex align-items-center justify-content-between">
                <h5 class="mb-0">📋 Logs em Tempo Real</h5>
                <span id="log-status" class="badge bg-secondary" style="font-size:0.65rem;">Conectando...</span>
            </div>
            <div id="logs-content" class="log-box"></div>
        </div>

        <!-- ✅ Tabs -->
        <div>
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

                    <!-- Tab: Componentes (Consumo & Produção) -->
                    <div class="tab-pane fade" id="component-usage" role="tabpanel">

                        <!-- ═══ PAINEL DE PRODUÇÃO UNIFICADO ═══ -->
                        <div class="card mb-4 border-0 shadow-sm">
                            <div class="card-header d-flex justify-content-between align-items-center py-3 board-header-yellow">
                                <div>
                                    <h5 class="mb-0">🏭 Painel de Produção</h5>
                                    <small style="color:rgba(255,255,255,0.5);font-size:0.72rem;">
                                        ⏳ Em Espera <span id="waiting-count-badge" class="badge bg-warning ms-1">0</span>
                                        &nbsp;⚙️ Produzindo <span id="inprod-count-badge" class="badge bg-success ms-1">0</span>
                                        &nbsp;✅ Concluídos <span id="done-count-badge" class="badge bg-secondary ms-1">0</span>
                                    </small>
                                </div>
                                <button class="btn btn-sm btn-outline-light" onclick="syncAndRefreshPending()">🔄 Sincronizar Bling</button>
                            </div>
                            <div class="card-body p-0" id="production-board-section">
                                <p class="text-center text-muted py-4">⏳ Carregando...</p>
                            </div>
                        </div>

                        <!-- ═══ CONSUMO MENSAL ═══ -->
                        <div class="card mb-4 border-0 shadow-sm">
                            <div class="card-header d-flex justify-content-between align-items-center board-header-green">
                                <div>
                                    <h5 class="mb-0">📊 Consumo de Insumos & Componentes</h5>
                                    <small style="color:rgba(255,255,255,0.45);font-size:0.72rem;" id="consumption-month-label">Mês atual • Reinicia todo mês</small>
                                </div>
                                <span class="badge bg-warning" id="consumption-total-badge">0 insumos</span>
                            </div>
                            <div class="card-body p-0" id="consumption-table-section">
                                <div class="text-center py-4 text-muted">⏳ Carregando consumo...</div>
                            </div>
                        </div>

                        <!-- ═══ HISTÓRICO DE FINALIZAÇÕES ═══ -->
                        <div class="card border-0 shadow-sm">
                            <div class="card-header board-header-purple">
                                <h5 class="mb-0">📜 Histórico de Finalizações (Mês)</h5>
                                <small style="color:rgba(255,255,255,0.45);font-size:0.72rem;">Registro de cada produto finalizado com tempo de produção</small>
                            </div>
                            <div class="card-body p-0" id="production-history-section">
                                <div class="text-center py-4 text-muted">⏳ Carregando histórico...</div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

    </div><!-- /.main-wrap -->
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

        const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
        let wsLogs = null;

        function connectLogs() {
            const logStatus = document.getElementById('log-status');
            if (logStatus) { logStatus.textContent = 'Conectando...'; logStatus.className = 'badge bg-secondary'; }
            try {
                wsLogs = new WebSocket(`${proto}://${window.location.host}/ws/logs`);
                wsLogs.onopen = () => {
                    if (logStatus) { logStatus.textContent = '🟢 Ao Vivo'; logStatus.className = 'badge bg-success'; }
                };
                wsLogs.onmessage = (e) => {
                    const data = JSON.parse(e.data);
                    const box = document.getElementById('logs-content');
                    if (!box) return;
                    if (data.logs) {
                        // Popula histórico inicial
                        data.logs.slice(-60).forEach(l => box.innerHTML += formatLog(l));
                        box.scrollTop = box.scrollHeight;
                    }
                    if (data.log) {
                        // Entrada incremental
                        box.innerHTML += formatLog(data.log);
                        box.scrollTop = box.scrollHeight;
                        // Limita a 200 entradas no DOM
                        const entries = box.querySelectorAll('.log-entry');
                        if (entries.length > 200) entries[0].remove();
                    }
                };
                wsLogs.onerror = () => {
                    if (logStatus) { logStatus.textContent = '🔴 Erro WS'; logStatus.className = 'badge bg-danger'; }
                };
                wsLogs.onclose = () => {
                    if (logStatus) { logStatus.textContent = '⚠️ Reconectando'; logStatus.className = 'badge bg-warning text-dark'; }
                    setTimeout(connectLogs, 4000);
                };
            } catch(e) {
                console.error('WebSocket logs error:', e);
                setTimeout(connectLogs, 5000);
            }
        }

        connectLogs();

        /* ✅ Auth — polling HTTP robusto (não depende só do WebSocket) */
        async function pollAuthStatus() {
            try {
                const r = await fetch('/api/status');
                if (!r.ok) return;
                const d = await r.json();
                updateAuthStatus(d.authenticated, d.auth_url || document.getElementById('auth-link').href);
            } catch(e) {
                console.warn('pollAuthStatus falhou:', e);
            }
        }

        // Verifica imediatamente e depois a cada 5s
        pollAuthStatus();
        setInterval(pollAuthStatus, 5000);

        /* ✅ Atualizar Status de Autenticação */
        function updateAuthStatus(authenticated, authUrl) {
            const badge = document.getElementById('status-badge');
            const authLink = document.getElementById('auth-link');
            isAuthenticated = !!authenticated;

            if (isAuthenticated) {
                if (badge) { badge.className = 'badge bg-success'; badge.textContent = '🟢 Online'; }
                if (authLink) authLink.classList.add('d-none');
                const ct = document.getElementById('content-tabs');
                const ar = document.getElementById('auth-required-tabs');
                if (ct) ct.classList.remove('hidden');
                if (ar) ar.classList.add('hidden');
            } else {
                if (badge) { badge.className = 'badge bg-danger'; badge.textContent = '🔴 Offline'; }
                if (authLink) authLink.classList.remove('d-none');
                const ct = document.getElementById('content-tabs');
                const ar = document.getElementById('auth-required-tabs');
                if (ct) ct.classList.add('hidden');
                if (ar) ar.classList.remove('hidden');
            }
            if (authUrl && authLink) authLink.href = authUrl;
        }

        /* ✅ Atualizar KPIs */
        function updateKpis(dSalesStats) {
            const kpiDaily = document.getElementById('kpi-daily');
            const kpiWeekly = document.getElementById('kpi-weekly');
            const kpiHistoric = document.getElementById('kpi-historic');

            if (kpiDaily) kpiDaily.textContent = dSalesStats.daily ?? 0;
            if (kpiWeekly) kpiWeekly.textContent = dSalesStats.weekly ?? 0;
            if (kpiHistoric) kpiHistoric.textContent = dSalesStats.monthly ?? 0;
            const lr = document.getElementById('last-recalculated');
            if (lr) lr.textContent = formatDateTime(dSalesStats.last_update);

            document.querySelectorAll('.kpi-card').forEach(card => {
                card.classList.add('updating');
                setTimeout(() => card.classList.remove('updating'), 500);
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

        // timerKey = item_key para pedidos (único por unidade), productName para timers manuais
        function openProductionChecklist(productName, timerKey) {
            timerKey = timerKey || productName;
            const isCadeira = productName.toUpperCase().includes('CADEIRA');

            // Sanitiza para uso em atributos HTML
            const pnSafe = productName.replace(/"/g, '&quot;');
            const tkSafe = timerKey.replace(/"/g, '&quot;');

            let checklistHtml = '';
            if (isCadeira) {
                checklistHtml = '<h6 class="text-muted mb-3">📋 Marque o que foi retirado/usado para esta unidade</h6>' +
                    '<div class="row g-2 mb-4" style="max-height:320px;overflow-y:auto;">' +
                    RECIPE_CADEIRA.map((item, i) =>
                        '<div class="col-md-6">' +
                        '<div class="form-check p-2 border rounded bg-white d-flex align-items-center gap-2 checklist-item"' +
                        ' style="cursor:pointer;transition:all .2s;"' +
                        ' data-pname="' + pnSafe + '" data-tkey="' + tkSafe + '"' +
                        ' onclick="toggleChecklist(this,' + i + ',this.dataset.pname,this.dataset.tkey)">' +
                        '<input class="form-check-input ms-1" type="checkbox" id="check' + i + '" onclick="event.stopPropagation()">' +
                        '<label class="form-check-label flex-grow-1 small fw-bold mb-0" for="check' + i + '" style="cursor:pointer;">' +
                        item.nome + ' <span class="badge bg-light text-dark border float-end">' + item.qtd + ' ' + item.un + '</span>' +
                        '</label></div></div>'
                    ).join('') +
                    '</div>' +
                    '<div id="checklist-progress" class="alert alert-info py-2 small mb-0">' +
                    '<strong>0 / ' + RECIPE_CADEIRA.length + '</strong> itens marcados como usados</div>';
            } else {
                checklistHtml = '<div class="alert alert-secondary">Este produto não possui lista técnica automática de insumos.</div>';
            }

            const modalHtml =
                '<div class="modal fade" id="productionModal" tabindex="-1" data-bs-backdrop="static">' +
                '<div class="modal-dialog modal-lg modal-dialog-centered"><div class="modal-content border-0 shadow-2xl">' +
                '<div class="modal-header text-white modal-prod-header">' +
                '<h5 class="modal-title">🛠️ Produção: ' + pnSafe + '</h5>' +
                '<button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" onclick="clearInterval(timerInterval)"></button>' +
                '</div><div class="modal-body" style="background:#f5f5f0;">' +
                '<div class="card mb-4 border-0" style="background:var(--jaguar,#01010d);border-bottom:3px solid var(--amarelo,#ffb600)!important;">' +
                '<div class="card-body text-center py-4">' +
                '<div class="text-uppercase small fw-bold mb-2" style="letter-spacing:.12em;color:rgba(255,255,255,0.5);font-size:0.65rem;">⏱ Tempo de Produção</div>' +
                '<div id="timer-display" class="timer-display mb-3">00:00:00</div>' +
                '<div id="timer-status" class="badge mb-3 bg-secondary" style="font-size:.8rem;padding:.4rem 1rem;">Parado</div>' +
                '<div class="d-flex justify-content-center gap-2" id="timer-btn-group" data-tkey="' + tkSafe + '" data-pnome="' + pnSafe + '">' +
                '<button class="btn btn-success px-4 fw-bold" onclick="controlTimer('start',document.getElementById('timer-btn-group').dataset.tkey,document.getElementById('timer-btn-group').dataset.pnome)">▶ Iniciar</button>' +
                '<button class="btn btn-warning px-4 fw-bold text-dark" onclick="controlTimer('pause',document.getElementById('timer-btn-group').dataset.tkey)">⏸ Pausar</button>' +
                '<button class="btn btn-outline-light px-4" onclick="controlTimer('reset',document.getElementById('timer-btn-group').dataset.tkey)">↺ Zerar</button>' +
                '</div></div></div>' +
                checklistHtml +
                '</div><div class="modal-footer bg-white d-flex justify-content-between">' +
                '<button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal" onclick="clearInterval(timerInterval)">Fechar</button>' +
                '<button type="button" class="btn btn-success px-4 fw-bold"' +
                ' onclick="controlTimer('finish',document.getElementById('timer-btn-group').dataset.tkey,document.getElementById('timer-btn-group').dataset.pnome)">✅ CONCLUIR & SALVAR</button>' +
                '</div></div></div></div>';

            const oldModal = document.getElementById('productionModal');
            if (oldModal) oldModal.remove();
            document.body.insertAdjacentHTML('beforeend', modalHtml);
            new bootstrap.Modal(document.getElementById('productionModal')).show();

            // Carrega timer e checklist pelo timerKey (único por unidade)
            controlTimer('get', timerKey, productName);
            _loadChecklistState(timerKey);
        }

        async function _loadChecklistState(timerKey) {
            try {
                const res = await fetch('/api/checklist/state/' + encodeURIComponent(timerKey));
                const data = await res.json();
                const saved = data.checklist || {};
                RECIPE_CADEIRA.forEach((item, i) => {
                    if (saved[item.nome]) {
                        const cb = document.getElementById('check' + i);
                        const ct = cb && cb.closest('.checklist-item');
                        if (cb && ct) {
                            cb.checked = true;
                            ct.style.background = '#d1fae5';
                            ct.style.borderColor = '#10b981';
                        }
                    }
                });
                _updateChecklistProgress();
            } catch(e) { console.error('_loadChecklistState:', e); }
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

        // timerKey = item_key do pedido (garante checklist por unidade, não por nome)
        function toggleChecklist(container, idx, productName, timerKey) {
            const cb = container.querySelector('input[type=checkbox]');
            const isChecked = cb.checked;  // browser já atualizou antes do onclick
            const item = RECIPE_CADEIRA[idx];
            timerKey = timerKey || productName;

            container.style.background  = isChecked ? '#d1fae5' : '';
            container.style.borderColor = isChecked ? '#10b981' : '';

            fetch('/api/checklist/state', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ timer_key: timerKey, componente: item.nome, checked: isChecked })
            }).catch(e => console.error('checklist/state:', e));

            registerConsumption(item.nome, item.qtd, item.un, productName, isChecked);
            _updateChecklistProgress();
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

        /* Timer — timerKey é único por unidade (item_key para pedidos, nome para manual) */
        async function controlTimer(action, timerKey, produtoNome) {
            if (!timerKey) return;
            produtoNome = produtoNome || timerKey;
            try {
                const res = await fetch('/api/timer/action', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        action: action,
                        timer_key: timerKey,
                        produto_nome: produtoNome,
                        produto: timerKey  // retrocompatibilidade
                    })
                });
                if (!res.ok) throw new Error('HTTP ' + res.status);
                const data = await res.json();

                if (action === 'finish') {
                    clearInterval(timerInterval);
                    timerInterval = null;
                    const nome = data.produto || produtoNome;
                    const elapsed = (data.registro && data.registro.tempo_segundos) || data.elapsed || 0;
                    showToast('✅ Concluído!', nome + ' — ' + formatSeconds(elapsed) + ' registrado.', 'success');
                    const modalEl = document.getElementById('productionModal');
                    if (modalEl) {
                        try {
                            (bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl)).hide();
                        } catch(me) { modalEl.remove(); }
                    }
                    loadProductionBoard();
                    fetchAPI('/api/consumption/summary').then(d => renderConsumptionTable(d)).catch(() => {});
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
                console.error('controlTimer:', e);
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
            let seconds = startSeconds;
            const display = document.getElementById('timer-display');
            
            timerInterval = setInterval(() => {
                seconds++;
                display.textContent = new Date(seconds * 1000).toISOString().substr(11, 8);
            }, 1000);
        }

        function updateTimerDisplay(seconds, state) {
            const display = document.getElementById('timer-display');
            const badge = document.getElementById('timer-status');
            
            display.textContent = new Date(seconds * 1000).toISOString().substr(11, 8);
            
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
                        <td class="text-muted small">${item.base || '—'}</td>
                        <td class="text-muted small">${item.cor || '—'}</td>
                        <td class="text-muted small">#${item.pedido_numero || item.order_id}</td>
                        <td class="text-muted small">${item.cliente || '—'}</td>
                        <td class="text-center">
                            <button class="btn btn-xs btn-success btn-sm fw-bold me-1"
                                data-ikey="${item.item_key}"
                                data-pnome="${(item.nome || item.nome_original || '').replace(/"/g,'')}"
                                onclick="startPendingOrder(this.dataset.ikey, this.dataset.pnome)">▶ Produzir</button>
                            <button class="btn btn-xs btn-outline-secondary btn-sm"
                                data-dkey="${item.item_key}"
                                onclick="dismissPendingOrder(this.dataset.dkey)">✕</button>
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
                    const itemKey = item.item_key || null;
                    const base    = item.base || '—';
                    const cor     = item.cor  || '—';
                    const chkDone = Object.values(item.checklist || {}).filter(Boolean).length;
                    const chkTotal= RECIPE_CADEIRA.length;

                    // Guarda estado para ticker local
                    _boardTimerState[nome] = { base: elapsed, startedAt: Date.now() / 1000, estado, serverTime };

                    // timerKey = item_key → cada unidade tem cronômetro independente
                    const timerKeyB = item.item_key || item.timer_key || nome;
                    const openBtn = '<button class="btn btn-xs btn-outline-primary btn-sm me-1"' +
                        ' data-tkey="' + timerKeyB + '" data-pnome="' + nomeSafe + '"' +
                        ' onclick="openProductionChecklist(this.dataset.pnome,this.dataset.tkey)">🛠 Abrir</button>';
                    const finishBtn = itemKey
                        ? '<button class="btn btn-xs btn-success btn-sm"' +
                          ' data-ikey="' + itemKey + '" data-tkey="' + timerKeyB + '" data-pnome="' + nomeSafe + '"' +
                          ' onclick="finishBoardItem(this.dataset.ikey,this.dataset.pnome,this.dataset.tkey)">✅ Concluir</button>'
                        : '<button class="btn btn-xs btn-success btn-sm"' +
                          ' data-tkey="' + timerKeyB + '" data-pnome="' + nomeSafe + '"' +
                          ' onclick="controlTimer('finish',this.dataset.tkey,this.dataset.pnome)">✅ Concluir</button>';

                    html += `<tr>
                        <td class="ps-3 fw-bold">${nomeSafe}</td>
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
                        <td class="text-center" style="white-space:nowrap;">${openBtn}${finishBtn}</td>
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
                        <td class="text-muted small">${item.base || '—'}</td>
                        <td class="text-muted small">${item.cor  || '—'}</td>
                        <td class="text-muted small">#${item.pedido_numero || item.order_id}</td>
                        <td class="text-muted small">${item.cliente || '—'}</td>
                        <td class="text-center"><span class="badge bg-success">✅ Concluído</span><br><small class="text-muted">${fin}</small></td>
                    </tr>`;
                });
                html += `</tbody></table></div>`;
            }

            div.innerHTML = html;

            // ── Ticker local (1s) ────────────────────────────────────────
            _boardTick = setInterval(() => {
                Object.entries(_boardTimerState).forEach(([nome, s]) => {
                    if (s.estado !== 'running') return;
                    const elapsed = s.base + (Date.now() / 1000 - s.startedAt);
                    const safeId  = nome.replace(/[^a-zA-Z0-9]/g, '_');
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
                await loadProductionBoard();
                // Passa itemKey como timerKey — cada unidade tem timer próprio
                openProductionChecklist(produtoNome, itemKey);
                showToast('✅ Iniciado', 'Produção: ' + produtoNome, 'success');
            } catch(e) {
                console.error('startPendingOrder:', e);
                showToast('Erro', 'Falha ao iniciar produção', 'danger');
            }
        }

        async function finishBoardItem(itemKey, produtoNome, timerKey) {
            if (!confirm('Concluir produção de "' + produtoNome + '"?')) return;
            timerKey = timerKey || itemKey;
            try {
                await fetch('/api/pending-orders/finish', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ item_key: itemKey, produto_nome: produtoNome, timer_key: timerKey })
                });
                showToast('✅ Concluído!', produtoNome, 'success');
                loadProductionBoard();
                fetchAPI('/api/consumption/summary').then(d => renderConsumptionTable(d)).catch(() => {});
            } catch(e) { showToast('Erro', 'Falha ao concluir', 'danger'); }
        }

        async function dismissPendingOrder(itemKey) {
            if (!confirm('Remover este pedido da fila?')) return;
            try {
                await fetch('/api/pending-orders/dismiss', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ item_key: itemKey })
                });
                await loadProductionBoard();
            } catch(e) { showToast('Erro', 'Falha ao remover pedido', 'danger'); }
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

        // renderActiveTimers mantido como stub (o board substituiu)
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
                    <td class="fw-bold">${h.produto}</td>
                    <td class="text-center font-monospace fw-bold text-primary">${formatSeconds(h.tempo_segundos)}</td>
                </tr>`).join('')}</tbody></table></div>`;
        }



        function formatSeconds(s) {
            s = Math.floor(s || 0);
            const h = Math.floor(s / 3600).toString().padStart(2, '0');
            const m = Math.floor((s % 3600) / 60).toString().padStart(2, '0');
            const sec = (s % 60).toString().padStart(2, '0');
            return `${h}:${m}:${sec}`;
        }


        /* ✅ WebSocket KPI — com reconexão robusta */
        const protoKpi = window.location.protocol === 'https:' ? 'wss' : 'ws';
        let wsKpi = null;
        let _kpiReconnectTimer = null;

        function setupKpiWebSocket() {
            if (wsKpi) { try { wsKpi.close(); } catch(e) {} }
            wsKpi = new WebSocket(`${protoKpi}://${window.location.host}/ws/kpi-updates`);

            wsKpi.onmessage = (e) => {
                try {
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
                } catch(err) {
                    console.error('WS KPI parse error:', err);
                }
            };

            wsKpi.onerror = (e) => {
                console.error("Erro WebSocket KPI:", e);
            };

            wsKpi.onclose = () => {
                console.log("WebSocket KPI desconectado. Reconectando em 4s...");
                if (_kpiReconnectTimer) clearTimeout(_kpiReconnectTimer);
                _kpiReconnectTimer = setTimeout(setupKpiWebSocket, 4000);
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
                            borderColor: '#ffb600',
                            backgroundColor: 'rgba(255, 182, 0, 0.1)',
                            tension: 0.4,
                            fill: true,
                            borderWidth: 2
                        }, {
                            label: 'Média Móvel (7 dias)',
                            data: data.moving_avg,
                            borderColor: '#01010d',
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
            if (kpiTab) kpiTab.addEventListener('shown.bs.tab', loadKPIChart);

            const componentUsageTab = document.querySelector('[data-bs-target="#component-usage"]');
            if (componentUsageTab) {
                componentUsageTab.addEventListener('shown.bs.tab', () => {
                    refreshComponentTab();
                    loadProductionBoard();
                    // Inicia polling automático a cada 10s (atualiza timers ao vivo para todos)
                    if (!_boardPoll) {
                        _boardPoll = setInterval(loadProductionBoard, 10000);
                    }
                });
                componentUsageTab.addEventListener('hidden.bs.tab', () => {
                    // Para polling e ticker ao sair da aba (economiza recursos)
                    if (_boardPoll) { clearInterval(_boardPoll); _boardPoll = null; }
                    if (_boardTick) { clearInterval(_boardTick); _boardTick = null; }
                });
            }
        });
    </script>

    <!-- ✅ Footer SW Móveis MDF -->
    <footer class="sw-footer">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.5rem;">
            <div>
                <div class="sw-footer-brand">SW MÓVEIS MDF</div>
                <div>Gestão Inteligente de Produção — Design inteligente, funcionalidade e conforto</div>
            </div>
            <div style="text-align:right;">
                <div>Desenvolvido por <strong style="color:var(--amarelo);">João Victor Dias Santana</strong></div>
                <div style="color:rgba(255,255,255,0.4);">Versão 4.6 — 2025</div>
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
    
    # ✅ REGRA DE OURO: Define uma SECRET_KEY estável para persistência de sessão
    # Isso evita que o Flask invalide cookies a cada reinício do servidor.
    flask_app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'sw-moveis-mdf-secure-key-2025')
    
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