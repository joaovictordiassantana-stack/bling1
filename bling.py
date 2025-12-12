#!/usr/bin/env python3

from gevent import monkey
monkey.patch_all()   # torna as bibliotecas padrÃ£o cooperativas com gevent (requests, socket, threading...)
"""
bling.py - Sistema completo de automaÃ§Ã£o Bling com design premium (CORRIGIDO v4.6)
Implementa OAuth 2.0, API robusta, gerenciamento de estoque/compras e dashboard web.
- CORREÃ‡ÃƒO CRÃTICA (v4.4): ImplementaÃ§Ã£o de WebSocket para notificaÃ§Ã£o em TEMPO REAL de KPIs.
- FIX SINCRONIZAÃ‡ÃƒO (v4.4): get_stats() agora forÃ§a a leitura do arquivo para sincronizaÃ§Ã£o multi-worker.
- FIX SPAM DE LOG (v4.5): Ajuste no _load_stats para evitar logs repetitivos de 'Nenhum KPI encontrado'.
- FIX SPAM DE LOG (v4.6): Reduzido nÃ­vel de log para INFO e removidos logs DEBUG repetitivos de /api/sales/stats.
- FEATURE (v4.6): HistÃ³rico de pedidos expandido de 9 para 30 dias.
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
import hmac
import hashlib

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
# ImportaÃ§Ã£o necessÃ¡ria para tratamento correto do WebSocket
try:
    from simple_websocket import ConnectionClosed
except ImportError:
    class ConnectionClosed(Exception): pass

# ============================================================================ 
# 0. VARIÃVEIS GLOBAIS DE CONTROLE (LOCK)
# ============================================================================
# Lock global para impedir mÃºltiplas trocas de token simultÃ¢neas (Erro Worker Timeout)
token_exchange_lock = Lock()

# NOVO (v4.4): VariÃ¡vel global para notificar subscribers sobre mudanÃ§as de KPI
kpi_update_callbacks = []
kpi_update_lock = Lock()
# ============================================================================ 
# 1. LOGS AVANÃ‡ADOS
# ============================================================================

class InMemoryLogHandler(logging.Handler):
    """Handler de log que armazena os registros em memÃ³ria para o WebSocket."""
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

# ConfiguraÃ§Ã£o global de diretÃ³rios e logs
LOGS_DIR = Path('logs')
LOG_FILE = LOGS_DIR / 'automacao_bling.log'
ERROR_LOG_FILE = LOGS_DIR / 'errors.log'

def setup_logging():
    LOGS_DIR.mkdir(exist_ok=True)
    global memory_handler
    memory_handler = InMemoryLogHandler()
    
    # Define o log principal para INFO (ou DEBUG se necessÃ¡rio, mas INFO Ã© o padrÃ£o)
    logger = logging.getLogger('bling_automacao')
    # FIX SPAM DE LOG (v4.6): Volta para INFO para reduzir spam de /api/sales/stats
    logger.setLevel(logging.INFO) 
    
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

# ============================================================================ 
# 2. CONFIGURAÃ‡Ã•ES
# ============================================================================

class Config:
    """ConfiguraÃ§Ãµes globais da aplicaÃ§Ã£o."""
    
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
    
    # AutomaÃ§Ã£o
    CHECK_MIN_STOCK: bool = True
    MIN_STOCK_THRESHOLD: int = 10
    DEFAULT_BATCH_SIZE: int = 10
    DELAY_BETWEEN_BATCHES: float = 0.5
    
    # Arquivos
    TOKENS_FILE: Path = Path('tokens.json')
    COMPONENT_CONFIG_FILE: Path = Path('component_config.json')
    SALES_STATS_FILE: Path = Path('sales_stats.json') # PersistÃªncia de KPIs

# ============================================================================ 
# 3. UTILITÃRIOS E AUTH (FUNÃ‡Ã•ES SEGURAS)
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
        logger.error(f"Erro lendo {path.name}: {e}")
        return {}

def save_tokens(data: Dict[str, Any], path: Path | str = "tokens.json"):
    if isinstance(path, str): path = Path(path)
    try:
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4, ensure_ascii=False)
        logger.info("Tokens salvos com sucesso.")
    except Exception as e:
        logger.error(f"Erro ao salvar tokens: {e}")

def load_stats_safe(path: Path):
    """Carrega as estatÃ­sticas de vendas de forma segura."""
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
        logger.error(f"Erro lendo {path.name}: {e}")
        return None

def save_stats(data: Dict[str, Any], path: Path):
    """Salva as estatÃ­sticas de vendas, convertendo datetime para string ISO."""
    try:
        # Cria uma cÃ³pia para evitar modificar o objeto original antes do dump
        data_to_save = data.copy()
        if 'last_recalculated' in data_to_save and isinstance(data_to_save['last_recalculated'], datetime):
            data_to_save['last_recalculated'] = data_to_save['last_recalculated'].isoformat()

        with open(path, "w", encoding="utf-8") as file:
            json.dump(data_to_save, file, indent=4, ensure_ascii=False)
        logger.info("EstatÃ­sticas de KPIs salvas com sucesso.")
    except Exception as e:
        logger.error(f"Erro ao salvar estatÃ­sticas de KPIs: {e}")

def is_token_valid(token_data):
    if not token_data:
        return False
    expires_at = token_data.get("expires_at")
    if not expires_at:
        return False
    # Checa se o tempo atual Ã© menor que o tempo de expiraÃ§Ã£o menos uma margem de seguranÃ§a de 20 segundos
    return time.time() < float(expires_at) - 20

# --- FUNÃ‡ÃƒO PARA BUSCA DE PRODUTOS (CORRIGIDO PARA V3) ---
def get_bling_products_safe(bling_client, sku: str | None = None, nome: str | None = None, access_token: str | None = None):
    try:
        filters = {}
        if sku:
            # CORREÃ‡ÃƒO: API v3 usa 'codigo' e nÃ£o 'sku'
            filters['codigo'] = sku.strip()
        if nome and not sku:
            filters['nome'] = nome.strip()

        page = 1
        all_items = []
        token = access_token or getattr(bling_client, "access_token", None)
        
        while True:
            resp = bling_client.get_products(token, page=page, limit=100, **filters)
            if not resp: 
                break
                
            items = resp.get('data') or resp.get('produtos') or []
            if isinstance(items, dict) and 'produto' in items:
                items = items.get('produto') or []
            
            if not items:
                break
                
            all_items.extend(items)
            if len(items) < 100:
                break
            page += 1
            
        return {"success": True, "data": all_items}
        
    except Exception as e:
        logger.exception("Erro na busca de produtos no Bling: %s", e)
        return {"success": False, "error": str(e)}

# ============================================================================ 
# 4. CLASSES DE DADOS E EXCEÃ‡Ã•ES (ATUALIZADO PARA RECALCULO COMPLETO)
# ============================================================================

class BlingAuthError(Exception): pass
class BlingAPIError(Exception): pass

# NOVO: EstatÃ­sticas de Vendas (Simplificado para RecÃ¡lculo)
@dataclass
class SalesManager:
    """
    Gerencia contadores de Pedidos de Venda DiÃ¡rias, Semanaais e o HistÃ³rico.
    Implementa persistÃªncia em arquivo para garantir consistÃªncia entre workers.
    """
    
    config: Config
    lock: Lock = field(default_factory=Lock)
    
    # Contadores (serÃ£o redefinidos a cada recalculate)
    daily_count: int = 0
    weekly_count: int = 0
    historic_count: int = 0
    
    # Data da Ãºltima atualizaÃ§Ã£o dos dados
    last_recalculated: datetime = field(default_factory=datetime.now)
    
    # NOVO (v4.5): Flag para controlar o log de falha inicial (Evita spam no polling)
    _initial_load_failed: bool = True 

    def __post_init__(self):
        # Carrega o estado persistido na inicializaÃ§Ã£o
        self._load_stats()


    # NOVO: Carregamento do estado persistente (FIX DE LOG)
    def _load_stats(self):
        data = load_stats_safe(self.config.SALES_STATS_FILE)
        if data:
            with self.lock:
                self.daily_count = data.get('daily', 0)
                self.weekly_count = data.get('weekly', 0)
                self.historic_count = data.get('historic', 0)
                # Usa a data carregada ou a data de inicializaÃ§Ã£o se o carregamento falhar
                self.last_recalculated = data.get('last_recalculated', datetime.now())
            # FIX SPAM DE LOG (v4.6): Altera para INFO e sÃ³ loga se nÃ£o foi a falha inicial
            if not self._initial_load_failed:  
                logger.info(f"KPIs carregados do arquivo. HistÃ³rico: {self.historic_count}.")
                self._initial_load_failed = False 
            else:
                self._initial_load_failed = False 
        else:
             # FIX (v4.5): SÃ³ loga o erro de 'Nenhum KPI encontrado' uma vez
             if self._initial_load_failed:
                 logger.debug("Nenhum KPI persistido encontrado, usando valores iniciais (0).")
                # A flag permanece True atÃ© que um load seja bem-sucedido.


    # NOVO: MÃ©todo para obter o estado a ser salvo
    def _get_state_for_save(self) -> Dict[str, Any]:
         return {
            "daily": self.daily_count,
            "weekly": self.weekly_count,
            "historic": self.historic_count,
            "last_recalculated": self.last_recalculated,
         }


    def get_stats(self) -> Dict[str, Any]:
        """Retorna todas as estatÃ­sticas em formato JSON para a API."""
        # CRÃTICO (v4.4): Sempre relÃª do arquivo para garantir sincronizaÃ§Ã£o entre workers
        self._load_stats() 
        
        with self.lock:
            # Retorna o timestamp em formato ISO para o front
            return {
                "daily": self.daily_count,
                "weekly": self.weekly_count,
                "historic": self.historic_count,
                # Retorna o timestamp de quando o worker processou por Ãºltimo
                "last_update": self.last_recalculated.isoformat() 
            }

    # MÃ‰TODO CORRIGIDO (v4.4): Adiciona notificaÃ§Ã£o via WebSocket
    def recalculate_from_orders(self, orders: List[Dict[str, Any]]):
        """Calcula KPIs baseando-se na data/hora de emissÃ£o dos pedidos."""
        now = datetime.now()
        yesterday = now - timedelta(hours=24) 
        last_week = now - timedelta(days=7)
        
        daily = 0
        weekly = 0
        historic = 0
        
        # O cÃ¡lculo Ã© feito fora do lock.
        for order in orders:
            if not isinstance(order, dict):
                logger.warning(f"Item inesperado encontrado na lista de pedidos de venda, ignorando: {order}")
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
                logger.debug(f"Pedido {order.get('id')} sem dataEmissao. Estrutura: {order.keys()}")
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
                logger.warning(f"Erro ao parsear data '{data_emissao_str}' do pedido {order.get('id')}: {e}")
                continue

            historic += 1 
                            
            if order_date >= last_week:
                weekly += 1
                            
            if order_date >= yesterday:
                daily += 1 

        # ATUALIZAÃ‡ÃƒO E PERSISTÃŠNCIA DENTRO DO LOCK
        with self.lock:
            # Atualiza todos os contadores de uma vez
            self.daily_count = daily
            self.weekly_count = weekly
            self.historic_count = historic
            self.last_recalculated = now # Atualiza o tempo de processamento
            
            # PERSISTE O ESTADO ATUAL
            save_stats(self._get_state_for_save(), self.config.SALES_STATS_FILE)
            
            # NOVO (v4.4): Notifica subscribers sobre a mudanÃ§a
            stats_data = self._get_state_for_save()
            
            # Converte a data de volta para ISO string para o WS
            stats_data['last_update'] = stats_data.pop('last_recalculated').isoformat()
            
            global kpi_update_callbacks, kpi_update_lock
            with kpi_update_lock:
                for callback in kpi_update_callbacks:
                    try:
                        callback(stats_data)
                    except Exception as e:
                        logger.error(f"Erro ao notificar KPI subscriber: {e}")
            
            logger.info(f"âœ… EstatÃ­sticas recalculadas com {len(orders)} pedidos analisados: "
                       f"DiÃ¡rio={daily}, Semanal={weekly}, HistÃ³rico={historic}")


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
        # SÃ³ gera novo state se nÃ£o estiver autenticado E nÃ£o tiver state salvo
        if self.is_authenticated():
            return "#" # JÃ¡ autenticado
            
        if self.state is None:
            self.state = secrets.token_urlsafe(16)
            self._save_state(self.state)
            
        return f"https://www.bling.com.br/Api/v3/oauth/authorize?client_id={self.config.CLIENT_ID}&redirect_uri={self.config.REDIRECT_URI}&response_type=code&scope=*/*&state={self.state}"
    
    def exchange_code_for_token(self, code: str, state: str) -> bool:
        """
        Tenta trocar o cÃ³digo OAuth por token. Implementa verificaÃ§Ã£o de Lock e State.
        """
        if self.is_authenticated():
            self.logger.info("Tentativa de callback ignorada: Token jÃ¡ vÃ¡lido.")
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
        if not self.refresh_token:
            return False
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
        # Usa margem de 60 segundos
        return bool(self.access_token and self.expires_at and time.time() < (self.expires_at - 60))
    
    def get_valid_token(self) -> Optional[str]:
        if self.is_authenticated():
            return self.access_token
        # Tenta renovar se nÃ£o for vÃ¡lido
        if self.refresh_access_token():
            return self.access_token
        return None

# CORREÃ‡ÃƒO: Adicionado limite de profundidade para evitar loop infinito
def extract_image_url(prod: dict, depth=0) -> Optional[str]:
    """Extrai URL da imagem procurando em midia, imagens e campos diretos."""
    if not prod or not isinstance(prod, dict):
        return None
    
    # ProteÃ§Ã£o contra loop
    if depth > 3: return None

    # 1. Tenta campos diretos comuns
    for key in ["imagemURL", "url", "urlThumbnail", "link", "caminho"]:
        val = prod.get(key)
        if val and isinstance(val, str) and val.startswith("http"):
            return val

    # 2. Tenta encontrar dentro de listas de mÃ­dia (padrÃ£o Bling V3)
    for list_key in ["midia", "midias", "imagens", "fotos", "anexos"]:
        items = prod.get(list_key, [])
        if isinstance(items, list):
            for item in items:
                if isinstance(item, str) and item.startswith("http"):
                    return item
                if isinstance(item, dict):
                    ret = extract_image_url(item, depth + 1)
                    if ret: return ret

    # 3. Tenta descer um nÃ­vel se houver 'data' ou 'produto' aninhado
    for nested in ["data", "produto"]:
        if nested in prod and isinstance(prod[nested], dict):
             if prod[nested].get('id') != prod.get('id'):
                 return extract_image_url(prod[nested], depth + 1)

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
                elif response.status_code == 429:  # Rate limit
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
                    # O Bling V3 retorna o objeto do produto dentro de 'data'
                    return response.json().get("data", {})
                elif response.status_code == 429:  # Rate limit
                    time.sleep(2)
                    continue
                else:
                    self.logger.warning(f"Erro API Detalhes Produto {product_id}: {response.status_code} - {response.text}")
            except Exception as e:
                self.logger.warning(f"Erro conexao API Detalhes Produto {product_id}: {e}")
            time.sleep(1)
        return {}

    def get_sales_orders(self, access_token: str, **params) -> Dict[str, Any]:
        """MÃ©todo dedicado para buscar pedidos de venda."""
        headers = {'Authorization': f'Bearer {access_token}', 'Accept': 'application/json'}
        url = f"{self.config.BLING_API_URL}/pedidos/vendas"
        
        for attempt in range(self.config.MAX_RETRIES):
            try:
                response = self.session.get(url, headers=headers, params=params, timeout=self.config.REQUEST_TIMEOUT)
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:  # Rate limit
                    time.sleep(2)
                    continue
                else:
                    self.logger.warning(f"Erro API Pedidos de Venda: {response.status_code} - {response.text}")
                    error_logger.error(f"FALHA NA BUSCA DE PEDIDOS: {response.status_code} - {response.text}") 
            except Exception as e:
                self.logger.warning(f"Erro conexao API Pedidos de Venda: {e}")
            time.sleep(1)
        return {}


# ============================================================================ 
# 6. ORQUESTRADOR (ATUALIZADO PARA RECALCULO DE VENDAS)
# ============================================================================

class AutomationOrchestrator:
    def __init__(self, config: Config, sales_manager: 'SalesManager'):
        self.config = config
        self.auth = BlingAuth(config)
        self.api_client = BlingAPIClient(config) 
        self.component_config = ComponentConfigManager(config.COMPONENT_CONFIG_FILE)
        
        self.sales_manager = sales_manager 
        
        self.kits: List[Dict[str, Any]] = []
        self.products: List[Dict[str, Any]] = []
        self.is_running: bool = False
        self.lock = Lock()
        self.recalculation_lock = Lock() 
        self.logger = logger
    
    def load_bling_products(self):
        """Worker background para carregar dados."""
        if not self.auth.is_authenticated():
            self.logger.info("Aguardando autenticaÃ§Ã£o para carregar dados...")
            return
            
        token = self.auth.get_valid_token()
        if not token:
             self.logger.warning("Token invÃ¡lido no worker.")
             return
             
        self._load_products_and_kits(token)
    
    def check_and_refresh_token(self):
        """Verifica e renova o token, se necessÃ¡rio."""
        if not self.auth.is_authenticated():
            if self.auth.refresh_access_token():
                self.logger.info("Token renovado com sucesso.")
            else:
                self.logger.warning("Falha ao renovar token. AutenticaÃ§Ã£o manual necessÃ¡ria.")

    def load_data_worker(self):
        """Worker principal que busca dados, atualiza e executa a lÃ³gica."""
        self.logger.info("Iniciando Worker de carregamento de dados e lÃ³gica.")
        
        if not self.config.CLIENT_ID or not self.config.REDIRECT_URI:
            self.logger.error("ConfiguraÃ§Ãµes BLING_CLIENT_ID/REDIRECT_URI ausentes. O worker nÃ£o pode iniciar.")
            return

        while True:
            try:
                self.check_and_refresh_token()
                
                self.load_bling_products() 
                
                # FIX: Garante que o recÃ¡lculo dos KPIs Ã© acionado
                self.process_sales_orders() 

            except Exception as e:
                self.logger.error(f"Erro grave no loop do worker: {e}. Esperando 60s antes de tentar novamente.")
                time.sleep(60)
                continue
            
            self.logger.info("Worker finalizado. PrÃ³xima execuÃ§Ã£o em 10 minutos.")
            time.sleep(600) # 10 minutos (600 segundos)

    # MÃ‰TODO CORRIGIDO (v4.2): Adiciona debounce lock
    def process_sales_orders(self):
        """Busca pedidos de venda faturados/em andamento dos Ãºltimos 30 dias e ATUALIZA O SALES_MANAGER POR RECALCULO."""
        
        if not self.recalculation_lock.acquire(blocking=False):
            self.logger.warning("RecÃ¡lculo de KPIs jÃ¡ em andamento. Ignorando nova solicitaÃ§Ã£o.")
            return
        
        try:
            token = self.auth.get_valid_token()
            if not token:
                self.logger.warning("Token indisponÃ­vel para buscar pedidos de venda.")
                return
                
            # FEATURE (v4.6): Expande o perÃ­odo de busca de 9 para 30 dias
            self.logger.info("Iniciando busca COMPLETA de pedidos de venda para recalcular os KPIs (Ãšltimos 30 dias)...")
            now = datetime.now()
            params = {
                'dataEmissaoInicial': (now - timedelta(days=30)).strftime('%Y-%m-%d'),
                'dataEmissaoFinal': now.strftime('%Y-%m-%d'),  # CRÃTICO: Adiciona data final
                'pagina': 1,
                'limite': 100,  # Aumenta limite para reduzir chamadas
            }
            # ADICIONAR apÃ³s definir params:
            self.logger.info(f"ðŸ” ParÃ¢metros da busca: {params}")

            all_orders = []
            page = 1
            MAX_PAGES = 100  # ProteÃ§Ã£o contra loop infinito (100 pÃ¡ginas * 100 itens = 10.000 pedidos max)
            while page <= MAX_PAGES:
                current_params = params.copy()
                current_params['pagina'] = page
                
                response_data = self.api_client.get_sales_orders(token, **current_params)
                
                if response_data and 'data' in response_data:
                    items = response_data['data']
                    
                    if not items:  # Lista vazia = fim dos resultados
                        break
                        
                    all_orders.extend(items)
                    
                    # Log de progresso a cada 5 pÃ¡ginas
                    if page % 5 == 0:
                        self.logger.info(f"ðŸ“„ PÃ¡gina {page}: {len(items)} pedidos carregados (Total: {len(all_orders)})")
                        
                    # Se retornou menos que o limite, Ã© a Ãºltima pÃ¡gina
                    if len(items) < current_params['limite']:
                        break
                        
                    page += 1
                    time.sleep(0.3)  # Reduz delay entre pÃ¡ginas
                else:
                    self.logger.warning(f"âš ï¸ Resposta vazia na pÃ¡gina {page}")
                    break
            
            if page > MAX_PAGES:
                self.logger.error(f"ðŸš¨ LIMITE DE PÃGINAS ATINGIDO! PossÃ­vel problema com filtro de data. Total carregado: {len(all_orders)}")

            if all_orders:
                # NOVO: Valida se os pedidos estÃ£o no perÃ­odo esperado
                now = datetime.now()
                thirty_days_ago = now - timedelta(days=30)
                
                orders_outside_range = 0
                oldest_order = None
                newest_order = None
                
                for order in all_orders:
                    data_obj = order.get('data')
                    if isinstance(data_obj, dict):
                        data_str = data_obj.get('dataEmissao')
                    elif isinstance(data_obj, str):
                        data_str = data_obj
                    else:
                        continue
                        
                    try:
                        order_date = datetime.strptime(data_str, '%Y-%m-%d')
                        
                        if not oldest_order or order_date < oldest_order:
                            oldest_order = order_date
                        if not newest_order or order_date > newest_order:
                            newest_order = order_date
                            
                        if order_date < thirty_days_ago:
                            orders_outside_range += 1
                    except:
                        pass
                        
                # Log de validaÃ§Ã£o
                if oldest_order and newest_order:
                    self.logger.info(f"ðŸ“… PerÃ­odo dos pedidos: {oldest_order.strftime('%Y-%m-%d')} atÃ© {newest_order.strftime('%Y-%m-%d')}")
                    
                if orders_outside_range > 0:
                    self.logger.warning(f"âš ï¸ ALERTA: {orders_outside_range} pedidos fora do perÃ­odo de 30 dias! "
                                      f"A API pode estar ignorando o filtro de data.")
                
                self.logger.info(f"ðŸ“Š Total de pedidos encontrados: {len(all_orders)}")
                
                # Se encontrou pedidos fora do range, filtra localmente como fallback
                if orders_outside_range > 0:
                    self.logger.warning(f"ðŸ”§ Aplicando filtro local para remover {orders_outside_range} pedidos antigos...")
                    
                    filtered_orders = []
                    for order in all_orders:
                        data_obj = order.get('data')
                        if isinstance(data_obj, dict):
                            data_str = data_obj.get('dataEmissao')
                        elif isinstance(data_obj, str):
                            data_str = data_obj
                        else:
                            continue
                            
                        try:
                            order_date = datetime.strptime(data_str, '%Y-%m-%d')
                            if order_date >= thirty_days_ago:
                                filtered_orders.append(order)
                        except:
                            filtered_orders.append(order)  # MantÃ©m pedidos sem data vÃ¡lida
                            
                    self.logger.info(f"âœ… Filtro local aplicado: {len(all_orders)} -> {len(filtered_orders)} pedidos")
                    all_orders = filtered_orders
                
                for idx, order in enumerate(all_orders[:3]):
                    data_obj = order.get('data')
                    if isinstance(data_obj, dict):
                        data_str = data_obj.get('dataEmissao', 'N/A')
                        hora_str = data_obj.get('horaEmissao', 'N/A')
                    elif isinstance(data_obj, str):
                        data_str = data_obj
                        hora_str = 'N/A'
                    else:
                        data_str = 'N/A'
                        hora_str = 'N/A'
                    self.logger.debug(f"Amostra pedido {idx+1}: Data EmissÃ£o={data_str} {hora_str}")
                    
                # Notifica o SalesManager para recalcular os KPIs
                self.sales_manager.recalculate_from_orders(all_orders)
            
            else:
                # Se nÃ£o encontrar NADA, ainda atualiza o timestamp e zera os contadores
                with self.sales_manager.lock:
                    self.sales_manager.historic_count = 0 
                    self.sales_manager.daily_count = 0
                    self.sales_manager.weekly_count = 0
                    self.sales_manager.last_recalculated = datetime.now()
                    save_stats(self.sales_manager._get_state_for_save(), self.config.SALES_STATS_FILE)
                
                self.logger.warning("âš ï¸ Busca de pedidos de venda concluÃ­da. Nenhuma resposta ou pedido encontrado no perÃ­odo.")
        except Exception as e:
            self.logger.exception(f"Erro no processamento de pedidos de venda: {e}")
        finally:
            self.recalculation_lock.release() 


    def _load_products_and_kits(self, access_token: str):
        self.logger.info("Iniciando carga otimizada de produtos e kits...")
        self.kits.clear()
        self.products.clear()
        
        todos_produtos = []
        page = 1
        
        # PASSO 1: Baixar TUDO primeiro (PaginaÃ§Ã£o)
        while True:
            try:
                resp = self.api_client.get_products(access_token, page=page, limit=100)
                items = resp.get('data', [])
                
                if not items:
                    break
                
                todos_produtos.extend(items)
                
                if len(items) < 100:
                    break
                    
                page += 1
                time.sleep(0.2) 
            except Exception as e:
                self.logger.error(f"Erro ao carregar pÃ¡gina {page}: {e}")
                break
        
        # PASSO 2: Criar Mapa para busca rÃ¡pida (ID -> Produto)
        produto_map = {str(p.get("id")): p for p in todos_produtos}
        
        self.logger.info(f"Total baixado: {len(todos_produtos)}. Processando Kits...")

        # PASSO 3: Separar Kits e preencher nomes dos componentes
        for p in todos_produtos:
            p_id = p.get("id")
            
            estrutura = p.get("estrutura", {})
            componentes = estrutura.get("componentes", [])
            
            eh_kit = len(componentes) > 0 or p.get("tipo") == "K" or p.get("formato") == "K"

            img_url = extract_image_url(p)
            
            if eh_kit:
                comps_formatados = []
                
                if not componentes and p_id:
                     try:
                         det = self.api_client.get_product_details(access_token, p_id)
                         componentes = det.get("estrutura", {}).get("componentes", [])
                         if not img_url: img_url = extract_image_url(det)
                     except:
                         pass

                for c in componentes:
                    filho_ref = c.get("produto", {})
                    filho_id = str(filho_ref.get("id"))
                    
                    produto_filho = produto_map.get(filho_id)
                    
                    nome_final = "Item nÃ£o carregado"
                    if produto_filho:
                        nome_final = produto_filho.get("nome")
                    elif filho_ref.get("nome"):
                        nome_final = filho_ref.get("nome")
                    
                    comps_formatados.append({
                        "nome": nome_final,
                        "quantidade": c.get("quantidade", 0),
                        "sku": produto_filho.get("codigo") if produto_filho else ""
                    })

                self.kits.append({
                    "id": p_id,
                    "sku": p.get("codigo"),
                    "produto": p.get("nome"), 
                    "imagemURL": img_url,
                    "componentes": comps_formatados
                })
            else:
                self.products.append({
                    "id": p.get("id"),
                    "sku": p.get("codigo"),
                    "produto": p.get("nome"), 
                    "imagemURL": img_url,
                    "tipo": p.get("tipo"),
                    "situacao": p.get("situacao"),
                    "preco": p.get("preco"),
                    "estoque": p.get("estoqueAtual", 0)
                })

        self.logger.info(f"Processamento final: {len(self.kits)} kits, {len(self.products)} produtos.")

    def get_all_products(self) -> List[Dict[str, Any]]:
        return self.products

    def get_all_kits(self) -> List[Dict[str, Any]]:
        return self.kits

    def run_purchase_check(self, create_orders=False):
        self.logger.info("VerificaÃ§Ã£o de compras iniciada (SimulaÃ§Ã£o).")
        return True

# InstÃ¢ncias Globais
config = Config()

if not config.REDIRECT_URI:
    logger.error("ERRO FATAL: BLING_REDIRECT_URI nÃ£o configurada no Render")
    pass

sales_manager = SalesManager(config) 
orchestrator = AutomationOrchestrator(config, sales_manager) 
auth = orchestrator.auth

# ============================================================================ 
# 7. DECORADOR (TOKEN REQUIRED AJUSTADO)
# ============================================================================

def token_required(f):
    """Decorador para verificar se o token de acesso estÃ¡ disponÃ­vel e vÃ¡lido."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not orchestrator.auth or not orchestrator.auth.is_authenticated():
            orchestrator.auth.logger.warning("Request sem auth vÃ¡lida: retornando 401 json")
            return jsonify({"needAuth": True, "message": "Token expirado ou invÃ¡lido"}), 401

        token = orchestrator.auth.get_valid_token()
        if not token:
            return jsonify({"needAuth": True, "message": "Falha no refresh token"}), 401
        return f(token=token, *args, **kwargs)
    return decorated

