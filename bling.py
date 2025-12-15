#!/usr/bin/env python3

from gevent import monkey
monkey.patch_all()   # torna as bibliotecas padrão cooperativas com gevent (requests, socket, threading...)
"""
bling.py - Sistema completo de automação Bling com design premium (CORRIGIDO v4.7)
Implementa OAuth 2.0, API robusta, gerenciamento de estoque/compras e dashboard web.
- CORREÇÃO CRÍTICA (v4.4): Implementação de WebSocket para notificação em TEMPO REAL de KPIs.
- FIX SINCRONIZAÇÃO (v4.4): get_stats() agora força a leitura do arquivo para sincronização multi-worker.
- FIX SPAM DE LOG (v4.5): Ajuste no _load_stats para evitar logs repetitivos de 'Nenhum KPI encontrado'.
- FIX SPAM DE LOG (v4.7): O log de leitura de KPIs foi totalmente removido para evitar spam no console.
- FEATURE (v4.6): Histórico de pedidos expandido de 9 para 30 dias.
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


    # NOVO: Carregamento do estado persistente (FIX DE SPAM RESOLVIDO)
    def _load_stats(self):
        data = load_stats_safe(self.config.SALES_STATS_FILE)
        if data:
            with self.lock:
                self.daily_count = data.get('daily', 0)
                self.weekly_count = data.get('weekly', 0)
                self.historic_count = data.get('historic', 0)
                # Usa a data carregada ou a data de inicialização se o carregamento falhar
                self.last_recalculated = data.get('last_recalculated', datetime.now())
            
            # REMOVIDO O LOG DE SUCESSO REPETITIVO AQUI
            # O logger.info(f"KPIs carregados...") foi removido,
            # eliminando o spam a cada 5 segundos que ocorria na leitura de rotina.
            
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
        last_month = now - timedelta(days=30)
        
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

    # MÉTODO CORRIGIDO (v4.2): Adiciona debounce lock
    def process_sales_orders(self):
        """Busca pedidos de venda faturados/em andamento dos últimos 30 dias e ATUALIZA O SALES_MANAGER POR RECALCULO."""
        if not self.recalculation_lock.acquire(blocking=False):
            self.logger.warning("Recálculo de KPIs já em andamento. Ignorando nova solicitação.")
            return
        
        try:
            token = self.auth.get_valid_token()
            if not token:
                self.logger.warning("Token indisponível para buscar pedidos de venda.")
                return

            # FEATURE (v4.6): Expande o período de busca de 9 para 30 dias
            self.logger.info("Iniciando busca COMPLETA de pedidos de venda para recalcular os KPIs (Últimos 30 dias)...")
            now = datetime.now()
            thirty_days_ago = now - timedelta(days=30)
            
            params = {
                'dataEmissaoInicial': (now - timedelta(days=30)).strftime('%Y-%m-%d'),
                'dataEmissaoFinal': now.strftime('%Y-%m-%d'), # CRÍTICO: Adiciona data final
                'pagina': 1,
                'limite': 100, # Aumenta limite para reduzir chamadas
            }
            
            # ADICIONAR após definir params:
            self.logger.info(f"🔎 Parâmetros da busca: {params}")

            all_orders = []
            page = 1
            
            oldest_order: Optional[datetime] = None
            newest_order: Optional[datetime] = None
            orders_outside_range = 0
            
            while True:
                params['pagina'] = page
                response = self.api_client.get_sales_orders(token, **params)
                
                # Pedidos v3 vêm em 'data'
                orders_data = response.get('data', [])
                
                if not orders_data:
                    break
                
                # A API V3 retorna uma lista de objetos aninhados, extrai o pedido
                extracted_orders = [item.get('pedidoVenda', item) for item in orders_data if item and isinstance(item, dict)]
                
                if not extracted_orders:
                    break
                    
                all_orders.extend(extracted_orders)
                
                # Validação de range (A API V3 parece ignorar filtros de data em buscas paginadas)
                for order in extracted_orders:
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
                
                if len(extracted_orders) < params['limite']:
                    break
                
                page += 1
            
            # Log de validação
            if oldest_order and newest_order:
                self.logger.info(f"📅 Período dos pedidos: {oldest_order.strftime('%Y-%m-%d')} até {newest_order.strftime('%Y-%m-%d')}")
            if orders_outside_range > 0:
                self.logger.warning(f"⚠️ ALERTA: {orders_outside_range} pedidos fora do período de 30 dias! "
                                    f"A API pode estar ignorando o filtro de data.")
            self.logger.info(f"📊 Total de pedidos encontrados: {len(all_orders)}")

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
                        filtered_orders.append(order) # Mantém pedidos sem data válida
                self.logger.info(f"✅ Filtro local aplicado: {len(all_orders)} -> {len(filtered_orders)} pedidos")
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
                    data_str, hora_str = 'N/A', 'N/A'
                
                status = order.get('situacao', {}).get('descricao', 'N/D')
                self.logger.debug(f"Amostra {idx+1}: ID={order.get('id')}, Data={data_str} {hora_str}, Status={status}")


            self.sales_manager.recalculate_from_orders(all_orders)

        except Exception as e:
            self.logger.error(f"Erro no processamento de pedidos de venda: {e}")
            error_logger.error(f"Erro no processamento de pedidos de venda: {e}", exc_info=True)
        finally:
            self.recalculation_lock.release()

    def _load_products_and_kits(self, access_token: str):
        """Busca todos os produtos e separa kits de produtos simples."""
        self.logger.info("Iniciando busca de produtos e kits...")
        
        # PASSO 1: Busca todos os produtos (apenas a lista inicial)
        search_result = get_bling_products_safe(self.api_client, access_token=access_token)
        if not search_result.get("success"):
            self.logger.error("Falha ao buscar produtos: %s", search_result.get("error"))
            return
            
        todos_produtos = search_result.get("data", [])
        self.logger.info(f"Total de produtos baixados: {len(todos_produtos)}")

        # Limpa listas anteriores
        with self.lock:
            self.kits = []
            self.products = []
            
            if not todos_produtos:
                self.logger.info("Nenhum produto encontrado.")
                return

        # PASSO 2: Cria um mapa para busca rápida de componentes
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
                        # Busca detalhes para extrair a estrutura (necessário em alguns casos V3)
                        det = self.api_client.get_product_details(access_token, p_id)
                        componentes = det.get("estrutura", {}).get("componentes", [])
                        if not img_url:
                            img_url = extract_image_url(det)
                    except:
                        pass
                        
                for c in componentes:
                    filho_ref = c.get("produto", {})
                    filho_id = str(filho_ref.get("id"))
                    produto_filho = produto_map.get(filho_id)
                    
                    nome_final = "Item não carregado"
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
        self.logger.info("Verificação de compras iniciada (Simulação).")
        return True

# ============================================================================ 
# 7. FLASK WEB SERVER (ATUALIZADO COM WEBSOCKET)
# ============================================================================

# Instâncias Globais
config = Config()
sales_manager = SalesManager(config)
orchestrator = AutomationOrchestrator(config, sales_manager)


def token_required(f):
    """Decorator para exigir token válido antes de acessar a API."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not orchestrator.auth.is_authenticated():
            # Retorna 401 se não estiver autenticado
            return jsonify({"error": "Unauthorized"}), 401
        token = orchestrator.auth.get_valid_token()
        return f(*args, token=token, **kwargs)
    return decorated_function

