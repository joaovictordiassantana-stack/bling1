#!/usr/bin/env python3

from gevent import monkey
monkey.patch_all()   # torna as bibliotecas padrão cooperativas com gevent (requests, socket, threading...)
"""
bling.py - Sistema completo de automação Bling com design premium (CORRIGIDO v4.7)
Implementa OAuth 2.0, API robusta, gerenciamento de estoque/compras e dashboard web.
- CORREÇÃO CRÍTICA (v4.4): Implementação de WebSocket para notificação em TEMPO REAL de KPIs.
- FIX SINCRONIZAÇÃO (v4.4): get_stats() agora força a leitura do arquivo para sincronização multi-worker.
- FIX SPAM DE LOG (v4.5): Ajuste no _load_stats para evitar logs repetitivos de 'Nenhum KPI encontrado'.
- FIX SPAM DE LOG (v4.6): Reduzido nível de log para INFO e removidos logs DEBUG repetitivos de /api/sales/stats.
- FEATURE (v4.6): Histórico de pedidos expandido de 9 para 30 dias.
- FIX CRÍTICO (v4.7): Corrigido contador de histórico para validar período de 30 dias.
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
            # FIX SPAM DE LOG (v4.6): Altera para INFO e só loga se não foi a falha inicial
            if not self._initial_load_failed:  
                logger.info(f"KPIs carregados do arquivo. Histórico: {self.historic_count}.")
                self._initial_load_failed = False 
            else:
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
        last_month = now - timedelta(days=30)  # ✅ NOVO: Adiciona referência de 30 dias
        
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

            # ✅ CORRIGIDO: Só conta histórico se estiver nos últimos 30 dias
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
                    self.logger.warning(f"Erro API Detalhe Produto {product_id}: {response.status_code} - {response.text}")
            except Exception as e:
                self.logger.warning(f"Erro conexao API (Detalhe Produto {product_id}): {e}")
            time.sleep(1)
        return {}
    
    def get_sales_orders(self, access_token: str, filters: Dict[str, Any]) -> Dict[str, Any]:
        """Busca pedidos de venda com filtros. Retorna uma página de pedidos."""
        headers = {'Authorization': f'Bearer {access_token}', 'Accept': 'application/json'}
        # Converte a data para ISO 8601 (sem o 'Z' de UTC, que o Bling não gosta)
        # O filtro de data é crucial para evitar buscar milhões de pedidos.
        if 'dataEmissaoInicial' in filters and isinstance(filters['dataEmissaoInicial'], datetime):
            filters['dataEmissaoInicial'] = filters['dataEmissaoInicial'].isoformat(timespec='seconds')

        url = f"{self.config.BLING_API_URL}/pedidos/vendas"
        
        # Parâmetros padrão de paginação
        params = {
            'pagina': filters.pop('pagina', 1),
            'limite': filters.pop('limite', 100),
            **filters # Adiciona os filtros personalizados (ex: status, data)
        }
        
        # FIX: Remove o filtro de dataEmissaoFinal se estiver vazio (problema de serialização)
        if params.get('dataEmissaoFinal') is None:
            params.pop('dataEmissaoFinal', None)

        for attempt in range(self.config.MAX_RETRIES):
            try:
                response = self.session.get(url, headers=headers, params=params, timeout=self.config.REQUEST_TIMEOUT)
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:  # Rate limit
                    self.logger.warning("Rate limit Bling atingido. Esperando 5s.")
                    time.sleep(5)
                    continue
                elif response.status_code == 401:
                    # Token inválido ou expirado (deve ser tratado pela lógica principal)
                    self.logger.warning("Token de acesso inválido ou expirado.")
                    raise BlingAuthError("Token inválido ou expirado.")
                else:
                    self.logger.warning(f"Erro API Pedidos ({response.status_code}): {response.text}")
            except BlingAuthError:
                raise
            except Exception as e:
                self.logger.error(f"Erro de conexão ao buscar pedidos: {e}")
            time.sleep(1)
        
        # Se falhar após todas as tentativas
        raise BlingAPIError(f"Falha ao buscar pedidos de venda após {self.config.MAX_RETRIES} tentativas.")


# ============================================================================ 
# 6. ORCHESTRATOR / WORKER (LÓGICA DE NEGÓCIO)
# ============================================================================

class AutomationOrchestrator:
    """
    Coordena o BlingAuth, BlingAPIClient, SalesManager e a lógica de negócio.
    É responsável por rodar o worker em background.
    """
    def __init__(self, config: Config):
        self.config = config
        self.auth = BlingAuth(config)
        self.api_client = BlingAPIClient(config)
        self.sales_manager = SalesManager(config)
        self.component_manager = ComponentConfigManager(config.COMPONENT_CONFIG_FILE)
        self.logger = logger
        self.products: List[Dict[str, Any]] = [] # Produtos simples
        self.kits: List[Dict[str, Any]] = [] # Kits com componentes
        self.product_lock = Lock()
        self.recalculation_lock = Lock() # Lock específico para recálculo de KPIs
        
        # Carrega dados iniciais do arquivo (se existirem)
        self._load_products_and_kits(self.auth.get_valid_token() or "")

    def _load_products_and_kits(self, access_token: str):
        """Carrega a lista de produtos e kits do arquivo de configuração persistido."""
        if not access_token:
            # Não é um erro crítico se não há token na inicialização.
            self.logger.warning("Token indisponível na inicialização. Kits/Produtos serão carregados no worker.")
            return
            
        # Tenta carregar do arquivo
        try:
            config_data = self.component_manager.config
            self.products = config_data.get('products', [])
            self.kits = config_data.get('kits', [])
            
            if self.products or self.kits:
                self.logger.info(f"Dados de produtos/kits carregados do arquivo: {len(self.products)} produtos, {len(self.kits)} kits.")
                return
            
            # Se não houver dados no arquivo, força a busca da API
            self.logger.info("Nenhum dado persistido encontrado, iniciando busca na API.")
            self.load_bling_products(access_token)
            
        except Exception as e:
            self.logger.error(f"Erro ao carregar dados persistidos: {e}")
            self.load_bling_products(access_token)
    
    def load_bling_products(self, access_token: Optional[str] = None):
        """Busca todos os produtos e kits do Bling e salva localmente."""
        token = access_token or self.auth.get_valid_token()
        if not token:
            self.logger.warning("Token indisponível para carregar produtos.")
            return
            
        todos_produtos = []
        page = 1
        
        self.logger.info("Iniciando busca completa de produtos (API).")
        
        # PASSO 1: Buscar todos os produtos paginando
        while True:
            try:
                # Busca 50 produtos por página
                resp = self.api_client.get_products(token, page=page, limit=50)
                items = resp.get('data') or []
                
                if not items:
                    break
                    
                # A API V3 retorna uma lista de dicionários com a chave 'id' e 'nome'
                if items and isinstance(items, list):
                    todos_produtos.extend(items)

                if len(items) < 50:
                    break
                page += 1
                time.sleep(0.2)
            except Exception as e:
                self.logger.error(f"Erro ao carregar página {page}: {e}")
                break

        # PASSO 2: Criar Mapa para busca rápida (ID -> Produto)
        produto_map = {str(p.get("id")): p for p in todos_produtos}
        self.logger.info(f"Total baixado: {len(todos_produtos)}. Processando Kits...")
        
        # Resetar listas
        new_products = []
        new_kits = []
        
        # PASSO 3: Separar Kits e preencher nomes dos componentes
        for p in todos_produtos:
            p_id = p.get("id")
            estrutura = p.get("estrutura", {})
            componentes = estrutura.get("componentes", [])
            # Heurística para identificar kits: tem componentes ou o campo 'tipo'/'formato' é 'K'
            eh_kit = len(componentes) > 0 or p.get("tipo") == "K" or p.get("formato") == "K"
            img_url = extract_image_url(p)
            
            if eh_kit:
                comps_formatados = []
                # Se for kit mas não tiver componentes na lista sumária, busca o detalhe
                if not componentes and p_id:
                    try:
                        det = self.api_client.get_product_details(token, p_id)
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
                    
                new_kits.append({
                    "id": p_id,
                    "sku": p.get("codigo"),
                    "produto": p.get("nome"),
                    "imagemURL": img_url,
                    "componentes": comps_formatados
                })
            else:
                new_products.append({
                    "id": p.get("id"),
                    "sku": p.get("codigo"),
                    "produto": p.get("nome"),
                    "imagemURL": img_url,
                    "tipo": p.get("tipo"),
                    "estoque": p.get("estoque", {}).get("saldoAtual", 0) # Estoque atual na lista sumária
                })

        # ATUALIZAÇÃO E PERSISTÊNCIA DENTRO DO LOCK
        with self.product_lock:
            self.products = new_products
            self.kits = new_kits
            self.component_manager.config['products'] = self.products
            self.component_manager.config['kits'] = self.kits
            self.component_manager._save_config()
            self.logger.info(f"✅ Produtos e Kits atualizados e salvos: {len(self.products)} produtos, {len(self.kits)} kits.")

    def get_display_products(self) -> List[Dict[str, Any]]:
        """Retorna a lista combinada de produtos e kits para exibição no dashboard."""
        with self.product_lock:
            # Retorna uma cópia combinada para evitar modificação externa
            return self.products + self.kits
            
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
            now = datetime.now()
            thirty_days_ago = now - timedelta(days=30) 
            
            # Filtra pedidos "Atendidos" ou "Em Aberto" (Aberto, Em Andamento, Atendido, etc)
            # O status 9 é 'Atendido' (Faturado/Enviado) - é o mais confiável
            # Incluir 15 (Em Aberto) e 24 (Em Andamento) para contagem de KPIs
            filters = {
                'dataEmissaoInicial': thirty_days_ago, # Busca pedidos desde 30 dias atrás
                'status': '15|24|9', # Em Aberto, Em Andamento, Atendido
            }
            
            self.logger.info(f"Iniciando busca de pedidos de venda (30 dias) com filtros: {filters.get('status')}")
            
            all_orders = []
            page = 1
            max_pages = 5 # Limita a 5 páginas (500 pedidos) para evitar timeout em ambientes free

            while page <= max_pages:
                try:
                    resp = self.api_client.get_sales_orders(token, {**filters, 'pagina': page, 'limite': 100})
                    items = resp.get('data') or []
                    
                    if not items:
                        break
                        
                    all_orders.extend(items)
                    
                    # Se vieram menos do que o limite, esta é a última página
                    if len(items) < 100:
                        break
                        
                    page += 1
                    time.sleep(0.3) # Pequena pausa entre páginas
                    
                except BlingAuthError:
                    self.logger.warning("Erro de autenticação ao buscar pedidos. Interrompendo a busca.")
                    return
                except BlingAPIError as e:
                    self.logger.error(f"Erro de API ao buscar pedidos: {e}. Interrompendo a busca.")
                    return
                except Exception as e:
                    self.logger.error(f"Erro inesperado ao buscar página {page} de pedidos: {e}")
                    break

            self.logger.info(f"Total de pedidos encontrados na busca API (Status {filters.get('status')}): {len(all_orders)}")

            # VALIDAÇÃO DOS DADOS RETORNADOS (APÓS FIX V4.7)
            orders_outside_range = 0
            oldest_order = None
            newest_order = None
            
            # Se a API ignorar o filtro de data (comum em algumas APIs), filtramos localmente.
            # NOVO: Valida se os pedidos estão no período esperado
            thirty_days_ago = now - timedelta(days=30)
            
            for order in all_orders:
                data_obj = order.get('data')
                data_str = None
                
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
                self.logger.info(f"📅 Período dos pedidos retornados: {oldest_order.strftime('%Y-%m-%d')} até {newest_order.strftime('%Y-%m-%d')}")
            
            if orders_outside_range > 0:
                self.logger.warning(f"⚠️ ALERTA: {orders_outside_range} pedidos fora do período de 30 dias! "
                                   f"A API pode estar ignorando o filtro de data. Aplicando filtro local...")
                                   
            self.logger.info(f"📊 Total de pedidos encontrados: {len(all_orders)}")

            # Filtro local como fallback se a API retornar dados antigos
            if orders_outside_range > 0:
                filtered_orders = []
                for order in all_orders:
                    data_obj = order.get('data')
                    data_str = None
                    
                    if isinstance(data_obj, dict):
                        data_str = data_obj.get('dataEmissao')
                    elif isinstance(data_obj, str):
                        data_str = data_obj
                    else:
                        filtered_orders.append(order) # Mantém pedidos sem data (caso raríssimo)
                        continue

                    try:
                        order_date = datetime.strptime(data_str, '%Y-%m-%d')
                        if order_date >= thirty_days_ago:
                            filtered_orders.append(order)
                    except:
                        filtered_orders.append(order)
                        
                all_orders = filtered_orders
                self.logger.info(f"Total de pedidos APÓS filtro local: {len(all_orders)}")


            # O cálculo real é feito no SalesManager, que contém a lógica de 30/7/1 dia(s)
            self.sales_manager.recalculate_from_orders(all_orders)

        finally:
            self.recalculation_lock.release()

# ============================================================================ 
# 7. DASHBOARD WEB (HTML)
# ============================================================================
# HTML Template (MUITO GRANDE - Omitido por Limite de Código)
DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bling Automação v4.7</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.2/css/all.min.css" integrity="sha512-SnH5WK+bZxgPHs44uWIX+LLMDJqgC/qM9S4rVj1f8gN/xT3g9T4p8WcQ4oF1wM1vN/N0uY5q8D1G5O9W7fI4Q==" crossorigin="anonymous" referrerpolicy="no-referrer" />
    <style>
        body { background-color: #f8f9fa; }
        .navbar { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
        .log-box { 
            font-family: 'Courier New', monospace; 
            font-size: .85em; 
            background: #1e1e1e; 
            color: #d4d4d4; 
            border-radius: .5rem; 
            padding: 1rem; 
            max-height: 400px; 
            overflow-y: auto; 
        }
        .log-level-INFO { color: #4ec9b0; }
        .log-level-WARNING { color: #dcdcaa; }
        .log-level-ERROR { color: #f48771; }
        .log-level-DEBUG { color: #569cd6; } /* Adicionado para debug */
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
                    <h5>Pedidos Semanais (Últimos 7d)</h5>
                    <h3 id="kpi-weekly" class="text-warning">0</h3>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card p-3 text-center kpi-card kpi-historic">
                    <h5>Pedidos Históricos (Últimos 30d)</h5>
                    <h3 id="kpi-historic" class="text-success">0</h3>
                </div>
            </div>
        </div>
        <p class="text-muted text-end"><small>Último Recálculo: <span id="last-recalculated">N/D</span></small></p>

        <ul class="nav nav-tabs" id="content-tabs" role="tablist">
            <li class="nav-item" role="presentation">
                <button class="nav-link active" id="search-tab" data-bs-toggle="tab" data-bs-target="#search-pane" type="button" role="tab" aria-controls="search-pane" aria-selected="true"><i class="fas fa-search"></i> Busca de Produtos</button>
            </li>
            <li class="nav-item" role="presentation">
                <button class="nav-link" id="kits-tab" data-bs-toggle="tab" data-bs-target="#kits-pane" type="button" role="tab" aria-controls="kits-pane" aria-selected="false"><i class="fas fa-box-open"></i> Kits e Componentes</button>
            </li>
            <li class="nav-item" role="presentation">
                <button class="nav-link" id="logs-tab" data-bs-toggle="tab" data-bs-target="#logs-pane" type="button" role="tab" aria-controls="logs-pane" aria-selected="false"><i class="fas fa-terminal"></i> Logs em Tempo Real</button>
            </li>
        </ul>
        <div class="tab-content border border-top-0 p-3 bg-white" id="content-tabs-content">
            <div class="tab-pane fade show active" id="search-pane" role="tabpanel" aria-labelledby="search-tab" tabindex="0">
                <div class="input-group mb-3">
                    <input type="text" class="form-control" id="search-input" placeholder="Buscar produto ou SKU no Bling..." aria-label="Buscar produto ou SKU">
                    <button class="btn btn-primary" type="button" id="btn-search"><i class="fas fa-search"></i> Buscar</button>
                </div>
                <div id="search-results">
                    <div class="alert alert-info">Digite um termo ou SKU para buscar no Bling (máx 20 resultados).</div>
                </div>
            </div>
            <div class="tab-pane fade" id="kits-pane" role="tabpanel" aria-labelledby="kits-tab" tabindex="0">
                <h4 class="mb-3">Lista de Kits e seus Componentes</h4>
                <div id="kits-list">
                    <div class="alert alert-info">Carregando lista de Kits...</div>
                </div>
            </div>
            <div class="tab-pane fade" id="logs-pane" role="tabpanel" aria-labelledby="logs-tab" tabindex="0">
                <div class="log-box" id="logs-content">
                    <div class="text-muted">Conectando ao log stream...</div>
                </div>
            </div>
        </div>
    </div>
    
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        const API = '/api/sales';
        
        function formatLog(log) {
            const levelClass = `log-level-${log.level}`;
            return `<div class="${levelClass}">[${log.timestamp}] [${log.level}] ${log.message}</div>`;
        }

        function formatDateTime(isoString) {
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

        // WebSocket Logs
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
                    document.getElementById('content-tabs').classList.remove('hidden');
                } else {
                    badge.className = 'badge bg-danger me-2';
                    badge.textContent = 'Offline';
                    document.getElementById('auth-link').classList.remove('d-none');
                    document.getElementById('content-tabs').classList.add('hidden');
                }

                document.getElementById('auth-link').href = dStatus.auth_url;
                
                // 2. Load initial KPIs
                const rStats = await fetch(API + '/stats');
                const dSalesStats = await rStats.json();
                updateKpis(dSalesStats);

            } catch (e) {
                console.error("Erro ao verificar status:", e);
                // Não precisa de polling se usar WebSocket para KPI
            }
        }

        // WebSocket para KPIs em tempo real
        const wsKpi = new WebSocket(`${proto}://${window.location.host}/ws/kpi`);
        wsKpi.onmessage = (e) => {
            const data = JSON.parse(e.data);
            if (data.type === 'kpi_update') {
                updateKpis(data.data);
            }
        };
        wsKpi.onerror = (e) => {
            console.error("Erro no WebSocket KPI:", e);
        };
        wsKpi.onclose = () => {
            console.log("WebSocket KPI desconectado. Reconectando em 5s...");
            setTimeout(() => {
                // Tenta reconectar (recarregar a página é a forma mais simples)
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
                    // Formata os componentes para exibição
                    let compsHtml = '';
                    if (p.componentes && p.componentes.length > 0) {
                        compsHtml = '<ul class="list-unstyled mt-2 small">';
                        p.componentes.forEach(c => {
                            compsHtml += `<li><i class="fas fa-caret-right"></i> ${c.nome} (${c.quantidade}x) <span class="text-muted">| ${c.sku}</span></li>`;
                        });
                        compsHtml += '</ul>';
                    }

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
                                        <b>Estoque:</b> ${p.estoque} <b style="margin-left:10px;">Tipo:</b> ${p.tipo}
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
                div.innerHTML = '<div class="alert alert-danger">Erro ao realizar a busca. Verifique os logs.</div>';
            }
        }
        
        async function loadKits() {
            const div = document.getElementById('kits-list');
            div.innerHTML = '<div class="alert alert-info">Carregando Kits...</div>';
            
            try {
                const r = await fetch(API + '/kits');
                const data = await r.json();

                if(!data.length) {
                    div.innerHTML = '<div class="alert alert-warning">Nenhum Kit encontrado ou o worker ainda não terminou de processar.</div>';
                    return;
                }

                let html = '<table class="table table-striped table-hover small"><thead><tr><th style="width:60px;"></th><th style="width:120px;">SKU</th><th>Produto</th><th>Componentes</th></tr></thead><tbody>';
                
                data.forEach(k => {
                    const imgHtml = `<img src="${k.imagemURL || ''}" style="width:50px;height:50px;object-fit:contain;border-radius:3px;background:#f1f1f1" onerror="this.src='data:image/svg+xml;utf8,<svg xmlns=\\'http://www.w3.org/2000/svg\\' width=\\'50\\' height=\\'50\\' viewBox=\\'0 0 50 50\\'><rect width=\\'50\\' height=\\'50\\' fill=\\'%23ccc\\' /><text x=\\'50%\\' y=\\'50%\\' dominant-baseline=\\'middle\\' text-anchor=\\'middle\\' font-size=\\'10px\\' fill=\\'%23666\\'>SEM IMAGEM</text></svg>'" />`;
                    
                    let comps = '';
                    if (k.componentes && k.componentes.length > 0) {
                        comps = '<ul>';
                        k.componentes.forEach(c => {
                            comps += `<li>${c.nome} (${c.quantidade}x) <span class="text-muted">| ${c.sku}</span></li>`;
                        });
                        comps += '</ul>';
                    } else {
                        comps = '<span class="text-info" style="font-size:0.8em">KIT sem componentes detalhados.</span>';
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
            checkStatus();
            loadKits();
        });

    </script>
</body>
</html>"""

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
        
        # Rota de erro fatal se a URI não estiver configurada
        if not self.orchestrator.config.REDIRECT_URI:
            @self.app.route('/', defaults={'path': ''})
            @self.app.route('/<path:path>')
            def fatal_error_config(path):
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
                self.logger.warning("Concorrência detectada no callback. Redirecionando para evitar Worker Timeout.")
                time.sleep(3) # Espera curta para tentar resolver a concorrência
                return redirect('/')
            
            try:
                # FIX: Previne o uso do mesmo código de autorização mais de uma vez (erro 400 Bling)
                with self.code_lock:
                    if code in self.used_codes:
                        self.logger.warning(f"Código de autorização {code} já utilizado. Ignorando.")
                        return redirect('/')
                    self.used_codes.add(code)
                
                if self.orchestrator.auth.exchange_code_for_token(code, state):
                    self.logger.info("Autenticação Bling concluída com sucesso.")
                else:
                    self.logger.error("Falha na troca de código por token.")
            finally:
                token_exchange_lock.release()
                
            return redirect('/')

        @self.app.route("/api/sales/status")
        def api_status():
            is_auth = self.orchestrator.auth.is_authenticated()
            auth_url = self.orchestrator.auth.get_authorization_url()
            return jsonify({
                "authenticated": is_auth,
                "auth_url": auth_url,
                "token_valid_until": self.orchestrator.auth.expires_at
            })

        @self.app.route("/api/sales/stats")
        def api_stats():
            # FIX SPAM DE LOG (v4.6): Altera para DEBUG, pois o front faz polling regular
            self.logger.debug("Requisição de estatísticas de vendas recebida.") 
            return jsonify(self.orchestrator.sales_manager.get_stats())

        @self.app.route("/api/sales/product/search")
        def api_product_search():
            q = request.args.get('q', '').strip()
            if not q:
                return jsonify([])

            token = self.orchestrator.auth.get_valid_token()
            if not token:
                # Retorna 401 para o frontend saber que precisa reautenticar
                return jsonify({"error": "Unauthorized"}), 401 

            all_results_base = []
            
            def process_response(resp):
                items = resp.get('data') or []
                for item in items:
                    # Usa o ID como chave para evitar duplicatas, priorizando resultados mais relevantes
                    if str(item.get('id')) not in [p['id'] for p in all_results_base]:
                         all_results_base.append({
                            "id": str(item.get("id")),
                            "sku": item.get("codigo"),
                            "nome": item.get("nome"),
                            "tipo": item.get("tipo"),
                            "situacao": item.get("situacao"),
                            "preco": item.get("preco"),
                            # Usa estoqueAtual (saldoAtual) se disponível
                            "estoque": item.get("estoque", {}).get("saldoAtual", 0), 
                        })

            # 1. Busca por SKU exato
            termo = q
            self.logger.info(f"Buscando API por SKU: {termo}")
            resp_sku = self.orchestrator.api_client.get_products(token, codigo=termo, limit=20)
            process_response(resp_sku)

            # 2. Busca por Nome
            if len(all_results_base) < 20: # Se já achou 20 por SKU, não precisa buscar por nome
                self.logger.info(f"Buscando API por NOME: {termo}")
                resp_nome = self.orchestrator.api_client.get_products(token, nome=termo, limit=20)
                process_response(resp_nome)

            final_results = []
            MAX_DETALHES = 10 # Limita o número de chamadas de detalhe para evitar Rate Limit
            
            # 3. Busca detalhes e componentes
            for idx, p in enumerate(all_results_base):
                if idx >= MAX_DETALHES:
                    break
                    
                try:
                    details = self.orchestrator.api_client.get_product_details(token, p["id"])
                except Exception as e:
                    self.orchestrator.logger.exception("Erro ao buscar detalhe produto %s", p["id"])
                    details = {}
                
                # Pega o estoque mais detalhado possível
                estoque_val = (
                    details.get("estoqueAtual") 
                    or details.get("saldoDisponivel") 
                    or details.get("estoque", {}).get("saldoVirtualTotal", 0)
                )

                # Formata a lista de componentes se for um Kit
                componentes = []
                if details.get("estrutura", {}).get("componentes"):
                    componentes = [
                        {
                            "nome": c.get("produto", {}).get("nome", "Sem nome"),
                            "quantidade": c.get("quantidade", 0),
                            "sku": c.get("produto", {}).get("codigo", "N/D")
                        } 
                        for c in details["estrutura"]["componentes"]
                    ]

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
                    "imagemURL": extract_image_url(details),
                    "componentes": componentes
                }
                
                final_results.append(produto_completo)

            return jsonify(final_results)

        @self.app.route("/api/sales/kits")
        def api_kits():
            """Retorna a lista de Kits processados pelo worker."""
            return jsonify(self.orchestrator.kits)

    def setup_websocket(self):
        @self.sock.route('/ws/logs')
        def logs_socket(ws):
            # Envia o histórico de logs ao conectar
            initial_logs = memory_handler.get_logs(limit=50)
            ws.send(json.dumps({"logs": initial_logs}))
            
            # Mantém a conexão aberta, mas não há um loop de envio contínuo aqui
            # O logger em memória não tem um mecanismo de notificação de push,
            # então o log do console é o principal. O front só recebe o histórico.
            # No entanto, a conexão precisa ser mantida para evitar desconexão imediata.
            while True:
                try:
                    ws.receive(timeout=10) # Keepalive
                except ConnectionClosed:
                    break
                except Exception:
                    pass
            self.logger.info("WebSocket Logs desconectado.")


        @self.sock.route('/ws/kpi')
        def kpi_socket(ws):
            global kpi_update_callbacks, kpi_update_lock

            def notify_kpi(stats):
                """Função de callback chamada quando um novo KPI é recalculado."""
                try:
                    # stats já está no formato JSON-ready {'daily': 2, 'weekly': 13, 'historic': 2287, 'last_update': '2025-12-12T14:40:00.000000'}
                    ws.send(json.dumps({"type": "kpi_update", "data": stats}))
                    self.logger.debug(f"📤 KPI update enviado via WebSocket: {stats}")
                except ConnectionClosed:
                    self.logger.warning("Conexão WebSocket fechada, falha ao enviar KPI.")
                    # Levanta para ser pego pelo bloco finally
                    raise
                except Exception as e:
                    self.logger.error(f"Erro ao enviar KPI via WebSocket: {e}")
            
            # Adiciona o callback à lista global
            with kpi_update_lock:
                kpi_update_callbacks.append(notify_kpi)
            
            self.logger.info("Novo WebSocket KPI conectado.")

            try:
                # Loop para manter a conexão aberta e evitar que o greenlet morra
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
                self.logger.info("WebSocket KPI desconectado.")

# ============================================================================ 
# 10. ENTRY POINT
# ============================================================================

orchestrator = AutomationOrchestrator(Config)
# Necessário para o WebServer ter acesso ao SalesManager
sales_manager = orchestrator.sales_manager 


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
_os.environ.setdefault('GUNICORN_CMD_ARGS', f'--bind=0.0.0.0:{_os.environ.get("PORT", "8000")} --workers=4 --worker-class=gevent --timeout=300')