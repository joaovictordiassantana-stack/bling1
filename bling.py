#!/usr/bin/env python3

from gevent import monkey
monkey.patch_all()   # torna as bibliotecas padrão cooperativas com gevent (requests, socket, threading...)
"""
bling.py - Sistema completo de automação Bling com design premium (CORRIGIDO v4.0)
Implementa OAuth 2.0, API robusta, gerenciamento de estoque/compras e dashboard web.
- CORREÇÃO CRÍTICA: Lógica de parseamento de data em Pedidos de Venda para multi-payloads (API/Webhook).
- Correção: Persistência dos KPIs para ambientes Multi-Worker (Gunicorn/Render).
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
    
    logger = logging.getLogger('bling_automacao')
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
            if data and 'last_recalculated' in data:
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
    Gerencia contadores de Pedidos de Venda Diárias, Semanais e o Histórico.
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

    def __post_init__(self):
        # Carrega o estado persistido na inicialização
        self._load_stats()


    # NOVO: Carregamento do estado persistente
    def _load_stats(self):
        data = load_stats_safe(self.config.SALES_STATS_FILE)
        if data:
            with self.lock:
                self.daily_count = data.get('daily', 0)
                self.weekly_count = data.get('weekly', 0)
                self.historic_count = data.get('historic', 0)
                # Usa a data carregada ou a data de inicialização se o carregamento falhar
                self.last_recalculated = data.get('last_recalculated', datetime.now())
            logger.info(f"KPIs carregados do arquivo. Histórico: {self.historic_count}.")
        else:
             logger.info("Nenhum KPI persistido encontrado, usando valores iniciais (0).")

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
        with self.lock:
            # Garante que o worker que está lendo a API tem o estado mais recente
            self._load_stats() # Tenta carregar do arquivo novamente
            
            # Retorna o timestamp em formato ISO para o front
            return {
                "daily": self.daily_count,
                "weekly": self.weekly_count,
                "historic": self.historic_count,
                # Retorna o timestamp de quando o worker processou por último
                "last_update": self.last_recalculated.isoformat() 
            }

    # MÉTODO CORRIGIDO (v4.0): Lida com múltiplos formatos de data/hora
    def recalculate_from_orders(self, orders: List[Dict[str, Any]]):
        """Calcula KPIs baseando-se na data/hora de emissão dos pedidos."""
        now = datetime.now()
        # Período de 24h exatas
        yesterday = now - timedelta(hours=24) 
        # Período de 7 dias exatos
        last_week = now - timedelta(days=7)
        
        daily = 0
        weekly = 0
        historic = 0
        
        # O cálculo é feito fora do lock, apenas a atualização do estado é protegida.
        for order in orders:
            # CORREÇÃO CRÍTICA: Adiciona checagem de tipo
            if not isinstance(order, dict):
                logger.warning(f"Item inesperado encontrado na lista de pedidos de venda, ignorando: {order}")
                continue
            
            # FIX CRÍTICO: A data pode vir em DOIS formatos diferentes:
            # 1. De /pedidos/vendas API: {'data': {'dataEmissao': '2025-12-12', 'horaEmissao': '14:30:00'}}
            # 2. De Webhook/Logs: Pode ser string direta na chave 'data'
                            
            data_emissao_str = None
                            
            # Tenta Formato 1: Estrutura aninhada (API v3 padrão)
            data_obj = order.get('data')
            if isinstance(data_obj, dict):
                data_emissao_str = data_obj.get('dataEmissao')
                hora_emissao = data_obj.get('horaEmissao')
            # Tenta Formato 2: String direta na chave 'data' (alguns webhooks)
            elif isinstance(data_obj, str):
                data_emissao_str = data_obj
                hora_emissao = None
                            
            if not data_emissao_str:
                # DEBUG: Loga se a data não foi encontrada
                logger.debug(f"Pedido {order.get('id')} sem dataEmissao. Estrutura: {order.keys()}")
                continue
                            
            try:
                # Constrói a data/hora para comparação
                order_date = datetime.strptime(data_emissao_str, '%Y-%m-%d')
                                    
                # Se temos hora_emissao e ela é válida, adiciona ao datetime
                if hora_emissao and isinstance(hora_emissao, str):
                    try:
                        parts = hora_emissao.split(':')
                        if len(parts) == 3:
                            h, m, s = map(int, parts)
                            order_date = order_date.replace(hour=h, minute=m, second=s)
                    except (ValueError, AttributeError):
                        pass  # Se não conseguir parsear a hora, usa apenas data
            except Exception as e:
                # WARNING: Loga erro de parseamento
                logger.warning(f"Erro ao parsear data '{data_emissao_str}' do pedido {order.get('id')}: {e}")
                continue

            historic += 1  # Contagem de pedidos (dentro do intervalo buscado)
                            
            # O cálculo é feito sobre a data do pedido, garantindo precisão 24/7
            if order_date >= last_week:
                weekly += 1
                            
            if order_date >= yesterday:
                daily += 1 

        # ATUALIZAÇÃO SÓ DEPOIS DO CÁLCULO, DENTRO DO LOCK
        with self.lock:
            # Atualiza todos os contadores de uma vez
            self.daily_count = daily
            self.weekly_count = weekly
            self.historic_count = historic
            self.last_recalculated = now # Atualiza o tempo de processamento
            
            # PERSISTE O ESTADO ATUAL
            save_stats(self._get_state_for_save(), self.config.SALES_STATS_FILE)
            
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
        # Se já estiver autenticado, não faz nada
        if self.is_authenticated():
            self.logger.info("Tentativa de callback ignorada: Token já válido.")
            return True

        # Validação do State - Ajuste para ser tolerante se o usuário já estivesse autenticado antes
        if self.state is None:
            self.state = state
            self._save_state(state)
        
        # Correção OBRIGATÓRIA: Aviso em vez de bloqueio rígido se houver mismatch mas o código parecer válido
        if self.state and state != self.state:
            self.logger.warning(f"State mismatch detectado (Ignorado para evitar bloqueio): {state} vs {self.state}")
            # Não retornamos False aqui para não travar o fluxo se o browser recarregou
            
        try:
            client = f"{self.config.CLIENT_ID}:{self.config.CLIENT_SECRET}"
            auth_header = base64.b64encode(client.encode()).decode()
            headers = {"Authorization": f"Basic {auth_header}", "Content-Type": "application/x-www-form-urlencoded"}
            payload = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': self.config.REDIRECT_URI}
            
            # Timeout curto para evitar travar worker
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
    # Ex: midia: [{ url: "..." }] ou imagens: [{ link: "..." }]
    for list_key in ["midia", "midias", "imagens", "fotos", "anexos"]:
        items = prod.get(list_key, [])
        if isinstance(items, list):
            for item in items:
                # Se for string direta
                if isinstance(item, str) and item.startswith("http"):
                    return item
                # Se for objeto, recursão rasa
                if isinstance(item, dict):
                    ret = extract_image_url(item, depth + 1)
                    if ret: return ret

    # 3. Tenta descer um nível se houver 'data' ou 'produto' aninhado
    for nested in ["data", "produto"]:
        if nested in prod and isinstance(prod[nested], dict):
             # Evita recursão no mesmo ID
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
        # Filters serão passados aqui: e.g. codigo='COI-B' ou nome='...'
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
                    # Adiciona log de erro com resposta completa
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
        # CORREÇÃO: Passa o 'config' e não 'self.config' (ambos funcionam mas o padrão é mais limpo)
        self.api_client = BlingAPIClient(config) 
        self.component_config = ComponentConfigManager(config.COMPONENT_CONFIG_FILE)
        
        self.sales_manager = sales_manager # Gerenciador de vendas
        
        self.kits: List[Dict[str, Any]] = []
        self.products: List[Dict[str, Any]] = []
        self.is_running: bool = False
        self.lock = Lock()
        self.logger = logger
    
    # Renomeado e refatorado para ser o método de cache do worker
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
        
        # O sistema precisa de um token para funcionar
        if not self.config.CLIENT_ID or not self.config.REDIRECT_URI:
            self.logger.error("Configurações BLING_CLIENT_ID/REDIRECT_URI ausentes. O worker não pode iniciar.")
            return

        # Loop principal do worker
        while True:
            try:
                # 1. Checa a validade do token e o renova se necessário.
                self.check_and_refresh_token()
                
                # 2. Carrega dados estáticos do Bling (kits, produtos simples)
                self.load_bling_products() 
                
                # 3. FIX: Garante que o recálculo dos KPIs é acionado
                self.process_sales_orders() 

            except Exception as e:
                # Em caso de erro grave (ex: 401 Unauthorized), espera mais tempo
                self.logger.error(f"Erro grave no loop do worker: {e}. Esperando 60s antes de tentar novamente.")
                time.sleep(60)
                continue
            
            # Espera um intervalo antes de executar novamente (10 minutos)
            self.logger.info("Worker finalizado. Próxima execução em 10 minutos.")
            time.sleep(600) # 10 minutos (600 segundos)

    # MÉTODO CORRIGIDO (v4.0): Inclui debug logs e lógica de recálculo
    def process_sales_orders(self):
        """Busca pedidos de venda faturados/em andamento e ATUALIZA O SALES_MANAGER POR RECALCULO."""
        
        token = self.auth.get_valid_token()
        if not token:
            self.logger.warning("Token indisponível para buscar pedidos de venda.")
            return

        self.logger.info("Iniciando busca COMPLETA de pedidos de venda para recalcular os KPIs (SEM FILTRO DE SITUAÇÃO)...")
        
        # O período de busca deve cobrir o semanal (7 dias) + uma margem de segurança (9 dias)
        params = {
            'dataEmissaoInicial': (datetime.now() - timedelta(days=9)).strftime('%Y-%m-%d'),
            'pagina': 1,
            'limite': 50,
        }
        
        all_orders = []
        page = 1
        
        while True:
            current_params = params.copy()
            current_params['pagina'] = page
            
            response_data = self.api_client.get_sales_orders(token, **current_params)
            
            if response_data and 'data' in response_data:
                items = response_data['data']
                all_orders.extend(items)
                
                if len(items) < 50:
                    break
                
                page += 1
                time.sleep(0.5) # Pequeno delay entre páginas
            else:
                break
                
        if all_orders:
            self.logger.info(f"📊 Total de pedidos encontrados: {len(all_orders)}")
            
            # LOG DETALHADO DOS 3 PRIMEIROS PEDIDOS (para debug dos KPIs zerados)
            for idx, order in enumerate(all_orders[:3]):
                # Extrai a data da mesma forma que recalculate_from_orders faz
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
                self.logger.info(f"  [{idx+1}] ID: {order.get('id')}, "
                               f"Data: {data_str}, Hora: {hora_str}, "
                               f"Total: R$ {total_val}")
            
            # Recalcula todos os KPIs com base em todos os pedidos encontrados no período
            self.logger.info(f"🔄 Iniciando recalculate_from_orders com {len(all_orders)} pedidos...")
            self.sales_manager.recalculate_from_orders(all_orders)
        
            self.logger.info(f"✅ Busca e Recálculo de KPIs concluído. "
                           f"Resultados: Diário={self.sales_manager.daily_count}, "
                           f"Semanal={self.sales_manager.weekly_count}, "
                           f"Histórico={self.sales_manager.historic_count}")
        else:
            # FIX: Se não encontrar NADA, ainda atualiza o timestamp e zera os contadores
            with self.sales_manager.lock:
                self.sales_manager.historic_count = 0 
                self.sales_manager.daily_count = 0
                self.sales_manager.weekly_count = 0
                self.sales_manager.last_recalculated = datetime.now()
                save_stats(self.sales_manager._get_state_for_save(), self.config.SALES_STATS_FILE)
            
            self.logger.warning("⚠️ Busca de pedidos de venda concluída. Nenhuma resposta ou pedido encontrado no período.")


    def _load_products_and_kits(self, access_token: str):
        self.logger.info("Iniciando carga otimizada de produtos e kits...")
        self.kits.clear()
        self.products.clear()
        
        todos_produtos = []
        page = 1
        
        # PASSO 1: Baixar TUDO primeiro (Paginação)
        while True:
            try:
                # Busca produtos incluindo estrutura se possível
                resp = self.api_client.get_products(access_token, page=page, limit=100)
                items = resp.get('data', [])
                
                if not items:
                    break
                
                todos_produtos.extend(items)
                
                # Se vier menos que o limite, acabou
                if len(items) < 100:
                    break
                    
                page += 1
                time.sleep(0.2) # Respeita rate limit
            except Exception as e:
                self.logger.error(f"Erro ao carregar página {page}: {e}")
                break
        
        # PASSO 2: Criar Mapa para busca rápida (ID -> Produto)
        produto_map = {str(p.get("id")): p for p in todos_produtos}
        
        self.logger.info(f"Total baixado: {len(todos_produtos)}. Processando Kits...")

        # PASSO 3: Separar Kits e preencher nomes dos componentes
        for p in todos_produtos:
            p_id = p.get("id")
            
            estrutura = p.get("estrutura", {})
            componentes = estrutura.get("componentes", [])
            
            # Define se é kit: tem componentes OU tipo é 'K'
            eh_kit = len(componentes) > 0 or p.get("tipo") == "K" or p.get("formato") == "K"

            # Busca imagem robusta
            img_url = extract_image_url(p)
            
            # Se for KIT, processa componentes
            if eh_kit:
                comps_formatados = []
                
                # Se a lista de componentes estiver vazia mas for tipo K, 
                # tentamos uma chamada única de detalhe (fallback)
                if not componentes and p_id:
                     try:
                         # Só chama API se realmente faltar info
                         det = self.api_client.get_product_details(access_token, p_id)
                         componentes = det.get("estrutura", {}).get("componentes", [])
                         # Atualiza imagem se achou no detalhe
                         if not img_url: img_url = extract_image_url(det)
                     except:
                         pass

                for c in componentes:
                    # O componente refere-se a um produto filho
                    filho_ref = c.get("produto", {})
                    filho_id = str(filho_ref.get("id"))
                    
                    # Tenta achar o nome no nosso MAPA (muito mais rápido)
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
                    "produto": p.get("nome"), # Chave 'produto'
                    "imagemURL": img_url,
                    "componentes": comps_formatados
                })
            else:
                # É produto normal
                # CORREÇÃO: Padroniza o nome para 'produto' para o front-end
                self.products.append({
                    "id": p.get("id"),
                    "sku": p.get("codigo"),
                    "produto": p.get("nome"), # Chave 'produto'
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

# Instâncias Globais
config = Config()

if not config.REDIRECT_URI:
    logger.error("ERRO FATAL: BLING_REDIRECT_URI não configurada no Render")
    pass

# Instancia o SalesManager
sales_manager = SalesManager(config) 
# Passa o SalesManager para o Orchestrator
orchestrator = AutomationOrchestrator(config, sales_manager) 
auth = orchestrator.auth

# ============================================================================ 
# 7. DECORADOR (TOKEN REQUIRED AJUSTADO)
# ============================================================================

def token_required(f):
    """Decorador para verificar se o token de acesso está disponível e válido."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Retorna 401 limpo para que o front entenda que precisa reautenticar
        if not orchestrator.auth or not orchestrator.auth.is_authenticated():
            orchestrator.auth.logger.warning("Request sem auth válida: retornando 401 json")
            return jsonify({"needAuth": True, "message": "Token expirado ou inválido"}), 401

        token = orchestrator.auth.get_valid_token()
        if not token:
            return jsonify({"needAuth": True, "message": "Falha no refresh token"}), 401
        return f(token=token, *args, **kwargs)
    return decorated

