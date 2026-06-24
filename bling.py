#!/usr/bin/env python3

# ============================================================================
# GEVENT MONKEY PATCH — DEVE SER A PRIMEIRA COISA A EXECUTAR
# Gunicorn 25.1.0 criou um control server baseado em asyncio.
# Com worker gevent, asyncio.get_event_loop() falha: "no running event loop".
# Solução: monkey_patch antes de tudo + forçar criação do event loop asyncio.
# ============================================================================
try:
    from gevent import monkey as _gm
    # Gunicorn com worker_class='gevent' já executa monkey.patch_all() internamente
    # ao subir o worker, ANTES de importar a aplicação. Chamar de novo aqui causa
    # o "MonkeyPatchWarning: Patching more than once" e pode deixar o estado de
    # threading inconsistente — contribuindo para os KeyError nas greenlets do
    # pymongo (pymongo_server_rtt_thread / pymongo_server_monitor_thread).
    if not _gm.is_module_patched('threading'):
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
        _mongo_client = MongoClient(
            _MONGO_URI,
            serverSelectionTimeoutMS=5000,
            connect=False,        # ← conexão lazy: evita threads nativas de monitor
                                   #   sendo criadas antes do gevent.monkey.patch_all()
                                   #   completar, que causava KeyError nas greenlets
                                   #   pymongo_server_rtt_thread/pymongo_server_monitor_thread
        )
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
        except Exception as e:
            logger.debug(f"MongoStore.get('{collection}','{doc_id}') falhou: {e}")
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
# No Render o /tmp não persiste entre deploys — usa diretório local como fallback
_default_data_dir = os.environ.get('DATA_DIR', '.')
DATA_DIR = Path(_default_data_dir)
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Testa escrita
    _test = DATA_DIR / '.write_test'
    _test.write_text('ok')
    _test.unlink()
except Exception:
    DATA_DIR = Path('.')

# ============================================================================ 
# 0. RATE LIMITER GLOBAL (NÍVEL PRODUÇÃO)
# ============================================================================