class WebServer:
    code_lock = Lock()
    used_codes = set()
    
    def __init__(self, app: Flask, orchestrator: AutomationOrchestrator):
        self.app = app
        self.orchestrator = orchestrator
        self.logger = logger
        self.setup_routes()
        self.sock = Sock(app)
        self.setup_websocket()

    def setup_routes(self):
        
        # Verifica se a URI de Redirecionamento está configurada
        if not self.orchestrator.config.REDIRECT_URI:
             # Este erro é crítico para o deploy em plataformas como Render
            from flask import abort
            self.logger.error("ERRO FATAL: BLING_REDIRECT_URI não configurada no Render")
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
                self.logger.info("Callback ignorado: Usuário já autenticado.")
                return redirect('/')

            if not code or not state:
                return redirect('/')
                
            if not token_exchange_lock.acquire(blocking=False):
                self.logger.warning("Concorrência detectada no callback. Redirecionando para home.")
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
                self.logger.error(f"Erro crítico no callback: {e}")
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

        # ENDPOINT DE ESTATÍSTICAS DE VENDAS (AGORA CORRIGIDO COM RE-LEITURA)
        @self.app.route("/api/sales/stats")
        def api_sales_stats():
            """Retorna os contadores Diário, Semanal e Histórico."""
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

            # Busca na API V3 com filtro (ainda limitada, o cache é mais rápido)
            all_results_base = []
            seen_ids = set()

            def process_response(resp_data):
                """Processa resposta da API e adiciona à lista de resultados básicos"""
                items = resp_data.get('data') or []
                for p in items:
                    p_id = p.get('id')
                    if p_id and p_id not in seen_ids:
                        # Extrai a imagem para o preview rápido
                        p['imagemURL'] = extract_image_url(p)
                        all_results_base.append(p)
                        seen_ids.add(p_id)


            # Tenta buscar por SKU (código)
            if termo:
                resp = get_bling_products_safe(self.orchestrator.api_client, sku=termo, access_token=token)
                if resp.get("success"):
                    process_response({"data": resp.get("data")})
                    
            # Se não achou por SKU, tenta por nome (se for diferente)
            if len(all_results_base) == 0:
                resp = get_bling_products_safe(self.orchestrator.api_client, nome=termo, access_token=token)
                if resp.get("success"):
                    process_response({"data": resp.get("data")})

            # Filtra o cache local como fallback e para incluir kits
            termo_lower = termo.lower()
            final_results = all_results_base.copy()
            
            # Busca nos kits do cache local
            kits_cache = self.orchestrator.get_all_kits()
            for kit in kits_cache:
                if kit.get("id") not in seen_ids and (termo_lower in str(kit.get("produto", "")).lower() or termo_lower in str(kit.get("sku", "")).lower()):
                    final_results.append(kit)
                    seen_ids.add(kit.get("id"))
            
            # Busca nos produtos simples do cache local (evita duplicar o que veio da API)
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
                    self.logger.warning("Webhook Bling: Assinatura inválida detectada.")
                    return jsonify({"status": "error", "message": "Signature Mismatch"}), 401

                event_data = json.loads(payload)
                self.logger.info(f"Webhook Bling recebido: Tipo={event_data.get('nomeEvento')}")
                
                # Exemplo: Se for evento de Vendas, pode-se forçar um recálculo imediato
                if event_data.get('nomeEvento') in ["vendas", "vendas_alterado", "vendas_incluido"]:
                    # Aciona um worker rápido para buscar dados novos e recalcular
                    Thread(target=self.orchestrator.process_sales_orders, daemon=True).start()
                    
                return jsonify({"status": "ok"}), 200

            except Exception as e:
                self.logger.error(f"Erro no processamento do webhook: {e}")
                return jsonify({"status": "error", "message": str(e)}), 500

    def setup_websocket(self):
        """Configura o WebSocket para logs em tempo real e KPIs."""
        
        # WEBSOCKET para LOGS (MANTIDO)
        @self.sock.route('/ws/logs')
        def logs_websocket(ws):
            self.logger.info("WebSocket de Logs conectado.")
            # Envia os logs históricos imediatamente
            try:
                initial_logs = memory_handler.get_logs(limit=200)
                ws.send(json.dumps({'logs': initial_logs}))
            except ConnectionClosed:
                 return
            except Exception as e:
                self.logger.error(f"Erro ao enviar logs iniciais: {e}")
                
            # Mantém a conexão aberta
            while True:
                try:
                    # Recebe mensagens (keepalive) ou aguarda timeout
                    ws.receive(timeout=5)
                except ConnectionClosed:
                    break
                except Exception:
                    pass
            self.logger.info("WebSocket de Logs desconectado.")
            
        # WEBSOCKET para KPIs (CORRIGIDO)
        @self.sock.route('/ws/kpis')
        def kpis_websocket(ws):
            self.logger.info("WebSocket KPI conectado.")

            # Função de callback que será chamada pelo SalesManager
            def notify_kpi(stats_data):
                try:
                    ws.send(json.dumps({'kpi': stats_data}))
                except (ConnectionClosed, Exception):
                    # Se houver erro ou a conexão fechar, o worker remove o callback
                    # É importante não segurar o lock kpi_update_lock aqui, pois ele 
                    # já está segurado pelo thread que chamou notify_kpi.
                    pass 

            # Adiciona o callback à lista global (protegido por lock)
            global kpi_update_callbacks, kpi_update_lock
            with kpi_update_lock:
                kpi_update_callbacks.append(notify_kpi)

            # Envia os dados atuais imediatamente
            try:
                current_stats = sales_manager.get_stats()
                current_stats.pop('last_update', None) # Será formatado no callback
                notify_kpi(current_stats)
            except Exception as e:
                self.logger.error(f"Erro ao enviar KPI inicial: {e}")
                
            # Mantém a conexão aberta
            try:
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
# 8. TEMPLATE HTML (DASHBOARD)
# ============================================================================

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bling Automação v4.7</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <style>
        body { background-color: #2e3440; color: #eceff4; }
        .navbar { background-color: #3b4252; }
        .card { background-color: #4c566a; border: none; }
        .log-box { height: 400px; overflow-y: auto; }
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
        
        <div id="content-tabs" class="hidden">
            <ul class="nav nav-tabs" id="myTab" role="tablist">
                <li class="nav-item" role="presentation">
                    <button class="nav-link active" id="products-tab" data-bs-toggle="tab" data-bs-target="#products-pane" type="button" role="tab" aria-controls="products-pane" aria-selected="true" onclick="loadProducts()">Produtos</button>
                </li>
                <li class="nav-item" role="presentation">
                    <button class="nav-link" id="kits-tab" data-bs-toggle="tab" data-bs-target="#kits-pane" type="button" role="tab" aria-controls="kits-pane" aria-selected="false" onclick="loadKits()">Kits</button>
                </li>
                <li class="nav-item" role="presentation">
                    <button class="nav-link" id="search-tab" data-bs-toggle="tab" data-bs-target="#search-pane" type="button" role="tab" aria-controls="search-pane" aria-selected="false">Busca</button>
                </li>
            </ul>
            <div class="tab-content pt-3" id="myTabContent">
                <div class="tab-pane fade show active" id="products-pane" role="tabpanel" aria-labelledby="products-tab" tabindex="0">
                    <div id="auth-required-products" class="alert alert-warning hidden">Autenticação Bling necessária para carregar dados.</div>
                    <div id="products-list"></div>
                </div>
                <div class="tab-pane fade" id="kits-pane" role="tabpanel" aria-labelledby="kits-tab" tabindex="0">
                    <div id="auth-required-kits" class="alert alert-warning hidden">Autenticação Bling necessária para carregar dados.</div>
                    <div id="kits-list"></div>
                </div>
                <div class="tab-pane fade" id="search-pane" role="tabpanel" aria-labelledby="search-tab" tabindex="0">
                    <div class="input-group mb-3">
                        <input type="text" class="form-control" placeholder="Buscar por SKU ou Nome" id="search-input">
                        <button class="btn btn-primary" type="button" onclick="performSearch()">Buscar</button>
                    </div>
                    <div id="search-results-list" class="list-group"></div>
                </div>
            </div>
        </div>
        <div id="auth-required-main" class="alert alert-warning mt-4 hidden">Por favor, autentique com o Bling para acessar as ferramentas de automação.</div>
    </div>

    <script>
        const API = '/api';
        
        function formatLog(log) {
            // Remove o nome do logger, ex: 'bling_automacao - '
            const message = log.message.replace(/[^ ]+ - /, '');
            return `<div class="log-entry log-level-${log.level}">${log.timestamp} [${log.level}] - ${message}</div>`;
        }
        
        function formatDateTime(isoString) {
            if (!isoString || isoString === 'N/D') return 'N/D';
            try {
                const date = new Date(isoString);
                // Formato dd/mm/yyyy hh:mm:ss
                return date.toLocaleDateString('pt-BR', {
                    day: '2-digit', month: '2-digit', year: 'numeric',
                    hour: '2-digit', minute: '2-digit', second: '2-digit'
                });
            } catch {
                return isoString;
            }
        }
        
        // WEBSOCKET LOGS
        const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
        const ws = new WebSocket(`${proto}://${window.location.host}/ws/logs`);
        ws.onmessage = (e) => {
            const data = JSON.parse(e.data);
            const box = document.getElementById('logs-content');
            if(data.logs) {
                // Se for a carga inicial, limpa e adiciona
                box.innerHTML = '';
                data.logs.forEach(l => box.innerHTML += formatLog(l));
                box.scrollTop = box.scrollHeight;
            } else if (data.message) {
                // Se for uma única mensagem (em tempo real)
                box.innerHTML += formatLog(data);
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
                    document.getElementById('auth-required-main').classList.add('hidden');
                } else {
                    badge.className = 'badge bg-danger me-2';
                    badge.textContent = 'Offline';
                    document.getElementById('auth-link').classList.remove('d-none');
                    document.getElementById('content-tabs').classList.add('hidden');
                    document.getElementById('auth-required-main').classList.remove('hidden');
                }
                document.getElementById('auth-link').href = dStatus.auth_url;

                // Não precisa mais do polling de stats, pois o WS faz isso
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
        // Mantém o polling de status, mas não mais o de stats
        setInterval(checkStatus, 5000); 

        // WEBSOCKET KPI
        const protoKpi = window.location.protocol === 'https:' ? 'wss' : 'ws';
        const wsKpi = new WebSocket(`${protoKpi}://${window.location.host}/ws/kpis`);
        wsKpi.onmessage = (e) => {
            const data = JSON.parse(e.data);
            if(data.kpi) {
                // Recebe a atualização em tempo real
                updateKpis(data.kpi);
            }
        }

        async function loadProducts() {
            const div = document.getElementById('products-list');
            const authRequiredDiv = document.getElementById('auth-required-products');
            if (!isAuthenticated) {
                div.innerHTML = '';
                authRequiredDiv.classList.remove('hidden');
                return;
            }
            authRequiredDiv.classList.add('hidden');
            div.innerHTML = '<div class="alert alert-info">Carregando dados. Este processo depende da finalização do cache em segundo plano (Worker) e pode demorar alguns minutos.</div>';

            try {
                const r = await fetch(`${API}/all_products`);
                if (r.status === 401) {
                    div.innerHTML = '';
                    authRequiredDiv.classList.remove('hidden');
                    checkStatus(); // Tenta reautenticar
                    return;
                }
                const data = await r.json();
                
                let html = '<div class="list-group">';
                data.forEach(p => {
                    html += `
                        <div class="list-group-item">
                            <div class="d-flex">
                                <img src="${p.imagemURL || ''}" style="width:60px;height:60px;object-fit:contain;margin-right:10px;border-radius:6px;background:#f1f1f1" onerror="this.style.display='none'">
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
                // O endpoint /api/kits retorna kits e produtos simples juntos para otimizar
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
                    const imgHtml = k.imagemURL ? `<img src="${k.imagemURL}" style="width:50px;height:50px;object-fit:contain;border-radius:4px;" onerror="this.style.display='none'">` : '<span class="text-muted">-</span>';
                    
                    let comps = '';
                    if (k.componentes && k.componentes.length > 0) {
                        comps = k.componentes.map(c => `${c.quantidade}x ${c.nome} (SKU: ${c.sku})`).join('<br>');
                    } else {
                         comps = k.tipo || 'Produto Simples';
                    }

                    html += `
                        <tr>
                            <td>${imgHtml}</td>
                            <td>${k.sku || 'N/D'}</td>
                            <td>${k.produto || k.nome || 'Sem nome'}</td>
                            <td>${comps}</td>
                        </tr>
                    `;
                });

                html += `</tbody></table>`;
                div.innerHTML = html;
                
            } catch(e) {
                div.innerHTML = `<div class="alert alert-danger">Erro: ${e}</div>`;
            }
        };

        async function performSearch() {
            const termo = document.getElementById('search-input').value.trim();
            const div = document.getElementById('search-results-list');
            div.innerHTML = '';

            if (termo.length < 3) {
                div.innerHTML = '<div class="alert alert-info">Digite pelo menos 3 caracteres para buscar.</div>';
                return;
            }
            
            div.innerHTML = '<div class="alert alert-info">Buscando...</div>';

            try {
                const r = await fetch(`${API}/product/search?q=${encodeURIComponent(termo)}`);
                if (r.status === 401) {
                    div.innerHTML = '<div class="alert alert-warning">Autenticação expirada. Por favor, cheque o status.</div>';
                    checkStatus();
                    return;
                }
                const data = await r.json();
                
                if (data.length === 0) {
                     div.innerHTML = '<div class="alert alert-warning">Nenhum produto ou kit encontrado.</div>';
                     return;
                }

                let html = '<div class="list-group">';
                data.forEach(p => {
                    const isKit = p.componentes && p.componentes.length > 0;
                    const tipo = isKit ? 'Kit' : (p.tipo || 'Simples');
                    
                    let compsHtml = '';
                    if (isKit) {
                         compsHtml = `
                            <div class="mt-2">
                                <b>Componentes:</b><br>
                                ${p.componentes.map(c => `${c.quantidade}x ${c.nome || 'N/D'} (SKU: ${c.sku || 'N/D'})`).join("<br>")}
                            </div>
                        `;
                    }
                    
                    html += `
                        <div class="list-group-item">
                            <div class="d-flex">
                                <img src="${p.imagemURL || ''}" style="width:60px;height:60px;object-fit:contain;margin-right:10px;border-radius:6px;background:#f1f1f1" onerror="this.style.display='none'">
                                <div class="flex-grow-1">
                                    <div class="d-flex w-100 justify-content-between">
                                        <h5 class="mb-1">${p.nome || p.produto || 'Sem nome'} <span class="badge bg-secondary">${tipo}</span></h5>
                                        <small>SKU: ${p.sku || 'N/D'}</small>
                                    </div>
                                    <small class="text-muted d-block">
                                        <b>Estoque:</b> ${p.estoque !== undefined ? p.estoque : 'N/D'} 
                                        <b style="margin-left:10px;">Situação:</b> ${p.situacao || 'N/D'}
                                    </small>
                                    ${compsHtml}
                                </div>
                            </div>
                        </div>
                    `;
                });
                html += '</div>';
                div.innerHTML = html;


            } catch(e) {
                div.innerHTML = `<div class="alert alert-danger">Erro na busca: ${e}</div>`;
            }
        }
        
    </script>
</body>
</html>
"""

# ============================================================================ 
# 9. INICIALIZAÇÃO DA APLICAÇÃO
# ============================================================================

# ... (O restante do código, que não foi modificado, segue abaixo)
# ...

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

# --- GUNICORN CONFIGURAÇÕES (TIMEOUT AJUSTADO PARA 300) ---
import os as _os
_os.environ.setdefault('GUNICORN_CMD_ARGS', f"--timeout 300 --workers 2 --bind 0.0.0.0:{_os.environ.get('PORT', '8000')}")