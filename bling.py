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

    # Suppress cosmetic KeyError from pymongo monitor threads under gevent/Python 3.13
    # These errors are harmless — pymongo threads cleanup fails because gevent already
    # owns the thread registry. Does NOT affect functionality.
    import gevent.hub as _gh
    _orig_handle = _gh.Hub.handle_error
    def _patched_handle_error(self, context, type, value, tb):
        if type is KeyError and 'pymongo' in str(context).lower():
            return  # suppress pymongo thread cleanup KeyError
        _orig_handle(self, context, type, value, tb)
    _gh.Hub.handle_error = _patched_handle_error
    del _gh, _orig_handle, _patched_handle_error

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
def _setup_gunicorn_filters():
    import logging as _log_setup
    for _name in ('gunicorn.arbiter', 'gunicorn.error', 'gunicorn'):
        _log_setup.getLogger(_name).addFilter(
            type('_SuppressNoLoop', (_log_setup.Filter,), {
                'filter': staticmethod(lambda r: 'no running event loop' not in r.getMessage())
            })()
        )
_setup_gunicorn_filters()

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
        # Python 3.13 + gevent + pymongo: usar options que minimizam threads background
        # directConnection=False + heartbeatFrequencyMS alto reduz monitor threads
        _mongo_client = MongoClient(
            _MONGO_URI,
            serverSelectionTimeoutMS=8000,
            connectTimeoutMS=8000,
            socketTimeoutMS=15000,
            connect=False,           # lazy — não conecta no import
            maxPoolSize=3,           # pool mínimo
            minPoolSize=0,
            maxIdleTimeMS=45000,
            heartbeatFrequencyMS=60000,  # reduz frequência do monitor thread (padrão=10s)
            serverMonitoringMode='stream',  # pymongo >=4.3 — desativa polling agressivo
        )
        _mongo_db = _mongo_client.get_database('sw_moveis')
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
    def set(collection: str, data: dict, doc_id: str = 'main', replace: bool = False) -> bool:
        """
        Salva documento no MongoDB.
        replace=True: substitui o documento inteiro (útil para dados nested complexos).
        replace=False (padrão): usa $set — merge de campos (seguro para atualizações parciais).
        """
        if not MONGO_AVAILABLE:
            return False
        try:
            payload = {k: v for k, v in data.items() if k != '_id'}
            if replace:
                _mongo_db[collection].replace_one(
                    {'_id': doc_id},
                    {'_id': doc_id, **payload},
                    upsert=True
                )
            else:
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
        """Alias de set() — mantido para compatibilidade com chamadas existentes."""
        return MongoStore.set(collection, data, doc_id)

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
# No Render o diretório de trabalho é read-only — usa /tmp para arquivos temporários
_default_data_dir = '/tmp' if os.path.isdir('/tmp') and os.access('/tmp', os.W_OK) else '.'
DATA_DIR = Path(os.environ.get('DATA_DIR', _default_data_dir))

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
        from collections import deque
        self.logs = deque(maxlen=max_logs)  # O(1) rotation vs O(n) list.pop(0)
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
            self.logs.append(log_entry)  # deque(maxlen) descarta o mais antigo automaticamente
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
        logs_list = list(self.logs)
        if limit:
            return logs_list[-limit:]
        return logs_list

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
    """Inicia timer para limpar callbacks órfãos e fazer reset mensal — idempotente."""
    global _cleanup_timer_started
    if _cleanup_timer_started:
        return
    _cleanup_timer_started = True

    def _monthly_reset():
        """Limpa dados do mês anterior no MongoDB e reinicia contadores."""
        try:
            now = datetime.now()
            mes_atual = now.strftime('%Y-%m')

            # 1. Remove documentos de production_history de meses anteriores
            if MONGO_AVAILABLE:
                try:
                    result = _mongo_db['production_history'].delete_many(
                        {'_id': {'$ne': mes_atual}}
                    )
                    if result.deleted_count:
                        logger.info(f"🗓️ Reset mensal: {result.deleted_count} mês(es) antigo(s) removido(s) do histórico de produção.")
                except Exception as e:
                    logger.error(f"Reset mensal production_history: {e}")

            # 2. Remove meses antigos do component_consumption
            if MONGO_AVAILABLE:
                try:
                    doc = MongoStore.get('component_consumption', 'main')
                    data = doc.get('data', {})
                    meses_antigos = [k for k in data if k != mes_atual]
                    if meses_antigos:
                        for k in meses_antigos:
                            del data[k]
                        MongoStore.set('component_consumption', {'data': data}, 'main', replace=True)
                        logger.info(f"🗓️ Reset mensal: {len(meses_antigos)} mês(es) antigo(s) removido(s) do consumo de componentes.")
                        # Atualiza o objeto em memória também
                        component_consumption.data = data
                except Exception as e:
                    logger.error(f"Reset mensal component_consumption: {e}")

            logger.info(f"✅ Reset mensal concluído para {mes_atual}.")
        except Exception as e:
            logger.error(f"Erro no reset mensal: {e}")

    def cleanup_loop():
        last_reset_month = datetime.now().month
        while True:
            time.sleep(300)  # verifica a cada 5 minutos
            cleanup_kpi_callbacks()

            # Verifica se virou o mês
            now = datetime.now()
            if now.month != last_reset_month:
                logger.info(f"🗓️ Novo mês detectado ({now.strftime('%Y-%m')}) — iniciando reset mensal...")
                _monthly_reset()
                last_reset_month = now.month

    Thread(target=cleanup_loop, daemon=True, name="cleanup_timer").start()

# ============================================================================ 
# 2. CONFIGURAÇÕES
# ============================================================================

class Config:
    """Configurações globais da aplicação."""
    
    # Bling OAuth
    CLIENT_ID: str = os.environ.get('BLING_CLIENT_ID', '')
    CLIENT_SECRET: str = os.environ.get('BLING_CLIENT_SECRET', '')
    WEBHOOK_SECRET: str = os.environ.get('BLING_WEBHOOK_SECRET', '')
    REDIRECT_URI: str = os.environ.get('BLING_REDIRECT_URI', '')
    
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
    """Salva tokens em MongoDB E arquivo local (dupla redundância — nunca perde token)."""
    if MONGO_AVAILABLE:
        try:
            MongoStore.set('auth_tokens', data, 'tokens')
            logger.info("Tokens salvos no MongoDB.")
        except Exception as e:
            logger.error(f"Erro ao salvar tokens no MongoDB: {e}")
    # Sempre salva no arquivo também — fallback garantido se MongoDB falhar no próximo boot
    if isinstance(path, str): path = Path(path)
    try:
        atomic_write_json(data, path)
        logger.debug("Tokens salvos em arquivo local (backup).")
    except Exception as e:
        logger.error(f"Erro ao salvar tokens em arquivo: {e}")

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
    """Salva estatísticas em MongoDB E arquivo local (dupla redundância)."""
    data_to_save = data.copy()
    if 'last_recalculated' in data_to_save and isinstance(data_to_save['last_recalculated'], datetime):
        data_to_save['last_recalculated'] = data_to_save['last_recalculated'].isoformat()
    if MONGO_AVAILABLE:
        try:
            MongoStore.set('sales_stats', data_to_save, 'stats')
            logger.info("Estatísticas salvas no MongoDB.")
        except Exception as e:
            logger.error(f"Erro ao salvar stats no MongoDB: {e}")
    # Sempre salva no arquivo também
    try:
        atomic_write_json(data_to_save, path)
        logger.debug("Estatísticas salvas em arquivo local (backup).")
    except Exception as e:
        logger.error(f"Erro ao salvar stats em arquivo: {e}")

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
    """Salva cache de produtos e kits em MongoDB E arquivo local (dupla redundância)."""
    total_produtos = len(products or []) + len(kits or [])
    logger.debug(f"save_products_cache chamado. products={len(products or [])} kits={len(kits or [])} total={total_produtos}")

    if total_produtos == 0:
        logger.warning("⛔ Cache vazio ignorado. API não retornou produtos ou parsing falhou.")
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
        except Exception as e:
            logger.error(f"Erro ao salvar cache no MongoDB: {e}")
    # Sempre salva no arquivo também
    try:
        atomic_write_json(payload, cache_file)
        logger.debug(f"Cache salvo em arquivo local (backup). Total: {total_produtos}")
    except Exception as e:
        logger.exception("Erro ao salvar cache de produtos em arquivo.")

def safe_iter(data):
    """Garante que o dado é iterável (lista ou tupla), senão retorna lista vazia."""
    if isinstance(data, (list, tuple)):
        return data
    return []