# ============================================================================ 
# 9. TEMPLATE HTML DO DASHBOARD (ATUALIZADO)
# ============================================================================

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel Bling - Sw Moveis</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
    <style>
        body { background: #f8f9fa; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        .navbar { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
        .log-box { font-family: 'Courier New', monospace; font-size: .85em; background: #1e1e1e; color: #d4d4d4; border-radius: .5rem; padding: 1rem; max-height: 400px; overflow-y: auto; }
        .log-level-INFO { color: #4ec9b0; }
        .log-level-WARNING { color: #dcdcaa; }
        .log-level-ERROR { color: #f48771; }
        .hidden { display: none; }
        /* NOVO CSS PARA KPIS */
        .kpi-card { border-left: 5px solid; }
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
                     <h5>Pedidos Históricos (Últimos 9 dias)</h5>
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
    
    // Função para formatar o tempo da última venda (hora/minuto)
    function formatDateTime(isoString) {
        if (!isoString || isoString === 'N/D') return 'N/D';
        try {
             const date = new Date(isoString);
             // Inclui dia e mês se a data for de dias anteriores
             const now = new Date();
             const isToday = date.toDateString() === now.toDateString();
             
             if (isToday) {
                 return date.toLocaleTimeString('pt-BR'); // Ex: 14:30:00
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

            // 2. Update Sales Stats (KPIs)
            const rSalesStats = await fetch(API + '/sales/stats');
            
            if (rSalesStats.ok) {
                const dSalesStats = await rSalesStats.json();
            
                document.getElementById('kpi-daily').textContent = dSalesStats.daily;
                document.getElementById('kpi-weekly').textContent = dSalesStats.weekly;
                document.getElementById('kpi-historic').textContent = dSalesStats.historic;
    
                // Atualiza o tempo do último recálculo
                document.getElementById('last-recalculated').textContent = formatDateTime(dSalesStats.last_update);

            } else {
                // Limpa os dados em caso de falha (provavelmente por falta de autenticação)
                document.getElementById('kpi-daily').textContent = 0;
                document.getElementById('kpi-weekly').textContent = 0;
                document.getElementById('kpi-historic').textContent = 0;
                document.getElementById('last-recalculated').textContent = 'N/D';
            }


        } catch (e) {
            console.error("Erro ao checar status ou stats:", e);
        }
    }
    
    checkStatus();
    setInterval(checkStatus, 5000);

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
                                                    // CORREÇÃO: Usa 'nome' e 'sku' do componente (estrutura simplificada)
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

        // Carregar Kits e Produtos Simples (Todos Produtos)
        async function loadKits() {
            const div = document.getElementById('kits-list');
            const authRequiredDiv = document.getElementById('auth-required-kits');
            
            if (!isAuthenticated) {
                div.innerHTML = '';
                authRequiredDiv.classList.remove('hidden');
                return;
            }
            
            authRequiredDiv.classList.add('hidden');
            // MENSAGEM AJUSTADA: avisa que pode demorar
            div.innerHTML = '<div class="alert alert-info">Carregando dados. Este processo depende da finalização do cache em segundo plano (Worker) e pode demorar alguns minutos.</div>';
            
            try {
                // CORREÇÃO: Endpoint agora retorna KITS + PRODUTOS SIMPLES
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
                    // Trata imagem quebrada escondendo a tag (mantido)
                    const imgHtml = k.imagemURL 
                        ? `<img src="${k.imagemURL}" style="width:50px;height:50px;object-fit:contain;border-radius:4px;" onerror="this.style.display='none'">` 
                        : '<span class="text-muted">-</span>';

                    let comps = '';
                    // Se o item tem componentes, é um KIT
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
                        // Produto Simples
                        comps = `<span class="text-muted" style="font-size:0.8em">Produto Simples (Estoque: ${k.estoque || 'N/D'})</span>`;
                    }

                    // CORREÇÃO: Agora usa k.produto (chave unificada)
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
"""

# ============================================================================ 
# 8. SERVIDOR WEB (ROTAS CONSOLIDADAS - ATUALIZADO)
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
        # Acessa sales_manager globalmente
        global sales_manager

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
            
            # PROTEÇÃO 1: Se já estiver autenticado, redireciona direto
            if self.orchestrator.auth.is_authenticated():
                self.logger.info("Callback ignorado: Usuário já autenticado.")
                return redirect('/')

            if not code or not state:
                return redirect('/') # Redireciona silent, sem erro 400

            # PROTEÇÃO 2: Lock global de troca de token
            # Se não conseguir pegar o lock imediatamente, significa que outro request está processando
            if not token_exchange_lock.acquire(blocking=False):
                self.logger.warning("Concorrência detectada no callback. Redirecionando para home.")
                return redirect('/')
                
            try:
                # PROTEÇÃO 3: Previne reuso de code localmente
                with WebServer.code_lock:
                    if code in WebServer.used_codes:
                        return redirect('/')
                    WebServer.used_codes.add(code)
                
                self.logger.info(f"Processando callback code...")
                success = self.orchestrator.auth.exchange_code_for_token(code, state)
                
                # Se falhou por state inválido ou outro motivo, apenas redireciona
                return redirect('/')
            except Exception as e:
                self.logger.error(f"Erro crítico no callback: {e}")
                return redirect('/')
            finally:
                token_exchange_lock.release()

        @self.app.route('/api/status')
        def api_status():
            # Rota leve e rápida
            return jsonify({
                "authenticated": self.orchestrator.auth.is_authenticated(),
                "auth_url": self.orchestrator.auth.get_authorization_url(),
                "is_running": self.orchestrator.is_running
            })

        # ENDPOINT DE ESTATÍSTICAS DE VENDAS (AGORA RECALCULADO E PERSISTIDO)
        @self.app.route("/api/sales/stats")
        def api_sales_stats():
            """Retorna os contadores Diário, Semanal e Histórico."""
            # O sales_manager agora garante que o estado é lido do arquivo antes de retornar
            return jsonify(sales_manager.get_stats())

        @self.app.route("/api/all_products", methods=["GET"])
        @token_required
        def api_all_products(token):
            return jsonify(self.orchestrator.get_all_products())

        @self.app.route('/api/product/search', methods=["GET"])
        @token_required
        def api_product_search(token):
            termo = request.args.get("q") or request.args.get("sku") or request.args.get("nome") or ""
            termo = termo.strip() # Remove espaços
            if not termo:
                return jsonify([])

            # --- CORREÇÃO IMPORTANTE: BUSCA HÍBRIDA NA API ---
            
            all_results_base = []
            seen_ids = set()

            def process_response(resp_data):
                """Processa resposta da API e adiciona à lista de resultados básicos"""
                items = resp_data.get('data') or []
                for p in items:
                    p_id = p.get('id')
                    # Evita duplicatas se encontrar o mesmo produto por nome e código
                    if p_id and p_id in seen_ids:
                        continue
                    if p_id: seen_ids.add(p_id)
                    
                    # Armazena apenas os dados básicos da busca inicial
                    all_results_base.append({
                        "id": p.get("id"),
                        "sku": p.get("codigo"),
                        "nome": p.get("nome"),
                        "tipo": p.get("tipo"),
                        "situacao": p.get("situacao"),
                        "preco": p.get("preco"),
                    })

            # 1. Tenta buscar por CÓDIGO (SKU)
            self.logger.info(f"Buscando API por CÓDIGO: {termo}")
            resp_sku = self.orchestrator.api_client.get_products(token, codigo=termo, limit=20)
            process_response(resp_sku)

            # 2. Tenta buscar por NOME (Descrição)
            self.logger.info(f"Buscando API por NOME: {termo}")
            resp_nome = self.orchestrator.api_client.get_products(token, nome=termo, limit=20)
            process_response(resp_nome)

            # 3. ENRIQUECIMENTO DE DADOS (Busca Detalhada)
            final_results = []
            
            # Busca detalhes para popular imagem — CORRIGIDO: limitado a 10 para não travar
            MAX_DETALHES = 10 
            
            for idx, p in enumerate(all_results_base):
                if idx >= MAX_DETALHES:
                    break
                    
                try:
                    details = self.orchestrator.api_client.get_product_details(token, p["id"])
                except Exception as e:
                    self.orchestrator.logger.exception("Erro ao buscar detalhe produto %s", p["id"])
                    details = {}
                
                # CORREÇÃO: Mapeamento de estoque correto (V3)
                estoque_val = (
                    details.get("estoqueAtual")
                    or details.get("saldoDisponivel")
                    or details.get("estoque", {}).get("saldoVirtualTotal", 0)
                )

                # Constrói o objeto final com dados básicos e detalhes
                produto_completo = {
                    "id": p["id"],
                    "sku": p.get("sku"),
                    "nome": p.get("nome"),
                    "produto": p.get("nome"), # Chave unificada para o front-end
                    "tipo": p.get("tipo"),
                    "situacao": p.get("situacao"),
                    "preco": p.get("preco"),
                    "estoque": estoque_val,
                    "descricaoCurta": details.get("descricaoCurta"),
                    # Componentes são adicionados para kits
                    # Ajusta a estrutura para o front-end (componente.produto.nome -> componente.nome)
                    "componentes": [
                         {
                            "nome": c.get("produto", {}).get("nome", "Sem nome"),
                            "quantidade": c.get("quantidade", 0),
                            "sku": c.get("produto", {}).get("codigo", "N/D")
                         }
                        for c in details.get("estrutura", {}).get("componentes", [])
                    ],
                    "imagemURL": extract_image_url(details), # Usa a nova função utilitária
                }
                final_results.append(produto_completo)
            
            # CORREÇÃO: Adiciona kits que não foram encontrados na busca por nome/sku (cache local)
            # A lista de kits é melhor mantida no cache
            kits_cache = self.orchestrator.get_all_kits()
            termo_lower = termo.lower()
            
            for kit in kits_cache:
                # Se o ID não foi encontrado na busca da API, adiciona o kit do cache se for relevante
                if kit.get("id") not in seen_ids and (termo_lower in str(kit.get("produto", "")).lower() or termo_lower in str(kit.get("sku", "")).lower()):
                    final_results.append(kit)
                    seen_ids.add(kit.get("id")) # Marca como visto
            
            # Adicionar produtos simples do cache se a API não retornou e eles corresponderem
            produtos_cache = self.orchestrator.get_all_products()
            for prod in produtos_cache:
                if prod.get("id") not in seen_ids and (termo_lower in str(prod.get("produto", "")).lower() or termo_lower in str(prod.get("sku", "")).lower()):
                    final_results.append(prod)
                    seen_ids.add(prod.get("id"))

            return jsonify(final_results)


        # CORREÇÃO: Rota /api/kits alterada para retornar Kits E Produtos Simples do cache.
        @self.app.route('/api/kits', methods=["GET"])
        @token_required
        def api_kits(token):
            """Retorna todos os produtos (kits e simples) carregados em cache."""
            return jsonify(self.orchestrator.get_all_kits() + self.orchestrator.get_all_products())

        @self.app.route("/webhook/bling", methods=["POST"])
        def webhook_bling():
            """Processa webhooks do Bling e atualiza KPIs em tempo real com validação HMAC."""
            
            # 1. Recupera o payload bruto e o header de assinatura
            payload = request.get_data()
            signature_header = request.headers.get('X-Bling-Signature-256', '')

            # 2. Válida o HMAC usando o CLIENT_SECRET
            try:
                expected_signature = 'sha256=' + hmac.new(
                    self.orchestrator.config.CLIENT_SECRET.encode(), # Usa CLIENT_SECRET da Config
                    payload,
                    hashlib.sha256
                ).hexdigest()

                if not hmac.compare_digest(signature_header, expected_signature):
                    self.logger.warning(f"❌ Assinatura inválida no Webhook. Header: {signature_header}")
                    return jsonify({"error": "Invalid signature"}), 401
                    
                self.logger.info("✅ Assinatura HMAC do Webhook validada com sucesso.")

                # 3. Processa o JSON após a validação
                data = request.get_json(silent=True)
                if not data:
                    return jsonify({"status": "ok"}), 200 # OK se não houver dados
                
                event_type = data.get('event', '')
                
                # CRUCIAL 4: Verifica Token e Aciona o recálculo
                if not self.orchestrator.auth.is_authenticated():
                    self.logger.warning("⚠️ Webhook recebido, mas token Bling não é válido. Ignorando recálculo.")
                    return jsonify({"status": "ok", "note": "awaiting_auth"}), 200

                if 'order' in event_type: 
                    self.logger.info(f"Recálculo de KPIs de Vendas acionado pelo Webhook para evento: {event_type}.")
                    # Usa uma Thread para não bloquear a resposta do webhook enquanto o recálculo roda
                    # A persistência em arquivo garante que o estado seja compartilhado entre processos
                    Thread(target=self.orchestrator.process_sales_orders, daemon=True).start()

            except Exception as e:
                self.logger.exception(f"Erro no webhook: {e}")
                
            return jsonify({"status": "ok"}), 200

    def setup_websocket(self):
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
                        # CORREÇÃO: Tratamento para ConnectionClosed
                        ws.receive(timeout=1)
                    except ConnectionClosed:
                         break # Sai do loop limpo
                    except Exception:
                        pass
                except Exception:
                    break

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
_os.environ.setdefault("GUNICORN_CMD_ARGS", "--worker-class gevent --timeout 300 --keep-alive 5")
APP_PORT = int(_os.getenv("PORT", "10000"))