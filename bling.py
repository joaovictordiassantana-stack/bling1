#!/usr/bin/env python3

from gevent import monkey
monkey.patch_all()   # torna as bibliotecas padrão cooperativas com gevent (requests, socket, threading...)
"""
bling.py - Sistema completo de automação Bling com design premium (CORRIGIDO v4.7)
Implementa OAuth 2.0, API robusta, gerenciamento de estoque/compras e dashboard web.
- CORREÇÃO CRÍTICA (v4.4): Implementação de WebSocket para notificação em TEMPO REAL de KPIs.
- FIX SINCRONIZAÇÃO (v4.4): get_stats() agora força a leitura do arquivo para sincronização multi-worker.
- FIX SPAM DE LOG (v4.5): Ajuste no _load_stats para evitar logs repetitivos de 'Nenhum KPI encontrado'.
- FIX CRÍTICO (v4.7): Corrigido filtro de data na busca de pedidos (adicionado dataEmissaoFinal + validação local).
- FIX SPAM (v4.7): Melhorada paginação com limite de segurança e logs informativos.
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

# NOVO (v4.4): Variável global para notificar subscribers sobre mudanças de KPI
kpi_update_callbacks = []
kpi_update_lock = Lock()
# ============================================================================ 
# 1. LOGS AVANÇADOS
# ============================================================================

class InMemoryLogHandler(logging.Handler):
    """Handler de log que armazena os registros em memória para o WebSocket."""
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
    # Ajustado para DEBUG para incluir os novos logs de /api/sales/stats
    logger.setLevel(logging.DEBUG) 
    
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
    
    # Automação
    CHECK_MIN_STOCK: bool = True
    MIN_STOCK_THRESHOLD: int = 10
    DEFAULT_BATCH_SIZE: int = 10
    DELAY_BETWEEN_BATCHES: float = 0.5
    
    # Arquivos
    TOKENS_FILE: Path = Path('tokens.json')
    COMPONENT_CONFIG_FILE: Path = Path('component_config.json')
    SALES_STATS_FILE: Path = Path('sales_stats.json') # Persistência de KPIs

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
        logger.error(f"Erro lendo {path.name}: {e}")
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
        logger.error(f"Erro ao salvar estatísticas de KPIs: {e}")

def is_token_valid(token_data):
    if not token_data:
        return False
    expires_at = token_data.get("expires_at")
    if not expires_at:
        return False
    # Checa se o tempo atual é menor que o tempo de expiração menos uma margem de segurança de 20 segundos
    return time.time() < float(expires_at) - 20

# --- FUNÇÃO PARA BUSCA DE PRODUTOS (CORRIGIDO PARA V3) ---
def get_bling_products_safe(bling_client, sku: str | None = None, nome: str | None = None, access_token: str | None = None):
    try:
        filters = {}
        if sku:
            # CORREÇÃO: API v3 usa 'codigo' e não 'sku'
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
# 4. CLASSES DE DADOS E EXCEÇÕES (ATUALIZADO PARA RECALCULO COMPLETO)
# ============================================================================

class BlingAuthError(Exception): pass
class BlingAPIError(Exception): pass

# NOVO: Estatísticas de Vendas (Simplificado para Recálculo)
@dataclass
class SalesManager:
    """
    Gerencia contadores de Pedidos de Venda Diárias, Semanaais e o Histórico.
    Implementa persistência em arquivo para garantir consistência entre workers.
    """
    
    config: Config
    lock: Lock = field(default_factory=Lock)
    
    # Contadores (serão redefinidos a cada recalculate)
    daily_count: int = 0
    weekly_count: int = 0
    historic_count: int = 0
    
    # Data da última atualização dos dados
    last_recalculated: datetime = field(default_factory=datetime.now)
    
    # NOVO (v4.5): Flag para controlar o log de falha inicial (Evita spam no polling)
    _initial_load_failed: bool = True 

    def __post_init__(self):
        # Carrega o estado persistido na inicialização
        self._load_stats()


    # NOVO: Carregamento do estado persistente (FIX DE LOG)
    def _load_stats(self):
        data = load_stats_safe(self.config.SALES_STATS_FILE)
        if data:
            with self.lock:
                self.daily_count = data.get('daily', 0)
                self.weekly_count = data.get('weekly', 0)
                self.historic_count = data.get('historic', 0)
                # Usa a data carregada ou a data de inicialização se o carregamento falhar
                self.last_recalculated = data.get('last_recalculated', datetime.now())
            logger.debug(f"KPIs carregados do arquivo. Histórico: {self.historic_count}.")
            # Se carregou com sucesso, reseta a flag
            self._initial_load_failed = False 
        else:
             # FIX (v4.5): Só loga o erro de 'Nenhum KPI encontrado' uma vez
             if self._initial_load_failed:
                 logger.debug("Nenhum KPI persistido encontrado, usando valores iniciais (0).")
                # A flag permanece True até que um load seja bem-sucedido.


    # NOVO: Método para obter o estado a ser salvo
    def _get_state_for_save(self) -> Dict[str, Any]:
         return {
            "daily": self.daily_count,
            "weekly": self.weekly_count,
            "historic": self.historic_count,
            "last_recalculated": self.last_recalculated,
         }


    def get_stats(self) -> Dict[str, Any]:
        """Retorna todas as estatísticas em formato JSON para a API."""
        # CRÍTICO (v4.4): Sempre relê do arquivo para garantir sincronização entre workers
        self._load_stats() 
        
        with self.lock:
            # Retorna o timestamp em formato ISO para o front
            return {
                "daily": self.daily_count,
                "weekly": self.weekly_count,
                "historic": self.historic_count,
                # Retorna o timestamp de quando o worker processou por último
                "last_update": self.last_recalculated.isoformat() 
            }

    # MÉTODO CORRIGIDO (v4.4): Adiciona notificação via WebSocket
    def recalculate_from_orders(self, orders: List[Dict[str, Any]]):
        """Calcula KPIs baseando-se na data/hora de emissão dos pedidos."""
        now = datetime.now()
        yesterday = now - timedelta(hours=24) 
        last_week = now - timedelta(days=7)
        
        daily = 0
        weekly = 0
        historic = 0
        
        # O cálculo é feito fora do lock.
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

        # ATUALIZAÇÃO E PERSISTÊNCIA DENTRO DO LOCK
        with self.lock:
            # Atualiza todos os contadores de uma vez
            self.daily_count = daily
            self.weekly_count = weekly
            self.historic_count = historic
            self.last_recalculated = now # Atualiza o tempo de processamento
            
            # PERSISTE O ESTADO ATUAL
            save_stats(self._get_state_for_save(), self.config.SALES_STATS_FILE)
            
            # NOVO (v4.4): Notifica subscribers sobre a mudança
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
            
            logger.info(f"✅ Estatísticas recalculadas com {len(orders)} pedidos analisados: "
                       f"Diário={daily}, Semanal={weekly}, Histórico={historic}")


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
        # Só gera novo state se não estiver autenticado E não tiver state salvo
        if self.is_authenticated():
            return "#" # Já autenticado
            
        if self.state is None:
            self.state = secrets.token_urlsafe(16)
            self._save_state(self.state)
            
        return f"https://www.bling.com.br/Api/v3/oauth/authorize?client_id={self.config.CLIENT_ID}&redirect_uri={self.config.REDIRECT_URI}&response_type=code&scope=*/*&state={self.state}"
    
    def exchange_code_for_token(self, code: str, state: str) -> bool:
        """
        Tenta trocar o código OAuth por token. Implementa verificação de Lock e State.
        """
        if self.is_authenticated():
            self.logger.info("Tentativa de callback ignorada: Token já válido.")
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
        # Tenta renovar se não for válido
        if self.refresh_access_token():
            return self.access_token
        return None

# CORREÇÃO: Adicionado limite de profundidade para evitar loop infinito
def extract_image_url(prod: dict, depth=0) -> Optional[str]:
    """Extrai URL da imagem procurando em midia, imagens e campos diretos."""
    if not prod or not isinstance(prod, dict):
        return None
    
    # Proteção contra loop
    if depth > 3: return None

    # 1. Tenta campos diretos comuns
    for key in ["imagemURL", "url", "urlThumbnail", "link", "caminho"]:
        val = prod.get(key)
        if val and isinstance(val, str) and val.startswith("http"):
            return val

    # 2. Tenta encontrar dentro de listas de mídia (padrão Bling V3)
    for list_key in ["midia", "midias", "imagens", "fotos", "anexos"]:
        items = prod.get(list_key, [])
        if isinstance(items, list):
            for item in items:
                if isinstance(item, str) and item.startswith("http"):
                    return item
                if isinstance(item, dict):
                    ret = extract_image_url(item, depth + 1)
                    if ret: return ret

    # 3. Tenta descer um nível se houver 'data' ou 'produto' aninhado
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
        """Método dedicado para buscar pedidos de venda."""
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
            self.logger.info("Aguardando autenticação para carregar dados...")
            return
            
        token = self.auth.get_valid_token()
        if not token:
             self.logger.warning("Token inválido no worker.")
             return
             
        self._load_products_and_kits(token)
    
    def check_and_refresh_token(self):
        """Verifica e renova o token, se necessário."""
        if not self.auth.is_authenticated():
            if self.auth.refresh_access_token():
                self.logger.info("Token renovado com sucesso.")
            else:
                self.logger.warning("Falha ao renovar token. Autenticação manual necessária.")

    def load_data_worker(self):
        """Worker principal que busca dados, atualiza e executa a lógica."""
        self.logger.info("Iniciando Worker de carregamento de dados e lógica.")
        
        if not self.config.CLIENT_ID or not self.config.REDIRECT_URI:
            self.logger.error("Configurações BLING_CLIENT_ID/REDIRECT_URI ausentes. O worker não pode iniciar.")
            return

        while True:
            try:
                self.check_and_refresh_token()
                
                self.load_bling_products() 
                
                # FIX: Garante que o recálculo dos KPIs é acionado
                self.process_sales_orders() 

            except Exception as e:
                self.logger.error(f"Erro grave no loop do worker: {e}. Esperando 60s antes de tentar novamente.")
                time.sleep(60)
                continue
            
            self.logger.info("Worker finalizado. Próxima execução em 10 minutos.")
            time.sleep(600) # 10 minutos (600 segundos)

    # MÉTODO CORRIGIDO (v4.7): Adiciona filtro de 30 dias com dataEmissaoFinal e paginação segura.
    def process_sales_orders(self):
        """Busca pedidos de venda faturados/em andamento e ATUALIZA O SALES_MANAGER POR RECALCULO."""
        
        if not self.recalculation_lock.acquire(blocking=False):
            self.logger.warning("Recálculo de KPIs já em andamento. Ignorando nova solicitação.")
            return
        try:
            token = self.auth.get_valid_token()
            if not token:
                self.logger.warning("Token indisponível para buscar pedidos de venda.")
                return

            # 1. CORRIGIR A BUSCA DE PEDIDOS (Filtro de Data)
            self.logger.info("Iniciando busca COMPLETA de pedidos de venda para recalcular os KPIs (Últimos 30 dias)...")
            now = datetime.now()
            params = {
                'dataEmissaoInicial': (now - timedelta(days=30)).strftime('%Y-%m-%d'),
                'dataEmissaoFinal': now.strftime('%Y-%m-%d'),  # CRÍTICO: Adiciona data final
                'pagina': 1,
                'limite': 100,  # Aumenta limite para reduzir chamadas
            }
            # 2. ADICIONAR LOG DE DEBUG TEMPORÁRIO
            self.logger.info(f"🔍 Parâmetros da busca: {params}")

            # 3. MELHORAR A LÓGICA DE PAGINAÇÃO
            all_orders = []
            page = 1
            MAX_PAGES = 100  # Proteção contra loop infinito (100 páginas * 100 itens = 10.000 pedidos max)
            while page <= MAX_PAGES:
                current_params = params.copy()
                current_params['pagina'] = page

                response_data = self.api_client.get_sales_orders(token, **current_params)

                if response_data and 'data' in response_data:
                    items = response_data['data']

                    if not items:  # Lista vazia = fim dos resultados
                        break

                    all_orders.extend(items)

                    # Log de progresso a cada 5 páginas
                    if page % 5 == 0:
                        self.logger.info(f"📄 Página {page}: {len(items)} pedidos carregados (Total: {len(all_orders)})")

                    # Se retornou menos que o limite, é a última página
                    if len(items) < current_params['limite']:
                        break

                    page += 1
                    time.sleep(0.3)  # Reduz delay entre páginas
                else:
                    self.logger.warning(f"⚠️ Resposta vazia na página {page}")
                    break
            
            if page > MAX_PAGES:
                self.logger.error(f"🚨 LIMITE DE PÁGINAS ATINGIDO! Possível problema com filtro de data. Total carregado: {len(all_orders)}")

            # 4. ADICIONAR VALIDAÇÃO DOS PEDIDOS CARREGADOS
            if all_orders:
                # NOVO: Valida se os pedidos estão no período esperado
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
                
                # Log de validação
                if oldest_order and newest_order:
                    self.logger.info(f"📅 Período dos pedidos: {oldest_order.strftime('%Y-%m-%d')} até {newest_order.strftime('%Y-%m-%d')}")

                if orders_outside_range > 0:
                    self.logger.warning(f"⚠️ ALERTA: {orders_outside_range} pedidos fora do período de 30 dias! "
                                      f"A API pode estar ignorando o filtro de data.")
                    self.logger.info(f"📊 Total de pedidos ANTES do filtro: {len(all_orders)}")

                # 5. ALTERNATIVA: FILTRAR LOCALMENTE SE A API FALHAR
                # Se encontrou pedidos fora do range, filtra localmente como fallback
                if orders_outside_range > 0:
                    self.logger.warning(f"🔧 Aplicando filtro local para remover {orders_outside_range} pedidos antigos...")

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
                            filtered_orders.append(order)  # Mantém pedidos sem data válida

                    self.logger.info(f"✅ Filtro local aplicado: {len(all_orders)} -> {len(filtered_orders)} pedidos")
                    all_orders = filtered_orders
            
            # Recalculate KPIs using the potentially filtered list
            if all_orders:
                self.logger.info(f"📊 Total de pedidos ENCONTRADOS/PROCESSADOS: {len(all_orders)}")
                for idx, order in enumerate(all_orders[:3]):
                    data_obj = order.get('data')
                    if isinstance(data_obj, dict):
                        data_str = data_obj.get('dataEmissao', 'N/A')
                        hora_str = data_obj.get('horaEmissao', 'N/A')
                    elif isinstance(data_obj, str):
                        data_str = data_obj
                        hora_str = "N/A"
                    else:
                        data_str = "ERRO: tipo inesperado"
                        hora_str = "N/A"
                    total_val = order.get('total', 0)
                    self.logger.info(f" [{idx+1}] ID: {order.get('id')}, "
                                   f"Data: {data_str}, Hora: {hora_str}, "
                                   f"Total: R$ {total_val}")
                self.logger.info(f"🔄 Iniciando recalculate_from_orders com {len(all_orders)} pedidos...")
                self.sales_manager.recalculate_from_orders(all_orders)
            else:
                self.logger.info("Nenhum pedido de venda encontrado no período.")
                
        finally:
            self.recalculation_lock.release()

    def _load_products_and_kits(self, token: str):
        """Carrega todos os produtos e identifica kits (composições)."""
        self.logger.info("Iniciando carregamento e análise de todos os produtos do Bling.")
        try:
            # 1. Busca todos os produtos ativos (usando a função segura)
            result = get_bling_products_safe(self.api_client, access_token=token)

            if not result['success']:
                self.logger.error(f"Falha ao carregar produtos: {result['error']}")
                return

            all_products = result['data']
            kits = []
            regular_products = []

            # 2. Classificação de Produtos e Kits
            for prod_data in all_products:
                # O produto em si está aninhado em 'produto' na API v3
                product = prod_data.get('produto', prod_data)
                
                # Assume-se que um produto com 'tipo' 'C' (Composição/Kit) é um kit
                if product.get('tipo') == 'C':
                    kits.append(product)
                else:
                    regular_products.append(product)

            with self.lock:
                self.products = regular_products
                self.kits = kits
            
            self.logger.info(f"✅ Produtos carregados: {len(all_products)} encontrados. "
                             f"({len(regular_products)} produtos normais, {len(kits)} kits)")

            # 3. Executa a lógica de automação de kits em thread separada para não bloquear
            if self.config.CHECK_MIN_STOCK and self.kits:
                Thread(target=self._update_kit_stock_worker, args=(token, kits), daemon=True).start()

        except Exception as e:
            self.logger.error(f"Erro no carregamento principal de produtos: {e}")

    def _update_kit_stock_worker(self, token: str, kits: List[Dict[str, Any]]):
        """Worker que atualiza o estoque de Kits baseado na composição e estoque dos componentes."""
        self.logger.info(f"Iniciando worker de atualização de estoque para {len(kits)} kits.")
        updated_count = 0
        
        for kit in kits:
            try:
                # 1. Busca detalhes do Kit para obter a composição (API V3)
                kit_details = self.api_client.get_product_details(token, kit.get('id'))
                
                if not kit_details:
                    self.logger.warning(f"Não foi possível obter detalhes do kit {kit.get('codigo')}.")
                    continue
                
                # Assume-se que o estoque é o do primeiro depósito (padrão)
                kit_stock = kit_details.get('estoque', {}).get('saldo', 0)
                
                # 2. Encontra o estoque mínimo baseado nos componentes
                component_stock_limit = self._calculate_component_limit(kit_details)
                
                if component_stock_limit is not None:
                    # 3. Calcula o novo estoque desejado para o KIT
                    # O novo estoque é o mínimo do componente menos o limite. Se < 0, é 0.
                    
                    # Logica: Queremos que o estoque do kit seja ajustado para refletir o estoque real máximo
                    # O componente_stock_limit é o número máximo de kits que podem ser montados.
                    new_kit_stock = component_stock_limit 
                    
                    if new_kit_stock < 0: 
                        new_kit_stock = 0

                    # 4. Verifica se precisa de atualização
                    if new_kit_stock != kit_stock:
                        self.logger.info(f"📦 Kit {kit.get('codigo')} - Atualizando estoque: {kit_stock} -> {new_kit_stock}. (Componentes limitam a {component_stock_limit})")
                        
                        # Simula a chamada de API de atualização de estoque
                        # API V3: PUT /produtos/{idProduto}/saldos/depositos/{idDeposito}
                        # O Bling V3 não possui um endpoint simples de 'setar' estoque como a V2. 
                        # Seria necessário um lançamento de estoque ou ajuste manual na V3.
                        # Para fins de demonstração, simulamos o log da ação:
                        self.logger.info(f"Simulação: PUT para atualizar estoque do kit {kit.get('id')} para {new_kit_stock}")
                        
                        updated_count += 1
                    else:
                        self.logger.debug(f"Kit {kit.get('codigo')} ({kit_stock}) já está sincronizado.")
                        
                # Delay para evitar rate limiting
                time.sleep(self.config.DELAY_BETWEEN_BATCHES) 
            
            except Exception as e:
                self.logger.error(f"Erro ao processar kit {kit.get('codigo')}: {e}")
        
        self.logger.info(f"Worker de estoque finalizado. {updated_count} kits tiveram estoque ajustado (simulado).")


    def _calculate_component_limit(self, kit_details: Dict[str, Any]) -> Optional[int]:
        """
        Calcula o número máximo de kits que podem ser montados
        baseado no estoque dos componentes e na quantidade necessária para cada kit.
        """
        if not kit_details.get('composicao'):
            return None
            
        components = kit_details['composicao'].get('componentes', [])
        max_kits = float('inf')
        
        for comp_wrapper in components:
            comp = comp_wrapper.get('componente')
            if not comp: continue
            
            # 1. Obtém dados do componente (a composição do kit já traz dados do produto)
            component_code = comp.get('codigo')
            required_qty = comp.get('quantidade', 1.0)
            
            # O Bling retorna 'estoque' dentro do objeto do componente na composição V3
            stock_info = comp.get('estoque')
            if not stock_info:
                self.logger.warning(f"Componente {component_code} sem informação de estoque.")
                continue

            available_stock = stock_info.get('saldo', 0.0)
            
            try:
                available_stock = float(available_stock)
                required_qty = float(required_qty)
                
                if required_qty <= 0:
                    self.logger.warning(f"Componente {component_code} com quantidade inválida ({required_qty}) no kit.")
                    continue
                
                # Número de kits que podem ser formados com o estoque deste componente
                kits_possible = int(available_stock / required_qty)
                
                max_kits = min(max_kits, kits_possible)
                
            except ValueError:
                self.logger.error(f"Erro ao converter estoque/quantidade para número no componente {component_code}.")
                continue

        # Se não houver componentes válidos
        if max_kits == float('inf'):
            return None
            
        return max_kits

# ============================================================================ 
# 7. WEB SERVER (FLASK)
# ============================================================================

class WebServer:
    def __init__(self, app: Flask, orchestrator: AutomationOrchestrator):
        self.app = app
        self.orchestrator = orchestrator
        self.setup_routes()
        self.setup_websocket()
        self.logger = logger
        
        # HTML Básico para visualização do status
        self.DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Automação Bling v4.7</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f7f6; color: #333; margin: 0; padding: 20px; }
        .container { max-width: 1200px; margin: auto; background: #fff; padding: 30px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }
        .header { text-align: center; margin-bottom: 30px; border-bottom: 2px solid #007bff; padding-bottom: 15px; }
        .header h1 { color: #007bff; margin: 0; }
        .status-box { display: flex; justify-content: space-around; flex-wrap: wrap; margin-bottom: 30px; }
        .kpi-card { background-color: #e0f7fa; padding: 20px; border-radius: 8px; text-align: center; margin: 10px; flex-basis: 30%; min-width: 200px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
        .kpi-card h3 { margin-top: 0; color: #007bff; }
        .kpi-card p { font-size: 2em; margin: 5px 0; font-weight: bold; color: #00bcd4; }
        .log-section { margin-top: 30px; border-top: 1px solid #ddd; padding-top: 20px; }
        .log-container { max-height: 400px; overflow-y: scroll; background: #f9f9f9; border: 1px solid #ddd; padding: 10px; border-radius: 6px; font-family: monospace; font-size: 0.8em; }
        .log-container p { margin: 0; padding: 2px 0; border-bottom: 1px dotted #eee; }
        .log-container .ERROR { color: #d9534f; font-weight: bold; }
        .log-container .WARNING { color: #f0ad4e; }
        .log-container .INFO { color: #5cb85c; }
        .log-container .DEBUG { color: #777; }
        .auth-link { text-align: center; margin-top: 20px; }
        .auth-link a { display: inline-block; background-color: #28a745; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; font-weight: bold; }
        .last-update { text-align: center; font-size: 0.9em; color: #666; margin-top: 10px; }
        .component-list { margin-top: 20px; }
        .component-list h2 { color: #007bff; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Painel de Controle de Automação Bling v4.7</h1>
        </div>

        {% if not auth_url or auth_url == '#' %}
            <div class="status-box">
                <div class="kpi-card">
                    <h3>Pedidos Diários (24h)</h3>
                    <p id="daily_count">...</p>
                </div>
                <div class="kpi-card">
                    <h3>Pedidos Semanais (7d)</h3>
                    <p id="weekly_count">...</p>
                </div>
                <div class="kpi-card">
                    <h3>Pedidos Históricos</h3>
                    <p id="historic_count">...</p>
                </div>
            </div>
            <div class="last-update">
                Última atualização dos KPIs: <span id="last_update_ts">Aguardando dados...</span>
            </div>
            <div class="component-list">
                <h2>Status de Componentes/Produtos</h2>
                <p>Kits encontrados: <span id="kit_count">...</span> | Produtos normais: <span id="product_count">...</span></p>
                <p>Status do Worker: <span id="worker_status">...</span></p>
            </div>
            <div class="log-section">
                <h2>Logs Recentes (WebSocket)</h2>
                <div class="log-container" id="log-output">
                </div>
            </div>
        {% else %}
            <div class="auth-link">
                <h2>Autenticação Necessária</h2>
                <p>Por favor, autentique-se no Bling para iniciar a automação.</p>
                <a href="{{ auth_url }}">AUTENTICAR NO BLING</a>
            </div>
        {% endif %}
    </div>

    <script>
        const authUrl = "{{ auth_url }}";

        if (authUrl === '#') {
            const statsWs = new WebSocket("ws://" + window.location.host + "/ws/kpi");
            const logWs = new WebSocket("ws://" + window.location.host + "/ws/logs");

            // ==============================================
            // WebSocket para KPIs
            // ==============================================
            statsWs.onopen = () => {
                console.log("WebSocket KPI conectado.");
            };

            statsWs.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    document.getElementById('daily_count').innerText = data.daily.toLocaleString();
                    document.getElementById('weekly_count').innerText = data.weekly.toLocaleString();
                    document.getElementById('historic_count').innerText = data.historic.toLocaleString();
                    
                    const updateTime = new Date(data.last_update);
                    document.getElementById('last_update_ts').innerText = updateTime.toLocaleString('pt-BR');
                } catch (e) {
                    console.error("Erro ao parsear KPI:", e);
                }
            };

            statsWs.onerror = (error) => {
                console.error("Erro no WebSocket KPI:", error);
            };

            statsWs.onclose = () => {
                console.log("WebSocket KPI desconectado. Tentando reconectar em 5s...");
                setTimeout(() => {
                    new WebSocket("ws://" + window.location.host + "/ws/kpi");
                }, 5000);
            };

            // ==============================================
            // WebSocket para Logs
            // ==============================================
            const logOutput = document.getElementById('log-output');

            logWs.onopen = () => {
                console.log("WebSocket Log conectado.");
                // Requisição inicial dos logs
                logWs.send(JSON.stringify({ action: "FETCH_INITIAL" }));
            };

            logWs.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    
                    if (data.initial_logs) {
                        logOutput.innerHTML = '';
                        data.initial_logs.forEach(log => {
                            appendLog(log);
                        });
                    } else if (data.log) {
                        appendLog(data.log);
                    }
                } catch (e) {
                    console.error("Erro ao parsear Log:", e);
                }
            };

            logWs.onerror = (error) => {
                console.error("Erro no WebSocket Log:", error);
            };

            logWs.onclose = () => {
                console.log("WebSocket Log desconectado. Tentando reconectar em 5s...");
                setTimeout(() => {
                    new WebSocket("ws://" + window.location.host + "/ws/logs");
                }, 5000);
            };
            
            function appendLog(log) {
                const p = document.createElement('p');
                p.className = log.level; // Aplica a classe de cor (ERROR, WARNING, INFO, DEBUG)
                p.innerHTML = `[${log.timestamp}] <strong>${log.level}</strong>: ${log.message.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')}`;
                logOutput.appendChild(p);
                // Mantém o scroll no final
                logOutput.scrollTop = logOutput.scrollHeight;
            }

            // ==============================================
            // Fetch de Dados de Produtos e Status
            // ==============================================
            function fetchComponentStatus() {
                fetch('/api/component/status')
                    .then(response => response.json())
                    .then(data => {
                        document.getElementById('kit_count').innerText = data.kits.toLocaleString();
                        document.getElementById('product_count').innerText = data.products.toLocaleString();
                        document.getElementById('worker_status').innerText = data.worker_running ? 'ATIVO' : 'INATIVO';
                    })
                    .catch(error => {
                        console.error('Erro ao buscar status:', error);
                        document.getElementById('worker_status').innerText = 'ERRO';
                    });
            }

            // Inicia e repete o fetch de status a cada 15s
            fetchComponentStatus();
            setInterval(fetchComponentStatus, 15000);
        }
    </script>
</body>
</html>
        """

    def setup_routes(self):
        # Rota principal para o Dashboard
        self.app.add_url_rule('/', 'index', self.index)
        # Rota de Callback OAuth2
        self.app.add_url_rule('/bling/callback', 'bling_callback', self.bling_callback)
        # API para obter KPIs
        self.app.add_url_rule('/api/sales/stats', 'sales_stats', self.sales_stats)
        # API para obter status de componentes
        self.app.add_url_rule('/api/component/status', 'component_status', self.component_status)

    def setup_websocket(self):
        # Configuração do WebSocket
        sock = Sock(self.app)
        sock.route('/ws/kpi')(self.kpi_websocket)
        sock.route('/ws/logs')(self.log_websocket)

    def index(self):
        auth_url = self.orchestrator.auth.get_authorization_url()
        return render_template_string(self.DASHBOARD_HTML, auth_url=auth_url)

    def bling_callback(self):
        code = request.args.get('code')
        state = request.args.get('state')

        if not code:
            self.logger.error("Callback Bling sem código de autorização.")
            return jsonify({"error": "Código de autorização ausente"}), 400

        with token_exchange_lock:
            if self.orchestrator.auth.exchange_code_for_token(code, state):
                self.logger.info("Autenticação Bling concluída e tokens salvos.")
                return redirect(url_for('index'))
            else:
                self.logger.error("Falha na troca de código por token no Bling.")
                return jsonify({"error": "Falha na troca de código por token"}), 400

    def sales_stats(self):
        """API para retornar as estatísticas de vendas (KPIs)."""
        stats = self.orchestrator.sales_manager.get_stats()
        # Retorna o status HTTP 200 OK e os dados JSON
        return jsonify(stats), 200

    def component_status(self):
        """API para retornar o status de kits e produtos."""
        with self.orchestrator.lock:
            status = {
                "kits": len(self.orchestrator.kits),
                "products": len(self.orchestrator.products),
                "worker_running": self.orchestrator.is_running
            }
        return jsonify(status)

    # ============================================================================ 
    # 8. WEBSOCKETS (ATUALIZADO)
    # ============================================================================
    
    def kpi_websocket(self, ws):
        """
        WebSocket para enviar atualizações de KPI em tempo real.
        Registra um callback que é chamado quando os KPIs são recalculados.
        """
        self.logger.info("WebSocket KPI conectado.")
        
        def notify_kpi(data: Dict[str, Any]):
            """Callback chamado pelo SalesManager."""
            try:
                # Envia a string JSON para o cliente
                ws.send(json.dumps(data))
            except ConnectionClosed:
                # Se a conexão foi fechada, o loop principal cuidará da remoção
                self.logger.debug("Tentativa de enviar KPI falhou: Conexão fechada.")
                raise
            except Exception as e:
                self.logger.error(f"Erro ao enviar KPI para o WebSocket: {e}")
                
        # 1. Registra o callback para futuras atualizações
        with kpi_update_lock:
            kpi_update_callbacks.append(notify_kpi)

        # 2. Envia o estado atual imediatamente após a conexão
        try:
            current_stats = self.orchestrator.sales_manager.get_stats()
            # Garante que a data é uma string ISO para o WS
            if isinstance(current_stats.get('last_update'), datetime):
                 current_stats['last_update'] = current_stats['last_update'].isoformat()
            
            ws.send(json.dumps(current_stats))
        except Exception as e:
            self.logger.warning(f"Falha ao enviar dados iniciais de KPI: {e}")
        
        # 3. Mantém a conexão aberta, escutando por mensagens de keepalive
        try:
            while True:
                # Recebe mensagens com timeout (Keepalive)
                ws.receive(timeout=5) 
        except ConnectionClosed:
            self.logger.debug("WebSocket KPI fechado pelo cliente.")
        except Exception as e:
            self.logger.error(f"Erro no loop do WebSocket KPI: {e}")
        finally:
            # 4. Remove o callback quando desconectar
            with kpi_update_lock:
                if notify_kpi in kpi_update_callbacks:
                    kpi_update_callbacks.remove(notify_kpi)
            self.logger.info("WebSocket KPI desconectado.")


    def log_websocket(self, ws):
        """
        WebSocket para enviar logs em tempo real.
        Adiciona um handler ao logger que envia logs para este WebSocket.
        """
        self.logger.info("WebSocket Log conectado.")
        
        # Handler específico para esta conexão WS
        class WebSocketLogHandler(logging.Handler):
            def __init__(self, websocket):
                super().__init__()
                self.websocket = websocket
                self.formatter = logging.Formatter(
                    '%(asctime)s - %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%dT%H:%M:%S'
                )
            
            def emit(self, record):
                try:
                    # Filtra logs de gunicorn e flask/werkzeug para não poluir
                    if record.name.startswith(('werkzeug', 'gunicorn')):
                        return

                    log_entry = {
                        'timestamp': self.formatter.formatTime(record),
                        'level': record.levelname,
                        'message': self.format(record),
                    }
                    # Envia a string JSON
                    self.websocket.send(json.dumps({'log': log_entry}))
                except ConnectionClosed:
                    # Se a conexão fechar durante o envio, interrompe o handler
                    self.websocket.close()
                except Exception:
                    # Ignora outros erros de envio de log
                    pass

        ws_handler = WebSocketLogHandler(ws)
        # Adiciona o handler ao logger principal
        self.logger.addHandler(ws_handler)

        try:
            while True:
                # Recebe mensagens para ações
                message = ws.receive(timeout=5) 
                if message:
                    data = json.loads(message)
                    if data.get('action') == 'FETCH_INITIAL':
                        # Envia os logs iniciais armazenados em memória
                        initial_logs = memory_handler.get_logs(limit=100)
                        ws.send(json.dumps({'initial_logs': initial_logs}))
                    
        except ConnectionClosed:
            self.logger.debug("WebSocket Log fechado pelo cliente.")
        except Exception as e:
            self.logger.error(f"Erro no loop do WebSocket Log: {e}")
        finally:
            # Remove o handler quando desconectar
            self.logger.removeHandler(ws_handler)
            # Tenta fechar a conexão se ainda estiver aberta
            try:
                if not ws.closed:
                    ws.close()
            except Exception:
                pass
            self.logger.info("WebSocket Log desconectado.")


# ============================================================================ 
# 9. INICIALIZAÇÃO E MAIN
# ============================================================================

# Inicialização das classes globais (antes do app)
CONFIG = Config()
SALES_MANAGER = SalesManager(CONFIG)
orchestrator = AutomationOrchestrator(CONFIG, SALES_MANAGER)

# Este bloco precisa estar após a definição do orchestrator para funcionar
# no Gunicorn/WSGI.

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
        # Define que o worker está rodando
        orchestrator.is_running = True
        Thread(target=orchestrator.load_data_worker, daemon=True).start()
        app.run(host='0.0.0.0', port=args.port, debug=False)

if __name__ == "__main__":
    run_cli()

# --- GUNICORN CONFIGURAÇÕES (TIMEOUT AJUSTADO PARA 300) ---
import os as _os
_os.environ.setdefault("FLASK_APP", "bling:app")
# Exemplo de uso: gunicorn bling:app -w 4 -k gevent --bind 0.0.0.0:8000 --timeout 300