class RateLimiter:
    """Limitador de taxa centralizado para evitar 429 da API Bling.
    
    Garante intervalo mínimo entre requisições, thread-safe.
    Taxa segura por worker: ~1.2 req/s (min_interval=0.85s).
    Com 2 workers Gunicorn ativos, a taxa agregada fica ~2.4 req/s,
    dentro do limite do Bling (que costuma ser ~3 req/s).
    """
    def __init__(self, min_interval=0.85):
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
LOGS_DIR = DATA_DIR / 'logs'
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
    BLING_API_URL: str = 'https://api.bling.com.br/Api/v3'
    TOKEN_URL: str = 'https://api.bling.com.br/Api/v3/oauth/token'
    
    # Retry e Timeout
    REQUEST_TIMEOUT: int = 30
    AUTH_TIMEOUT: int = 20  # Timeout para auth (aumentado para cold start no Render)
    MAX_RETRIES: int = 3
    BASE_DELAY: float = 1.0
    
    # Rate Limiting (Configurável) - OTIMIZADO
    MAX_PAGES_PER_BATCH: int = 5  # Pode aumentar um pouco se quiser
    DELAY_BETWEEN_PAGES: float = 0.8  # Reduzido de 5.0 para 0.8 (mais rápido)
    DELAY_BETWEEN_BATCHES: float = 5.0  # Reduzido de 15.0 para 5.0
    
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
    # MongoDB primeiro — com retry curto, pois com connect=False (lazy) a
    # primeira operação Mongo do processo pode coincidir com o estabelecimento
    # da conexão TCP/DNS e falhar por timing, não por ausência real do dado.
    # Sem o retry, isso fazia o sistema reportar "nenhum token encontrado"
    # mesmo com um token válido salvo, forçando reautenticação manual
    # desnecessária a cada novo boot.
    if MONGO_AVAILABLE:
        for attempt in range(3):
            try:
                data = MongoStore.get('auth_tokens', 'tokens')
                if data:
                    return data
                break  # MongoDB respondeu, mas não há token salvo — não é erro
            except Exception as e:
                if attempt < 2:
                    logger.debug(f"load_tokens_safe: tentativa {attempt+1}/3 falhou ({e}), retentando em 0.3s...")
                    time.sleep(0.3)
                else:
                    logger.warning(f"load_tokens_safe: falha ao ler tokens do MongoDB após 3 tentativas: {e}")
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
        self.rate_limiter = RateLimiter(min_interval=0.85)
        
        # Configuração de Sessão com Retry Automático
        self.session = requests.Session()
        
        # Estratégia de Retry: Tenta 3 vezes em caso de falha de conexão, reset ou 50x
        retry_strategy = Retry(
            total=3,
            backoff_factor=2,  # Espera 2s, 4s, 8s — mais folga para 429 do Bling
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST", "PUT", "DELETE"],
            raise_on_status=False,
            respect_retry_after_header=True,  # Honra header Retry-After do Bling se presente
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
            
        # Garante header de auth atualizado + JWT obrigatório (Bling descontinuou token opaco)
        kwargs.setdefault('headers', {})
        kwargs['headers']['Authorization'] = f'Bearer {token}'
        kwargs['headers']['enable-jwt']    = '1'
        
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
                    kwargs['headers']['enable-jwt']    = '1'
                    # Tenta novamente (apenas 1 vez para evitar loop infinito)
                    response = self.session.request(method, url, timeout=45, **kwargs)
                else:
                    return None

            if response.status_code == 403:
                # Antes de desistir: recarrega tokens do MongoDB (outro worker pode ter salvo um novo)
                self.logger.warning(f"⚠️ 403 em '{endpoint}' — recarregando tokens do storage e tentando novamente...")
                if self.auth.reload_tokens_from_disk():
                    fresh_token = self.auth._access_token
                    if fresh_token and fresh_token != token:
                        self.logger.info(f"🔄 Token renovado do storage — retentando '{endpoint}'")
                        kwargs['headers']['Authorization'] = f'Bearer {fresh_token}'
                        kwargs['headers']['enable-jwt']    = '1'
                        try:
                            response = self.session.request(method, url, timeout=45, **kwargs)
                            self.logger.info(f"♻️  Retry '{endpoint}' → status {response.status_code}")
                            if response.status_code not in (403, 401):
                                try:
                                    retry_data = response.json()
                                    if isinstance(retry_data, dict) and isinstance(retry_data.get('error'), dict):
                                        err = retry_data['error']
                                        self.logger.error(
                                            f"⛔ Bling retornou erro estruturado no retry '{endpoint}': "
                                            f"{err.get('type','?')} — {err.get('message','')}"
                                        )
                                        return None
                                    return retry_data
                                except Exception:
                                    return {}
                        except Exception as _re:
                            self.logger.error(f"Retry pós-403 falhou em '{endpoint}': {_re}")
                self.logger.error(
                    f"⛔ HTTP 403 definitivo em '{endpoint}'. "
                    f"Body Bling: {response.text[:300]!r}. "
                    "Token inválido mesmo após recarregar. "
                    "Acesse /admin/reset-tokens e reautentique."
                )
                return None

            if response.status_code == 429:
                self.logger.warning(f"Rate limit (429) em {endpoint}. urllib3 já retentará automaticamente.")
                # Não levanta exceção aqui — o Retry adapter já tratou via status_forcelist

            response.raise_for_status()
            
            try:
                data = response.json()
            except json.JSONDecodeError:
                return {}

            # ── Detecta erro estruturado do Bling mesmo com HTTP 200 ──────────
            # Alguns endpoints da API v3 retornam status 200 com corpo
            # {"error":{"type":"FORBIDDEN","message":"Não permitido.",...}}
            # em vez de um 403 real — isso passava direto por raise_for_status()
            # e era retornado como se fosse dado válido, vazando o erro cru
            # para qualquer código que chamasse self.api.get(...).
            if isinstance(data, dict) and isinstance(data.get('error'), dict):
                err = data['error']
                err_type = err.get('type', 'UNKNOWN')
                err_msg  = err.get('message') or err.get('description') or 'Erro não especificado'
                self.logger.error(
                    f"⛔ Bling retornou erro estruturado em '{endpoint}' (HTTP {response.status_code}): "
                    f"{err_type} — {err_msg}"
                )
                if err_type == 'FORBIDDEN':
                    self.logger.error(
                        f"   → Causa provável: o token tem acesso geral à API, mas falta escopo "
                        f"específico para '{endpoint}'. Verifique os escopos marcados no painel "
                        f"Bling em Configurações > API > Aplicativos, e refaça o OAuth se ajustar."
                    )
                return None

            return data

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
            status = e.response.status_code if e.response is not None else '?'
            if status == 404:
                self.logger.debug(f"404 em {endpoint} — recurso não encontrado.")
            else:
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
    
    OAUTH_STATE_FILE: Path = DATA_DIR / 'oauth_state.json'

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
        """Retorna o token de acesso, renovando se necessário.
        
        Re-sincroniza do MongoDB a cada 5 minutos para garantir que múltiplos
        workers Gunicorn compartilhem sempre o mesmo token fresco.
        """
        now = time.time()

        # Re-sincroniza do storage a cada 5 minutos (evita divergência entre workers)
        last_sync = getattr(self, '_last_storage_sync', 0)
        if now - last_sync > 300:
            self._last_storage_sync = now
            disk = self._load_tokens()
            disk_access  = disk.get('access_token')
            disk_expires = disk.get('expires_at', 0)
            # Só atualiza memória se o token do storage for diferente e mais fresco
            if disk_access and disk_access != self._access_token:
                self._access_token  = disk_access
                self._refresh_token = disk.get('refresh_token', self._refresh_token)
                self._expires_at    = disk_expires

        if self._access_token and self._expires_at > now + 60:
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
        
        return f"https://api.bling.com.br/Api/v3/oauth/authorize?{urlencode(params)}"
    
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
        client_id_preview = (self.config.CLIENT_ID or '')[:8] + '...' if self.config.CLIENT_ID else '❌ VAZIO'
        secret_ok         = '✅ presente' if self.config.CLIENT_ID and self.config.CLIENT_SECRET else '❌ VAZIO'
        redirect_uri      = self.config.REDIRECT_URI or '❌ VAZIO'
        self.logger.debug(
            f"🔐 TOKEN REQUEST | grant_type={grant_type} | "
            f"client_id={client_id_preview} | secret={secret_ok} | "
            f"redirect_uri={redirect_uri} | url={self.config.TOKEN_URL} | "
            f"extra_keys={list(kwargs.keys())}"
        )

        auth_header = base64.b64encode(
            f"{self.config.CLIENT_ID}:{self.config.CLIENT_SECRET}".encode()
        ).decode()
        
        headers = {
            'Authorization': f'Basic {auth_header}',
            'Content-Type': 'application/x-www-form-urlencoded',
            'enable-jwt': '1',   # ← OBRIGATÓRIO: Bling descontinuou token opaco
        }
        
        data = {
            'grant_type': grant_type,
            **kwargs
        }
        
        response = None
        try:
            response = requests.post(
                self.config.TOKEN_URL,
                headers=headers,
                data=data,
                timeout=self.config.AUTH_TIMEOUT
            )

            self.logger.debug(
                f"🔐 BLING RESPONSE | status={response.status_code} | "
                f"body={response.text[:600]!r}"
            )

            response.raise_for_status()
            
            token_data = response.json()
            
            self._access_token  = token_data.get('access_token')
            self._refresh_token = token_data.get('refresh_token', self._refresh_token)
            expires_in          = token_data.get('expires_in', 3600)
            self._expires_at    = time.time() + expires_in

            self.logger.info(
                f"✅ TOKEN OK ({grant_type}) | access_token={str(self._access_token or '')[:12]}... | "
                f"expires_in={expires_in}s | "
                f"refresh={'presente' if self._refresh_token else 'ausente'}"
            )
            
            self._save_tokens()
            return True
            
        except requests.exceptions.HTTPError as e:
            body = response.text[:600] if response is not None else 'sem resposta'
            status = response.status_code if response is not None else '?'
            self.logger.critical(
                f"❌ TOKEN HTTP ERROR | status={status} | "
                f"grant_type={grant_type} | body={body!r}"
            )
        except RequestException as e:
            self.logger.critical(
                f"❌ TOKEN CONNECTION ERROR | grant_type={grant_type} | erro={e}"
            )
        except Exception as e:
            self.logger.critical(
                f"❌ TOKEN UNEXPECTED ERROR | grant_type={grant_type} | erro={e}"
            )
            
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
        inicio_semana = hoje - timedelta(days=6)   # últimos 7 dias corridos (não semana calendar)
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

        # Crescimento: últimos 7 dias vs média dos últimos 30 dias
        # Usa janela de 30 dias do gráfico (sempre disponível, independente do mês)
        # em vez de dividir por "20 dias úteis" — que distorce no início do mês
        last_7    = sum(counts[-7:])
        last_30   = sum(counts)
        dias_30_com_ped = max(sum(1 for c in counts if c > 0), 1)
        ritmo_7d_esperado = (last_30 / dias_30_com_ped) * 7  # média diária × 7
        growth = round(((last_7 - ritmo_7d_esperado) / ritmo_7d_esperado * 100), 1) if ritmo_7d_esperado > 0.5 else 0
        monthly_total = len(monthly_orders)

        # Média diária de produção (pedidos/dia nos últimos 30d com pedidos)
        dias_com_pedidos = sum(1 for c in counts if c > 0)
        avg_daily = round(sum(counts) / max(dias_com_pedidos, 1), 1)

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

            self.stats_history = {
                'dates':        [d.isoformat() for d in dates],
                'daily':        counts,
                'moving_avg':   [round(v, 2) for v in moving_avg],
                'growth':       growth,
                'avg_daily':    avg_daily,
                'last_7':       last_7,
                'ritmo_7d':     round(ritmo_7d_esperado, 1),
                'monthly_total': monthly_total,
                'weekly_count': len(weekly_orders),
                'daily_count':  len(daily_orders),
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
        """Salva timers — MongoDB E arquivo local (dupla redundância)."""
        if MONGO_AVAILABLE:
            try:
                MongoStore.set('production_timers', {'timers': self.timers}, 'timers', replace=True)
            except Exception as e:
                logger.error(f"Erro ao salvar timers no MongoDB: {e}")
        # Salva em arquivo APENAS como fallback quando MongoDB não disponível
        if not MONGO_AVAILABLE:
            temp_file = self.FILE_PATH.with_suffix('.tmp')
            try:
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(self.timers, f, indent=4, ensure_ascii=False)
                shutil.move(str(temp_file), str(self.FILE_PATH))
            except Exception as e:
                logger.warning(f"Fallback arquivo timers falhou: {e}")

    def _auto_pause_on_restart(self):
        """
        Ao reiniciar: soma o tempo que estava rodando desde o último checkpoint
        e retoma automaticamente (start_ts = agora).
        Assim o timer continua contando sem interrupção visível para o usuário.

        Nota: a validação contra pending_orders (remoção de timers órfãos)
        roda separadamente em purge_orphan_timers(), chamado após a
        instanciação global de pending_orders (ver final do arquivo).
        """
        changed = False
        now = time.time()
        for k, v in self.timers.items():
            if v.get('state') == 'running':
                start_ts = v.get('start_ts', 0)
                if start_ts and start_ts > 0:
                    # Soma o tempo decorrido desde o último checkpoint
                    v['accumulated'] = v.get('accumulated', 0) + (now - start_ts)
                # Retoma imediatamente — timer continua rodando
                v['start_ts'] = now
                v['state'] = 'running'
                changed = True
                logger.info(f"▶️ Restart: timer '{k}' retomado automaticamente ({int(v['accumulated'])}s acumulados).")
        if changed:
            self._save()

    def purge_orphan_timers(self, valid_timer_keys: set):
        """
        Remove timers cujo item correspondente não está mais 'in_production'
        em pending_orders. Chamado após pending_orders ser instanciado/carregado.

        valid_timer_keys: conjunto de timer_key de itens com status='in_production'.
        """
        orphans = [k for k in self.timers if k not in valid_timer_keys]
        if not orphans:
            return 0
        for k in orphans:
            elapsed = self.timers[k].get('accumulated', 0)
            del self.timers[k]
            logger.info(f"🗑️  Timer órfão removido: '{k}' ({int(elapsed)}s acumulados, item não está mais em produção).")
        self._save()
        logger.info(f"🧹 {len(orphans)} timer(s) órfão(s) removido(s).")
        return len(orphans)

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
        if timer_existed and _is_cadeira(nome_real):
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
            if not MONGO_AVAILABLE:
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
        # Arquivo apenas como fallback sem MongoDB
        if not MONGO_AVAILABLE:
            temp = self.FILE_PATH.with_suffix('.tmp')
            try:
                with open(temp, 'w', encoding='utf-8') as f:
                    json.dump(self.data, f, indent=4, ensure_ascii=False)
                shutil.move(str(temp), str(self.FILE_PATH))
            except Exception as e:
                logger.warning(f"Fallback arquivo consumo falhou: {e}")

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

def _is_cadeira(nome: str) -> bool:
    """Detecta se um produto é cadeira/poltrona/estofado (requer receita de 3 leituras)."""
    n = (nome or '').upper()
    return any(k in n for k in ('CADEIRA', 'POLTRONA', 'ESTOFADO', 'SOFÁ', 'SOFA', 'PUFF'))

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

def make_scan_code(item_key: str) -> str:
    """
    Gera um código curto e estável (8 hex chars) a partir do item_key completo.

    Por que não usar item_key diretamente como barcode?
    O item_key tem formato '{order_id}_{sku_safe}_{unit}' — pode ter 30-45+
    caracteres com underscores e letras maiúsculas. Em CODE128 isso gera um
    SVG largo demais para etiquetas de 62mm, as barras ficam finas demais e
    scanners baratos não conseguem ler.

    8 caracteres hex (ex: "A3F2C891") em CODE128:
    - Gera ~80px de largura com barras confortáveis (width≥2)
    - Cabe perfeitamente numa etiqueta 62x40mm
    - Zero colisões observadas em até 100k chaves (sha256 truncado)
    - Determinístico: mesmo item_key → mesmo código, sem BD extra

    O item_key continua como chave interna do dicionário (sem migração).
    """
    return hashlib.sha256(item_key.encode('utf-8')).hexdigest()[:8].upper()


class PendingOrdersManager:
    """
    Gerencia pedidos do Bling que chegaram e estão aguardando produção.
    Persiste no MongoDB (principal) ou arquivo (fallback).
    """
    FILE_PATH = DATA_DIR / 'pending_orders.json'

    def __init__(self):
        self.data = self._load()
        self._restore_in_production_to_waiting()
        self._backfill_scan_codes()
        self._op_cache     = {}   # oid -> {numero_op, situacao, previsao}
        self._op_cache_ts  = 0.0  # timestamp do último refresh
        self._op_cache_lock = __import__('threading').Lock()

    def _backfill_scan_codes(self):
        """
        Itens carregados do MongoDB antes do campo 'scan_code' existir
        não têm esse campo — gera retroativamente.
        Prioridade: gtin (EAN real do Bling) > make_scan_code(item_key).
        """
        changed = False
        for key, item in self.data.items():
            if not item.get('scan_code'):
                gtin = item.get('gtin') or ''
                item['scan_code'] = gtin if gtin else make_scan_code(key)
                changed = True
        if changed:
            self._save()
            logger.info("🔧 scan_code retroativo gerado para itens legados.")

    def _restore_in_production_to_waiting(self):
        """
        Ao reiniciar: itens 'in_production' voltam para 'waiting' para que
        o usuário possa bipeá-los novamente para retomar a etapa.

        Preserva 'fsm_step' e as flags de scan (scan_iniciado, scan_tapecaria)
        para que a próxima leitura continue da etapa correta:
        - fsm_step='marcenaria' → próxima leitura avança para tapecaria
        - fsm_step='tapecaria'  → próxima leitura conclui o item
        - fsm_step='mdf'        → próxima leitura conclui o item
        """
        changed = False
        for key, item in self.data.items():
            if item.get('status') == 'in_production':
                item['status'] = 'waiting'
                item.pop('started_at', None)
                # NÃO remove fsm_step nem scan_iniciado/scan_tapecaria —
                # a próxima bipagem usa esses campos para saber em qual etapa
                # o item estava e avança corretamente
                changed = True
                step = item.get('fsm_step', '')
                step_label = {'marcenaria': 'em Marcenaria', 'tapecaria': 'em Tapeçaria',
                              'mdf': 'em Produção MDF'}.get(step, '')
                logger.info(f"♻️ Restart: '{item.get('nome','?')}' voltou para espera {step_label} — bipe para continuar.")
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
        if not MONGO_AVAILABLE:
            temp = self.FILE_PATH.with_suffix('.tmp')
            try:
                with open(temp, 'w', encoding='utf-8') as f:
                    json.dump(self.data, f, indent=4, ensure_ascii=False)
                shutil.move(str(temp), str(self.FILE_PATH))
            except Exception as e:
                logger.warning(f"Fallback arquivo pending_orders falhou: {e}")

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

    def finish_production(self, item_key: str, tempo_segundos: int = None):
        """Move item para status 'done' — persiste no MongoDB para não sumir ao reiniciar."""
        if item_key in self.data:
            self.data[item_key]['status'] = 'done'
            self.data[item_key]['finished_at'] = datetime.now().isoformat()
            self.data[item_key]['mes_conclusao'] = datetime.now().strftime('%Y-%m')
            if tempo_segundos is not None:
                self.data[item_key]['tempo_producao'] = tempo_segundos
            self._save_one(item_key)
        return self.data.get(item_key)

    def dismiss(self, item_key: str):
        """Remove item da fila — sincroniza MongoDB E arquivo."""
        if item_key in self.data:
            del self.data[item_key]
        if MONGO_AVAILABLE:
            try:
                _mongo_db['pending_orders'].delete_one({'_id': item_key})
            except Exception as e:
                logger.error(f"dismiss: erro MongoDB {item_key}: {e}")
        if not MONGO_AVAILABLE:
            temp = self.FILE_PATH.with_suffix('.tmp')
            try:
                with open(temp, 'w', encoding='utf-8') as f:
                    json.dump(self.data, f, indent=4, ensure_ascii=False)
                shutil.move(str(temp), str(self.FILE_PATH))
            except Exception as e:
                logger.warning(f"dismiss: fallback arquivo falhou: {e}")

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
        - Qualquer item sem pedido_numero E sem ordem_producao válidos é removido (dado fantasma)
        - Qualquer item 'in_production' ou 'waiting' com mais de 30 dias é removido
        """
        agora = datetime.now()
        mes_atual = f"{agora.year}-{agora.month:02d}"
        to_remove = []

        for key, item in self.data.items():
            status = item.get('status', 'waiting')

            # ── Regra 0: remove dados fantasma (sem identificador válido) ────
            has_id = (item.get('pedido_numero') or item.get('ordem_producao') or
                      item.get('order_id') or item.get('order_id_bling'))
            if not has_id:
                to_remove.append(key)
                logger.info(f"🗑️ Purge: item '{key}' sem identificador — removido (dado fantasma).")
                continue

            try:
                if status == 'done':
                    mes_ref = item.get('mes_conclusao', '')
                    if not mes_ref:
                        fin = item.get('finished_at', '')
                        mes_ref = fin[:7] if fin else item.get('added_at', '')[:7]
                    if mes_ref and mes_ref != mes_atual:
                        to_remove.append(key)
                else:
                    mes_ref = item.get('added_at', '')[:7]
                    # Regra extra: in_production/waiting sem added_at válido OU com > 30 dias → remove
                    if not mes_ref or mes_ref != mes_atual:
                        to_remove.append(key)
                        continue
                    # Hard limit 30 dias para itens não concluídos
                    try:
                        added_dt = datetime.fromisoformat(item.get('added_at', ''))
                        if (agora - added_dt).days >= 30:
                            to_remove.append(key)
                            logger.info(f"🗑️ Purge: '{key}' em status '{status}' há {(agora-added_dt).days}d — removido.")
                    except Exception:
                        pass
            except Exception:
                pass

        if to_remove:
            for key in set(to_remove):
                self.data.pop(key, None)
                if MONGO_AVAILABLE:
                    try:
                        _mongo_db['pending_orders'].delete_one({'_id': key})
                    except Exception:
                        pass
            self._save()
            logger.info(f"🗓️ Reset: {len(set(to_remove))} itens antigos/fantasma removidos da fila.")
        return len(set(to_remove))

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

                # GTIN/EAN do produto no Bling — este é o código de barras real
                # que o Bling imprime nas etiquetas físicas e que está cadastrado
                # no produto. Quando disponível, é usado como scan_code diretamente,
                # garantindo que bipe no scanner = mesma leitura que no sistema Bling.
                # Fallback: make_scan_code(sub_key) quando GTIN não está cadastrado.
                gtin_produto = (produto_cache or {}).get('gtin') or ''
                # GTIN também pode vir no próprio item do pedido (API v3 inclui em alguns casos)
                if not gtin_produto:
                    gtin_produto = (item.get('produto', {}) or {}).get('gtin') or ''

                # Extrai base/cor — tenta nome completo, fallback para nome original do item
                base, cor = _extract_base_cor(nome_produto)
                if not base and not cor:
                    base, cor = _extract_base_cor(nome_raw)

                cliente = ''
                contato = pedido.get('contato')
                if isinstance(contato, dict):
                    cliente = contato.get('nome', '') or contato.get('nomeFantasia', '')

                # Extrai data estimada de entrega — suporte a múltiplos campos da API Bling
                data_entrega_raw = (
                    pedido.get('dataEntrega') or
                    pedido.get('dataPrevista') or
                    pedido.get('dataSaida') or
                    item.get('dataEntrega') or
                    item.get('dataPrevista') or
                    ''
                )
                # Ordem de produção Bling (OP interna — NÃO mistura com número do pedido)
                ordem_producao = (
                    pedido.get('ordemProducao') or
                    item.get('ordemProducao') or
                    ''   # vazio é correto — não usar numero do pedido como OP
                )

                # Número do pedido Bling — usado como barcode principal
                numero_pedido = str(pedido.get('numero') or order_id)

                item_data = {
                    'nome': nome_produto,
                    'nome_original': nome_raw,
                    'sku': sku_raw,
                    'gtin': gtin_produto,   # EAN/GTIN do Bling — código de barras físico real
                    'base': base,
                    'cor': cor,
                    'imagem': imagem,
                    'pedido_data': pedido.get('data') or pedido.get('dataEmissao', ''),
                    'pedido_numero': numero_pedido,
                    'cliente': cliente,
                    'data_entrega': data_entrega_raw,
                    'ordem_producao': ordem_producao,
                    'order_id': order_id,
                    'order_id_bling': order_id,
                    'qtd_total': qtd,
                }

                for unit in range(qtd):
                    sku_safe = (sku_raw or nome_raw[:20]).replace(' ', '_').replace('/', '_')
                    sub_key = f"{order_id}_{sku_safe}_{unit}"
                    # Lookup O(1) usando sets pré-computados
                    already = (sub_key in existing_keys or
                                (str(order_id), sku_raw, unit) in existing_order_sku_idx)
                    if not already:
                        # scan_code: usa GTIN do Bling quando disponível (bate 100%
                        # com o código de barras físico do produto cadastrado no Bling).
                        # Fallback: hash curto do sub_key quando GTIN não cadastrado.
                        sc = gtin_produto if gtin_produto else make_scan_code(sub_key)
                        self.data[sub_key] = {
                            **item_data,
                            'qtd': 1,
                            'order_id': order_id,
                            'item_key': sub_key,
                            'scan_code': sc,
                            'qtd_unit_idx': unit,
                            'status': 'waiting',
                            'added_at': datetime.now().isoformat()
                        }
                        existing_keys.add(sub_key)
                        existing_order_sku_idx.add((str(order_id), sku_raw, unit))
                        self._save_one(sub_key)
                        added += 1
                        # ── Computa componentes automaticamente na hora da venda ──
                        # Não precisa de checklist — já registra ao entrar na fila
                        try:
                            nome_upper = nome_produto.upper()
                            if 'CADEIRA' in nome_upper:
                                # Evita duplicação: checa se já foi registrado para este sub_key
                                consumo_key = f"auto_{sub_key}"
                                _cc = globals().get('component_consumption')
                                if _cc is not None:
                                    existing_consumo = _cc.get_current_month().get('checklist_logs', [])
                                    already_computed = any(
                                        l.get('produto') == consumo_key for l in existing_consumo
                                    )
                                    if not already_computed:
                                        for comp in RECIPE_CADEIRA:
                                            _cc.register_component(
                                                comp['nome'], comp['qtd'], comp['un'], consumo_key
                                            )
                                        logger.info(f"✅ Componentes computados automaticamente para pedido {sub_key} ({nome_produto})")
                        except Exception as _ce:
                            logger.warning(f"Erro ao computar componentes automáticos: {_ce}")

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

# Remove timers órfãos — timers cujo item correspondente não está mais
# em 'in_production' (purgado pelo reset mensal, finalizado, ou nunca
# vinculado corretamente). Evita o problema de "37 produzindo do mês
# passado ainda armazenados" persistindo indefinidamente entre deploys.
try:
    _valid_timer_keys = {
        item['timer_key']
        for item in pending_orders.data.values()
        if item.get('status') == 'in_production' and item.get('timer_key')
    }
    _purged = production_timer.purge_orphan_timers(_valid_timer_keys)
    if _purged:
        logger.info(f"♻️ Início: {_purged} timer(s) órfão(s) removido(s).")
except Exception as _e:
    logger.warning(f"purge_orphan_timers falhou: {_e}")

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
                # Pedidos/KPIs primeiro — popula o board de produção rapidamente.
                # O cache de produtos (mais pesado, ~15-30 páginas) roda depois,
                # evitando que o frontend fique vazio por minutos no cold start.
                logger.info(f"🔄 Ciclo #{cycle_count}: atualizando pedidos/KPIs...")
                self.process_sales_orders()

                if cycle_count == 1 or cycle_count % 3 == 0:
                    logger.info(f"🔄 Ciclo #{cycle_count}: atualizando cache de produtos...")
                    self.process_products_cache()

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
                # Janela de 60 dias corridos (cobre o mês atual completo + mês
                # anterior para comparações) — em vez de cap fixo de 2000
                # registros, que cortava pedidos do início do mês em meses
                # de alto volume (2500+) e distorcia o cálculo de tendência.
                # Hard ceiling de 5000 como rede de segurança contra picos extremos.
                cutoff_date = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
                merged_window = [o for o in merged if o.get('data', '') >= cutoff_date]
                self.sales._sales_history = merged_window[-5000:]
                self.logger.info(f"📦 Histórico de pedidos: {len(valid_orders)} novos/atualizados, {len(self.sales._sales_history)} total em memória (janela 60d).")
                
                # 2. Recalcula as estatísticas
                self.sales.recalculate_from_orders(self.sales._sales_history)
                
                # 3. Sincroniza pedidos com fila de produção pendente
                try:
                    with self._cache_lock:
                        cache_flat = {**self._products_cache, **self._kits_cache}
                    # Tenta sync direto (funciona se itens vierem na listagem)
                    added = pending_orders.sync_from_orders(valid_orders, cache_flat)
                    # Se nenhum item foi adicionado e há pedidos, busca detalhes individuais
                    # em lote pequeno (síncrono — ~40 pedidos, ~34s, com checkpoint
                    # persistente para não repetir entre ciclos/workers)
                    if added == 0 and valid_orders:
                        self.logger.info("⚠️ Itens não vieram na listagem. Buscando lote individual...")
                        self._fetch_orders_with_items(valid_orders, cache_flat)
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
                    "gtin": p.get("gtin") or p.get("codigoBarras") or "",  # EAN/GTIN — código de barras real do produto
                    "estoqueAtual": saldo,
                    "imagem": img_url,
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
                            "gtin": v.get("gtin") or v.get("codigoBarras") or p.get("gtin") or "",
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

        Estratégia incremental:
        - Mantém um checkpoint persistente (MongoDB: collection 'sync_checkpoint',
          doc '_id'='fetched_order_ids') com os IDs já buscados individualmente —
          evita repetir 2500+ chamadas a cada ciclo e evita duplicação entre os
          2 workers Gunicorn (que têm memória `pending_orders.data` separada).
        - Processa em lotes pequenos por ciclo (BATCH_SIZE) para não travar
          o worker por 30+ minutos.
        """
        BATCH_SIZE = 40  # ~40 * 0.85s ≈ 34s por ciclo — seguro e visível no log

        # 1) Carrega checkpoint persistente do MongoDB (IDs já processados)
        fetched_ids: set = set()
        if MONGO_AVAILABLE:
            try:
                doc = _mongo_db['sync_checkpoint'].find_one({'_id': 'fetched_order_ids'})
                if doc and isinstance(doc.get('ids'), list):
                    fetched_ids = set(str(i) for i in doc['ids'])
            except Exception as e:
                self.logger.warning(f"Checkpoint: erro ao carregar — {e}")

        # 2) Também considera o que já está na memória local (qualquer status)
        local_ids = {str(v.get('order_id')) for v in pending_orders.data.values() if v.get('order_id')}
        already_have = fetched_ids | local_ids

        # 3) Filtra apenas pedidos do mês atual
        agora_fetch = datetime.now()
        orders_mes = []
        skipped_mes = 0
        for o in orders:
            data_str = o.get('data') or o.get('dataEmissao') or ''
            if data_str:
                dt = _parse_order_date(data_str)
                if dt:
                    if dt.month == agora_fetch.month and dt.year == agora_fetch.year:
                        orders_mes.append(o)
                    else:
                        skipped_mes += 1
                else:
                    orders_mes.append(o)  # data não parseável: inclui por segurança
            else:
                orders_mes.append(o)

        orders_to_fetch_all = [o for o in orders_mes if str(o.get('id', '')) not in already_have]

        # ── LOG DE DIAGNÓSTICO CRÍTICO ─────────────────────────────────────
        self.logger.info(
            f"📋 Sync individual — total recebido: {len(orders)} | "
            f"mês atual ({agora_fetch.month}/{agora_fetch.year}): {len(orders_mes)} "
            f"(descartados de outros meses: {skipped_mes}) | "
            f"já processados (checkpoint+memória): {len(already_have)} | "
            f"pendentes de busca: {len(orders_to_fetch_all)}"
        )

        if not orders_to_fetch_all:
            self.logger.info("✅ Todos os pedidos do mês já foram processados (checkpoint completo).")
            return

        # 4) Limita ao tamanho do lote — restante fica para o próximo ciclo
        orders_to_fetch = orders_to_fetch_all[:BATCH_SIZE]
        remaining = len(orders_to_fetch_all) - len(orders_to_fetch)

        self.logger.info(
            f"🔍 Buscando itens de {len(orders_to_fetch)} pedidos individualmente "
            f"(lote de {BATCH_SIZE}, restam {remaining} para próximos ciclos)..."
        )
        enriched = []
        newly_fetched_ids = []

        for pedido in orders_to_fetch:
            order_id = str(pedido.get('id', ''))
            if not order_id:
                continue
            try:
                resp = self.api.get(f'pedidos/vendas/{order_id}')
                if not resp:
                    self.logger.debug(f"  Pedido {order_id}: resposta vazia (404/403) — marcando como processado para não retentar")
                    newly_fetched_ids.append(order_id)
                    continue
                detail = resp.get('data', resp)
                merged = {
                    **pedido,
                    'itens': detail.get('itens', []),
                    'dataEntrega': detail.get('dataEntrega') or detail.get('dataPrevista') or pedido.get('dataEntrega', ''),
                    'dataPrevista': detail.get('dataPrevista') or pedido.get('dataPrevista', ''),
                    'ordemProducao': detail.get('ordemProducao') or pedido.get('ordemProducao', ''),
                }
                if merged['itens']:
                    enriched.append(merged)
                # Marca como processado independente de ter itens — evita re-tentar
                # pedidos vazios (ex: cancelados) infinitamente
                newly_fetched_ids.append(order_id)
                time.sleep(0.85)  # respeita rate limit (alinhado ao RateLimiter)
            except Exception as e:
                self.logger.error(f"Erro ao buscar pedido {order_id}: {e}")
                continue

        self.logger.info(
            f"📦 Lote concluído: {len(enriched)}/{len(orders_to_fetch)} pedidos com itens válidos."
        )

        if enriched:
            added = pending_orders.sync_from_orders(enriched, cache_flat)
            self.logger.info(f"✅ {added} novo(s) item(ns) adicionados à fila de espera.")
        else:
            self.logger.warning("⚠️ Nenhum item encontrado neste lote (todos sem 'itens' no detalhe).")

        # 5) Persiste checkpoint — soma os IDs deste lote ao histórico global
        if newly_fetched_ids and MONGO_AVAILABLE:
            try:
                _mongo_db['sync_checkpoint'].update_one(
                    {'_id': 'fetched_order_ids'},
                    {'$addToSet': {'ids': {'$each': newly_fetched_ids}},
                     '$set': {'updated_at': datetime.now().isoformat()}},
                    upsert=True
                )
                self.logger.info(f"💾 Checkpoint atualizado: +{len(newly_fetched_ids)} IDs.")
            except Exception as e:
                self.logger.warning(f"Checkpoint: erro ao salvar — {e}")

        # 6) Acorda o worker imediatamente se restam pedidos — processa próximo lote
        if remaining > 0:
            self.logger.info(f"⏭️  {remaining} pedidos restantes — agendando próximo lote.")
            try:
                self.wake_worker()
            except Exception:
                pass

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

        # 3.1 Adiciona snapshot de produção (contadores das 3 etapas)
        try:
            waiting_count  = len(pending_orders.get_waiting())
            inprod_count   = len(pending_orders.get_in_production())
            done_count     = len(pending_orders.get_done())
            payload["production_snapshot"] = {
                "waiting":       waiting_count,
                "in_production": inprod_count,
                "done":          done_count,
            }
        except Exception:
            pass

        # 3.2 Adiciona lista de produtos se o cache foi atualizado
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

    # Lock granular por item_key — evita que 2 bipagens simultâneas/duplicadas
    # do MESMO produto avancem 2 etapas FSM de uma vez só (race condition).
    # Não bloqueia bipagens de itens DIFERENTES entre si.
    _barcode_locks_guard = Lock()       # protege a criação/limpeza do dict abaixo
    _barcode_item_locks: Dict[str, Lock] = {}
    _barcode_last_scan: Dict[str, float] = {}  # item_key -> timestamp do último scan aceito
    BARCODE_DEBOUNCE_SECONDS = 2.0      # ignora a mesma leitura repetida em menos de 2s

    @classmethod
    def _get_item_lock(cls, item_key: str) -> Lock:
        """Retorna (criando se necessário) o Lock dedicado a este item_key."""
        with cls._barcode_locks_guard:
            if item_key not in cls._barcode_item_locks:
                cls._barcode_item_locks[item_key] = Lock()
            return cls._barcode_item_locks[item_key]

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

            logger.critical(
                f"\U0001f510 /auth INICIADO | "
                f"client_id={'\u2705 ' + (self.config.CLIENT_ID or '')[:8] + '...' if self.config.CLIENT_ID else '\u274c VAZIO'} | "
                f"redirect_uri={self.config.REDIRECT_URI!r} | "
                f"auth_url={auth_url[:120]!r}"
            )

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
        @self.app.route("/api/sales/orders-summary")
        @token_required
        def api_sales_orders_summary(token):
            """Retorna lista compacta dos pedidos reais do Bling (numero + data) para exibir nos KPI cards."""
            orders = self.orchestrator.sales._sales_history or []
            result = []
            for o in orders:
                data_str = o.get('data') or o.get('dataEmissao', '')
                if data_str:
                    result.append({
                        'id': o.get('id'),
                        'numero': o.get('numero') or o.get('id'),
                        'data': data_str,
                    })
            return jsonify({'orders': result[-500:]})  # máx 500 mais recentes

        # Novo Endpoint: Histórico de Vendas para Dashboard
        @self.app.route("/api/sales/history")
        @token_required
        def api_sales_history(token):
            # Suporta filtro por data via query params
            date_from = request.args.get('from', '')
            date_to   = request.args.get('to', '')
            stats = self.orchestrator.sales.stats_history
            if not stats or not stats.get('dates'):
                if not self.orchestrator.sales.daily_count:
                    Thread(target=self.orchestrator.process_sales_orders, daemon=True).start()
                return jsonify({"labels": [], "daily": [], "moving_avg": [], "growth": 0, "avg_daily": 0})
            labels = stats.get('dates', [])
            daily  = stats.get('daily', [])
            # Filtra por período se solicitado
            if date_from or date_to:
                filtered = [(l, d) for l, d in zip(labels, daily)
                            if (not date_from or l >= date_from) and (not date_to or l <= date_to)]
                labels = [x[0] for x in filtered]
                daily  = [x[1] for x in filtered]
                # Recalcula moving_avg (janela de 7 dias)
                moving_avg = []
                for i in range(len(daily)):
                    window = daily[max(0, i-6):i+1]
                    moving_avg.append(round(sum(window)/len(window), 1) if window else 0)
                total = sum(daily)
                n = max(len(daily), 1)
                lw = sum(daily[-7:]) if len(daily) >= 7 else total
                # Período anterior do mesmo tamanho
                all_daily  = stats.get('daily', [])
                all_labels = stats.get('dates', [])
                prev = [(l2, d2) for l2, d2 in zip(all_labels, all_daily)
                        if l2 < (labels[0] if labels else '')]
                prev_total = sum(x[1] for x in prev[-len(daily):]) if prev else 0
                growth     = round((total - prev_total) / prev_total * 100, 1) if prev_total else 0
                avg_daily  = round(total / n, 1)
            else:
                moving_avg = stats.get('moving_avg', [])
                growth     = stats.get('growth', 0)
                avg_daily  = stats.get('avg_daily', 0)
            return jsonify({
                "labels": labels, "daily": daily, "moving_avg": moving_avg,
                "growth": growth, "avg_daily": avg_daily
            })

        @self.app.route("/api/production/report")
        @token_required
        def api_production_report(token):
            """
            Relatório de produção direto do Bling.
            Parâmetros: dias=7|30 (padrão 30)
            Retorna: pedidos recebidos, produzidos, crescimento, tempo médio, top produtos.
            """
            try:
                _dias_raw = request.args.get('dias', '30')
                try:
                    dias = int(_dias_raw)
                except (ValueError, TypeError):
                    logger.warning(f"/api/report: parâmetro 'dias' inválido recebido: {_dias_raw!r}. Usando padrão 30.")
                    dias = 30
                hoje = datetime.now()
                data_ini = (hoje - timedelta(days=dias)).strftime('%Y-%m-%d')

                # Filtra history local (já baixado do Bling)
                all_orders = self.orchestrator.sales._sales_history or []
                pedidos_periodo = [
                    o for o in all_orders
                    if (o.get('data') or o.get('dataEmissao', ''))[:10] >= data_ini
                ]

                # Período anterior (para crescimento)
                data_ant = (hoje - timedelta(days=dias*2)).strftime('%Y-%m-%d')
                pedidos_anterior = [
                    o for o in all_orders
                    if data_ant <= (o.get('data') or o.get('dataEmissao', ''))[:10] < data_ini
                ]

                total_recebidos = len(pedidos_periodo)
                total_anterior  = len(pedidos_anterior)
                crescimento = round((total_recebidos - total_anterior) / total_anterior * 100, 1) if total_anterior else 0

                # Produzidos no período (do board local)
                done_items = [i for i in pending_orders.data.values()
                              if i.get('status') == 'done'
                              and (i.get('finished_at', '') or '')[:10] >= data_ini]
                total_produzidos = len(done_items)

                # Tempo médio de produção em dias
                tempos = [i.get('tempo_producao', 0) for i in done_items if i.get('tempo_producao')]
                avg_tempo_dias = round(sum(tempos) / len(tempos) / 86400, 2) if tempos else 0

                # Top 10 produtos mais pedidos no período
                from collections import Counter
                produto_counter = Counter()
                for o in pedidos_periodo:
                    for item in (o.get('itens') or []):
                        nome = item.get('descricao') or item.get('nome') or ''
                        if nome:
                            produto_counter[nome[:60]] += int(item.get('quantidade', 1))
                top_produtos = [{'nome': k, 'qtd': v} for k, v in produto_counter.most_common(10)]

                # Distribuição por dia
                from collections import defaultdict
                por_dia = defaultdict(int)
                for o in pedidos_periodo:
                    d = (o.get('data') or o.get('dataEmissao', ''))[:10]
                    if d: por_dia[d] += 1
                labels = sorted(por_dia.keys())
                counts = [por_dia[l] for l in labels]

                return jsonify({
                    'dias': dias,
                    'total_recebidos':  total_recebidos,
                    'total_anterior':   total_anterior,
                    'crescimento':      crescimento,
                    'total_produzidos': total_produzidos,
                    'avg_tempo_dias':   avg_tempo_dias,
                    'top_produtos':     top_produtos,
                    'labels':           labels,
                    'counts':           counts,
                })
            except Exception as e:
                logger.error(f"Erro no relatório de produção: {e}")
                return jsonify({'error': str(e)}), 500



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

            # Apenas leitura permitida — escrita vai via /api/barcode/scan
            if action != 'get':
                return jsonify({
                    'error': 'Controle manual do timer desativado. Use o leitor de código de barras.',
                    'use': '/api/barcode/scan'
                }), 403

            status = production_timer.get_status(produto)
            return jsonify(status)

        @self.app.route('/api/production/board')
        @token_required
        def api_production_board(token):
            """
            Retorna snapshot completo da aba de produção.
            - waiting: pedidos aguardando leitura do código de barras para iniciar
            - in_production: pedidos em andamento + tempo ao vivo do timer
            - done: concluídos do mês (para histórico)
            - timers_orphan: timers sem item_key
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
            hoje_ip = datetime.now().date()
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
                # Prazo
                data_entrega_str = enriched.get('data_entrega', '')
                dias_restantes = None
                urgencia = 'normal'
                if data_entrega_str:
                    dt_ent = _parse_order_date(data_entrega_str)
                    if dt_ent:
                        dias_restantes = (dt_ent.date() - hoje_ip).days
                        if dias_restantes < 0: urgencia = 'atrasado'
                        elif dias_restantes <= 2: urgencia = 'critico'
                        elif dias_restantes <= 5: urgencia = 'atencao'
                enriched['dias_restantes'] = dias_restantes
                enriched['urgencia'] = urgencia
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
            hoje = datetime.now().date()
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

                # ── Calcula prazo / urgência ──────────────────────────────
                data_entrega_str = enriched.get('data_entrega', '')
                dias_restantes = None
                urgencia = 'normal'  # normal | atencao | critico | atrasado
                if data_entrega_str:
                    dt_ent = _parse_order_date(data_entrega_str)
                    if dt_ent:
                        dias_restantes = (dt_ent.date() - hoje).days
                        if dias_restantes < 0:
                            urgencia = 'atrasado'
                        elif dias_restantes == 0:
                            urgencia = 'critico'
                        elif dias_restantes <= 2:
                            urgencia = 'critico'
                        elif dias_restantes <= 5:
                            urgencia = 'atencao'
                enriched['dias_restantes'] = dias_restantes
                enriched['urgencia'] = urgencia
                waiting_enriched.append(enriched)

            # ── Ordena: atrasados/críticos primeiro; agrupa por produto; dentro do grupo: urgência primeiro ──
            def _sort_key(item):
                urg = item.get('urgencia', 'normal')
                dias = item.get('dias_restantes')
                urg_order = {'atrasado': 0, 'critico': 1, 'atencao': 2, 'normal': 3}[urg]
                dias_val = dias if dias is not None else 9999
                nome_grp = (item.get('nome') or item.get('nome_original') or '').upper()
                return (nome_grp, urg_order, dias_val)

            waiting_enriched.sort(key=_sort_key)

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

            # ── Busca OPs do Bling em cache (5min TTL, thread-safe) ───────────
            # Circuit breaker: se 'ordens/producao' não está disponível (módulo
            # não habilitado na conta Bling — retorna FORBIDDEN), desativa por
            # 1h em vez de tentar até 25x a cada 5min. Evita lentidão e ruído
            # de erro quando o recurso simplesmente não existe para esta conta.
            with pending_orders._op_cache_lock:
                _op_cache    = pending_orders._op_cache
                _op_cache_ts = pending_orders._op_cache_ts
                needs_refresh = time.time() - _op_cache_ts > 300

            _ops_disabled_until = getattr(self.orchestrator, '_ops_endpoint_disabled_until', 0)
            _ops_circuit_open = time.time() < _ops_disabled_until

            if needs_refresh and not _ops_circuit_open:
                new_op_cache = dict(_op_cache)  # cópia para não bloquear leituras
                order_ids_to_fetch = set()
                for it in list(pending_orders.data.values()):
                    oid = (it.get('order_id_bling') or it.get('order_id') or
                           it.get('pedido_numero'))
                    if oid:
                        order_ids_to_fetch.add(str(oid))
                fetched = 0
                consecutive_failures = 0
                for oid in list(order_ids_to_fetch):
                    if fetched >= 25:   # máx 25 req por ciclo para não travar
                        break
                    if oid in new_op_cache:  # já cacheado
                        continue
                    try:
                        resp = self.orchestrator.api.get(
                            'ordens/producao',
                            params={'numeroPedidoVenda': oid}
                        )
                        if resp is None:
                            # api.get retorna None tanto para erro de rede quanto
                            # para erro estruturado FORBIDDEN — trata como falha
                            consecutive_failures += 1
                            if consecutive_failures >= 3:
                                # 3 falhas seguidas = endpoint indisponível para esta
                                # conta (módulo não habilitado). Abre o circuito.
                                self.orchestrator._ops_endpoint_disabled_until = time.time() + 3600
                                self.logger.warning(
                                    "⚠️ Endpoint 'ordens/producao' indisponível (FORBIDDEN/erro) — "
                                    "provavelmente o módulo Ordens de Produção não está habilitado "
                                    "nesta conta Bling. Desativado por 1h para evitar lentidão. "
                                    "OPs internas não aparecerão nos cards (apenas o número do "
                                    "pedido, que já é suficiente para o código de barras)."
                                )
                                break
                            continue
                        consecutive_failures = 0
                        if resp.get('data'):
                            ops = resp['data'] if isinstance(resp['data'], list) else [resp['data']]
                            for op in ops:
                                numero_op = str(op.get('numero') or op.get('id') or '')
                                previsao  = (op.get('dataPrevisao') or
                                             op.get('dataPrevista') or '')
                                situacao  = ''
                                sit = op.get('situacao')
                                if isinstance(sit, dict):
                                    situacao = sit.get('nome', '')
                                elif sit:
                                    situacao = str(sit)
                                if numero_op:
                                    new_op_cache[oid] = {
                                        'numero_op':    numero_op,
                                        'codigo_barras': numero_op,
                                        'situacao':     situacao,
                                        'previsao':     previsao,
                                    }
                                    fetched += 1
                                    break
                    except Exception:
                        consecutive_failures += 1
                        pass
                with pending_orders._op_cache_lock:
                    pending_orders._op_cache    = new_op_cache
                    pending_orders._op_cache_ts = time.time()
                _op_cache = new_op_cache

            def _inject_op(item):
                """Injeta dados da OP do Bling no item se disponível."""
                oid = str(item.get('order_id_bling') or item.get('order_id') or item.get('pedido_numero') or '')
                op_data = _op_cache.get(oid, {})
                if op_data.get('numero_op'):
                    item['ordem_producao'] = op_data['numero_op']
                    item['op_situacao']    = op_data.get('situacao', '')
                    if op_data.get('previsao') and not item.get('data_entrega'):
                        item['data_entrega'] = op_data['previsao']
                return item

            # Aplica enriquecimento de OP em todos os grupos
            waiting_enriched  = [_inject_op(i) for i in waiting_enriched]
            in_prod           = [_inject_op(i) for i in in_prod]
            done_enriched_out = [_inject_op(i) for i in done_enriched]

            return jsonify({
                'waiting': waiting_enriched,
                'in_production': in_prod,
                'orphan_timers': orphan,
                'done': done_enriched_out,
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

        @self.app.route('/api/barcode/scan', methods=['POST'])
        @token_required
        def api_barcode_scan(token):
            """
            Processa leitura de código de barras com FSM de etapas e identificação de leitor.

            Formato do campo 'codigo': pode vir com prefixo de leitor "R1:", "R2:", "R3:", "R4:"
            emitido pelo próprio scanner (programado para prefixar).  Ex: "R2:2781"
            Se não vier prefixo, o reader_id fica None e qualquer leitor é aceito.

            FSM — Cadeiras/Poltronas/Estofados (3 leituras):
              waiting         → [qualquer leitor] → in_production (Marcenaria iniciada)
              in_production   → [R2 ou R3] se fsm_step=='marcenaria' → tapecaria
              tapecaria       → [R3 ou R4] → done

            FSM — MDF / demais produtos (2 leituras):
              waiting         → [qualquer leitor] → in_production
              in_production   → [qualquer leitor] → done

            Anti-duplicação: flags scan_iniciado / scan_concluido / scan_tapecaria no item.
            """
            data = request.json or {}
            raw_codigo = str(data.get('codigo', '')).strip()
            if not raw_codigo:
                return jsonify({'error': 'codigo obrigatório'}), 400

            # ── Extrai reader_id do prefixo R1:/R2:/R3:/R4: ──────────────────
            reader_id = None
            codigo    = raw_codigo
            import re as _re_scan
            _pfx = _re_scan.match(r'^(R[1-4]):(.+)$', raw_codigo, _re_scan.IGNORECASE)
            if _pfx:
                reader_id = _pfx.group(1).upper()   # "R1", "R2", "R3" ou "R4"
                codigo    = _pfx.group(2).strip()

            # ── Busca pedido pelo número do pedido, ordem_producao, order_id ou OP ──
            found_key = None
            found_item = None

            # ── PRIORIDADE 1: match por scan_code OU gtin ────────────────────────
            # scan_code é o GTIN do Bling (quando cadastrado) ou um hash SHA256[:8].
            # Se o operador bipar o EAN físico do produto (mesmo que scan_code seja hash),
            # o match por gtin garante que funcione também.
            if not found_item:
                for key, item in pending_orders.data.items():
                    if item.get('status') == 'done':
                        continue
                    if item.get('scan_code') == codigo or item.get('gtin') == codigo:
                        found_key, found_item = key, item
                        break

            # ── PRIORIDADE 2: match exato por item_key (compatibilidade retroativa) ──
            # Etiquetas antigas impressas com item_key bruto (30-45 chars) ou
            # testes manuais diretos via item_key ainda funcionam.
            if not found_item and codigo in pending_orders.data:
                _item = pending_orders.data[codigo]
                if _item.get('status') != 'done':
                    found_key, found_item = codigo, _item
                else:
                    return jsonify({
                        'acao': 'ja_concluido',
                        'codigo': codigo,
                        'reader_id': reader_id,
                        'mensagem': f'Este item já foi concluído anteriormente.'
                    })

            # ── PRIORIDADE 3: match por pedido_numero/ordem_producao ──
            # Fallback para quando alguém bipa o código do próprio Bling
            # (número do pedido impresso na OP pelo ERP).
            if not found_item:
                for key, item in pending_orders.data.items():
                    if item.get('status') == 'done':
                        continue
                    pnum = str(item.get('pedido_numero', '') or item.get('order_id', '') or '')
                    op   = str(item.get('ordem_producao', '') or '')
                    oid  = str(item.get('order_id_bling') or item.get('order_id') or pnum or '')
                    op_cached = ''
                    with pending_orders._op_cache_lock:
                        op_cached = pending_orders._op_cache.get(oid, {}).get('numero_op', '')
                    if codigo in (pnum, op, op_cached):
                        found_key  = key
                        found_item = item
                        break

            if not found_item:
                return jsonify({
                    'acao': 'nao_encontrado',
                    'codigo': codigo,
                    'reader_id': reader_id,
                    'mensagem': f'Nenhum pedido ativo encontrado para #{codigo}'
                }), 404

            # ── LOCK + DEBOUNCE POR ITEM — evita avanço duplo de etapa FSM ──
            # Cenário real: scanner dispara 2 eventos Enter por bipagem (comum em
            # modelos baratos/gatilho com bounce), ou o operador aproxima o produto
            # 2x rapidamente. Sem isso, 2 requisições concorrentes podem ler o
            # MESMO status_atual antes de qualquer uma escrever, e ambas avançam
            # a etapa — fazendo a peça "pular" Marcenaria→Concluído numa única
            # bipagem física, sem nunca passar pela Tapeçaria.
            item_lock = WebServer._get_item_lock(found_key)
            if not item_lock.acquire(timeout=0.05):
                # Outra requisição para o MESMO item já está processando agora
                return jsonify({
                    'acao': 'processando',
                    'codigo': codigo,
                    'reader_id': reader_id,
                    'item_key': found_key,
                    'mensagem': 'Leitura já em processamento — aguarde.'
                }), 200

            try:
                now_ts = time.time()
                last_ts = WebServer._barcode_last_scan.get(found_key, 0)
                if (now_ts - last_ts) < WebServer.BARCODE_DEBOUNCE_SECONDS:
                    return jsonify({
                        'acao': 'debounce',
                        'codigo': codigo,
                        'reader_id': reader_id,
                        'item_key': found_key,
                        'mensagem': 'Leitura duplicada ignorada (bipado há menos de 2s).'
                    }), 200
                WebServer._barcode_last_scan[found_key] = now_ts

                # Releitura do item DENTRO do lock — garante estado fresco,
                # já que outra requisição pode ter alterado pending_orders.data
                # entre o match inicial (fora do lock) e agora.
                found_item = pending_orders.data.get(found_key)
                if not found_item:
                    return jsonify({
                        'acao': 'nao_encontrado',
                        'codigo': codigo,
                        'reader_id': reader_id,
                        'mensagem': f'Item removido durante o processamento — bipe novamente.'
                    }), 404

                status_atual  = found_item.get('status', 'waiting')
                nome_produto  = found_item.get('nome') or found_item.get('nome_original', '')
                timer_key     = found_item.get('timer_key') or f"{nome_produto}||{found_key}"
                eh_cadeira    = _is_cadeira(nome_produto)
                fsm_step      = found_item.get('fsm_step', '')   # '' | 'marcenaria' | 'tapecaria'

                def _broadcast_async():
                    def _do():
                        try:
                            usage = self.orchestrator.calculate_component_usage()
                            self.orchestrator._component_usage_cache = usage
                            self.orchestrator.broadcast_kpi_update(component_usage=usage)
                        except Exception: pass
                    Thread(target=_do, daemon=True).start()

                # ══════════════════════════════════════════════════════════
                # ETAPA 1 — waiting → in_production (Marcenaria / Início MDF)
                # ══════════════════════════════════════════════════════════
                if status_atual == 'waiting':
                    # Caso pós-restart: item estava em produção, foi restaurado para
                    # waiting com scan_iniciado=True e fsm_step já definido.
                    # A bipagem deve retomar da etapa onde parou, não reiniciar do zero.
                    if found_item.get('scan_iniciado') and found_item.get('fsm_step'):
                        pending_orders.start_production(found_key)
                        production_timer.start(timer_key)
                        pending_orders.data[found_key]['timer_key'] = timer_key
                        pending_orders._save_one(found_key)
                        _broadcast_async()
                        step_label = {'marcenaria': 'Marcenaria', 'tapecaria': 'Tapeçaria', 'mdf': 'Produção'}.get(fsm_step, 'Produção')
                        return jsonify({
                            'acao': 'retomado',
                            'codigo': codigo,
                            'reader_id': reader_id,
                            'item_key': found_key,
                            'nome': nome_produto,
                            'fsm_step': fsm_step,
                            'mensagem': f'▶️ {step_label} RETOMADA: {nome_produto}'
                        })

                    if found_item.get('scan_iniciado'):
                        return jsonify({
                            'acao': 'ja_lido_etapa',
                            'codigo': codigo,
                            'reader_id': reader_id,
                            'item_key': found_key,
                            'nome': nome_produto,
                            'mensagem': f'Pedido #{codigo} já foi iniciado. Veja a aba Produzindo.'
                        })

                    pending_orders.start_production(found_key)
                    production_timer.start(timer_key)
                    pending_orders.data[found_key]['timer_key']   = timer_key
                    pending_orders.data[found_key]['scan_iniciado'] = True
                    pending_orders.data[found_key]['reader_inicio'] = reader_id
                    pending_orders.data[found_key]['fsm_step'] = 'marcenaria' if eh_cadeira else 'mdf'
                    pending_orders._save_one(found_key)

                    try:
                        _cc = globals().get('component_consumption')
                        if _cc and eh_cadeira:
                            consumo_key = f"scan_{found_key}"
                            existing_logs = _cc.get_current_month().get('checklist_logs', [])
                            already = any(l.get('produto') == consumo_key for l in existing_logs)
                            if not already:
                                for comp in RECIPE_CADEIRA:
                                    _cc.register_component(comp['nome'], comp['qtd'], comp['un'], consumo_key)
                                logger.info(f"✅ Componentes registrados via scan para {nome_produto}")
                    except Exception as _ce:
                        logger.warning(f"Scan: erro componentes: {_ce}")

                    _broadcast_async()
                    etapa_label = 'Marcenaria' if eh_cadeira else 'Produção'
                    return jsonify({
                        'acao': 'iniciado',
                        'codigo': codigo,
                        'reader_id': reader_id,
                        'item_key': found_key,
                        'nome': nome_produto,
                        'timer_key': timer_key,
                        'fsm_step': pending_orders.data[found_key]['fsm_step'],
                        'mensagem': f'✅ {etapa_label} INICIADA: {nome_produto}'
                    })

                # ══════════════════════════════════════════════════════════
                # ETAPA 2 — in_production, FSM marcenaria → tapecaria (cadeiras)
                #           in_production, FSM mdf → done
                # ══════════════════════════════════════════════════════════
                elif status_atual == 'in_production':
                    current_step = found_item.get('fsm_step', 'mdf')

                    if eh_cadeira and current_step == 'marcenaria':
                        if found_item.get('scan_tapecaria'):
                            return jsonify({
                                'acao': 'ja_lido_etapa',
                                'codigo': codigo,
                                'reader_id': reader_id,
                                'item_key': found_key,
                                'nome': nome_produto,
                                'mensagem': f'#{codigo} já passou para Tapeçaria.'
                            })
                        pending_orders.data[found_key]['fsm_step']        = 'tapecaria'
                        pending_orders.data[found_key]['scan_tapecaria']   = True
                        pending_orders.data[found_key]['reader_tapecaria'] = reader_id
                        pending_orders._save_one(found_key)
                        _broadcast_async()
                        return jsonify({
                            'acao': 'tapecaria',
                            'codigo': codigo,
                            'reader_id': reader_id,
                            'item_key': found_key,
                            'nome': nome_produto,
                            'fsm_step': 'tapecaria',
                            'mensagem': f'🧵 Tapeçaria INICIADA: {nome_produto}'
                        })

                    if eh_cadeira and current_step == 'tapecaria':
                        if found_item.get('scan_concluido'):
                            return jsonify({
                                'acao': 'ja_lido_etapa',
                                'codigo': codigo,
                                'reader_id': reader_id,
                                'item_key': found_key,
                                'nome': nome_produto,
                                'mensagem': f'Pedido #{codigo} já foi concluído.'
                            })
                        result = production_timer.stop_and_log(timer_key)
                        tempo  = result.get('elapsed', 0)
                        pending_orders.finish_production(found_key, tempo_segundos=tempo)
                        pending_orders.data[found_key]['scan_concluido']   = True
                        pending_orders.data[found_key]['reader_conclusao'] = reader_id
                        pending_orders._save_one(found_key)
                        _broadcast_async()
                        h = int(tempo // 3600); m = int((tempo % 3600) // 60); s = int(tempo % 60)
                        return jsonify({
                            'acao': 'concluido',
                            'codigo': codigo,
                            'reader_id': reader_id,
                            'item_key': found_key,
                            'nome': nome_produto,
                            'tempo_producao': tempo,
                            'mensagem': f'✅ Produção CONCLUÍDA: {nome_produto} ({h:02d}:{m:02d}:{s:02d})'
                        })

                    if not eh_cadeira or current_step == 'mdf':
                        if found_item.get('scan_concluido'):
                            return jsonify({
                                'acao': 'ja_lido_etapa',
                                'codigo': codigo,
                                'reader_id': reader_id,
                                'item_key': found_key,
                                'nome': nome_produto,
                                'mensagem': f'Pedido #{codigo} já foi concluído. Veja a aba Concluídos.'
                            })
                        result = production_timer.stop_and_log(timer_key)
                        tempo  = result.get('elapsed', 0)
                        pending_orders.finish_production(found_key, tempo_segundos=tempo)
                        pending_orders.data[found_key]['scan_concluido']   = True
                        pending_orders.data[found_key]['reader_conclusao'] = reader_id
                        pending_orders._save_one(found_key)
                        _broadcast_async()
                        h = int(tempo // 3600); m = int((tempo % 3600) // 60); s = int(tempo % 60)
                        return jsonify({
                            'acao': 'concluido',
                            'codigo': codigo,
                            'reader_id': reader_id,
                            'item_key': found_key,
                            'nome': nome_produto,
                            'tempo_producao': tempo,
                            'mensagem': f'✅ Produção CONCLUÍDA: {nome_produto} ({h:02d}:{m:02d}:{s:02d})'
                        })

                    logger.warning(f"Scan: estado FSM inesperado para {found_key}: step={current_step} status={status_atual}")
                    return jsonify({
                        'acao': 'erro_fsm',
                        'codigo': codigo,
                        'mensagem': f'Estado de produção inconsistente para #{codigo}. Contate o administrador.'
                    }), 500

                else:
                    return jsonify({
                        'acao': 'ja_concluido',
                        'codigo': codigo,
                        'reader_id': reader_id,
                        'mensagem': f'Pedido #{codigo} já foi concluído anteriormente.'
                    })
            finally:
                item_lock.release()

        @self.app.route('/api/pending-orders/start', methods=['POST'])
        @token_required
        def api_pending_orders_start(token):
            """DESATIVADO — produção só avança via leitura de código de barras (/api/barcode/scan)."""
            return jsonify({
                'error': 'Operação não permitida. Use o leitor de código de barras para iniciar a produção.',
                'use': '/api/barcode/scan'
            }), 403

        @self.app.route('/api/pending-orders/finish', methods=['POST'])
        @token_required
        def api_pending_orders_finish(token):
            """DESATIVADO — produção só conclui via leitura de código de barras (/api/barcode/scan)."""
            return jsonify({
                'error': 'Operação não permitida. Use o leitor de código de barras para concluir a produção.',
                'use': '/api/barcode/scan'
            }), 403

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
            """
            Força sincronização imediata com o Bling — busca pedidos do mês
            e popula a fila de produção. Usado pelo botão '🔄 Sincronizar Bling'.
            Roda de forma síncrona (bloqueante) para o front saber quantos itens
            novos entraram, mas com timeout de segurança.
            """
            orch = self.orchestrator
            if not orch.auth.is_authenticated():
                return jsonify({'message': 'Autentique-se com o Bling primeiro (/auth).', 'added': 0}), 200

            before = len(pending_orders.data)
            try:
                # force=True ignora o lock de "recálculo em andamento" de outro worker
                orch.process_sales_orders(force=True)
            except Exception as e:
                logger.exception("Erro em sync manual de pedidos")
                return jsonify({'message': f'Erro ao sincronizar: {e}', 'added': 0}), 500

            after = len(pending_orders.data)
            added = max(0, after - before)
            total_pedidos = len(orch.sales._sales_history or [])

            if added == 0:
                msg = (f"Nenhum item novo. {total_pedidos} pedido(s) no histórico — "
                       "todos já estão na fila ou fora do mês atual.")
            else:
                msg = f"{added} novo(s) item(ns) adicionados à fila."

            return jsonify({'added': added, 'message': msg, 'total_pedidos': total_pedidos})

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

        # ── Rota de emergência: reset de tokens OAuth ─────────────────────────────
        @self.app.route('/admin/reset-tokens', methods=['GET', 'POST'])
        def admin_reset_tokens():
            """
            Página de emergência para limpar tokens OAuth revogados/corrompidos
            e reiniciar o fluxo de autenticação com o Bling.
            GET  → exibe página com botão de reset
            POST → executa o reset e redireciona para /auth
            """
            if request.method == 'POST':
                try:
                    # 1) Limpa MongoDB — collection correta é 'auth_tokens', doc_id='tokens'
                    #    (MongoStore.set usa _mongo_db['auth_tokens'], NÃO _mongo_db['tokens'])
                    if _mongo_db is not None:
                        result_at = _mongo_db['auth_tokens'].delete_many({})
                        result_t  = _mongo_db['tokens'].delete_many({})  # limpa ambas por segurança
                        logger.info(
                            f"🗑️  MongoDB limpo: auth_tokens={result_at.deleted_count} docs, "
                            f"tokens={result_t.deleted_count} docs"
                        )
                    # 2) Limpa arquivo local
                    tokens_path = Path(self.orchestrator.auth.config.TOKENS_FILE)
                    if tokens_path.exists():
                        tokens_path.write_text('{}')
                    # 3) Limpa TODA a memória do AuthManager — incluindo env var
                    auth = self.orchestrator.auth
                    auth._tokens        = {}
                    auth._access_token  = None
                    auth._refresh_token = None
                    auth._expires_at    = 0
                    auth._initial_load_failed = False
                    # 4) Para o worker de background
                    try:
                        self.orchestrator.stop_worker()
                    except Exception:
                        pass
                    logger.info("🔄 Tokens resetados corretamente. Aguardando nova autenticação.")
                except Exception as e:
                    logger.error(f"Erro ao resetar tokens: {e}")
                    return f"""<!DOCTYPE html><html><body style="font-family:sans-serif;padding:40px;background:#0f172a;color:#e2e8f0">
                        <h2>❌ Erro ao resetar tokens</h2><pre style="color:#f87171">{e}</pre>
                        <a href="/admin/reset-tokens" style="color:#60a5fa">← Voltar</a></body></html>""", 500
                return redirect('/auth')

            # GET → página com botão
            return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Reset OAuth — SW Móveis MDF</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      min-height: 100vh; display: flex; align-items: center; justify-content: center;
      background: #0f172a; font-family: 'Segoe UI', sans-serif; color: #e2e8f0;
    }
    .card {
      background: #1e293b; border: 1px solid #334155; border-radius: 16px;
      padding: 48px 40px; max-width: 480px; width: 100%; text-align: center;
      box-shadow: 0 20px 60px rgba(0,0,0,.5);
    }
    .icon { font-size: 56px; margin-bottom: 16px; }
    h1 { font-size: 22px; font-weight: 700; color: #f1f5f9; margin-bottom: 8px; }
    p  { color: #94a3b8; font-size: 14px; line-height: 1.6; margin-bottom: 24px; }
    .badge {
      display: inline-block; background: #ef444420; color: #f87171;
      border: 1px solid #ef444440; border-radius: 8px;
      padding: 6px 14px; font-size: 13px; margin-bottom: 28px;
    }
    .btn-reset {
      display: block; width: 100%; padding: 14px;
      background: #ef4444; color: #fff; border: none; border-radius: 10px;
      font-size: 16px; font-weight: 600; cursor: pointer; transition: background .2s;
      margin-bottom: 12px;
    }
    .btn-reset:hover { background: #dc2626; }
    .btn-back {
      display: block; width: 100%; padding: 12px;
      background: #1e293b; color: #94a3b8; border: 1px solid #334155;
      border-radius: 10px; font-size: 14px; text-decoration: none; transition: background .2s;
    }
    .btn-back:hover { background: #0f172a; color: #e2e8f0; }
    .warning {
      background: #78350f20; border: 1px solid #78350f60; border-radius: 8px;
      padding: 12px; font-size: 12px; color: #fbbf24; margin-top: 20px; text-align: left;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">🔑</div>
    <h1>Reset de Autenticação Bling</h1>
    <p>Use este botão quando a API do Bling retornar <strong>HTTP 403</strong> mesmo com token aparentemente válido. O reset apaga os tokens salvos e inicia um novo fluxo OAuth.</p>
    <div class="badge">⚠️ Token atual: revogado / inválido</div>

    <form method="POST">
      <button type="submit" class="btn-reset">🗑️ Resetar Tokens e Reautenticar</button>
    </form>
    <a href="/" class="btn-back">← Voltar ao Dashboard</a>

    <div class="warning">
      <strong>O que acontece:</strong><br>
      1. Tokens apagados do MongoDB e disco<br>
      2. Worker de background pausado<br>
      3. Redirecionado para o Bling para nova autorização<br>
      4. Após autorizar, o sistema retoma automaticamente
    </div>
  </div>
</body>
</html>"""

        # ── Rota de reparo: repovoar pedido_numero em itens existentes ─────────
        @self.app.route('/admin/repair-orders', methods=['GET', 'POST'])
        def admin_repair_orders():
            """Corrige itens existentes no MongoDB que têm order_id mas não têm pedido_numero."""
            if request.method == 'POST':
                repaired = 0
                skipped  = 0
                errors   = []
                for key, item in list(pending_orders.data.items()):
                    pnum = item.get('pedido_numero')
                    if pnum and str(pnum) != '0':
                        skipped += 1
                        continue
                    # Tenta preencher de order_id_bling ou order_id
                    oid = str(item.get('order_id_bling') or item.get('order_id') or '')
                    if not oid or oid == '0':
                        errors.append(f"{key}: sem order_id")
                        continue
                    # Usa o order_id como pedido_numero (é o número interno Bling)
                    pending_orders.data[key]['pedido_numero'] = oid
                    if not item.get('order_id'):
                        pending_orders.data[key]['order_id'] = oid
                    pending_orders._save_one(key)
                    repaired += 1

                msg = (f"Reparados: {repaired} | Já tinham número: {skipped} | "
                       f"Sem order_id: {len(errors)}")
                logger.info(f"🔧 Repair orders: {msg}")
                return f"""<!DOCTYPE html><html><body style="font-family:sans-serif;padding:40px;background:#0f172a;color:#e2e8f0">
                    <h2>✅ Reparo concluído</h2>
                    <p style="color:#86efac">{msg}</p>
                    {'<p style="color:#f87171">Sem order_id: ' + ', '.join(errors[:10]) + '</p>' if errors else ''}
                    <a href="/admin/repair-orders" style="color:#60a5fa">← Voltar</a> &nbsp;
                    <a href="/" style="color:#60a5fa">Ir ao Dashboard →</a>
                    </body></html>"""

            # GET — preview
            need_repair = []
            ok = 0
            no_id = 0
            for key, item in pending_orders.data.items():
                pnum = item.get('pedido_numero')
                oid  = item.get('order_id_bling') or item.get('order_id')
                if pnum and str(pnum) != '0':
                    ok += 1
                elif oid and str(oid) != '0':
                    need_repair.append(f"{key} → usar order_id={oid}")
                else:
                    no_id += 1

            return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="UTF-8"><title>Repair Orders</title>
<style>body{{font-family:'Segoe UI',sans-serif;padding:40px;background:#0f172a;color:#e2e8f0;max-width:700px;margin:0 auto}}
h1{{color:#f1f5f9}}h2{{color:#94a3b8;font-size:1rem;margin-top:24px}}
.tag-green{{color:#86efac}}.tag-yellow{{color:#fbbf24}}.tag-red{{color:#f87171}}
ul{{padding-left:20px;font-size:.85rem;color:#94a3b8}}
.btn{{display:inline-block;padding:12px 28px;border-radius:8px;font-weight:700;text-decoration:none;border:none;cursor:pointer;font-size:1rem}}
.btn-blue{{background:#3b82f6;color:#fff}}.btn-back{{background:#1e293b;color:#94a3b8;border:1px solid #334155;margin-right:12px}}</style>
</head><body>
<h1>🔧 Reparo de Pedidos</h1>
<p style="color:#94a3b8">Preenche <code>pedido_numero</code> usando <code>order_id</code> em itens que estão com o campo vazio.</p>
<h2 class="tag-green">✅ Já têm pedido_numero ({ok})</h2>
<h2 class="tag-yellow">⚠️ Serão reparados — usarão order_id ({len(need_repair)})</h2>
<ul>{''.join(f'<li>{r}</li>' for r in need_repair[:20]) or '<li>Nenhum</li>'}</ul>
<h2 class="tag-red">❌ Sem nenhum identificador — serão ignorados ({no_id})</h2>
<p style="color:#64748b;font-size:.8rem;">Itens sem identificador devem ser removidos pelo <a href="/admin/purge-ghost-orders" style="color:#60a5fa">Purge</a>.</p>
<br>
<a href="/" class="btn btn-back">← Voltar</a>
<form method="POST" style="display:inline">
  <button type="submit" class="btn btn-blue">🔧 Confirmar Reparo ({len(need_repair)} itens)</button>
</form>
</body></html>"""

        # ── Rota de status: progresso do checkpoint de sync individual ─────────
        @self.app.route('/admin/sync-status', methods=['GET', 'POST'])
        def admin_sync_status():
            if request.method == 'POST' and request.form.get('action') == 'reset':
                if MONGO_AVAILABLE:
                    try:
                        _mongo_db['sync_checkpoint'].delete_one({'_id': 'fetched_order_ids'})
                    except Exception:
                        pass
                return redirect('/admin/sync-status')

            if request.method == 'POST' and request.form.get('action') == 'reenable_ops':
                self.orchestrator._ops_endpoint_disabled_until = 0
                return redirect('/admin/sync-status')

            fetched_count = 0
            updated_at = 'N/D'
            if MONGO_AVAILABLE:
                try:
                    doc = _mongo_db['sync_checkpoint'].find_one({'_id': 'fetched_order_ids'})
                    if doc:
                        fetched_count = len(doc.get('ids', []))
                        updated_at = doc.get('updated_at', 'N/D')
                except Exception:
                    pass

            total_history = len(self.orchestrator.sales._sales_history or [])
            board_total = len(pending_orders.data)
            waiting = len(pending_orders.get_waiting())

            ops_disabled_until = getattr(self.orchestrator, '_ops_endpoint_disabled_until', 0)
            ops_circuit_open = time.time() < ops_disabled_until
            if ops_circuit_open:
                mins_left = int((ops_disabled_until - time.time()) / 60)
                ops_status_html = f'<span style="color:#fbbf24">⚠️ Desativado (FORBIDDEN) — reativa em {mins_left} min</span>'
            else:
                ops_status_html = '<span style="color:#86efac">✅ Ativo</span>'

            return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="UTF-8"><title>Sync Status</title>
<style>body{{font-family:'Segoe UI',sans-serif;padding:40px;background:#0f172a;color:#e2e8f0;max-width:600px;margin:0 auto}}
h1{{color:#f1f5f9}}.row{{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #1e293b}}
.label{{color:#94a3b8}}.value{{font-weight:700;color:#86efac;font-family:monospace}}
.btn{{display:inline-block;padding:10px 24px;border-radius:8px;font-weight:700;text-decoration:none;border:none;cursor:pointer;font-size:.9rem;margin-top:20px}}
.btn-back{{background:#1e293b;color:#94a3b8;border:1px solid #334155;margin-right:12px}}
.btn-red{{background:#ef4444;color:#fff}}
.btn-blue{{background:#3b82f6;color:#fff}}</style>
</head><body>
<h1>📊 Status de Sincronização</h1>
<div class="row"><span class="label">Endpoint "Ordens de Produção"</span><span class="value">{ops_status_html}</span></div>
<div class="row"><span class="label">Pedidos com itens já buscados (checkpoint)</span><span class="value">{fetched_count}</span></div>
<div class="row"><span class="label">Última atualização do checkpoint</span><span class="value">{updated_at}</span></div>
<div class="row"><span class="label">Total no histórico de vendas</span><span class="value">{total_history}</span></div>
<div class="row"><span class="label">Itens no board (todos status)</span><span class="value">{board_total}</span></div>
<div class="row"><span class="label">Itens "Em Espera"</span><span class="value">{waiting}</span></div>
<p style="color:#64748b;font-size:.8rem;margin-top:16px;">
O sistema processa ~40 pedidos por ciclo (~10 min entre ciclos, acelerado enquanto houver pendentes).
Reiniciar o checkpoint força reprocessamento de todos os pedidos do mês.
</p>
{f'''<p style="color:#fbbf24;font-size:.8rem;background:#78350f20;border:1px solid #78350f60;border-radius:8px;padding:12px;">
⚠️ O endpoint de Ordens de Produção retornou erro de permissão (FORBIDDEN). Isso geralmente
significa que o módulo "Ordens de Produção" não está habilitado nesta conta Bling, ou o escopo
correspondente não foi marcado ao criar o aplicativo. O sistema continua funcionando normalmente
sem ele — apenas o número da OP interna não aparece nos cards (o código de barras do produto
já é suficiente). Se você habilitar o módulo no Bling, use o botão abaixo para reativar agora.
</p>''' if ops_circuit_open else ''}
<a href="/" class="btn btn-back">← Voltar</a>
<form method="POST" style="display:inline">
  <input type="hidden" name="action" value="reset">
  <button type="submit" class="btn btn-red" onclick="return confirm('Resetar checkpoint? Isso fará o sistema rebuscar todos os pedidos do mês novamente.')">🔄 Resetar Checkpoint</button>
</form>
{f'''<form method="POST" style="display:inline">
  <input type="hidden" name="action" value="reenable_ops">
  <button type="submit" class="btn btn-blue">🔓 Reativar Ordens de Produção</button>
</form>''' if ops_circuit_open else ''}
</body></html>"""

        # ── Rota de emergência: purge de pedidos fantasma/antigos ────────────────
        @self.app.route('/admin/purge-ghost-orders', methods=['GET', 'POST'])
        def admin_purge_ghost_orders():
            if request.method == 'POST':
                try:
                    removed = pending_orders.reset_if_new_month()
                    # Força também limpeza direto no MongoDB para itens que não estão em memória
                    mongo_removed = 0
                    if MONGO_AVAILABLE:
                        agora = datetime.now()
                        cutoff = (agora - timedelta(days=30)).isoformat()
                        try:
                            # Remove itens sem identificador válido
                            r1 = _mongo_db['pending_orders'].delete_many({
                                'pedido_numero': {'$in': [None, '', 0]},
                                'order_id':      {'$in': [None, '', 0]},
                                'ordem_producao':{'$in': [None, '', 0]},
                            })
                            # Remove itens waiting/in_production com mais de 30 dias
                            r2 = _mongo_db['pending_orders'].delete_many({
                                'status': {'$in': ['waiting', 'in_production']},
                                'added_at': {'$lt': cutoff},
                            })
                            mongo_removed = r1.deleted_count + r2.deleted_count
                        except Exception as _me:
                            logger.error(f"Purge MongoDB direto: {_me}")
                    # Recarrega memória do MongoDB após purge
                    pending_orders.load()
                    msg = f"Removidos: {removed} em memória + {mongo_removed} direto no MongoDB. Total em fila: {len(pending_orders.data)}"
                    logger.info(f"🧹 Purge manual: {msg}")
                    return f"""<!DOCTYPE html><html><body style="font-family:sans-serif;padding:40px;background:#0f172a;color:#e2e8f0">
                        <h2>✅ Purge concluído</h2><p style="color:#86efac">{msg}</p>
                        <a href="/admin/purge-ghost-orders" style="color:#60a5fa">← Voltar</a> &nbsp;
                        <a href="/" style="color:#60a5fa">Ir ao Dashboard →</a>
                        </body></html>"""
                except Exception as e:
                    return f"""<!DOCTYPE html><html><body style="font-family:sans-serif;padding:40px;background:#0f172a;color:#e2e8f0">
                        <h2>❌ Erro</h2><pre style="color:#f87171">{e}</pre>
                        <a href="/admin/purge-ghost-orders" style="color:#60a5fa">← Voltar</a></body></html>""", 500

            # GET — mostra preview do que será removido
            agora = datetime.now()
            mes_atual = f"{agora.year}-{agora.month:02d}"
            ghost, old_items, ok = [], [], []
            for key, item in pending_orders.data.items():
                has_id = (item.get('pedido_numero') or item.get('ordem_producao') or item.get('order_id'))
                if not has_id:
                    ghost.append(key)
                    continue
                status = item.get('status', 'waiting')
                mes_ref = item.get('added_at', '')[:7] if status != 'done' else (item.get('mes_conclusao') or item.get('finished_at','')[:7])
                try:
                    added_dt = datetime.fromisoformat(item.get('added_at',''))
                    if status != 'done' and (agora - added_dt).days >= 30:
                        old_items.append(f"{key} ({(agora-added_dt).days}d, {status})")
                        continue
                except Exception:
                    pass
                if mes_ref and mes_ref != mes_atual and status != 'done':
                    old_items.append(f"{key} (mês={mes_ref}, {status})")
                else:
                    ok.append(key)

            return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="UTF-8">
<title>Purge Pedidos Fantasma</title>
<style>body{{font-family:'Segoe UI',sans-serif;padding:40px;background:#0f172a;color:#e2e8f0;max-width:700px;margin:0 auto}}
h1{{color:#f1f5f9}}h2{{color:#94a3b8;font-size:1rem;margin-top:24px}}
.tag-red{{color:#f87171}}.tag-yellow{{color:#fbbf24}}.tag-green{{color:#86efac}}
ul{{padding-left:20px;font-size:.85rem;color:#94a3b8}}
.btn{{display:inline-block;padding:12px 28px;border-radius:8px;font-weight:700;text-decoration:none;border:none;cursor:pointer;font-size:1rem}}
.btn-red{{background:#ef4444;color:#fff}}.btn-back{{background:#1e293b;color:#94a3b8;border:1px solid #334155;margin-right:12px}}</style>
</head><body>
<h1>🧹 Purge de Pedidos Fantasma</h1>
<h2 class="tag-red">❌ SERÃO REMOVIDOS — sem identificador ({len(ghost)})</h2>
<ul>{''.join(f'<li>{g}</li>' for g in ghost[:20]) or '<li>Nenhum</li>'}</ul>
<h2 class="tag-yellow">⚠️ SERÃO REMOVIDOS — antigos/expirados ({len(old_items)})</h2>
<ul>{''.join(f'<li>{o}</li>' for o in old_items[:20]) or '<li>Nenhum</li>'}</ul>
<h2 class="tag-green">✅ SERÃO MANTIDOS ({len(ok)})</h2>
<ul>{''.join(f'<li>{k}</li>' for k in ok[:20]) or '<li>Nenhum</li>'}</ul>
<br>
<a href="/" class="btn btn-back">← Voltar</a>
<form method="POST" style="display:inline">
  <button type="submit" class="btn btn-red">🗑️ Confirmar Purge ({len(ghost)+len(old_items)} itens)</button>
</form>
</body></html>"""

        # Rota de Callback OAuth (Recebe o code do Bling)
        @self.app.route('/callback')
        def callback():
            code  = request.args.get('code')
            state = request.args.get('state')
            error = request.args.get('error')
            
            logger.critical(
                f"🔐 CALLBACK RECEBIDO | code={'✅ presente' if code else '❌ ausente'} | "
                f"state={'✅ presente' if state else '❌ ausente'} | "
                f"error={error!r} | "
                f"args={dict(request.args)}"
            )

            if error:
                logger.critical(f"❌ BLING RETORNOU ERRO NO CALLBACK: {error!r}")
                return f"Bling retornou erro: {error}", 400
            
            if not code:
                logger.error("Código de autorização OAuth não recebido.")
                return "Erro: Código de autorização não recebido.", 400
                
            if not self.orchestrator.auth._validate_oauth_state(state):
                logger.critical(
                    f"❌ STATE INVÁLIDO | recebido={state!r} | "
                    f"session_keys={list(session.keys())}"
                )
                return "Erro: State inválido ou expirado.", 403

            success = self.orchestrator.auth.exchange_code_for_token(code)

            if success:
                logger.info("✅ Autenticação OAuth concluída com sucesso.")
                # Força re-sincronização imediata ignorando o cache de 5min
                self.orchestrator.auth._last_storage_sync = 0
                self.orchestrator.auth.reload_tokens_from_disk()

                if not self.orchestrator.is_running():
                    self.orchestrator.start_worker()
                    start_cleanup_timer()
                    logger.info("🚀 Worker iniciado após autenticação.")
                else:
                    # Acorda o worker E reseta o timer de sync para ele pegar o token novo
                    self.orchestrator.auth._last_storage_sync = 0
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
    <link rel="icon" href="https://i.imgur.com/j79HO6n.png" type="image/png">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/jsbarcode@3.11.6/dist/JsBarcode.all.min.js"></script>
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

        /* ══ SCANNER GLOBAL ══ */
        #scanner-indicator {
            position: fixed; top: 70px; right: 16px; z-index: 9999;
            background: #01010d; color: #ffb600;
            border: 2px solid #ffb600; border-radius: 50px;
            padding: 6px 14px; font-size: 0.72rem; font-weight: 700;
            letter-spacing: 0.06em; display: none;
            box-shadow: 0 4px 20px rgba(255,182,0,0.4);
            animation: pulse-badge 1s infinite;
        }
        #scanner-indicator.active { display: flex; align-items: center; gap: 6px; }

        /* ══ BOARD SUB-ABAS ══ */
        .board-tab-btn { outline: none; }
        .board-tab-btn:hover { opacity: 0.85; transform: translateY(-1px); }
        .active-board-tab { opacity: 1 !important; box-shadow: 0 4px 14px rgba(0,0,0,0.25); }

        /* ══ CARD DE BARCODE (Em Espera / Produzindo) ══ */
        .bc-card {
            border: 1.5px solid var(--border);
            border-radius: var(--radius);
            background: #fff;
            padding: 18px 16px 14px;
            transition: box-shadow .2s, border-color .2s, transform .2s;
            position: relative;
        }
        .bc-card:hover { box-shadow: 0 6px 24px rgba(0,0,0,.09); border-color: var(--sw-yellow); transform: translateY(-2px); }
        .bc-card.urgente { border-color: #ef4444; background: #fff5f5; }
        .bc-card.atencao { border-color: #f59e0b; }
        .bc-card.urgente  { border-color: #ef4444 !important; background: #fff5f5; }
        .bc-card.critico  { border-color: #f97316 !important; background: #fff8f0; }
        .bc-card.atencao  { border-color: #f59e0b !important; background: #fffbeb; }
        .bc-card.normal   { border-color: var(--border); }
        .bc-card.inprod   { border-color: #10b981 !important; background: #f0fdf4; }
        /* Urgency pulse for overdue */
        .bc-card.urgente .bc-num { animation: pulse-animation 1.2s infinite; color: #ef4444; }

        .bc-card .bc-num  { font-family: 'Bebas Neue', sans-serif; font-size: 1.5rem; letter-spacing: .05em; color: var(--sw-black); }
        .bc-card .bc-nome { font-size: 0.78rem; font-weight: 700; color: #374151; margin-bottom: 10px; line-height: 1.3; }
        .bc-card .bc-meta { font-size: 0.68rem; color: #9ca3af; margin-top: 6px; }
        .bc-card .bc-prazo { position: absolute; top: 10px; right: 10px; }
        .bc-card .bc-svg-wrap {
            background: #fff;
            border: 1px solid #e5e7eb;
            border-radius: 6px;
            padding: 8px 6px 4px;
            width: 100%;
            max-width: 100%;
            overflow: hidden;          /* nunca deixa o SVG escapar do card */
            box-sizing: border-box;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .bc-card .bc-svg-wrap svg {
            display: block;
            width: 100% !important;    /* JsBarcode define width/height em px fixos —
                                           força responsivo dentro do container */
            height: auto;
            max-height: 52px;
            max-width: 100%;
        }
        .bc-card .bc-lido-overlay {
            position: absolute; inset: 0; background: rgba(16,185,129,.92); border-radius: var(--radius);
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            color: #fff; font-weight: 800; font-size: 1rem; letter-spacing: .04em;
            animation: fadeIn .3s ease-out;
        }

        /* ══ IMPRESSÃO ══ */
        @media print {
            body > *:not(#print-area) { display: none !important; }
            #print-area {
                display: block !important;
                position: fixed; inset: 0;
                background: white; z-index: 99999;
                padding: 20px; text-align: center;
            }
        }
        #print-area { display: none; }

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

    <!-- SCANNER GLOBAL INDICATOR -->
    <div id="scanner-indicator">📡 Lendo código...</div>

    <!-- ÁREA DE IMPRESSÃO -->
    <div id="print-area"></div>

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
            <div class="d-flex align-items-center gap-2">
                <span id="status-badge" class="badge bg-secondary" title="Aguardando WebSocket...">⏳ Conectando...</span>
                <a id="auth-link" href="{{ auth_url }}" class="btn btn-sm btn-outline-light">Autenticar</a>

                <!-- Menu unificado de administração -->
                <div class="dropdown">
                    <button class="btn btn-sm dropdown-toggle" type="button" data-bs-toggle="dropdown" aria-expanded="false"
                        style="background:#1e293b;color:#fff;font-weight:600;border:1px solid #334155;border-radius:50px;padding:4px 14px;font-size:.72rem;">
                        ⚙️ Admin
                    </button>
                    <ul class="dropdown-menu dropdown-menu-end" style="font-size:.82rem;min-width:240px;">
                        <li><a class="dropdown-item" href="/admin/reset-tokens">
                            🔑 Reset OAuth
                            <div class="text-muted" style="font-size:.68rem;">Use quando a API retornar 403</div>
                        </a></li>
                        <li><hr class="dropdown-divider"></li>
                        <li><a class="dropdown-item" href="/admin/repair-orders">
                            🔧 Reparar Pedidos
                            <div class="text-muted" style="font-size:.68rem;">Preenche número de pedido ausente</div>
                        </a></li>
                        <li><hr class="dropdown-divider"></li>
                        <li><a class="dropdown-item" href="/admin/sync-status">
                            📊 Status da Sincronização
                            <div class="text-muted" style="font-size:.68rem;">Progresso da busca individual de pedidos</div>
                        </a></li>
                        <li><hr class="dropdown-divider"></li>
                        <li><a class="dropdown-item" href="/admin/purge-ghost-orders">
                            🧹 Limpar Antigos
                            <div class="text-muted" style="font-size:.68rem;">Remove pedidos fantasma/+30 dias</div>
                        </a></li>
                    </ul>
                </div>
            </div>
        </div>
    </nav>

    <!-- CONTAINER PRINCIPAL -->
    <div class="container-fluid px-4 py-5">

        <!-- PAGE HEADER -->
        <div class="row mb-4">
            <div class="col-12">
                <h2 class="mb-0 page-title">Painel de <span class="highlight">Produção</span></h2>
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

        <!-- TABS PRINCIPAIS -->
        <div class="row">
            <div class="col-12">
                <ul class="nav nav-tabs mb-0" id="myTab" role="tablist" style="border-bottom:2px solid var(--border);flex-wrap:nowrap;overflow-x:auto;">
                    <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#tab-dashboard">📊 Dashboard</button></li>
                    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-producao">🏭 Produção</button></li>
                    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-insumos">📦 Insumos</button></li>
                    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-expedicao">🚚 Expedição</button></li>
                    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-relatorio">📋 Relatório</button></li>
                    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-ficha">🔧 Ficha Técnica</button></li>
                </ul>

                <!-- AUTH REQUIRED -->
                <div id="auth-required-tabs" class="alert alert-warning hidden mt-3">
                    🔐 É necessário autenticar com o SW Móveis para visualizar o conteúdo.
                </div>

                <!-- TAB CONTENT -->
                <div id="content-tabs" class="tab-content hidden mt-4">

                    <!-- ══════════════════════════════════════════════
                         TAB 1: DASHBOARD
                    ══════════════════════════════════════════════ -->
                    <div class="tab-pane fade show active" id="tab-dashboard" role="tabpanel">

                        <!-- Filtro de data unificado -->
                        <div class="d-flex align-items-center gap-3 mb-4 flex-wrap">
                            <div class="d-flex gap-2 align-items-center">
                                <label class="text-muted small fw-bold mb-0">De:</label>
                                <input type="date" id="filter-date-from" class="form-control form-control-sm" style="width:140px;">
                                <label class="text-muted small fw-bold mb-0">Até:</label>
                                <input type="date" id="filter-date-to" class="form-control form-control-sm" style="width:140px;">
                                <button class="btn btn-primary btn-sm" onclick="applyDashboardFilter()">Filtrar</button>
                                <button class="btn btn-outline-secondary btn-sm" onclick="resetDashboardFilter()">Limpar</button>
                            </div>
                            <button class="btn btn-outline-dark btn-sm ms-auto" onclick="printDashboard()">🖨️ Imprimir</button>
                        </div>

                        <!-- KPIs: 3 etapas de produção -->
                        <div class="row mb-4" id="dash-kpi-row">
                            <div class="col-md-4 mb-3">
                                <div class="card p-4 kpi-card kpi-daily text-center h-100">
                                    <h5>⏳ Em Espera</h5>
                                    <h3 id="kpi-waiting" class="text-warning">0</h3>
                                    <small class="text-muted">Pedidos na fila</small>
                                    <div id="kpi-waiting-nums" class="mt-2" style="font-size:.68rem;color:#888;max-height:50px;overflow-y:auto;"></div>
                                </div>
                            </div>
                            <div class="col-md-4 mb-3">
                                <div class="card p-4 kpi-card kpi-weekly text-center h-100">
                                    <h5>⚙️ Produzindo</h5>
                                    <h3 id="kpi-inprod" style="color:#10b981;">0</h3>
                                    <small class="text-muted">Em produção agora</small>
                                    <div id="kpi-inprod-nums" class="mt-2" style="font-size:.68rem;color:#888;max-height:50px;overflow-y:auto;"></div>
                                </div>
                            </div>
                            <div class="col-md-4 mb-3">
                                <div class="card p-4 kpi-card kpi-historic text-center h-100">
                                    <h5>✅ Concluídos</h5>
                                    <h3 id="kpi-done" style="color:var(--success);">0</h3>
                                    <small class="text-muted">Este mês</small>
                                    <div id="kpi-done-nums" class="mt-2" style="font-size:.68rem;color:#888;max-height:50px;overflow-y:auto;"></div>
                                </div>
                            </div>
                        </div>

                        <!-- KPIs: Pedidos -->
                        <div class="row mb-4">
                            <div class="col-md-3 mb-3">
                                <div class="card p-3 text-center border-start border-4" style="border-color:#ffb600!important;">
                                    <div style="font-size:.65rem;font-weight:800;text-transform:uppercase;letter-spacing:.1em;color:#807f7f;">Pedidos Hoje</div>
                                    <div class="fw-bold" id="kpi-daily" style="font-family:'Bebas Neue',sans-serif;font-size:2.5rem;color:#ffb600;">0</div>
                                    <div id="kpi-daily-orders" style="font-size:.65rem;color:#aaa;max-height:40px;overflow-y:auto;"></div>
                                </div>
                            </div>
                            <div class="col-md-3 mb-3">
                                <div class="card p-3 text-center border-start border-4" style="border-color:#f59e0b!important;">
                                    <div style="font-size:.65rem;font-weight:800;text-transform:uppercase;letter-spacing:.1em;color:#807f7f;">Esta Semana</div>
                                    <div class="fw-bold" id="kpi-weekly" style="font-family:'Bebas Neue',sans-serif;font-size:2.5rem;color:#f59e0b;">0</div>
                                    <div id="kpi-weekly-orders" style="font-size:.65rem;color:#aaa;max-height:40px;overflow-y:auto;"></div>
                                </div>
                            </div>
                            <div class="col-md-3 mb-3">
                                <div class="card p-3 text-center border-start border-4" style="border-color:#10b981!important;">
                                    <div style="font-size:.65rem;font-weight:800;text-transform:uppercase;letter-spacing:.1em;color:#807f7f;">Este Mês</div>
                                    <div class="fw-bold" id="kpi-historic" style="font-family:'Bebas Neue',sans-serif;font-size:2.5rem;color:#10b981;">0</div>
                                    <div id="kpi-monthly-orders" style="font-size:.65rem;color:#aaa;max-height:40px;overflow-y:auto;"></div>
                                </div>
                            </div>
                            <div class="col-md-3 mb-3">
                                <div class="card p-3 text-center border-start border-4" style="border-color:#6366f1!important;">
                                    <div style="font-size:.65rem;font-weight:800;text-transform:uppercase;letter-spacing:.1em;color:#807f7f;">Tendência</div>
                                    <div class="fw-bold" id="trend-indicator" style="font-family:'Bebas Neue',sans-serif;font-size:1.4rem;color:#6366f1;">—</div>
                                    <div id="growth-weekly" style="font-size:.9rem;font-weight:700;"></div>
                                    <div id="growth-tooltip" style="font-size:.6rem;color:#aaa;margin-top:3px;line-height:1.3;"></div>
                                </div>
                            </div>
                        </div>

                        <!-- Gráfico principal: pedidos + produção -->
                        <div class="row mb-4">
                            <div class="col-lg-8 mb-4">
                                <div class="card">
                                    <div class="card-header d-flex justify-content-between align-items-center">
                                        <h5 class="mb-0">📈 Pedidos × Produção <span style="color:var(--sw-yellow)">(Últimos 30 dias)</span></h5>
                                        <small id="last-recalculated" class="text-muted"></small>
                                    </div>
                                    <div class="card-body" style="height:340px;">
                                        <canvas id="salesChart"></canvas>
                                    </div>
                                </div>
                            </div>
                            <div class="col-lg-4 mb-4">
                                <div class="card h-100">
                                    <div class="card-header"><h5 class="mb-0">⚡ Produção por Etapa</h5></div>
                                    <div class="card-body" style="height:300px;">
                                        <canvas id="stagesChart"></canvas>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <!-- Gráfico barcode por produção + queda/subida -->
                        <div class="row mb-4">
                            <div class="col-lg-6 mb-4">
                                <div class="card">
                                    <div class="card-header"><h5 class="mb-0">📊 Produção por Dia (Barras)</h5></div>
                                    <div class="card-body" style="height:260px;">
                                        <canvas id="prodBarChart"></canvas>
                                    </div>
                                </div>
                            </div>
                            <div class="col-lg-6 mb-4">
                                <div class="card">
                                    <div class="card-header"><h5 class="mb-0">📉 Queda / 📈 Subida Diária</h5></div>
                                    <div class="card-body" style="height:260px;">
                                        <canvas id="deltaChart"></canvas>
                                    </div>
                                </div>
                            </div>
                        </div>

                    </div>

                    <!-- ══════════════════════════════════════════════
                         TAB 2: PRODUÇÃO
                    ══════════════════════════════════════════════ -->
                    <div class="tab-pane fade" id="tab-producao" role="tabpanel">

                        <!-- Header com sync + sub-abas -->
                        <div class="d-flex justify-content-between align-items-center flex-wrap gap-2 mb-3">
                            <div class="d-flex gap-2 flex-wrap">
                                <button id="tab-waiting-btn" onclick="switchBoardTab('waiting')" class="board-tab-btn active-board-tab"
                                    style="background:rgba(255,182,0,0.18);border:2px solid #ffb600;color:#ffb600;border-radius:50px;padding:6px 20px;font-size:.8rem;font-weight:700;cursor:pointer;transition:all .2s;">
                                    ⏳ Em Espera <span id="waiting-count-badge" style="background:#ffb600;color:#000;border-radius:50px;padding:1px 9px;font-size:.72rem;margin-left:4px;">0</span>
                                </button>
                                <button id="tab-inprod-btn" onclick="switchBoardTab('inprod')" class="board-tab-btn"
                                    style="background:rgba(16,185,129,0.12);border:2px solid rgba(16,185,129,0.4);color:#10b981;border-radius:50px;padding:6px 20px;font-size:.8rem;font-weight:700;cursor:pointer;transition:all .2s;">
                                    ⚙️ Produzindo <span id="inprod-count-badge" style="background:#10b981;color:#fff;border-radius:50px;padding:1px 9px;font-size:.72rem;margin-left:4px;">0</span>
                                </button>
                                <button id="tab-done-btn" onclick="switchBoardTab('done')" class="board-tab-btn"
                                    style="background:rgba(99,102,241,0.12);border:2px solid rgba(99,102,241,0.3);color:#6366f1;border-radius:50px;padding:6px 20px;font-size:.8rem;font-weight:700;cursor:pointer;transition:all .2s;">
                                    ✅ Concluídos <span id="done-count-badge" style="background:#6366f1;color:#fff;border-radius:50px;padding:1px 9px;font-size:.72rem;margin-left:4px;">0</span>
                                </button>
                            </div>
                            <button class="btn btn-sm btn-outline-primary" onclick="syncAndRefreshPending()">🔄 Sincronizar Bling</button>
                        </div>

                        <!-- Sub-abas Marcenaria / Tapeçaria (visível na aba Produzindo) -->
                        <div id="setor-tabs-wrap" style="display:none;" class="mb-3">
                            <div class="d-flex gap-2">
                                <button onclick="switchSetor('todos')" id="setor-todos" class="btn btn-sm btn-dark active">Todos</button>
                                <button onclick="switchSetor('marcenaria')" id="setor-marc" class="btn btn-sm btn-outline-secondary">🪚 Marcenaria</button>
                                <button onclick="switchSetor('tapecaria')" id="setor-tape" class="btn btn-sm btn-outline-secondary">🧵 Tapeçaria</button>
                                <button onclick="printSetor()" class="btn btn-sm btn-outline-dark ms-auto">🖨️ Imprimir Setor</button>
                            </div>
                        </div>

                        <!-- Buscador (visível na aba Produzindo) -->
                        <div id="search-inprod-wrap" style="display:none;" class="mb-3">
                            <input type="text" id="search-inprod" class="form-control form-control-sm" placeholder="🔍 Buscar por número do pedido ou cliente..."
                                oninput="filterInProd(this.value)" style="max-width:360px;">
                        </div>

                        <!-- Painéis das 3 sub-abas -->
                        <div id="board-waiting" class="board-panel"></div>
                        <div id="board-inprod"   class="board-panel" style="display:none;"></div>
                        <div id="board-done"     class="board-panel" style="display:none;"></div>

                    </div>

                    <!-- ══════════════════════════════════════════════
                         TAB 3: INSUMOS
                    ══════════════════════════════════════════════ -->
                    <div class="tab-pane fade" id="tab-insumos" role="tabpanel">

                        <div class="d-flex justify-content-between align-items-center mb-4 flex-wrap gap-2">
                            <div>
                                <h5 class="mb-0">📦 Gestão de Insumos</h5>
                                <small class="text-muted">Consumo real × necessidade por pedidos em espera</small>
                            </div>
                            <span class="badge bg-light text-dark border" id="consumption-total-badge">0 insumos</span>
                        </div>

                        <!-- Guia Compras (necessidade baseada em pedidos) -->
                        <div class="card mb-4 border-0 shadow-sm">
                            <div class="card-header" style="background:linear-gradient(135deg,#1e3a5f,#2563eb)!important;">
                                <h5 class="mb-0">🛒 Guia de Compras — Baseado nos Pedidos em Espera</h5>
                                <small class="text-white-50">Quantidade necessária para produzir todos os pedidos aguardando</small>
                            </div>
                            <div class="card-body p-0" id="purchase-guide-section">
                                <div class="text-center py-4 text-muted">⏳ Calculando...</div>
                            </div>
                        </div>

                        <!-- Consumo Mensal -->
                        <div class="card mb-4 border-0 shadow-sm">
                            <div class="card-header d-flex justify-content-between align-items-center" style="background:linear-gradient(135deg,#065f46,#059669)!important;">
                                <div>
                                    <h5 class="mb-0">📊 Consumo Real do Mês</h5>
                                    <small class="text-white-50" id="consumption-month-label">Mês atual</small>
                                </div>
                            </div>
                            <div class="card-body p-0" id="consumption-table-section">
                                <div class="text-center py-4 text-muted">⏳ Carregando...</div>
                            </div>
                        </div>

                    </div>

                    <!-- ══════════════════════════════════════════════
                         TAB 4: EXPEDIÇÃO
                    ══════════════════════════════════════════════ -->
                    <div class="tab-pane fade" id="tab-expedicao" role="tabpanel">

                        <div class="d-flex justify-content-between align-items-center mb-4 flex-wrap gap-2">
                            <h5 class="mb-0">🚚 Expedição — Pedidos Prontos para Envio</h5>
                            <button class="btn btn-outline-dark btn-sm" onclick="printExpedicao()">🖨️ Imprimir Lista</button>
                        </div>

                        <!-- Filtro de prazo por cor -->
                        <div class="d-flex gap-2 mb-3 flex-wrap">
                            <button onclick="filterExpedicao('all')" class="btn btn-sm btn-dark">Todos</button>
                            <button onclick="filterExpedicao('atrasado')" class="btn btn-sm btn-danger">🔴 Atrasados</button>
                            <button onclick="filterExpedicao('critico')" class="btn btn-sm btn-warning text-dark">🟡 Crítico (≤2d)</button>
                            <button onclick="filterExpedicao('atencao')" class="btn btn-sm btn-info text-dark">🔵 Atenção (≤5d)</button>
                            <button onclick="filterExpedicao('normal')" class="btn btn-sm btn-success">🟢 No prazo</button>
                        </div>

                        <div id="expedicao-section">
                            <div class="text-center py-5 text-muted">⏳ Carregando...</div>
                        </div>

                    </div>

                    <!-- ══════════════════════════════════════════════
                         TAB 5: RELATÓRIO
                    ══════════════════════════════════════════════ -->
                    <div class="tab-pane fade" id="tab-relatorio" role="tabpanel">

                        <div class="d-flex justify-content-between align-items-center mb-4 flex-wrap gap-2">
                            <h5 class="mb-0">📋 Relatório de Produção</h5>
                            <div class="d-flex gap-2">
                                <button onclick="loadRelatorio(7)"  class="btn btn-sm btn-outline-primary">7 dias</button>
                                <button onclick="loadRelatorio(30)" class="btn btn-sm btn-outline-primary">30 dias</button>
                                <button onclick="printRelatorio()"  class="btn btn-sm btn-outline-dark">🖨️ Imprimir</button>
                            </div>
                        </div>

                        <div id="relatorio-section">
                            <div class="text-center py-5 text-muted">⏳ Selecione o período acima.</div>
                        </div>

                        <!-- Histórico de Finalizações -->
                        <div class="card border-0 shadow-sm mt-4">
                            <div class="card-header" style="background:linear-gradient(135deg,#3b0764,#7c3aed)!important;">
                                <h5 class="mb-0">📜 Histórico de Finalizações (Mês)</h5>
                            </div>
                            <div class="card-body p-0" id="production-history-section">
                                <div class="text-center py-4 text-muted">⏳ Carregando...</div>
                            </div>
                        </div>

                    </div>

                    <!-- ══════════════════════════════════════════════
                         TAB 6: FICHA TÉCNICA
                    ══════════════════════════════════════════════ -->
                    <div class="tab-pane fade" id="tab-ficha" role="tabpanel">

                        <div class="d-flex justify-content-between align-items-center mb-4 flex-wrap gap-2">
                            <h5 class="mb-0">🔧 Ficha Técnica de Produtos</h5>
                            <button class="btn btn-sm btn-outline-dark" onclick="printFicha()">🖨️ Imprimir</button>
                        </div>

                        <div class="card border-0 shadow-sm">
                            <div class="card-header" style="background:linear-gradient(135deg,#01010d,#374151)!important;">
                                <h5 class="mb-0">📐 Cadeira SW — Lista Técnica de Insumos</h5>
                                <small class="text-white-50">Componentes por unidade produzida</small>
                            </div>
                            <div class="card-body p-0" id="ficha-section">
                                <div class="text-center py-4 text-muted">⏳ Carregando...</div>
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
        let stagesChart = null;
        let prodBarChart = null;
        let deltaChart = null;
        let _currentSetor = 'todos';
        let _boardDataRaw = null;  // raw data for filtering

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

        /* Atualizar KPIs com Animação */
        /* Atualizar KPIs — V5.0: crescimento vs média mensal ÷ 20 dias úteis */
        function updateKpis(dSalesStats) {
            const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
            set('kpi-daily',    dSalesStats.daily_count   ?? dSalesStats.daily   ?? 0);
            set('kpi-weekly',   dSalesStats.weekly_count  ?? dSalesStats.weekly  ?? 0);
            set('kpi-historic', dSalesStats.monthly_count ?? dSalesStats.monthly ?? 0);
            const lu = document.getElementById('last-recalculated');
            if (lu) lu.textContent = '⏱ ' + formatDateTime(dSalesStats.last_update);

            const gr    = dSalesStats.growth  || 0;
            const last7 = dSalesStats.last_7  || 0;
            const ritmo = dSalesStats.ritmo_7d || 0;

            let trendIcon = '📊 Estável'; let trendColor = '#6366f1';
            if (gr > 10)       { trendIcon = '📈 Acelerando'; trendColor = '#10b981'; }
            else if (gr > 0)   { trendIcon = '📈 Subindo';    trendColor = '#10b981'; }
            else if (gr < -10) { trendIcon = '📉 Caindo';     trendColor = '#ef4444'; }
            else if (gr < 0)   { trendIcon = '📉 Abaixo';     trendColor = '#f59e0b'; }

            set('trend-indicator', trendIcon);
            set('growth-weekly', (gr > 0 ? '+' : '') + gr.toFixed(1) + '%');
            const tEl = document.getElementById('trend-indicator');
            const gEl = document.getElementById('growth-weekly');
            if (tEl) tEl.style.color = trendColor;
            if (gEl) gEl.style.color = trendColor;
            const ttEl = document.getElementById('growth-tooltip');
            if (ttEl) ttEl.textContent = ritmo > 0
                ? `7d: ${last7} pedidos · Ritmo esperado: ${ritmo.toFixed(1)} (mês÷20×7)`
                : '';

            if (isAuthenticated) _loadKpiOrderNumbers();
            document.querySelectorAll('.kpi-card').forEach(c => {
                c.classList.add('updating');
                setTimeout(() => c.classList.remove('updating'), 600);
            });
        }

        /* Atualiza KPIs de produção (3 etapas) */
        function updateProductionKpis(boardData) {
            const waiting = (boardData.waiting || []).length;
            const inprod  = (boardData.in_production || []).length + (boardData.orphan_timers || []).length;
            const done    = (boardData.done || []).length;
            const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
            set('kpi-waiting', waiting);
            set('kpi-inprod',  inprod);
            set('kpi-done',    done);
            // Números dos pedidos em cada etapa
            const fmtNums = arr => arr.map(i => '#'+(i.pedido_numero||i.order_id||'')).filter(Boolean).join(' · ');
            const dw = document.getElementById('kpi-waiting-nums');
            const di = document.getElementById('kpi-inprod-nums');
            const dd = document.getElementById('kpi-done-nums');
            if (dw) dw.textContent = fmtNums(boardData.waiting||[]);
            if (di) di.textContent = fmtNums([...(boardData.in_production||[]),...(boardData.orphan_timers||[])]);
            if (dd) dd.textContent = fmtNums((boardData.done||[]).slice(-5));
        }


        async function _loadKpiOrderNumbers() {
            try {
                const res = await fetch('/api/sales/orders-summary');
                if (!res.ok) return;
                const data = await res.json();

                // Usa data local (sem timezone offset) para comparar corretamente
                const now = new Date();
                const todayStr = now.toISOString().slice(0, 10); // 'YYYY-MM-DD'
                const weekAgo = new Date(now); weekAgo.setDate(now.getDate() - 7);
                const weekAgoStr = weekAgo.toISOString().slice(0, 10);
                const monthStr = now.toISOString().slice(0, 7); // 'YYYY-MM'

                const dailyNums = [], weeklyNums = [], monthlyNums = [];
                (data.orders || []).forEach(o => {
                    // data pode vir como 'YYYY-MM-DD' ou 'YYYY-MM-DD HH:MM'
                    const dateStr = (o.data || '').slice(0, 10);
                    const num = '#' + (o.numero || o.id);
                    if (dateStr === todayStr) dailyNums.push(num);
                    if (dateStr >= weekAgoStr) weeklyNums.push(num);
                    if (dateStr.startsWith(monthStr)) monthlyNums.push(num);
                });

                // Mostra todos (sem limite de 10) — scroll no div
                const fmt = (arr) => arr.length === 0 ? '—' : arr.join(' · ');

                const dEl = document.getElementById('kpi-daily-orders');
                const wEl = document.getElementById('kpi-weekly-orders');
                const mEl = document.getElementById('kpi-monthly-orders');
                if (dEl) dEl.textContent = fmt(dailyNums);
                if (wEl) wEl.textContent = fmt(weeklyNums);
                if (mEl) mEl.textContent = fmt(monthlyNums);
            } catch(e) { /* silencioso */ }
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
                                        <div class="text-center mt-2" style="background:#1e293b;border-radius:8px;padding:8px 12px;">
                                            <span style="font-size:.75rem;color:#94a3b8;">
                                                ⚠️ Controle exclusivo por leitura de código de barras
                                            </span>
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
                                <span style="font-size:.75rem;color:#64748b;">Apenas leitura de código conclui a produção</span>
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
                const tab = document.getElementById('tab-insumos');
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
                    // Recarrega o board para remover o item da lista in_production
                    if (typeof loadProductionBoard === 'function') {
                        setTimeout(() => loadProductionBoard(), 800);
                    }

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

        let _boardTick = null;
        let _boardPoll = null;
        let _boardTimerState = {};
        let _currentBoardTab = 'waiting';   // aba activa
        let _boardData = null;               // último snapshot
        // Set de item_keys já lidos nesta sessão — anti-duplicação por aba
        // Formato: "itemKey:etapa" onde etapa = 'waiting'|'inprod'
        const _scannedThisSession = new Set();

        /* Troca a sub-aba visível */
        function switchBoardTab(tab) {
            _currentBoardTab = tab;
            ['waiting','inprod','done'].forEach(t => {
                const panel = document.getElementById('board-' + t);
                const btn   = document.getElementById('tab-' + t + '-btn');
                if (!panel || !btn) return;
                const isActive = t === tab;
                panel.style.display = isActive ? 'block' : 'none';
                btn.classList.toggle('active-board-tab', isActive);
                btn.style.opacity = isActive ? '1' : '0.65';
                btn.style.boxShadow = isActive ? '0 4px 14px rgba(0,0,0,.25)' : 'none';
            });
            // Mostra buscador e setor tabs só quando Produzindo está ativo
            const sw = document.getElementById('setor-tabs-wrap');
            const si = document.getElementById('search-inprod-wrap');
            const isInprod = tab === 'inprod';
            if (sw) sw.style.display = isInprod ? 'block' : 'none';
            if (si) si.style.display = isInprod ? 'block' : 'none';
            // Limpa busca ao trocar aba
            if (!isInprod) {
                const inp = document.getElementById('search-inprod');
                if (inp) inp.value = '';
                if (_boardDataRaw) _boardData = _boardDataRaw;
            }
            if (_boardData) _renderCurrentTab();
        }

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

        async function loadPendingOrders() { await loadProductionBoard(); }

        async function loadProductionBoard() {
            try {
                const data = await fetch('/api/production/board').then(r => r.json());
                _boardDataRaw = data;
                _boardData = data;
                renderProductionBoard(data);
                updateProductionKpis(data);
                _updateDashboardStagesChart(data);
            } catch(e) {
                console.error('Erro ao carregar board:', e);
            }
        }

        function renderProductionBoard(data) {
            const waiting  = data.waiting       || [];
            const inProd   = data.in_production  || [];
            const orphans  = data.orphan_timers  || [];
            const done     = data.done           || [];
            const serverTime = data.server_time  || (Date.now() / 1000);

            // Combina in_production + orphans
            const allInProd = [...inProd, ...orphans];

            // Atualiza contadores nas badges
            const wb = document.getElementById('waiting-count-badge');
            const ib = document.getElementById('inprod-count-badge');
            const db = document.getElementById('done-count-badge');
            if (wb) wb.textContent = waiting.length;
            if (ib) ib.textContent = allInProd.length;
            if (db) db.textContent = done.length;

            // Alerta de urgência
            const atrasados = waiting.filter(i => i.urgencia === 'atrasado').length;
            if (atrasados > 0 && !window._alertedAtrasados) {
                window._alertedAtrasados = true;
                showToast('🚨 ATENÇÃO', `${atrasados} pedido(s) em ATRASO!`, 'danger');
            }

            // Para ticker anterior
            if (_boardTick) { clearInterval(_boardTick); _boardTick = null; }
            _boardTimerState = {};

            // Salva para o ticker
            allInProd.forEach(item => {
                const tkey = item.timer_key || item.nome || item.nome_original || '';
                if (tkey) {
                    _boardTimerState[tkey] = {
                        base: item.tempo_decorrido || 0,
                        startedAt: Date.now() / 1000,
                        estado: item.estado || 'paused',
                        serverTime
                    };
                }
            });

            _boardData = { waiting, allInProd, done, serverTime };
            _renderCurrentTab();

            // Ticker ao vivo para produzindo
            _boardTick = setInterval(() => {
                Object.entries(_boardTimerState).forEach(([tkey, s]) => {
                    if (s.estado !== 'running') return;
                    const elapsed = s.base + (Date.now() / 1000 - s.startedAt);
                    const displayNome = tkey.includes('||') ? tkey.split('||')[0] : tkey;
                    const safeId = displayNome.replace(/[^a-zA-Z0-9]/g, '_');
                    const el = document.getElementById('btimer_' + safeId);
                    if (el) el.textContent = formatSeconds(Math.floor(elapsed));
                });
            }, 1000);
        }

        function _renderCurrentTab() {
            if (!_boardData) return;
            const { waiting, allInProd, done } = _boardData;
            switch (_currentBoardTab) {
                case 'waiting': _renderWaiting(waiting); break;
                case 'inprod':  _renderInProd(allInProd); break;
                case 'done':    _renderDone(done); break;
            }
        }

        /* ── ABA EM ESPERA: cards com barcode, leitura única por pedido ── */
        function _renderWaiting(items) {
            const div = document.getElementById('board-waiting');
            if (!div) return;
            if (items.length === 0) {
                div.innerHTML = `<div class="text-center py-5 text-muted">
                    <div style="font-size:3rem;opacity:.3;">📭</div>
                    <p class="mt-2">Nenhum pedido em espera.</p>
                    <button class="btn btn-sm btn-outline-primary mt-2" onclick="syncAndRefreshPending()">🔄 Sincronizar Bling</button>
                </div>`;
                return;
            }

            // Agrupa por produto
            const grupos = {};
            items.forEach(item => {
                const key = (item.nome || item.nome_original || 'N/D').toUpperCase();
                if (!grupos[key]) grupos[key] = { nome: item.nome || item.nome_original || 'N/D', items: [] };
                grupos[key].items.push(item);
            });

            // Ordena grupos: urgentes primeiro
            const gruposArr = Object.values(grupos).sort((a, b) => {
                const urgMap = {atrasado:0,critico:1,atencao:2,normal:3};
                const urgA = Math.min(...a.items.map(i => urgMap[i.urgencia||'normal']));
                const urgB = Math.min(...b.items.map(i => urgMap[i.urgencia||'normal']));
                return urgA !== urgB ? urgA - urgB : a.nome.localeCompare(b.nome);
            });

            let html = '<div class="p-3">';
            gruposArr.forEach(grupo => {
                const hasUrgent = grupo.items.some(i => ['atrasado','critico'].includes(i.urgencia||'normal'));
                html += `<div class="mb-4">
                    <div class="d-flex align-items-center gap-2 mb-2">
                        <span style="font-family:'Bebas Neue',sans-serif;font-size:1.1rem;letter-spacing:.04em;color:${hasUrgent?'#b91c1c':'#01010d'};">
                            ${hasUrgent?'🔴':'🟡'} ${escapeHtml(grupo.nome)}
                        </span>
                        <span class="badge" style="background:${hasUrgent?'#ef4444':'#ffb600'};color:${hasUrgent?'#fff':'#000'};">${grupo.items.length} un.</span>
                    </div>
                    <div class="row g-3">`;

                grupo.items.forEach(item => {
                    const ikey = item.item_key || '';
                    // Fallback em cascata: ordem_producao → pedido_numero → order_id → item_key
                    // Filtra valores inválidos: 0, '0', null, undefined, ''
                    const _rawOp = item.ordem_producao || item.pedido_numero || item.order_id || '';
                    const op = (_rawOp && String(_rawOp) !== '0') ? String(_rawOp) : '';
                    const svgId = 'bcw_' + ikey.replace(/[^a-z0-9]/gi,'_');
                    const urg = item.urgencia || 'normal';
                    const dias = item.dias_restantes;
                    const jaLido = _scannedThisSession.has(ikey + ':waiting');

                    let prazoBadge = '';
                    if (dias !== null && dias !== undefined) {
                        if (urg==='atrasado') prazoBadge = `<span class="badge bg-danger" style="font-size:.65rem;">⚠️ ${Math.abs(dias)}d ATRASO</span>`;
                        else if (urg==='critico') prazoBadge = `<span class="badge bg-danger" style="font-size:.65rem;">🔥 ${dias===0?'HOJE':dias+'d'}</span>`;
                        else if (urg==='atencao') prazoBadge = `<span class="badge bg-warning text-dark" style="font-size:.65rem;">⏰ ${dias}d</span>`;
                        else prazoBadge = `<span class="badge bg-light text-dark border" style="font-size:.65rem;">${dias}d</span>`;
                    }

                    html += `<div class="col-sm-6 col-lg-4 col-xl-3">
                        <div class="bc-card ${urg==='atrasado'||urg==='critico'?'urgente':urg==='atencao'?'atencao':''}">
                            ${prazoBadge ? `<div class="bc-prazo">${prazoBadge}</div>` : ''}
                            <div class="bc-nome">${escapeHtml(item.nome||item.nome_original||'N/D')}</div>
                            ${item.base||item.cor ? `<div style="font-size:.68rem;color:#6b7280;margin-bottom:8px;">${escapeHtml((item.base||'')+(item.base&&item.cor?' / ':'')+( item.cor||''))}</div>` : ''}
                            <div class="bc-num">${op ? 'Ped. #'+escapeHtml(op) : '<span style="color:#ef4444;font-size:.75rem;">⚠️ Sem número — remova e sincronize</span>'}</div>
                            ${item.qtd_total > 1 ? `<div style="font-size:.66rem;color:#6366f1;font-weight:700;margin-bottom:4px;">📦 Unidade ${(item.qtd_unit_idx||0)+1} de ${item.qtd_total}</div>` : ''}
                            <div class="bc-svg-wrap my-2 text-center">
                                <svg id="${svgId}"></svg>
                            </div>
                            <div class="bc-meta">${escapeHtml(item.cliente||'')} ${item.pedido_data ? '· '+new Date(item.pedido_data).toLocaleDateString('pt-BR') : ''}</div>
                            ${jaLido
                                ? `<div class="bc-lido-overlay">✅ CÓDIGO LIDO<br><small style="font-weight:400;font-size:.72rem;">Indo para Produzindo...</small></div>`
                                : `<div class="mt-2 d-flex gap-2">
                                    <div class="text-center w-100 py-1" style="background:#f0fdf4;border:1px dashed #86efac;border-radius:8px;font-size:.72rem;color:#16a34a;font-weight:600;">
                                        📷 Bipe a etiqueta para iniciar
                                    </div>
                                    <button class="btn btn-outline-primary btn-sm flex-shrink-0" onclick="printItemLabel('${escapeHtml(item.scan_code||ikey)}','${escapeHtml(item.nome||item.nome_original||'')}','${escapeHtml(String(op||''))}','${escapeHtml(item.cliente||'')}','${item.qtd_total>1?escapeHtml('Unidade '+((item.qtd_unit_idx||0)+1)+' de '+item.qtd_total):''}')" title="Imprimir etiqueta deste produto">🏷️</button>
                                    <button class="btn btn-outline-secondary btn-sm flex-shrink-0" data-dkey="${ikey}" onclick="dismissPendingOrder(this.dataset.dkey,event)" title="Remover">✕</button>
                                   </div>`
                            }
                        </div>
                    </div>`;
                });

                html += `</div></div>`;
            });
            html += '</div>';
            div.innerHTML = html;

            // Renderiza barcodes — usa scan_code (8 chars hex, único por produto individual)
            // O scan_code é o hash SHA256[:8] do item_key — curto o suficiente para
            // imprimir nitidamente em etiqueta 62x40mm e ler com qualquer scanner.
            items.forEach(item => {
                const ikey = item.item_key || '';
                const code = item.scan_code || ikey;  // fallback para itens legados sem scan_code
                const svgId = 'bcw_' + ikey.replace(/[^a-z0-9]/gi,'_');
                renderItemBarcode(document.getElementById(svgId), code);
            });
        }

        /** Renderiza um barcode CODE128 contido no card.
         *  scan_code tem sempre 8 chars hex (ex: "A3F2C891") — comprimento fixo,
         *  então a largura de barra pode ser constante e confortável.
         *  displayValue:true mostra o código abaixo das barras, permitindo
         *  conferência visual e digitação manual como fallback se o scanner falhar. */
        function renderItemBarcode(svgEl, code) {
            if (!svgEl || !code) return;
            try {
                JsBarcode(svgEl, code, {
                    format: 'CODE128', width: 1.8, height: 46,
                    displayValue: true, fontSize: 10, textMargin: 2,
                    margin: 2, background: '#fff', lineColor: '#000'
                });
                // Força viewBox para que width:100% do CSS escale proporcionalmente
                const w = svgEl.getAttribute('width');
                const h = svgEl.getAttribute('height');
                if (w && h) {
                    svgEl.setAttribute('viewBox', `0 0 ${w} ${h}`);
                    svgEl.removeAttribute('width');
                    svgEl.removeAttribute('height');
                }
            } catch(e) {
                svgEl.innerHTML = `<text font-size="9" fill="#ef4444">Código inválido</text>`;
            }
        }

        /* Remove pedido da fila (botão ✕) */
        async function dismissPendingOrder(itemKey, evt) {
            if (evt) { evt.stopPropagation(); evt.preventDefault(); }
            if (!itemKey) return;
            if (!confirm('Remover este pedido da fila de produção?')) return;
            try {
                await fetch('/api/pending-orders/dismiss', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ item_key: itemKey })
                });
                showToast('Removido', 'Pedido removido da fila.', 'info');
                await loadProductionBoard();
            } catch(e) {
                showToast('Erro', 'Falha ao remover pedido.', 'danger');
            }
        }

        /* ── ABA PRODUZINDO: cards com barcode + timer, leitura única ── */
        function _renderInProd(items) {
            const div = document.getElementById('board-inprod');
            if (!div) return;

            // Filtra por setor usando classificação semântica
            let filtered = items;
            if (_currentSetor === 'marcenaria') {
                filtered = items.filter(i => _classifySetor(i.nome||i.nome_original||'') === 'marcenaria');
            } else if (_currentSetor === 'tapecaria') {
                filtered = items.filter(i => _classifySetor(i.nome||i.nome_original||'') === 'tapecaria');
            }

            if (filtered.length === 0) {
                div.innerHTML = `<div class="text-center py-5 text-muted">
                    <div style="font-size:3rem;opacity:.3;">⚙️</div>
                    <p class="mt-2">${items.length === 0 ? 'Nenhum item em produção.' : 'Nenhum item para este setor.'}</p>
                </div>`;
                return;
            }

            let html = '<div class="p-3 row g-3">';
            filtered.forEach(item => {
                const ikey  = item.item_key || '';
                const scanCode = item.scan_code || ikey;
                const nome  = item.nome || item.nome_original || item.produto || 'N/D';
                const _rawOp3 = item.pedido_numero || item.ordem_producao || item.order_id || '';
                const op    = (_rawOp3 && String(_rawOp3) !== '0') ? String(_rawOp3) : '';
                const opInterna = item.ordem_producao ? String(item.ordem_producao) : '';
                const cliente = item.cliente || '';
                const tkey  = item.timer_key || nome;
                const safeId = nome.replace(/[^a-zA-Z0-9]/g,'_');
                const svgId  = 'bci_' + ikey.replace(/[^a-z0-9]/gi,'_');
                const elapsed = item.tempo_decorrido || 0;
                const estado  = item.estado || 'paused';
                const jaLido  = _scannedThisSession.has(ikey + ':inprod');
                const urg = item.urgencia || 'normal';
                const dias = item.dias_restantes;

                let prazoBadge = '';
                if (dias !== null && dias !== undefined) {
                    if (urg==='atrasado') prazoBadge = `<span class="badge bg-danger" style="font-size:.65rem;">⚠️ ${Math.abs(dias)}d ATRASO</span>`;
                    else if (urg==='critico') prazoBadge = `<span class="badge bg-danger" style="font-size:.65rem;">🔥 ${dias===0?'HOJE':dias+'d'}</span>`;
                    else if (urg==='atencao') prazoBadge = `<span class="badge bg-warning text-dark" style="font-size:.65rem;">⏰ ${dias}d</span>`;
                }

                // Tempo em dias/horas — alerta visual se exceder 24h (provável bug/item travado)
                const TEMPO_ANOMALO = 86400; // 24h
                const isAnomalo = elapsed > TEMPO_ANOMALO;
                const elapsedFmt = elapsed > 86400
                    ? (elapsed/86400).toFixed(1)+'d'
                    : formatSeconds(elapsed);

                // Data prevista formatada
                const dataEntFmt = item.data_entrega ? (() => { try { return new Date(item.data_entrega).toLocaleDateString('pt-BR'); } catch { return item.data_entrega; } })() : '';

                html += `<div class="col-sm-6 col-lg-4 col-xl-3">
                    <div class="bc-card inprod" onclick="openOPModal('${escapeHtml(op)}','${escapeHtml(nome)}','${escapeHtml(scanCode)}','${escapeHtml(opInterna)}','${escapeHtml(cliente)}')" style="cursor:pointer;">
                        ${prazoBadge ? `<div class="bc-prazo">${prazoBadge}</div>` : ''}
                        <div class="bc-nome">${escapeHtml(nome)}</div>
                        <div class="bc-num">${op ? 'Ped. #'+escapeHtml(op) : '<span style="color:#ef4444;font-size:.75rem;">⚠️ Sem número</span>'}</div>
                        ${dataEntFmt ? `<div style="font-size:.65rem;color:#6b7280;margin-bottom:4px;">📅 Previsto: ${dataEntFmt}</div>` : ''}
                        <div class="bc-svg-wrap my-2 text-center">
                            <svg id="${svgId}"></svg>
                        </div>
                        <!-- Timer ao vivo -->
                        <div class="text-center my-2">
                            <span id="btimer_${safeId}" class="font-monospace fw-bold" style="font-size:1.5rem;color:${isAnomalo?'#ef4444':'#10b981'};">${elapsedFmt}</span>
                            <div>
                                <span class="badge ${estado==='running'?'bg-success':'bg-warning text-dark'}" style="${estado==='running'?'animation:pulse-animation 1.5s infinite;':''}">
                                    ${estado==='running'?'🟢 PRODUZINDO':'⏸ PAUSADO'}
                                </span>
                            </div>
                            ${isAnomalo ? `<div style="font-size:.65rem;color:#ef4444;font-weight:700;margin-top:4px;">⚠️ Tempo anômalo — possível item travado</div>` : ''}
                        </div>
                        <div class="bc-meta">${escapeHtml(item.cliente||'')} ${item.pedido_data?'· '+new Date(item.pedido_data).toLocaleDateString('pt-BR'):''}</div>
                        ${jaLido
                            ? `<div class="bc-lido-overlay">✅ CONCLUÍDO!<br><small style="font-weight:400;font-size:.72rem;">Registrado com sucesso</small></div>`
                            : `<div class="mt-2 d-flex gap-2" onclick="event.stopPropagation()">
                                <div class="text-center w-100 py-1" style="background:#fef2f2;border:1px dashed #fca5a5;border-radius:8px;font-size:.72rem;color:#dc2626;font-weight:600;">
                                    📷 Bipe a etiqueta para avançar
                                </div>
                                <button class="btn btn-outline-primary btn-sm flex-shrink-0" onclick="event.stopPropagation();printItemLabel('${escapeHtml(scanCode)}','${escapeHtml(nome)}','${escapeHtml(op)}','${escapeHtml(cliente)}')" title="Reimprimir etiqueta deste produto">🏷️</button>
                               </div>`
                        }
                    </div>
                </div>`;
            });
            html += '</div>';
            div.innerHTML = html;

            // Renderiza barcodes — usa scan_code (8 chars hex, único por produto individual)
            filtered.forEach(item => {
                const ikey = item.item_key || '';
                const code = item.scan_code || ikey;
                const svgId = 'bci_' + ikey.replace(/[^a-z0-9]/gi,'_');
                renderItemBarcode(document.getElementById(svgId), code);
            });
        }

        /* scanOrStartWaiting e scanOrFinishInProd removidos — produção avança exclusivamente via /api/barcode/scan */

        /* ── ABA CONCLUÍDOS: tabela simples ── */
        function _renderDone(items) {
            const div = document.getElementById('board-done');
            if (!div) return;
            if (items.length === 0) {
                div.innerHTML = `<div class="text-center py-5 text-muted">
                    <div style="font-size:3rem;opacity:.3;">✅</div>
                    <p class="mt-2">Nenhum item concluído este mês.</p>
                </div>`;
                return;
            }
            let html = `<div class="table-responsive">
                <table class="table table-sm align-middle mb-0">
                    <thead class="table-success">
                        <tr>
                            <th class="ps-3">Produto</th>
                            <th>Base / Cor</th>
                            <th>#Pedido / OP</th>
                            <th>Cliente</th>
                            <th class="text-center">Tempo</th>
                            <th class="text-center">Concluído em</th>
                        </tr>
                    </thead>
                    <tbody>`;
            items.slice().reverse().forEach(item => {
                const nome   = escapeHtml(item.nome || item.nome_original || 'N/D');
                const baseCor = escapeHtml(((item.base||'')+(item.base&&item.cor?' / ':'')+( item.cor||''))||'—');
                const op     = escapeHtml(item.ordem_producao || item.pedido_numero || item.order_id || '—');
                const fin    = item.finished_at ? new Date(item.finished_at).toLocaleString('pt-BR') : '—';
                const tempo  = item.tempo_producao
                    ? (() => {
                        const tp = item.tempo_producao;
                        const fmt = tp > 86400
                            ? `<span class="fw-bold text-success">${(tp/86400).toFixed(2)}d</span>`
                            : tp > 3600
                                ? `<span class="font-monospace fw-bold text-success">${Math.floor(tp/3600)}h${Math.floor((tp%3600)/60)}m</span>`
                                : `<span class="font-monospace fw-bold text-success">${Math.floor(tp/60)}m</span>`;
                        return fmt;
                    })()
                    : '<span class="text-muted">—</span>';
                html += `<tr class="table-success">
                    <td class="ps-3 fw-bold text-success">${nome}</td>
                    <td class="text-muted small">${baseCor}</td>
                    <td class="text-muted small">#${op}</td>
                    <td class="text-muted small">${escapeHtml(item.cliente||'—')}</td>
                    <td class="text-center">${tempo}</td>
                    <td class="text-center"><small class="text-muted">${fin}</small></td>
                </tr>`;
            });
            html += `</tbody></table></div>`;
            div.innerHTML = html;
        }

        /* Auto-registra componentes quando inicia produção via botão */
        async function _autoRegisterComponents(nomeProduto, itemKey) {
            if (!nomeProduto.toUpperCase().includes('CADEIRA')) return;
            // O backend já registra via sync_from_orders — aqui é redundância
            // para garantir no caso de pedidos antigos sem registro automático
            const consumoKey = 'scan_btn_' + itemKey;
            try {
                for (const comp of RECIPE_CADEIRA) {
                    await fetch('/api/consumption/register', {
                        method:'POST', headers:{'Content-Type':'application/json'},
                        body: JSON.stringify({ component_name: comp.nome, qty: comp.qtd, unit: comp.un, product_name: consumoKey, checked: true })
                    });
                    break; // faz só 1 para checar duplicação — o backend cuida do resto
                }
            } catch(e) { /* silencioso */ }
        }

        /** Renderiza mini-barcodes reais (JsBarcode) nas células da tabela */
        function _renderBarcodes() {
            document.querySelectorAll('svg[id^="bc_"]').forEach(svg => {
                const wrap = svg.closest('[onclick]');
                if (!wrap) return;
                const onclk = wrap.getAttribute('onclick') || '';
                const m = onclk.match(/openOPModal..([^']+)/);
                if (!m) return;
                const val = m[1];
                if (!val || svg.children.length > 0) return;
                try {
                    JsBarcode(svg, val, { format:'CODE128', width:1.2, height:24, displayValue:false, margin:2, background:'#ffffff', lineColor:'#000000' });
                } catch(e) {
                    svg.innerHTML = `<text x="0" y="12" font-size="9" font-family="monospace">${val}</text>`;
                }
            });
        }


        /** Modal para ver e imprimir a Ordem de Produção do Bling — com barcode real */
        function openOPModal(pedidoNum, nomeProduto, itemKey, ordemProducao, clienteNome) {
            // itemKey = código real lido pelo scanner (único por produto individual)
            // pedidoNum = número do pedido Bling (informativo, pode repetir entre produtos)
            const codigoBarras = String(itemKey || pedidoNum || ordemProducao || '');

            const existing = document.getElementById('opModal');
            if (existing) { bootstrap.Modal.getInstance(existing)?.hide(); existing.remove(); }

            const modalHtml = `
            <div class="modal fade" id="opModal" tabindex="-1">
                <div class="modal-dialog modal-dialog-centered" style="max-width:420px;">
                    <div class="modal-content border-0 shadow-lg">
                        <div class="modal-header" style="background:#01010d;color:#fff;border-bottom:3px solid #ffb600;">
                            <h5 class="modal-title" style="font-family:'Bebas Neue',sans-serif;letter-spacing:.06em;">
                                📋 Ordem de Produção
                            </h5>
                            <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                        </div>
                        <div class="modal-body text-center" style="background:#f9f9f7;padding:24px;">
                            <p class="text-muted small mb-1" style="font-size:0.78rem;">${escapeHtml(nomeProduto)}</p>
                            ${clienteNome ? `<p class="text-muted mb-1" style="font-size:0.72rem;">Cliente: ${escapeHtml(clienteNome)}</p>` : ''}
                            <h4 class="fw-bold font-monospace mb-1" style="color:#01010d;letter-spacing:.06em;">
                                Ped. #${escapeHtml(String(pedidoNum||''))}
                            </h4>
                            ${ordemProducao ? `<div style="font-size:.72rem;color:#94a3b8;margin-bottom:12px;">OP Interna: ${escapeHtml(String(ordemProducao))}</div>` : ''}

                            <!-- Barcode real via JsBarcode (código do produto individual) -->
                            <div id="op-barcode-wrap" style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:20px 16px 12px;display:inline-block;margin-bottom:8px;min-width:280px;">
                                <svg id="opBarcodeReal"></svg>
                            </div>
                            <div style="font-size:.65rem;color:#94a3b8;margin-bottom:12px;">Código deste produto — pedidos com vários itens têm um código por item</div>

                            <!-- Status do pedido -->
                            <div id="op-status-box" class="mt-2 mb-1"></div>

                            <p class="text-muted mb-0" style="font-size:0.72rem;">
                                Scanner: aponte para o código acima.<br>
                                <strong>1ª leitura</strong> = Marcenaria · <strong>2ª</strong> = Tapeçaria · <strong>3ª</strong> = Concluído
                            </p>
                        </div>
                        <div class="modal-footer bg-white justify-content-between" style="border-top:1px solid #f0f0f0;">
                            <button class="btn btn-outline-secondary btn-sm" data-bs-dismiss="modal">Fechar</button>
                            <div class="d-flex gap-2">
                                <button class="btn btn-outline-primary btn-sm fw-bold" onclick="printItemLabel('${escapeHtml(codigoBarras)}', '${escapeHtml(nomeProduto)}', '${escapeHtml(String(pedidoNum||''))}', '${escapeHtml(clienteNome||'')}')">
                                    🏷️ Etiqueta
                                </button>
                                <button class="btn btn-primary btn-sm fw-bold" onclick="printOP('${escapeHtml(String(pedidoNum||''))}', '${escapeHtml(nomeProduto)}', '${escapeHtml(String(ordemProducao||''))}', '${escapeHtml(clienteNome||'')}')">
                                    🖨️ Folha A4
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>`;

            document.body.insertAdjacentHTML('beforeend', modalHtml);
            const modal = new bootstrap.Modal(document.getElementById('opModal'));
            modal.show();

            // Renderiza barcode real com JsBarcode (Code128 — lido por qualquer scanner)
            setTimeout(() => {
                if (!codigoBarras) {
                    document.getElementById('op-barcode-wrap').innerHTML =
                        `<div style="color:#ef4444;font-size:.85rem;">⚠️ Sem código identificador — remova este item e sincronize.</div>`;
                } else {
                    try {
                        JsBarcode('#opBarcodeReal', codigoBarras, {
                            format: 'CODE128', width: 1.8, height: 60,
                            displayValue: false, margin: 6,
                            background: '#ffffff', lineColor: '#000000',
                        });
                    } catch(e) {
                        document.getElementById('op-barcode-wrap').innerHTML =
                            `<div class="font-monospace fw-bold" style="font-size:1rem;letter-spacing:.1em;word-break:break-all;">${escapeHtml(codigoBarras)}</div>`;
                    }
                }
                // Mostra status atual do item (busca por item_key — sem ambiguidade)
                _updateOPStatusBox(itemKey || codigoBarras);
            }, 60);
        }

        async function _updateOPStatusBox(itemKeyLookup) {
            const box = document.getElementById('op-status-box');
            if (!box) return;
            try {
                const res = await fetch('/api/production/board');
                const data = await res.json();
                const all = [...(data.waiting||[]), ...(data.in_production||[]), ...(data.done||[])];
                const item = all.find(i => String(i.item_key||'') === String(itemKeyLookup));
                if (!item) {
                    box.innerHTML = `<span class="badge bg-secondary">Status desconhecido</span>`;
                    return;
                }
                const st      = item.status || 'waiting';
                const fsmStep = item.fsm_step || '';
                let inProdLabel = `<span class="badge bg-success" style="animation:pulse-animation 1.5s infinite;">⚙️ Em Produção</span>`;
                if (st === 'in_production') {
                    if (fsmStep === 'marcenaria')
                        inProdLabel = `<span class="badge bg-warning text-dark" style="animation:pulse-animation 1.5s infinite;">🪚 Marcenaria</span>`;
                    else if (fsmStep === 'tapecaria')
                        inProdLabel = `<span class="badge bg-info text-dark" style="animation:pulse-animation 1.5s infinite;">🧵 Tapeçaria</span>`;
                }
                const labels = {
                    waiting:      `<span class="badge bg-warning text-dark">⏳ Aguardando</span>`,
                    in_production: inProdLabel,
                    done:         `<span class="badge bg-primary">✅ Concluído</span>`,
                };
                box.innerHTML = labels[st] || `<span class="badge bg-secondary">${st}</span>`;
            } catch { box.innerHTML = ''; }
        }

        /** ════════════════════════════════════════════════════
         *  ETIQUETA INDIVIDUAL POR PRODUTO (formato adesivo 62x40mm)
         *  Barcode codifica o item_key — único por produto dentro
         *  do pedido. Cole na peça física; cada produto é bipado
         *  independentemente em sua própria etapa (Marcenaria,
         *  Tapeçaria, Concluído), mesmo que outros itens do mesmo
         *  pedido estejam em etapas diferentes.
         *  ════════════════════════════════════════════════════ */
        function printItemLabel(scanCode, nomeProduto, pedidoNum, clienteNome, unitLabel) {
            if (!scanCode) {
                alert('Item sem código de leitura — sincronize novamente.');
                return;
            }

            const nomeShort = (nomeProduto || '').length > 42
                ? nomeProduto.substring(0, 40) + '\u2026'
                : (nomeProduto || '');

            // Janela dedicada — evita conflito de CSS @page com a página principal
            // (que usa padding:20px para impressão de relatórios em A4)
            const printWin = window.open('', '_blank', 'width=400,height=300');
            if (!printWin) {
                alert('Bloqueou pop-up. Permita pop-ups para este site e tente novamente.');
                return;
            }

            const pedHtml = pedidoNum
                ? '<span style="font-size:7.5pt;font-weight:700;font-family:monospace;white-space:nowrap;">Ped.' + escapeHtml(String(pedidoNum)) + '</span>'
                : '';
            const unitHtml = unitLabel
                ? '<div style="font-size:6.5pt;color:#4f46e5;font-weight:700;">📦 ' + escapeHtml(unitLabel) + '</div>'
                : '';
            const cliHtml = clienteNome
                ? '<div style="font-size:6.5pt;color:#444;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;">' + escapeHtml(clienteNome) + '</div>'
                : '';

            printWin.document.write(
                '<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Etiqueta</title>'
                + '<style>'
                + '@page{size:62mm 40mm;margin:0}'
                + '*{margin:0;padding:0;box-sizing:border-box}'
                + 'html,body{width:62mm;height:40mm}'
                + 'body{font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center}'
                + '.lbl{width:62mm;height:40mm;padding:2.5mm 3mm;display:flex;flex-direction:column;justify-content:space-between;overflow:hidden}'
                + '.top{display:flex;justify-content:space-between;align-items:flex-start;gap:2mm}'
                + '.brand{font-size:7.5pt;font-weight:900;letter-spacing:.04em;white-space:nowrap}'
                + '.nome{font-size:8pt;font-weight:700;line-height:1.15;max-height:7mm;overflow:hidden}'
                + '.bc{width:100%;text-align:center;overflow:hidden;margin:.5mm 0}'
                + '.bc svg{width:100%;max-width:54mm;height:auto;display:block;margin:0 auto}'
                + '.foot{font-size:6pt;color:#666;text-align:center;letter-spacing:.03em}'
                + '</style></head><body>'
                + '<div class="lbl">'
                + '<div class="top"><span class="brand">SW M\u00d3VEIS MDF</span>' + pedHtml + '</div>'
                + '<div class="nome">' + escapeHtml(nomeShort) + '</div>'
                + unitHtml + cliHtml
                + '<div class="bc"><svg id="bcSvg"></svg></div>'
                + '<div class="foot">BIPE PARA AVAN\u00c7AR ETAPA</div>'
                + '</div>'
                + '<script src="https://cdn.jsdelivr.net/npm/jsbarcode@3.11.6/dist/JsBarcode.all.min.js"></script>'
                + '<script>'
                + '(function(){'
                + 'var code=' + JSON.stringify(scanCode) + ';'
                + 'try{'
                + 'JsBarcode("#bcSvg",code,{format:"CODE128",width:2,height:42,displayValue:true,fontSize:9,textMargin:1,margin:2,background:"#fff",lineColor:"#000"});'
                + '}catch(e){'
                + 'document.getElementById("bcSvg").outerHTML="<div style=\'font-family:monospace;font-weight:700;font-size:11pt;\'>"+code+"</div>";'
                + '}'
                + 'setTimeout(function(){window.focus();window.print();},220);'
                + '})()'
                + '</script>'
                + '</body></html>'
            );
            printWin.document.close();
            printWin.onafterprint = function() { printWin.close(); };
            setTimeout(function() { if (!printWin.closed) printWin.close(); }, 60000);
        }

        /** Impressão da OP em página limpa (sem travar) */
        function printOP(pedidoNum, nomeProduto, ordemProducao, clienteNome) {
            // pedidoNum  = número do pedido Bling (usado como barcode)
            // ordemProducao = número da OP interna (informativo)
            const codigoBarras = String(pedidoNum || ordemProducao || '');
            if (!codigoBarras) {
                alert('Este pedido não tem número identificador. Use o botão ✕ para removê-lo e sincronize novamente.');
                return;
            }

            const tempSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            tempSvg.id = '_printBcSvg';
            tempSvg.style.display = 'none';
            document.body.appendChild(tempSvg);

            try {
                JsBarcode('#_printBcSvg', codigoBarras, {
                    format: 'CODE128', width: 3, height: 90,
                    displayValue: true, fontSize: 16, fontOptions: 'bold',
                    margin: 8, background: '#ffffff', lineColor: '#000000'
                });
            } catch(e) { console.warn('JsBarcode error:', e); }

            const svgHtml = tempSvg.outerHTML;
            tempSvg.remove();

            const printContent = `
                <div style="font-family:Arial,sans-serif;text-align:center;padding:30px;">
                    <h2 style="letter-spacing:.05em;margin-bottom:4px;">SW Móveis MDF</h2>
                    <p style="color:#666;font-size:13px;margin-bottom:20px;">Ordem de Produção</p>
                    <div style="border:2px solid #000;border-radius:8px;padding:20px;display:inline-block;min-width:320px;max-width:420px;">
                        <div style="font-size:13px;color:#555;margin-bottom:6px;font-weight:bold;">${escapeHtml(nomeProduto||'')}</div>
                        ${clienteNome ? `<div style="font-size:11px;color:#888;margin-bottom:6px;">Cliente: ${escapeHtml(clienteNome)}</div>` : ''}
                        <div style="font-size:26px;font-weight:900;font-family:monospace;margin-bottom:4px;letter-spacing:.06em;">
                            Ped. #${escapeHtml(String(pedidoNum||''))}
                        </div>
                        ${ordemProducao ? `<div style="font-size:11px;color:#888;margin-bottom:12px;">OP Interna: ${escapeHtml(String(ordemProducao))}</div>` : ''}
                        <div style="margin:16px 0;">${svgHtml}</div>
                    </div>
                    <p style="color:#888;font-size:11px;margin-top:16px;">
                        1ª leitura = Marcenaria &nbsp;|&nbsp; 2ª leitura = Tapeçaria &nbsp;|&nbsp; 3ª leitura = Concluído
                    </p>
                </div>`;

            const printArea = document.getElementById('print-area');
            printArea.innerHTML = printContent;
            printArea.style.display = 'block';
            window.print();
            window.onafterprint = () => {
                printArea.innerHTML = '';
                printArea.style.display = 'none';
                window.onafterprint = null;
            };
        }

        /** ════════════════════════════════════════════════════
         *  LISTENER GLOBAL DE SCANNER DE CÓDIGO DE BARRAS
         *  Scanners USB emulam teclado: digitam o código + Enter
         *  MUITO rápido (cada tecla em <30ms). Humanos digitam mais
         *  lento. Usamos essa diferença de velocidade para distinguir
         *  uma bipagem real de digitação manual em um campo de busca —
         *  assim o scanner funciona mesmo que algum input esteja com
         *  foco (cenário comum: operador clicou na busca por engano).
         *
         *  1ª leitura (Em Espera)   → inicia/retoma produção
         *  2ª leitura (Produzindo)  → avança etapa ou conclui
         *  Anti-duplicação: debounce de 2s no frontend (espelha o
         *  backend) + _scannedThisSession por item+etapa.
         *  ════════════════════════════════════════════════════ */
        (function() {
            let _scanBuffer = '';
            let _scanTimer  = null;
            let _lastKeyTs  = 0;
            const _MIN_LEN  = 3;
            const _MAX_KEY_INTERVAL_MS = 35;  // scanners digitam bem mais rápido que isso
            const _MAX_KEY_INTERVAL_MS_FALLBACK = 80; // tolerância para scanners mais lentos/bluetooth
            let _fastKeyCount = 0; // quantas teclas consecutivas vieram "rápido"

            // Debounce local por código — evita 2 disparos da MESMA leitura em <2s
            // (espelha BARCODE_DEBOUNCE_SECONDS do backend, mas reage instantaneamente
            // sem precisar de round-trip de rede)
            const _localLastScan = {};
            const _LOCAL_DEBOUNCE_MS = 1800;

            const _indicator = document.getElementById('scanner-indicator');

            function _showIndicator(msg, color) {
                if (!_indicator) return;
                _indicator.textContent = '📡 ' + msg;
                _indicator.style.borderColor = color || '#ffb600';
                _indicator.style.color = color || '#ffb600';
                _indicator.classList.add('active');
                clearTimeout(_indicator._hideTimer);
                _indicator._hideTimer = setTimeout(() => _indicator.classList.remove('active'), 3500);
            }

            document.addEventListener('keydown', function(e) {
                const now = performance.now();
                const interval = now - _lastKeyTs;
                _lastKeyTs = now;

                const tag = document.activeElement?.tagName?.toLowerCase();
                const isEditable = tag === 'input' || tag === 'textarea' || tag === 'select';

                if (e.key === 'Enter') {
                    const code = _scanBuffer.trim();
                    const wasFast = _fastKeyCount >= Math.max(2, code.length - 1);
                    _scanBuffer = '';
                    _fastKeyCount = 0;
                    clearTimeout(_scanTimer);

                    if (code.length < _MIN_LEN) return;

                    // Se o foco está num campo editável, só processa como bipagem
                    // se a velocidade de digitação foi de scanner (rápida demais
                    // para ser humano). Caso contrário, deixa o Enter seguir o
                    // comportamento normal do input (ex: submeter busca).
                    if (isEditable && !wasFast) return;

                    // Se foi reconhecido como scanner mesmo em campo editável,
                    // evita que o Enter exiba sugestões/submeta o formulário
                    if (isEditable && wasFast) e.preventDefault();

                    _processScan(code);
                    return;
                }

                if (e.key.length === 1) {
                    // Conta como "tecla rápida" se o intervalo desde a tecla
                    // anterior for característico de scanner
                    if (interval > 0 && interval <= _MAX_KEY_INTERVAL_MS_FALLBACK) {
                        _fastKeyCount++;
                    } else {
                        _fastKeyCount = isEditable ? 0 : 1; // fora de input, não exige velocidade
                    }

                    _scanBuffer += e.key;
                    clearTimeout(_scanTimer);
                    _scanTimer = setTimeout(() => { _scanBuffer = ''; _fastKeyCount = 0; }, 400);
                }
            });

            async function _processScan(codigo) {
                if (!isAuthenticated) {
                    _showIndicator('Não autenticado', '#ef4444');
                    return;
                }

                // Debounce local — evita round-trip duplicado para a mesma leitura
                const nowMs = Date.now();
                const lastMs = _localLastScan[codigo] || 0;
                if ((nowMs - lastMs) < _LOCAL_DEBOUNCE_MS) {
                    _showIndicator('Leitura repetida ignorada', '#6b7280');
                    return;
                }
                _localLastScan[codigo] = nowMs;

                // Extrai prefixo de leitor do código (ex: "R2:2781" → reader "R2", codigo "2781")
                const _pfxMatch = codigo.match(/^(R[1-4]):(.+)$/i);
                const readerLabel = _pfxMatch ? ` [${_pfxMatch[1].toUpperCase()}]` : '';
                _showIndicator('Lendo: ' + codigo + readerLabel, '#ffb600');
                try {
                    const res = await fetch('/api/barcode/scan', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ codigo: codigo })
                    });
                    const result = await res.json();

                    if (res.status === 404) {
                        _showIndicator('Não encontrado: ' + codigo, '#ef4444');
                        showToast('Scanner', result.mensagem || 'Código não encontrado', 'warning');
                        return;
                    }

                    const acao   = result.acao;
                    const ikey   = result.item_key || '';
                    const nome   = result.nome || '';
                    const rLabel = result.reader_id ? ` [${result.reader_id}]` : '';

                    if (acao === 'ja_lido_etapa') {
                        _showIndicator('Já processado nesta etapa', '#6b7280');
                        showToast('Aviso', result.mensagem, 'warning');
                        return;
                    }

                    if (acao === 'processando' || acao === 'debounce') {
                        // Lock/debounce do backend pegou uma leitura duplicada/concorrente —
                        // não é erro, apenas confirma que a primeira leitura já está sendo tratada.
                        _showIndicator('Leitura já em andamento', '#6b7280');
                        return;
                    }

                    if (acao === 'iniciado' || acao === 'retomado') {
                        if (ikey) _scannedThisSession.add(ikey + ':waiting');
                        const fsm = result.fsm_step || '';
                        const isRetomado = acao === 'retomado';
                        let etiqLabel;
                        if (isRetomado) {
                            const stepMap = {marcenaria:'🪚 MARCENARIA RETOMADA', tapecaria:'🧵 TAPEÇARIA RETOMADA', mdf:'🔄 PRODUÇÃO RETOMADA'};
                            etiqLabel = stepMap[fsm] || '▶️ PRODUÇÃO RETOMADA';
                        } else {
                            etiqLabel = fsm === 'marcenaria' ? '🪚 MARCENARIA INICIADA' : '🚀 PRODUÇÃO INICIADA';
                        }
                        _showIndicator(etiqLabel + rLabel + ' — ' + nome, '#10b981');
                        showToast(etiqLabel, nome + ' · #' + (result.codigo || codigo), 'success');
                        await loadProductionBoard();
                        switchBoardTab('inprod');

                    } else if (acao === 'tapecaria') {
                        // Cadeira avançou Marcenaria → Tapeçaria
                        if (ikey) _scannedThisSession.add(ikey + ':marcenaria');
                        _showIndicator('🧵 TAPEÇARIA INICIADA' + rLabel + ' — ' + nome, '#6366f1');
                        showToast('🧵 Tapeçaria', nome + ' · #' + (result.codigo || codigo), 'success');
                        await loadProductionBoard();
                        switchBoardTab('inprod');

                    } else if (acao === 'concluido') {
                        if (ikey) _scannedThisSession.add(ikey + ':inprod');
                        _showIndicator('✅ CONCLUÍDO' + rLabel + ' — ' + nome, '#6366f1');
                        const tempoFmt = result.tempo_producao ? ' · ' + formatSeconds(result.tempo_producao) : '';
                        showToast('✅ Concluído!', nome + tempoFmt, 'success');
                        await loadProductionBoard();
                        await refreshComponentTab();
                        switchBoardTab('done');

                    } else if (acao === 'ja_concluido') {
                        _showIndicator('Já concluído', '#6b7280');
                        showToast('Info', result.mensagem, 'info');

                    } else if (acao === 'erro_fsm') {
                        _showIndicator('Erro de estado — contacte admin', '#ef4444');
                        showToast('Erro FSM', result.mensagem, 'danger');

                    } else {
                        _showIndicator(result.mensagem || 'Processado', '#ffb600');
                    }
                } catch(e) {
                    _showIndicator('Erro de comunicação', '#ef4444');
                    showToast('Erro', 'Falha ao processar leitura', 'danger');
                }
            }

            // Para testes no console: window._testScan('2781')
            window._testScan = _processScan;
        })();

        function updateComponentUsage(usageData) {
            if (usageData && usageData.history_production) renderProductionHistory(usageData.history_production);
        }

        async function refreshComponentTab() {
            try {
                const consumptionData = await fetchAPI('/api/consumption/summary');
                renderConsumptionTable(consumptionData);
            } catch(e) {
                const sec = document.getElementById('consumption-table-section');
                if (sec) sec.innerHTML = '<div class="alert alert-warning m-3">⚠️ Erro ao carregar consumo.</div>';
            }
            try {
                const histData = await fetchAPI('/api/components/usage');
                if (histData && histData.history_production) {
                    renderProductionHistory(histData.history_production);
                }
            } catch(e) { /* silencioso */ }
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

            function fmtTempo(secs) {
                if (!secs || secs <= 0) return '—';
                if (secs >= 86400) return (secs/86400).toFixed(2) + 'd';
                if (secs >= 3600)  return Math.floor(secs/3600) + 'h' + Math.floor((secs%3600)/60) + 'm';
                return Math.floor(secs/60) + 'min';
            }

            div.innerHTML = `<div class="table-responsive" style="max-height:340px;overflow-y:auto;">
                <table class="table table-sm table-striped align-middle mb-0">
                <thead class="table-dark sticky-top"><tr>
                    <th class="ps-3">Data/Hora</th>
                    <th>Produto</th>
                    <th class="text-center">Tempo</th>
                    <th>#Pedido</th>
                </tr></thead>
                <tbody>${reversed.map(h => {
                    const dt = h.data_conclusao ? new Date(h.data_conclusao) : null;
                    const dtStr = (dt && !isNaN(dt)) ? dt.toLocaleString('pt-BR') : (h.data_conclusao || '—');
                    const nome  = escapeHtml(h.produto || h.nome || 'N/D');
                    const pedNum = h.pedido_numero || h.order_id || '';
                    const tempo  = fmtTempo(h.tempo_segundos || 0);
                    return `<tr>
                        <td class="ps-3 small text-muted">${dtStr}</td>
                        <td class="fw-bold" style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${nome}">${nome}</td>
                        <td class="text-center fw-bold text-success font-monospace">${tempo}</td>
                        <td class="small text-muted">${pedNum ? '#'+pedNum : '—'}</td>
                    </tr>`;
                }).join('')}</tbody></table></div>`;
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

            let _wsFirstAuthDone = false;
            wsKpi.onmessage = (e) => {
                let data;
                try { data = JSON.parse(e.data); } catch { return; }

                if (data.type === 'full_update') {
                    updateAuthStatus(data.authenticated, data.auth_url);

                    if (data.sales_stats) updateKpis(data.sales_stats);
                    if (data.component_usage) updateComponentUsage(data.component_usage);

                    // Atualiza KPIs das 3 etapas de produção via broadcast (sem reload do board)
                    if (data.production_snapshot) {
                        const ps = data.production_snapshot;
                        const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
                        set('kpi-waiting', ps.waiting || 0);
                        set('kpi-inprod',  ps.in_production || 0);
                        set('kpi-done',    ps.done || 0);
                        set('waiting-count-badge', ps.waiting || 0);
                        set('inprod-count-badge',  ps.in_production || 0);
                        set('done-count-badge',    ps.done || 0);
                    }

                    // Primeira mensagem autenticada: sync pedidos + recarrega board
                    if (data.authenticated && !_wsFirstAuthDone) {
                        _wsFirstAuthDone = true;
                        fetch('/api/pending-orders/sync', { method: 'POST' }).catch(() => {});
                        const prodTab = document.getElementById('tab-producao');
                        if (prodTab && prodTab.classList.contains('active')) {
                            loadProductionBoard();
                        }
                    }

                    // Sincroniza cache: não há mais botão de recarregar na aba Produtos
                    if (data.cache_updated) {
                        showToast('Cache', 'Produtos atualizados no servidor.', 'info');
                    }
                }
            };

            wsKpi.onerror = () => { /* silencioso — onclose vai reconectar */ };

            wsKpi.onclose = () => {
                _wsFirstAuthDone = false;
                setTimeout(() => {
                    _kpiReconnectDelay = Math.min(_kpiReconnectDelay * 1.5, 30000);
                    _connectKpiWs();
                }, _kpiReconnectDelay);
            };
        }

        _connectKpiWs();

        /* ══════════════════════════════════════════════════════════
           DASHBOARD — Gráficos unificados
        ══════════════════════════════════════════════════════════ */

        let _dashFilter = { from: null, to: null };

        function applyDashboardFilter() {
            const f = document.getElementById('filter-date-from')?.value;
            const t = document.getElementById('filter-date-to')?.value;
            _dashFilter = { from: f || null, to: t || null };
            loadKPIChart();
        }
        function resetDashboardFilter() {
            _dashFilter = { from: null, to: null };
            const f = document.getElementById('filter-date-from');
            const t = document.getElementById('filter-date-to');
            if (f) f.value = ''; if (t) t.value = '';
            loadKPIChart();
        }
        function printDashboard() {
            const area = document.getElementById('print-area');
            const dash = document.getElementById('tab-dashboard');
            if (area && dash) {
                area.innerHTML = '<div style="padding:20px;">' + dash.innerHTML + '</div>';
                area.style.display = 'block';
                window.print();
                window.onafterprint = () => { area.innerHTML = ''; area.style.display = 'none'; window.onafterprint = null; };
            }
        }

        async function loadKPIChart() {
            try {
                let url = '/api/sales/history';
                const params = [];
                if (_dashFilter.from) params.push('from=' + _dashFilter.from);
                if (_dashFilter.to)   params.push('to='   + _dashFilter.to);
                if (params.length) url += '?' + params.join('&');

                const data = await fetchAPI(url);
                const ctx = document.getElementById('salesChart')?.getContext('2d');
                if (!ctx) return;
                if (salesChart) salesChart.destroy();

                // Dados de produção concluída (do board)
                const boardData = _boardDataRaw || {};
                const doneItems = boardData.done || [];
                // Conta concluídos por data
                const doneByDate = {};
                doneItems.forEach(d => {
                    const ds = (d.finished_at||'').slice(0,10);
                    if (ds) doneByDate[ds] = (doneByDate[ds]||0) + 1;
                });
                const prodCounts = (data.labels||[]).map(l => doneByDate[l]||0);

                salesChart = new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: data.labels || [],
                        datasets: [
                            {
                                label: 'Pedidos Recebidos',
                                data: data.daily || [],
                                borderColor: '#ffb600',
                                backgroundColor: 'rgba(255,182,0,0.1)',
                                tension: 0.4, fill: true, borderWidth: 2,
                                pointRadius: 3,
                            },
                            {
                                label: 'Produção Concluída',
                                data: prodCounts,
                                borderColor: '#10b981',
                                backgroundColor: 'rgba(16,185,129,0.08)',
                                tension: 0.4, fill: true, borderWidth: 2,
                                pointRadius: 3,
                            },
                            {
                                label: 'Média Móvel (7d)',
                                data: data.moving_avg || [],
                                borderColor: '#6366f1',
                                borderDash: [5,5],
                                tension: 0.4, borderWidth: 1.5,
                                pointRadius: 0,
                            }
                        ]
                    },
                    options: {
                        responsive: true, maintainAspectRatio: false,
                        plugins: { legend: { position: 'top' }, tooltip: { mode: 'index', intersect: false } },
                        scales: { y: { beginAtZero: true, ticks: { precision: 0 } } }
                    }
                });

                // Gráfico de barras de produção por dia
                _buildProdBarChart(data.labels||[], data.daily||[], prodCounts);
                // Gráfico delta (queda/subida)
                _buildDeltaChart(data.labels||[], data.daily||[]);

                // Métricas
                const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
                set('avg-daily',    (data.avg_daily||0).toFixed(1));
                const gr = data.growth||0;
                set('growth-weekly', (gr>0?'+':'')+gr.toFixed(1)+'%');
                set('trend-indicator', gr>5?'📈 Subindo':gr<-5?'📉 Caindo':'📊 Estável');
            } catch(e) {
                console.error('Erro ao carregar KPI Chart:', e);
            }
        }

        function _buildProdBarChart(labels, pedidos, producao) {
            const ctx = document.getElementById('prodBarChart')?.getContext('2d');
            if (!ctx) return;
            if (prodBarChart) prodBarChart.destroy();
            const last14 = -14;
            prodBarChart = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: labels.slice(last14),
                    datasets: [
                        { label: 'Pedidos', data: pedidos.slice(last14), backgroundColor: 'rgba(255,182,0,0.7)', borderRadius: 4 },
                        { label: 'Produzidos', data: producao.slice(last14), backgroundColor: 'rgba(16,185,129,0.7)', borderRadius: 4 }
                    ]
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    plugins: { legend: { position: 'top' } },
                    scales: { y: { beginAtZero: true, ticks: { precision: 0 } } }
                }
            });
        }

        function _buildDeltaChart(labels, counts) {
            const ctx = document.getElementById('deltaChart')?.getContext('2d');
            if (!ctx) return;
            if (deltaChart) deltaChart.destroy();
            const last14 = -14;
            const lbl = labels.slice(last14);
            const cnt = counts.slice(last14);
            const deltas = cnt.map((v,i) => i === 0 ? 0 : v - cnt[i-1]);
            deltaChart = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: lbl,
                    datasets: [{
                        label: 'Variação Diária',
                        data: deltas,
                        backgroundColor: deltas.map(d => d >= 0 ? 'rgba(16,185,129,0.75)' : 'rgba(239,68,68,0.75)'),
                        borderRadius: 4,
                    }]
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    plugins: { legend: { display: false } },
                    scales: { y: { ticks: { precision: 0 } } }
                }
            });
        }

        function _updateDashboardStagesChart(boardData) {
            const ctx = document.getElementById('stagesChart')?.getContext('2d');
            if (!ctx) return;
            if (stagesChart) stagesChart.destroy();
            const w = (boardData.waiting||[]).length;
            const p = (boardData.in_production||[]).length + (boardData.orphan_timers||[]).length;
            const d = (boardData.done||[]).length;
            stagesChart = new Chart(ctx, {
                type: 'doughnut',
                data: {
                    labels: ['Em Espera', 'Produzindo', 'Concluídos'],
                    datasets: [{
                        data: [w, p, d],
                        backgroundColor: ['#ffb600','#10b981','#6366f1'],
                        borderWidth: 2,
                        borderColor: '#fff',
                    }]
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'bottom' },
                        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${ctx.raw} pedido(s)` } }
                    }
                }
            });
        }

        /* ══════════════════════════════════════════════════════════
           SETOR: Marcenaria / Tapeçaria + Buscador
        ══════════════════════════════════════════════════════════ */

        function _classifySetor(nome) {
            const n = (nome || '').toUpperCase();
            // Tapeçaria: produtos com acabamento têxtil, estofamento
            const tapecaria_kw = ['CADEIRA','POLTRONA','ESTOFADO','ESPUMA','TECIDO',
                'COURVIM','COURO','VELUDO','LINHO','MATELASSÊ','MATELASSE',
                'ASSENTO','ENCOSTO','BASE ESTOFADA','RECLINÁVEL','RECLINAVEL',
                'HIDRÁULICA','HIDRAULICA','BERLIN','EVIDENCE','MADRID','LUNA'];
            // Marcenaria: estrutura em madeira/MDF
            const marcenaria_kw = ['MDF','COMPENSADO','MADEIRA','COMPENSAD','SARRAFO',
                'ARMÁRIO','ARMARIO','BALCÃO','BALCAO','BANCADA','CARRINHO','GABINETE',
                'LAVATÓRIO','LAVATORIO','ESPELHO','PRATELEIRA','NICHO','PAINEL'];
            const isTape = tapecaria_kw.some(k => n.includes(k));
            const isMarc = marcenaria_kw.some(k => n.includes(k));
            if (isTape && isMarc) return 'tapecaria';  // prioriza tapeçaria em ambiguidade
            if (isTape) return 'tapecaria';
            if (isMarc) return 'marcenaria';
            return 'outros';
        }

        function switchSetor(setor) {
            _currentSetor = setor;
            ['todos','marc','tape'].forEach(s => {
                const btn = document.getElementById('setor-'+s);
                if (btn) btn.className = btn.className.replace('btn-dark','btn-outline-secondary');
            });
            const map = {todos:'setor-todos', marcenaria:'setor-marc', tapecaria:'setor-tape'};
            const btn = document.getElementById(map[setor]);
            if (btn) btn.className = btn.className.replace('btn-outline-secondary','btn-dark');
            _renderCurrentTab();
        }

        function filterInProd(query) {
            if (!_boardDataRaw) return;
            const q = query.toLowerCase().trim();
            if (!q) { _boardData = _boardDataRaw; }
            else {
                _boardData = {
                    ..._boardDataRaw,
                    in_production: (_boardDataRaw.in_production||[]).filter(i =>
                        (i.pedido_numero||'').toLowerCase().includes(q) ||
                        (i.order_id||'').toLowerCase().includes(q) ||
                        (i.cliente||'').toLowerCase().includes(q) ||
                        (i.nome||'').toLowerCase().includes(q)
                    ),
                    orphan_timers: (_boardDataRaw.orphan_timers||[]).filter(i =>
                        (i.nome||'').toLowerCase().includes(q)
                    )
                };
            }
            _renderCurrentTab();
        }

        function printSetor() {
            const area = document.getElementById('print-area');
            const boardEl = document.getElementById('board-inprod');
            if (!area || !boardEl) return;
            const titulo = _currentSetor === 'todos' ? 'Todos os Setores' :
                           _currentSetor === 'marcenaria' ? '🪚 Marcenaria' : '🧵 Tapeçaria';
            area.innerHTML = `<div style="padding:20px;font-family:Arial,sans-serif;">
                <h2 style="text-align:center;">SW Móveis MDF — Produção: ${titulo}</h2>
                <p style="text-align:center;color:#666;font-size:12px;">${new Date().toLocaleString('pt-BR')}</p>
                ${boardEl.innerHTML}
            </div>`;
            area.style.display = 'block';
            window.print();
            window.onafterprint = () => { area.innerHTML=''; area.style.display='none'; window.onafterprint=null; };
        }

        /* ══════════════════════════════════════════════════════════
           EXPEDIÇÃO
        ══════════════════════════════════════════════════════════ */
        let _expedicaoFilter = 'all';

        async function loadExpedicao() {
            const sec = document.getElementById('expedicao-section');
            if (!sec) return;
            try {
                const data = await fetch('/api/production/board').then(r => r.json());
                const done = data.done || [];
                _renderExpedicao(done);
            } catch(e) {
                if (sec) sec.innerHTML = '<div class="alert alert-danger m-3">Erro ao carregar expedição.</div>';
            }
        }

        function filterExpedicao(filter) {
            _expedicaoFilter = filter;
            loadExpedicao();
        }

        function _renderExpedicao(items) {
            const sec = document.getElementById('expedicao-section');
            if (!sec) return;
            let filtered = items;
            if (_expedicaoFilter !== 'all') {
                filtered = items.filter(i => (i.urgencia||'normal') === _expedicaoFilter);
            }
            if (filtered.length === 0) {
                sec.innerHTML = '<div class="text-center py-5 text-muted"><div style="font-size:3rem;opacity:.3;">🚚</div><p class="mt-2">Nenhum item neste filtro.</p></div>';
                return;
            }
            const urgColors = { atrasado:'#ef4444', critico:'#f97316', atencao:'#f59e0b', normal:'#10b981' };
            let html = `<div class="table-responsive"><table class="table table-hover table-sm align-middle mb-0">
                <thead><tr style="background:#f9f9f7;">
                    <th class="ps-3">Produto</th><th>Base/Cor</th><th>#Pedido</th>
                    <th>Cliente</th><th class="text-center">Prazo</th>
                    <th class="text-center">Dias Rest.</th><th class="text-center">Tempo Prod.</th>
                    <th class="text-center">Concluído em</th>
                </tr></thead><tbody>`;
            filtered.slice().sort((a,b) => {
                const uo = {atrasado:0,critico:1,atencao:2,normal:3};
                return (uo[a.urgencia||'normal']||3) - (uo[b.urgencia||'normal']||3);
            }).forEach(item => {
                const urg = item.urgencia || 'normal';
                const dias = item.dias_restantes;
                const rowBg = urg==='atrasado' ? 'background:rgba(239,68,68,.07);' : urg==='critico' ? 'background:rgba(249,115,22,.05);' : '';
                const diasCell = dias === null || dias === undefined ? '—' :
                    `<span class="badge" style="background:${urgColors[urg]};color:#fff;">${dias<0?'ATRASO '+Math.abs(dias)+'d':dias===0?'HOJE':dias+'d'}</span>`;
                const dataEntFmt = item.data_entrega ? (() => { try { return new Date(item.data_entrega).toLocaleDateString('pt-BR'); } catch { return item.data_entrega; } })() : '—';
                const finFmt = item.finished_at ? new Date(item.finished_at).toLocaleString('pt-BR') : '—';
                // Converte tempo de produção para dias/horas
                const tp = item.tempo_producao || 0;
                const tpFmt = tp > 86400 ? (tp/86400).toFixed(1)+'d' : tp > 3600 ? Math.floor(tp/3600)+'h'+Math.floor((tp%3600)/60)+'m' : tp > 0 ? Math.floor(tp/60)+'m' : '—';
                html += `<tr style="${rowBg}">
                    <td class="ps-3 fw-bold">${escapeHtml(item.nome||item.nome_original||'N/D')}</td>
                    <td class="small text-muted">${escapeHtml(((item.base||'')+(item.base&&item.cor?' / ':'')+( item.cor||''))||'—')}</td>
                    <td class="small">#${escapeHtml(String(item.pedido_numero||item.order_id||'—'))}</td>
                    <td class="small text-muted">${escapeHtml(item.cliente||'—')}</td>
                    <td class="text-center small">${dataEntFmt}</td>
                    <td class="text-center">${diasCell}</td>
                    <td class="text-center small fw-bold text-success">${tpFmt}</td>
                    <td class="text-center small text-muted">${finFmt}</td>
                </tr>`;
            });
            html += '</tbody></table></div>';
            sec.innerHTML = html;
        }

        function printExpedicao() {
            const area = document.getElementById('print-area');
            const sec  = document.getElementById('expedicao-section');
            if (!area || !sec) return;
            area.innerHTML = `<div style="padding:20px;font-family:Arial,sans-serif;">
                <h2 style="text-align:center;">SW Móveis MDF — Lista de Expedição</h2>
                <p style="text-align:center;color:#666;font-size:12px;">${new Date().toLocaleString('pt-BR')}</p>
                ${sec.innerHTML}
            </div>`;
            area.style.display = 'block';
            window.print();
            window.onafterprint = () => { area.innerHTML=''; area.style.display='none'; window.onafterprint=null; };
        }

        /* ══════════════════════════════════════════════════════════
           RELATÓRIO DE PRODUÇÃO (7 / 30 dias)
        ══════════════════════════════════════════════════════════ */

        async function loadRelatorio(dias) {
            const sec = document.getElementById('relatorio-section');
            if (!sec) return;
            sec.innerHTML = '<div class="text-center py-4 text-muted">⏳ Buscando dados do Bling...</div>';
            try {
                const data = await fetchAPI('/api/production/report?dias=' + dias);

                if (data.error) {
                    sec.innerHTML = '<div class="alert alert-danger m-3">Erro: ' + escapeHtml(data.error) + '</div>';
                    return;
                }

                const growthColor = data.crescimento >= 0 ? '#10b981' : '#ef4444';
                const growthSign  = data.crescimento >= 0 ? '+' : '';

                sec.innerHTML = `
                <div class="row g-3 mb-4">
                    <div class="col-md-3">
                        <div class="card p-3 text-center h-100" style="border-left:4px solid #ffb600;">
                            <div class="text-muted small fw-bold text-uppercase" style="font-size:.65rem;letter-spacing:.08em;">Pedidos Recebidos</div>
                            <div style="font-size:2.8rem;font-family:'Bebas Neue',sans-serif;color:#ffb600;line-height:1;">${data.total_recebidos}</div>
                            <small class="text-muted">Últimos ${dias} dias</small>
                        </div>
                    </div>
                    <div class="col-md-3">
                        <div class="card p-3 text-center h-100" style="border-left:4px solid #10b981;">
                            <div class="text-muted small fw-bold text-uppercase" style="font-size:.65rem;letter-spacing:.08em;">Produzidos</div>
                            <div style="font-size:2.8rem;font-family:'Bebas Neue',sans-serif;color:#10b981;line-height:1;">${data.total_produzidos}</div>
                            <small class="text-muted">Concluídos no período</small>
                        </div>
                    </div>
                    <div class="col-md-3">
                        <div class="card p-3 text-center h-100" style="border-left:4px solid ${growthColor};">
                            <div class="text-muted small fw-bold text-uppercase" style="font-size:.65rem;letter-spacing:.08em;">Crescimento</div>
                            <div style="font-size:2.8rem;font-family:'Bebas Neue',sans-serif;color:${growthColor};line-height:1;">${growthSign}${data.crescimento}%</div>
                            <small class="text-muted">vs. ${dias} dias anteriores</small>
                        </div>
                    </div>
                    <div class="col-md-3">
                        <div class="card p-3 text-center h-100" style="border-left:4px solid #6366f1;">
                            <div class="text-muted small fw-bold text-uppercase" style="font-size:.65rem;letter-spacing:.08em;">Tempo Médio</div>
                            <div style="font-size:2.8rem;font-family:'Bebas Neue',sans-serif;color:#6366f1;line-height:1;">${data.avg_tempo_dias || '—'}</div>
                            <small class="text-muted">dias por pedido</small>
                        </div>
                    </div>
                </div>

                <div class="row g-3 mb-4">
                    <div class="col-lg-7">
                        <div class="card p-3">
                            <div class="fw-bold mb-2" style="font-size:.85rem;">📊 Pedidos por Dia (últimos ${dias}d)</div>
                            <div style="height:220px;"><canvas id="relatorio-chart"></canvas></div>
                        </div>
                    </div>
                    <div class="col-lg-5">
                        <div class="card p-3 h-100">
                            <div class="fw-bold mb-2" style="font-size:.85rem;">🏆 Top 10 Produtos Mais Vendidos</div>
                            <div style="max-height:240px;overflow-y:auto;">
                            ${(data.top_produtos||[]).map((p,i) => `
                                <div class="d-flex justify-content-between align-items-center py-1 border-bottom">
                                    <span style="font-size:.75rem;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(p.nome)}">
                                        <span class="badge bg-light text-dark border me-1">${i+1}</span>${escapeHtml(p.nome)}
                                    </span>
                                    <span class="badge bg-warning text-dark ms-2">${p.qtd} un.</span>
                                </div>`).join('')}
                            </div>
                        </div>
                    </div>
                </div>`;

                // Renderiza gráfico de barras
                setTimeout(() => {
                    const ctx = document.getElementById('relatorio-chart')?.getContext('2d');
                    if (!ctx) return;
                    new Chart(ctx, {
                        type: 'bar',
                        data: {
                            labels: data.labels,
                            datasets: [{
                                label: 'Pedidos / dia',
                                data: data.counts,
                                backgroundColor: 'rgba(255,182,0,0.75)',
                                borderRadius: 4
                            }]
                        },
                        options: {
                            responsive: true, maintainAspectRatio: false,
                            plugins: { legend: { display: false } },
                            scales: { y: { beginAtZero: true, ticks: { precision: 0 } } }
                        }
                    });
                }, 80);

            } catch(e) {
                if (sec) sec.innerHTML = '<div class="alert alert-danger m-3">Erro ao carregar relatório: ' + e.message + '</div>';
            }
        }

        function printRelatorio() {
            const area = document.getElementById('print-area');
            const sec  = document.getElementById('relatorio-section');
            if (!area || !sec) return;
            area.innerHTML = `<div style="padding:20px;font-family:Arial,sans-serif;">
                <h2 style="text-align:center;">SW Móveis MDF — Relatório de Produção</h2>
                <p style="text-align:center;color:#666;font-size:12px;">${new Date().toLocaleString('pt-BR')}</p>
                ${sec.innerHTML}
            </div>`;
            area.style.display = 'block';
            window.print();
            window.onafterprint = () => { area.innerHTML=''; area.style.display='none'; window.onafterprint=null; };
        }

        /* ══════════════════════════════════════════════════════════
           FICHA TÉCNICA
        ══════════════════════════════════════════════════════════ */

        async function loadFichaTecnica() {
            const sec = document.getElementById('ficha-section');
            if (!sec) return;

            // Contagem de quantas cadeiras estão em espera para contextualizar
            let waitingCount = 0;
            try {
                const bd = await fetch('/api/production/board').then(r => r.json());
                waitingCount = (bd.waiting || []).filter(i =>
                    (i.nome||i.nome_original||'').toUpperCase().includes('CADEIRA') ||
                    (i.nome||i.nome_original||'').toUpperCase().includes('POLTRONA')
                ).length;
            } catch(e) {}

            let html = `
            <div class="p-3">
                ${waitingCount > 0 ? `<div class="alert alert-info border-0 py-2 mb-3">
                    <strong>${waitingCount}</strong> cadeira(s)/poltrona(s) em espera — 
                    <a href="#" onclick="switchTabTo('tab-insumos');return false;">ver guia de compras</a>
                </div>` : ''}

                <div class="table-responsive">
                <table class="table table-sm align-middle mb-0 border">
                    <thead><tr style="background:#01010d;color:#fff;">
                        <th class="ps-3 py-2">#</th>
                        <th>Componente / Insumo</th>
                        <th class="text-center">Qtd/un</th>
                        <th>Unidade</th>
                        <th class="text-center">Para 10 un.</th>
                        <th class="text-center">Para ${waitingCount > 0 ? waitingCount + ' em espera' : '20 un.'}</th>
                    </tr></thead>
                    <tbody>
                    ${RECIPE_CADEIRA.map((c, i) => {
                        const for10  = (c.qtd * 10);
                        const forWait = (c.qtd * (waitingCount || 20));
                        return `<tr class="${i%2===0?'':'table-light'}">
                            <td class="ps-3 text-muted small">${i+1}</td>
                            <td class="fw-bold">${escapeHtml(c.nome)}</td>
                            <td class="text-center">${c.qtd}</td>
                            <td class="text-muted small">${escapeHtml(c.un)}</td>
                            <td class="text-center text-primary fw-bold">${for10 % 1 === 0 ? for10 : for10.toFixed(2)}</td>
                            <td class="text-center fw-bold" style="color:${waitingCount>0?'#10b981':'#6366f1'};">
                                ${forWait % 1 === 0 ? forWait : forWait.toFixed(2)}
                            </td>
                        </tr>`;
                    }).join('')}
                    </tbody>
                </table>
                </div>

                <div class="mt-3 p-3 border-top d-flex gap-3 flex-wrap align-items-center">
                    <small class="text-muted">
                        <strong>${RECIPE_CADEIRA.length}</strong> componentes cadastrados · 
                        Computados automaticamente na venda · 
                        Categoria: <strong>Cadeiras / Poltronas</strong>
                    </small>
                    <button class="btn btn-sm btn-outline-dark ms-auto" onclick="printFicha()">🖨️ Imprimir Ficha</button>
                </div>
            </div>`;
            sec.innerHTML = html;
        }

        function switchTabTo(tabId) {
            const btn = document.querySelector('[data-bs-target="#'+tabId+'"]');
            if (btn) { try { new bootstrap.Tab(btn).show(); } catch(e) {} }
        }

        function printFicha() {
            const area = document.getElementById('print-area');
            const sec  = document.getElementById('ficha-section');
            if (!area || !sec) return;
            area.innerHTML = `<div style="padding:20px;font-family:Arial,sans-serif;">
                <h2>SW Móveis MDF — Ficha Técnica: Cadeira SW</h2>
                <p style="color:#666;font-size:12px;">${new Date().toLocaleString('pt-BR')}</p>
                ${sec.innerHTML}
            </div>`;
            area.style.display = 'block';
            window.print();
            window.onafterprint = () => { area.innerHTML=''; area.style.display='none'; window.onafterprint=null; };
        }

        /* ══════════════════════════════════════════════════════════
           GUIA DE COMPRAS (Insumos necessários pelos pedidos)
        ══════════════════════════════════════════════════════════ */

        async function loadPurchaseGuide() {
            const sec = document.getElementById('purchase-guide-section');
            if (!sec) return;
            try {
                const data = await fetch('/api/production/board').then(r => r.json());
                const waiting = data.waiting || [];
                // Conta quantas cadeiras estão em espera
                const cadeiras = waiting.filter(i => (i.nome||i.nome_original||'').toUpperCase().includes('CADEIRA')).length;
                if (cadeiras === 0) {
                    sec.innerHTML = '<div class="text-center py-4 text-muted">Nenhuma cadeira em espera no momento.</div>';
                    return;
                }
                let html = `<div class="p-3">
                    <div class="alert alert-info border-0 mb-3">
                        <strong>${cadeiras} cadeira(s)</strong> em espera · Calculando insumos necessários para produção completa
                    </div>
                    <div class="table-responsive"><table class="table table-sm align-middle mb-0">
                        <thead><tr style="background:#f0f9ff;">
                            <th class="ps-3">Insumo</th><th class="text-center">Qtd/un</th>
                            <th class="text-center fw-bold text-primary">Total Necessário</th><th>Unidade</th>
                        </tr></thead><tbody>`;
                RECIPE_CADEIRA.forEach(c => {
                    const total = (c.qtd * cadeiras);
                    html += `<tr>
                        <td class="ps-3">${escapeHtml(c.nome)}</td>
                        <td class="text-center text-muted">${c.qtd}</td>
                        <td class="text-center fw-bold text-primary">${total % 1 === 0 ? total : total.toFixed(2)}</td>
                        <td class="text-muted small">${escapeHtml(c.un)}</td>
                    </tr>`;
                });
                html += `</tbody></table></div></div>`;
                sec.innerHTML = html;
            } catch(e) {
                if (sec) sec.innerHTML = '<div class="alert alert-danger m-3">Erro ao calcular guia de compras.</div>';
            }
        }


        /* ══════════════════════════════════════════════════════════
        /* ══════════════════════════════════════════════════════════ */




        function _onAuthConfirmed() {
            if (!_boardInitialized) {
                _boardInitialized = true;
                loadProductionBoard();
                refreshComponentTab();
                loadKPIChart();
                if (!_boardPoll) _boardPoll = setInterval(loadProductionBoard, 10000);
            }
        }

        /* Inicializa conexão WS após declarar todas as funções */

        document.addEventListener('DOMContentLoaded', () => {
            // Tab Dashboard
            document.querySelector('[data-bs-target="#tab-dashboard"]')?.addEventListener('shown.bs.tab', () => {
                loadKPIChart();
            });

            // Tab Produção
            const prodTab = document.querySelector('[data-bs-target="#tab-producao"]');
            if (prodTab) {
                prodTab.addEventListener('shown.bs.tab', () => {
                    loadProductionBoard();
                    if (!_boardPoll) _boardPoll = setInterval(loadProductionBoard, 10000);
                });
                prodTab.addEventListener('hidden.bs.tab', () => {
                    if (_boardPoll) { clearInterval(_boardPoll); _boardPoll = null; }
                    if (_boardTick) { clearInterval(_boardTick); _boardTick = null; }
                    // Esconde buscador e setor tabs ao sair
                    const sw = document.getElementById('setor-tabs-wrap');
                    const si = document.getElementById('search-inprod-wrap');
                    if (sw) sw.style.display = 'none';
                    if (si) si.style.display = 'none';
                });
            }

            // Tab Insumos
            document.querySelector('[data-bs-target="#tab-insumos"]')?.addEventListener('shown.bs.tab', () => {
                refreshComponentTab();
                loadPurchaseGuide();
            });

            // Tab Expedição
            document.querySelector('[data-bs-target="#tab-expedicao"]')?.addEventListener('shown.bs.tab', loadExpedicao);

            // Tab Relatório
            document.querySelector('[data-bs-target="#tab-relatorio"]')?.addEventListener('shown.bs.tab', () => {
                loadRelatorio(30);
                // Carrega histórico de finalizações
                try { fetchAPI('/api/components/usage').then(d => { if(d.history_production) renderProductionHistory(d.history_production); }).catch(()=>{}); } catch(e) {}
            });

            // Tab Ficha Técnica
            document.querySelector('[data-bs-target="#tab-ficha"]')?.addEventListener('shown.bs.tab', loadFichaTecnica);

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
    
    # ✅ REGRA DE OURO: Define uma SECRET_KEY estável para persistência de sessão.
    # CRÍTICO: Sempre configure FLASK_SECRET_KEY como variável de ambiente em produção.
    _secret = os.environ.get('FLASK_SECRET_KEY')
    if not _secret:
        logger.warning(
            "⚠️  FLASK_SECRET_KEY não configurada! Usando chave temporária gerada aleatoriamente. "
            "Configure essa variável em produção para evitar invalidação de sessões ao reiniciar."
        )
        _secret = secrets.token_hex(32)
    flask_app.config['SECRET_KEY'] = _secret
    
    # 4. Inicializa o WebServer (Rotas e WebSockets)
    WebServer(config, orchestrator, flask_app)

    # 5. Inicia worker automaticamente se já existe token salvo.
    #    Garante que após reinício do servidor (Render, deploy, idle)
    #    o sistema volte ao ar sem pedir reautenticação desnecessária.
    def _try_auto_start():
        try:
            time.sleep(2)  # aguarda Flask terminar de subir

            # ── Purge imediato de dados fantasma e itens antigos no boot ──
            try:
                removed = pending_orders.reset_if_new_month()
                if removed:
                    logger.info(f"🧹 Boot purge: {removed} itens fantasma/antigos removidos do MongoDB.")
            except Exception as _pe:
                logger.warning(f"Boot purge: {_pe}")

            if orchestrator.is_running():
                return
            # Reseta timer de sync para garantir leitura fresca do MongoDB no boot
            orchestrator.auth._last_storage_sync = 0
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