def _parse_order_date(date_str) -> Optional[datetime]:
    """
    Centraliza o parse de datas de pedidos do Bling.
    Suporta: 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM', 'YYYY-MM-DDTHH:MM', 'DD/MM/YYYY'.
    Retorna None se não conseguir parsear.
    """
    if not date_str:
        return None
    try:
        date_clean = str(date_str).split(' ')[0].split('T')[0].strip()
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%Y/%m/%d'):
            try:
                return datetime.strptime(date_clean, fmt)
            except ValueError:
                continue
    except Exception:
        pass
    return None

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

            # Tratamento de Token Expirado (401/403) — antes do raise_for_status
            if response.status_code in (401, 403):
                self.logger.warning(f"⚠️ {response.status_code} em {endpoint} — tentando refresh...")
                if self.auth.refresh_token():
                    new_token = self.auth.get_access_token()
                    if new_token:
                        kwargs['headers']['Authorization'] = f'Bearer {new_token}'
                        response = self.session.request(method, url, timeout=45, **kwargs)
                        self.logger.info(f"✅ Retry após refresh: {response.status_code}")
                    else:
                        self.logger.error("Refresh retornou token vazio.")
                        return None
                else:
                    self.logger.error(f"❌ Refresh falhou — acesse /auth para re-autenticar.")
                    return None

            if response.status_code == 429:
                self.logger.warning(f"Rate limit (429) em {endpoint}. urllib3 já retentará automaticamente.")
                # Não levanta exceção aqui — o Retry adapter já tratou via status_forcelist

            response.raise_for_status()
            
            try:
                return response.json()
            except json.JSONDecodeError:
                return {}

        except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
            self.logger.error(f"Erro de Conexão (Reset/Queda) em {endpoint}: {str(e)}")
            # Recria sessão com todos os adapters e headers configurados corretamente
            self.session.close()
            self.session = requests.Session()
            retry_strategy = Retry(
                total=3,
                backoff_factor=1,
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
                'User-Agent': 'SWMoveis/4.6 (Integracao Bling)'
            })
            return None
            
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
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

        # Usa compare_digest para evitar timing attacks (bug #10)
        is_valid = hmac.compare_digest(saved_state, state)
        if is_valid:
            # Limpa o state imediatamente após uso para impedir reutilização (CSRF)
            self._clean_oauth_state()
            self.logger.info(f"State OAuth validado com sucesso e limpo.")

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

        if not self.config.CLIENT_ID or not self.config.CLIENT_SECRET:
            raise ValueError("CRÍTICO: BLING_CLIENT_ID e BLING_CLIENT_SECRET devem estar configurados nas variáveis de ambiente!")
        if not self.config.REDIRECT_URI:
            raise ValueError("CRÍTICO: BLING_REDIRECT_URI não configurada nas variáveis de ambiente!")

        self.logger = logging.getLogger('bling_automacao')
        self._tokens = self._load_tokens()
        self._access_token = self._tokens.get('access_token')
        self._refresh_token = self._tokens.get('refresh_token')
        self._expires_at = self._tokens.get('expires_at', 0)
        self._initial_load_failed = False  # será True se a carga inicial falhar
        
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
            'scope': (
                'produtos:read produtos:write '
                'pedidos:read pedidos:write '
                'estoques:read estoques:write '
                'contatos:read contatos:write '
                'notafiscal:read notafiscal:write'
            ),
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
            self._initial_load_failed = True  # marca falha para suprimir logs repetitivos
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

                # Fallback: carrega do arquivo local se MongoDB não trouxe nada
                if not self._sales_history:
                    sales_history_file = self.config.SALES_STATS_FILE.parent / 'sales_history.json'
                    if sales_history_file.exists():
                        try:
                            with open(sales_history_file, 'r', encoding='utf-8') as f:
                                loaded = json.load(f).get('orders', [])
                            if loaded:
                                self._sales_history = loaded
                                logger.info(f"✅ sales_history: {len(loaded)} pedidos carregados do arquivo local")
                        except Exception as e:
                            logger.warning(f"Falha ao carregar sales_history do arquivo: {e}")

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
            sh = self.stats_history or {}
            return {
                "daily":         self.daily_count,
                "weekly":        self.weekly_count,
                "monthly":       self.monthly_count,
                "historic":      self.historic_count,
                # V5.0 fields for KPI display
                "daily_count":   sh.get("daily_count",   self.daily_count),
                "weekly_count":  sh.get("weekly_count",  self.weekly_count),
                "monthly_count": sh.get("monthly_count", self.monthly_count),
                "growth":        sh.get("growth",  0),
                "avg_daily":     sh.get("avg_daily", 0),
                "last_7":        sh.get("last_7",  0),
                "ritmo_7d":      sh.get("ritmo_7d", 0),
                "history_data":  self.history_data,
                "stats_history": self.stats_history,
                "last_recalculated": self.last_recalculated.isoformat()
            }

    def _save_sales_history(self):
        """Salva histórico de pedidos em MongoDB E arquivo local (dupla redundância)."""
        try:
            compact = []
            for o in self._sales_history:
                data_val = o.get('data') or o.get('dataEmissao') or o.get('dataSaida') or ''
                itens = o.get('itens', [])
                compact.append({
                    'id': o.get('id'),
                    'data': data_val,
                    'dataEmissao': data_val,
                    'numero': o.get('numero'),
                    'contato': o.get('contato'),
                    'itens': itens,
                })
            compact = [o for o in compact if o.get('id')]

            if MONGO_AVAILABLE:
                try:
                    MongoStore.set('sales_history', {'orders': compact}, 'history')
                    logger.info(f"✅ sales_history salvo no MongoDB: {len(compact)} pedidos")
                except Exception as e:
                    logger.error(f"Erro ao salvar sales_history no MongoDB: {e}")

            # Sempre salva no arquivo também
            sales_history_file = self.config.SALES_STATS_FILE.parent / 'sales_history.json'
            try:
                atomic_write_json({'orders': compact}, sales_history_file)
                logger.debug(f"sales_history salvo em arquivo local (backup): {len(compact)} pedidos")
            except Exception as e:
                logger.error(f"Erro ao salvar sales_history em arquivo: {e}")

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
        inicio_semana = hoje - timedelta(days=6)  # rolling 7 days
        inicio_mes = hoje.replace(day=1)
        
        inicio_grafico = hoje - timedelta(days=29) # Últimos 30 dias
        
        daily_orders = []
        weekly_orders = []
        monthly_orders = []
        
        # Dicionário para gráfico (agora usa janela móvel)
        daily_counts_chart = defaultdict(int) 
        monthly_report = defaultdict(int)

        ignorados = 0
        formatos_falhos = []
        for o in all_orders:
            try:
                date_str = o.get('data') or o.get('dataEmissao')
                if not date_str:
                    ignorados += 1
                    continue

                # Suporta: 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM:SS', 'DD/MM/YYYY'
                date_part = str(date_str).split('T')[0].split(' ')[0].strip()
                dt = None
                for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%Y/%m/%d'):
                    try:
                        dt = datetime.strptime(date_part, fmt)
                        break
                    except ValueError:
                        continue

                if dt is None:
                    ignorados += 1
                    if len(formatos_falhos) < 3:
                        formatos_falhos.append(date_str)
                    continue

                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=tz_br)

                dt_pedido = dt.date()

                if dt.year == now.year:
                    monthly_report[dt.month] += 1

                # KPIs
                if dt_pedido == hoje: daily_orders.append(o)
                if dt_pedido >= inicio_semana: weekly_orders.append(o)
                if dt_pedido >= inicio_mes: monthly_orders.append(o)

                if dt_pedido >= inicio_grafico:
                    daily_counts_chart[dt_pedido] += 1

            except Exception as e:
                ignorados += 1
                self.logger.debug(f"Erro ao processar pedido {o.get('id','?')}: {e}")
                continue

        if ignorados > 0:
            self.logger.warning(f"⚠️ {ignorados}/{len(all_orders)} pedidos ignorados por data inválida. Amostras: {formatos_falhos}")

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

        pedidos_processados = len(daily_orders) + len(weekly_orders) + len(monthly_orders) + len(daily_counts_chart)

        # Só atualiza KPIs se pelo menos 1 pedido foi processado com sucesso
        # Isso evita que uma falha de parse sobrescreva KPIs válidos com zeros
        if pedidos_processados == 0 and len(all_orders) > 0:
            self.logger.warning(f"⚠️ Nenhum pedido processado de {len(all_orders)} recebidos — mantendo KPIs anteriores.")
            return

        with self.lock:
            self.daily_count = len(daily_orders)
            self.weekly_count = len(weekly_orders)
            self.monthly_count = len(monthly_orders)
            self.historic_count = len(all_orders)

            self.history_data['yearly_monthly_report'] = dict(monthly_report)

            # Crescimento V5.0: últimos 7d vs média mensal ÷ 20 dias úteis
            last_7_val = sum(counts[-7:])
            monthly_total_val = len(monthly_orders)
            ritmo_7d_val = (monthly_total_val / 20) * 7 if monthly_total_val > 0 else 0
            growth_v5 = round(((last_7_val - ritmo_7d_val) / ritmo_7d_val * 100), 1) if ritmo_7d_val else 0
            dias_com = sum(1 for c in counts if c > 0)
            avg_d = round(sum(counts) / max(dias_com, 1), 1)
            self.stats_history = {
                'dates':         [d.isoformat() for d in dates],
                'daily':         counts,
                'moving_avg':    [round(v, 2) for v in moving_avg],
                'growth':        growth_v5,
                'avg_daily':     avg_d,
                'last_7':        last_7_val,
                'ritmo_7d':      round(ritmo_7d_val, 1),
                'monthly_total': monthly_total_val,
                'weekly_count':  len(weekly_orders),
                'daily_count':   len(daily_orders),
                'monthly_count': len(monthly_orders),
            }
            self.last_recalculated = now
            self._orders_cache = {o.get('id'): o for o in all_orders[-100:]}

        save_stats(self._get_state_for_save(), self.config.SALES_STATS_FILE)
        self._save_sales_history()
        self.logger.info(f"✅ Estatísticas atualizadas: D:{self.daily_count} W:{self.weekly_count} M:{self.monthly_count} | Histórico: {len(all_orders)} pedidos")

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
                if data:  # doc existe no MongoDB (mesmo sem timers ativos)
                    logger.info(f"✅ Timers MongoDB: {len(timers)} ativo(s)")
                    return timers
                logger.info("MongoDB sem doc de timers — verificando arquivo...")
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
        """Salva timers no MongoDB (primário). Arquivo só como fallback sem Mongo."""
        if MONGO_AVAILABLE:
            try:
                MongoStore.set('production_timers', {'timers': self.timers}, 'timers', replace=True)
                return  # MongoDB OK — não precisa de arquivo
            except Exception as e:
                logger.error(f"Erro ao salvar timers no MongoDB: {e}")
        # Fallback: arquivo local (só quando MongoDB indisponível)
        if not MONGO_AVAILABLE:
            temp_file = self.FILE_PATH.with_suffix('.tmp')
            try:
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(self.timers, f, indent=4, ensure_ascii=False)
                import shutil
                shutil.move(str(temp_file), str(self.FILE_PATH))
            except Exception as e:
                logger.warning(f"Fallback timer arquivo: {e}")


    def _auto_pause_on_restart(self):
        """
        Ao reiniciar: soma o tempo que estava rodando desde o último checkpoint
        e retoma automaticamente (start_ts = agora).
        Assim o timer continua contando sem interrupção visível para o usuário.
        """
        changed = False
        now = time.time()
        MAX_TIMER_SECONDS = 30 * 24 * 3600   # 30 dias — cap máximo razoável
        CLEANUP_AFTER     = 60 * 24 * 3600   # 60 dias — remove timers abandonados

        stale_keys = []
        for k, v in list(self.timers.items()):
            acc = v.get('accumulated', 0)

            # Remove timers muito antigos (>60 dias abandonados)
            if acc > CLEANUP_AFTER:
                logger.info(f"🗑️ Timer '{k[:40]}' removido (>60d sem conclusao).")
                stale_keys.append(k)
                changed = True
                continue

            if v.get('state') == 'running':
                start_ts = v.get('start_ts', 0)
                if start_ts and start_ts > 0:
                    v['accumulated'] = acc + (now - start_ts)

                # Cap: pausa timers que excedem 30 dias
                if v['accumulated'] > MAX_TIMER_SECONDS:
                    logger.warning(f"⏰ Timer '{k[:50]}' excedeu 30d — pausado automaticamente.")
                    v['state']    = 'paused'
                    v['start_ts'] = 0
                    changed = True
                    continue

                v['start_ts'] = now
                v['state']    = 'running'
                changed = True
                logger.info(f"▶️ Restart: timer '{k}' retomado automaticamente ({int(v['accumulated'])}s acumulados).")

        for k in stale_keys:
            del self.timers[k]

        if changed:
            self._save()

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
            timer_existed = True
            status = self.pause(produto_nome)
            total_seconds = status['elapsed']
        else:
            # Timer realmente não existe
            timer_existed = False
            total_seconds = 0
            logger.info(f"⚠️ Timer não encontrado para '{produto_nome}' — registrando com tempo 0")

        # Auto-registra componentes NÃO marcados no checklist (apenas se timer existiu)
        # Se timer não existiu, não auto-registra para evitar duplicação de componentes
        if timer_existed and 'CADEIRA' in nome_real.upper():
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
                "checklist_total": len(RECIPE_CADEIRA) if 'CADEIRA' in nome.upper() else 0,
                "has_recipe": 'CADEIRA' in nome.upper(),
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

        saved_mongo = False
        if MONGO_AVAILABLE:
            for _att in range(3):
                try:
                    _mongo_db['production_history'].update_one(
                        {'_id': mes_chave},
                        {'$push': {'registros': reg_clean}},
                        upsert=True
                    )
                    saved_mongo = True
                    logger.info(f"✅ Histórico MongoDB: {reg_clean.get('produto','?')} ({int(reg_clean.get('tempo_segundos',0))}s)")
                    break
                except Exception as e:
                    logger.error(f"Histórico MongoDB tentativa {_att+1}/3: {e}")
                    if _att < 2: time.sleep(1)

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
            if not saved_mongo:
                logger.info(f"✅ Histórico salvo em arquivo local (MongoDB indisponível).")
        except Exception as e:
            logger.error(f"Erro ao salvar histórico em arquivo: {e}")
            if not saved_mongo:
                logger.error(f"❌ CRÍTICO: Histórico de '{reg_clean.get('produto','?')}' NÃO foi salvo em nenhum storage!")

    def get_monthly_history_details(self):
        """Retorna histórico do mês — merge MongoDB + arquivo (máxima redundância)."""
        mes_chave = datetime.now().strftime('%Y-%m')
        mongo_regs = []
        file_regs  = []
        if MONGO_AVAILABLE:
            try:
                doc = _mongo_db['production_history'].find_one({'_id': mes_chave})
                mongo_regs = (doc or {}).get('registros', [])
            except Exception as e:
                logger.warning(f"Falha ao carregar histórico do MongoDB: {e}")
        if self.HISTORY_PATH.exists():
            try:
                with open(self.HISTORY_PATH, 'r', encoding='utf-8') as f:
                    file_regs = json.load(f).get(mes_chave, [])
            except Exception as e:
                logger.error(f"Erro ao carregar histórico do arquivo: {e}")
        if not mongo_regs:
            return file_regs
        if not file_regs:
            return mongo_regs
        seen   = {r.get('timestamp', '') for r in mongo_regs if r.get('timestamp')}
        extras = [r for r in file_regs if r.get('timestamp', '') not in seen]
        merged = sorted(mongo_regs + extras, key=lambda r: r.get('timestamp', 0))
        return merged

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
        """Carrega consumo — MongoDB + arquivo com merge para máxima redundância."""
        mongo_data = {}
        file_data = {}
        if MONGO_AVAILABLE:
            try:
                doc = MongoStore.get('component_consumption', 'main')
                mongo_data = doc.get('data', {})
                if mongo_data:
                    logger.info(f"✅ Consumo carregado do MongoDB: {list(mongo_data.keys())}")
            except Exception as e:
                logger.warning(f"Falha ao carregar consumo do MongoDB: {e}")
        if self.FILE_PATH.exists():
            try:
                with open(self.FILE_PATH, 'r', encoding='utf-8') as f:
                    file_data = json.load(f)
                    if file_data and not mongo_data:
                        logger.info(f"✅ Consumo carregado do arquivo local: {list(file_data.keys())}")
            except Exception as e:
                logger.error(f"Erro ao carregar consumo do arquivo: {e}")
        # Merge: MongoDB como base, arquivo preenche meses ausentes
        if not mongo_data and not file_data:
            return {}
        if not mongo_data:
            return file_data
        if not file_data:
            return mongo_data
        # Merge por mês: MongoDB tem precedência, arquivo preenche meses faltantes
        merged = dict(file_data)
        merged.update(mongo_data)  # MongoDB sobrescreve arquivo para meses em comum
        return merged

    def _save(self):
        """Salva consumo — MongoDB E arquivo local (dupla redundância)."""
        # Protege contra apagar dados reais: só bloqueia se data é None ou não é dict
        if self.data is None:
            logger.warning("⛔ _save de consumo ignorado: self.data é None")
            return
        mes_count = len(self.data)
        comp_count = sum(len(v.get('components', {})) for v in self.data.values())
        if MONGO_AVAILABLE:
            try:
                MongoStore.set('component_consumption', {'data': self.data}, 'main', replace=True)
                logger.debug(f"✅ Consumo salvo no MongoDB: {mes_count} mês(es), {comp_count} componente(s)")
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
        """Garante estrutura para o mês atual e persiste imediatamente."""
        key = self._current_month_key()
        if key not in self.data:
            self.data[key] = {
                'components': {},
                'checklist_logs': []
            }
            # Persiste sempre que cria o mês — garante que o doc existe no MongoDB
            # antes do primeiro register_component, eliminando race condition
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
    "COURVIM AZUL","COURVIM VERDE","COURVIM ROSA","COURVIM VINHO",
    "COURVIM MARROM","COURVIM BEGE","COURVIM NUDE","COURVIM",
    "VELUDO PRETO","VELUDO CINZA","VELUDO AZUL","VELUDO VERDE",
    "VELUDO ROSA","VELUDO BEGE","VELUDO VINHO","VELUDO AMARELO",
    "VELUDO MARROM","VELUDO NUDE","VELUDO CREME","VELUDO",
    "LINHO BEGE","LINHO CINZA","LINHO PRETO","LINHO BRANCO","LINHO NATURAL","LINHO",
    "TECIDO PRETO","TECIDO CINZA","TECIDO BEGE","TECIDO BRANCO",
    "TECIDO MARROM","TECIDO AZUL","TECIDO VERDE","TECIDO ROSA","TECIDO",
    "COURO PRETO","COURO BRANCO","COURO CARAMELO","COURO MARROM","COURO",
    "MARSALA","BORDO","BORDÔ","CARAMELO","NUDE","CREME","NATURAL",
    "PRETO","BRANCO","CINZA ESCURO","CINZA CLARO","CINZA",
    "BEGE ESCURO","BEGE CLARO","BEGE","MARROM",
    "AZUL MARINHO","AZUL ROYAL","AZUL","VERDE MUSGO","VERDE ESCURO","VERDE",
    "ROSA CHOQUE","ROSA CLARO","ROSA","AMARELO","LARANJA","VINHO","ROXO",
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

    # 2. Separador " - ", " / ", " | " ou "-" simples
    if not base or not cor:
        sep = None
        for _s in [" - ", " / ", " | ", "- ", " -"]:
            if _s in nome:
                sep = _s
                break
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
    FSM de produção com esteira por tipo de produto.
    CADEIRAS: waiting→marcenaria→tapecaria→done (3 leituras de barcode)
    MDF/OUTROS: waiting→in_production→done (2 leituras de barcode)
    """
    FILE_PATH = DATA_DIR / 'pending_orders.json'

    # Palavras-chave que identificam produtos de ESTEIRA (cadeiras/poltronas — 3 leituras).
    # REGRA: deve conter UMA dessas palavras E ser reconhecidamente uma cadeira/poltrona.
    # Palavras genéricas como EVIDENCE/BERLIN sozinhas classificam LAVATÓRIO EVIDENCE errado.
    ESTEIRA_KW = frozenset([
        'CADEIRA','POLTRONA',
        'HIDRÁULICA','HIDRAULICA',
        'RECLINÁVEL','RECLINAVEL',
    ])
    # Palavras que EXCLUEM da esteira mesmo se ESTEIRA_KW der match
    ESTEIRA_EXCL = frozenset([
        'LAVATÓRIO','LAVATORIO','ARMÁRIO','ARMARIO',
        'BANCADA','BALCÃO','BALCAO','CARRINHO','ESPELHO',
        'PAINEL','NICHO','PRATELEIRA','GABINETE',
    ])
    ESTEIRA_TRANSITIONS = {'waiting': 'marcenaria', 'marcenaria': 'tapecaria', 'tapecaria': 'done'}
    SIMPLES_TRANSITIONS = {'waiting': 'in_production', 'in_production': 'done'}
    ACTIVE_STATES       = {'waiting', 'in_production', 'marcenaria', 'tapecaria'}

    @classmethod
    def _is_esteira(cls, nome: str) -> bool:
        """Retorna True APENAS se for cadeira/poltrona real (não lavatório/móvel MDF)."""
        n = nome.upper()
        # Se contém palavra de exclusão, nunca é esteira
        if any(excl in n for excl in cls.ESTEIRA_EXCL):
            return False
        return any(k in n for k in cls.ESTEIRA_KW)

    @classmethod
    def _next_state(cls, current: str, nome: str) -> Optional[str]:
        if cls._is_esteira(nome): return cls.ESTEIRA_TRANSITIONS.get(current)
        return cls.SIMPLES_TRANSITIONS.get(current)

    def __init__(self):
        self.data = self._load()
        self._op_cache     = {}
        self._op_cache_ts  = 0.0
        self._op_cache_lock = __import__('threading').Lock()
        self._restore_in_production_to_waiting()

    def _restore_in_production_to_waiting(self):
        """Ao reiniciar: itens in_production voltam para waiting.
        O timer foi pausado pelo _auto_pause_on_restart. O usuário
        clica em Produzir novamente para retomar.
        """
        changed = False
        for key, item in self.data.items():
            if item.get('status') == 'in_production':
                item['status'] = 'waiting'
                item.pop('started_at', None)
                changed = True
                logger.info(f"♻️ Restart: '{item.get('nome','?')}'  voltou para fila de espera.")
        if changed:
            self._save()

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
        """Salva pending_orders — upsert novos + delete removidos no MongoDB + arquivo."""
        if MONGO_AVAILABLE:
            try:
                existing = {str(d['_id']) for d in _mongo_db['pending_orders'].find({}, {'_id': 1})}
                current  = set(self.data.keys())
                for k in existing - current:
                    _mongo_db['pending_orders'].delete_one({'_id': k})
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

    def advance_production(self, item_key: str, tempo_segundos: int = None) -> Optional[Dict]:
        """
        FSM: avança exatamente UMA etapa.
        Cadeiras: waiting→marcenaria→tapecaria→done
        MDF:      waiting→in_production→done
        """
        item = self.data.get(item_key)
        if not item:
            logger.warning(f"advance_production: '{item_key}' não encontrado.")
            return None
        current = item.get('status', 'waiting')
        nome    = item.get('nome') or item.get('nome_original', '')
        target  = self._next_state(current, nome)
        if not target:
            logger.warning(f"FSM: sem transição para status='{current}' nome='{nome[:30]}'")
            return None
        now = datetime.now()
        item['status']       = target
        item[f'ts_{target}'] = now.isoformat()
        if current == 'waiting':
            item['started_at'] = now.isoformat()
            item.pop('finished_at', None)
            item.pop('mes_conclusao', None)
        item['setor'] = target if target not in ('done',) else item.get('setor', 'tapecaria')
        if target == 'done':
            item['finished_at']   = now.isoformat()
            item['mes_conclusao'] = now.strftime('%Y-%m')
            if tempo_segundos:
                item['tempo_producao'] = int(tempo_segundos)
        self._save_one(item_key)
        logger.info(f"FSM ✅ '{nome[:35]}' {current}→{target}")
        return item

    def start_production(self, item_key: str, setor: str = 'tapecaria') -> Optional[Dict]:
        return self.advance_production(item_key)

    def finish_production(self, item_key: str, tempo_segundos: int = None) -> Optional[Dict]:
        return self.advance_production(item_key, tempo_segundos=tempo_segundos)

    def dismiss(self, item_key: str):
        """Remove item da fila — sincroniza MongoDB E arquivo."""
        if item_key in self.data:
            del self.data[item_key]
        if MONGO_AVAILABLE:
            try:
                _mongo_db['pending_orders'].delete_one({'_id': item_key})
            except Exception as e:
                logger.error(f"dismiss: erro MongoDB {item_key}: {e}")
        temp = self.FILE_PATH.with_suffix('.tmp')
        try:
            with open(temp, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
            shutil.move(str(temp), str(self.FILE_PATH))
        except Exception as e:
            logger.error(f"dismiss: erro arquivo: {e}")

    def get_waiting(self):
        """Retorna todos os itens aguardando produção."""
        return [v for v in self.data.values() if v.get('status') == 'waiting']

    def get_in_production(self):
        """Retorna todos os itens em produção."""
        return [v for v in self.data.values() if v.get('status') in ('in_production', 'marcenaria', 'tapecaria')]

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
                        _mongo_db['pending_orders'].delete_one({'_id': key})
                    except Exception:
                        pass
            self._save()  # sempre sincroniza arquivo
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

        # Pré-computa conjuntos para lookup O(1) — evita O(n²) no loop interno
        existing_keys = set(self.data.keys())
        existing_order_sku_idx = {
            (v.get('order_id'), v.get('sku'), v.get('qtd_unit_idx'))
            for v in self.data.values()
        }

        for pedido in orders:
            order_id = str(pedido.get('id', ''))
            if not order_id:
                continue

            # ── Filtro: apenas pedidos do mês atual ─────────────────────────
            data_str = pedido.get('data') or pedido.get('dataEmissao') or ''
            if data_str:
                dt = _parse_order_date(data_str)
                if dt and (dt.month != mes_atual or dt.year != ano_atual):
                    continue

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
                    sku_safe = (sku_raw or nome_raw[:20]).replace(' ', '_').replace('/', '_')
                    sub_key = f"{order_id}_{sku_safe}_{unit}"
                    # Lookup O(1) usando sets pré-computados
                    already = (sub_key in existing_keys or
                                (str(order_id), sku_raw, unit) in existing_order_sku_idx)
                    if not already:
                        self.data[sub_key] = {
                            **item_data,
                            'qtd': 1,
                            'order_id': order_id,
                            'item_key': sub_key,
                            'qtd_unit_idx': unit,
                            'status': 'waiting',
                            'added_at': datetime.now().isoformat()
                        }
                        existing_keys.add(sub_key)
                        existing_order_sku_idx.add((str(order_id), sku_raw, unit))
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
            # API Bling V3: datas só como 'YYYY-MM-DD', sem hora
            # 'situacao' é parâmetro da V2 — na V3 é ignorado ou causa 400
            # Buscamos TODOS os pedidos do mês e filtramos em memória
            params = {
                'dataEmissaoInicial': start_date.strftime('%Y-%m-%d'),
                'dataEmissaoFinal': now.strftime('%Y-%m-%d'),
                'limite': 100,
            }
            
            all_orders = []
            page = 1
            
            while True:
                params['pagina'] = page
                self.logger.info(f"🔍 Buscando pedidos página {page} | params: {params}")
                try:
                    response = self.api.get('pedidos/vendas', params=params)
                except Exception as e:
                    self.logger.error(f"Erro na API ao buscar pedidos: {e}")
                    break

                if response is None:
                    self.logger.warning(f"⚠️ API retornou None na página {page} — token expirado ou erro HTTP. KPIs não serão zerados.")
                    break
    
                data = []
                if isinstance(response, dict):
                    if 'data' in response:
                        data = response['data']
                        # Bling V3: response.data pode vir com paginação
                        # Logar chaves da resposta para diagnóstico
                        if page == 1:
                            self.logger.info(f"📄 Resposta API V3 — chaves: {list(response.keys())} | itens página 1: {len(data)}")
                    elif 'retorno' in response and 'pedidos' in response['retorno']:
                        data = response['retorno']['pedidos']
                        if data and isinstance(data[0], dict) and 'pedido' in data[0]:
                            data = [d['pedido'] for d in data]
                        self.logger.info(f"📄 Resposta formato V2 legacy | itens: {len(data)}")
                    else:
                        self.logger.warning(f"⚠️ Estrutura de resposta inesperada. Chaves: {list(response.keys())} | Raw[:200]: {str(response)[:200]}")
                elif isinstance(response, list):
                    data = response
                    self.logger.info(f"📄 Resposta como lista direta | itens: {len(data)}")
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

                if valid_orders:
                    sample_dates = [o.get('data', '?') for o in valid_orders[:3]]
                    self.logger.info(f"✅ {len(valid_orders)} pedidos válidos. Amostras de datas: {sample_dates}")
                else:
                    self.logger.warning(f"⚠️ 0 pedidos válidos de {len(all_orders)} recebidos. Nenhum tinha 'id' + 'data'.")
                    sample_raw = [{k: v for k, v in o.items() if k in ('id', 'data', 'dataEmissao', 'dataSaida', 'numero')} for o in all_orders[:3]]
                    self.logger.warning(f"Amostras raw: {sample_raw}")
                # 1. Mescla pedidos novos com histórico (O(1) por dict, não O(n²))
                history_map = {o['id']: o for o in self.sales._sales_history if o.get('id')}
                for o in valid_orders:
                    if o.get('id'):
                        history_map[o['id']] = o  # insere ou atualiza
                # Reconstrói lista ordenada por data (mais recente por último)
                merged = sorted(history_map.values(),
                                key=lambda x: x.get('data', ''), reverse=False)
                # Limita a 2000 mais recentes
                self.sales._sales_history = merged[-2000:]
                self.logger.info(f"📦 Histórico de pedidos: {len(valid_orders)} novos/atualizados, {len(self.sales._sales_history)} total em memória.")
                
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
                
                # Salva stats + history no MongoDB ANTES de broadcastar
                try:
                    save_stats(self.sales._get_state_for_save(), self.config.SALES_STATS_FILE)
                    self.sales._save_sales_history()
                except Exception as _se:
                    self.logger.warning(f"Erro ao persistir stats após recálculo: {_se}")
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
                dt = _parse_order_date(data_str)
                if dt:
                    if dt.month == agora_fetch.month and dt.year == agora_fetch.year:
                        orders_mes.append(o)
                else:
                    orders_mes.append(o)  # data não parseável: inclui por segurança
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
                    todos_pedidos = list(self.sales._sales_history or [])

            for pedido in todos_pedidos:
                data_str = pedido.get('data')
                if not data_str: continue

                try:
                    dt_pedido = _parse_order_date(data_str)
                    if dt_pedido is None:
                        continue
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

            # Debug: log exact credentials being used
            cid = self.orchestrator.auth.config.CLIENT_ID
            ruri = self.orchestrator.auth.config.REDIRECT_URI
            logger.info(f"🔐 /auth iniciado | CLIENT_ID: {cid[:8]}...{cid[-4:] if len(cid)>12 else cid} | REDIRECT_URI: {ruri}")

            if not cid or not ruri:
                missing = []
                if not cid: missing.append('BLING_CLIENT_ID')
                if not ruri: missing.append('BLING_REDIRECT_URI')
                logger.error(f"❌ Variáveis faltando no Render: {', '.join(missing)}")
                return f"Erro: configure {', '.join(missing)} nas variáveis de ambiente do Render.", 500

            state    = secrets.token_urlsafe(32)
            self.orchestrator.auth._save_oauth_state(state)
            auth_url = self.orchestrator.auth.create_auth_flow(state)
            logger.info(f"🔗 Redirecionando para Bling OAuth: {auth_url[:80]}...")
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

        @self.app.route('/api/production/report')
        @token_required
        def api_production_report(token):
            """Relatório direto do Bling: pedidos, produzidos, top produtos, crescimento."""
            try:
                dias = int(request.args.get('dias', 30))
                hoje     = datetime.now()
                data_ini = (hoje - timedelta(days=dias)).strftime('%Y-%m-%d')
                data_ant = (hoje - timedelta(days=dias*2)).strftime('%Y-%m-%d')

                all_orders = self.orchestrator.sales._sales_history or []
                pedidos_p  = [o for o in all_orders if (o.get('data') or o.get('dataEmissao',''))[:10] >= data_ini]
                pedidos_a  = [o for o in all_orders if data_ant <= (o.get('data') or o.get('dataEmissao',''))[:10] < data_ini]

                total_rec  = len(pedidos_p)
                total_ant  = len(pedidos_a)
                crescimento = round((total_rec - total_ant) / total_ant * 100, 1) if total_ant else 0

                done_items = [i for i in pending_orders.data.values()
                              if i.get('status')=='done' and (i.get('finished_at','') or '')[:10] >= data_ini]
                total_prod = len(done_items)

                tempos = [i.get('tempo_producao',0) for i in done_items if i.get('tempo_producao')]
                avg_tp = round(sum(tempos)/len(tempos)/86400, 2) if tempos else 0

                from collections import Counter as _Ctr
                pc = _Ctr()
                for o in pedidos_p:
                    for item in (o.get('itens') or []):
                        n = (item.get('descricao') or item.get('nome') or '').strip()
                        if n: pc[n[:60]] += max(1, int(item.get('quantidade',1)))
                if not pc:
                    for po in pending_orders.data.values():
                        oid = str(po.get('order_id_bling') or po.get('order_id') or po.get('pedido_numero') or '')
                        data_po = (po.get('pedido_data','') or '')[:10]
                        if data_po >= data_ini:
                            n = (po.get('nome') or po.get('nome_original') or '').strip()
                            if n: pc[n[:60]] += 1

                from collections import defaultdict as _dd
                pd = _dd(int)
                for o in pedidos_p:
                    d = (o.get('data') or o.get('dataEmissao',''))[:10]
                    if d: pd[d] += 1
                labels = sorted(pd.keys())
                counts = [pd[l] for l in labels]

                return jsonify({'dias':dias,'total_recebidos':total_rec,'total_anterior':total_ant,
                    'crescimento':crescimento,'total_produzidos':total_prod,'avg_tempo_dias':avg_tp,
                    'top_produtos':[{'nome':k,'qtd':v} for k,v in pc.most_common(10)],
                    'labels':labels,'counts':counts})
            except Exception as e:
                logger.error('api_production_report: {}'.format(e))
                return jsonify({'error':str(e)}), 500


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
                tempo_prod = status.get('elapsed') or 0
                # Finaliza o pending_order vinculado a este timer_key
                if '||' in produto:
                    ikey = produto.split('||', 1)[1]
                    if ikey in pending_orders.data:
                        pending_orders.finish_production(ikey, tempo_segundos=tempo_prod)
                else:
                    # Fallback: busca por nome do produto em status in_production
                    for ikey, pitem in list(pending_orders.data.items()):
                        nome_item = pitem.get('nome') or pitem.get('nome_original', '')
                        if nome_item == produto and pitem.get('status') == 'in_production':
                            pending_orders.finish_production(ikey, tempo_segundos=tempo_prod)
                            break
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

            # Enriquece done com tempo de produção
            # Prioridade: tempo salvo no item > tempo do histórico de produção (por nome)
            done_items = pending_orders.get_done()
            hist_registros = production_timer.get_monthly_history_details()
            # Mapa: nome_produto (upper) -> lista de registros (pode ter múltiplas conclusões)
            hist_map = {}
            for reg in hist_registros:
                nome_h = (reg.get('produto') or '').strip().upper()
                if nome_h:
                    if nome_h not in hist_map:
                        hist_map[nome_h] = []
                    hist_map[nome_h].append(reg.get('tempo_segundos', 0))

            done_enriched = []
            for item in done_items:
                enriched_done = dict(item)
                nome_up = (item.get('nome') or item.get('nome_original', '')).strip().upper()
                # Se o item já tem tempo_producao salvo, usa esse; senão pega do histórico
                if not enriched_done.get('tempo_producao') and nome_up in hist_map:
                    tempos = hist_map[nome_up]
                    enriched_done['tempo_producao'] = tempos[-1] if tempos else 0
                done_enriched.append(enriched_done)

            return jsonify({
                'waiting': waiting_enriched,
                'in_production': in_prod,
                'orphan_timers': orphan,
                'done': done_enriched,
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
            # Prioridade: timer_key do cliente > timer_key salvo no item > reconstruído
            timer_key = (
                data.get('timer_key') or
                item_data.get('timer_key') or
                (f"{produto_nome}||{item_key}" if produto_nome else None)
            )
            # Para o timer ANTES de finalizar o pedido (para capturar o tempo)
            tempo_producao = None
            if timer_key:
                result = production_timer.stop_and_log(timer_key)
                tempo_producao = result.get('elapsed') or 0
                # Se tempo=0 e tem || no timer_key, tenta fallback pelo nome
                if tempo_producao == 0 and '||' in timer_key:
                    nome_fallback = timer_key.split('||')[0]
                    if nome_fallback in production_timer.timers:
                        result2 = production_timer.stop_and_log(nome_fallback)
                        tempo_producao = result2.get('elapsed') or 0
            elif produto_nome:
                result = production_timer.stop_and_log(produto_nome)
                tempo_producao = result.get('elapsed') or 0

            # Finaliza o pedido com o tempo capturado
            item = pending_orders.finish_production(item_key, tempo_segundos=tempo_producao)
            return jsonify({'success': True, 'item': item, 'tempo_producao': tempo_producao})

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

        @self.app.route('/api/barcode/scan', methods=['POST'])
        @token_required
        def api_barcode_scan(token):
            data    = request.json or {}
            codigo  = str(data.get('codigo', '')).strip()
            ikey_ov = str(data.get('item_key_override', '')).strip()
            if not codigo and not ikey_ov:
                return jsonify({'error': 'codigo obrigatorio'}), 400
            found_key = None
            found_item = None
            if ikey_ov and ikey_ov in pending_orders.data:
                item = pending_orders.data[ikey_ov]
                if item.get('status') not in ('done',):
                    found_key = ikey_ov
                    found_item = item
            if not found_item and codigo:
                for key, item in pending_orders.data.items():
                    if item.get('status') == 'done':
                        continue
                    pnum = str(item.get('pedido_numero','') or item.get('order_id','') or '')
                    op   = str(item.get('ordem_producao','') or '')
                    # Each product is unique — match by item_key OR barcode
                    if codigo in (pnum, op, key):
                        found_key = key
                        found_item = item
                        break

            # Anti-dup: each item has a unique scan_state tracking
            # 'scan_state' advances only ONCE per step per item
            if found_item and found_key:
                cur_status = found_item.get('status', 'waiting')
                scan_key   = f"scan_{found_key}_{cur_status}"
                # Use a short TTL flag in the item itself to prevent double-scan
                if found_item.get('_last_scan_key') == scan_key:
                    import time as _t
                    last_scan_ts = found_item.get('_last_scan_ts', 0)
                    if _t.time() - last_scan_ts < 5:  # 5s debounce per item per state
                        return jsonify({'acao':'ja_lido_etapa','codigo':codigo,
                            'mensagem':f'Produto já foi lido nesta etapa. Aguarde.'}), 200
                # Mark this scan
                found_item['_last_scan_key'] = scan_key
                found_item['_last_scan_ts']  = __import__('time').time()
            if not found_item:
                return jsonify({'acao':'nao_encontrado','codigo':codigo,
                    'mensagem':'Pedido {} nao encontrado.'.format(codigo)}), 404
            nome  = found_item.get('nome') or found_item.get('nome_original','')
            cur   = found_item.get('status', 'waiting')
            prox  = pending_orders._next_state(cur, nome)
            is_e  = pending_orders._is_esteira(nome)
            if not prox:
                return jsonify({'acao':'ja_concluido','codigo':codigo,'mensagem':'Pedido ja concluido.'})
            timer_key = found_item.get('timer_key') or '{}||{}'.format(nome, found_key)
            if cur == 'waiting':
                production_timer.start(timer_key)
                pending_orders.data[found_key]['timer_key'] = timer_key
            if cur == 'waiting' and is_e:
                try:
                    _cc = globals().get('component_consumption')
                    if _cc:
                        ck = 'scan_{}'.format(found_key)
                        logs = _cc.get_current_month().get('checklist_logs', [])
                        if not any(l.get('produto') == ck for l in logs):
                            for comp in RECIPE_CADEIRA:
                                _cc.register_component(comp['nome'], comp['qtd'], comp['un'], ck)
                except Exception as _ce:
                    logger.warning('Scan insumos: {}'.format(_ce))
            tempo_prod = 0
            if prox == 'done':
                result_t   = production_timer.stop_and_log(timer_key)
                tempo_prod = result_t.get('elapsed', 0)
            result_item = pending_orders.advance_production(found_key, tempo_segundos=tempo_prod or None)
            if not result_item:
                return jsonify({'acao':'erro_fsm','mensagem':'FSM recusou transicao'}), 409
            def _bc():
                try:
                    u = self.orchestrator.calculate_component_usage()
                    self.orchestrator._component_usage_cache = u
                    self.orchestrator.broadcast_kpi_update(component_usage=u)
                except Exception:
                    pass
            Thread(target=_bc, daemon=True).start()
            labels = {'marcenaria':'Marcenaria','tapecaria':'Tapecaria',
                      'in_production':'Em Producao','done':'Concluido'}
            label  = labels.get(prox, prox)
            acao   = 'concluido' if prox == 'done' else 'avancado'
            h = int(tempo_prod//3600)
            m = int((tempo_prod%3600)//60)
            s = int(tempo_prod%60)
            if prox == 'done':
                msg = 'CONCLUIDO: {} ({:02d}:{:02d}:{:02d})'.format(nome[:40],h,m,s)
            else:
                msg = '{}: {}'.format(label, nome[:40])
            return jsonify({'acao':acao,'codigo':codigo,'item_key':found_key,
                'nome':nome,'status_anterior':cur,'status_atual':prox,
                'status_label':label,'is_esteira':is_e,
                'tempo_producao':tempo_prod if prox=='done' else 0,'mensagem':msg})

        @self.app.route('/api/expedicao')
        @token_required
        def api_expedicao(token):
            try:
                def _si(v, d, mx=None):
                    try:
                        r = int(str(v).strip())
                        return max(1, min(r, mx) if mx else r)
                    except Exception:
                        return d
                page     = _si(request.args.get('page', 1), 1)
                per_page = _si(request.args.get('per_page', 50), 50, mx=200)
                urg_flt  = request.args.get('urgencia', 'all')
                hoje     = datetime.now().date()
                items = []
                for item in pending_orders.data.values():
                    if item.get('status') != 'done':
                        continue
                    de = item.get('data_entrega', '')
                    dias = None
                    urg = 'normal'
                    if de:
                        dt = _parse_order_date(de)
                        if dt:
                            dias = (dt.date() - hoje).days
                            if dias < 0:
                                urg = 'atrasado'
                            elif dias <= 2:
                                urg = 'critico'
                            elif dias <= 5:
                                urg = 'atencao'
                    items.append(dict(list(item.items()) + [('dias_restantes', dias), ('urgencia', urg)]))
                if urg_flt != 'all':
                    items = [i for i in items if i.get('urgencia') == urg_flt]
                uo = {'atrasado':0,'critico':1,'atencao':2,'normal':3}
                items.sort(key=lambda i: (uo.get(i.get('urgencia','normal'), 3), i.get('dias_restantes') or 9999))
                total = len(items)
                pg    = items[(page-1)*per_page : page*per_page]
                return jsonify({'items':pg,'total':total,'page':page,'per_page':per_page,
                    'pages':max(1,(total+per_page-1)//per_page)})
            except Exception as e:
                logger.error('api_expedicao: {}'.format(e))
                return jsonify({'error':str(e),'items':[],'total':0}), 500

        @self.app.route('/api/production/print-op/<path:item_key>')
        @token_required
        def api_print_op(token, item_key):
            item = pending_orders.data.get(item_key)
            if not item:
                return "<h2>Pedido nao encontrado</h2>", 404
            nome    = item.get('nome') or item.get('nome_original', 'N/D')
            op_num  = str(item.get('ordem_producao') or item.get('pedido_numero') or item.get('order_id', ''))
            cliente = item.get('cliente', '-')
            setor   = (item.get('setor') or '').title() or 'Producao'
            de      = item.get('data_entrega', '')
            base    = item.get('base', '')
            cor     = item.get('cor', '')
            status  = item.get('status', 'waiting')
            def fd(ds):
                if not ds:
                    return '-'
                for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d', '%d/%m/%Y'):
                    try:
                        import datetime as _dmod
                        return _dmod.datetime.strptime(ds[:10], fmt[:8]).strftime('%d/%m/%Y')
                    except Exception:
                        pass
                return ds[:10]
            slabel = {'waiting':'Aguardando','marcenaria':'Marcenaria','tapecaria':'Tapecaria',
                      'in_production':'Em Producao','done':'Concluido'}.get(status, status)
            if pending_orders._is_esteira(nome):
                instrucoes = '1a leitura=Marcenaria | 2a=Tapecaria | 3a=Concluido'
            else:
                instrucoes = '1a leitura=Producao | 2a=Concluido'
            now_str = datetime.now().strftime('%d/%m/%Y %H:%M')
            bc_color = '#000'
            bc_bg    = '#fff'
            html_parts = [
                '<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8">',
                '<title>OP ' + op_num + '</title>',
                '<script src="https://cdn.jsdelivr.net/npm/jsbarcode@3.11.6/dist/JsBarcode.all.min.js"></script>',
                '<style>',
                'STYLE_PLACEHOLDER',
                '</style></head><body>',
                '<div class="hdr">',
                '<div><div class="em">SW MOVEIS MDF</div>',
                '<div style="font-size:12px;color:#555">Ordem de Producao</div></div>',
                '<div style="text-align:right">',
                '<div class="op">OP #' + op_num + '</div>',
                '<span class="badge">' + slabel + '</span></div></div>',
                '<div class="grid">',
                '<div class="f" style="grid-column:1/-1"><label>Produto</label>',
                '<span style="font-size:16px">' + nome + '</span></div>',
                '<div class="f"><label>Setor</label><span>' + setor + '</span></div>',
                '<div class="f"><label>Cliente</label><span>' + cliente + '</span></div>',
                '<div class="f"><label>Pedido No</label><span>#' + op_num + '</span></div>',
                '<div class="f"><label>Base / Cor</label><span>' + base + (' / ' + cor if cor else '') + '</span></div>',
                '<div class="f"><label>Data Prevista</label><span style="font-size:16px">' + fd(de) + '</span></div>',
                '</div>',
                '<div class="bc"><svg id="op-bc"></svg>',
                '<div style="font-family:monospace;font-size:14px;font-weight:700;margin-top:6px">' + op_num + '</div>',
                '<div style="font-size:11px;color:#666;margin-top:4px">' + instrucoes + '</div></div>',
                '<div class="ft">SW Moveis MDF &nbsp;x&nbsp; ' + now_str + ' &nbsp;x&nbsp; OP #' + op_num + '</div>',
                '<div style="text-align:center;margin-top:16px">',
                '<button onclick="window.print()" style="padding:10px 24px;font-size:14px;background:#000;',
                'color:#fff;border:none;border-radius:6px;cursor:pointer">Imprimir</button></div>',
                '<script>',
                'JsBarcode("#op-bc","' + op_num + '",',
                '{format:"CODE128",width:2.5,height:80,displayValue:false,margin:8,',
                'background:"' + bc_bg + '",lineColor:"' + bc_color + '"});',
                '</script></body></html>',
            ]
            css = (
                '*{box-sizing:border-box;margin:0;padding:0}'
                'body{font-family:Arial,sans-serif;padding:20px;background:#fff}'
                '.hdr{display:flex;justify-content:space-between;border-bottom:3px solid #000;padding-bottom:12px;margin-bottom:16px}'
                '.em{font-size:22px;font-weight:900}'
                '.op{font-size:32px;font-weight:900;font-family:monospace}'
                '.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}'
                '.f{border:1px solid #ccc;border-radius:4px;padding:8px 10px}'
                '.f label{font-size:10px;font-weight:700;text-transform:uppercase;color:#666;display:block;margin-bottom:2px}'
                '.f span{font-size:14px;font-weight:700}'
                '.bc{text-align:center;border:2px solid #000;border-radius:6px;padding:16px;margin-bottom:16px}'
                '.badge{display:inline-block;padding:4px 14px;border-radius:50px;font-size:12px;font-weight:700;background:#ffb600;color:#000}'
                '.ft{border-top:1px solid #ccc;padding-top:8px;font-size:10px;color:#888;text-align:center}'
                '@media print{button{display:none!important}@page{size:A4;margin:10mm}}'
            )
            html = ''.join(html_parts).replace('STYLE_PLACEHOLDER', css)
            return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


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
            code  = request.args.get('code')
            state = request.args.get('state')
            error = request.args.get('error')

            if error:
                logger.error(f"❌ Bling retornou erro no OAuth: {error} — {request.args.get('error_description','')}")
                return f"Erro Bling OAuth: {error}. Tente novamente em /auth", 400

            logger.info("🔐 Callback OAuth recebido.")

            if not code:
                logger.error("Código de autorização OAuth não recebido.")
                return "Erro: Código de autorização não recebido.", 400

            if not self.orchestrator.auth._validate_oauth_state(state):
                logger.error(f"State OAuth inválido. Recebido: {state[:10] if state else 'None'}...")
                return "Erro: State inválido ou expirado. Acesse /auth novamente.", 403

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
                return "Erro ao trocar código pelo token. Verifique os logs.", 500

        # Rota de Busca com correção de 404 e Imagem
        @self.app.route('/api/products/search')
        @self.app.route('/products/search') # Aceita as duas chamadas
        @token_required
        def api_products_search(token):
            with self.orchestrator._cache_lock:
                cache_empty = (len(self.orchestrator._products_cache) == 0 and
                               len(self.orchestrator._kits_cache) == 0)
            if cache_empty:
                self.logger.info("🔄 Cache vazio na busca — iniciando em background...")
                if not getattr(self.orchestrator, '_cache_loading', False):
                    self.orchestrator._cache_loading = True
                    Thread(target=self.orchestrator.process_products_cache, daemon=True).start()
                return jsonify([]), 200
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

        @self.app.route('/api/status')
        def api_status():
            """HTTP status — also tries to reload token from disk if memory token expired."""
            import time as _t
            auth = self.orchestrator.auth
            # Try memory token first
            auth_ok = bool(auth._access_token and auth._expires_at > _t.time() + 30)
            # If not OK, try loading from disk (handles restart without re-auth)
            if not auth_ok:
                try:
                    auth.reload_tokens_from_disk()
                    auth_ok = bool(auth._access_token and auth._expires_at > _t.time() + 30)
                except Exception:
                    pass
            # If still not OK, try refresh
            if not auth_ok and auth._refresh_token:
                try:
                    if auth.refresh_token():
                        auth_ok = True
                except Exception:
                    pass
            return jsonify({
                'authenticated': auth_ok,
                'auth_url':      auth.get_auth_url(),
                'production': {
                    'waiting':       len(pending_orders.get_waiting()),
                    'in_production': len(pending_orders.get_in_production()),
                    'done':          len(pending_orders.get_done()),
                }
            })

        @self.app.route('/api/mongo-status')
        def api_mongo_status():
            """
            Diagnóstico completo do MongoDB.
            Testa conexão, leitura, escrita e mostra o que está salvo em cada coleção.
            Acesse: /api/mongo-status
            """
            result = {
                'mongodb_available': MONGO_AVAILABLE,
                'storage_backend': 'MongoDB' if MONGO_AVAILABLE else '⚠️ Arquivo Local (EFÊMERO — dados somem no restart!)',
                'env_vars': {
                    'MONGODB_URI_set': bool(os.environ.get('MONGODB_URI')),
                    'MONGO_URI_set':   bool(os.environ.get('MONGO_URI')),
                },
                'connection_test': None,
                'write_test': None,
                'collections': {},
                'errors': []
            }

            if not MONGO_AVAILABLE:
                uri_set = result['env_vars']['MONGODB_URI_set'] or result['env_vars']['MONGO_URI_set']
                if not uri_set:
                    result['errors'].append('❌ CRÍTICO: variável MONGODB_URI não está configurada no Render! '
                                            'Vá em Environment > Add Environment Variable > MONGODB_URI')
                else:
                    result['errors'].append('❌ MONGODB_URI está configurada mas a conexão falhou na inicialização. '
                                            'Verifique se o IP do Render está liberado no Atlas (Network Access > 0.0.0.0/0)')
                return jsonify(result), 200

            # Testa ping
            try:
                _mongo_client.admin.command('ping')
                result['connection_test'] = '✅ ping OK'
            except Exception as e:
                result['connection_test'] = f'❌ ping falhou: {e}'
                result['errors'].append(str(e))

            # Testa escrita e leitura
            try:
                _mongo_db['_diag_test'].replace_one(
                    {'_id': 'test'},
                    {'_id': 'test', 'ts': time.time()},
                    upsert=True
                )
                doc = _mongo_db['_diag_test'].find_one({'_id': 'test'})
                result['write_test'] = '✅ escrita/leitura OK' if doc else '❌ escrita OK mas leitura falhou'
            except Exception as e:
                result['write_test'] = f'❌ falhou: {e}'
                result['errors'].append(str(e))

            # Inspeciona cada coleção relevante
            collections_to_check = {
                'auth_tokens':           ('tokens',  ['access_token', 'refresh_token', 'expires_at']),
                'production_timers':     ('timers',  ['timers']),
                'production_history':    (None,      ['registros']),
                'component_consumption': ('main',    ['data']),
                'pending_orders':        (None,      None),
                'sales_stats':           ('stats',   ['daily', 'monthly']),
                'sales_history':         ('history', ['orders']),
                'products_cache':        ('cache',   ['products', 'kits']),
            }

            for col, (doc_id, fields) in collections_to_check.items():
                try:
                    count = _mongo_db[col].count_documents({})
                    info = {'total_docs': count}
                    if count == 0:
                        info['status'] = '⚠️ vazio'
                    else:
                        info['status'] = '✅ tem dados'
                        if doc_id:
                            doc = _mongo_db[col].find_one({'_id': doc_id})
                            if doc and fields:
                                info['campos_presentes'] = [f for f in fields if f in doc]
                                info['campos_ausentes']  = [f for f in fields if f not in doc]
                                for f in fields:
                                    val = doc.get(f)
                                    if isinstance(val, list):
                                        info[f'qtd_{f}'] = len(val)
                                    elif isinstance(val, dict):
                                        info[f'qtd_{f}_chaves'] = len(val)
                        else:
                            sample = list(_mongo_db[col].find({}, {'_id': 1}).limit(5))
                            info['sample_ids'] = [str(d['_id']) for d in sample]
                    result['collections'][col] = info
                except Exception as e:
                    result['collections'][col] = {'status': f'❌ erro: {e}'}
                    result['errors'].append(f'{col}: {e}')

            result['resumo'] = (
                '✅ MongoDB OK — dados persistem entre restarts'
                if not result['errors'] and result['write_test'] and 'OK' in result['write_test']
                else '⚠️ MongoDB com problemas — veja errors acima'
            )
            return jsonify(result), 200

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
            # Sempre recalcula — nunca serve cache para history_production
            # (garante que finalizações recentes aparecem imediatamente)
            """Retorna uso de componentes (do cache do worker)."""
            try:
                # Retorna cache se disponível E não vazio
                cache = None  # Sempre recalcula para garantir history atualizado
                _old_cache = getattr(self.orchestrator, '_component_usage_cache', None)
                
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
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/jsbarcode@3.11.6/dist/JsBarcode.all.min.js"></script>
    <style>
        :root {
            --sw-yellow: #ffb600;
            --sw-dark:   #01010d;
            --border:    rgba(255,255,255,0.08);
            --card-bg:   #ffffff;
            --success:   #10b981;
            --danger:    #ef4444;
            --warning:   #f59e0b;
        }
        * { box-sizing: border-box; }
        body { background: #f4f4f0; font-family: 'Inter', sans-serif; color: #1a1a1a; }

        /* Navbar */
        .sw-navbar { background: var(--sw-dark); padding: 10px 24px; display: flex; align-items: center; justify-content: space-between; box-shadow: 0 2px 20px rgba(0,0,0,.4); position: sticky; top: 0; z-index: 1000; }
        .sw-logo   { font-family: 'Bebas Neue', sans-serif; font-size: 1.6rem; color: var(--sw-yellow); letter-spacing: .06em; }
        .sw-logo span { color: #fff; }

        /* Cards */
        .card { border: none; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,.07); background: var(--card-bg); transition: box-shadow .2s, transform .2s; }
        .card:hover { box-shadow: 0 6px 24px rgba(0,0,0,.12); }
        .card-header { background: var(--sw-dark) !important; color: #fff; border-radius: 12px 12px 0 0 !important; border: none; padding: 14px 20px; }

        /* KPI cards */
        .kpi-card { border-radius: 12px; padding: 20px; background: #fff; transition: transform .15s; }
        .kpi-card:hover { transform: translateY(-2px); }
        .kpi-card h5 { font-size: .75rem; text-transform: uppercase; letter-spacing: .1em; color: #888; margin-bottom: 6px; }
        .kpi-card h3 { font-family: 'Bebas Neue', sans-serif; font-size: 3rem; margin: 0; line-height: 1; }
        .kpi-num    { font-family: 'Bebas Neue', sans-serif; font-size: 2.8rem; line-height: 1; }

        /* Nav tabs */
        .nav-tabs { border-bottom: 2px solid #e5e5e5; flex-wrap: nowrap; overflow-x: auto; }
        .nav-tabs .nav-link { color: #666; font-weight: 600; font-size: .82rem; padding: 10px 16px; border: none; border-bottom: 3px solid transparent; white-space: nowrap; }
        .nav-tabs .nav-link.active { color: var(--sw-dark); border-bottom-color: var(--sw-yellow); background: transparent; }
        .nav-tabs .nav-link:hover  { color: var(--sw-dark); }

        /* Board cards */
        .bc-card { background: #fff; border: 2px solid #e5e5e5; border-radius: 12px; padding: 14px; height: 100%; transition: border-color .2s, box-shadow .2s; position: relative; overflow: hidden; }
        .bc-card:hover { border-color: var(--sw-yellow); box-shadow: 0 4px 16px rgba(0,0,0,.1); }
        .bc-card.inprod   { border-color: var(--success); background: #f0fdf4; }
        .bc-card.marcen   { border-color: #f59e0b; background: #fffbeb; }
        .bc-card.tapec    { border-color: #8b5cf6; background: #faf5ff; }
        .bc-card.done-card{ border-color: #6366f1; background: #f5f3ff; }
        .bc-card.urgente  { border-color: var(--danger) !important; background: #fff5f5; }
        .bc-nome { font-weight: 700; font-size: .88rem; margin-bottom: 4px; line-height: 1.3; }
        .bc-num  { font-family: monospace; font-size: .75rem; color: #666; margin-bottom: 6px; }
        .bc-meta { font-size: .7rem; color: #999; margin-top: 6px; }
        .bc-svg-wrap svg { max-width: 100%; height: auto; display: block; margin: 0 auto; }

        /* Board tab buttons */
        .board-tab-btn { border-radius: 50px; padding: 6px 18px; font-size: .8rem; font-weight: 700; cursor: pointer; transition: all .2s; }
        .active-board-tab { box-shadow: 0 4px 14px rgba(0,0,0,.2) !important; }

        /* Scanner indicator */
        #scanner-indicator { position: fixed; bottom: 20px; right: 20px; background: var(--sw-dark); color: var(--sw-yellow); border: 2px solid var(--sw-yellow); border-radius: 50px; padding: 8px 20px; font-size: .8rem; font-weight: 700; z-index: 9999; display: none; transition: all .3s; }
        #scanner-indicator.active { display: block; animation: scan-pulse .4s ease; }
        @keyframes scan-pulse { 0%{transform:scale(.9);opacity:.6} 100%{transform:scale(1);opacity:1} }

        /* Animations */
        @keyframes fadeInUp { from{opacity:0;transform:translateY(12px)} to{opacity:1;transform:translateY(0)} }
        @keyframes pulse-animation { 0%,100%{opacity:1} 50%{opacity:.5} }
        .fade-in-up { animation: fadeInUp .35s ease both; }
        .kpi-card.updating { animation: pulse-animation .6s; }

        /* Status badges */
        .badge-marcenaria { background: #f59e0b; color: #000; }
        .badge-tapecaria  { background: #8b5cf6; color: #fff; }
        .badge-inprod     { background: var(--success); color: #fff; }
        .badge-done       { background: #6366f1; color: #fff; }
        .badge-waiting    { background: var(--sw-yellow); color: #000; }
        .badge-atrasado   { background: var(--danger); color: #fff; }
        .badge-critico    { background: #f97316; color: #fff; }
        .badge-atencao    { background: var(--warning); color: #000; }

        /* Print */
        @media print {
            body > *:not(#print-area) { display: none !important; }
            #print-area { display: block !important; position: fixed !important; inset: 0 !important; background: #fff !important; z-index: 999999 !important; padding: 0 !important; }
            #print-area * { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }
            @page { size: A4; margin: 10mm; }
        }
        #print-area { display: none; }

        /* Toast */
        .toast-container { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); z-index: 9998; display: flex; flex-direction: column; gap: 8px; align-items: center; }
        .sw-toast { background: var(--sw-dark); color: #fff; border-left: 4px solid var(--sw-yellow); border-radius: 8px; padding: 12px 20px; font-size: .85rem; font-weight: 600; box-shadow: 0 4px 20px rgba(0,0,0,.3); animation: fadeInUp .3s ease; min-width: 260px; max-width: 420px; }
        .sw-toast.success { border-left-color: var(--success); }
        .sw-toast.danger  { border-left-color: var(--danger); }
        .sw-toast.warning { border-left-color: var(--warning); }
        .sw-toast.info    { border-left-color: #6366f1; }

        .hidden { display: none !important; }
        /* Auth-required visible by default until JS confirms auth */
        #content-tabs { display: none; }
        #auth-required-tabs { display: block; }
        .sw-pattern-bar { height: 4px; background: linear-gradient(90deg, var(--sw-yellow) 0%, #fff 50%, var(--sw-yellow) 100%); }

        /* Setor badge */
        .setor-badge { font-size: .65rem; font-weight: 700; padding: 2px 8px; border-radius: 50px; display: inline-block; margin-bottom: 4px; }
    </style>
</head>
<body>
    <!-- NAVBAR -->
    <nav class="sw-navbar">
        <div class="sw-logo">SW <span>Móveis</span> MDF</div>
        <div class="d-flex align-items-center gap-2 flex-wrap">
            <span id="last-reader-badge" style="display:none;padding:3px 10px;border-radius:50px;font-size:.7rem;font-weight:700;transition:all .3s">—</span>
            <span id="status-badge" class="badge bg-secondary" title="Aguardando...">⏳ Conectando...</span>
            <a id="auth-link" href="{{ auth_url }}" class="btn btn-sm btn-outline-light" style="border-radius:50px">Autenticar</a>
        </div>
    </nav>

    <!-- CONTAINER PRINCIPAL -->
    <div class="container-fluid px-4 py-4">

        <!-- AUTH REQUIRED -->
        <!-- AUTH REQUIRED — sempre visível até autenticação confirmada -->
        <div id="auth-required-tabs" class="mt-4" style="display:block">
            <div class="text-center py-5">
                <div style="font-size:4rem;margin-bottom:16px">🔐</div>
                <h3 class="fw-bold mb-2">Autenticação necessária</h3>
                <p class="text-muted mb-4">Conecte sua conta Bling para acessar o painel de produção SW Móveis MDF.</p>
                <a id="auth-link-main" href="/auth" class="btn btn-warning btn-lg fw-bold px-5" style="border-radius:50px;font-size:1.1rem">
                    🔗 Autenticar com Bling
                </a>
                <div class="mt-3" id="connect-status" style="font-size:.82rem;color:#888">
                    ⏳ Verificando conexão...
                </div>
            </div>
        </div>

        <!-- TABS PRINCIPAIS -->
        <div id="content-tabs" class="hidden">
            <ul class="nav nav-tabs mb-0 mt-3" id="mainTab">
                <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#tab-dashboard">📊 Dashboard</button></li>
                <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-producao">🏭 Produção</button></li>
                <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-insumos">📦 Insumos</button></li>
                <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-expedicao">🚚 Expedição</button></li>
                <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-relatorio">📋 Relatório</button></li>
                <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-ficha">🔧 Ficha Técnica</button></li>
            </ul>

            <div class="tab-content pt-4">

                <!-- ═══ TAB 1: DASHBOARD ═══ -->
                <div class="tab-pane fade show active" id="tab-dashboard">
                    <!-- Filtro data unificado -->
                    <div class="d-flex gap-2 align-items-center flex-wrap mb-4">
                        <label class="text-muted small fw-bold mb-0">De:</label>
                        <input type="date" id="filter-date-from" class="form-control form-control-sm" style="width:140px">
                        <label class="text-muted small fw-bold mb-0">Até:</label>
                        <input type="date" id="filter-date-to" class="form-control form-control-sm" style="width:140px">
                        <button class="btn btn-primary btn-sm" onclick="applyDashboardFilter()">Filtrar</button>
                        <button class="btn btn-outline-secondary btn-sm" onclick="resetDashboardFilter()">Limpar</button>
                        <button class="btn btn-outline-dark btn-sm ms-auto" onclick="printDashboard()">🖨️ Imprimir</button>
                    </div>

                    <!-- KPIs Pedidos -->
                    <div class="row g-3 mb-4">
                        <div class="col-6 col-md-3">
                            <div class="kpi-card border-start border-4 border-warning fade-in-up">
                                <h5>Pedidos Hoje</h5>
                                <div class="kpi-num" id="kpi-daily" style="color:var(--sw-yellow)">0</div>
                                <div id="kpi-daily-sub" style="font-size:.65rem;color:#aaa;margin-top:4px;max-height:36px;overflow:hidden"></div>
                            </div>
                        </div>
                        <div class="col-6 col-md-3">
                            <div class="kpi-card border-start border-4 border-warning fade-in-up" style="animation-delay:.05s">
                                <h5>Esta Semana</h5>
                                <div class="kpi-num" id="kpi-weekly" style="color:#f59e0b">0</div>
                            </div>
                        </div>
                        <div class="col-6 col-md-3">
                            <div class="kpi-card border-start border-4 border-success fade-in-up" style="animation-delay:.1s">
                                <h5>Este Mês</h5>
                                <div class="kpi-num" id="kpi-historic" style="color:var(--success)">0</div>
                            </div>
                        </div>
                        <div class="col-6 col-md-3">
                            <div class="kpi-card border-start border-4 fade-in-up" style="border-color:#6366f1!important;animation-delay:.15s">
                                <h5>Tendência</h5>
                                <div class="kpi-num" id="trend-indicator" style="font-size:1.4rem;color:#6366f1">—</div>
                                <div id="growth-weekly" style="font-size:.85rem;font-weight:700"></div>
                                <div id="growth-tooltip" style="font-size:.6rem;color:#aaa;margin-top:2px;line-height:1.3"></div>
                            </div>
                        </div>
                    </div>

                    <!-- KPIs Produção (3 etapas) -->
                    <div class="row g-3 mb-4">
                        <div class="col-md-4">
                            <div class="kpi-card text-center fade-in-up" style="border-top:4px solid var(--sw-yellow)">
                                <h5>⏳ Em Espera</h5>
                                <div class="kpi-num" id="kpi-waiting" style="color:var(--sw-yellow)">0</div>
                                <div id="kpi-waiting-nums" style="font-size:.62rem;color:#aaa;max-height:40px;overflow:hidden"></div>
                            </div>
                        </div>
                        <div class="col-md-4">
                            <div class="kpi-card text-center fade-in-up" style="border-top:4px solid var(--success);animation-delay:.05s">
                                <h5>⚙️ Produzindo</h5>
                                <div class="kpi-num" id="kpi-inprod" style="color:var(--success)">0</div>
                                <div id="kpi-inprod-nums" style="font-size:.62rem;color:#aaa;max-height:40px;overflow:hidden"></div>
                            </div>
                        </div>
                        <div class="col-md-4">
                            <div class="kpi-card text-center fade-in-up" style="border-top:4px solid #6366f1;animation-delay:.1s">
                                <h5>✅ Concluídos (Mês)</h5>
                                <div class="kpi-num" id="kpi-done" style="color:#6366f1">0</div>
                                <div id="kpi-done-nums" style="font-size:.62rem;color:#aaa;max-height:40px;overflow:hidden"></div>
                            </div>
                        </div>
                    </div>

                    <!-- Gráficos -->
                    <div class="row g-3 mb-4">
                        <div class="col-lg-8">
                            <div class="card">
                                <div class="card-header d-flex justify-content-between align-items-center">
                                    <span>📈 Pedidos × Produção <small id="last-recalculated" class="text-white-50 ms-2" style="font-size:.7rem"></small></span>
                                </div>
                                <div class="card-body" style="height:300px"><canvas id="salesChart"></canvas></div>
                            </div>
                        </div>
                        <div class="col-lg-4">
                            <div class="card h-100">
                                <div class="card-header">⚡ Etapas de Produção</div>
                                <div class="card-body" style="height:260px"><canvas id="stagesChart"></canvas></div>
                            </div>
                        </div>
                    </div>
                    <div class="row g-3">
                        <div class="col-md-6">
                            <div class="card">
                                <div class="card-header">📊 Produção por Dia</div>
                                <div class="card-body" style="height:220px"><canvas id="prodBarChart"></canvas></div>
                            </div>
                        </div>
                        <div class="col-md-6">
                            <div class="card">
                                <div class="card-header">📉 Queda / 📈 Subida</div>
                                <div class="card-body" style="height:220px"><canvas id="deltaChart"></canvas></div>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- ═══ TAB 2: PRODUÇÃO ═══ -->
                <div class="tab-pane fade" id="tab-producao">
                    <!-- Sub-abas do board -->
                    <div class="d-flex justify-content-between align-items-center flex-wrap gap-2 mb-3">
                        <div class="d-flex gap-2 flex-wrap">
                            <button id="tab-waiting-btn" onclick="switchBoardTab('waiting')" class="board-tab-btn active-board-tab"
                                style="background:rgba(255,182,0,.18);border:2px solid #ffb600;color:#ffb600;">
                                ⏳ Em Espera <span id="waiting-count-badge" style="background:#ffb600;color:#000;border-radius:50px;padding:1px 8px;font-size:.7rem;margin-left:4px">0</span>
                            </button>
                            <button id="tab-inprod-btn" onclick="switchBoardTab('inprod')" class="board-tab-btn"
                                style="background:rgba(16,185,129,.12);border:2px solid rgba(16,185,129,.4);color:#10b981;">
                                ⚙️ Produzindo <span id="inprod-count-badge" style="background:#10b981;color:#fff;border-radius:50px;padding:1px 8px;font-size:.7rem;margin-left:4px">0</span>
                            </button>
                            <button id="tab-done-btn" onclick="switchBoardTab('done')" class="board-tab-btn"
                                style="background:rgba(99,102,241,.12);border:2px solid rgba(99,102,241,.3);color:#6366f1;">
                                ✅ Concluídos <span id="done-count-badge" style="background:#6366f1;color:#fff;border-radius:50px;padding:1px 8px;font-size:.7rem;margin-left:4px">0</span>
                            </button>
                        </div>
                        <button class="btn btn-sm btn-outline-primary" onclick="syncAndRefreshPending()">🔄 Sincronizar</button>
                    </div>

                    <!-- Setor tabs (visível em Produzindo) -->
                    <div id="setor-tabs-wrap" style="display:none" class="mb-3">
                        <div class="d-flex gap-2">
                            <button onclick="switchSetor('todos')" id="setor-todos" class="btn btn-sm btn-dark">Todos</button>
                            <button onclick="switchSetor('marcenaria')" id="setor-marc" class="btn btn-sm btn-outline-secondary">🪚 Marcenaria</button>
                            <button onclick="switchSetor('tapecaria')" id="setor-tape" class="btn btn-sm btn-outline-secondary">🧵 Tapeçaria</button>
                            <button onclick="printSetor()" class="btn btn-sm btn-outline-dark ms-auto">🖨️ Imprimir Setor</button>
                        </div>
                    </div>

                    <!-- Buscador (só em Produzindo) -->
                    <div id="search-inprod-wrap" style="display:none" class="mb-3">
                        <input type="text" id="search-inprod" class="form-control form-control-sm" placeholder="🔍 Buscar pedido ou cliente..." oninput="filterInProd(this.value)" style="max-width:360px">
                    </div>

                    <!-- Painéis -->
                    <div id="board-waiting" class="board-panel"></div>
                    <div id="board-inprod"   class="board-panel" style="display:none"></div>
                    <div id="board-done"     class="board-panel" style="display:none"></div>
                </div>

                <!-- ═══ TAB 3: INSUMOS ═══ -->
                <div class="tab-pane fade" id="tab-insumos">
                    <div class="d-flex justify-content-between align-items-center mb-4">
                        <div><h5 class="mb-0">📦 Gestão de Insumos</h5><small class="text-muted">Consumo real × necessidade pelos pedidos</small></div>
                    </div>
                    <div class="card mb-4">
                        <div class="card-header">🛒 Guia de Compras — Baseado nos Pedidos em Espera</div>
                        <div class="card-body p-0" id="purchase-guide-section"><div class="text-center py-4 text-muted">⏳ Calculando...</div></div>
                    </div>
                    <div class="card">
                        <div class="card-header">📊 Consumo Real do Mês <small id="consumption-month-label" class="text-white-50"></small></div>
                        <div class="card-body p-0" id="consumption-table-section"><div class="text-center py-4 text-muted">⏳ Carregando...</div></div>
                    </div>
                </div>

                <!-- ═══ TAB 4: EXPEDIÇÃO ═══ -->
                <div class="tab-pane fade" id="tab-expedicao">
                    <div class="d-flex justify-content-between align-items-center mb-3">
                        <h5 class="mb-0">🚚 Expedição</h5>
                        <button class="btn btn-outline-dark btn-sm" onclick="printExpedicao()">🖨️ Imprimir</button>
                    </div>
                    <div class="d-flex gap-2 mb-3 flex-wrap">
                        <button onclick="filterExpedicao('all')" class="btn btn-sm btn-dark">Todos</button>
                        <button onclick="filterExpedicao('atrasado')" class="btn btn-sm btn-danger">🔴 Atrasados</button>
                        <button onclick="filterExpedicao('critico')" class="btn btn-sm btn-warning text-dark">🟡 Crítico</button>
                        <button onclick="filterExpedicao('atencao')" class="btn btn-sm btn-info text-dark">🔵 Atenção</button>
                        <button onclick="filterExpedicao('normal')" class="btn btn-sm btn-success">🟢 No prazo</button>
                    </div>
                    <div id="expedicao-section"><div class="text-center py-5 text-muted">⏳ Carregando...</div></div>
                </div>

                <!-- ═══ TAB 5: RELATÓRIO ═══ -->
                <div class="tab-pane fade" id="tab-relatorio">
                    <div class="d-flex justify-content-between align-items-center mb-4">
                        <h5 class="mb-0">📋 Relatório de Produção</h5>
                        <div class="d-flex gap-2">
                            <button onclick="loadRelatorio(7)"  class="btn btn-sm btn-outline-primary">7 dias</button>
                            <button onclick="loadRelatorio(30)" class="btn btn-sm btn-outline-primary">30 dias</button>
                            <button onclick="printRelatorio()"  class="btn btn-sm btn-outline-dark">🖨️</button>
                        </div>
                    </div>
                    <div id="relatorio-section"><div class="text-center py-5 text-muted">Selecione o período acima.</div></div>
                    <div class="card mt-4">
                        <div class="card-header">📜 Histórico de Finalizações</div>
                        <div class="card-body p-0" id="production-history-section"><div class="text-center py-4 text-muted">⏳ Carregando...</div></div>
                    </div>
                </div>

                <!-- ═══ TAB 6: FICHA TÉCNICA ═══ -->
                <div class="tab-pane fade" id="tab-ficha">
                    <div class="d-flex justify-content-between align-items-center mb-4">
                        <h5 class="mb-0">🔧 Ficha Técnica</h5>
                        <button class="btn btn-sm btn-outline-dark" onclick="printFicha()">🖨️ Imprimir</button>
                    </div>
                    <div class="card">
                        <div class="card-header">📐 Cadeira SW — Insumos por Unidade</div>
                        <div class="card-body p-0" id="ficha-section"><div class="text-center py-4 text-muted">⏳ Carregando...</div></div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- SCANNER INDICATOR -->
    <div id="scanner-indicator">📡 Lendo código...</div>

    <!-- PRINT AREA -->
    <div id="print-area"></div>

    <!-- TOAST CONTAINER -->
    <div class="toast-container" id="toast-container"></div>

    <div class="sw-pattern-bar mt-4"></div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        const API = '/api';
        let isAuthenticated = false;
        let salesChart = null, stagesChart = null, prodBarChart = null, deltaChart = null;
        let _currentSetor = 'todos';
        let _boardDataRaw = null;
        let _boardData    = null;
        let _currentBoardTab = 'waiting';
        let _boardPoll = null;
        let _boardTick = null;
        let _boardInitialized = false;
        let _expedicaoPage = 1;
        let _expedicaoFilter = 'all';
        let _dashFilter = {from: null, to: null};
        let wsKpi = null;
        let _kpiReconnectDelay = 2000;
        let _kpiReconnectTimer = null;
        let _wsFirstAuthDone = false;
        let _wsConnected = false;

        /* ── Safe date helpers ── */
        function safeDate(raw) {
            if (!raw || raw === 'null' || raw === 'N/D') return null;
            raw = String(raw).trim();
            if (raw.length >= 8 && raw[2] === '/' && raw[5] === '/') {
                raw = raw.slice(6,10) + '-' + raw.slice(3,5) + '-' + raw.slice(0,2);
            }
            if (raw.length > 10 && raw[10] === ' ') raw = raw.slice(0,10) + 'T' + raw.slice(11);
            const dt = new Date(raw);
            return isNaN(dt.getTime()) ? null : dt;
        }
        function safeDateStr(raw, opts) {
            const d = safeDate(raw);
            return d ? d.toLocaleDateString('pt-BR', opts||{}) : '—';
        }
        function safeDateTimeStr(raw) {
            const d = safeDate(raw);
            return d ? d.toLocaleString('pt-BR') : '—';
        }
        function formatDateTime(iso) {
            if (!iso) return '—';
            try {
                const d = new Date(iso);
                if (isNaN(d.getTime())) return '—';
                return d.toLocaleString('pt-BR');
            } catch(e) { return '—'; }
        }
        function formatSeconds(s) {
            if (!s || s<=0) return '—';
            if (s>=86400) return (s/86400).toFixed(2)+'d';
            if (s>=3600)  return Math.floor(s/3600)+'h'+String(Math.floor((s%3600)/60)).padStart(2,'0')+'m';
            return Math.floor(s/60)+'m'+String(Math.floor(s%60)).padStart(2,'0')+'s';
        }
        function escapeHtml(str) {
            if (!str) return '';
            return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
        }

        /* ── Toast ── */
        function showToast(title, msg, type) {
            type = type || 'info';
            const c = document.getElementById('toast-container');
            if (!c) return;
            const t = document.createElement('div');
            t.className = 'sw-toast ' + type;
            t.innerHTML = '<strong>' + escapeHtml(title) + '</strong>' + (msg ? '<br><span style="font-weight:400;opacity:.85">' + escapeHtml(String(msg).slice(0,80)) + '</span>' : '');
            c.appendChild(t);
            setTimeout(() => { t.style.opacity='0'; t.style.transform='translateY(8px)'; setTimeout(()=>t.remove(),300); }, 3500);
        }

        /* ── fetchAPI ── */
        async function fetchAPI(url, opts) {
            const res = await fetch(url, opts||{});
            if (!res.ok) throw new Error('HTTP ' + res.status + ': ' + await res.text().catch(()=>''));
            return res.json();
        }

        /* ── Auth status ── */
        function updateAuthStatus(authenticated, authUrl) {
            isAuthenticated = !!authenticated;
            const badge  = document.getElementById('status-badge');
            const link   = document.getElementById('auth-link');
            const tabs   = document.getElementById('content-tabs');
            const authEl = document.getElementById('auth-required-tabs');
            if (badge) {
                badge.textContent = authenticated ? '✅ Autenticado' : '⚠️ Não autenticado — clique em Autenticar';
                badge.className   = 'badge ' + (authenticated ? 'bg-success' : 'bg-warning text-dark');
            }
            if (authUrl) {
                const lnk = document.getElementById('auth-link');
                if (lnk) lnk.href = authUrl;
            }
            if (link)   link.style.display = authenticated ? 'none' : 'inline-block';
            if (tabs)   tabs.classList.toggle('hidden', !authenticated);
            // Always show auth-required section when NOT authenticated
            // Show/hide auth-required section
            if (authEl) {
                authEl.style.display = authenticated ? 'none' : 'block';
            }
            // Show/hide main content tabs
            if (tabs) {
                tabs.style.display = authenticated ? 'block' : 'none';
            }
        }

        /* ── Setor classifier ── */
        function _classifySetor(nome) {
            const n = (nome||'').toUpperCase();
            const tape = ['CADEIRA','POLTRONA','EVIDENCE','BERLIN','MADRID','DIAMANTE',
                          'HIDRÁULICA','HIDRAULICA','RECLINÁVEL','RECLINAVEL','ASSENTO','ESPUMA'];
            const marc = ['MDF','COMPENSADO','MADEIRA','SARRAFO','ARMÁRIO','ARMARIO',
                          'BALCÃO','BALCAO','BANCADA','CARRINHO','LAVATÓRIO','LAVATORIO'];
            if (tape.some(k => n.includes(k))) return 'tapecaria';
            if (marc.some(k => n.includes(k))) return 'marcenaria';
            return 'outros';
        }

        /* ═══════════════════════════════════════════════════
           SCANNER USB — buffer inteligente
        ═══════════════════════════════════════════════════ */
        /* ═══════════════════════════════════════════════════════════
           SISTEMA DE IDENTIFICAÇÃO DE LEITORES
           4 leitores físicos USB — cada um identificado pelo prefixo
           que o usuário configura no programa de configuração do leitor:
             Leitor 1 (Entrada/Espera):    prefixo R1-
             Leitor 2 (Marcenaria):        prefixo R2-
             Leitor 3 (Tapeçaria):         prefixo R3-
             Leitor 4 (Conclusão/QC):      prefixo R4-
           Sem prefixo: qualquer leitor pode ler (modo universal)
        ═══════════════════════════════════════════════════════════ */
        const LEITOR_CONFIG = {
            'R1-': { nome: 'Leitor 1 — Entrada',     cor: '#ffb600', etapa: 'waiting'      },
            'R2-': { nome: 'Leitor 2 — Marcenaria',  cor: '#f59e0b', etapa: 'marcenaria'   },
            'R3-': { nome: 'Leitor 3 — Tapeçaria',   cor: '#8b5cf6', etapa: 'tapecaria'    },
            'R4-': { nome: 'Leitor 4 — Conclusão',   cor: '#10b981', etapa: 'done'         },
        };
        let _lastLeitor = null;

        function _detectLeitor(codigo) {
            for (const [prefix, cfg] of Object.entries(LEITOR_CONFIG)) {
                if (codigo.startsWith(prefix)) {
                    return { ...cfg, prefix, codigoPuro: codigo.slice(prefix.length) };
                }
            }
            return { nome: 'Leitor Universal', cor: '#6366f1', etapa: null, prefix: '', codigoPuro: codigo };
        }

        (function() {
            let _buf = '', _timer = null, _lastAt = 0;
            const MIN_LEN = 4, SCAN_GAP = 100;
            const _ind = document.getElementById('scanner-indicator');

            function _showInd(msg, color) {
                if (!_ind) return;
                _ind.textContent = '📡 ' + msg;
                _ind.style.borderColor = color || '#ffb600';
                _ind.style.color       = color || '#ffb600';
                _ind.classList.add('active');
                clearTimeout(_ind._t);
                _ind._t = setTimeout(() => _ind.classList.remove('active'), 3500);
            }

            async function _processScan(codigoRaw) {
                if (!isAuthenticated) { _showInd('Não autenticado', '#ef4444'); return; }

                // Detecta qual leitor enviou o código
                const leitor = _detectLeitor(codigoRaw);
                const codigo = leitor.codigoPuro;
                _lastLeitor  = leitor;

                // Mostra identificação do leitor + código
                _showInd(leitor.nome + ' · #' + codigo, leitor.cor);

                // Exibe badge do leitor na UI
                const lBadge = document.getElementById('last-reader-badge');
                if (lBadge) {
                    lBadge.textContent = leitor.nome;
                    lBadge.style.background = leitor.cor;
                    lBadge.style.color = leitor.cor === '#ffb600' || leitor.cor === '#f59e0b' ? '#000' : '#fff';
                    lBadge.style.display = 'inline-block';
                }

                try {
                    const res = await fetch('/api/barcode/scan', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({codigo: codigo, leitor: leitor.nome})
                    });
                    const result = await res.json();
                    const acao   = result.acao   || '';
                    const nome   = result.nome    || '';
                    const label  = result.status_label || '';
                    const isEst  = result.is_esteira || false;

                    if (acao === 'avancado') {
                        const colors = {marcenaria:'#f59e0b', tapecaria:'#8b5cf6', in_production:'#10b981'};
                        const c = colors[result.status_atual] || leitor.cor;
                        const instrucao = isEst && result.status_atual === 'marcenaria'
                            ? '→ Leia novamente para Tapeçaria'
                            : isEst && result.status_atual === 'tapecaria'
                            ? '→ Leia novamente para Concluir'
                            : '→ Leia novamente para Concluir';
                        _showInd(leitor.nome + ' · ' + label + ' · ' + nome.slice(0,20), c);
                        showToast(
                            leitor.nome + ' · ' + label,
                            nome + '
' + instrucao,
                            'success'
                        );
                        await loadProductionBoard();
                        switchBoardTab('inprod');
                        if (result.status_atual === 'marcenaria') switchSetor('marcenaria');
                        else if (result.status_atual === 'tapecaria') switchSetor('tapecaria');

                    } else if (acao === 'concluido') {
                        const tp = result.tempo_producao || 0;
                        _showInd(leitor.nome + ' · ✅ CONCLUÍDO · ' + nome.slice(0,20), '#6366f1');
                        showToast(
                            leitor.nome + ' · ✅ Concluído!',
                            nome + (tp ? ' · ' + formatSeconds(tp) : ''),
                            'success'
                        );
                        await loadProductionBoard();
                        switchBoardTab('done');

                    } else if (acao === 'nao_encontrado') {
                        _showInd(leitor.nome + ' · ❌ Não encontrado: #' + codigo, '#ef4444');
                        showToast('Não encontrado', 'Pedido #' + codigo + ' não está na fila ativa.', 'warning');

                    } else if (acao === 'ja_concluido') {
                        _showInd(leitor.nome + ' · Já concluído', '#6b7280');
                        showToast('Info', result.mensagem||'Pedido já concluído.', 'info');

                    } else {
                        _showInd(leitor.nome + ' · ' + (result.mensagem || 'Processado'), '#6b7280');
                    }
                } catch(e) {
                    _showInd(leitor.nome + ' · ❌ Erro de comunicação', '#ef4444');
                    showToast('Erro Scanner', 'Falha ao comunicar com servidor. Verifique conexão.', 'danger');
                    console.error('Scanner error:', e);
                }
            }

            document.addEventListener('keydown', function(e) {
                const tag = (document.activeElement?.tagName||'').toLowerCase();
                if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
                const now = Date.now();
                if (e.key === 'Enter') {
                    const code = _buf.trim().split(' ').join('');
                    _buf = ''; clearTimeout(_timer);
                    if (code.length >= MIN_LEN) _processScan(code);
                    return;
                }
                if (e.key.length === 1) {
                    if (_buf.length > 0 && (now - _lastAt) > 300) _buf = '';
                    _buf += e.key; _lastAt = now;
                    clearTimeout(_timer);
                    _timer = setTimeout(() => { _buf = ''; }, 500);
                }
            });
        })();

        /* ═══════════════════════════════════════════════════
           WEBSOCKET com reconexão
        ═══════════════════════════════════════════════════ */
        function _scheduleReconnect() {
            _wsConnected = false;
            _kpiReconnectDelay = Math.min(_kpiReconnectDelay * 1.5, 30000);
            _kpiReconnectTimer = setTimeout(_connectKpiWs, _kpiReconnectDelay);
        }

        function _connectKpiWs() {
            if (_kpiReconnectTimer) { clearTimeout(_kpiReconnectTimer); _kpiReconnectTimer = null; }
            if (wsKpi && wsKpi.readyState < 2) try { wsKpi.close(); } catch(e) {}
            const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
            try {
                wsKpi = new WebSocket(proto + '://' + window.location.host + '/ws/kpi-updates');
            } catch(e) { _scheduleReconnect(); return; }

            wsKpi.onopen = () => { _wsConnected = true; _kpiReconnectDelay = 2000; };
            wsKpi.onclose = () => _scheduleReconnect();
            wsKpi.onerror = () => {};

            wsKpi.onmessage = (e) => {
                let data;
                try { data = JSON.parse(e.data); } catch { return; }
                if (data.type !== 'full_update') return;

                updateAuthStatus(data.authenticated, data.auth_url);
                if (data.sales_stats) updateKpis(data.sales_stats);
                if (data.production_snapshot) {
                    const ps = data.production_snapshot;
                    const set = (id,v) => { const el=document.getElementById(id); if(el) el.textContent=v; };
                    set('kpi-waiting', ps.waiting||0);
                    set('kpi-inprod',  ps.in_production||0);
                    set('kpi-done',    ps.done||0);
                    set('waiting-count-badge', ps.waiting||0);
                    set('inprod-count-badge',  ps.in_production||0);
                    set('done-count-badge',    ps.done||0);
                }
                if (data.authenticated && !_wsFirstAuthDone) {
                    _wsFirstAuthDone = true;
                    _onAuthConfirmed();
                    fetch('/api/pending-orders/sync', {method:'POST'}).catch(()=>{});
                }
                if (data.cache_updated) showToast('Cache', 'Produtos atualizados.', 'info');
            };
        }

        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) {
                if (!wsKpi || wsKpi.readyState > 1) { _kpiReconnectDelay = 2000; _connectKpiWs(); }
                if (isAuthenticated && _boardInitialized) loadProductionBoard();
            }
        });

        /* ═══════════════════════════════════════════════════
           AUTH CONFIRMED
        ═══════════════════════════════════════════════════ */
        function _onAuthConfirmed() {
            _boardInitialized = true;
            loadProductionBoard();
            loadKPIChart();
            // Note: _boardPoll is managed by tab shown/hidden listeners
            // to avoid duplicate polling when switching tabs
        }

        /* ═══════════════════════════════════════════════════
           KPI UPDATE
        ═══════════════════════════════════════════════════ */
        function updateKpis(s) {
            const set = (id,v) => { const el=document.getElementById(id); if(el) el.textContent=v; };
            set('kpi-daily',    s.daily_count ?? s.daily   ?? 0);
            set('kpi-weekly',   s.weekly_count ?? s.weekly  ?? 0);
            set('kpi-historic', s.monthly_count ?? s.monthly ?? 0);
            const lu = document.getElementById('last-recalculated');
            if (lu) lu.textContent = '⏱ ' + formatDateTime(s.last_update || s.last_recalculated);
            const gr    = s.growth  || 0;
            const last7 = s.last_7  || 0;
            const ritmo = s.ritmo_7d || 0;
            let icon='📊 Estável', color='#6366f1';
            if (gr>10)      {icon='📈 Acelerando'; color='#10b981';}
            else if (gr>0)  {icon='📈 Subindo';    color='#10b981';}
            else if (gr<-10){icon='📉 Caindo';     color='#ef4444';}
            else if (gr<0)  {icon='📉 Abaixo';     color='#f59e0b';}
            set('trend-indicator', icon);
            set('growth-weekly', (gr>0?'+':'') + gr.toFixed(1) + '%');
            const tEl = document.getElementById('trend-indicator');
            const gEl = document.getElementById('growth-weekly');
            if (tEl) tEl.style.color = color;
            if (gEl) gEl.style.color = color;
            const ttEl = document.getElementById('growth-tooltip');
            if (ttEl && ritmo>0) ttEl.textContent = '7d: ' + last7 + ' · Ritmo: ' + ritmo.toFixed(1) + ' (mês÷20×7)';
            document.querySelectorAll('.kpi-card').forEach(c => {
                c.classList.add('updating');
                setTimeout(() => c.classList.remove('updating'), 600);
            });
        }

        function updateProductionKpis(bd) {
            const w = (bd.waiting||[]).length;
            const p = (bd.in_production||[]).length + (bd.orphan_timers||[]).length;
            const d = (bd.done||[]).length;
            const set = (id,v) => { const el=document.getElementById(id); if(el) el.textContent=v; };
            set('kpi-waiting', w); set('kpi-inprod', p); set('kpi-done', d);
            set('waiting-count-badge', w); set('inprod-count-badge', p); set('done-count-badge', d);
            const fmtNums = arr => arr.map(i=>'#'+(i.pedido_numero||i.order_id||'')).filter(Boolean).slice(0,8).join(' ');
            const dw = document.getElementById('kpi-waiting-nums');
            const di = document.getElementById('kpi-inprod-nums');
            const dd = document.getElementById('kpi-done-nums');
            if (dw) dw.textContent = fmtNums(bd.waiting||[]);
            if (di) di.textContent = fmtNums([...(bd.in_production||[]),...(bd.orphan_timers||[])]);
            if (dd) dd.textContent = fmtNums((bd.done||[]).slice(-6));
        }

        /* ═══════════════════════════════════════════════════
           PRODUCTION BOARD
        ═══════════════════════════════════════════════════ */
        async function loadProductionBoard() {
            try {
                const data = await fetch('/api/production/board').then(r=>r.json());
                _boardDataRaw = data; _boardData = data;
                renderProductionBoard(data);
                updateProductionKpis(data);
                _updateDashboardStagesChart(data);
            } catch(e) { console.error('Board error:', e); }
        }

        function renderProductionBoard(data) {
            _renderWaiting(data.waiting || []);
            _renderInProd([...(data.in_production||[]), ...(data.orphan_timers||[])]);
            _renderDone(data.done || []);
        }

        function _renderCurrentTab() {
            if (!_boardData) return;
            if (_currentBoardTab === 'waiting') _renderWaiting(_boardData.waiting||[]);
            else if (_currentBoardTab === 'inprod') {
                let items = [...(_boardData.in_production||[]),...(_boardData.orphan_timers||[])];
                if (_currentSetor === 'marcenaria') items = items.filter(i => (i.setor||i.status) === 'marcenaria');
                else if (_currentSetor === 'tapecaria') items = items.filter(i => (i.setor||i.status) === 'tapecaria');
                _renderInProd(items);
            }
            else _renderDone(_boardData.done||[]);
        }

        function switchBoardTab(tab) {
            _currentBoardTab = tab;
            ['waiting','inprod','done'].forEach(t => {
                const panel = document.getElementById('board-'+t);
                const btn   = document.getElementById('tab-'+t+'-btn');
                if (panel) panel.style.display = t===tab ? 'block' : 'none';
                if (btn)   btn.classList.toggle('active-board-tab', t===tab);
            });
            const sw = document.getElementById('setor-tabs-wrap');
            const si = document.getElementById('search-inprod-wrap');
            if (sw) sw.style.display = tab==='inprod' ? 'block' : 'none';
            if (si) si.style.display = tab==='inprod' ? 'block' : 'none';
            if (tab !== 'inprod') { const inp=document.getElementById('search-inprod'); if(inp) inp.value=''; }
            if (_boardData) _renderCurrentTab();
            // Re-render barcodes when switching to Produzindo (were hidden before)
            if (tab === 'inprod') {
                setTimeout(() => {
                    if (window._lastInProdItems) {
                        window._lastInProdItems.forEach(item => {
                            const op    = String(item.ordem_producao || item.pedido_numero || item.order_id || '');
                            const svgId = 'bci_' + (item.item_key||'').replace(/[^a-z0-9]/gi,'_');
                            const svgEl = document.getElementById(svgId);
                            if (svgEl && op && svgEl.children.length === 0) {
                                try { JsBarcode(svgEl, op, {format:'CODE128',width:2.4,height:65,displayValue:true,fontSize:13,margin:6,background:'#fff',lineColor:'#000'}); }
                                catch(e) {}
                            }
                        });
                    }
                }, 120);
            }
        }

        function switchSetor(s) {
            _currentSetor = s;
            ['todos','marc','tape'].forEach(id => {
                const b = document.getElementById('setor-'+id);
                if (b) b.className = 'btn btn-sm ' + (
                    (id==='todos'&&s==='todos')||(id==='marc'&&s==='marcenaria')||(id==='tape'&&s==='tapecaria')
                    ? 'btn-dark' : 'btn-outline-secondary');
            });
            _renderCurrentTab();
        }

        function filterInProd(q) {
            if (!_boardDataRaw) return;
            q = q.toLowerCase().trim();
            if (!q) { _boardData = _boardDataRaw; }
            else {
                _boardData = {..._boardDataRaw,
                    in_production: (_boardDataRaw.in_production||[]).filter(i =>
                        (i.pedido_numero||'').toLowerCase().includes(q) ||
                        (i.order_id||'').toLowerCase().includes(q) ||
                        (i.cliente||'').toLowerCase().includes(q) ||
                        (i.nome||'').toLowerCase().includes(q)
                    )
                };
            }
            _renderCurrentTab();
        }

        /* ── Waiting board ── */
        function _renderWaiting(items) {
            const div = document.getElementById('board-waiting');
            if (!div) return;
            if (!items.length) {
                div.innerHTML = '<div class="text-center py-5 text-muted"><div style="font-size:3rem;opacity:.3">📦</div><p class="mt-2">Nenhum pedido aguardando.</p></div>';
                return;
            }
            // Agrupa por pedido (order_id)
            const pedidos = {};
            items.forEach(item => {
                const pid = item.order_id || item.pedido_numero || item.order_id_bling || 'sem-pedido';
                if (!pedidos[pid]) pedidos[pid] = {id: pid, numero: item.pedido_numero||pid, cliente: item.cliente||'', itens: []};
                pedidos[pid].itens.push(item);
            });
            let html = '<div class="row g-3 p-2">';
            Object.values(pedidos).forEach(ped => {
                const urgencia = ped.itens.some(i => i.urgencia==='atrasado') ? 'atrasado' :
                                 ped.itens.some(i => i.urgencia==='critico')  ? 'critico'  : 'normal';
                const borderColor = urgencia==='atrasado' ? '#ef4444' : urgencia==='critico' ? '#f97316' : '#e5e5e5';
                html += '<div class="col-12 col-md-6 col-lg-4 fade-in-up"><div class="bc-card" style="border-color:' + borderColor + '">';
                html += '<div class="d-flex justify-content-between align-items-start mb-2">';
                html += '<span class="badge" style="background:#ffb600;color:#000">Pedido #' + escapeHtml(String(ped.numero)) + '</span>';
                if (ped.cliente) html += '<small class="text-muted" style="font-size:.7rem">' + escapeHtml(ped.cliente.slice(0,20)) + '</small>';
                html += '</div>';
                ped.itens.forEach((item, idx) => {
                    const ikey = item.item_key || '';
                    const nome = item.nome || item.nome_original || 'N/D';
                    const op   = item.ordem_producao || item.pedido_numero || ikey;
                    const isEsteira = _classifySetor(nome) === 'tapecaria';
                    const setor_lbl = isEsteira ? '🧵 Cadeira (3 etapas)' : '🪚 MDF (2 etapas)';
                    const prazo = safeDateStr(item.data_entrega);
                    const dias  = item.dias_restantes;
                    const svgId = 'bcw_' + ikey.replace(/[^a-z0-9]/gi,'_') + '_' + idx;

                    html += '<div style="border-top:1px solid #f0f0f0;padding-top:8px;margin-top:8px">';
                    html += '<div class="bc-nome">' + escapeHtml(nome) + '</div>';
                    html += '<div style="font-size:.65rem;color:#888;margin-bottom:4px">' + setor_lbl + '</div>';
                    if (dias !== null && dias !== undefined) {
                        const urgCls = dias<0 ? 'badge-atrasado' : dias<=2 ? 'badge-critico' : dias<=5 ? 'badge-atencao' : 'bg-success text-white';
                        html += '<span class="badge ' + urgCls + '" style="font-size:.62rem">' + (dias<0?'ATRASO '+Math.abs(dias)+'d':dias===0?'HOJE':dias+'d') + '</span> ';
                    }
                    html += '<span class="badge bg-light text-dark" style="font-size:.6rem">📅 ' + prazo + '</span>';
                    html += '<div class="text-center my-2"><svg id="' + svgId + '"></svg></div>';
                    html += '</div>';
                });
                html += '</div></div>';
            });
            html += '</div>';
            div.innerHTML = html;

            // Render barcodes
            items.forEach((item, idx) => {
                const ikey  = item.item_key || '';
                const op    = String(item.ordem_producao || item.pedido_numero || item.order_id || '');
                const svgId = 'bcw_' + ikey.replace(/[^a-z0-9]/gi,'_') + '_' + idx;
                const svgEl = document.getElementById(svgId);
                if (svgEl && op) {
                    try {
                        JsBarcode(svgEl, op, {format:'CODE128',width:1.8,height:50,
                            displayValue:true,fontSize:11,margin:4,background:'#fff',lineColor:'#000'});
                    } catch(e) { if(svgEl) svgEl.textContent = op; }
                }
            });
        }

        /* ── InProd board ── */
        function _renderInProd(items) {
            const div = document.getElementById('board-inprod');
            if (!div) return;
            if (!items.length) {
                div.innerHTML = '<div class="text-center py-5 text-muted"><div style="font-size:3rem;opacity:.3">⚙️</div><p class="mt-2">Nenhum item em produção.</p></div>';
                return;
            }
            let html = '<div class="row g-3 p-2">';
            items.forEach(item => {
                const ikey    = item.item_key || '';
                const nome    = item.nome || item.nome_original || 'N/D';
                const op      = item.ordem_producao || item.pedido_numero || item.order_id || '';
                const status  = item.status || 'in_production';
                const setor   = item.setor  || status;
                const isEst   = _classifySetor(nome) === 'tapecaria';
                const elapsed = item.tempo_decorrido || 0;
                const estado  = item.estado || 'paused';
                const safeId  = ('bci_' + ikey.replace(/[^a-z0-9]/gi,'_'));
                const dias    = item.dias_restantes;
                const prazo   = safeDateStr(item.data_entrega);

                // Stage badge
                const stageBadges = {
                    marcenaria:    '<span class="setor-badge" style="background:#f59e0b;color:#000">🪚 Marcenaria</span>',
                    tapecaria:     '<span class="setor-badge" style="background:#8b5cf6;color:#fff">🧵 Tapeçaria</span>',
                    in_production: '<span class="setor-badge" style="background:#10b981;color:#fff">⚙️ Em Produção</span>',
                };
                const stageBadge = stageBadges[setor] || stageBadges['in_production'];

                // Next action
                // Buttons removed — scan only
                
                
                

                // Urgência
                let urgBadge = '';
                if (dias !== null && dias !== undefined) {
                    if (dias<0)     urgBadge = '<span class="badge badge-atrasado" style="font-size:.62rem">⚠️ '+Math.abs(dias)+'d ATRASO</span> ';
                    else if(dias<=2)urgBadge = '<span class="badge badge-critico" style="font-size:.62rem">🔥 '+dias+'d</span> ';
                    else if(dias<=5)urgBadge = '<span class="badge badge-atencao" style="font-size:.62rem">⏰ '+dias+'d</span> ';
                }

                const cardClass = setor==='marcenaria' ? 'bc-card marcen' : setor==='tapecaria' ? 'bc-card tapec' : 'bc-card inprod';

                html += '<div class="col-sm-6 col-lg-4 col-xl-3 fade-in-up">';
                html += '<div class="' + cardClass + '">';
                html += stageBadge;
                if (urgBadge) html += '<div class="mb-1">' + urgBadge + '</div>';
                html += '<div class="bc-nome">' + escapeHtml(nome) + '</div>';
                html += '<div class="bc-num">#' + escapeHtml(String(op)) + '</div>';
                if (prazo !== '—') html += '<div style="font-size:.65rem;color:#888;margin-bottom:4px">📅 ' + prazo + '</div>';

                // BARCODE — renderizado imediatamente via SVG
                html += '<div class="bc-svg-wrap my-2 text-center"><svg id="' + safeId + '"></svg></div>';

                // Timer
                html += '<div class="text-center my-1">';
                html += '<span class="font-monospace fw-bold" id="btimer_' + safeId + '" style="font-size:1.3rem;color:#10b981">' + formatSeconds(elapsed) + '</span>';
                html += '<div><span class="badge ' + (estado==='running'?'bg-success':'bg-warning text-dark') + '" style="font-size:.65rem' + (estado==='running'?';animation:pulse-animation 1.5s infinite':'') + '">' + (estado==='running'?'🟢 RODANDO':'⏸ PAUSADO') + '</span></div>';
                html += '</div>';

                // Info: leitor instrução
                html += '<div class="text-center mt-2" style="font-size:.7rem;color:#888;font-style:italic">' + btnLabel + '</div>';

                if (item.cliente) html += '<div class="bc-meta">' + escapeHtml(item.cliente) + '</div>';
                html += '</div></div>';
            });
            html += '</div>';
            div.innerHTML = html;

            // Render barcodes — use 150ms delay to ensure DOM is visible
            // JsBarcode fails silently on display:none elements
            function _renderBcInProd() {
                items.forEach(item => {
                    const ikey  = item.item_key || '';
                    const op    = String(item.ordem_producao || item.pedido_numero || item.order_id || '');
                    if (!op) return;
                    const svgId = 'bci_' + ikey.replace(/[^a-z0-9]/gi,'_');
                    const svgEl = document.getElementById(svgId);
                    if (!svgEl) return;
                    // Skip if panel still hidden
                    const panel = document.getElementById('board-inprod');
                    if (panel && panel.style.display === 'none') return;
                    try {
                        JsBarcode(svgEl, op, {
                            format: 'CODE128', width: 2.4, height: 65,
                            displayValue: true, fontSize: 13, margin: 6,
                            background: '#fff', lineColor: '#000'
                        });
                    } catch(e) {
                        if (svgEl) svgEl.innerHTML = '<text x="4" y="20" font-family="monospace" font-size="13">' + op + '</text>';
                    }
                });
            }
            // First attempt after short delay
            setTimeout(_renderBcInProd, 100);
            // Second attempt after panel is definitely visible
            setTimeout(_renderBcInProd, 500);
            // Store for re-render on tab switch
            window._lastInProdItems = items;
        }

        /* ── Done board ── */
        function _renderDone(items) {
            const div = document.getElementById('board-done');
            if (!div) return;
            if (!items.length) {
                div.innerHTML = '<div class="text-center py-5 text-muted"><div style="font-size:3rem;opacity:.3">✅</div><p class="mt-2">Nenhum item concluído este mês.</p></div>';
                return;
            }
            let html = '<div class="table-responsive p-2"><table class="table table-hover table-sm align-middle mb-0">';
            html += '<thead><tr style="background:#f9f9f7"><th class="ps-3">Produto</th><th>Setor</th><th>#Pedido</th><th class="text-center">Tempo</th><th class="text-center">Concluído</th><th></th></tr></thead><tbody>';
            [...items].reverse().forEach(item => {
                const nome  = item.nome || item.nome_original || 'N/D';
                const op    = item.ordem_producao || item.pedido_numero || item.order_id || '—';
                const setor = (item.setor||'').replace('in_production','MDF').replace('marcenaria','Marc.').replace('tapecaria','Tapec.');
                const tp    = item.tempo_producao || 0;
                const finAt = safeDateTimeStr(item.finished_at);
                html += '<tr>';
                html += '<td class="ps-3 fw-bold" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escapeHtml(nome) + '</td>';
                html += '<td><span class="badge badge-done" style="font-size:.65rem">' + escapeHtml(setor||'MDF') + '</span></td>';
                html += '<td class="small text-muted">#' + escapeHtml(String(op)) + '</td>';
                html += '<td class="text-center fw-bold text-success font-monospace" style="font-size:.8rem">' + formatSeconds(tp) + '</td>';
                html += '<td class="text-center small text-muted">' + finAt + '</td>';
                html += '<td><button class="btn btn-outline-secondary btn-sm" style="font-size:.65rem;padding:2px 8px" onclick="printOP(\'' + escapeHtml(String(op)) + '\',\'' + escapeHtml(nome.replace(/'/g,'')) + '\',\'' + escapeHtml(item.item_key||'') + '\')">🖨️</button></td>';
                html += '</tr>';
            });
            html += '</tbody></table></div>';
            div.innerHTML = html;
        }

        /* ── Print OP ── */
        function printOP(op, nome, ikey) {
            if (ikey) {
                const w = window.open('/api/production/print-op/' + encodeURIComponent(ikey), '_blank', 'width=800,height=650,toolbar=yes');
                if (!w) showToast('Pop-up bloqueado', 'Permita pop-ups para imprimir.', 'warning');
            } else {
                showToast('Erro', 'item_key não disponível para impressão', 'danger');
            }
        }

        /* ── Sync ── */
        async function syncAndRefreshPending() {
            try {
                await fetch('/api/pending-orders/sync', {method:'POST'});
                await loadProductionBoard();
                showToast('Sincronizado', 'Pedidos atualizados do Bling.', 'success');
            } catch(e) { showToast('Erro', 'Falha ao sincronizar', 'danger'); }
        }

        /* ═══════════════════════════════════════════════════
           DASHBOARD CHARTS
        ═══════════════════════════════════════════════════ */
        let _dashFilter = {from:null, to:null};

        function applyDashboardFilter() {
            _dashFilter.from = document.getElementById('filter-date-from')?.value || null;
            _dashFilter.to   = document.getElementById('filter-date-to')?.value   || null;
            loadKPIChart();
        }
        function resetDashboardFilter() {
            _dashFilter = {from:null,to:null};
            const f=document.getElementById('filter-date-from'), t=document.getElementById('filter-date-to');
            if(f) f.value=''; if(t) t.value='';
            loadKPIChart();
        }

        async function loadKPIChart() {
            try {
                let url = '/api/sales/history';
                const ps = [];
                if (_dashFilter.from) ps.push('from='+_dashFilter.from);
                if (_dashFilter.to)   ps.push('to='+_dashFilter.to);
                if (ps.length) url += '?' + ps.join('&');
                const data = await fetchAPI(url);
                const ctx  = document.getElementById('salesChart')?.getContext('2d');
                if (!ctx) return;
                if (salesChart) salesChart.destroy();

                const bd = _boardDataRaw || {};
                const doneByDate = {};
                (bd.done||[]).forEach(d => { const ds=(d.finished_at||'').slice(0,10); if(ds) doneByDate[ds]=(doneByDate[ds]||0)+1; });
                const prodCounts = (data.labels||[]).map(l => doneByDate[l]||0);

                salesChart = new Chart(ctx, {
                    type:'line', data:{labels:data.labels||[],datasets:[
                        {label:'Pedidos',data:data.daily||[],borderColor:'#ffb600',backgroundColor:'rgba(255,182,0,.1)',tension:.4,fill:true,borderWidth:2,pointRadius:3},
                        {label:'Produzidos',data:prodCounts,borderColor:'#10b981',backgroundColor:'rgba(16,185,129,.08)',tension:.4,fill:true,borderWidth:2,pointRadius:3},
                        {label:'Média 7d',data:data.moving_avg||[],borderColor:'#6366f1',borderDash:[5,5],tension:.4,borderWidth:1.5,pointRadius:0},
                    ]},
                    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:'top'},tooltip:{mode:'index',intersect:false}},scales:{y:{beginAtZero:true,ticks:{precision:0}}}}
                });

                _buildProdBarChart(data.labels||[], data.daily||[], prodCounts);
                _buildDeltaChart(data.labels||[], data.daily||[]);

                const set=(id,v)=>{const el=document.getElementById(id);if(el)el.textContent=v;};
                set('growth-weekly', (data.growth>0?'+':'') + (data.growth||0).toFixed(1)+'%');
            } catch(e) { console.error('KPI chart error:', e); }
        }

        function _buildProdBarChart(labels, pedidos, producao) {
            const ctx = document.getElementById('prodBarChart')?.getContext('2d');
            if (!ctx) return;
            if (prodBarChart) prodBarChart.destroy();
            prodBarChart = new Chart(ctx, {
                type:'bar', data:{labels:labels.slice(-14),datasets:[
                    {label:'Pedidos',data:pedidos.slice(-14),backgroundColor:'rgba(255,182,0,.7)',borderRadius:4},
                    {label:'Produzidos',data:producao.slice(-14),backgroundColor:'rgba(16,185,129,.7)',borderRadius:4},
                ]},
                options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:'top'}},scales:{y:{beginAtZero:true,ticks:{precision:0}}}}
            });
        }

        function _buildDeltaChart(labels, counts) {
            const ctx = document.getElementById('deltaChart')?.getContext('2d');
            if (!ctx) return;
            if (deltaChart) deltaChart.destroy();
            const last14 = counts.slice(-14);
            const deltas = last14.map((v,i) => i===0?0:v-last14[i-1]);
            deltaChart = new Chart(ctx, {
                type:'bar', data:{labels:labels.slice(-14),datasets:[
                    {label:'Variação',data:deltas,backgroundColor:deltas.map(d=>d>=0?'rgba(16,185,129,.75)':'rgba(239,68,68,.75)'),borderRadius:4}
                ]},
                options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{ticks:{precision:0}}}}
            });
        }

        function _updateDashboardStagesChart(bd) {
            const ctx = document.getElementById('stagesChart')?.getContext('2d');
            if (!ctx) return;
            if (stagesChart) stagesChart.destroy();
            const w = (bd.waiting||[]).length;
            const p = (bd.in_production||[]).length + (bd.orphan_timers||[]).length;
            const d = (bd.done||[]).length;
            stagesChart = new Chart(ctx, {
                type:'doughnut', data:{
                    labels:['Em Espera','Produzindo','Concluídos'],
                    datasets:[{data:[w,p,d],backgroundColor:['#ffb600','#10b981','#6366f1'],borderWidth:2,borderColor:'#fff'}]
                },
                options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:'bottom'}}}
            });
        }

        /* ═══════════════════════════════════════════════════
           EXPEDIÇÃO
        ═══════════════════════════════════════════════════ */
        async function loadExpedicao(page) {
            _expedicaoPage = page || 1;
            const sec = document.getElementById('expedicao-section');
            if (!sec) return;
            sec.innerHTML = '<div class="text-center py-4 text-muted">⏳ Carregando...</div>';
            try {
                const data = await fetchAPI('/api/expedicao?page=' + _expedicaoPage + '&per_page=50&urgencia=' + _expedicaoFilter);
                _renderExpedicao(data.items||[], data.total||0, data.pages||1);
            } catch(e) {
                sec.innerHTML = '<div class="alert alert-danger m-3">Erro ao carregar expedição: ' + escapeHtml(e.message) + '</div>';
            }
        }

        function filterExpedicao(f) { _expedicaoFilter = f; loadExpedicao(1); }

        function _renderExpedicao(items, total, pages) {
            const sec = document.getElementById('expedicao-section');
            if (!sec) return;
            if (!items.length) {
                sec.innerHTML = '<div class="text-center py-5 text-muted"><div style="font-size:3rem;opacity:.3">🚚</div><p class="mt-2">Nenhum item.</p></div>';
                return;
            }
            const urgColors = {atrasado:'#ef4444',critico:'#f97316',atencao:'#f59e0b',normal:'#10b981'};
            let pag = '';
            if (pages > 1) {
                pag = '<div class="d-flex gap-1 align-items-center">';
                if (_expedicaoPage>1)    pag += '<button class="btn btn-sm btn-outline-secondary" onclick="loadExpedicao(' + (_expedicaoPage-1) + ')">‹</button>';
                pag += '<small class="text-muted px-2">' + _expedicaoPage + '/' + pages + '</small>';
                if (_expedicaoPage<pages) pag += '<button class="btn btn-sm btn-outline-secondary" onclick="loadExpedicao(' + (_expedicaoPage+1) + ')">›</button>';
                pag += '</div>';
            }
            let html = '<div class="d-flex justify-content-between align-items-center px-3 py-2 border-bottom"><small class="text-muted">' + total + ' item(s)</small>' + pag + '</div>';
            html += '<div class="table-responsive"><table class="table table-hover table-sm align-middle mb-0"><thead><tr style="background:#f9f9f7"><th class="ps-3">Produto</th><th>#Pedido</th><th>Cliente</th><th class="text-center">Prazo</th><th class="text-center">Dias</th><th class="text-center">Tempo Prod.</th><th class="text-center">Concluído</th></tr></thead><tbody>';
            items.forEach(item => {
                const urg  = item.urgencia || 'normal';
                const dias = item.dias_restantes;
                const rb   = urg==='atrasado'?'background:rgba(239,68,68,.07);':urg==='critico'?'background:rgba(249,115,22,.05);':'';
                const dc   = dias===null||dias===undefined ? '—' :
                    '<span class="badge" style="background:' + urgColors[urg] + ';color:#fff">' + (dias<0?'⚠️ '+Math.abs(dias)+'d':dias===0?'HOJE':dias+'d') + '</span>';
                const tp   = item.tempo_producao||0;
                const tpF  = tp>86400?(tp/86400).toFixed(2)+'d':tp>3600?Math.floor(tp/3600)+'h'+String(Math.floor((tp%3600)/60)).padStart(2,'0')+'m':tp>0?Math.floor(tp/60)+'m':'—';
                html += '<tr style="' + rb + '"><td class="ps-3 fw-bold" style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escapeHtml(item.nome||'N/D') + '</td>';
                html += '<td class="small">#' + escapeHtml(String(item.pedido_numero||'—')) + '</td>';
                html += '<td class="small text-muted" style="max-width:100px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escapeHtml(item.cliente||'—') + '</td>';
                html += '<td class="text-center small">' + safeDateStr(item.data_entrega) + '</td>';
                html += '<td class="text-center">' + dc + '</td>';
                html += '<td class="text-center fw-bold text-success">' + tpF + '</td>';
                html += '<td class="text-center small text-muted">' + safeDateTimeStr(item.finished_at) + '</td></tr>';
            });
            html += '</tbody></table></div>';
            sec.innerHTML = html;
        }

        /* ═══════════════════════════════════════════════════
           RELATÓRIO
        ═══════════════════════════════════════════════════ */
        async function loadRelatorio(dias) {
            const sec = document.getElementById('relatorio-section');
            if (!sec) return;
            sec.innerHTML = '<div class="text-center py-4 text-muted">⏳ Buscando dados...</div>';
            try {
                const data = await fetchAPI('/api/production/report?dias=' + dias);
                if (data.error) { sec.innerHTML = '<div class="alert alert-danger m-3">' + escapeHtml(data.error) + '</div>'; return; }
                const gc = data.crescimento>=0 ? '#10b981' : '#ef4444';
                const gs = data.crescimento>=0 ? '+' : '';
                sec.innerHTML = '<div class="row g-3 mb-4">' +
                    kpiBox('Pedidos Recebidos', data.total_recebidos, 'Últimos '+dias+' dias', '#ffb600') +
                    kpiBox('Produzidos', data.total_produzidos, 'Concluídos', '#10b981') +
                    kpiBox('Crescimento', gs+data.crescimento+'%', 'vs período anterior', gc) +
                    kpiBox('Tempo Médio', (data.avg_tempo_dias||'—')+'d', 'por pedido', '#6366f1') +
                    '</div>' +
                    '<div class="row g-3"><div class="col-lg-7"><div class="card p-3"><div style="height:200px"><canvas id="relatorio-chart"></canvas></div></div></div>' +
                    '<div class="col-lg-5"><div class="card p-3 h-100"><div class="fw-bold mb-2" style="font-size:.85rem">🏆 Top Produtos</div><div style="max-height:220px;overflow-y:auto">' +
                    (data.top_produtos||[]).map((p,i) => '<div class="d-flex justify-content-between align-items-center py-1 border-bottom"><span style="font-size:.75rem;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><span class="badge bg-light text-dark border me-1">' + (i+1) + '</span>' + escapeHtml(p.nome) + '</span><span class="badge bg-warning text-dark ms-2">' + p.qtd + ' un.</span></div>').join('') +
                    '</div></div></div></div>';
                setTimeout(() => {
                    const ctx = document.getElementById('relatorio-chart')?.getContext('2d');
                    if (!ctx) return;
                    new Chart(ctx, {type:'bar',data:{labels:data.labels||[],datasets:[{label:'Pedidos/dia',data:data.counts||[],backgroundColor:'rgba(255,182,0,.75)',borderRadius:4}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{beginAtZero:true,ticks:{precision:0}}}}});
                }, 80);
            } catch(e) { sec.innerHTML = '<div class="alert alert-danger m-3">Erro: ' + escapeHtml(e.message) + '</div>'; }
        }

        function kpiBox(label, val, sub, color) {
            return '<div class="col-6 col-md-3"><div class="kpi-card text-center" style="border-left:4px solid '+color+'"><h5>'+label+'</h5><div class="kpi-num" style="font-size:2.2rem;color:'+color+'">'+val+'</div><small class="text-muted">'+sub+'</small></div></div>';
        }

        /* ═══════════════════════════════════════════════════
           INSUMOS / FICHA
        ═══════════════════════════════════════════════════ */
        async function loadPurchaseGuide() {
            const sec = document.getElementById('purchase-guide-section');
            if (!sec) return;
            try {
                const bd = await fetch('/api/production/board').then(r=>r.json());
                const n  = (bd.waiting||[]).filter(i=>_classifySetor(i.nome||i.nome_original||'') === 'tapecaria').length;
                if (!n) { sec.innerHTML = '<div class="text-center py-4 text-muted">Nenhuma cadeira em espera.</div>'; return; }
                let html = '<div class="p-3"><div class="alert alert-info border-0 py-2 mb-3"><strong>' + n + ' cadeira(s)</strong> em espera</div>';
                html += '<div class="table-responsive"><table class="table table-sm mb-0"><thead><tr style="background:#f0f9ff"><th class="ps-3">Insumo</th><th class="text-center">Qtd/un</th><th class="text-center text-primary fw-bold">Total</th><th>Unidade</th></tr></thead><tbody>';
                RECIPE_CADEIRA.forEach(c => {
                    const tot = c.qtd * n;
                    html += '<tr><td class="ps-3">' + escapeHtml(c.nome) + '</td><td class="text-center text-muted">' + c.qtd + '</td><td class="text-center fw-bold text-primary">' + (tot%1===0?tot:tot.toFixed(2)) + '</td><td class="text-muted small">' + escapeHtml(c.un) + '</td></tr>';
                });
                html += '</tbody></table></div></div>';
                sec.innerHTML = html;
            } catch(e) { sec.innerHTML = '<div class="alert alert-danger m-3">Erro</div>'; }
        }

        async function loadFichaTecnica() {
            const sec = document.getElementById('ficha-section');
            if (!sec) return;
            let html = '<div class="table-responsive"><table class="table table-sm align-middle mb-0"><thead><tr style="background:#01010d;color:#fff"><th class="ps-3">#</th><th>Componente</th><th class="text-center">Qtd</th><th>Un.</th><th class="text-center">Para 10 un.</th></tr></thead><tbody>';
            RECIPE_CADEIRA.forEach((c,i) => {
                html += '<tr class="' + (i%2?'table-light':'') + '"><td class="ps-3 text-muted small">' + (i+1) + '</td><td class="fw-bold">' + escapeHtml(c.nome) + '</td><td class="text-center">' + c.qtd + '</td><td class="text-muted small">' + escapeHtml(c.un) + '</td><td class="text-center text-primary fw-bold">' + (c.qtd*10) + '</td></tr>';
            });
            html += '</tbody></table></div><div class="p-3 border-top"><small class="text-muted"><strong>' + RECIPE_CADEIRA.length + '</strong> componentes por unidade</small></div>';
            sec.innerHTML = html;
        }

        function refreshComponentTab() {
            loadPurchaseGuide();
            fetchAPI('/api/consumption/summary').then(d => { if (d && typeof renderConsumptionTable === 'function') renderConsumptionTable(d); }).catch(()=>{});
            fetchAPI('/api/components/usage').then(d => { if (d && d.history_production) renderProductionHistory(d.history_production); }).catch(()=>{});
        }

        function renderConsumptionTable(data) {
            const sec = document.getElementById('consumption-table-section');
            if (!sec || !data) return;
            const lbl = document.getElementById('consumption-month-label');
            if (lbl && data.month) lbl.textContent = data.month;
            if (!data.components || !Object.keys(data.components).length) { sec.innerHTML = '<div class="text-center py-4 text-muted">Sem consumo registrado.</div>'; return; }
            let html = '<div class="table-responsive"><table class="table table-sm mb-0"><thead><tr style="background:#f9f9f7"><th class="ps-3">Componente</th><th class="text-center">Total Usado</th><th>Un.</th></tr></thead><tbody>';
            Object.entries(data.components).forEach(([nome, info]) => {
                html += '<tr><td class="ps-3 fw-bold">' + escapeHtml(nome) + '</td><td class="text-center">' + (info.total||0) + '</td><td class="text-muted small">' + escapeHtml(info.unidade||'') + '</td></tr>';
            });
            html += '</tbody></table></div>';
            sec.innerHTML = html;
        }

        function renderProductionHistory(history) {
            const div = document.getElementById('production-history-section');
            if (!div) return;
            const rev = [...(history||[])].reverse();
            if (!rev.length) { div.innerHTML = '<div class="text-center py-4 text-muted">Nenhum produto finalizado.</div>'; return; }
            div.innerHTML = '<div class="table-responsive" style="max-height:320px;overflow-y:auto"><table class="table table-sm table-striped mb-0 align-middle"><thead class="table-dark sticky-top"><tr><th class="ps-3">Data</th><th>Produto</th><th class="text-center">Tempo</th><th>#Pedido</th></tr></thead><tbody>' +
                rev.map(h => '<tr><td class="ps-3 small text-muted">' + safeDateTimeStr(h.data_conclusao||h.finished_at) + '</td><td class="fw-bold" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escapeHtml(h.produto||h.nome||'N/D') + '</td><td class="text-center fw-bold text-success font-monospace">' + formatSeconds(h.tempo_segundos||0) + '</td><td class="small text-muted">' + (h.pedido_numero ? '#'+h.pedido_numero : '—') + '</td></tr>').join('') +
                '</tbody></table></div>';
        }

        /* ── Print helpers ── */
        function _print(htmlContent, title) {
            const a = document.getElementById('print-area');
            a.innerHTML = '<div style="padding:20px;font-family:Arial,sans-serif"><h2 style="text-align:center">SW Móveis MDF — ' + title + '</h2><p style="text-align:center;color:#666;font-size:12px">' + new Date().toLocaleString('pt-BR') + '</p>' + htmlContent + '</div>';
            a.style.display = 'block'; window.print();
            window.onafterprint = () => { a.innerHTML=''; a.style.display='none'; window.onafterprint=null; };
        }
        function printDashboard()  { _print(document.getElementById('tab-dashboard')?.innerHTML||'', 'Dashboard'); }
        function printExpedicao()  { _print(document.getElementById('expedicao-section')?.innerHTML||'', 'Expedição'); }
        function printRelatorio()  { _print(document.getElementById('relatorio-section')?.innerHTML||'', 'Relatório'); }
        function printFicha()      { _print(document.getElementById('ficha-section')?.innerHTML||'', 'Ficha Técnica'); }
        function printSetor() {
            const s = _currentSetor==='marcenaria'?'Marcenaria':_currentSetor==='tapecaria'?'Tapeçaria':'Todos os Setores';
            _print(document.getElementById('board-inprod')?.innerHTML||'', 'Produção: '+s);
        }

        /* ═══════════════════════════════════════════════════
           DOM READY — TAB LISTENERS
        ═══════════════════════════════════════════════════ */
        document.addEventListener('DOMContentLoaded', () => {
            // Default board tab = Em Espera
            switchBoardTab('waiting');

            // HTTP fallback: check auth status immediately (don't wait for WS)
            // This ensures the page shows content even if WS is slow to connect
            (async () => {
                try {
                    const r = await fetch('/api/status');
                    if (r.ok) {
                        const d = await r.json();
                        updateAuthStatus(d.authenticated, d.auth_url);
                        // Update connect status message
                        const cs = document.getElementById('connect-status');
                        if (cs) cs.textContent = d.authenticated ? '✅ Autenticado' : '⚠️ Token expirado — clique em Autenticar';
                        // Update main auth link href
                        const aml = document.getElementById('auth-link-main');
                        if (aml && d.auth_url) aml.href = d.auth_url;

                        if (d.authenticated && !_wsFirstAuthDone) {
                            _wsFirstAuthDone = true;
                            _onAuthConfirmed();
                        }
                        // Update production KPI badges from HTTP
                        if (d.production) {
                            const set = (id,v) => { const el=document.getElementById(id); if(el) el.textContent=v; };
                            set('kpi-waiting', d.production.waiting||0);
                            set('kpi-inprod',  d.production.in_production||0);
                            set('kpi-done',    d.production.done||0);
                            set('waiting-count-badge', d.production.waiting||0);
                            set('inprod-count-badge',  d.production.in_production||0);
                            set('done-count-badge',    d.production.done||0);
                        }
                    }
                } catch(e) {
                    console.warn('HTTP status check failed:', e);
                    const cs = document.getElementById('connect-status');
                    if (cs) cs.textContent = '⚠️ Erro de conexão — tente recarregar a página';
                }
                // Always connect WS regardless of HTTP check result
                _connectKpiWs();
            })();

            document.querySelector('[data-bs-target="#tab-dashboard"]')?.addEventListener('shown.bs.tab', loadKPIChart);
            document.querySelector('[data-bs-target="#tab-producao"]')?.addEventListener('shown.bs.tab', () => {
                loadProductionBoard();
                if (!_boardPoll) _boardPoll = setInterval(loadProductionBoard, 10000);
            });
            document.querySelector('[data-bs-target="#tab-producao"]')?.addEventListener('hidden.bs.tab', () => {
                if (_boardPoll) { clearInterval(_boardPoll); _boardPoll = null; }
            });
            document.querySelector('[data-bs-target="#tab-insumos"]')?.addEventListener('shown.bs.tab', refreshComponentTab);
            document.querySelector('[data-bs-target="#tab-expedicao"]')?.addEventListener('shown.bs.tab', () => loadExpedicao(1));
            document.querySelector('[data-bs-target="#tab-relatorio"]')?.addEventListener('shown.bs.tab', () => loadRelatorio(30));
            document.querySelector('[data-bs-target="#tab-ficha"]')?.addEventListener('shown.bs.tab', loadFichaTecnica);

            // WS logs
            let _wsLogs;
            function _connectWsLogs() {
                const proto = location.protocol==='https:'?'wss':'ws';
                _wsLogs = new WebSocket(proto+'://'+location.host+'/ws/logs');
                _wsLogs.onmessage = e => {
                    try {
                        const d = JSON.parse(e.data);
                        const box = document.getElementById('logs-content');
                        if (!box || !d.logs) return;
                        d.logs.forEach(l => {
                            const div = document.createElement('div');
                            div.textContent = '['+l.timestamp+'] ['+l.level+'] '+l.message;
                            div.style.color = l.level==='ERROR'?'#ef4444':l.level==='WARNING'?'#f59e0b':'#d1d5db';
                            box.appendChild(div);
                        });
                        const entries = box.querySelectorAll('div');
                        if (entries.length > 200) for(let i=0;i<entries.length-200;i++) entries[i].remove();
                        box.scrollTop = box.scrollHeight;
                    } catch(e) {}
                };
                _wsLogs.onclose = () => setTimeout(_connectWsLogs, 4000);
                _wsLogs.onerror = () => _wsLogs.close();
            }
            _connectWsLogs();
        });
    </script>
</body>
</html>
"""

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
    
    # ✅ REGRA DE OURO: Define uma SECRET_KEY estável para persistência de sessão.
    # CRÍTICO: Sempre configure FLASK_SECRET_KEY como variável de ambiente em produção.
    _secret = os.environ.get('FLASK_SECRET_KEY')
    if not _secret:
        # Derive stable key from MongoDB URI (persistent across restarts)
        # This prevents OAuth state cookie invalidation
        import hashlib as _hl
        _seed = (os.environ.get('MONGODB_URI') or os.environ.get('MONGO_URI') or 'sw-moveis-mdf-fallback-2026')
        _secret = _hl.sha256(_seed.encode()).hexdigest()
        logger.warning(
            "⚠️  FLASK_SECRET_KEY não configurada! Usando chave estável derivada do MongoDB URI. "
            "Configure FLASK_SECRET_KEY no Render para máxima segurança."
        )
    flask_app.config['SECRET_KEY'] = _secret
    
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
    _orchestrator = app.orchestrator  # atribuído em WebServer.__init__ via flask_app.orchestrator
    if not _orchestrator.is_running():
        _orchestrator.start_worker()
        start_cleanup_timer()
        logger.info("✅ Worker de fundo iniciado em modo local.")

    logger.info("Iniciando servidor Flask em modo local...")
    app.run(host='0.0.0.0', port=5000, debug=False)