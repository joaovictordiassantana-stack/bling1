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
# CONFIGURAÇÃO DE DISCO PERSISTENTE (RENDER)
# ============================================================================
# Configure a variável de ambiente DATA_DIR='/data' no painel do Render.
# Sem isso usa pasta local (efêmera — dados somem ao reiniciar).
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
    atomic_write_json(data, path) # Usa a nova função segura
    logger.info("Tokens salvos com sucesso (Atômico).")

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
    data_to_save = data.copy()
    if 'last_recalculated' in data_to_save and isinstance(data_to_save['last_recalculated'], datetime):
        data_to_save['last_recalculated'] = data_to_save['last_recalculated'].isoformat()

    atomic_write_json(data_to_save, path) # Usa a nova função segura
    logger.info("Estatísticas de KPIs salvas com sucesso (Atômico).")

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
    logger.debug(f"save_products_cache chamado. products={len(products or [])} kits={len(kits or [])} total={total_produtos}")
    
    # ✅ 3. Nunca salvar cache se produtos == 0
    if total_produtos == 0:
        logger.warning("⛔ Cache vazio ignorado. Não salvando no disco. Isto indica que a API não retornou produtos ou que o parsing falhou.")
        return
        
    try:
        payload = {
            "updated_at": datetime.now().isoformat(),
            "products": products or [],
            "kits": kits or []
        }
        atomic_write_json(payload, cache_file) # Usa a nova função segura
        
        skus = [p.get('sku') for p in (products or [])[:5]] + [k.get('sku') for k in (kits or [])[:5]]
        logger.info(f"Cache salvo com sample skus: {skus}")
        logger.info(f"Cache de produtos e kits salvo com sucesso (Atômico). Total: {total_produtos}")
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
        self._auto_pause_on_restart() # Pausa timers abertos ao reiniciar para segurança

    def _load(self):
        if not self.FILE_PATH.exists():
            return {}
        try:
            with open(self.FILE_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f'Erro ao ler timers: {e}')
            return {}

    def _save(self):
        """Salva o estado atual. Se o servidor cair, o arquivo .json estará seguro."""
        temp_file = self.FILE_PATH.with_suffix('.tmp')
        try:
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self.timers, f, indent=4)
            # A operação de renomear é atômica no sistema operativo
            shutil.move(str(temp_file), str(self.FILE_PATH))
        except Exception as e:
            logger.error(f"Erro ao salvar timers: {e}")

    def _auto_pause_on_restart(self):
        """Se o servidor cair, pausa os timers para não contar tempo falso e salva o tempo decorrido."""
        changed = False
        for k, v in self.timers.items():
            if v['state'] == 'running':
                # Como o servidor caiu, não sabemos o exato momento, mas o arquivo JSON
                # reflete o estado da última vez que foi salvo. Se estava 'running',
                # pausamos para evitar contagem infinita.
                v['state'] = 'paused'
                v['start_ts'] = 0 
                changed = True
        if changed: self._save()

    def start(self, produto_nome):
        now = time.time()
        if produto_nome not in self.timers:
            self.timers[produto_nome] = {
                'start_ts': now, 
                'accumulated': 0, 
                'state': 'running',
                'created_at': datetime.now().isoformat()
            }
        else:
            t = self.timers[produto_nome]
            if t['state'] != 'running':
                t['start_ts'] = now
                t['state'] = 'running'
        self._save()
        
        # Inicia uma thread para salvar o progresso a cada 30 segundos enquanto estiver rodando
        def background_saver(nome):
            while nome in self.timers and self.timers[nome]['state'] == 'running':
                time.sleep(30)
                if nome in self.timers and self.timers[nome]['state'] == 'running':
                    t = self.timers[nome]
                    now_ts = time.time()
                    t['accumulated'] += (now_ts - t['start_ts'])
                    t['start_ts'] = now_ts
                    self._save()
        
        Thread(target=background_saver, args=(produto_nome,), daemon=True).start()
        
        return self.get_status(produto_nome)

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
        """Finaliza a produção, salva histórico. Retorna registro para o frontend confirmar."""
        status = self.pause(produto_nome)
        total_seconds = status['elapsed']
        registro = {
            'produto': produto_nome,
            'tempo_segundos': total_seconds,
            'data_conclusao': datetime.now().isoformat(),
            'timestamp': time.time()
        }
        self._add_to_history(registro)
        if produto_nome in self.timers:
            del self.timers[produto_nome]
            self._save()
        return {'elapsed': 0, 'state': 'finished', 'registro': registro}

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
        return {'elapsed': int(total), 'state': t['state']}

    def get_active_timers(self):
        """Retorna tudo que está sendo produzido agora para o Chefe ver."""
        active = []
        for nome, data in self.timers.items():
            current_total = data['accumulated']
            if data['state'] == 'running':
                current_total += (time.time() - data['start_ts'])
            
            active.append({
                "produto": nome,
                "estado": data['state'], # running ou paused
                "tempo_decorrido": int(current_total),
                "inicio": data.get('created_at', '')
            })
        return active

    def _add_to_history(self, registro):
        """Salva no histórico mensal — sempre do disco, encoding utf-8."""
        try:
            if self.HISTORY_PATH.exists():
                with open(self.HISTORY_PATH, 'r', encoding='utf-8') as f:
                    history = json.load(f)
            else:
                history = {}
            mes_chave = datetime.now().strftime('%Y-%m')
            if mes_chave not in history:
                history[mes_chave] = []
            history[mes_chave].append(registro)
            temp = self.HISTORY_PATH.with_suffix('.tmp')
            with open(temp, 'w', encoding='utf-8') as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
            shutil.move(str(temp), str(self.HISTORY_PATH))
            logger.info(f"✅ Histórico salvo: {registro['produto']} ({registro['tempo_segundos']}s)")
        except Exception as e:
            logger.error(f'Erro ao salvar histórico de produção: {e}')

    def get_monthly_history_details(self):
        """Retorna a lista detalhada do mês atual — sempre do disco."""
        if not self.HISTORY_PATH.exists():
            return []
        try:
            with open(self.HISTORY_PATH, 'r', encoding='utf-8') as f:
                history = json.load(f)
            mes_chave = datetime.now().strftime('%Y-%m')
            return history.get(mes_chave, [])
        except Exception as e:
            logger.error(f'Erro ao ler histórico: {e}')
            return []

