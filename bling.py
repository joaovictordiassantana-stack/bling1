#!/usr/bin/env python3

from gevent import monkey
monkey.patch_all()   # torna as bibliotecas padrão cooperativas com gevent (requests, socket, threading...)
"""
bling.py - Sistema completo de automação Bling com design premium (CORRIGIDO v4.6)
Implementa OAuth 2.0, API robusta, gerenciamento de estoque/compras e dashboard web.
- CORREÇÃO CRÍTICA (v4.4): Implementação de WebSocket para notificação em TEMPO REAL de KPIs.
- FIX SINCRONIZAÇÃO (v4.4): get_stats() agora força a leitura do arquivo para sincronização multi-worker.
- FIX SPAM DE LOG (v4.5): Ajuste no _load_stats para evitar logs repetitivos de 'Nenhum KPI encontrado'.
- ALTERAÇÃO (v4.6): Bloco 'Pedidos Históricos' (Produtos/Vendas) alterado de 9 para 30 dias.
- FIX SINTAXE (v4.6): Corrige 'SyntaxError: unterminated string literal' na função extract_image_url.
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
        # NOTE: A aplicação vai falhar na inicialização se esta variável for crítica e não estiver definida,
        # mas a falha será tratada no WebServer.setup_routes.
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
    # A linha abaixo estava incompleta e causou o erro de SyntaxError na versão anterior.
    for list_key in ["midia", "imagens"]: 
        media_list = prod.get(list_key)
        # O Bling V3 costuma aninhar imagens em um dicionário 'imagem' dentro de uma lista de 'midia'.
        if isinstance(media_list, list):
            for item in media_list:
                if isinstance(item, dict) and 'imagem' in item and isinstance(item['imagem'], dict):
                    url = item['imagem'].get('url')
                    if url and isinstance(url, str) and url.startswith("http"):
                        return url
                elif isinstance(item, dict):
                    # Tenta a URL diretamente no item (caso não esteja aninhado)
                    url = item.get('url')
                    if url and isinstance(url, str) and url.startswith("http"):
                        return url
    
    # 3. Tenta campos aninhados (recursão, se for um produto complexo)
    if 'produto' in prod and isinstance(prod['produto'], dict):
        result = extract_image_url(prod['produto'], depth + 1)
        if result:
            return result
            
    return None

# ============================================================================ 
# 6. CLIENTE BLING API (REQUESTS)
# ============================================================================

class BlingAPIClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.logger = logger
        self.session.headers.update({'Accept': 'application/json'})

    def _request_with_retry(self, method: str, url: str, access_token: str, **kwargs) -> Optional[Dict[str, Any]]:
        headers = {'Authorization': f'Bearer {access_token}', 'Accept': 'application/json'}
        kwargs.setdefault('timeout', self.config.REQUEST_TIMEOUT)
        
        for attempt in range(self.config.MAX_RETRIES):
            try:
                response = self.session.request(method, url, headers=headers, **kwargs)
                
                if response.status_code == 200 or response.status_code == 201:
                    return response.json()
                elif response.status_code == 401:
                    raise BlingAuthError("Token expirado ou inválido.")
                elif response.status_code == 404:
                    self.logger.warning(f"Recurso não encontrado: {url}")
                    return None
                elif response.status_code == 429: # Rate limit
                    self.logger.warning(f"Rate limit. Tentando novamente em {self.config.BASE_DELAY * (2 ** attempt)}s...")
                    time.sleep(self.config.BASE_DELAY * (2 ** attempt))
                    continue
                else:
                    self.logger.error(f"Erro API ({url}): {response.status_code} - {response.text}")
                    return None

            except RequestException as e:
                self.logger.error(f"Erro de conexão com API Bling: {e}")
                if attempt < self.config.MAX_RETRIES - 1:
                    time.sleep(self.config.BASE_DELAY * (2 ** attempt))
                    continue
                else:
                    self.logger.error(f"Tentativas esgotadas para {url}.")
                    return None
            except BlingAuthError:
                return None # Deixa o worker cuidar da renovação
                
        return None

    def get_products(self, access_token: str, **params) -> Dict[str, Any]:
        """Busca produtos na API V3."""
        headers = {'Authorization': f'Bearer {access_token}', 'Accept': 'application/json'}
        url = f"{self.config.BLING_API_URL}/produtos"
        for attempt in range(self.config.MAX_RETRIES):
            try:
                response = self.session.get(url, headers=headers, params=params, timeout=self.config.REQUEST_TIMEOUT)
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429: # Rate limit
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
                elif response.status_code == 429: # Rate limit
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
                elif response.status_code == 429: # Rate limit
                    time.sleep(2)
                    continue
                else:
                    self.logger.warning(f"Erro API Pedidos de Venda: {response.status_code} - {response.text}")
            except Exception as e:
                self.logger.warning(f"Erro conexao API Pedidos de Venda: {e}")
                time.sleep(1)
        return {}


# ============================================================================ 
# 7. ORCHESTRATOR DE AUTOMAÇÃO (THREAD PRINCIPAL)
# ============================================================================

@dataclass
class AutomationOrchestrator:
    """Gerencia o cliente API, autenticação e a thread de processamento."""
    
    config: Config = field(default_factory=Config)
    auth: BlingAuth = field(init=False)
    api_client: BlingAPIClient = field(init=False)
    sales_manager: SalesManager = field(init=False)
    component_manager: ComponentConfigManager = field(init=False)
    recalculation_lock: Lock = field(default_factory=Lock)
    
    # Cache
    products: List[Dict[str, Any]] = field(default_factory=list)
    kits: List[Dict[str, Any]] = field(default_factory=list)
    
    def __post_init__(self):
        self.auth = BlingAuth(self.config)
        self.api_client = BlingAPIClient(self.config)
        # Deve ser inicializado após o self.config
        self.sales_manager = SalesManager(self.config) 
        self.component_manager = ComponentConfigManager(self.config.COMPONENT_CONFIG_FILE)
        self.logger = logger
        
    def load_data_worker(self):
        """Loop principal do worker de automação (executa a cada 10 min)."""
        while True:
            try:
                # 1. Checar/renovar token
                if not self.auth.get_valid_token():
                    self.logger.warning("Token inválido/expirado e falha na renovação. Não é possível rodar o worker.")
                    time.sleep(60)
                    continue
                
                # 2. Processar Pedidos de Venda (Recálculo de KPIs)
                self.process_sales_orders()
                
                # 3. Recarregar e Processar Produtos/Kits
                self.load_all_products_and_kits()
                
                # 4. Outras automações (ex: checar estoque mínimo, atualizar e-commerce, etc.)
                self.logger.info("Outras rotinas de automação executadas com sucesso.")
                
            except BlingAuthError:
                self.logger.error("Erro de autenticação. Pulando ciclo e tentando novamente.")
                time.sleep(60)
                continue
            except Exception as e:
                self.logger.exception(f"Erro crítico no worker: {e}. Pulando ciclo e tentando novamente.")
                time.sleep(60)
                continue
            
            self.logger.info("Worker finalizado. Próxima execução em 10 minutos.")
            time.sleep(600) # 10 minutos (600 segundos)

    # MÉTODO CORRIGIDO (v4.2): Adiciona debounce lock
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
            
            # >>> ALTERAÇÃO PARA 30 DIAS <<<
            self.logger.info("Iniciando busca COMPLETA de pedidos de venda para recalcular os KPIs (Últimos 30 dias)...")
            params = { 
                'dataEmissaoInicial': (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d'),
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
                        hora_str = 'N/A'
                    else:
                        data_str, hora_str = 'N/A', 'N/A'
                        
                    self.logger.debug(f"Amostra de Pedido {idx+1}: ID={order.get('id')}, Data={data_str} {hora_str}")
                    
                self.sales_manager.recalculate_from_orders(all_orders)
            else:
                self.logger.warning("Nenhum pedido encontrado no período (30 dias). KPIs mantidos em 0.")
                self.sales_manager.recalculate_from_orders([]) # Garante notificação e persistência de 0
                
        finally:
            self.recalculation_lock.release()


    def load_all_products_and_kits(self):
        """Busca todos os produtos e kits para cache."""
        token = self.auth.get_valid_token()
        if not token:
            self.logger.warning("Token indisponível para buscar produtos.")
            return
        
        self.logger.info("Iniciando cache de produtos e kits...")
        
        # 1. Busca todos os produtos (ativos)
        # Obs: É necessário buscar todos para montar o mapa de kits
        products_resp = get_bling_products_safe(self.api_client, access_token=token)
        
        if not products_resp.get("success"):
            self.logger.error("Falha ao buscar produtos para cache: %s", products_resp.get("error"))
            return
            
        all_products = products_resp["data"]
        product_map = {str(p.get("id")): p for p in all_products if p.get("id")}
        
        self.products = []
        self.kits = []
        
        self.logger.info(f"Total de {len(all_products)} produtos encontrados no Bling.")
        
        # 2. Processa e separa Produtos Simples e Kits
        for p in all_products:
            p_id = p.get("id")
            estrutura = p.get("estrutura", {})
            componentes = estrutura.get("componentes", [])
            
            # Verifica se é um kit por estrutura, tipo ou formato (V3 usa a propriedade 'estrutura')
            eh_kit = len(componentes) > 0 or p.get("tipo") == "K" or p.get("formato") == "K"
            
            img_url = extract_image_url(p)
            
            if eh_kit:
                comps_formatados = []
                # Se não tem componentes no retorno de lista, tenta buscar os detalhes do produto
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
                    produto_filho = product_map.get(filho_id)
                    
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

# ============================================================================ 
# 8. TEMPLATE HTML (DASHBOARD)
# ============================================================================

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bling Automação Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.2/css/all.min.css">
    <style>
        body { background-color: #f8f9fa; color: #343a40; }
        .navbar { background-color: #343a40 !important; }
        .nav-link { color: #f8f9fa !important; }
        .nav-link.active { color: #0d6efd !important; background-color: #495057 !important; }
        .card { border-radius: 12px; box-shadow: 0 4px 8px rgba(0,0,0,0.05); }
        h2 { border-bottom: 2px solid #0d6efd; padding-bottom: 10px; margin-bottom: 20px; color: #0d6efd; }
        .btn-outline-light { border-color: #f8f9fa; }
        /* Esconde tabs se não autenticado (JS remove a class) - debug */
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
                Último Recálculo de KPIs: <span id="last-recalculated">N/D</span>
            </small>
        </div>

        <div id="content-tabs" class="hidden">
            <ul class="nav nav-tabs" id="myTab" role="tablist">
                <li class="nav-item" role="presentation">
                    <button class="nav-link active" id="search-tab" data-bs-toggle="tab" data-bs-target="#search-pane" type="button" role="tab" aria-controls="search-pane" aria-selected="true">
                        <i class="fas fa-search"></i> Busca de Produtos
                    </button>
                </li>
                <li class="nav-item" role="presentation">
                    <button class="nav-link" id="kits-tab" data-bs-toggle="tab" data-bs-target="#kits-pane" type="button" role="tab" aria-controls="kits-pane" aria-selected="false">
                        <i class="fas fa-box"></i> Kits e Componentes
                    </button>
                </li>
                <li class="nav-item" role="presentation">
                    <button class="nav-link" id="logs-tab" data-bs-toggle="tab" data-bs-target="#logs-pane" type="button" role="tab" aria-controls="logs-pane" aria-selected="false">
                        <i class="fas fa-stream"></i> Logs (Live)
                    </button>
                </li>
            </ul>
            <div class="tab-content pt-3" id="myTabContent">
                <div class="tab-pane fade show active" id="search-pane" role="tabpanel" aria-labelledby="search-tab">
                    <div class="input-group mb-3">
                        <input type="text" class="form-control" placeholder="Buscar por SKU ou Nome do Produto/Kit" id="search-input">
                        <button class="btn btn-primary" type="button" onclick="searchProduct()">Buscar</button>
                    </div>
                    <div id="search-results">
                        <div class="alert alert-info">Digite um termo para começar a buscar.</div>
                    </div>
                </div>

                <div class="tab-pane fade" id="kits-pane" role="tabpanel" aria-labelledby="kits-tab">
                    <div id="kits-list">
                        <div class="alert alert-info">Carregando Kits...</div>
                    </div>
                </div>

                <div class="tab-pane fade" id="logs-pane" role="tabpanel" aria-labelledby="logs-tab">
                    <div id="logs-content" style="height: 400px; overflow-y: scroll; background-color: #212529; color: #fff; padding: 15px; border-radius: 8px; font-family: monospace; font-size: 0.85em;">
                        </div>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        const API = '/api';
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

        // Função auxiliar para formatar data/hora (para o KPI)
        function formatDateTime(isoString) {
            if (!isoString || isoString === 'N/D') return 'N/D';
            try {
                const date = new Date(isoString);
                // Formato dd/mm/yyyy hh:mm:ss
                return date.toLocaleDateString('pt-BR', {
                    year: 'numeric', month: '2-digit', day: '2-digit',
                    hour: '2-digit', minute: '2-digit', second: '2-digit'
                });
            } catch {
                return isoString; // Retorna o original se houver erro de parse
            }
        }
        
        // Função auxiliar para formatar logs
        function formatLog(log) {
            let color = 'white';
            if (log.level === 'WARNING') color = '#ffc107';
            if (log.level === 'ERROR' || log.level === 'CRITICAL') color = '#dc3545';
            if (log.level === 'INFO') color = '#198754';
            
            return `<div style="color:${color}">[${log.timestamp}] [${log.level}] ${log.message}</div>`;
        }
        
        // Função para atualizar os KPIs (chamada via polling E via WebSocket)
        function updateKpis(dSalesStats) {
            document.getElementById('kpi-daily').textContent = dSalesStats.daily;
            document.getElementById('kpi-weekly').textContent = dSalesStats.weekly;
            document.getElementById('kpi-historic').textContent = dSalesStats.historic;
            document.getElementById('last-recalculated').textContent = formatDateTime(dSalesStats.last_update);
        }
        
        // WebSocket para KPIs (Notificação em Tempo Real)
        const wsKpi = new WebSocket(`${proto}://${window.location.host}/ws/kpis`);
        wsKpi.onmessage = (e) => {
            const data = JSON.parse(e.data);
            if (data.daily !== undefined) {
                updateKpis(data);
            }
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

                // 2. Update Sales Stats (KPIs) via Polling (Mantido como fallback, mas o WS é principal)
                if (isAuthenticated) {
                    const rSalesStats = await fetch(API + '/sales/stats');
                    if (rSalesStats.ok) {
                        const dSalesStats = await rSalesStats.json();
                        updateKpis(dSalesStats); // Usa a função de atualização
                    } else {
                        document.getElementById('kpi-daily').textContent = 0;
                        document.getElementById('kpi-weekly').textContent = 0;
                        document.getElementById('kpi-historic').textContent = 0;
                    }
                    
                    // 3. Carrega logs iniciais
                    const rLogs = await fetch(API + '/logs');
                    if (rLogs.ok) {
                        const dLogs = await rLogs.json();
                        const box = document.getElementById('logs-content');
                        box.innerHTML = ''; // Limpa antes de carregar
                        dLogs.logs.forEach(l => box.innerHTML += formatLog(l));
                        box.scrollTop = box.scrollHeight;
                    }
                }
                
            } catch(e) {
                console.error("Erro no checkStatus:", e);
                const badge = document.getElementById('status-badge');
                badge.className = 'badge bg-danger me-2';
                badge.textContent = 'Erro';
            }
        }

        async function searchProduct() {
            if (!isAuthenticated) {
                document.getElementById('search-results').innerHTML = '<div class="alert alert-warning">Você precisa estar autenticado para realizar buscas.</div>';
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
                                    ${p.componentes && p.componentes.length > 0 ? `
                                        <div class="mt-2">
                                            <b>Componentes:</b><br>
                                            ${p.componentes.map(c => `${c.quantidade}x ${c.nome || 'Sem nome'} (SKU: ${c.sku || 'N/D'})` ).join("<br>")}
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
        }
        
        async function loadKits() {
            if (!isAuthenticated) {
                document.getElementById('kits-list').innerHTML = '<div class="alert alert-warning">Você precisa estar autenticado para ver a lista de Kits.</div>';
                return;
            }
            const div = document.getElementById('kits-list');
            div.innerHTML = 'Carregando Kits em cache...';
            try {
                const r = await fetch(`${API}/product/kits`);
                if (r.status === 401) {
                    div.innerHTML = '<div class="alert alert-warning">Sessão expirada. Autentique novamente.</div>';
                    checkStatus();
                    return;
                }
                const data = await r.json();
                if(!data.length) {
                    div.innerHTML = '<div class="alert alert-info">Nenhum Kit em cache.</div>';
                    return;
                }
                let html = '<div class="list-group">';
                data.forEach(k => {
                    html += `
                        <div class="list-group-item">
                            <div class="d-flex">
                                <img src="${k.imagemURL || ''}" style="width:60px;height:60px;object-fit:contain;margin-right:10px;border-radius:6px;background:#f1f1f1" onerror="this.style.display='none'">
                                <div class="flex-grow-1">
                                    <div class="d-flex w-100 justify-content-between">
                                        <h5 class="mb-1">${k.produto || 'Sem nome (Kit)'}</h5>
                                        <small>SKU: ${k.sku || 'N/D'}</small>
                                    </div>
                                    <div class="mt-2">
                                        <b>Componentes:</b><br>
                                        ${k.componentes.map(c => `${c.quantidade}x ${c.nome || 'Sem nome'} (SKU: ${c.sku || 'N/D'})` ).join("<br>")}
                                    </div>
                                </div>
                            </div>
                        </div>
                    `;
                });
                html += '</div>';
                div.innerHTML = html;

            } catch(e) {
                div.innerHTML = 'Falha ao carregar lista. Verifique os logs.';
            }
        }

        document.addEventListener('DOMContentLoaded', () => {
            checkStatus(); // Roda a primeira checagem de status e carrega KPIs/Logs iniciais
            // Associa a função loadKits ao evento de mostrar a aba de kits
            document.getElementById('kits-tab').addEventListener('shown.bs.tab', loadKits);
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
                    # Previne processamento duplicado (Webhooks ou recarga de página)
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
                "auth_url": self.orchestrator.auth.get_authorization_url()
            })

        @self.app.route('/api/sales/stats')
        def api_sales_stats():
            # A chamada ao get_stats já garante a leitura sincronizada do arquivo
            return jsonify(self.orchestrator.sales_manager.get_stats())
        
        @self.app.route('/api/product/search')
        def api_product_search():
            if not self.orchestrator.auth.is_authenticated():
                return jsonify({"error": "Unauthorized"}), 401
                
            termo = request.args.get('q', '').strip()
            if len(termo) < 3:
                return jsonify([])

            final_results = []
            seen_ids = set()
            
            produtos_cache = self.orchestrator.get_all_products()
            kits_cache = self.orchestrator.get_all_kits()
            
            termo_lower = termo.lower()
            
            # 1. Busca nos produtos simples em cache
            for p in produtos_cache:
                if p.get("id") not in seen_ids and (termo_lower in str(p.get("produto", "")).lower() or termo_lower in str(p.get("sku", "")).lower()):
                    final_results.append(p)
                    seen_ids.add(p.get("id"))
            
            # 2. Busca nos kits em cache
            for kit in kits_cache:
                if kit.get("id") not in seen_ids and (termo_lower in str(kit.get("produto", "")).lower() or termo_lower in str(kit.get("sku", "")).lower()):
                    final_results.append(kit)
                    seen_ids.add(kit.get("id"))
                    
            # 3. Busca em tempo real na API (para buscar algo que possa ter ficado fora do cache, mas sem detalhes)
            token = self.orchestrator.auth.get_valid_token()
            if token and len(final_results) < 5:
                self.logger.info(f"Buscando termo '{termo}' diretamente na API (Produtos e Kits)...")
                api_resp = get_bling_products_safe(self.orchestrator.api_client, nome=termo, access_token=token)
                
                if api_resp.get("success"):
                    for p in api_resp["data"]:
                        p_id = p.get("id")
                        if p_id and p_id not in seen_ids:
                            # Tenta pegar detalhes para estoque/componentes se não for um kit simples
                            details = self.orchestrator.api_client.get_product_details(token, p_id) or {}
                            
                            estoque_val = ( 
                                p.get("estoqueAtual", 0) 
                                or details.get("estoque", {}).get("saldoVirtualTotal", 0) 
                            )

                            produto_completo = {
                                "id": p_id,
                                "sku": p.get("codigo"),
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
                                "imagemURL": extract_image_url(details) or extract_image_url(p),
                            }
                            final_results.append(produto_completo)
                            seen_ids.add(p_id)
            
            # Limita o resultado final para evitar sobrecarga (max 15)
            return jsonify(final_results[:15])

        @self.app.route('/api/product/kits')
        def api_product_kits():
            if not self.orchestrator.auth.is_authenticated():
                return jsonify({"error": "Unauthorized"}), 401
            
            # Retorna apenas os kits que estão em cache
            return jsonify(self.orchestrator.get_all_kits())

        @self.app.route('/api/logs')
        def api_logs():
            """Retorna os logs em memória (para a carga inicial)."""
            global memory_handler
            return jsonify({"logs": memory_handler.get_logs(limit=200)})

    def setup_websocket(self):
        global memory_handler, kpi_update_callbacks, kpi_update_lock
        
        @self.sock.route('/ws/logs')
        def logs_consumer(ws):
            """WebSocket para logs em tempo real."""
            last_idx = 0
            self.logger.info("Novo WebSocket de log conectado.")
            
            # Envia logs antigos imediatamente
            all_logs = memory_handler.get_logs(limit=200)
            if all_logs:
                 ws.send(json.dumps({"logs": all_logs}))
                 last_idx = len(all_logs)
                 
            while True:
                try:
                    time.sleep(1) # Polling a cada 1 segundo
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
            self.logger.info("WebSocket de log desconectado.")
            
        @self.sock.route('/ws/kpis')
        def kpis_consumer(ws):
            """WebSocket para notificação de KPI em Tempo Real."""
            
            def notify_kpi(stats_data):
                """Função que será chamada pelo SalesManager."""
                try:
                    ws.send(json.dumps(stats_data))
                except ConnectionClosed:
                    # A conexão será fechada e tratada no finally
                    pass
                except Exception as e:
                    self.logger.error(f"Erro ao enviar KPI via WS: {e}")

            # 1. Registra o callback
            with kpi_update_lock:
                kpi_update_callbacks.append(notify_kpi)
            
            self.logger.info("WebSocket KPI conectado e registrado.")
            
            # 2. Loop de keepalive (mantém a conexão aberta)
            try:
                while True:
                    try:
                        ws.receive(timeout=5) # Keepalive
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

def create_app() -> Flask:
    app = Flask(__name__)
    # A instância do orchestrator deve ser global para ser acessível pelo Gunicorn/Flask
    global orchestrator
    WebServer(app, orchestrator)
    return app

# Cria a instância global do orchestrator
orchestrator = AutomationOrchestrator()
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
_os.environ.setdefault("GUNICORN_CMD_ARGS", "--bind 0.0.0.0:8000 --workers 4 --threads 2 --worker-class gevent --timeout 300 --graceful-timeout 300")