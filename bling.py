#!/usr/bin/env python3

from gevent import monkey
monkey.patch_all()   # torna as bibliotecas padrão cooperativas com gevent (requests, socket, threading...)
"""
bling.py - Sistema completo de automação Bling com design premium (CORRIGIDO v4.6)
Implementa OAuth 2.0, API robusta, gerenciamento de estoque/compras e dashboard web.
- CORREÇÃO CRÍTICA (v4.4): Implementação de WebSocket para notificação em TEMPO REAL de KPIs.
- FIX SINCRONIZAÇÃO (v4.4): get_stats() agora força a leitura do arquivo para sincronização multi-worker.
- CORREÇÃO (v4.6): MUDANÇA PARA CÁLCULO DE KPIS DE CALENDÁRIO (DIA/SEMANA/MÊS) e REMOÇÃO DE SPAM DE LOG.
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
    
    # Define o log principal para DEBUG para pegar todos os logs
    logger = logging.getLogger('bling_automacao')
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
        # Log removido para evitar spam, pois save_stats é chamado a cada recalculo
        # logger.info("Estatísticas de KPIs salvas com sucesso.") 
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
    Gerencia contadores de Pedidos de Venda Diárias, Semanaais e o Histórico (Mensal).
    Implementa persistência em arquivo para garantir consistência entre workers.
    """
    
    config: Config
    lock: Lock = field(default_factory=Lock)
    
    # Contadores (serão redefinidos a cada recalculate)
    daily_count: int = 0
    weekly_count: int = 0
    historic_count: int = 0 # Esta variável armazena a contagem Mensal
    
    # Data da última atualização dos dados
    last_recalculated: datetime = field(default_factory=datetime.now)

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
            # CORREÇÃO LOG: Remove o log de debug de carregamento para evitar spam no polling da API
            # logger.debug(f"KPIs carregados do arquivo. Histórico: {self.historic_count}.")
        else:
             # CORREÇÃO LOG: Log de inicialização de KPI persistente, ajustado para INFO
             logger.info("Nenhum KPI persistido encontrado, usando valores iniciais (0).")


    # NOVO: Método para obter o estado a ser salvo
    def _get_state_for_save(self) -> Dict[str, Any]:
         return {
            "daily": self.daily_count,
            "weekly": self.weekly_count,
            "historic": self.historic_count, # Mantém a chave 'historic' para compatibilidade de API/Front
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

    # MÉTODO CORRIGIDO (v4.6): Implementa cálculo por período de calendário (reset)
    def recalculate_from_orders(self, orders: List[Dict[str, Any]]):
        """Calcula KPIs baseando-se na data/hora de emissão dos pedidos, usando períodos de calendário (reset)."""
        now = datetime.now()
        
        # --- NOVO: Períodos de Calendário (Reset) ---
        
        # 1. Início do Dia (reset 00:00:00)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # 2. Início da Semana (considerando Segunda-feira como dia 0)
        # now.weekday() retorna 0 para Segunda-feira, 6 para Domingo.
        start_of_week = start_of_day - timedelta(days=now.weekday()) 
        
        # 3. Início do Mês 
        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        # --- Contadores ---
        daily = 0
        weekly = 0
        monthly = 0 
        
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
                # Constrói a data/hora para comparação
                order_date = datetime.strptime(data_emissao_str, '%Y-%m-%d')
                                    
                if hora_emissao and isinstance(hora_emissao, str):
                    try:
                        parts = hora_emissao.split(':')
                        if len(parts) == 3:
                            h, m, s = map(int, parts)
                            order_date = order_date.replace(hour=h, minute=m, second=s)
                    except (ValueError, AttributeError):
                        order_date = order_date.replace(hour=0, minute=0, second=0, microsecond=0)
                else:
                     order_date = order_date.replace(hour=0, minute=0, second=0, microsecond=0)
                         
            except Exception as e:
                logger.warning(f"Erro ao parsear data '{data_emissao_str}' do pedido {order.get('id')}: {e}")
                continue
            
            # 3. Contagem Mensal (antigo 'historic') - Pedidos desde o início do mês
            if order_date >= start_of_month:
                monthly += 1 
                            
            # 2. Contagem Semanal - Pedidos desde o início da semana (reset semanal)
            if order_date >= start_of_week:
                weekly += 1
                            
            # 1. Contagem Diária - Pedidos desde o início do dia (reset diário)
            if order_date >= start_of_day:
                daily += 1 

        # ATUALIZAÇÃO E PERSISTÊNCIA DENTRO DO LOCK
        with self.lock:
            # Atualiza todos os contadores de uma vez
            self.daily_count = daily
            self.weekly_count = weekly
            self.historic_count = monthly # Mantém o nome da variável de persistência como 'historic'
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
            
            # CORREÇÃO LOG: Altera a mensagem para 'Mensal'
            logger.info(f"✅ Estatísticas recalculadas com {len(orders)} pedidos analisados: "
                       f"Diário={daily}, Semanal={weekly}, Mensal={monthly}")


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
            self.logger.info("Aguardando autenticação...")
            return

        token = self.auth.get_valid_token()
        if not token:
            self.logger.warning("Token indisponível para buscar produtos.")
            return

        self.logger.info("Iniciando busca COMPLETA de todos os produtos do Bling para cache...")
        
        all_products_resp = get_bling_products_safe(self.api_client, access_token=token)
        
        if not all_products_resp.get("success"):
            self.logger.error(f"Falha ao carregar produtos: {all_products_resp.get('error')}")
            return
            
        all_products = all_products_resp.get("data", [])
        self.logger.info(f"Total de {len(all_products)} produtos encontrados no Bling.")
        
        # Cria um mapa para busca rápida de componentes de kits
        produto_map = {str(p.get("id")): p for p in all_products if p.get("id")}
        
        # Zera as listas
        self.kits = []
        self.products = []

        access_token = self.auth.get_valid_token()
        
        # Itera sobre os produtos para separar kits e produtos simples
        for p in all_products:
            # Pula produtos sem ID ou sem código (SKU)
            if not p.get("id") or not p.get("codigo"):
                continue

            p_id = p.get("id")
            estrutura = p.get("estrutura", {})
            componentes = estrutura.get("componentes", [])
            # Identificação de kits (pelo tipo 'K' ou se tem componentes na estrutura)
            eh_kit = len(componentes) > 0 or p.get("tipo") == "K" or p.get("formato") == "K"
            img_url = extract_image_url(p)
            
            if eh_kit:
                comps_formatados = []
                # Tenta buscar detalhes do kit se os componentes não vieram na listagem (API v3)
                if not componentes and p_id:
                    try:
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

    def load_data_worker(self):
        """Worker principal que executa a lógica de automação."""
        self.is_running = True
        self.logger.info("Worker de automação Bling iniciado.")
        
        while True:
            if not self.auth.is_authenticated():
                self.logger.warning("Worker em espera: Aguardando autenticação.")
                time.sleep(60)
                continue
            
            try:
                # 1. Carrega (ou recarrega) os produtos do Bling para o cache
                self.load_bling_products() 
                
                # 2. Recalcula os KPIs de Pedidos de Venda
                self.process_sales_orders() 
                
                # 3. Executa a verificação de compras/estoque (Simulação)
                self.run_purchase_check()
                
            except BlingAuthError:
                self.logger.error("Erro de autenticação Bling. Tentando renovar o token.")
                self.auth.refresh_access_token()
            except BlingAPIError as e:
                self.logger.error(f"Erro na API Bling: {e}. Tentando novamente em 60 segundos.")
                time.sleep(60)
                continue
            except Exception as e:
                self.logger.exception(f"Erro crítico no Worker: {e}")
                self.logger.info("Pausando worker. Tentar novamente.")
                time.sleep(60)
                continue
            
            self.logger.info("Worker finalizado. Próxima execução em 10 minutos.")
            time.sleep(600) # 10 minutos (600 segundos)

    # MÉTODO CORRIGIDO (v4.6): Mudar o período de busca para o Mês Corrente
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
            
            # --- CORREÇÃO: MUDANÇA PARA O MÊS CORRENTE ---
            now = datetime.now()
            # Define o intervalo para buscar pedidos: Início do Mês Corrente
            start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            start_date_str = start_of_month.strftime('%Y-%m-%d')
            
            # CORREÇÃO LOG: Ajusta o log para refletir o período de busca correto
            self.logger.info(f"Iniciando busca COMPLETA de pedidos de venda para recalcular os KPIs (Mês Corrente - Desde {start_date_str})...")

            # CORREÇÃO: Atualizar os parâmetros de busca para o formato V3 (Range)
            params = {
                # Filtra pela data de emissão no formato de range [YYYY-MM-DD TO YYYY-MM-DD]
                'dataEmissao': f'[{start_date_str} TO {now.strftime("%Y-%m-%d")}]',
                'pagina': 1,
                'limite': 50,
            }
            
            all_orders = []
            page = 1
            while True:
                current_params = params.copy()
                current_params['pagina'] = page
                
                # O filtro 'dataEmissao' já está em current_params
                response_data = self.api_client.get_sales_orders(token, **current_params)
                
                if response_data and 'data' in response_data:
                    items = response_data['data']
                    all_orders.extend(items)
                    if len(items) < 50:
                        break
                    page += 1
                    time.sleep(0.5)
                else:
                    break

            if all_orders:
                self.logger.info(f"📊 Total de pedidos encontrados: {len(all_orders)}")
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
                
            self.sales_manager.recalculate_from_orders(all_orders)

        except Exception as e:
            self.logger.exception(f"Erro no processamento de pedidos de venda: {e}")
        finally:
            self.recalculation_lock.release()

# Instâncias Globais
config = Config()
if not config.REDIRECT_URI:
    logger.error("ERRO FATAL: BLING_REDIRECT_URI não configurada no Render")
    pass
sales_manager = SalesManager(config)
orchestrator = AutomationOrchestrator(config, sales_manager)


# ============================================================================ 
# 7. TEMPLATE DASHBOARD (HTML/JS)
# ============================================================================

# O Dashboard HTML/JS foi mantido, mas o texto do KPI foi ajustado no JS
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bling Automação Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <style>
        body { background-color: #f8f9fa; }
        .navbar { background-color: #343a40; }
        .log-box {
            height: 300px;
            overflow-y: scroll;
            background-color: #212529;
            color: #ccc;
            padding: 10px;
            font-family: monospace;
            font-size: 0.85rem;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        .log-timestamp { color: #888; margin-right: 5px; }
        .log-level-INFO { color: #28a745; }
        .log-level-WARNING { color: #ffc107; }
        .log-level-ERROR { color: #dc3545; }
        .log-level-DEBUG { color: #007bff; /* debug */ }
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
                    <h5>Pedidos Diários (Dia de Hoje)</h5>
                    <h3 id="kpi-daily" class="text-primary">0</h3>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card p-3 text-center kpi-card kpi-weekly">
                    <h5>Pedidos Semanais (Semana Corrente)</h5>
                    <h3 id="kpi-weekly" class="text-warning">0</h3>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card p-3 text-center kpi-card kpi-historic">
                    <h5>Pedidos Mensais (Mês Corrente)</h5>
                    <h3 id="kpi-historic" class="text-success">0</h3>
                </div>
            </div>
            <small class="text-muted mt-2">
                Último Recálculo de KPIs: <span id="last-recalculated">N/D</span>
            </small>
        </div>

        <div class="card mb-4">
            <div class="card-header">Logs em Tempo Real</div>
            <div class="card-body bg-dark p-0">
                <div id="logs-content" class="log-box"></div>
            </div>
        </div>

        <ul class="nav nav-tabs" id="myTab" role="tablist">
            <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#tab-search">Busca de Produtos</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-kits" onclick="loadKits()">Kits Cadastrados</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-products">Produtos Simples</button></li>
        </ul>

        <div class="tab-content py-3" id="content-tabs">
            <div class="tab-pane fade show active" id="tab-search" role="tabpanel">
                <h4>Buscar Produtos (Cache ou Bling API)</h4>
                <div class="input-group mb-3">
                    <input type="text" id="search-input" class="form-control" placeholder="SKU ou nome do produto...">
                    <button class="btn btn-primary" type="button" onclick="searchProducts()">Buscar</button>
                </div>
                <div id="auth-required-search" class="alert alert-warning hidden">
                    Autenticação Bling necessária para realizar buscas.
                </div>
                <div id="search-results">
                    </div>
            </div>

            <div class="tab-pane fade" id="tab-kits" role="tabpanel">
                <h4>Kits Cadastrados</h4>
                <div id="auth-required-kits" class="alert alert-warning hidden">
                    Autenticação Bling necessária para carregar dados.
                </div>
                <div id="kits-list">
                    </div>
            </div>
            
            <div class="tab-pane fade" id="tab-products" role="tabpanel">
                <h4>Produtos Simples Cadastrados</h4>
                <div id="products-list">
                    <div class="alert alert-info">Carregando dados. Navegue para a aba 'Kits Cadastrados' para iniciar o cache.</div>
                </div>
            </div>
        </div>
        
    </div>

    <script>
        const API = '/api';
        let isAuthenticated = false;

        function formatLog(log) {
            let message = log.message.replace(/</g, "&lt;").replace(/>/g, "&gt;");
            let levelClass = `log-level-${log.level}`;
            let timestamp = log.timestamp.split('T')[1]; 
            return `<div><span class="log-timestamp">${timestamp}</span><span class="${levelClass}">[${log.level}]</span> ${message}</div>`;
        }
        
        function formatDateTime(isoString) {
            try {
                const date = new Date(isoString);
                return date.toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'medium' });
            } catch (e) {
                return isoString;
            }
        }

        // Configuração de WebSocket para Logs em tempo real
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
        
        // Configuração de WebSocket para Notificação de KPI em tempo real
        const wsKpi = new WebSocket(`${proto}://${window.location.host}/ws/kpi`);
        wsKpi.onmessage = (e) => {
             const dSalesStats = JSON.parse(e.data);
             updateKpis(dSalesStats);
        };


        // Função para atualizar os KPIs (chamada via polling E via WebSocket)
        function updateKpis(dSalesStats) {
            document.getElementById('kpi-daily').textContent = dSalesStats.daily;
            document.getElementById('kpi-weekly').textContent = dSalesStats.weekly;
            document.getElementById('kpi-historic').textContent = dSalesStats.historic;
            document.getElementById('last-recalculated').textContent = formatDateTime(dSalesStats.last_update);
        }

        async function checkStatus() {
            try {
                // 1. Check Auth Status
                const rStatus = await fetch(API + '/status');
                const dStatus = await rStatus.json();
                const badge = document.getElementById('status-badge');
                isAuthenticated = dStatus.authenticated;
                
                if(isAuthenticated) {
                    badge.className = 'badge bg-success me-2';
                    badge.textContent = 'Online';
                    document.getElementById('auth-link').classList.add('d-none');
                    // Remove a classe hidden do conteúdo principal se autenticado
                    document.getElementById('content-tabs').classList.remove('hidden'); 
                } else {
                    badge.className = 'badge bg-danger me-2';
                    badge.textContent = 'Offline';
                    document.getElementById('auth-link').classList.remove('d-none');
                    // Adiciona a classe hidden ao conteúdo principal se não autenticado
                    document.getElementById('content-tabs').classList.add('hidden'); 
                }
                document.getElementById('auth-link').href = dStatus.auth_url;

                // 2. Update Sales Stats (KPIs) via Polling (Mantido como fallback)
                if (isAuthenticated) {
                    const rSalesStats = await fetch(API + '/sales/stats');
                    if (rSalesStats.ok) {
                        const dSalesStats = await rSalesStats.json();
                        updateKpis(dSalesStats); // Usa a função de atualização
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
                
                // 3. Atualiza lista de produtos simples (depois de carregar os kits)
                if (isAuthenticated && document.getElementById('products-list').dataset.loaded === 'true') {
                    loadSimpleProducts();
                }

            } catch (e) {
                console.error("Erro no checkStatus:", e);
                document.getElementById('status-badge').className = 'badge bg-danger me-2';
                document.getElementById('status-badge').textContent = 'Erro de Conexão';
            }
        }

        // Polling (Atualiza a cada 5 segundos)
        checkStatus();
        setInterval(checkStatus, 5000); 


        // Função de busca (na aba de Busca)
        async function searchProducts() {
            if (!isAuthenticated) {
                document.getElementById('auth-required-search').classList.remove('hidden');
                document.getElementById('search-results').innerHTML = '<div class="alert alert-info">Autentique-se para realizar buscas.</div>';
                return;
            }
            document.getElementById('auth-required-search').classList.add('hidden');
            const q = document.getElementById('search-input').value;
            const div = document.getElementById('search-results');
            if (q.trim() === '') {
                 div.innerHTML = '<div class="alert alert-warning">Digite o SKU ou nome para buscar.</div>';
                 return;
            }
            div.innerHTML = '<div class="alert alert-info">Buscando...</div>';
            
            try {
                const r = await fetch(`${API}/product/search?q=${encodeURIComponent(q)}`);
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
                    // Determina se é kit para formatar melhor
                    const isKit = p.componentes && p.componentes.length > 0;
                    const stockText = isKit ? 'N/D' : `<b>Estoque:</b> ${p.estoque}`;
                    const priceText = isKit ? '' : ` | <b>Preço:</b> R$ ${p.preco || '0.00'}`;
                    const tipoText = isKit ? 'Kit' : p.tipo || 'N/D';

                    html += ` 
                        <div class="list-group-item"> 
                            <div class="d-flex"> 
                                <img src="${p.imagemURL || ''}" style="width:60px;height:60px;object-fit:contain;margin-right:10px;border-radius:6px;background:#f1f1f1" onerror="this.style.display='none'"> 
                                <div class="flex-grow-1"> 
                                    <div class="d-flex w-100 justify-content-between"> 
                                        <h5 class="mb-1">${p.nome || p.produto || 'Sem nome'}</h5> 
                                        <small class="text-muted">${p.sku || 'N/D'}</small> 
                                    </div> 
                                    <p class="mb-1 text-muted">${p.descricaoCurta || ''}</p> 
                                    <small class="text-muted d-block"> 
                                        ${stockText}
                                        <b style="margin-left:10px;">Tipo:</b> ${tipoText}
                                        ${priceText}
                                    </small> 
                                    ${isKit ? ` 
                                        <div class="mt-2 p-2 bg-light rounded"> 
                                            <b class="d-block mb-1">Componentes (${p.componentes.length}):</b> 
                                            <small>${p.componentes.map(c => `${c.quantidade}x ${c.nome || 'Sem nome'} (SKU: ${c.sku || 'N/D'})` ).join("<br>")}</small>
                                        </div> 
                                    ` : ""}
                                </div> 
                            </div> 
                        </div> `;
                });
                html += '</div>';
                div.innerHTML = html;
            } catch(e) {
                div.innerHTML = `<div class="alert alert-danger">Erro: ${e}</div>`;
            }
        };

        // Carregar Kits (chama o worker de cache no backend)
        async function loadKits() {
            const div = document.getElementById('kits-list');
            const authRequiredDiv = document.getElementById('auth-required-kits');
            if (!isAuthenticated) {
                div.innerHTML = '';
                authRequiredDiv.classList.remove('hidden');
                return;
            }
            authRequiredDiv.classList.add('hidden');
            div.innerHTML = '<div class="alert alert-info">Carregando dados. Este processo depende da finalização do cache em background (veja logs).</div>';
            
            try {
                const r = await fetch(`${API}/products/kits`);
                if (r.status === 401) {
                    div.innerHTML = '<div class="alert alert-warning">Sessão expirada. Autentique novamente.</div>';
                    checkStatus();
                    return;
                }
                const data = await r.json();
                
                if (data.length === 0 && document.getElementById('products-list').dataset.loaded !== 'true') {
                     div.innerHTML = '<div class="alert alert-info">Nenhum kit encontrado no cache, ou cache ainda não foi concluído.</div>';
                     return;
                }
                
                let html = '<div class="list-group">';
                data.forEach(p => {
                     html += ` 
                        <div class="list-group-item"> 
                            <div class="d-flex"> 
                                <img src="${p.imagemURL || ''}" style="width:60px;height:60px;object-fit:contain;margin-right:10px;border-radius:6px;background:#f1f1f1" onerror="this.style.display='none'"> 
                                <div class="flex-grow-1"> 
                                    <div class="d-flex w-100 justify-content-between"> 
                                        <h5 class="mb-1">${p.produto || 'Kit Sem Nome'}</h5> 
                                        <small class="text-muted">${p.sku || 'N/D'}</small> 
                                    </div> 
                                    <small class="text-muted d-block"> <b>ID:</b> ${p.id} </small>
                                    <div class="mt-2 p-2 bg-light rounded"> 
                                        <b class="d-block mb-1">Componentes (${p.componentes.length}):</b> 
                                        <small>${p.componentes.map(c => `${c.quantidade}x ${c.nome || 'Sem nome'} (SKU: ${c.sku || 'N/D'})` ).join("<br>")}</small>
                                    </div>
                                </div> 
                            </div> 
                        </div> `;
                });
                html += '</div>';
                div.innerHTML = html;
                
                // Marca que a lista de kits (e, por extensão, o cache principal) foi carregado
                document.getElementById('products-list').dataset.loaded = 'true'; 
                loadSimpleProducts(); // Carrega produtos simples em seguida
            
            } catch(e) {
                console.error("Erro carregando kits:", e);
                div.innerHTML = '<div class="alert alert-danger">Erro ao carregar lista. Verifique os logs.</div>';
            }
        }
        
        // Carregar Produtos Simples (Após Kits)
        async function loadSimpleProducts() {
            const div = document.getElementById('products-list');
            div.innerHTML = '<div class="alert alert-info">Carregando produtos simples...</div>';
             try {
                const r = await fetch(`${API}/products/simple`);
                const data = await r.json();
                
                if (data.length === 0) {
                     div.innerHTML = '<div class="alert alert-info">Nenhum produto simples encontrado no cache.</div>';
                     return;
                }
                
                let html = '<div class="list-group">';
                data.forEach(p => {
                     html += ` 
                        <div class="list-group-item"> 
                            <div class="d-flex"> 
                                <img src="${p.imagemURL || ''}" style="width:60px;height:60px;object-fit:contain;margin-right:10px;border-radius:6px;background:#f1f1f1" onerror="this.style.display='none'"> 
                                <div class="flex-grow-1"> 
                                    <div class="d-flex w-100 justify-content-between"> 
                                        <h5 class="mb-1">${p.produto || 'Produto Sem Nome'}</h5> 
                                        <small class="text-muted">${p.sku || 'N/D'}</small> 
                                    </div> 
                                    <small class="text-muted d-block"> 
                                        <b>ID:</b> ${p.id} | 
                                        <b>Tipo:</b> ${p.tipo} | 
                                        <b>Estoque:</b> ${p.estoque} 
                                    </small>
                                </div> 
                            </div> 
                        </div> `;
                });
                html += '</div>';
                div.innerHTML = html;

            } catch(e) {
                console.error("Erro carregando produtos simples:", e);
                div.innerHTML = '<div class="alert alert-danger">Erro ao carregar lista. Verifique os logs.</div>';
            }
        }

        document.addEventListener('DOMContentLoaded', () => { 
            // Não chama loadKits aqui para evitar a busca de todos os produtos imediatamente
            // A busca deve ser ativada pelo usuário ou pelo worker
        });

    </script>
</body>
</html>
"""

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
                self.logger.error("ERRO FATAL: BLING_REDIRECT_URI não configurada no Render")
                return "Erro de Configuração: BLING_REDIRECT_URI não configurada.", 500

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
            
            # Lock para evitar que múltiplas requisições de callback processem o mesmo código
            if not token_exchange_lock.acquire(blocking=False):
                self.logger.warning("Concorrência detectada no callback. Redirecionando para home.")
                return redirect('/')
            
            try:
                with WebServer.code_lock:
                    # Previne reuso do código OAuth (segurança)
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

        # ENDPOINT DE ESTATÍSTICAS DE VENDAS (AGORA CORRIGIDO COM RE-LEITURA E SEM LOG SPAM)
        @self.app.route("/api/sales/stats")
        def api_sales_stats():
            """Retorna os contadores Diário, Semanal e Histórico (Mensal)."""
            stats = sales_manager.get_stats()
            # DEBUG (v4.4/v4.6): Log de debug removido/comentado para evitar spam no polling
            # logger.debug(f"📡 API /sales/stats retornando: {stats}") 
            return jsonify(stats)

        # ENDPOINT DE BUSCA DE PRODUTOS
        @self.app.route("/api/product/search")
        def api_product_search():
            termo = request.args.get('q', '').strip()
            if not self.orchestrator.auth.is_authenticated():
                return jsonify({"error": "Não autenticado"}), 401
                
            if not termo:
                return jsonify([])

            access_token = self.orchestrator.auth.get_valid_token()
            
            # Tenta buscar diretamente pelo SKU/Nome no Bling
            resp = get_bling_products_safe(self.orchestrator.api_client, sku=termo if len(termo) < 10 else None, nome=termo if len(termo) >= 10 else None, access_token=access_token)
            
            initial_results = resp.get("data", [])
            final_results = []
            seen_ids = set()

            # Processa os resultados da busca na API, buscando detalhes se necessário
            for p in initial_results:
                product_id = p.get("id")
                if not product_id: continue
                
                details = p
                # Se não tem estrutura (kits) ou descrição curta, busca os detalhes
                if not details.get("estrutura") or not details.get("descricaoCurta"):
                    details = self.orchestrator.api_client.get_product_details(access_token, product_id)
                
                if not details: continue
                
                seen_ids.add(product_id)
                
                # Normaliza o estoque (pode vir em 'estoqueAtual' ou aninhado em 'estoque')
                estoque_val = ( 
                    p.get("estoqueAtual", 0) 
                    or details.get("estoqueAtual", 0)
                    or details.get("estoque", {}).get("saldoVirtualTotal", 0)
                )

                produto_completo = {
                    "id": p["id"],
                    "sku": p.get("codigo") or p.get("sku"),
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
            
            # Adiciona resultados do cache local se a busca Bling não foi exaustiva
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

        # ENDPOINT PARA OBTER KITS DO CACHE
        @self.app.route("/api/products/kits")
        def api_products_kits():
            if not self.orchestrator.auth.is_authenticated():
                return jsonify({"error": "Não autenticado"}), 401
            return jsonify(self.orchestrator.get_all_kits())

        # ENDPOINT PARA OBTER PRODUTOS SIMPLES DO CACHE
        @self.app.route("/api/products/simple")
        def api_products_simple():
            if not self.orchestrator.auth.is_authenticated():
                return jsonify({"error": "Não autenticado"}), 401
            return jsonify(self.orchestrator.get_all_products())


    def setup_websocket(self):
        
        # WebSocket para Logs em Tempo Real
        @self.sock.route('/ws/logs')
        def logs_socket(ws):
            last_idx = len(memory_handler.logs)
            self.logger.info("WebSocket de Logs conectado.")
            while True:
                try:
                    all_logs = memory_handler.get_logs()
                    if len(all_logs) > last_idx:
                        new_logs = all_logs[last_idx:]
                        ws.send(json.dumps({"logs": new_logs}))
                        last_idx = len(all_logs)
                    try:
                        # CORREÇÃO: Tratamento para ConnectionClosed
                        ws.receive(timeout=1)
                    except ConnectionClosed:
                         break # Sai do loop limpo
                    except Exception:
                        pass
                except Exception:
                    break

        # WebSocket para Notificação de KPI
        @self.sock.route('/ws/kpi')
        def kpi_socket(ws):
            
            def notify_kpi(stats_data):
                try:
                    ws.send(json.dumps(stats_data))
                except ConnectionClosed:
                    pass # O callback será removido no finally
                except Exception as e:
                    self.logger.error(f"Erro ao enviar KPI via WS: {e}")

            # Registra o callback
            with kpi_update_lock:
                kpi_update_callbacks.append(notify_kpi)
            
            self.logger.info("WebSocket KPI conectado.")
            # Envia o estado atual imediatamente
            try:
                notify_kpi(sales_manager.get_stats())
            except Exception:
                pass

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
        # Inicia o worker em uma thread separada
        Thread(target=orchestrator.load_data_worker, daemon=True).start()
        app.run(host='0.0.0.0', port=args.port, debug=False)

if __name__ == "__main__":
    run_cli()

# --- GUNICORN CONFIGURAÇÕES (TIMEOUT AJUSTADO PARA 300) ---
import os as _os
_os.environ.setdefault("GUNICORN_CMD_ARGS", "--workers 4 --threads 4 --worker-class gevent --timeout 300")