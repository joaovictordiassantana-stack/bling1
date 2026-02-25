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
        # Recupera checklist antes de pausar
        checklist_marcado = {}
        if produto_nome in self.timers:
            checklist_marcado = self.timers[produto_nome].get('checklist', {})
            status = self.pause(produto_nome)
            total_seconds = status['elapsed']
        else:
            # Timer não existe (concluído direto do board sem abrir modal)
            total_seconds = 0

        # Registra componentes da receita não marcados manualmente
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
            logger.info(f"✅ Componentes registrados para '{produto_nome}'")

        registro = {
            "produto": produto_nome,
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
            action = data.get('action') # start, pause, reset, finish
            produto = data.get('produto')
            
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
        def api_production_board():
            """
            Retorna snapshot completo da aba de produção.
            - waiting: pedidos do Bling aguardando alguém clicar em Produzir
            - in_production: pedidos em andamento + tempo ao vivo do timer
            - done: concluídos do mês (para histórico)
            - timers_orphan: timers sem item_key (iniciados manualmente)
            """
            # Mapa de produto_nome -> timer
            timers = production_timer.timers
            timer_map = {}
            for nome, t in timers.items():
                total = t.get('accumulated', 0)
                if t.get('state') == 'running' and t.get('start_ts', 0) > 0:
                    total += time.time() - t['start_ts']
                timer_map[nome] = {
                    'estado': t.get('state', 'paused'),
                    'tempo_decorrido': int(total),
                    'checklist': t.get('checklist', {}),
                    'created_at': t.get('created_at', ''),
                }

            # Enriquece in_production com dados do timer
            in_prod = []
            for item in pending_orders.get_in_production():
                nome = item.get('nome') or item.get('nome_original', '')
                t_info = timer_map.get(nome, {})
                in_prod.append({**item, **t_info})

            # Timers sem pedido vinculado (iniciados manualmente)
            nomes_com_pedido = {
                (v.get('nome') or v.get('nome_original', ''))
                for v in pending_orders.data.values()
            }
            orphan = []
            for nome, t in timers.items():
                if nome not in nomes_com_pedido:
                    total = t.get('accumulated', 0)
                    if t.get('state') == 'running' and t.get('start_ts', 0) > 0:
                        total += time.time() - t['start_ts']
                    orphan.append({
                        'nome': nome,
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

        @self.app.route('/api/checklist/state/<path:produto>', methods=['GET'])
        def api_checklist_get(produto):
            """Retorna estado salvo da checklist de um produto em produção."""
            t = production_timer.timers.get(produto, {})
            return jsonify({'checklist': t.get('checklist', {})})

        @self.app.route('/api/checklist/state', methods=['POST'])
        def api_checklist_set():
            """Salva estado de um item da checklist no servidor (persiste)."""
            data = request.json
            produto = data.get('produto', '')
            componente = data.get('componente', '')
            checked = data.get('checked', False)
            if produto and componente and produto in production_timer.timers:
                if 'checklist' not in production_timer.timers[produto]:
                    production_timer.timers[produto]['checklist'] = {}
                production_timer.timers[produto]['checklist'][componente] = checked
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

            # Notifica TODOS os usuários via WebSocket sobre o novo insumo registrado
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
            """Move pedido de 'Em Espera' para 'Em Produção' e inicia timer."""
            data = request.json
            item_key = data.get('item_key', '')
            produto_nome = data.get('produto_nome', '')
            if not item_key:
                return jsonify({'error': 'item_key obrigatório'}), 400
            item = pending_orders.start_production(item_key)
            # Inicia o timer de produção com o nome do produto
            if produto_nome:
                production_timer.start(produto_nome)
            return jsonify({'success': True, 'item': item})

        @self.app.route('/api/pending-orders/finish', methods=['POST'])
        def api_pending_orders_finish():
            """Finaliza produção de um pedido pendente."""
            data = request.json
            item_key = data.get('item_key', '')
            produto_nome = data.get('produto_nome', '')
            if not item_key:
                return jsonify({'error': 'item_key obrigatório'}), 400
            item = pending_orders.finish_production(item_key)
            # Finaliza o timer
            if produto_nome:
                production_timer.stop_and_log(produto_nome)
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
    <title>SW Móveis MDF — Painel de Gestão</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
    <style>
        /* ══════════════════════════════════════════
           SW MÓVEIS MDF — DESIGN SYSTEM
           Cores: Manual ID Visual 2025
        ══════════════════════════════════════════ */
        :root {
            --sw-yellow: #ffb600;
            --sw-yellow-light: #fede8f;
            --sw-yellow-pale: #f5f883;
            --sw-black: #01010d;
            --sw-gray: #807f7f;
            --sw-nurse: #ecedec;

            /* Alias funcionais */
            --primary: var(--sw-black);
            --accent: var(--sw-yellow);
            --accent-light: var(--sw-yellow-light);
            --success: #10b981;
            --warning: var(--sw-yellow);
            --error: #ef4444;
            --bg: #f9f9f7;
            --bg-card: #ffffff;
            --border: rgba(1,1,13,0.08);
            --text-muted: var(--sw-gray);

            /* Spacing / Radius */
            --radius-sm: 6px;
            --radius: 12px;
            --radius-lg: 20px;
            --shadow: 0 2px 12px rgba(1,1,13,0.07);
            --shadow-hover: 0 12px 40px rgba(1,1,13,0.13);
        }

        /* ══ RESET & BASE ══ */
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

        html { scroll-behavior: smooth; }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: var(--bg);
            color: var(--primary);
            font-size: 14px;
            line-height: 1.6;
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
            overflow-x: hidden;
        }

        /* ══ TIPOGRAFIA ══ */
        .font-bebas { font-family: 'Bebas Neue', sans-serif; letter-spacing: 0.04em; }
        h1, h2, h3, h4, h5, h6 { font-weight: 700; line-height: 1.2; }

        /* ══ NAVBAR ══ */
        .sw-nav {
            background: var(--sw-black);
            border-bottom: 3px solid var(--sw-yellow);
            padding: 0 1.5rem;
            height: 64px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            position: sticky;
            top: 0;
            z-index: 1000;
            will-change: transform;
        }

        .sw-nav-brand {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            text-decoration: none;
        }

        .sw-nav-brand img {
            height: 38px;
            width: auto;
        }

        .sw-nav-brand-text {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 1.4rem;
            color: var(--sw-yellow);
            letter-spacing: 0.06em;
            line-height: 1;
        }

        .sw-nav-brand-sub {
            font-size: 0.6rem;
            color: rgba(255,255,255,0.5);
            letter-spacing: 0.15em;
            text-transform: uppercase;
            display: block;
        }

        .sw-nav-right {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        /* ══ STATUS BADGE ══ */
        #status-badge {
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            padding: 0.3rem 0.85rem;
            border-radius: 50px;
            font-size: 0.72rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            transition: all 0.3s ease;
        }

        #status-badge.bg-success {
            background: #10b981 !important;
            color: white;
            box-shadow: 0 0 16px rgba(16,185,129,0.4);
        }

        #status-badge.bg-danger {
            background: #ef4444 !important;
            color: white;
        }

        #status-badge.bg-secondary {
            background: rgba(255,255,255,0.1) !important;
            color: rgba(255,255,255,0.6);
        }

        #status-badge::before {
            content: '';
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: currentColor;
            animation: pulse-dot 2s infinite;
        }

        @keyframes pulse-dot {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.5; transform: scale(0.8); }
        }

        /* ══ BTN AUTH ══ */
        #auth-link {
            padding: 0.4rem 1rem;
            border: 1.5px solid var(--sw-yellow);
            color: var(--sw-yellow);
            border-radius: var(--radius-sm);
            font-size: 0.78rem;
            font-weight: 600;
            text-decoration: none;
            letter-spacing: 0.04em;
            transition: all 0.2s ease;
            white-space: nowrap;
        }

        #auth-link:hover {
            background: var(--sw-yellow);
            color: var(--sw-black);
        }

        /* ══ MAIN CONTAINER ══ */
        .sw-main {
            max-width: 1440px;
            margin: 0 auto;
            padding: 2rem 1.5rem 4rem;
        }

        /* ══ PAGE HEADER ══ */
        .sw-page-header {
            margin-bottom: 2rem;
            padding-bottom: 1.5rem;
            border-bottom: 1px solid var(--border);
        }

        .sw-page-title {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 2.2rem;
            color: var(--sw-black);
            letter-spacing: 0.04em;
            line-height: 1;
        }

        .sw-page-title span {
            color: var(--sw-yellow);
        }

        .sw-page-sub {
            color: var(--text-muted);
            font-size: 0.85rem;
            margin-top: 0.3rem;
        }

        /* ══ KPI CARDS ══ */
        .kpi-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 1rem;
            margin-bottom: 2rem;
        }

        @media (max-width: 768px) {
            .kpi-grid { grid-template-columns: 1fr; }
        }

        .kpi-card {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 1.5rem;
            position: relative;
            overflow: hidden;
            transition: transform 0.25s ease, box-shadow 0.25s ease;
            will-change: transform;
        }

        .kpi-card::after {
            content: '';
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            height: 3px;
        }

        .kpi-daily::after { background: var(--sw-yellow); }
        .kpi-weekly::after { background: var(--sw-yellow-light); }
        .kpi-historic::after { background: var(--success); }

        .kpi-card:hover {
            transform: translateY(-3px);
            box-shadow: var(--shadow-hover);
        }

        .kpi-card .kpi-label {
            font-size: 0.7rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            color: var(--text-muted);
            margin-bottom: 0.5rem;
        }

        .kpi-card .kpi-value {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 3rem;
            line-height: 1;
            color: var(--sw-black);
        }

        .kpi-daily .kpi-value { color: var(--sw-yellow); }
        .kpi-weekly .kpi-value { color: #c49200; }
        .kpi-historic .kpi-value { color: var(--success); }

        .kpi-card .kpi-sub {
            font-size: 0.72rem;
            color: var(--text-muted);
            margin-top: 0.4rem;
        }

        .kpi-card.updating {
            animation: kpi-flash 0.5s ease;
        }

        @keyframes kpi-flash {
            0% { background: var(--sw-yellow-pale); }
            100% { background: var(--bg-card); }
        }

        /* ══ CARD ══ */
        .sw-card {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            overflow: hidden;
            box-shadow: var(--shadow);
            transition: box-shadow 0.25s ease;
        }

        .sw-card:hover { box-shadow: var(--shadow-hover); }

        .sw-card-header {
            background: var(--sw-black);
            color: white;
            padding: 1rem 1.25rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.75rem;
        }

        .sw-card-header h5, .sw-card-header h6 {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 1rem;
            letter-spacing: 0.06em;
            margin: 0;
            color: white;
        }

        .sw-card-header .sw-accent { color: var(--sw-yellow); }

        .sw-card-header small {
            font-size: 0.7rem;
            color: rgba(255,255,255,0.5);
            font-weight: 400;
        }

        .sw-card-body { padding: 1.25rem; }

        /* ══ ACCENT CARD HEADERS (abas coloridas) ══ */
        .sw-card-header.success { background: linear-gradient(135deg, #065f46, #059669); }
        .sw-card-header.purple { background: linear-gradient(135deg, #3b0764, #7c3aed); }
        .sw-card-header.production { background: linear-gradient(135deg, var(--sw-black), #1e3a5f); }

        /* ══ TIMESTAMP ══ */
        .sw-timestamp {
            font-size: 0.72rem;
            color: var(--text-muted);
            display: flex;
            align-items: center;
            gap: 0.3rem;
            margin-bottom: 1.5rem;
        }

        .sw-timestamp span { font-weight: 600; color: var(--sw-black); }

        /* ══ LOG BOX ══ */
        .log-box {
            font-family: 'Fira Code', 'Cascadia Code', 'Consolas', monospace;
            font-size: 0.76rem;
            background: var(--sw-black);
            color: #d4d4d4;
            border-radius: 0 0 var(--radius) var(--radius);
            padding: 1rem;
            max-height: 320px;
            overflow-y: auto;
            line-height: 1.6;
        }

        .log-box::-webkit-scrollbar { width: 4px; }
        .log-box::-webkit-scrollbar-track { background: rgba(255,255,255,0.03); }
        .log-box::-webkit-scrollbar-thumb { background: rgba(255,182,0,0.3); border-radius: 2px; }
        .log-box::-webkit-scrollbar-thumb:hover { background: rgba(255,182,0,0.5); }

        .log-entry { padding: 0.15rem 0; animation: log-in 0.25s ease-out; }

        @keyframes log-in {
            from { opacity: 0; transform: translateX(-8px); }
            to { opacity: 1; transform: translateX(0); }
        }

        .log-level-INFO { color: #4ec9b0; }
        .log-level-WARNING { color: var(--sw-yellow); }
        .log-level-ERROR { color: #f48771; }
        .log-level-DEBUG { color: #569cd6; }

        /* ══ TABS ══ */
        .sw-tabs {
            display: flex;
            gap: 0.25rem;
            border-bottom: 2px solid var(--border);
            margin-bottom: 1.5rem;
            overflow-x: auto;
        }

        .sw-tabs::-webkit-scrollbar { display: none; }

        .sw-tab-btn {
            padding: 0.65rem 1.1rem;
            font-size: 0.78rem;
            font-weight: 600;
            color: var(--text-muted);
            background: none;
            border: none;
            border-bottom: 3px solid transparent;
            cursor: pointer;
            transition: all 0.2s ease;
            white-space: nowrap;
            margin-bottom: -2px;
            letter-spacing: 0.03em;
        }

        .sw-tab-btn:hover { color: var(--sw-black); }

        .sw-tab-btn.active {
            color: var(--sw-black);
            border-bottom-color: var(--sw-yellow);
            font-weight: 700;
        }

        /* Bootstrap tab wrapper */
        .nav-tabs { border: none; }
        .nav-tabs .nav-link {
            padding: 0.65rem 1.1rem;
            font-size: 0.78rem;
            font-weight: 600;
            color: var(--text-muted);
            border: none;
            border-bottom: 3px solid transparent;
            margin-bottom: -2px;
            letter-spacing: 0.03em;
            transition: all 0.2s ease;
            background: none;
        }
        .nav-tabs .nav-link:hover { color: var(--sw-black); border-bottom-color: rgba(255,182,0,0.4); }
        .nav-tabs .nav-link.active {
            color: var(--sw-black);
            border-bottom-color: var(--sw-yellow);
            font-weight: 700;
            background: none;
        }
        .nav-tabs-wrapper {
            border-bottom: 2px solid var(--border);
            margin-bottom: 1.5rem;
            overflow-x: auto;
        }
        .nav-tabs-wrapper::-webkit-scrollbar { display: none; }

        /* ══ FORM CONTROLS ══ */
        .form-control, .form-select {
            border: 1.5px solid var(--border);
            border-radius: var(--radius-sm);
            padding: 0.65rem 0.9rem;
            font-size: 0.85rem;
            font-weight: 500;
            color: var(--sw-black);
            background: white;
            transition: border-color 0.2s ease, box-shadow 0.2s ease;
        }

        .form-control:focus, .form-select:focus {
            border-color: var(--sw-yellow);
            box-shadow: 0 0 0 3px rgba(255,182,0,0.15);
            outline: none;
        }

        .form-control::placeholder { color: var(--text-muted); font-weight: 400; }

        /* ══ BUTTONS ══ */
        .btn {
            font-weight: 600;
            font-size: 0.8rem;
            letter-spacing: 0.03em;
            border-radius: var(--radius-sm);
            transition: all 0.2s ease;
            cursor: pointer;
        }

        .btn-primary {
            background: var(--sw-yellow);
            border-color: var(--sw-yellow);
            color: var(--sw-black);
        }

        .btn-primary:hover {
            background: #e6a400;
            border-color: #e6a400;
            color: var(--sw-black);
            transform: translateY(-1px);
            box-shadow: 0 6px 20px rgba(255,182,0,0.35);
        }

        .btn-primary:active { transform: translateY(0); }

        .btn-outline-light {
            border: 1.5px solid rgba(255,255,255,0.3);
            color: white;
            background: transparent;
        }

        .btn-outline-light:hover {
            background: rgba(255,255,255,0.1);
            border-color: rgba(255,255,255,0.6);
            color: white;
        }

        .btn-outline-secondary {
            border: 1.5px solid var(--border);
            color: var(--text-muted);
            background: white;
        }

        .btn-outline-secondary:hover {
            background: var(--bg);
            border-color: var(--sw-yellow);
            color: var(--sw-black);
        }

        .btn-sm { padding: 0.35rem 0.8rem; font-size: 0.76rem; }

        /* ══ TABLE ══ */
        .table {
            font-size: 0.82rem;
            margin: 0;
        }

        .table thead th {
            background: var(--bg);
            border: none;
            border-bottom: 2px solid var(--border);
            font-weight: 700;
            color: var(--text-muted);
            font-size: 0.68rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            padding: 0.85rem 1rem;
        }

        .table tbody tr {
            border-bottom: 1px solid var(--border);
            transition: background 0.15s ease;
        }

        .table tbody tr:hover { background: rgba(255,182,0,0.05); }
        .table tbody tr:last-child { border-bottom: none; }
        .table td { padding: 0.8rem 1rem; vertical-align: middle; }

        /* ══ BADGES ══ */
        .badge {
            font-weight: 700;
            font-size: 0.65rem;
            letter-spacing: 0.06em;
            padding: 0.3rem 0.65rem;
            border-radius: 50px;
            text-transform: uppercase;
        }

        .badge.bg-success { background: #10b981 !important; }
        .badge.bg-warning { background: var(--sw-yellow) !important; color: var(--sw-black) !important; }
        .badge.bg-danger { background: #ef4444 !important; }
        .badge.bg-secondary { background: var(--sw-gray) !important; }
        .badge.bg-info { background: #3b82f6 !important; }
        .badge.bg-light { background: var(--sw-nurse) !important; color: var(--sw-black) !important; }

        /* ══ ALERTS ══ */
        .alert {
            border: none;
            border-left: 4px solid;
            border-radius: var(--radius-sm);
            font-size: 0.83rem;
            font-weight: 500;
            padding: 0.85rem 1.1rem;
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

        /* ══ METRIC BOXES (Dashboard tab) ══ */
        .metric-box {
            background: var(--sw-black);
            border-radius: var(--radius);
            padding: 1.25rem;
            color: white;
            text-align: center;
            transition: transform 0.2s ease, box-shadow 0.2s ease;
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
            font-size: 2.5rem;
            color: var(--sw-yellow);
            line-height: 1;
        }

        /* ══ INPUT GROUP ══ */
        .input-group { gap: 0.5rem; }
        .input-group .form-control { flex: 1; }
        .input-group .btn { flex-shrink: 0; }

        /* ══ LIST GROUP ══ */
        .list-group-item {
            border: 1px solid var(--border);
            border-radius: var(--radius-sm) !important;
            margin-bottom: 0.4rem;
            padding: 0.85rem 1rem;
            font-size: 0.83rem;
            transition: all 0.2s ease;
        }

        .list-group-item:hover {
            border-color: var(--sw-yellow);
            background: rgba(255,182,0,0.04);
            transform: translateX(3px);
        }

        /* ══ ACCORDION ══ */
        .accordion-button {
            font-weight: 600;
            font-size: 0.85rem;
            background: var(--bg);
        }

        .accordion-button:not(.collapsed) {
            background: rgba(255,182,0,0.08);
            color: var(--sw-black);
            box-shadow: none;
        }

        .accordion-button:focus {
            box-shadow: 0 0 0 3px rgba(255,182,0,0.2);
        }

        .accordion-button::after {
            filter: none;
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
            from { opacity: 0; transform: translateX(50px); }
            to { opacity: 1; transform: translateX(0); }
        }

        .toast.hide { animation: toast-out 0.25s ease forwards; }

        @keyframes toast-out {
            to { opacity: 0; transform: translateX(50px); }
        }

        .toast-header {
            background: transparent;
            border-bottom: 1px solid rgba(255,255,255,0.1);
            color: white;
        }

        .toast-header strong { color: var(--sw-yellow); }
        .toast-header .btn-close { filter: invert(1); }
        .toast-body { color: rgba(255,255,255,0.85); font-size: 0.82rem; }

        /* ══ MODAL ══ */
        .modal-content {
            border: none;
            border-radius: var(--radius-lg);
            overflow: hidden;
            box-shadow: 0 25px 80px rgba(1,1,13,0.3);
        }

        .modal-header {
            background: var(--sw-black);
            border-bottom: 3px solid var(--sw-yellow);
            color: white;
        }

        .modal-title { font-family: 'Bebas Neue', sans-serif; font-size: 1.1rem; letter-spacing: 0.06em; }
        .modal-body { background: var(--bg); }
        .modal-footer { background: white; border-top: 1px solid var(--border); }

        /* ══ PRODUCTION BOARD ══ */
        .prod-col-header {
            padding: 0.75rem 1rem;
            font-size: 0.68rem;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            border-bottom: 1px solid var(--border);
            display: flex;
            align-items: center;
            gap: 0.4rem;
        }

        .prod-col-header .count-pill {
            background: var(--sw-yellow);
            color: var(--sw-black);
            padding: 0.1rem 0.5rem;
            border-radius: 50px;
            font-size: 0.65rem;
        }

        .prod-card-item {
            padding: 0.75rem 1rem;
            border-bottom: 1px solid var(--border);
            cursor: pointer;
            transition: background 0.15s ease;
            font-size: 0.8rem;
        }

        .prod-card-item:hover { background: rgba(255,182,0,0.06); }
        .prod-card-item:last-child { border-bottom: none; }

        .prod-card-item .prod-name {
            font-weight: 600;
            color: var(--sw-black);
        }

        .prod-card-item .prod-meta {
            font-size: 0.72rem;
            color: var(--text-muted);
        }

        .prod-timer {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 1.1rem;
            color: var(--sw-yellow);
        }

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
        .text-yellow { color: var(--sw-yellow) !important; }
        .text-muted { color: var(--text-muted) !important; }

        /* ══ FOOTER ══ */
        .sw-footer {
            background: var(--sw-black);
            border-top: 3px solid var(--sw-yellow);
            padding: 1.5rem 2rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 0.75rem;
        }

        .sw-footer p { color: rgba(255,255,255,0.7); font-size: 0.78rem; margin: 0; }
        .sw-footer strong { color: var(--sw-yellow); }
        .sw-footer small { color: rgba(255,255,255,0.4); font-size: 0.7rem; }

        /* ══ PATTERN ACCENT (decorativo, leve) ══ */
        .sw-pattern-bar {
            height: 6px;
            background: repeating-linear-gradient(
                90deg,
                var(--sw-yellow) 0px,
                var(--sw-yellow) 12px,
                var(--sw-black) 12px,
                var(--sw-black) 18px
            );
        }

        /* ══ ANIMATIONS GLOBAIS ══ */
        @keyframes fadeInUp {
            from { opacity: 0; transform: translateY(12px); }
            to { opacity: 1; transform: translateY(0); }
        }

        @keyframes slideDown {
            from { opacity: 0; transform: translateY(-10px); }
            to { opacity: 1; transform: translateY(0); }
        }

        @keyframes pulse-animation {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        .pulse-animation { animation: pulse-animation 2s infinite; }
        .fade-in-up { animation: fadeInUp 0.4s ease-out both; }

        /* ══ RESPONSIVO ══ */
        @media (max-width: 768px) {
            .sw-main { padding: 1.25rem 1rem 3rem; }
            .kpi-grid { gap: 0.75rem; }
            .kpi-card .kpi-value { font-size: 2.2rem; }
            .sw-footer { flex-direction: column; text-align: center; }
            .metric-value { font-size: 1.8rem; }
        }

        /* ══ CHART customization ══ */
        canvas { display: block; }

        /* ══ TAB CONTENT ANIMATION ══ */
        .tab-pane.active { animation: fadeInUp 0.3s ease-out; }
    </style>
</head>
<body>

    <!-- ══ PATTERN BAR TOP ══ -->
    <div class="sw-pattern-bar"></div>

    <!-- ══ NAVBAR ══ -->
    <nav class="sw-nav">
        <a class="sw-nav-brand" href="#">
            <img src="https://i.imgur.com/j79HO6n.png" alt="SW Móveis MDF">
            <div>
                <div class="sw-nav-brand-text">SW Móveis MDF</div>
                <span class="sw-nav-brand-sub">Painel de Gestão</span>
            </div>
        </a>
        <div class="sw-nav-right">
            <span id="status-badge" class="badge bg-secondary">Carregando...</span>
            <a id="auth-link" href="{{ auth_url }}">Autenticar</a>
        </div>
    </nav>

    <!-- ══ MAIN ══ -->
    <div class="sw-main">

        <!-- PAGE HEADER -->
        <div class="sw-page-header fade-in-up">
            <h1 class="sw-page-title">Pedidos de <span>Venda</span></h1>
            <p class="sw-page-sub">Acompanhe pedidos abertos e fechados em tempo real</p>
        </div>

        <!-- KPI CARDS -->
        <div class="kpi-grid">
            <div class="kpi-card kpi-daily fade-in-up">
                <div class="kpi-label">⚡ Pedidos Diários</div>
                <div class="kpi-value" id="kpi-daily">0</div>
                <div class="kpi-sub">Últimas 24h</div>
            </div>
            <div class="kpi-card kpi-weekly fade-in-up" style="animation-delay: 0.07s">
                <div class="kpi-label">📅 Pedidos Semanais</div>
                <div class="kpi-value" id="kpi-weekly">0</div>
                <div class="kpi-sub">Últimos 7 dias</div>
            </div>
            <div class="kpi-card kpi-historic fade-in-up" style="animation-delay: 0.14s">
                <div class="kpi-label">📊 Pedidos Mensais</div>
                <div class="kpi-value" id="kpi-historic">0</div>
                <div class="kpi-sub">Este mês</div>
            </div>
        </div>

        <!-- TIMESTAMP -->
        <div class="sw-timestamp mb-4">
            ⏱ Último Recálculo: <span id="last-recalculated">N/D</span>
        </div>

        <!-- LOGS CARD -->
        <div class="sw-card mb-4 fade-in-up">
            <div class="sw-card-header">
                <div>
                    <h5>📋 Logs <span class="sw-accent">em Tempo Real</span></h5>
                </div>
            </div>
            <div id="logs-content" class="log-box"></div>
        </div>

        <!-- TABS -->
        <div class="nav-tabs-wrapper">
            <ul class="nav nav-tabs" id="myTab" role="tablist">
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
        </div>

        <!-- AUTH REQUIRED MESSAGE -->
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
                            <input type="text" class="form-control" id="search-input" placeholder="Digite SKU ou nome do produto...">
                            <button class="btn btn-primary" id="btn-search" type="button">Buscar</button>
                        </div>
                    </div>
                </div>
                <div id="search-results"></div>
            </div>

            <!-- TAB: PRODUTOS -->
            <div class="tab-pane fade" id="kits" role="tabpanel">
                <div class="mb-4 d-flex align-items-center gap-3">
                    <button class="btn btn-primary btn-sm" onclick="forceAndReloadKits(event)">🔄 Recarregar Lista</button>
                    <small class="text-muted">⚠️ Carregamento pode levar 2-5 minutos. Aguarde a notificação.</small>
                </div>
                <div id="kits-list"></div>
            </div>

            <!-- TAB: DASHBOARD KPI -->
            <div class="tab-pane fade" id="kpi-chart" role="tabpanel">
                <div class="row g-4">
                    <div class="col-lg-8">
                        <div class="sw-card">
                            <div class="sw-card-header">
                                <h5>📈 Evolução de Pedidos <span class="sw-accent">(30 dias)</span></h5>
                            </div>
                            <div class="sw-card-body" style="height: 380px;">
                                <canvas id="salesChart"></canvas>
                            </div>
                        </div>
                    </div>
                    <div class="col-lg-4">
                        <div class="sw-card h-100">
                            <div class="sw-card-header">
                                <h5>🎯 Métricas <span class="sw-accent">Rápidas</span></h5>
                            </div>
                            <div class="sw-card-body d-flex flex-column gap-3">
                                <div class="metric-box">
                                    <div class="metric-label">Média Diária</div>
                                    <div class="metric-value" id="avg-daily">0</div>
                                </div>
                                <div class="metric-box">
                                    <div class="metric-label">Crescimento Semanal</div>
                                    <div class="metric-value" id="growth-weekly">+0%</div>
                                </div>
                                <div class="metric-box">
                                    <div class="metric-label">Tendência</div>
                                    <div class="metric-value" id="trend-indicator" style="font-size: 1.5rem;">📊 Estável</div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- TAB: INSUMOS & PRODUÇÃO -->
            <div class="tab-pane fade" id="component-usage" role="tabpanel">

                <!-- PAINEL DE PRODUÇÃO -->
                <div class="sw-card mb-4">
                    <div class="sw-card-header production">
                        <div>
                            <h5>🏭 Painel de <span class="sw-accent">Produção</span></h5>
                            <small>
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

                <!-- CONSUMO MENSAL -->
                <div class="sw-card mb-4">
                    <div class="sw-card-header success">
                        <div>
                            <h5>📊 Consumo de Insumos</h5>
                            <small id="consumption-month-label">Mês atual • Reinicia todo mês</small>
                        </div>
                        <span class="badge bg-light" id="consumption-total-badge">0 insumos</span>
                    </div>
                    <div class="card-body p-0" id="consumption-table-section">
                        <div class="text-center py-4 text-muted">⏳ Carregando consumo...</div>
                    </div>
                </div>

                <!-- HISTÓRICO -->
                <div class="sw-card">
                    <div class="sw-card-header purple">
                        <div>
                            <h5>📜 Histórico de Finalizações</h5>
                            <small>Registro de cada produto finalizado com tempo de produção</small>
                        </div>
                    </div>
                    <div class="card-body p-0" id="production-history-section">
                        <div class="text-center py-4 text-muted">⏳ Carregando histórico...</div>
                    </div>
                </div>

            </div>
        </div>
    </div>

    <!-- TOAST CONTAINER -->
    <div class="toast-container position-fixed bottom-0 end-0 p-4"></div>

    <!-- SCRIPTS -->
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>

    <script>
        const API = '/api';
        let isAuthenticated = false;
        let salesChart = null;

        /* ══ FETCH API ══ */
        async function fetchAPI(url, options = {}) {
            try {
                const response = await fetch(url, options);
                if (response.status === 401) {
                    console.error("Sessão expirada (401). Redirecionando para autenticação.");
                    window.location.href = document.getElementById('auth-link').href;
                    throw new Error("Sessão expirada. Redirecionamento em curso.");
                }
                if (!response.ok) {
                    let errMsg = `HTTP ${response.status}`;
                    try { const d = await response.json(); errMsg = d.error || d.message || errMsg; } catch(e) {}
                    throw new Error(errMsg);
                }
                return await response.json();
            } catch(e) {
                if (e.message !== "Sessão expirada. Redirecionamento em curso.") console.error(`fetchAPI(${url}):`, e);
                throw e;
            }
        }

        /* ══ TOAST ══ */
        function showToast(title, message, type = 'info') {
            const container = document.querySelector('.toast-container');
            const borderColors = { success: '#10b981', danger: '#ef4444', warning: '#ffb600', info: '#3b82f6' };
            const color = borderColors[type] || '#ffb600';
            const id = `toast-${Date.now()}`;
            const div = document.createElement('div');
            div.id = id;
            div.className = `toast show align-items-center`;
            div.setAttribute('role','alert');
            div.style.borderLeftColor = color;
            div.innerHTML = `
                <div class="toast-header">
                    <strong class="me-auto" style="color:${color}">${title}</strong>
                    <button type="button" class="btn-close" onclick="this.closest('.toast').remove()"></button>
                </div>
                <div class="toast-body">${message}</div>`;
            container.appendChild(div);
            setTimeout(() => { div.classList.add('hide'); setTimeout(() => div.remove(), 300); }, 4000);
        }

        /* ══ WEBSOCKET ══ */
        let ws = null;
        let wsReconnectDelay = 1000;
        let wsReconnectTimer = null;

        function connectWS() {
            if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
            const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
            ws = new WebSocket(`${protocol}://${location.host}/ws`);
            ws.onopen = () => { wsReconnectDelay = 1000; console.log('WS conectado'); };
            ws.onmessage = (e) => { try { handleWSMessage(JSON.parse(e.data)); } catch(err) { console.warn('WS msg erro:', err); } };
            ws.onclose = () => {
                console.warn('WS fechado. Reconectando em', wsReconnectDelay, 'ms');
                wsReconnectTimer = setTimeout(() => { wsReconnectDelay = Math.min(wsReconnectDelay * 1.5, 30000); connectWS(); }, wsReconnectDelay);
            };
            ws.onerror = (e) => { console.error('WS error:', e); ws.close(); };
        }

        function handleWSMessage(data) {
            if (data.type === 'log') {
                appendLog(data.level, data.message, data.timestamp);
            } else if (data.type === 'kpi_update') {
                updateKPIs(data);
            } else if (data.type === 'auth_status') {
                updateAuthStatus(data.authenticated, data.user);
            } else if (data.type === 'cache_loaded') {
                showToast('Cache Atualizado', '✅ Cache de produtos recarregado!', 'success');
                loadKits();
            } else if (data.type === 'cache_loading') {
                showToast('Carregando Cache', '⏳ Atualizando produtos do Bling...', 'info');
            } else if (data.type === 'production_update') {
                if (document.querySelector('#component-usage.active, #component-usage.show')) {
                    loadProductionBoard();
                    refreshComponentTab();
                }
            }
        }

        /* ══ LOGS ══ */
        function appendLog(level, message, timestamp) {
            const box = document.getElementById('logs-content');
            if (!box) return;
            const div = document.createElement('div');
            div.className = `log-entry log-level-${level}`;
            const ts = timestamp ? `<span style="opacity:0.5">[${timestamp}]</span> ` : '';
            div.innerHTML = `${ts}<span class="log-level-${level}">[${level}]</span> ${escapeHtml(message)}`;
            box.appendChild(div);
            if (box.children.length > 500) box.removeChild(box.firstChild);
            box.scrollTop = box.scrollHeight;
        }

        function escapeHtml(s) {
            return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        }

        /* ══ KPI UPDATE ══ */
        function updateKPIs(data) {
            const fields = { 'kpi-daily': data.daily, 'kpi-weekly': data.weekly, 'kpi-historic': data.monthly ?? data.historic };
            for (const [id, val] of Object.entries(fields)) {
                const el = document.getElementById(id);
                if (el && val !== undefined) {
                    el.textContent = val;
                    const card = el.closest('.kpi-card');
                    if (card) { card.classList.add('updating'); setTimeout(() => card.classList.remove('updating'), 600); }
                }
            }
            if (data.last_recalculated) {
                const el = document.getElementById('last-recalculated');
                if (el) el.textContent = data.last_recalculated;
            }
        }

        /* ══ AUTH STATUS ══ */
        function updateAuthStatus(authenticated, user) {
            isAuthenticated = authenticated;
            const badge = document.getElementById('status-badge');
            const authLink = document.getElementById('auth-link');
            const contentTabs = document.getElementById('content-tabs');
            const authRequired = document.getElementById('auth-required-tabs');
            if (authenticated) {
                badge.className = 'badge bg-success';
                badge.textContent = user ? `Conectado: ${user}` : 'Conectado';
                authLink.style.display = 'none';
                contentTabs.classList.remove('hidden');
                authRequired.classList.add('hidden');
            } else {
                badge.className = 'badge bg-danger';
                badge.textContent = 'Desconectado';
                authLink.style.display = '';
                contentTabs.classList.add('hidden');
                authRequired.classList.remove('hidden');
            }
        }

        /* ══ INIT ══ */
        async function init() {
            try {
                const data = await fetchAPI('/api/status');
                updateAuthStatus(data.authenticated, data.user);
                if (data.authenticated) {
                    await loadInitialKPI();
                    await loadLogs();
                }
                connectWS();
            } catch(e) {
                console.error('Init error:', e);
                connectWS();
            }
        }

        async function loadInitialKPI() {
            try {
                const data = await fetchAPI('/api/kpi');
                updateKPIs(data);
            } catch(e) { console.error('loadInitialKPI:', e); }
        }

        async function loadLogs() {
            try {
                const data = await fetchAPI('/api/logs');
                const box = document.getElementById('logs-content');
                if (!box) return;
                box.innerHTML = '';
                (data.logs || []).forEach(l => appendLog(l.level, l.message, l.timestamp));
            } catch(e) { console.error('loadLogs:', e); }
        }

        /* ══ SEARCH ══ */
        document.addEventListener('DOMContentLoaded', () => {
            init();

            const searchBtn = document.getElementById('btn-search');
            const searchInput = document.getElementById('search-input');

            if (searchBtn) searchBtn.addEventListener('click', doSearch);
            if (searchInput) {
                searchInput.addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
            }

            const kpiTab = document.querySelector('[data-bs-target="#kpi-chart"]');
            if (kpiTab) kpiTab.addEventListener('shown.bs.tab', loadKPIChart);

            const componentUsageTab = document.querySelector('[data-bs-target="#component-usage"]');
            if (componentUsageTab) {
                componentUsageTab.addEventListener('shown.bs.tab', () => {
                    refreshComponentTab();
                    loadProductionBoard();
                    if (!_boardPoll) _boardPoll = setInterval(loadProductionBoard, 10000);
                });
                componentUsageTab.addEventListener('hidden.bs.tab', () => {
                    if (_boardPoll) { clearInterval(_boardPoll); _boardPoll = null; }
                    if (_boardTick) { clearInterval(_boardTick); _boardTick = null; }
                });
            }

            loadKits();
        });

        async function doSearch() {
            if (!isAuthenticated) { showToast('Aviso', 'Faça login primeiro!', 'warning'); return; }
            const q = document.getElementById('search-input')?.value?.trim();
            if (!q) { showToast('Aviso', 'Digite um SKU ou nome.', 'warning'); return; }
            const div = document.getElementById('search-results');
            div.innerHTML = '<div class="text-center py-4 text-muted pulse-animation">🔍 Buscando...</div>';
            try {
                const data = await fetchAPI(`/api/search?q=${encodeURIComponent(q)}`);
                renderSearchResults(data, div);
            } catch(e) {
                div.innerHTML = `<div class="alert alert-danger">Erro: ${escapeHtml(e.message)}</div>`;
            }
        }

        function renderSearchResults(data, container) {
            if (!data || (!data.orders?.length && !data.products?.length)) {
                container.innerHTML = '<div class="alert alert-info">Nenhum resultado encontrado.</div>';
                return;
            }
            let html = '';
            if (data.orders?.length) {
                html += `<div class="sw-card mb-4"><div class="sw-card-header"><h5>📋 Pedidos <span class="sw-accent">(${data.orders.length})</span></h5></div><div class="sw-card-body p-0"><div class="table-responsive"><table class="table"><thead><tr><th>Número</th><th>Data</th><th>Cliente</th><th>Valor</th><th>Status</th></tr></thead><tbody>`;
                data.orders.forEach(o => {
                    html += `<tr><td><strong>${escapeHtml(o.numero||'')}</strong></td><td>${escapeHtml(o.data||'')}</td><td>${escapeHtml(o.cliente||'')}</td><td>R$ ${escapeHtml(String(o.valor||0))}</td><td><span class="badge bg-${o.situacao==='Em aberto'?'warning':'secondary'}">${escapeHtml(o.situacao||'')}</span></td></tr>`;
                });
                html += '</tbody></table></div></div></div>';
            }
            if (data.products?.length) {
                html += `<div class="sw-card"><div class="sw-card-header"><h5>📦 Produtos <span class="sw-accent">(${data.products.length})</span></h5></div><div class="sw-card-body p-0"><div class="table-responsive"><table class="table"><thead><tr><th>SKU</th><th>Nome</th><th>Preço</th></tr></thead><tbody>`;
                data.products.forEach(p => {
                    html += `<tr><td><code style="color:var(--sw-yellow);font-size:0.78rem">${escapeHtml(p.sku||'')}</code></td><td>${escapeHtml(p.nome||'')}</td><td>R$ ${escapeHtml(String(p.preco||0))}</td></tr>`;
                });
                html += '</tbody></table></div></div></div>';
            }
            container.innerHTML = html;
        }

        /* ══ KITS/PRODUCTS ══ */
        async function loadKits() {
            if (!isAuthenticated) return;
            const div = document.getElementById('kits-list');
            if (!div) return;
            div.innerHTML = '<div class="text-center py-4 text-muted pulse-animation">⏳ Carregando produtos...</div>';
            try {
                const data = await fetchAPI('/api/kits');
                if (!data || !data.kits || !data.kits.length) {
                    div.innerHTML = '<div class="alert alert-info">Nenhum produto no cache. Clique em Recarregar.</div>';
                    return;
                }
                let html = '<div class="table-responsive"><table class="table"><thead><tr><th style="width:60px"></th><th style="width:120px">SKU</th><th>Nome</th><th>Componentes</th></tr></thead><tbody>';
                data.kits.forEach(k => {
                    const imgHtml = k.imagem ? `<img src="${escapeHtml(k.imagem)}" style="width:48px;height:48px;object-fit:cover;border-radius:6px;border:1px solid var(--border)">` : '<div style="width:48px;height:48px;border-radius:6px;background:var(--bg);display:flex;align-items:center;justify-content:center;font-size:1.2rem">📦</div>';
                    let comps = '<span class="badge bg-secondary">Sem dados</span>';
                    if (k.tipo === 'kit') comps = `<span class="badge bg-success">Kit</span>`;
                    else if (k.tipo === 'produto') comps = `<span class="badge" style="background:var(--sw-yellow);color:var(--sw-black)">Produto</span>`;
                    else if (k.tipo === 'servico') comps = `<span class="badge bg-info">Serviço</span>`;
                    html += `<tr onclick="openProductionChecklist('${escapeHtml(k.nome)}')" style="cursor:pointer"><td>${imgHtml}</td><td><code style="font-size:0.75rem;color:var(--sw-yellow)">${escapeHtml(k.sku||'')}</code></td><td style="font-weight:600">${escapeHtml(k.nome||'N/D')}</td><td>${comps}</td></tr>`;
                });
                html += '</tbody></table></div>';
                div.innerHTML = html;
            } catch(e) {
                div.innerHTML = `<div class="alert alert-danger">Erro ao carregar lista. Verifique os logs.</div>`;
            }
        }

        async function forceAndReloadKits(event) {
            if (!isAuthenticated) { showToast('Aviso', 'Faça login primeiro!', 'warning'); return; }
            const btn = event.target;
            btn.disabled = true;
            btn.innerHTML = '⏳ Carregando cache...';
            try {
                await fetchAPI('/api/force-load', { method: 'POST' });
                showToast('Info', 'Cache sendo atualizado. Aguarde notificação.', 'info');
            } catch(e) {
                showToast('Erro', 'Erro: ' + e.message, 'danger');
                btn.disabled = false;
                btn.innerHTML = '🔄 Recarregar Lista';
            }
        }

        /* ══ KPI CHART ══ */
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
                            backgroundColor: 'rgba(255,182,0,0.1)',
                            tension: 0.4,
                            fill: true,
                            borderWidth: 2,
                            pointBackgroundColor: '#ffb600',
                            pointRadius: 3
                        }, {
                            label: 'Média Móvel (7 dias)',
                            data: data.moving_avg,
                            borderColor: 'rgba(255,255,255,0.3)',
                            borderDash: [5, 5],
                            tension: 0.4,
                            borderWidth: 2,
                            pointRadius: 0
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: {
                                position: 'top',
                                labels: { color: '#01010d', font: { family: 'Inter', weight: '600', size: 11 } }
                            },
                            tooltip: { mode: 'index', intersect: false }
                        },
                        scales: {
                            x: { grid: { color: 'rgba(0,0,0,0.05)' }, ticks: { color: '#807f7f', font: { size: 10 } } },
                            y: { beginAtZero: true, grid: { color: 'rgba(0,0,0,0.05)' }, ticks: { color: '#807f7f', font: { size: 10 } } }
                        }
                    }
                });
                const avgEl = document.getElementById('avg-daily');
                const growthEl = document.getElementById('growth-weekly');
                const trendEl = document.getElementById('trend-indicator');
                if (avgEl) avgEl.textContent = data.avg_daily?.toFixed(1) ?? '0';
                if (growthEl) growthEl.textContent = (data.growth > 0 ? '+' : '') + (data.growth?.toFixed(1) ?? '0') + '%';
                if (trendEl) trendEl.textContent = data.growth > 10 ? '📈 Crescendo' : data.growth < -10 ? '📉 Caindo' : '📊 Estável';
            } catch(e) { console.error('loadKPIChart:', e); }
        }

        /* ══ PRODUCTION BOARD ══ */
        let _boardPoll = null;
        let _boardTick = null;
        let _timerData = {};

        async function loadProductionBoard() {
            try {
                const data = await fetchAPI('/api/production/board');
                renderProductionBoard(data);
            } catch(e) { console.error('loadProductionBoard:', e); }
        }

        function renderProductionBoard(data) {
            const section = document.getElementById('production-board-section');
            if (!section) return;
            const waiting = data.waiting || [];
            const inProd = data.in_production || [];
            const done = data.done || [];

            const wBadge = document.getElementById('waiting-count-badge');
            const iBadge = document.getElementById('inprod-count-badge');
            const dBadge = document.getElementById('done-count-badge');
            if (wBadge) wBadge.textContent = waiting.length;
            if (iBadge) iBadge.textContent = inProd.length;
            if (dBadge) dBadge.textContent = done.length;

            _timerData = {};
            inProd.forEach(item => { if (item.timer_start) _timerData[item.id] = item.timer_start; });

            let html = '<div class="row g-0">';
            // Coluna Espera
            html += `<div class="col-md-4" style="border-right:1px solid var(--border)">
                <div class="prod-col-header" style="color:var(--sw-yellow)">⏳ Em Espera <span class="count-pill">${waiting.length}</span></div>`;
            if (!waiting.length) html += '<div class="text-center text-muted py-4" style="font-size:0.8rem">Nenhum pedido</div>';
            waiting.forEach(item => {
                html += `<div class="prod-card-item" onclick="openProductionChecklist('${escapeHtml(item.nome||'')}')">
                    <div class="prod-name">${escapeHtml(item.nome||'N/D')}</div>
                    <div class="prod-meta">SKU: ${escapeHtml(item.sku||'')} • Qtd: ${item.qtd||1}</div>
                </div>`;
            });
            html += '</div>';

            // Coluna Produzindo
            html += `<div class="col-md-4" style="border-right:1px solid var(--border)">
                <div class="prod-col-header" style="color:#10b981">⚙️ Produzindo <span class="count-pill" style="background:#10b981">${inProd.length}</span></div>`;
            if (!inProd.length) html += '<div class="text-center text-muted py-4" style="font-size:0.8rem">Nenhum em produção</div>';
            inProd.forEach(item => {
                html += `<div class="prod-card-item" onclick="openProductionChecklist('${escapeHtml(item.nome||'')}')">
                    <div class="d-flex justify-content-between align-items-start">
                        <div>
                            <div class="prod-name">${escapeHtml(item.nome||'N/D')}</div>
                            <div class="prod-meta">SKU: ${escapeHtml(item.sku||'')}</div>
                        </div>
                        <div class="prod-timer" id="timer-${escapeHtml(item.id||'')}">--:--</div>
                    </div>
                </div>`;
            });
            html += '</div>';

            // Coluna Concluídos
            html += `<div class="col-md-4">
                <div class="prod-col-header" style="color:var(--sw-gray)">✅ Concluídos <span class="count-pill" style="background:var(--sw-gray)">${done.length}</span></div>`;
            if (!done.length) html += '<div class="text-center text-muted py-4" style="font-size:0.8rem">Nenhum concluído</div>';
            done.slice(0,10).forEach(item => {
                html += `<div class="prod-card-item">
                    <div class="prod-name" style="color:var(--text-muted)">${escapeHtml(item.nome||'N/D')}</div>
                    <div class="prod-meta">${escapeHtml(item.tempo_producao||'')}</div>
                </div>`;
            });
            html += '</div>';
            html += '</div>';
            section.innerHTML = html;

            // Iniciar ticker de timers
            if (_boardTick) clearInterval(_boardTick);
            _boardTick = setInterval(tickTimers, 1000);
        }

        function tickTimers() {
            const now = Date.now() / 1000;
            for (const [id, start] of Object.entries(_timerData)) {
                const el = document.getElementById(`timer-${id}`);
                if (!el) continue;
                const elapsed = Math.floor(now - start);
                const h = String(Math.floor(elapsed / 3600)).padStart(2,'0');
                const m = String(Math.floor((elapsed % 3600) / 60)).padStart(2,'0');
                const s = String(elapsed % 60).padStart(2,'0');
                el.textContent = elapsed >= 3600 ? `${h}:${m}:${s}` : `${m}:${s}`;
            }
        }

        async function syncAndRefreshPending() {
            if (!isAuthenticated) { showToast('Aviso', 'Faça login primeiro!', 'warning'); return; }
            try {
                await fetchAPI('/api/production/sync', { method: 'POST' });
                showToast('Sincronizado', 'Painel atualizado com dados do Bling.', 'success');
                await loadProductionBoard();
            } catch(e) { showToast('Erro', e.message, 'danger'); }
        }

        /* ══ COMPONENT TAB ══ */
        async function refreshComponentTab() {
            await Promise.all([loadConsumptionTable(), loadProductionHistory()]);
        }

        async function loadConsumptionTable() {
            const section = document.getElementById('consumption-table-section');
            if (!section) return;
            try {
                const data = await fetchAPI('/api/consumption');
                const badge = document.getElementById('consumption-total-badge');
                const label = document.getElementById('consumption-month-label');
                if (!data || !data.items?.length) {
                    section.innerHTML = '<div class="text-center py-4 text-muted">Nenhum dado de consumo.</div>';
                    if (badge) badge.textContent = '0 insumos';
                    return;
                }
                if (badge) badge.textContent = `${data.items.length} insumos`;
                if (label && data.month) label.textContent = `${data.month} • Reinicia todo mês`;
                let html = '<div class="table-responsive"><table class="table"><thead><tr><th>Insumo</th><th>Qtd. Consumida</th><th>Unidade</th></tr></thead><tbody>';
                data.items.forEach(item => {
                    html += `<tr><td style="font-weight:600">${escapeHtml(item.nome||'')}</td><td><strong style="color:var(--sw-yellow)">${item.qtd||0}</strong></td><td><span class="badge bg-light" style="color:var(--sw-black)">${escapeHtml(item.un||'')}</span></td></tr>`;
                });
                html += '</tbody></table></div>';
                section.innerHTML = html;
            } catch(e) { section.innerHTML = '<div class="text-center py-4 text-muted">Erro ao carregar consumo.</div>'; }
        }

        async function loadProductionHistory() {
            const section = document.getElementById('production-history-section');
            if (!section) return;
            try {
                const data = await fetchAPI('/api/production/history');
                if (!data || !data.items?.length) {
                    section.innerHTML = '<div class="text-center py-4 text-muted">Nenhum histórico este mês.</div>';
                    return;
                }
                let html = '<div class="table-responsive"><table class="table"><thead><tr><th>Produto</th><th>Finalizado em</th><th>Tempo de Produção</th><th>Responsável</th></tr></thead><tbody>';
                data.items.forEach(item => {
                    html += `<tr><td style="font-weight:600">${escapeHtml(item.nome||'')}</td><td style="color:var(--text-muted)">${escapeHtml(item.data_fim||'')}</td><td><span class="prod-timer" style="font-size:0.9rem">${escapeHtml(item.tempo||'')}</span></td><td>${escapeHtml(item.responsavel||'-')}</td></tr>`;
                });
                html += '</tbody></table></div>';
                section.innerHTML = html;
            } catch(e) { section.innerHTML = '<div class="text-center py-4 text-muted">Erro ao carregar histórico.</div>'; }
        }

        /* ══ PRODUCTION CHECKLIST MODAL ══ */
        async function openProductionChecklist(productName) {
            if (!isAuthenticated) { showToast('Aviso', 'Faça login primeiro!', 'warning'); return; }
            const existingModal = document.getElementById('productionModal');
            if (existingModal) { try { bootstrap.Modal.getInstance(existingModal)?.hide(); } catch(e) {} existingModal.remove(); }

            let checklistData = null;
            try {
                checklistData = await fetchAPI(`/api/production/checklist?nome=${encodeURIComponent(productName)}`);
            } catch(e) { showToast('Erro', 'Não foi possível carregar checklist.', 'danger'); return; }

            let timerInterval = null;
            let timerStart = null;
            const itemId = checklistData?.id;

            const modalHtml = `
                <div class="modal fade" id="productionModal" tabindex="-1" data-bs-backdrop="static">
                    <div class="modal-dialog modal-lg modal-dialog-centered">
                        <div class="modal-content">
                            <div class="modal-header">
                                <h5 class="modal-title">🛠️ Produção: ${escapeHtml(productName)}</h5>
                                <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" onclick="clearInterval(timerInterval)"></button>
                            </div>
                            <div class="modal-body p-4">
                                <div class="d-flex justify-content-between align-items-center mb-4">
                                    <div>
                                        <div style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;color:var(--text-muted)">Tempo de Produção</div>
                                        <div class="prod-timer" id="modal-timer" style="font-size:2.5rem">00:00</div>
                                    </div>
                                    <div class="d-flex gap-2">
                                        <button class="btn btn-primary" id="btn-start-prod" onclick="startProductionTimer('${escapeHtml(itemId||'')}')">▶ Iniciar</button>
                                        <button class="btn btn-outline-secondary" id="btn-finish-prod" onclick="finishProduction('${escapeHtml(itemId||'')}', '${escapeHtml(productName)}')">✅ Concluir</button>
                                    </div>
                                </div>
                                <div id="checklist-items">
                                    ${renderChecklistItems(checklistData?.components || [])}
                                </div>
                            </div>
                            <div class="modal-footer">
                                <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal" onclick="clearInterval(timerInterval)">Fechar</button>
                            </div>
                        </div>
                    </div>
                </div>`;

            document.body.insertAdjacentHTML('beforeend', modalHtml);
            const modal = new bootstrap.Modal(document.getElementById('productionModal'));
            modal.show();

            // Verificar se já tem timer ativo
            if (checklistData?.timer_start) {
                timerStart = checklistData.timer_start;
                startModalTimer();
                document.getElementById('btn-start-prod').disabled = true;
                document.getElementById('btn-start-prod').textContent = '⚙️ Em Produção';
            }

            function startModalTimer() {
                const el = document.getElementById('modal-timer');
                timerInterval = setInterval(() => {
                    if (!el) { clearInterval(timerInterval); return; }
                    const elapsed = Math.floor(Date.now()/1000 - timerStart);
                    const h = String(Math.floor(elapsed/3600)).padStart(2,'0');
                    const m = String(Math.floor((elapsed%3600)/60)).padStart(2,'0');
                    const s = String(elapsed%60).padStart(2,'0');
                    el.textContent = elapsed >= 3600 ? `${h}:${m}:${s}` : `${m}:${s}`;
                }, 1000);
            }

            window.startProductionTimer = async function(id) {
                try {
                    const res = await fetchAPI('/api/production/start', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({id, nome: productName}) });
                    timerStart = res.timer_start || Date.now()/1000;
                    startModalTimer();
                    const btn = document.getElementById('btn-start-prod');
                    if (btn) { btn.disabled = true; btn.textContent = '⚙️ Em Produção'; }
                    showToast('Produção Iniciada', `Timer iniciado para ${productName}`, 'success');
                    loadProductionBoard();
                } catch(e) { showToast('Erro', e.message, 'danger'); }
            };

            window.finishProduction = async function(id, nome) {
                try {
                    clearInterval(timerInterval);
                    await fetchAPI('/api/production/finish', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({id, nome}) });
                    const modalEl = document.getElementById('productionModal');
                    if (modalEl) { try { (bootstrap.Modal.getInstance(modalEl)||new bootstrap.Modal(modalEl)).hide(); } catch(e) {} setTimeout(() => modalEl.remove(), 300); }
                    showToast('Concluído!', `${nome} finalizado com sucesso!`, 'success');
                    loadProductionBoard();
                    refreshComponentTab();
                } catch(e) { showToast('Erro', e.message, 'danger'); }
            };
        }

        function renderChecklistItems(components) {
            if (!components?.length) return '<p class="text-muted text-center py-3">Sem componentes cadastrados.</p>';
            return components.map((c, i) => `
                <div class="d-flex align-items-center gap-3 p-3 mb-2" style="background:white;border:1px solid var(--border);border-radius:var(--radius-sm);transition:all 0.2s ease">
                    <input type="checkbox" id="chk-${i}" style="width:18px;height:18px;accent-color:var(--sw-yellow);cursor:pointer">
                    <label for="chk-${i}" style="cursor:pointer;font-size:0.85rem;font-weight:500;flex:1">${escapeHtml(c.nome||'')} <span style="color:var(--text-muted);font-weight:400">× ${c.qtd||1} ${escapeHtml(c.un||'')}</span></label>
                </div>`).join('');
        }

    </script>

    <!-- FOOTER -->
    <footer class="sw-footer">
        <div>
            <p><strong>SW Móveis MDF</strong> — Gestão Inteligente de Pedidos</p>
            <small>© 2025 — Desenvolvido por João Victor Dias Santana</small>
        </div>
        <div style="text-align:right">
            <p><strong>Versão</strong> 4.6</p>
            <small>Sistema Integrado Bling API v3</small>
        </div>
    </footer>
    <div class="sw-pattern-bar"></div>

</body>
</html>"""

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