class ComponentConsumptionManager:
    """
    Gerencia consumo de insumos via checklist.
    SEMPRE lê/escreve no disco — nunca depende de RAM entre requests.
    Garante persistência mesmo com múltiplos workers Gunicorn no Render.
    """
    FILE_PATH = DATA_DIR / 'component_consumption.json'
    _lock = Lock()

    def _current_month_key(self) -> str:
        return datetime.now().strftime('%Y-%m')

    def _load_disk(self) -> dict:
        if not self.FILE_PATH.exists():
            return {}
        try:
            with open(self.FILE_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f'Erro ao ler component_consumption.json: {e}')
            return {}

    def _save_disk(self, data: dict):
        temp = self.FILE_PATH.with_suffix('.tmp')
        try:
            with open(temp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            shutil.move(str(temp), str(self.FILE_PATH))
        except Exception as e:
            logger.error(f'Erro ao salvar consumo: {e}')

    def _ensure_month(self, data: dict, key: str):
        if key not in data:
            data[key] = {'components': {}, 'checklist_logs': []}

    def register_component(self, component_name: str, qty: float, unit: str, product_name: str) -> dict:
        """Thread-safe: lê disco → modifica → salva disco."""
        with self._lock:
            data = self._load_disk()
            key = self._current_month_key()
            self._ensure_month(data, key)
            month = data[key]
            if component_name not in month['components']:
                month['components'][component_name] = {'qtd': 0, 'un': unit, 'registros': []}
            comp = month['components'][component_name]
            comp['qtd'] = round(comp['qtd'] + qty, 3)
            comp['un'] = unit
            comp['registros'].append({'produto': product_name, 'qtd': qty, 'timestamp': datetime.now().isoformat()})
            month['checklist_logs'].append({'componente': component_name, 'produto': product_name,
                                            'qtd': qty, 'un': unit, 'timestamp': datetime.now().isoformat()})
            self._save_disk(data)
            logger.info(f'✅ Consumo: {component_name} x{qty} ({product_name})')
            return comp

    def unregister_component(self, component_name: str, qty: float, product_name: str):
        """Thread-safe: lê disco → remove → salva disco."""
        with self._lock:
            data = self._load_disk()
            key = self._current_month_key()
            self._ensure_month(data, key)
            month = data[key]
            if component_name in month['components']:
                comp = month['components'][component_name]
                comp['qtd'] = max(0, round(comp['qtd'] - qty, 3))
                removed = False
                for i in range(len(comp['registros']) - 1, -1, -1):
                    if comp['registros'][i]['produto'] == product_name and not removed:
                        comp['registros'].pop(i)
                        removed = True
                self._save_disk(data)

    def get_current_month(self) -> dict:
        data = self._load_disk()
        key = self._current_month_key()
        self._ensure_month(data, key)
        return data[key]

    def get_all_months(self) -> dict:
        return self._load_disk()

    def get_month_summary(self) -> list:
        month = self.get_current_month()
        out = []
        for nome, info in month['components'].items():
            out.append({'nome': nome, 'qtd_total': info['qtd'], 'un': info['un'],
                        'num_registros': len(info['registros']), 'registros': info['registros'][-5:]})
        return sorted(out, key=lambda x: x['qtd_total'], reverse=True)


# ============================================================================
# FILA DE PRODUÇÃO (pedidos Bling → em espera para produção)
# ============================================================================
QUEUE_FILE = DATA_DIR / 'production_queue.json'
_queue_lock = Lock()

def _load_queue() -> list:
    if not QUEUE_FILE.exists():
        return []
    try:
        with open(QUEUE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f'Erro ao ler fila: {e}')
        return []

def _save_queue(queue: list):
    temp = QUEUE_FILE.with_suffix('.tmp')
    try:
        with open(temp, 'w', encoding='utf-8') as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
        shutil.move(str(temp), str(QUEUE_FILE))
    except Exception as e:
        logger.error(f'Erro ao salvar fila: {e}')

def add_to_queue(item: dict) -> bool:
    """Adiciona um item à fila. Evita duplicatas por pedido_id+produto."""
    with _queue_lock:
        q = _load_queue()
        key = f"{item.get('pedido_id','')}-{item.get('produto','')}"
        for existing in q:
            if f"{existing.get('pedido_id','')}-{existing.get('produto','')}" == key:
                return False  # Já existe
        item['status'] = 'waiting'
        item['adicionado_em'] = datetime.now().isoformat()
        q.append(item)
        _save_queue(q)
        return True

def remove_from_queue(pedido_id: str, produto: str):
    """Remove item da fila (quando iniciou produção ou foi descartado)."""
    with _queue_lock:
        q = _load_queue()
        q = [x for x in q if not (str(x.get('pedido_id','')) == str(pedido_id) and x.get('produto','') == produto)]
        _save_queue(q)

def get_queue() -> list:
    return _load_queue()

# Instâncias globais
production_timer = ProductionTimer()
component_consumption = ComponentConsumptionManager()

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
                
            self.logger.info("Iniciando busca de pedidos (A partir de 01/01/2026)...")
            now = datetime.now()
            # Força o início EXATO no dia 1º do mês atual (Ex: 01/01/2026)
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            
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

        @self.app.route('/api/timers/status')
        def api_timers_status():
            """Estado ao vivo de todos os timers + histórico + fila. Sempre do disco."""
            active = production_timer.get_active_timers()
            history = production_timer.get_monthly_history_details()
            queue = get_queue()
            total_sec = sum(h.get('tempo_segundos', 0) for h in history)
            return jsonify({
                'active': active,
                'history': history,
                'queue': queue,
                'total_horas_mes': round(total_sec / 3600, 2)
            })

        @self.app.route('/api/sales/monthly-products')
        def api_monthly_products():
            """Produtos vendidos no mês com quantidade real por item de pedido."""
            try:
                agora = datetime.now()
                mes_atual = agora.month
                ano_atual = agora.year
                produtos_vendidos = defaultdict(int)
                todos_pedidos = []
                with self.orchestrator.sales.lock:
                    todos_pedidos = list(self.orchestrator.sales._sales_history or [])[-500:]
                for pedido in todos_pedidos:
                    data_str = pedido.get('data')
                    if not data_str:
                        continue
                    try:
                        data_limpa = str(data_str).split(' ')[0].split('T')[0]
                        dt = (datetime.strptime(data_limpa, '%Y-%m-%d') if '-' in data_limpa
                              else datetime.strptime(data_limpa, '%d/%m/%Y'))
                        if dt.month != mes_atual or dt.year != ano_atual:
                            continue
                    except Exception:
                        continue
                    for item in pedido.get('itens', []):
                        nome = (item.get('descricao') or item.get('nome') or '').strip()
                        try:
                            qtd = int(float(item.get('quantidade', 1)))
                        except Exception:
                            qtd = 1
                        if nome and qtd > 0:
                            produtos_vendidos[nome] += qtd
                lista = [{'nome': n, 'qtd': q}
                         for n, q in sorted(produtos_vendidos.items(), key=lambda x: x[1], reverse=True)]
                return jsonify({'mes': agora.strftime('%Y-%m'), 'produtos': lista})
            except Exception as e:
                self.logger.exception("Erro em /api/sales/monthly-products")
                return jsonify({'mes': '', 'produtos': []}), 500

        @self.app.route('/api/production-queue/add', methods=['POST'])
        def api_queue_add():
            """Adiciona item à fila de produção (vindo de pedido Bling)."""
            data = request.json
            pedido_id = str(data.get('pedido_id', ''))
            produto = str(data.get('produto', ''))
            if not produto:
                return jsonify({'error': 'produto obrigatório'}), 400
            added = add_to_queue({
                'pedido_id': pedido_id,
                'produto': produto,
                'cor': data.get('cor', ''),
                'base': data.get('base', ''),
                'qtd': int(data.get('qtd', 1)),
                'cliente': data.get('cliente', ''),
            })
            return jsonify({'added': added, 'queue': get_queue()})

        @self.app.route('/api/production-queue/start', methods=['POST'])
        def api_queue_start():
            """Inicia produção de um item da fila — move para timer ativo."""
            data = request.json
            pedido_id = str(data.get('pedido_id', ''))
            produto = str(data.get('produto', ''))
            remove_from_queue(pedido_id, produto)
            status = production_timer.start(produto)
            def _bc():
                try:
                    usage = self.orchestrator.calculate_component_usage()
                    self.orchestrator._component_usage_cache = usage
                    self.orchestrator.broadcast_kpi_update(component_usage=usage)
                except Exception as e:
                    self.logger.error(f'Broadcast fila→timer: {e}')
            Thread(target=_bc, daemon=True).start()
            return jsonify({'status': status, 'queue': get_queue()})

        @self.app.route('/api/production-queue/remove', methods=['POST'])
        def api_queue_remove():
            """Remove item da fila sem iniciar produção."""
            data = request.json
            remove_from_queue(str(data.get('pedido_id', '')), str(data.get('produto', '')))
            return jsonify({'queue': get_queue()})

        @self.app.route('/api/production-queue/from-orders', methods=['POST'])
        def api_queue_from_orders():
            """Lê pedidos recentes do Bling e popula a fila automaticamente.
            Correlaciona produto do pedido com cadeiras conhecidas."""
            try:
                agora = datetime.now()
                mes_atual = agora.month
                ano_atual = agora.year
                added = 0
                todos_pedidos = []
                with self.orchestrator.sales.lock:
                    todos_pedidos = list(self.orchestrator.sales._sales_history or [])[-500:]
                for pedido in todos_pedidos:
                    data_str = pedido.get('data')
                    if not data_str:
                        continue
                    try:
                        data_limpa = str(data_str).split(' ')[0].split('T')[0]
                        dt = (datetime.strptime(data_limpa, '%Y-%m-%d') if '-' in data_limpa
                              else datetime.strptime(data_limpa, '%d/%m/%Y'))
                        if dt.month != mes_atual or dt.year != ano_atual:
                            continue
                    except Exception:
                        continue
                    pedido_id = str(pedido.get('id', ''))
                    cliente = pedido.get('contato', {}).get('nome', '') if isinstance(pedido.get('contato'), dict) else str(pedido.get('cliente', ''))
                    for item in pedido.get('itens', []):
                        nome_raw = (item.get('descricao') or item.get('nome') or '').strip()
                        nome_up = nome_raw.upper()
                        if 'CADEIRA' not in nome_up:
                            continue
                        try:
                            qtd = int(float(item.get('quantidade', 1)))
                        except Exception:
                            qtd = 1
                        # Extrai cor e base do nome do produto
                        cor = ''
                        base = ''
                        partes = nome_raw.split(' ')
                        for i, p in enumerate(partes):
                            if p.upper() in ['BRANCA','PRETA','CINZA','BEGE','MARROM','VERDE','AZUL','VERMELHA','ROSA','AMADEIRADA']:
                                cor = p
                            if p.upper() in ['CROMADA','MADEIRA','PLASTICA','METALICA']:
                                base = p
                        for _ in range(qtd):
                            ok = add_to_queue({
                                'pedido_id': pedido_id,
                                'produto': nome_raw,
                                'cor': cor,
                                'base': base,
                                'qtd': 1,
                                'cliente': cliente,
                            })
                            if ok:
                                added += 1
                return jsonify({'added': added, 'queue': get_queue()})
            except Exception as e:
                self.logger.exception("Erro ao popular fila de pedidos")
                return jsonify({'error': str(e)}), 500

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
            global kpi_update_callbacks, kpi_update_lock
            if len(kpi_update_callbacks) >= 20:
                self.logger.warning("Limite de 20 conexões WS atingido.")
                return

            def kpi_callback(payload):
                try:
                    ws.send(json.dumps(payload))
                except ConnectionClosed:
                    raise
                except Exception:
                    raise ConnectionClosed()

            # Envio inicial IMEDIATO — nunca calcula componentes aqui.
            # Isso evita travar o "CARREGANDO" quando o cálculo demora.
            try:
                is_auth = self.orchestrator.auth.is_authenticated()
                auth_url = self.orchestrator.auth.get_authorization_url()
                payload = {
                    "type": "full_update",
                    "authenticated": is_auth,
                    "auth_error": False,
                    "is_running": self.orchestrator.is_running(),
                    "cache_updated": False,
                    "auth_url": auth_url,
                }
                try:
                    ss = self.orchestrator.sales._get_state_for_save()
                    last = ss.get('last_recalculated')
                    if isinstance(last, datetime):
                        ss['last_update'] = last.isoformat()
                        ss.pop('last_recalculated', None)
                    payload["sales_stats"] = ss
                except Exception:
                    pass
                ws.send(json.dumps(payload))
                self.logger.info("✅ Estado inicial rápido enviado")
                def _bg_calc():
                    try:
                        usage = self.orchestrator.calculate_component_usage()
                        self.orchestrator._component_usage_cache = usage
                        self.orchestrator.broadcast_kpi_update(component_usage=usage)
                    except Exception as e:
                        self.logger.error(f"Erro cálculo bg: {e}")
                Thread(target=_bg_calc, daemon=True).start()
            except Exception as e:
                self.logger.error(f"Erro WS envio inicial: {e}")

            with kpi_update_lock:
                kpi_update_callbacks.append(kpi_callback)
            try:
                while True:
                    ws.receive(timeout=60)
            except ConnectionClosed:
                pass
            finally:
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

        /* Remova ou oculte classes de estoque */
        .stock-badge, .estoque-info, .stock-info-row {
            display: none !important;
        }

        @keyframes pulse-animation {
            0% { opacity: 1; }
            50% { opacity: 0.5; }
            100% { opacity: 1; }
        }
        .pulse-animation {
            animation: pulse-animation 2s infinite;
        }
        .shadow-2xl {
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
        }
        .letter-spacing-2 {
            letter-spacing: 0.1em;
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
                <img src="https://i.imgur.com/j79HO6n.png" alt="SW Móveis" style="height: 40px; width: auto; filter: brightness(1.1);">
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
                    <h5>Pedidos Mensais</h5>
                    <h3 id="kpi-historic" style="color: var(--success);">0</h3>
                    <small class="text-muted">Este Mês</small>
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
                        <!-- Seção: Fila de Produção (Em Espera — pedidos Bling) -->
                        <div class="card mb-4 border-0 shadow-sm">
                            <div class="card-header d-flex justify-content-between align-items-center" style="background: linear-gradient(135deg, #92400e 0%, #d97706 100%);">
                                <div>
                                    <h5 class="mb-0">📋 Fila de Produção <span class="badge bg-light text-dark ms-2" id="queue-count-badge">0</span></h5>
                                    <small class="text-white-50">Pedidos do Bling aguardando produção • Clique em Iniciar para começar</small>
                                </div>
                                <button class="btn btn-sm btn-outline-light" onclick="syncQueueFromOrders()" title="Importar pedidos do mês do Bling para a fila">⬇️ Importar Pedidos</button>
                            </div>
                            <div class="card-body p-0" id="production-queue-section">
                                <div class="text-center py-4 text-muted">⏳ Carregando fila...</div>
                            </div>
                        </div>

                        <!-- Seção: Produção em Andamento -->
                        <div class="card mb-4 border-0 shadow-sm">
                            <div class="card-header d-flex justify-content-between align-items-center" style="background: linear-gradient(135deg, #1e293b 0%, #334155 100%);">
                                <div>
                                    <h5 class="mb-0">⚙️ Produção em Andamento</h5>
                                    <small class="text-white-50">Timers ativos — persistem mesmo após reinício</small>
                                </div>
                                <button class="btn btn-sm btn-outline-light" onclick="refreshComponentTab()">🔄 Atualizar</button>
                            </div>
                            <div class="card-body" id="active-timers-section">
                                <p class="text-center text-muted py-3">⏳ Carregando timers...</p>
                            </div>
                        </div>

                        <!-- Seção: Consumo Mensal de Insumos (via Checklist) -->
                        <div class="card mb-4 border-0 shadow-sm">
                            <div class="card-header d-flex justify-content-between align-items-center" style="background: linear-gradient(135deg, #065f46 0%, #059669 100%);">
                                <div>
                                    <h5 class="mb-0">📊 Consumo de Insumos & Componentes</h5>
                                    <small class="text-white-50" id="consumption-month-label">Mês atual • Reinicia todo mês</small>
                                </div>
                                <span class="badge bg-light text-dark" id="consumption-total-badge">0 itens registrados</span>
                            </div>
                            <div class="card-body p-0" id="consumption-table-section">
                                <div class="text-center py-4 text-muted">⏳ Carregando consumo...</div>
                            </div>
                        </div>

                        <!-- Seção: Produtos Vendidos no Mês -->
                        <div class="card mb-4 border-0 shadow-sm">
                            <div class="card-header" style="background: linear-gradient(135deg, #1e3a5f 0%, #1d4ed8 100%);">
                                <h5 class="mb-0">🛒 Produtos Vendidos (Mês Atual)</h5>
                                <small class="text-white-50">Baseado nos pedidos faturados conectados ao Bling</small>
                            </div>
                            <div class="card-body p-0" id="monthly-sales-section">
                                <div class="text-center py-4 text-muted">⏳ Aguardando dados...</div>
                            </div>
                        </div>

                        <!-- Seção: Histórico de Finalizações -->
                        <div class="card border-0 shadow-sm">
                            <div class="card-header" style="background: linear-gradient(135deg, #3b0764 0%, #7c3aed 100%);">
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

        function openProductionChecklist(productName) {
            const isCadeira = productName.toUpperCase().includes('CADEIRA');
            let checklistHtml = '';

            if (isCadeira) {
                checklistHtml = `
                    <h6 class="text-muted mb-3">📋 Marque o que foi retirado/usado para esta unidade</h6>
                    <div class="row g-2 mb-4" style="max-height: 320px; overflow-y: auto;">
                        ${RECIPE_CADEIRA.map((item, i) => `
                            <div class="col-md-6">
                                <div class="form-check p-2 border rounded bg-white d-flex align-items-center gap-2 checklist-item" 
                                     style="cursor:pointer; transition: all .2s;"
                                     onclick="toggleChecklist(this, ${i}, '${productName}')">
                                    <input class="form-check-input ms-1" type="checkbox" id="check${i}" onclick="event.stopPropagation()">
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
                                        <div class="d-flex justify-content-center gap-2">
                                            <button class="btn btn-success px-4 fw-bold" onclick="controlTimer('start', '${productName}')">
                                                ▶ Iniciar
                                            </button>
                                            <button class="btn btn-warning px-4 fw-bold text-dark" onclick="controlTimer('pause', '${productName}')">
                                                ⏸ Pausar
                                            </button>
                                            <button class="btn btn-outline-light px-4" onclick="controlTimer('reset', '${productName}')">
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
                                <button type="button" class="btn btn-success px-4 fw-bold" onclick="controlTimer('finish', '${productName}')">
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

            // Carrega estado atual do timer
            controlTimer('get', productName);
        }

        // Estado de checklist por produto (para saber o que está marcado)
        const checklistState = {};

        function toggleChecklist(container, idx, productName) {
            const cb = container.querySelector('input[type=checkbox]');
            cb.checked = !cb.checked;
            const item = RECIPE_CADEIRA[idx];

            if (cb.checked) {
                container.style.background = '#d1fae5';
                container.style.borderColor = '#10b981';
                // Registra na API
                registerConsumption(item.nome, item.qtd, item.un, productName, true);
            } else {
                container.style.background = '';
                container.style.borderColor = '';
                registerConsumption(item.nome, item.qtd, item.un, productName, false);
            }

            // Atualiza progress
            const total = RECIPE_CADEIRA.length;
            const checked = document.querySelectorAll('#productionModal .form-check-input:checked').length;
            const progressDiv = document.getElementById('checklist-progress');
            if (progressDiv) {
                progressDiv.innerHTML = `<strong>${checked} / ${total}</strong> itens marcados como usados${checked === total ? ' ✅ Tudo marcado!' : ''}`;
                progressDiv.className = `alert py-2 small mb-0 ${checked === total ? 'alert-success' : 'alert-info'}`;
            }
        }

        async function registerConsumption(componentName, qty, unit, productName, checked) {
            try {
                await fetch('/api/consumption/register', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        component_name: componentName,
                        qty: qty,
                        unit: unit,
                        product_name: productName,
                        checked: checked
                    })
                });
                // Atualiza a aba de consumo se estiver visível
                if (document.getElementById('component-usage').classList.contains('active')) {
                    refreshComponentTab();
                }
            } catch(e) {
                console.error('Erro ao registrar consumo:', e);
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
                const data = await res.json();

                if (action === 'finish') {
                    clearInterval(timerInterval);
                    const elapsed = data.registro ? data.registro.tempo_segundos : 0;
                    showToast('✅ Produção Concluída!',
                        produto + ' — ' + formatSeconds(elapsed) + ' salvo no histórico.', 'success');
                    const modal = bootstrap.Modal.getInstance(document.getElementById('productionModal'));
                    if (modal) modal.hide();
                    refreshComponentTab();
                    return;
                }
                if (action === 'reset') {
                    clearInterval(timerInterval);
                    updateTimerDisplay(0, 'stopped');
                    return;
                }
                updateTimerDisplay(data.elapsed, data.state);
                if (action === 'start' || (action === 'get' && data.state === 'running')) {
                    startLocalCounter(data.elapsed);
                } else {
                    clearInterval(timerInterval);
                }
            } catch (e) {
                console.error("Erro no timer:", e);
                showToast('Erro', 'Falha ao comunicar com o servidor.', 'danger');
            }
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

        /* Atualizar Componentes — via WebSocket broadcast */
        function updateComponentUsage(usageData) {
            if (!usageData) return;
            if (usageData.active_production) renderActiveTimers(usageData.active_production);
            if (usageData.history_production) renderProductionHistory(usageData.history_production);
            fetchAPI('/api/sales/monthly-products')
                .then(d => renderMonthlySales(d.produtos || [], d.mes || ''))
                .catch(() => { if (usageData.produtos_vendidos) renderMonthlySales(usageData.produtos_vendidos, ''); });
        }

        let _componentPolling = null;

        async function refreshComponentTab() {
            // 1. Insumos (checklist)
            try {
                const consumptionData = await fetchAPI('/api/consumption/summary');
                renderConsumptionTable(consumptionData);
            } catch(e) {
                const el = document.getElementById('consumption-table-section');
                if (el) el.innerHTML = '<div class="alert alert-danger m-3">Erro ao carregar consumo.</div>';
            }
            // 2. Timers + histórico + fila — tudo do disco
            try {
                const d = await fetchAPI('/api/timers/status');
                renderProductionQueue(d.queue || []);
                renderActiveTimers(d.active || []);
                renderProductionHistory(d.history || []);
            } catch(e) { console.error('Erro timers/fila:', e); }
            // 3. Produtos vendidos
            try {
                const s = await fetchAPI('/api/sales/monthly-products');
                renderMonthlySales(s.produtos || [], s.mes || '');
            } catch(e) { console.error('Erro vendas:', e); }
        }

        function startComponentPolling() {
            if (_componentPolling) return;
            _componentPolling = setInterval(async () => {
                try {
                    const d = await fetchAPI('/api/timers/status');
                    renderProductionQueue(d.queue || []);
                    renderActiveTimers(d.active || []);
                    renderProductionHistory(d.history || []);
                } catch(e) {}
            }, 5000);
        }

        function stopComponentPolling() {
            if (_componentPolling) { clearInterval(_componentPolling); _componentPolling = null; }
        }

        async function syncQueueFromOrders() {
            showToast('⏳', 'Importando pedidos do Bling...', 'info');
            try {
                const res = await fetch('/api/production-queue/from-orders', {method:'POST', headers:{'Content-Type':'application/json'}});
                const d = await res.json();
                showToast('✅ Fila atualizada', d.added + ' item(s) adicionado(s) à fila.', 'success');
                refreshComponentTab();
            } catch(e) {
                showToast('Erro', 'Falha ao importar pedidos.', 'danger');
            }
        }

        // Renderiza fila de espera (pedidos Bling aguardando produção)
        function renderProductionQueue(queue) {
            const div = document.getElementById('production-queue-section');
            const badge = document.getElementById('queue-count-badge');
            if (!div) return;
            if (badge) badge.textContent = queue.length;
            if (!queue || queue.length === 0) {
                div.innerHTML = '<div class="text-center py-4"><div style="font-size:2rem;opacity:.3;">📋</div>' +
                    '<p class="text-muted mt-2 mb-0">Nenhum pedido aguardando. Clique em ⬇️ Importar Pedidos para buscar do Bling.</p></div>';
                return;
            }
            const rows = queue.map(item => {
                const safe = (item.produto || '').replace(/'/g, "\\'");
                const pid = String(item.pedido_id || '');
                const info = [item.cor, item.base].filter(Boolean).join(' / ');
                return '<tr>' +
                    '<td class="ps-3"><div class="fw-bold">' + (item.produto || '—') + '</div>' +
                        (info ? '<small class="text-muted">' + info + '</small>' : '') + '</td>' +
                    '<td class="text-center text-muted small">' + (item.cliente || '—') + '</td>' +
                    '<td class="text-center"><span class="badge bg-warning text-dark">⏳ Em Espera</span></td>' +
                    '<td class="text-center">' +
                        '<button class="btn btn-success btn-sm me-1" onclick="startFromQueue(\'' + pid + '\',\'' + safe + '\')">▶ Iniciar</button>' +
                        '<button class="btn btn-outline-danger btn-sm" onclick="removeFromQueue(\'' + pid + '\',\'' + safe + '\')">✕</button>' +
                    '</td></tr>';
            }).join('');
            div.innerHTML = '<div class="table-responsive"><table class="table table-hover align-middle mb-0">' +
                '<thead style="background:#fef3c7;"><tr>' +
                '<th class="ps-3">Produto</th><th class="text-center">Cliente</th>' +
                '<th class="text-center">Status</th><th class="text-center">Ação</th></tr></thead>' +
                '<tbody>' + rows + '</tbody></table></div>';
        }

        async function startFromQueue(pedidoId, produto) {
            try {
                await fetch('/api/production-queue/start', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({pedido_id: pedidoId, produto: produto})
                });
                showToast('▶ Produção Iniciada', produto + ' movido para Em Andamento.', 'success');
                openProductionChecklist(produto);
                refreshComponentTab();
            } catch(e) { showToast('Erro', 'Falha ao iniciar.', 'danger'); }
        }

        async function removeFromQueue(pedidoId, produto) {
            try {
                await fetch('/api/production-queue/remove', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({pedido_id: pedidoId, produto: produto})
                });
                refreshComponentTab();
            } catch(e) {}
        }

        const _liveTimers = {};

        function renderActiveTimers(activeProduction) {
            const div = document.getElementById('active-timers-section');
            if (!div) return;
            Object.values(_liveTimers).forEach(iv => clearInterval(iv));
            Object.keys(_liveTimers).forEach(k => delete _liveTimers[k]);

            if (!activeProduction || activeProduction.length === 0) {
                div.innerHTML = '<div class="text-center py-4"><div style="font-size:2.5rem;opacity:.3;">🏭</div>' +
                    '<p class="text-muted mt-2 mb-0">Nenhuma produção ativa.</p></div>';
                return;
            }
            const rows = activeProduction.map((p, i) => {
                const running = p.estado === 'running';
                const safe = p.produto.replace(/'/g, "\\'");
                return '<tr>' +
                    '<td class="fw-bold ps-3">' + p.produto + '</td>' +
                    '<td class="text-center font-monospace fw-bold fs-5 text-primary" id="lt_' + i + '">' + formatSeconds(p.tempo_decorrido) + '</td>' +
                    '<td class="text-center"><span class="badge ' + (running ? 'bg-success' : 'bg-warning text-dark') + '"' +
                        (running ? ' style="animation:pulse-animation 1.5s infinite;"' : '') + '>' +
                        (running ? '🟢 PRODUZINDO' : '⏸ PAUSADO') + '</span></td>' +
                    '<td class="text-center"><button class="btn btn-sm btn-outline-primary" onclick="openProductionChecklist(\'' + safe + '\')">Abrir Timer</button></td>' +
                '</tr>';
            }).join('');
            div.innerHTML = '<div class="table-responsive"><table class="table table-hover align-middle mb-0">' +
                '<thead class="table-dark"><tr><th class="ps-3">Produto</th><th class="text-center">Tempo Decorrido</th>' +
                '<th class="text-center">Status</th><th class="text-center">Ação</th></tr></thead>' +
                '<tbody>' + rows + '</tbody></table></div>';

            activeProduction.forEach((p, i) => {
                if (p.estado === 'running') {
                    let secs = p.tempo_decorrido;
                    const el = document.getElementById('lt_' + i);
                    if (!el) return;
                    _liveTimers[i] = setInterval(() => {
                        secs++;
                        if (el && el.isConnected) el.textContent = formatSeconds(secs);
                        else clearInterval(_liveTimers[i]);
                    }, 1000);
                }
            });
        }

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

        function renderMonthlySales(produtos, mes) {
            const div = document.getElementById('monthly-sales-section');
            if (!div) return;
            let entries = Array.isArray(produtos)
                ? produtos
                : Object.entries(produtos || {}).map(([nome, qtd]) => ({nome, qtd})).sort((a, b) => b.qtd - a.qtd);
            if (entries.length === 0) {
                div.innerHTML = '<div class="text-center py-4 text-muted">Nenhum produto vendido registrado este mês.</div>';
                return;
            }
            const mNames = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez'];
            let mesLabel = '';
            if (mes) { const p = mes.split('-'); if (p.length===2) mesLabel = mNames[parseInt(p[1])-1]+'/'+p[0]; }
            const hdr = mesLabel
                ? '<div class="px-3 py-2 border-bottom bg-light"><small class="text-muted">📅 ' + mesLabel + ' • ' + entries.length + ' produto(s)</small></div>'
                : '';
            const rows = entries.map(item => {
                const nome = item.nome || '';
                const qtd  = item.qtd  || 0;
                const isCadeira = nome.toUpperCase().includes('CADEIRA');
                const safe = nome.replace(/'/g, "\\'");
                return '<tr>' +
                    '<td class="ps-3 fw-bold">' + nome + '</td>' +
                    '<td class="text-center"><span class="badge fs-6" style="background:linear-gradient(135deg,#1d4ed8,#3b82f6);color:white;padding:.4rem .9rem;">' + qtd + ' un</span></td>' +
                    '<td class="text-center">' + (isCadeira
                        ? '<button class="btn btn-xs btn-outline-secondary btn-sm" onclick="showTheoreticalUsage(\'' + safe + '\',' + qtd + ')">Ver insumos</button>'
                        : '<span class="text-muted small">—</span>') + '</td></tr>';
            }).join('');
            div.innerHTML = hdr + '<div class="table-responsive"><table class="table table-hover align-middle mb-0">' +
                '<thead style="background:#f8fafc;"><tr><th class="ps-3">Produto</th><th class="text-center">Qtd Vendida</th><th class="text-center">Insumos Teóricos</th></tr></thead>' +
                '<tbody>' + rows + '</tbody></table></div>';
        }

        function renderProductionHistory(history) {
            const div = document.getElementById('production-history-section');
            if (!div) return;
            const arr = history || [];
            const reversed = [...arr].reverse();
            if (reversed.length === 0) {
                div.innerHTML = '<div class="text-center py-4 text-muted">Nenhum produto finalizado este mês.</div>';
                return;
            }
            const totalSec = arr.reduce((acc, h) => acc + (h.tempo_segundos || 0), 0);
            const summary = '<div class="px-3 py-2 border-bottom bg-light d-flex gap-4">' +
                '<span class="small fw-bold">🏭 ' + arr.length + ' produto(s) finalizado(s)</span>' +
                '<span class="small text-muted">⏱ ' + (totalSec/3600).toFixed(1) + 'h no mês</span></div>';
            const rows = reversed.map(h => {
                let dt = '—';
                try { dt = new Date(h.data_conclusao).toLocaleString('pt-BR'); } catch(e) {}
                return '<tr>' +
                    '<td class="ps-3 small text-muted">' + dt + '</td>' +
                    '<td class="fw-bold">' + (h.produto || '—') + '</td>' +
                    '<td class="text-center font-monospace fw-bold text-primary">' + formatSeconds(h.tempo_segundos) + '</td>' +
                '</tr>';
            }).join('');
            div.innerHTML = summary + '<div class="table-responsive" style="max-height:320px;overflow-y:auto;">' +
                '<table class="table table-sm table-striped align-middle mb-0">' +
                '<thead class="table-dark sticky-top"><tr><th class="ps-3">Data/Hora</th><th>Produto</th><th class="text-center">Tempo</th></tr></thead>' +
                '<tbody>' + rows + '</tbody></table></div>';
        }

        function showTheoreticalUsage(productName, qty) {
            const lines = RECIPE_CADEIRA.map(item => `<tr><td>${item.nome}</td><td class="text-center fw-bold">${(item.qtd * qty).toFixed(2)}</td><td class="text-muted small">${item.un}</td></tr>`).join('');
            const html = `<div class="modal fade" id="theoreticalModal" tabindex="-1"><div class="modal-dialog modal-dialog-scrollable"><div class="modal-content"><div class="modal-header bg-dark text-white"><h6 class="modal-title">📋 Insumos Teóricos: ${qty}x ${productName}</h6><button class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div><div class="modal-body p-0"><table class="table table-sm mb-0"><thead class="table-light"><tr><th>Insumo</th><th class="text-center">Qtd Total</th><th>Un.</th></tr></thead><tbody>${lines}</tbody></table></div></div></div></div>`;
            const old = document.getElementById('theoreticalModal');
            if (old) old.remove();
            document.body.insertAdjacentHTML('beforeend', html);
            new bootstrap.Modal(document.getElementById('theoreticalModal')).show();
        }

        function formatSeconds(s) {
            s = Math.floor(s || 0);
            const h = Math.floor(s / 3600).toString().padStart(2, '0');
            const m = Math.floor((s % 3600) / 60).toString().padStart(2, '0');
            const sec = (s % 60).toString().padStart(2, '0');
            return `${h}:${m}:${sec}`;
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

            let _wsDelay = 2000;
            wsKpi.onopen = () => { _wsDelay = 2000; };
            wsKpi.onclose = () => {
                console.log("WS desconectado. Reconectando em", _wsDelay, "ms...");
                setTimeout(() => {
                    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
                    wsKpi = new WebSocket(`${proto}://${window.location.host}/ws/kpi-updates`);
                    setupKpiWebSocket();
                    _wsDelay = Math.min(_wsDelay * 1.5, 15000);
                }, _wsDelay);
            };
        }

        setupKpiWebSocket();

        let _wsGotMsg = false;
        const _wsWatchdog = setTimeout(async () => {
            if (_wsGotMsg) return;
            console.warn("⚠️ WS sem resposta em 8s — fallback HTTP");
            try {
                const d = await fetch('/_health').then(r => r.json());
                const badge = document.getElementById('status-badge');
                const authLink = document.getElementById('auth-link');
                const authMsg = document.getElementById('auth-required-tabs');
                if (d.auth_valid) {
                    if (badge) { badge.className = 'badge bg-success'; badge.textContent = '🟢 Online'; }
                    isAuthenticated = true;
                    if (authMsg) authMsg.classList.add('hidden');
                    if (authLink) authLink.classList.add('d-none');
                } else {
                    if (badge) { badge.className = 'badge bg-danger'; badge.textContent = '🔴 Offline'; }
                }
            } catch(e) {
                const badge = document.getElementById('status-badge');
                if (badge) { badge.className = 'badge bg-danger'; badge.textContent = '🔴 Sem Conexão'; }
            }
        }, 8000);
        wsKpi.addEventListener('message', () => { if (!_wsGotMsg) { _wsGotMsg = true; clearTimeout(_wsWatchdog); } });

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
                    refreshComponentTab();
                    startComponentPolling();
                });
                componentUsageTab.addEventListener('hidden.bs.tab', () => {
                    stopComponentPolling();
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