# ============================================================================ 
# 9. TEMPLATE HTML DO DASHBOARD (ATUALIZADO V4.6)
# ============================================================================

# -*- coding: utf-8 -*-

<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel Bling - Sw Móveis</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
    <style>
        body { background: #f8f9fa; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        .navbar { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
        .log-box { font-family: 'Courier New', monospace; font-size: .85em; background: #1e1e1e; color: #d4d4d4; border-radius: .5rem; padding: 1rem; max-height: 400px; overflow-y: auto; }
        .log-level-INFO { color: #4ec9b0; }
        .log-level-WARNING { color: #dcdcaa; }
        .log-level-ERROR { color: #f48771; }
        .log-level-DEBUG { color: #569cd6; }
        .hidden { display: none; }
        .kpi-card { border-left: 5px solid; transition: background-color 0.5s ease; }
        .kpi-daily { border-left-color: #0d6efd; }
        .kpi-weekly { border-left-color: #ffc107; }
        .kpi-historic { border-left-color: #198754; }
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
    
    function updateKpis(dSalesStats) {
        document.getElementById('kpi-daily').textContent = dSalesStats.daily;
        document.getElementById('kpi-weekly').textContent = dSalesStats.weekly;
        document.getElementById('kpi-historic').textContent = dSalesStats.historic;
        document.getElementById('last-recalculated').textContent = formatDateTime(dSalesStats.last_update);
    }
    
    async function checkStatus() {
        try {
            const rStatus = await fetch(API + '/status');
            const dStatus = await rStatus.json();
            const badge = document.getElementById('status-badge');
            
            isAuthenticated = dStatus.authenticated;
            
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
            document.getElementById('auth-link').href = dStatus.auth_url;

            if (isAuthenticated) {
                const rSalesStats = await fetch(API + '/sales/stats');
            
                if (rSalesStats.ok) {
                    const dSalesStats = await rSalesStats.json();
                    updateKpis(dSalesStats);
                } else {
                    document.getElementById('kpi-daily').textContent = 0;
                    document.getElementById('kpi-weekly').textContent = 0;
                    document.getElementById('kpi-historic').textContent = 0;
                    document.getElementById('last-recalculated').textContent = 'ERRO API';
                }
            } else {
                 document.getElementById('kpi-daily').textContent = 0;
                 document.getElementById('kpi-weekly').textContent = 0;
                 document.getElementById('kpi-historic').textContent = 0;
                 document.getElementById('last-recalculated').textContent = 'N/D - AUTENTIQUE';
            }
        } catch (e) {
            console.error("Erro ao checar status ou stats:", e);
        }
    }
    
    checkStatus();
    setInterval(checkStatus, 5000);
    
    const protoKpi = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const wsKpi = new WebSocket(`${protoKpi}://${window.location.host}/ws/kpi-updates`);
    
    wsKpi.onmessage = (e) => {
        const data = JSON.parse(e.data);
        
        if (data.type === 'kpi_update') {
            const stats = data.data;
            console.log("📊 KPI atualizado em tempo real:", stats);
            updateKpis(stats);
            
            const cards = document.querySelectorAll('.kpi-card');
            cards.forEach(card => {
                card.style.backgroundColor = '#e8f5e9';
                setTimeout(() => {
                    card.style.backgroundColor = '';
                }, 500);
            });
        }
    };
    
    wsKpi.onerror = (e) => {
        console.error("Erro WebSocket KPI:", e);
    };
    
    wsKpi.onclose = () => {
        console.log("WebSocket KPI desconectado. Reconectando em 5s...");
        setTimeout(() => {
            location.reload();
        }, 5000);
    };

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
                    html += `
                        <div class="list-group-item">
                            <div class="d-flex">
                                <img src="${p.imagemURL || ''}" 
                                     style="width:60px;height:60px;object-fit:contain;margin-right:10px;border-radius:6px;background:#f1f1f1"
                                     onerror="this.style.display='none'">
                                
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
                div.innerHTML = `<div class="alert alert-danger">Erro: ${e}</div>`;
            }
        };

        async function loadKits() {
            const div = document.getElementById('kits-list');
            const authRequiredDiv = document.getElementById('auth-required-kits');
            
            if (!isAuthenticated) {
                div.innerHTML = '';
                authRequiredDiv.classList.remove('hidden');
                return;
            }
            
            authRequiredDiv.classList.add('hidden');
            div.innerHTML = '<div class="alert alert-info">Carregando dados. Este processo depende da finalização do cache em segundo plano (Worker) e pode demorar alguns minutos.</div>';
            
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
                        comps = `<span class="text-muted" style="font-size:0.8em">Produto Simples (Estoque: ${k.estoque || 'N/D'})</span>`;
                    }

                    html += `
                        <tr>
                            <td style="width:60px">${imgHtml}</td>
                            <td style="width:120px; font-weight:bold;">${k.sku || ''}</td>
                            <td>${k.produto || 'N/D'}</td>
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


# ============================================================================ 
# 8. SERVIDOR WEB (ROTAS CONSOLIDADAS - ATUALIZADO V4.6)
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
        global sales_manager

        if not self.orchestrator.config.REDIRECT_URI:
            @self.app.route('/', defaults={'path': ''})
            @self.app.route('/<path:path>')
            def fatal_error_config(path):
                from flask import abort
                self.logger.error("ERRO FATAL: BLING_REDIRECT_URI nÃ£o configurada no Render")
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
                self.logger.info("Callback ignorado: UsuÃ¡rio jÃ¡ autenticado.")
                return redirect('/')

            if not code or not state:
                return redirect('/') 

            if not token_exchange_lock.acquire(blocking=False):
                self.logger.warning("ConcorrÃªncia detectada no callback. Redirecionando para home.")
                return redirect('/')
                
            try:
                with WebServer.code_lock:
                    if code in WebServer.used_codes:
                        return redirect('/')
                    WebServer.used_codes.add(code)
                
                self.logger.info(f"Processando callback code...")
                success = self.orchestrator.auth.exchange_code_for_token(code, state)
                
                return redirect('/')
            except Exception as e:
                self.logger.error(f"Erro crÃ­tico no callback: {e}")
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

        # ENDPOINT DE ESTATÃSTICAS DE VENDAS (AGORA CORRIGIDO COM RE-LEITURA)
        @self.app.route("/api/sales/stats")
        def api_sales_stats():
            """Retorna os contadores DiÃ¡rio, Semanal e HistÃ³rico."""
            stats = sales_manager.get_stats()
            
            # FIX SPAM DE LOG (v4.6): Removido o log DEBUG que causava spam no console
            
            return jsonify(stats)

        @self.app.route("/api/all_products", methods=["GET"])
        @token_required
        def api_all_products(token):
            return jsonify(self.orchestrator.get_all_products())

        @self.app.route('/api/product/search', methods=["GET"])
        @token_required
        def api_product_search(token):
            termo = request.args.get("q") or request.args.get("sku") or request.args.get("nome") or ""
            termo = termo.strip() 
            if not termo:
                return jsonify([])

            all_results_base = []
            seen_ids = set()

            def process_response(resp_data):
                """Processa resposta da API e adiciona Ã  lista de resultados bÃ¡sicos"""
                items = resp_data.get('data') or []
                for p in items:
                    p_id = p.get('id')
                    if p_id and p_id in seen_ids:
                        continue
                    if p_id: seen_ids.add(p_id)
                    
                    all_results_base.append({
                        "id": p.get("id"),
                        "sku": p.get("codigo"),
                        "nome": p.get("nome"),
                        "tipo": p.get("tipo"),
                        "situacao": p.get("situacao"),
                        "preco": p.get("preco"),
                    })

            self.logger.info(f"Buscando API por CÃ“DIGO: {termo}")
            resp_sku = self.orchestrator.api_client.get_products(token, codigo=termo, limit=20)
            process_response(resp_sku)

            self.logger.info(f"Buscando API por NOME: {termo}")
            resp_nome = self.orchestrator.api_client.get_products(token, nome=termo, limit=20)
            process_response(resp_nome)

            final_results = []
            MAX_DETALHES = 10 
            
            for idx, p in enumerate(all_results_base):
                if idx >= MAX_DETALHES:
                    break
                    
                try:
                    details = self.orchestrator.api_client.get_product_details(token, p["id"])
                except Exception as e:
                    self.orchestrator.logger.exception("Erro ao buscar detalhe produto %s", p["id"])
                    details = {}
                
                estoque_val = (
                    details.get("estoqueAtual")
                    or details.get("saldoDisponivel")
                    or details.get("estoque", {}).get("saldoVirtualTotal", 0)
                )

                produto_completo = {
                    "id": p["id"],
                    "sku": p.get("sku"),
                    "nome": p.get("nome"),
                    "produto": p.get("nome"), 
                    "tipo": p.get("tipo"),
                    "situacao": p.get("situacao"),
                    "preco": p.get("preco"),
                    "estoque": estoque_val,
                    "descricaoCurta": details.get("descricaoCurta"),
                    "componentes": [
                         {
                            "nome": c.get("produto", {}).get("nome", "Sem nome"),
                            "quantidade": c.get("quantidade", 0),
                            "sku": c.get("produto", {}).get("codigo", "N/D")
                         }
                        for c in details.get("estrutura", {}).get("componentes", [])
                    ],
                    "imagemURL": extract_image_url(details), 
                }
                final_results.append(produto_completo)
            
            kits_cache = self.orchestrator.get_all_kits()
            termo_lower = termo.lower()
            
            for kit in kits_cache:
                if kit.get("id") not in seen_ids and (termo_lower in str(kit.get("produto", "")).lower() or termo_lower in str(kit.get("sku", "")).lower()):
                    final_results.append(kit)
                    seen_ids.add(kit.get("id")) 
            
            produtos_cache = self.orchestrator.get_all_products()
            for prod in produtos_cache:
                if prod.get("id") not in seen_ids and (termo_lower in str(prod.get("produto", "")).lower() or termo_lower in str(prod.get("sku", "")).lower()):
                    final_results.append(prod)
                    seen_ids.add(prod.get("id"))

            return jsonify(final_results)


        @self.app.route('/api/kits', methods=["GET"])
        @token_required
        def api_kits(token):
            """Retorna todos os produtos (kits e simples) carregados em cache."""
            return jsonify(self.orchestrator.get_all_kits() + self.orchestrator.get_all_products())

        @self.app.route("/webhook/bling", methods=["POST"])
        def webhook_bling():
            payload = request.get_data()
            signature_header = request.headers.get('X-Bling-Signature-256', '')

            try:
                expected_signature = 'sha256=' + hmac.new(
                    self.orchestrator.config.CLIENT_SECRET.encode(), 
                    payload,
                    hashlib.sha256
                ).hexdigest()

                if not hmac.compare_digest(signature_header, expected_signature):
                    self.logger.warning(f"âŒ Assinatura invÃ¡lida no Webhook. Header: {signature_header}")
                    return jsonify({"error": "Invalid signature"}), 401
                    
                self.logger.info("âœ… Assinatura HMAC do Webhook validada com sucesso.")

                data = request.get_json(silent=True)
                if not data:
                    return jsonify({"status": "ok"}), 200 
                
                event_type = data.get('event', '')
                
                if not self.orchestrator.auth.is_authenticated():
                    self.logger.warning("âš ï¸ Webhook recebido, mas token Bling nÃ£o Ã© vÃ¡lido. Ignorando recÃ¡lculo.")
                    return jsonify({"status": "ok", "note": "awaiting_auth"}), 200

                if 'order' in event_type: 
                    self.logger.info(f"RecÃ¡lculo de KPIs de Vendas acionado pelo Webhook para evento: {event_type}.")
                    Thread(target=self.orchestrator.process_sales_orders, daemon=True).start()

            except Exception as e:
                self.logger.exception(f"Erro no webhook: {e}")
                
            return jsonify({"status": "ok"}), 200

    def setup_websocket(self):
        global kpi_update_callbacks, kpi_update_lock

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

        # NOVO (v4.4): WebSocket para notificaÃ§Ãµes de KPI em tempo real
        @self.sock.route('/ws/kpi-updates')
        def ws_kpi_updates(ws):
            logger.info("ðŸ“¡ WebSocket KPI conectado.")
            
            def notify_kpi(stats):
                """Callback para notificar via WebSocket quando KPI muda."""
                try:
                    # stats jÃ¡ estÃ¡ no formato JSON-ready {'daily': 2, 'weekly': 13, 'historic': 2287, 'last_update': '2025-12-12T14:40:00.000000'}
                    ws.send(json.dumps({"type": "kpi_update", "data": stats}))
                    logger.debug(f"ðŸ“¤ KPI update enviado via WebSocket: {stats}")
                except ConnectionClosed:
                    pass
                except Exception as e:
                    logger.warning(f"Erro ao enviar KPI update: {e}")
            
            # Registra esse callback
            with kpi_update_lock:
                kpi_update_callbacks.append(notify_kpi)
            
            try:
                # O loop sÃ³ precisa manter a conexÃ£o viva
                while True:
                    try:
                        ws.receive(timeout=5)  # Keepalive
                    except ConnectionClosed:
                        break
                    except Exception:
                        pass
            finally:
                # Remove o callback quando desconectar
                with kpi_update_lock:
                    if notify_kpi in kpi_update_callbacks:
                        kpi_update_callbacks.remove(notify_kpi)
                logger.info("WebSocket KPI desconectado.")

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

# --- GUNICORN CONFIGURAÃ‡Ã•ES (TIMEOUT AJUSTADO PARA 300) ---
import os as _os
_os.environ.setdefault("GUNICORN_CMD_ARGS", "--worker-class gevent --timeout 300 --keep-alive 5")
APP_PORT = int(_os.getenv("PORT", "10000"))