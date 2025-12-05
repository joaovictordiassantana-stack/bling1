#!/usr/bin/env python3
"""
bling.py - Sistema completo de automação Bling com design premium
Implementa OAuth 2.0, API robusta, gerenciamento de estoque/compras e dashboard web.
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

from pathlib import Path
from datetime import datetime, timedelta
from threading import Lock, Thread
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field

import requests
from requests.exceptions import RequestException
from flask import Flask, request, render_template_string, jsonify, redirect, url_for
from flask_sock import Sock

# ============================================================================
# 0. FUNÇÕES DE PERSISTÊNCIA DE TOKENS (RE-ADICIONADAS)
# ============================================================================

def load_tokens():
    if not os.path.exists("tokens.json"):
        return None
    try:
        with open("tokens.json", "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception as e:
        print(f"Erro ao carregar tokens: {e}")
        return None


def save_tokens(data):
    try:
        with open("tokens.json", "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4, ensure_ascii=False)
        print("INFO: Tokens salvos com sucesso.")
    except Exception as e:
        print(f"Erro ao salvar tokens: {e}")

def is_token_valid(token_data):
    if not token_data:
        return False
    expires_at = token_data.get("expires_at")
    if not expires_at:
        return False
    # Subtrai 20 segundos para garantir que o token não expire durante a requisição
    return time.time() < float(expires_at) - 20

def refresh_access_token():
    token_data = load_tokens()
    if not token_data or "refresh_token" not in token_data:
        print("ERRO: refresh_token não encontrado.")
        return None

    refresh_token = token_data["refresh_token"]

    client_id = Config.CLIENT_ID
    client_secret = Config.CLIENT_SECRET
        
    url = "https://www.bling.com.br/Api/v3/oauth/token"
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret
    }

    try:
        response = requests.post(url, data=payload)
        new_data = response.json()

        new_data["expires_at"] = time.time() + new_data.get("expires_in", 3600)

        save_tokens(new_data)

        print("Token renovado com sucesso.")
        return new_data

    except Exception as e:
        print("Erro ao renovar token:", e)
        return None


# ============================================================================
# 16. EXCEÇÕES CUSTOMIZADAS
# ============================================================================

class BlingAuthError(Exception):
    """Erro relacionado à autenticação OAuth do Bling."""
    pass

class BlingAPIError(Exception):
    """Erro geral na comunicação com a API do Bling."""
    pass

# ============================================================================
# 19. CONFIGURAÇÕES
# ============================================================================

class Config:
    """Configurações globais da aplicação."""
    
    # Bling OAuth
    CLIENT_ID: str = os.environ.get('BLING_CLIENT_ID', 'YOUR_CLIENT_ID')
    CLIENT_SECRET: str = os.environ.get('BLING_CLIENT_SECRET', 'YOUR_CLIENT_SECRET')
    REDIRECT_URI: str = os.environ.get('BLING_REDIRECT_URI', 'http://localhost:8000/callback')
    
    @staticmethod
    def validate_credentials():
        if Config.CLIENT_ID == 'YOUR_CLIENT_ID' or Config.CLIENT_SECRET == 'YOUR_CLIENT_SECRET':
            raise ValueError("As credenciais BLING_CLIENT_ID e BLING_CLIENT_SECRET devem ser configuradas. Verifique as variáveis de ambiente ou a classe Config.")
    
    # API
    BLING_API_URL: str = 'https://www.bling.com.br/Api/v3'
    TOKEN_URL: str = 'https://www.bling.com.br/Api/v3/oauth/token'
    
    # Retry e Timeout
    REQUEST_TIMEOUT: int = 30
    MAX_RETRIES: int = 3 # Reduzido de 5 para 3, conforme instruído.
    BASE_DELAY: float = 1.0 # Delay inicial para backoff exponencial
    
    # Automação
    CHECK_MIN_STOCK: bool = True
    MIN_STOCK_THRESHOLD: int = 10 # Estoque mínimo padrão se não configurado
    DEFAULT_BATCH_SIZE: int = 10
    DELAY_BETWEEN_BATCHES: float = 0.5 # Delay entre chamadas de API em lote
    
    # Arquivos
    TOKENS_FILE: Path = Path('tokens.json')
    COMPONENT_CONFIG_FILE: Path = Path('component_config.json')
    LOGS_DIR: Path = Path('logs')
    LOG_FILE: Path = LOGS_DIR / 'automacao_bling.log'
    ERROR_LOG_FILE: Path = LOGS_DIR / 'errors.log'

# ============================================================================
# 2. DATACLASSES E ESTRUTURAS
# ============================================================================

@dataclass
class Component:
    """Representa um componente (produto) no Bling."""
    sku: str
    name: str
    qty: int # Quantidade necessária para o Kit
    supplier: str = 'N/A'
    lead_time_days: int = 0
    unit_cost: float = 0.0
    min_stock: int = Config.MIN_STOCK_THRESHOLD
    current_stock: int = 0
    
    def __post_init__(self):
        # Garante que min_stock seja pelo menos 0
        self.min_stock = max(0, self.min_stock)

@dataclass
class Kit:
    """Representa um Kit (produto composto) no Bling."""
    sku: str
    name: str
    components: List[Component] = field(default_factory=list)
    price: float = 0.0

@dataclass
class PurchaseNeed:
    """Representa uma necessidade de compra de um componente."""
    component_sku: str
    component_name: str
    quantity_needed: int
    supplier: str
    lead_time_days: int
    reason: str

# ============================================================================
# 9. LOGS AVANÇADOS
# ============================================================================

class InMemoryLogHandler(logging.Handler):
    """Handler de log que armazena os registros em memória."""
    def __init__(self, max_logs=500):
        super().__init__()
        self.logs = []
        self.max_logs = max_logs

        self.formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%dT%H:%M:%S'
        )
        
    def emit(self, record):

        log_entry = {
        'timestamp': self.formatter.formatTime(record),
        'level': record.levelname,
        'message': self.format(record),
        'name': record.name
        }
        self.logs.append(log_entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)
    
    def get_logs(self, limit: Optional[int] = None) -> List[Dict[str, str]]:
        """Retorna os logs armazenados, limitados pelo parâmetro."""

        if limit:
            return self.logs[-limit:]
        return self.logs.copy()

# Configuração inicial de logs
def setup_logging():
    """Configura o sistema de logging com handlers de arquivo e memória."""
    Config.LOGS_DIR.mkdir(exist_ok=True)
    
    global memory_handler
    memory_handler = InMemoryLogHandler()
    
    # Logger principal
    logger = logging.getLogger('bling_automacao')
    logger.setLevel(logging.INFO)
    
    # Handler de arquivo principal
    file_handler = logging.handlers.RotatingFileHandler(
        Config.LOG_FILE, maxBytes=1024*1024*5, backupCount=5, encoding='utf-8'
    )
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-dT%H:%M:%S'
    ))
    
    # Handler de erro separado
    error_logger = logging.getLogger('error_logger')
    error_logger.setLevel(logging.ERROR)
    error_file_handler = logging.handlers.RotatingFileHandler(
        Config.ERROR_LOG_FILE, maxBytes=1024*1024*5, backupCount=5, encoding='utf-8'
    )
    error_file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-dT%H:%M:%S'
    ))
    error_logger.addHandler(error_file_handler)
    
    # Adiciona handlers ao logger principal
    logger.addHandler(file_handler)
    logger.addHandler(memory_handler)
    
    # Adiciona handler de console para CLI
    if not os.environ.get('FLASK_ENV'): # Não adiciona console handler se estiver rodando em ambiente Flask/WSGI
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
        logger.addHandler(console_handler)
        
    return logger, error_logger

logger, error_logger = setup_logging()

# ============================================================================
# 3. CONFIGURAÇÃO DE COMPONENTES
# ============================================================================

class ComponentConfigManager:
    """Gerencia as configurações locais de componentes (min_stock, fornecedor, etc)."""
    
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.config: Dict[str, Any] = self._load_or_create_config()
        self.defaults: Dict[str, Any] = self.config.get('component_defaults', {})
        self.components_map: Dict[str, Dict[str, Any]] = {
            c['sku']: c for c in self.config.get('components', [])
        }
        
    def _load_or_create_config(self) -> Dict[str, Any]:
        """Carrega a configuração do arquivo ou cria um novo com valores padrão."""
        if self.file_path.exists():
            try:
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Erro ao carregar {self.file_path}: {e}. Criando arquivo padrão.")
                error_logger.error(f"Erro ao carregar {self.file_path}: {e}")
        
        default_config = {
            "component_defaults": {
                "supplier": "Fornecedor Padrão",
                "lead_time_days": 7,
                "min_stock": Config.MIN_STOCK_THRESHOLD
            },
            "components": []
        }
        
        self._save_config(default_config)
        return default_config
    
    def _save_config(self, config: Dict[str, Any]):
        """Salva a configuração no arquivo."""
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
        except IOError as e:
            logger.error(f"Erro ao salvar {self.file_path}: {e}")
            error_logger.error(f"Erro ao salvar {self.file_path}: {e}")
    
    def get_component_config(self, sku: str) -> Dict[str, Any]:
        """Retorna a configuração de um componente específico ou os valores padrão."""
        return self.components_map.get(sku, self.defaults.copy())
    
    def update_component_config(self, sku: str, config: Dict[str, Any]):
        """Atualiza a configuração de um componente específico."""
        # Atualiza o mapa em memória
        if sku in self.components_map:
            self.components_map[sku].update(config)
        else:
            new_config = self.defaults.copy()
            new_config.update(config)
            new_config['sku'] = sku
            self.components_map[sku] = new_config
        
        # Reconstrói a lista de componentes no config
        self.config['components'] = list(self.components_map.values())
        
        # Salva no arquivo
        self._save_config(self.config)
        logger.info(f"Configuração do componente {sku} atualizada.")

# ============================================================================
# 4. GERENCIAMENTO DE NECESSIDADES DE COMPRA
# ============================================================================

class PurchaseNeedsManager:
    """Gerencia as necessidades de compra identificadas."""
    
    def __init__(self):
        self.needs: Dict[str, List[PurchaseNeed]] = {}  # supplier -> [needs]
        self.lock = Lock()
    
    def add_need(self, need: PurchaseNeed):
        """Adiciona uma necessidade de compra."""
        with self.lock:
            if need.supplier not in self.needs:
                self.needs[need.supplier] = []
            
            # Verifica se já existe uma necessidade para o mesmo componente do mesmo fornecedor
            existing = next((n for n in self.needs[need.supplier] if n.component_sku == need.component_sku), None)
            if existing:
                # Atualiza a quantidade se for maior
                if need.quantity_needed > existing.quantity_needed:
                    existing.quantity_needed = need.quantity_needed
                    existing.reason = need.reason
                    logger.info(f"Necessidade atualizada: {need.component_sku} - {need.quantity_needed} unidades")
            else:
                self.needs[need.supplier].append(need)
                logger.info(f"Nova necessidade adicionada: {need.component_sku} - {need.quantity_needed} unidades")
    
    def clear_needs(self):
        """Limpa todas as necessidades."""
        with self.lock:
            self.needs.clear()
            logger.info("Todas as necessidades de compra foram limpas.")
    
    def get_needs_by_supplier(self, supplier: str) -> List[PurchaseNeed]:
        """Retorna as necessidades de um fornecedor específico."""
        with self.lock:
            return self.needs.get(supplier, []).copy()
    
    def get_all_needs(self) -> Dict[str, List[PurchaseNeed]]:
        """Retorna todas as necessidades organizadas por fornecedor."""
        with self.lock:
            return {supplier: needs.copy() for supplier, needs in self.needs.items()}

# ============================================================================
# 5. ESTATÍSTICAS E MÉTRICAS
# ============================================================================

@dataclass
class ProcessingStats:
    """Estatísticas de processamento."""
    success: int = 0
    failed: int = 0
    ops_created: int = 0
    pos_created: int = 0
    stock_checks: int = 0
    elapsed_time_seconds: float = 0.0
    
    def reset(self):
        """Reseta todas as estatísticas."""
        self.success = 0
        self.failed = 0
        self.ops_created = 0
        self.pos_created = 0
        self.stock_checks = 0
        self.elapsed_time_seconds = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Converte para dicionário."""
        return {
            'success': self.success,
            'failed': self.failed,
            'ops_created': self.ops_created,
            'pos_created': self.pos_created,
            'stock_checks': self.stock_checks,
            'elapsed_time_seconds': round(self.elapsed_time_seconds, 2)
        }

# ============================================================================
# 6. CLIENTE BLING API
# ============================================================================

class BlingAPIClient:
    """Cliente para comunicação com a API do Bling."""
    
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.timeout = config.REQUEST_TIMEOUT
        
    def _get_headers(self, access_token: str) -> Dict[str, str]:
        """Retorna os headers padrão para requisições autenticadas."""
        return {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
    
    def _make_request(self, method: str, endpoint: str, access_token: str, **kwargs) -> requests.Response:
        """Faz uma requisição HTTP com retry automático."""
        url = f"{self.config.BLING_API_URL}/{endpoint.lstrip('/')}"
        headers = self._get_headers(access_token)
        
        for attempt in range(self.config.MAX_RETRIES):
            try:
                response = self.session.request(method, url, headers=headers, **kwargs)
                
                # Se a resposta for bem-sucedida, retorna
                if response.status_code < 500:
                    return response
                    
                # Para erros 5xx, tenta novamente
                logger.warning(f"Erro {response.status_code} na tentativa {attempt + 1}/{self.config.MAX_RETRIES}: {url}")
                
            except RequestException as e:
                logger.warning(f"Erro de conexão na tentativa {attempt + 1}/{self.config.MAX_RETRIES}: {e}")
                
            # Backoff exponencial
            if attempt < self.config.MAX_RETRIES - 1:
                delay = self.config.BASE_DELAY * (2 ** attempt)
                time.sleep(delay)
        
        # Se chegou aqui, todas as tentativas falharam
        raise BlingAPIError(f"Falha após {self.config.MAX_RETRIES} tentativas para {method} {url}")
    
    def get_products(self, access_token: str, page: int = 1, limit: int = 100, **filters) -> Dict[str, Any]:
        """Busca produtos com paginação e filtros."""
        params = {
            'pagina': page,
            'limite': limit,
            **filters
        }
        
        response = self._make_request('GET', '/produtos', access_token, params=params)
        
        if response.status_code == 200:
            return response.json()
        else:
            raise BlingAPIError(f"Erro ao buscar produtos: {response.status_code} - {response.text}")
    
    def get_product_by_sku(self, access_token: str, sku: str) -> Optional[Dict[str, Any]]:
        """Busca um produto específico pelo SKU."""
        try:
            response = self._make_request('GET', f'/produtos', access_token, params={'codigo': sku})
            
            if response.status_code == 200:
                data = response.json()
                products = data.get('data', [])
                return products[0] if products else None
            else:
                logger.error(f"Erro ao buscar produto {sku}: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Erro ao buscar produto {sku}: {e}")
            return None
    
    def get_stock(self, access_token: str, product_id: int) -> Optional[Dict[str, Any]]:
        """Busca informações de estoque de um produto."""
        try:
            response = self._make_request('GET', f'/estoques/{product_id}', access_token)
            
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Erro ao buscar estoque do produto {product_id}: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Erro ao buscar estoque do produto {product_id}: {e}")
            return None
    
    def create_production_order(self, access_token: str, order_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Cria uma ordem de produção."""
        try:
            response = self._make_request('POST', '/ordens-producao', access_token, json=order_data)
            
            if response.status_code in [200, 201]:
                return response.json()
            else:
                logger.error(f"Erro ao criar OP: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Erro ao criar OP: {e}")
            return None
    
    def create_purchase_order(self, access_token: str, order_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Cria uma ordem de compra."""
        try:
            response = self._make_request('POST', '/pedidos-compras', access_token, json=order_data)
            
            if response.status_code in [200, 201]:
                return response.json()
            else:
                logger.error(f"Erro ao criar PO: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Erro ao criar PO: {e}")
            return None

# ============================================================================
# 7. AUTENTICAÇÃO OAUTH
# ============================================================================

class BlingAuth:
    """Gerencia a autenticação OAuth 2.0 com o Bling."""
    
    def __init__(self, config: Config):
        self.config = config
        self.client_id = config.CLIENT_ID
        self.client_secret = config.CLIENT_SECRET
        self.redirect_uri = config.REDIRECT_URI
        self.auth_url_base = 'https://www.bling.com.br/Api/v3/oauth/authorize'
        
        # Estado da autenticação
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.expires_at: Optional[float] = None
        self.state: Optional[str] = None # Adicionado para o parâmetro state do OAuth
        
    def get_authorization_url(self) -> str:
        """Retorna a URL de autorização OAuth, gerando e armazenando o parâmetro state."""
        self.state = secrets.token_urlsafe(16) # Gera um state seguro
        return f"{self.auth_url_base}?client_id={self.client_id}&redirect_uri={self.redirect_uri}&response_type=code&state={self.state}"
    
    def exchange_code_for_token(self, code: str) -> bool:
        """Troca o código de autorização por tokens de acesso."""
        try:
            payload = {
                'grant_type': 'authorization_code',
                'code': code,
                'client_id': self.client_id,
                'client_secret': self.client_secret,
                'redirect_uri': self.redirect_uri
            }
            
            response = requests.post(self.config.TOKEN_URL, data=payload, timeout=self.config.REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                token_data = response.json()
                
                self.access_token = token_data.get('access_token')
                self.refresh_token = token_data.get('refresh_token')
                self.expires_at = time.time() + token_data.get('expires_in', 3600)
                
                # Salva os tokens
                save_data = {
                    'access_token': self.access_token,
                    'refresh_token': self.refresh_token,
                    'expires_at': self.expires_at
                }
                save_tokens(save_data)
                
                logger.info("Tokens obtidos e salvos com sucesso.")
                return True
            else:
                logger.error(f"Erro ao trocar código por token: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"Erro na troca de código por token: {e}")
            error_logger.error(f"Erro na troca de código por token: {e}")
            return False
    
    def load_tokens(self) -> bool:
        """Carrega tokens salvos do arquivo."""
        token_data = load_tokens()
        if token_data and is_token_valid(token_data):
            self.access_token = token_data.get('access_token')
            self.refresh_token = token_data.get('refresh_token')
            self.expires_at = token_data.get('expires_at')
            logger.info("Tokens carregados com sucesso.")
            return True
        elif token_data and token_data.get('refresh_token'):
            # Tenta renovar o token
            return self.refresh_access_token()
        else:
            logger.warning("Nenhum token válido encontrado.")
            return False
    
    def refresh_access_token(self) -> bool:
        """Renova o token de acesso usando o refresh token."""
        if not self.refresh_token:
            logger.error("Refresh token não disponível.")
            return False
        
        try:
            payload = {
                'grant_type': 'refresh_token',
                'refresh_token': self.refresh_token,
                'client_id': self.client_id,
                'client_secret': self.client_secret
            }
            
            response = requests.post(self.config.TOKEN_URL, data=payload, timeout=self.config.REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                token_data = response.json()
                
                self.access_token = token_data.get('access_token')
                # O refresh token pode ou não ser renovado
                if 'refresh_token' in token_data:
                    self.refresh_token = token_data.get('refresh_token')
                self.expires_at = time.time() + token_data.get('expires_in', 3600)
                
                # Salva os tokens atualizados
                save_data = {
                    'access_token': self.access_token,
                    'refresh_token': self.refresh_token,
                    'expires_at': self.expires_at
                }
                save_tokens(save_data)
                
                logger.info("Token renovado com sucesso.")
                return True
            else:
                logger.error(f"Erro ao renovar token: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"Erro na renovação do token: {e}")
            error_logger.error(f"Erro na renovação do token: {e}")
            return False
    
    def is_authenticated(self) -> bool:
        """Verifica se está autenticado com token válido."""
        if not self.access_token or not self.expires_at:
            return False
        
        # Verifica se o token ainda é válido (com margem de 60 segundos)
        return time.time() < (self.expires_at - 60)
    
    def get_valid_token(self) -> Optional[str]:
        """Retorna um token válido, renovando se necessário."""
        if self.is_authenticated():
            return self.access_token
        
        # Tenta renovar o token
        if self.refresh_access_token():
            return self.access_token
        
        return None

# ============================================================================
# 8. ORQUESTRADOR PRINCIPAL
# ============================================================================

class AutomationOrchestrator:
    """Orquestrador principal que coordena todas as operações."""
    
    def __init__(self, config: Config):
        self.config = config
        self.auth = BlingAuth(config)
        self.api_client = BlingAPIClient(config)
        self.component_config = ComponentConfigManager(config.COMPONENT_CONFIG_FILE)
        self.needs_manager = PurchaseNeedsManager()
        self.stats = ProcessingStats()
        
        # Estado
        self.kits: List[Kit] = []
        self.is_running: bool = False
        self.lock = Lock()
    
    def load_data(self) -> bool:
        """Carrega dados iniciais (kits, componentes, estoque)."""
        logger.info("Iniciando carregamento de dados...")
        
        # Verifica autenticação
        token = self.auth.get_valid_token()
        if not token:
            logger.error("Token de acesso não disponível. Execute a autenticação primeiro.")
            return False
        
        try:
            # Carrega produtos que são kits (produtos compostos)
            self._load_kits(token)
            
            # Atualiza estoque dos componentes
            self._update_component_stock(token)
            
            logger.info(f"Carregamento concluído: {len(self.kits)} kits carregados.")
            return True
            
        except Exception as e:
            logger.error(f"Erro no carregamento de dados: {e}")
            error_logger.error(f"Erro no carregamento de dados: {e}")
            return False
    
    def _load_kits(self, access_token: str):
        """Carrega kits (produtos compostos) do Bling."""
        logger.info("Carregando kits...")
        
        page = 1
        kits_loaded = 0
        
        while True:
            try:
                # Busca produtos com filtro para produtos compostos
                response = self.api_client.get_products(
                    access_token, 
                    page=page, 
                    limit=100,
                    tipo='P'  # Tipo P = Produto (pode incluir compostos)
                )
                
                products = response.get('data', [])
                if not products:
                    break
                
                for product in products:
                    # Verifica se é um produto composto (tem estrutura)
                    if self._is_kit_product(product):
                        kit = self._create_kit_from_product(product)
                        if kit:
                            self.kits.append(kit)
                            kits_loaded += 1
                
                page += 1
                
                # Delay entre páginas para não sobrecarregar a API
                time.sleep(self.config.DELAY_BETWEEN_BATCHES)
                
            except Exception as e:
                logger.error(f"Erro ao carregar página {page} de kits: {e}")
                break
        
        logger.info(f"{kits_loaded} kits carregados.")
    
    def _is_kit_product(self, product: Dict[str, Any]) -> bool:
        """Verifica se um produto é um kit (produto composto)."""
        # Implementação simplificada - pode ser refinada conforme a estrutura real da API
        estrutura = product.get('estrutura', {})
        componentes = estrutura.get('componentes', [])
        return len(componentes) > 0
    
    def _create_kit_from_product(self, product: Dict[str, Any]) -> Optional[Kit]:
        """Cria um objeto Kit a partir de um produto do Bling."""
        try:
            sku = product.get('codigo', '')
            name = product.get('nome', '')
            price = float(product.get('preco', 0))
            
            if not sku:
                return None
            
            kit = Kit(sku=sku, name=name, price=price)
            
            # Processa componentes
            estrutura = product.get('estrutura', {})
            componentes = estrutura.get('componentes', [])
            
            for comp_data in componentes:
                component = self._create_component_from_data(comp_data)
                if component:
                    kit.components.append(component)
            
            return kit
            
        except Exception as e:
            logger.error(f"Erro ao criar kit do produto {product.get('codigo', 'N/A')}: {e}")
            return None
    
    def _create_component_from_data(self, comp_data: Dict[str, Any]) -> Optional[Component]:
        """Cria um objeto Component a partir dos dados do Bling."""
        try:
            sku = comp_data.get('codigo', '')
            name = comp_data.get('nome', '')
            qty = int(comp_data.get('quantidade', 1))
            
            if not sku:
                return None
            
            # Busca configurações locais do componente
            config = self.component_config.get_component_config(sku)
            
            component = Component(
                sku=sku,
                name=name,
                qty=qty,
                supplier=config.get('supplier', 'N/A'),
                lead_time_days=config.get('lead_time_days', 0),
                unit_cost=config.get('unit_cost', 0.0),
                min_stock=config.get('min_stock', self.config.MIN_STOCK_THRESHOLD)
            )
            
            return component
            
        except Exception as e:
            logger.error(f"Erro ao criar componente: {e}")
            return None
    
    def _update_component_stock(self, access_token: str):
        """Atualiza o estoque atual de todos os componentes."""
        logger.info("Atualizando estoque dos componentes...")
        
        # Coleta todos os SKUs únicos de componentes
        component_skus = set()
        for kit in self.kits:
            for component in kit.components:
                component_skus.add(component.sku)
        
        updated_count = 0
        
        for sku in component_skus:
            try:
                # Busca o produto pelo SKU
                product = self.api_client.get_product_by_sku(access_token, sku)
                
                if product:
                    # Busca informações de estoque
                    product_id = product.get('id')
                    if product_id:
                        stock_info = self.api_client.get_stock(access_token, product_id)
                        
                        if stock_info:
                            # Atualiza o estoque em todos os componentes com este SKU
                            current_stock = stock_info.get('saldoVirtualTotal', 0)
                            self._update_component_stock_by_sku(sku, current_stock)
                            updated_count += 1
                
                # Delay entre consultas
                time.sleep(self.config.DELAY_BETWEEN_BATCHES)
                
            except Exception as e:
                logger.error(f"Erro ao atualizar estoque do componente {sku}: {e}")
        
        logger.info(f"Estoque atualizado para {updated_count} componentes.")
    
    def _update_component_stock_by_sku(self, sku: str, current_stock: int):
        """Atualiza o estoque atual de um componente específico em todos os kits."""
        for kit in self.kits:
            for component in kit.components:
                if component.sku == sku:
                    component.current_stock = current_stock
    
    def run_purchase_check(self, create_orders: bool = False) -> bool:
        """Executa verificação de necessidades de compra e criação de ordens."""
        if self.is_running:
            logger.warning("Processamento já em andamento.")
            return False
        
        with self.lock:
            self.is_running = True
        
        try:
            start_time = time.time()
            self.stats.reset()
            
            logger.info("Iniciando verificação de necessidades de compra...")
            
            # Verifica autenticação
            token = self.auth.get_valid_token()
            if not token:
                logger.error("Token de acesso não disponível.")
                return False
            
            # Limpa necessidades anteriores
            self.needs_manager.clear_needs()
            
            # Verifica cada kit
            for kit in self.kits:
                self._check_kit_needs(kit)
                self.stats.stock_checks += 1
            
            # Cria ordens se solicitado
            if create_orders:
                self._create_purchase_orders(token)
            
            self.stats.elapsed_time_seconds = time.time() - start_time
            
            logger.info(f"Verificação concluída em {self.stats.elapsed_time_seconds:.2f}s")
            return True
            
        except Exception as e:
            logger.error(f"Erro na verificação de necessidades: {e}")
            error_logger.error(f"Erro na verificação de necessidades: {e}")
            self.stats.failed += 1
            return False
        finally:
            with self.lock:
                self.is_running = False
    
    def _check_kit_needs(self, kit: Kit):
        """Verifica as necessidades de compra de um kit específico."""
        logger.debug(f"Verificando necessidades do kit {kit.sku}")
        
        for component in kit.components:
            # Verifica se o estoque está abaixo do mínimo
            if component.current_stock < component.min_stock:
                quantity_needed = component.min_stock - component.current_stock
                
                need = PurchaseNeed(
                    component_sku=component.sku,
                    component_name=component.name,
                    quantity_needed=quantity_needed,
                    supplier=component.supplier,
                    lead_time_days=component.lead_time_days,
                    reason=f"Estoque baixo: {component.current_stock} < {component.min_stock}"
                )
                
                self.needs_manager.add_need(need)
    
    def _create_purchase_orders(self, access_token: str):
        """Cria ordens de compra baseadas nas necessidades identificadas."""
        logger.info("Criando ordens de compra...")
        
        all_needs = self.needs_manager.get_all_needs()
        
        for supplier, needs in all_needs.items():
            if not needs:
                continue
            
            try:
                # Cria uma PO por fornecedor
                po_data = self._build_purchase_order_data(supplier, needs)
                
                result = self.api_client.create_purchase_order(access_token, po_data)
                
                if result:
                    self.stats.pos_created += 1
                    self.stats.success += 1
                    logger.info(f"PO criada para fornecedor {supplier}")
                else:
                    self.stats.failed += 1
                    logger.error(f"Falha ao criar PO para fornecedor {supplier}")
                
            except Exception as e:
                self.stats.failed += 1
                logger.error(f"Erro ao criar PO para fornecedor {supplier}: {e}")
    
    def _build_purchase_order_data(self, supplier: str, needs: List[PurchaseNeed]) -> Dict[str, Any]:
        """Constrói os dados para criação de uma ordem de compra."""
        # Estrutura básica de uma PO no Bling
        # Esta estrutura pode precisar ser ajustada conforme a documentação da API
        
        items = []
        for need in needs:
            item = {
                'codigo': need.component_sku,
                'descricao': need.component_name,
                'quantidade': need.quantity_needed,
                'valor': 0.0  # Será preenchido manualmente ou via integração com catálogo
            }
            items.append(item)
        
        po_data = {
            'fornecedor': {
                'nome': supplier
            },
            'itens': items,
            'observacoes': f"PO gerada automaticamente - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        }
        
        return po_data
    
    def process_kits(self, kits: List[Kit], batch_size: int = None, create_orders: bool = False, quantity: int = 1):
        """Processa uma lista específica de kits."""
        if self.is_running:
            logger.warning("Processamento já em andamento.")
            return
        
        if not kits:
            logger.warning("Nenhum kit fornecido para processamento.")
            return
        
        batch_size = batch_size or self.config.DEFAULT_BATCH_SIZE
        
        with self.lock:
            self.is_running = True
        
        try:
            start_time = time.time()
            self.stats.reset()
            
            logger.info(f"Iniciando processamento de {len(kits)} kits...")
            
            # Verifica autenticação
            token = self.auth.get_valid_token()
            if not token:
                logger.error("Token de acesso não disponível.")
                return
            
            # Processa kits em lotes
            for i in range(0, len(kits), batch_size):
                batch = kits[i:i + batch_size]
                
                for kit in batch:
                    try:
                        if create_orders:
                            # Cria ordem de produção para o kit
                            self._create_production_order(token, kit, quantity)
                        
                        # Verifica necessidades dos componentes
                        self._check_kit_needs(kit)
                        
                        self.stats.success += 1
                        
                    except Exception as e:
                        self.stats.failed += 1
                        logger.error(f"Erro ao processar kit {kit.sku}: {e}")
                
                # Delay entre lotes
                if i + batch_size < len(kits):
                    time.sleep(self.config.DELAY_BETWEEN_BATCHES)
            
            # Cria POs se necessário
            if create_orders:
                self._create_purchase_orders(token)
            
            self.stats.elapsed_time_seconds = time.time() - start_time
            
            logger.info(f"Processamento concluído em {self.stats.elapsed_time_seconds:.2f}s")
            
        except Exception as e:
            logger.error(f"Erro no processamento de kits: {e}")
            error_logger.error(f"Erro no processamento de kits: {e}")
        finally:
            with self.lock:
                self.is_running = False
    
    def _create_production_order(self, access_token: str, kit: Kit, quantity: int):
        """Cria uma ordem de produção para um kit."""
        try:
            op_data = {
                'produto': {
                    'codigo': kit.sku
                },
                'quantidade': quantity,
                'observacoes': f"OP gerada automaticamente - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            }
            
            result = self.api_client.create_production_order(access_token, op_data)
            
            if result:
                self.stats.ops_created += 1
                logger.info(f"OP criada para kit {kit.sku}")
            else:
                logger.error(f"Falha ao criar OP para kit {kit.sku}")
                
        except Exception as e:
            logger.error(f"Erro ao criar OP para kit {kit.sku}: {e}")

# ============================================================================
# 1. FUNÇÃO PARA BUSCAR PRODUTO POR SKU (PARA O DASHBOARD)
# ============================================================================

def get_bling_product_by_sku(sku):
    """
    Busca um produto no Bling pelo SKU e retorna os dados formatados.
    Esta função é usada pela rota /api/produtos do dashboard.
    """
    
    # Carrega tokens
    token_data = load_tokens()
    if not token_data or not is_token_valid(token_data):
        # Tenta renovar
        token_data = refresh_access_token()
        if not token_data:
            return {"error": "Token de acesso inválido. Faça a autenticação."}
    
    access_token = token_data["access_token"]
    
    # Faz a requisição para a API do Bling
    url = f"https://www.bling.com.br/Api/v3/produtos"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    params = {"codigo": sku}
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            return data
        else:
            return {"error": f"Erro na API: {response.status_code}"}
            
    except Exception as e:
        return {"error": f"Erro de conexão: {str(e)}"}

# ============================================================================
# 10. INSTÂNCIAS GLOBAIS
# ============================================================================

# Configuração
config = Config()

# Orquestrador principal
orchestrator = AutomationOrchestrator(config)

# Autenticação (referência para compatibilidade)
auth = orchestrator.auth

# PARTE 4 — TEMPLATE HTML DO FRONT-END (INTERFACE DO USUÁRIO)
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Consulta de Produto Bling</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background-color: #f8f9fa; }
        .container { max-width: 800px; margin-top: 50px; }
        .product-card { border: 1px solid #dee2e6; border-radius: 0.5rem; padding: 20px; background-color: #fff; box-shadow: 0 0.125rem 0.25rem rgba(0, 0, 0, 0.075); }
        .product-image { max-width: 100%; height: auto; border-radius: 0.25rem; margin-bottom: 15px; }
        .product-detail { margin-bottom: 5px; }
        .product-detail strong { display: inline-block; width: 120px; }
        #descricao { border-top: 1px solid #eee; padding-top: 15px; margin-top: 15px; }
        .hidden { display: none; }
    </style>
</head>
<body>
    <div class="container">
        <h1 class="mb-4 text-center">Consulta de Produto Bling</h1>
        
        <div class="input-group mb-3">
            <input type="text" class="form-control" id="skuInput" placeholder="Digite o SKU do produto" aria-label="SKU do produto">
            <button class="btn btn-primary" type="button" id="searchButton">Buscar</button>
        </div>

        <div id="loading" class="alert alert-info hidden" role="alert">
            Buscando produto...
        </div>

        <div id="errorAlert" class="alert alert-danger hidden" role="alert">
            Produto não encontrado. Verifique o SKU.
        </div>

        <div id="productDetails" class="product-card hidden">
            <div class="row">
                <div class="col-md-4 text-center">
                    <img id="imgProduto" src="/placeholder.png" alt="Imagem do Produto" class="product-image">
                </div>
                <div class="col-md-8">
                    <h2 id="nome" class="mb-3"></h2>
                    <div class="product-detail"><strong>Código:</strong> <span id="codigo"></span></div>
                    <div class="product-detail"><strong>Tipo:</strong> <span id="tipo"></span></div>
                    <div class="product-detail"><strong>Situação:</strong> <span id="situacao"></span></div>
                    <div class="product-detail"><strong>Formato:</strong> <span id="formato"></span></div>
                    <div class="product-detail"><strong>Preço:</strong> <span id="preco"></span></div>
                    <div class="product-detail"><strong>Preço Custo:</strong> <span id="precoCusto"></span></div>
                    <div class="product-detail"><strong>Estoque:</strong> <span id="estoque"></span></div>
                </div>
            </div>
            <div id="descricao">
                <h4>Descrição</h4>
                <!-- A descrição será inserida aqui com innerHTML -->
            </div>
        </div>
    </div>
    <script>
        {% raw %}
        
        document.getElementById('searchButton').addEventListener('click', buscarProduto);
        document.getElementById('skuInput').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                buscarProduto();
            }
        });

        function showElement(id) { document.getElementById(id).classList.remove('hidden'); }
        function hideElement(id) { document.getElementById(id).classList.add('hidden'); }

        function exibirErro(mensagem) {
            hideElement('productDetails');
            showElement('errorAlert');
            document.getElementById('errorAlert').innerText = mensagem;
        }

        function limparDetalhes() {
            hideElement('productDetails');
            hideElement('errorAlert');
            document.getElementById('nome').innerText = '';
            document.getElementById('codigo').innerText = '';
            document.getElementById('tipo').innerText = '';
            document.getElementById('situacao').innerText = '';
            document.getElementById('formato').innerText = '';
            document.getElementById('preco').innerText = '';
            document.getElementById('precoCusto').innerText = '';
            document.getElementById('estoque').innerText = '';
            document.getElementById('descricao').innerHTML = '<h4>Descrição</h4>';
            document.getElementById('imgProduto').src = '/placeholder.png';
        }

        async function buscarProduto() {
            const sku = document.getElementById('skuInput').value.trim();
            if (!sku) {
                exibirErro("Por favor, digite um SKU.");
                return;
            }

            limparDetalhes();
            showElement('loading');

            try {
                const response = await fetch(`/api/produtos?sku=${sku}`);
                const data = await response.json();
                
                // ✅ 1. Ajustar o fetch da API: Ler sempre json.data[0] e armazenar como const p.
                const p = data.data?.[0]; 

                hideElement('loading');

                // ✅ 5. Ajustar verificação se o produto existe
                if (!p) {
                    exibirErro("Produto não encontrado. Verifique o SKU.");
                    return;
                }

                // ✅ 2. Atualizar os campos que aparecem na interface (com fallback)
                document.getElementById("nome").innerText = p.nome || "Sem nome";
                document.getElementById("codigo").innerText = p.codigo || "N/D";
                document.getElementById("tipo").innerText = p.tipo || "N/D";
                document.getElementById("situacao").innerText = p.situacao || "N/D";
                document.getElementById("formato").innerText = p.formato || "N/D";
                
                // Formatação de preço simples (pode ser melhorada com Intl.NumberFormat)
                document.getElementById("preco").innerText = p.preco ? `R$ ${parseFloat(p.preco).toFixed(2).replace('.', ',')}` : "N/D";
                document.getElementById("precoCusto").innerText = p.precoCusto ? `R$ ${parseFloat(p.precoCusto).toFixed(2).replace('.', ',')}` : "N/D";
                
                // ✅ Estoque em p.estoque.saldoVirtualTotal (com fallback)
                document.getElementById("estoque").innerText = p.estoque?.saldoVirtualTotal ?? "0";

                // ✅ 4. Ajustar descrição (ela é HTML) - Usar innerHTML
                document.getElementById("descricao").innerHTML = `<h4>Descrição</h4>${p.descricaoCurta || "Sem descrição."}`;

                // ✅ 3. Ajustar exibição da imagem - Usar p.imagemURL
                document.getElementById("imgProduto").src = p.imagemURL || "/placeholder.png";

                showElement('productDetails');

            } catch (error) {
                hideElement('loading');
                console.error('Erro ao buscar produto:', error);
                exibirErro("Ocorreu um erro ao comunicar com a API.");
            }
        }        
        {% endraw %}
    </script>
</body>
</html>
"""

class WebServer:
    """Gerencia o servidor Flask, rotas e websocket."""
    
    def __init__(self, app: Flask, orchestrator: AutomationOrchestrator):
        self.app = app
        self.orchestrator = orchestrator
        self.sock = Sock(app)
        self.setup_routes()
        self.setup_websocket()

    def setup_routes(self):
        """Configura todas as rotas da API e do Dashboard."""
        
        # 1. Dashboard e Páginas de Auth


        # PARTE 5 — ROTA DO FRONT-END
        @self.app.route("/")
        def dashboard():
            """Rota principal que serve o dashboard de consulta de produto."""
            return render_template_string(DASHBOARD_TEMPLATE, auth_url=self.orchestrator.auth.get_authorization_url() if hasattr(self.orchestrator, 'auth') else '#', api_base='/api')

        @self.app.route('/callback')
        def callback():
            code = request.args.get('code')
            error = request.args.get('error')
            received_state = request.args.get('state')
            expected_state = self.orchestrator.auth.state
            
            # 1. Validação do state
            if not received_state or received_state != expected_state:
                return render_template_string(ERROR_TEMPLATE, message="Erro de Segurança: Parâmetro 'state' inválido ou ausente."), 400
            
            if error:
                return render_template_string(ERROR_TEMPLATE, message=f"Erro de Autorização: {error}")
            
            if code:
                try:
                    self.orchestrator.auth.exchange_code_for_token(code)
                    # Limpa o state após o uso
                    self.orchestrator.auth.state = None
                    return render_template_string(SUCCESS_TEMPLATE, message="Autenticação concluída com sucesso!")
                except BlingAuthError as e:
                    return render_template_string(ERROR_TEMPLATE, message=f"Falha na troca de código: {e}")
            
            return render_template_string(ERROR_TEMPLATE, message="Parâmetros de callback inválidos.")

        # 2. Rotas de Status e Estatísticas
        @self.app.route('/api/status')
        def api_status():
            is_valid = self.orchestrator.auth.is_authenticated()
            return jsonify({
                "authenticated": is_valid,
                "auth_url": self.orchestrator.auth.get_authorization_url(),
                "token_expires_at": (
                    datetime.fromtimestamp(self.orchestrator.auth.expires_at).isoformat()
                    if self.orchestrator.auth.expires_at
                    else None
                ),
                "data_loaded": True, # Assume True, pois o carregamento é feito por worker/processo
                "is_running": self.orchestrator.is_running
            })

        @self.app.route('/api/stats')
        def api_stats():
            return jsonify(self.orchestrator.stats.to_dict())

        # 3. Rotas de Dados
        @self.app.route("/api/produtos", methods=["GET"])
        def api_produtos():
            sku = request.args.get("sku")

            if not sku:
                return jsonify({"error": "SKU não informado"}), 400

            print("Consulta de produto recebida:", sku, flush=True)

            data = get_bling_product_by_sku(sku)

            return jsonify(data), 200

        @self.app.route('/api/kits')
        def api_kits():
            kits_data = [
                {
                    "sku": k.sku,
                    "name": k.name,
                    "price": k.price,
                    "components": [
                        {
                            "sku": c.sku,
                            "name": c.name,
                            "qty": c.qty,
                            "supplier": c.supplier,
                            "lead_time_days": c.lead_time_days,
                            "unit_cost": c.unit_cost
                        } for c in k.components
                    ]
                } for k in self.orchestrator.kits
            ]
            return jsonify({"kits": kits_data})

        @self.app.route('/api/stock')
        def api_stock():
            all_components: Dict[str, Component] = {}
            for kit in self.orchestrator.kits:
                for component in kit.components:
                    all_components[component.sku] = component
            
            stock_data = [
                {
                    "sku": c.sku,
                    "name": c.name,
                    "current_stock": c.current_stock,
                    "min_stock": c.min_stock,
                    "supplier": c.supplier,
                    "lead_time_days": c.lead_time_days,
                    "alert_level": "danger" if c.current_stock < c.min_stock else ("warning" if c.current_stock < c.min_stock * 1.5 else "ok")
                } for c in all_components.values()
            ]
            return jsonify({"stock": stock_data})

        @self.app.route('/api/needs')
        def api_needs():
            needs_list = []
            for supplier, needs in self.orchestrator.needs_manager.needs.items():
                for need in needs:
                    needs_list.append({
                        "component_sku": need.component_sku,
                        "component_name": need.component_name,
                        "quantity_needed": need.quantity_needed,
                        "supplier": need.supplier,
                        "lead_time_days": need.lead_time_days,
                        "reason": need.reason
                    })
            return jsonify({"needs": needs_list})

        # 4. Rotas de Ação
        @self.app.route('/api/recheck', methods=['POST'])
        def api_recheck():
            if self.orchestrator.is_running:
                return jsonify({"status": "warning", "message": "Processamento já em andamento."}), 409
            
            # Executa a verificação em uma thread para não bloquear a requisição HTTP
            Thread(target=self.orchestrator.run_purchase_check, args=(True,), daemon=True).start()
            
            return jsonify({"status": "ok", "message": "Verificação de estoque e POs iniciada em background."})

        @self.app.route('/api/process_kits', methods=['POST'])
        def api_process_kits():
            if self.orchestrator.is_running:
                return jsonify({"status": "warning", "message": "Processamento já em andamento."}), 409
                
            data = request.get_json(silent=True) or {}
            sku_list = data.get('skus', [])
            quantity = data.get('quantity', 1)
            batch_size = data.get('batch_size', self.orchestrator.config.DEFAULT_BATCH_SIZE)
            
            kits_to_process = [k for k in self.orchestrator.kits if k.sku in sku_list]
            
            if not kits_to_process:
                return jsonify({"status": "error", "message": "Nenhum kit encontrado com os SKUs fornecidos."}), 404
            
            # Executa o processamento em uma thread
            Thread(target=self.orchestrator.process_kits, args=(kits_to_process, batch_size, True, quantity), daemon=True).start()
            
            return jsonify({"status": "ok", "message": f"Processamento de {len(kits_to_process)} kits iniciado em background."})

        # 5. Webhook
        @self.app.route("/webhook/bling", methods=["POST"])
        def webhook_bling():
            try:
                data = request.get_json(silent=True)
            except Exception:
                data = None

            print("WEBHOOK RECEBIDO:", data, flush=True)

            return jsonify({"status": "ok"}), 200

    # 10. WEBSOCKET PARA LOGS
    def setup_websocket(self):
        """Configura a rota WebSocket para logs em tempo real."""
        
        @self.sock.route('/ws/logs')
        def ws_logs(ws):
            logger.info("Cliente WebSocket conectado para logs.")
            last_log_count = 0
            
            # O loop continua enquanto a conexão WebSocket estiver aberta.
            # O fechamento da conexão pelo cliente irá levantar uma exceção, que será capturada.
            while not ws.closed:
                try:
                    # Envia todos os logs novos desde a última verificação
                    current_logs = memory_handler.get_logs()
                    new_logs = current_logs[last_log_count:]
                    
                    if new_logs:
                        ws.send(json.dumps({"logs": new_logs}))
                        last_log_count = len(current_logs)
                        
                    time.sleep(1) # Envia a cada 1 segundo
                except Exception as e:
                    logger.warning(f"Erro no WebSocket: {e}. Fechando conexão.")
                    break
            logger.info("Cliente WebSocket desconectado.")

# 14. DEPLOY E SERVIDOR (Função factory e background task)
def background_load():
    """Função executada em thread para carregar dados em background."""
    
    # Delay desnecessário removido conforme instruído.
    # O carregamento de dados deve ser otimizado dentro de orchestrator.load_data()
    # para evitar o carregamento de TODOS os kits na inicialização.
    
    logger.info("Iniciando Carregamento de Dados em Background")
    
    try:
        # 1. Tenta carregar tokens
        if not auth.load_tokens():
            logger.warning("Tokens não carregados. Necessário autenticar via dashboard.")
            return
            
        # 2. Busca kits e componentes (inclui estoque)
        if orchestrator.load_data():
            logger.info("Carregamento de dados em background concluído com sucesso.")
        else:
            logger.error("Falha no carregamento de dados em background.")
            
    except Exception as e:
        logger.error(f"Erro crítico no background_load: {e}")
        error_logger.error(f"Erro crítico no background_load: {e}")
        
    

def create_app() -> Flask:
    """Função factory para criar a aplicação Flask."""
    app = Flask(__name__, template_folder='.')
    # A WebServer inicializa as rotas e o websocket
    WebServer(app, orchestrator)
    return app

# Variável global para WSGI
app = create_app()

# ============================================================================
# 18. TEMPLATES HTML (Mínimos para Auth)
# ============================================================================

SUCCESS_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sucesso!</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
</head>
<body class="bg-light">
    <div class="container d-flex justify-content-center align-items-center" style="min-height: 100vh;">
        <div class="card shadow-lg p-5 text-center">
            <h1 class="text-success">✅ Sucesso!</h1>
            <p class="lead">{{ message }}</p>
            <p>Você pode fechar esta janela e voltar ao dashboard.</p>
            <a href="/" class="btn btn-primary">Voltar ao Dashboard</a>
        </div>
    </div>
</body>
</html>
"""

ERROR_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Erro!</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
</head>
<body class="bg-light">
    <div class="container d-flex justify-content-center align-items-center" style="min-height: 100vh;">
        <div class="card shadow-lg p-5 text-center">
            <h1 class="text-danger">❌ Erro!</h1>
            <p class="lead">{{ message }}</p>
            <p>Verifique o log para mais detalhes ou tente novamente.</p>
            <a href="/" class="btn btn-primary">Voltar ao Dashboard</a>
        </div>
    </div>
</body>
</html>
"""

# 11. DASHBOARD HTML COMPLETO (Será minificado e completo na próxima fase)
# Por enquanto, apenas o esqueleto com o CSS e JS embutidos
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel Bling - Automação ERP</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
    <style>
        {% raw %}
        body { background: #f8f9fa; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        .navbar { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; box-shadow: 0 4px 6px rgba(0,0,0,.1); }
        .navbar-brand { font-weight: 700; font-size: 1.5rem; }
        .status-badge { padding: .5rem 1rem; border-radius: 20px; font-size: .9rem; font-weight: 600; }
        .card { border-radius: 1rem; box-shadow: 0 4px 6px rgba(0,0,0,.07); border: none; margin-bottom: 1.5rem; transition: transform 0.3s ease, box-shadow 0.3s ease; }
        .card:hover { transform: translateY(-5px); box-shadow: 0 8px 15px rgba(0,0,0,.1); }
        .card-title { font-weight: 600; color: #343a40; margin-bottom: 1rem; }
        .kpi-value { font-size: 2.5rem; font-weight: 700; margin-bottom: .25rem; }
        .kpi-label { font-size: .9rem; color: #6c757d; text-transform: uppercase; letter-spacing: .5px; }
        .log-box { font-family: 'Courier New', monospace; font-size: .85em; background: #1e1e1e; color: #d4d4d4; border-radius: .5rem; padding: 1rem; max-height: 400px; overflow-y: auto; }
        .log-entry { padding: .25rem 0; border-bottom: 1px solid #333; }
        .log-entry:last-child { border-bottom: none; }
        .log-level-INFO { color: #4ec9b0; }
        .log-level-WARNING { color: #dcdcaa; }
        .log-level-ERROR { color: #f48771; }
        .log-level-DEBUG { color: #9cdcfe; }
        .nav-tabs .nav-link { color: #6c757d; font-weight: 500; }
        .nav-tabs .nav-link.active { background-color: #fff; border-color: #dee2e6 #dee2e6 #fff; color: #667eea; font-weight: 600; }
        .table-danger td { background-color: #f8d7da !important; }
        .table-warning td { background-color: #fff3cd !important; }
        .btn-primary { background: linear-gradient(45deg, #667eea, #764ba2); border: none; transition: all 0.3s ease; }
        .btn-primary:hover { transform: translateY(-2px); box-shadow: 0 4px 8px rgba(102, 126, 234, 0.4); }
        .spinner-border-sm { width: 1rem; height: 1rem; border-width: .15em; }
        {% endraw %}
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg">
        <div class="container-fluid">
            <a class="navbar-brand text-white" href="#">Bling Automação</a>
            <div class="d-flex">
                <span id="status-badge" class="status-badge bg-secondary text-white me-2">Carregando...</span>
                <a id="auth-link" href="{{ auth_url }}" class="btn btn-sm btn-outline-light">Autenticar Bling</a>
            </div>
        </div>
    </nav>

    <div class="container mt-4">
        <div class="row">
            <!-- 5. SISTEMA DE ESTATÍSTICAS (KPIs) -->
            <div class="col-md-2"><div class="card text-center"><div class="card-body"><div id="kpi-success" class="kpi-value text-success">0</div><div class="kpi-label">Sucesso ✅</div></div></div></div>
            <div class="col-md-2"><div class="card text-center"><div class="card-body"><div id="kpi-failed" class="kpi-value text-danger">0</div><div class="kpi-label">Falhas ❌</div></div></div></div>
            <div class="col-md-2"><div class="card text-center"><div class="card-body"><div id="kpi-ops" class="kpi-value text-primary">0</div><div class="kpi-label">OPs Criadas 🏭</div></div></div></div>
            <div class="col-md-2"><div class="card text-center"><div class="card-body"><div id="kpi-pos" class="kpi-value text-info">0</div><div class="kpi-label">POs Criadas 🛒</div></div></div></div>
            <div class="col-md-2"><div class="card text-center"><div class="card-body"><div id="kpi-checks" class="kpi-value text-secondary">0</div><div class="kpi-label">Checks Estoque 🔍</div></div></div></div>
            <div class="col-md-2"><div class="card text-center"><div class="card-body"><div id="kpi-time" class="kpi-value text-dark">0s</div><div class="kpi-label">Tempo Total ⏱️</div></div></div></div>
        </div>

        <div class="row">
            <div class="col-md-6">
                <div class="card">
                    <div class="card-body">
                        <h5 class="card-title">Gráfico de Processamento</h5>
                        <!-- 5. SISTEMA DE ESTATÍSTICAS (Gráfico) -->
                        <canvas id="processingChart"></canvas>
                    </div>
                </div>
            </div>
            <div class="col-md-6">
                <div class="card">
                    <div class="card-body">
                        <h5 class="card-title">Logs em Tempo Real</h5>
                        <!-- 5. SISTEMA DE ESTATÍSTICAS (Logs) -->
                        <div id="logs-content" class="log-box">
                            <p class="text-white-50">Aguardando conexão com o WebSocket...</p>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <div class="card mt-4">
            <div class="card-header">
                <ul class="nav nav-tabs card-header-tabs" id="myTab" role="tablist">
                    <li class="nav-item"><a class="nav-link active" id="stock-tab" data-bs-toggle="tab" href="#stock" role="tab">Estoque de Componentes</a></li>
                    <li class="nav-item"><a class="nav-link" id="needs-tab" data-bs-toggle="tab" href="#needs" role="tab">Necessidades de Compra</a></li>
                    <li class="nav-item"><a class="nav-link" id="kits-tab" data-bs-toggle="tab" href="#kits" role="tab">Kits e Estrutura</a></li>
                    <li class="nav-item"><a class="nav-link" id="search-tab" data-bs-toggle="tab" href="#search" role="tab">Busca Detalhada</a></li>
                </ul>
            </div>
            <div class="card-body">
                <div class="tab-content" id="myTabContent">
                    <!-- Tabela de Estoque -->
                    <div class="tab-pane fade show active" id="stock" role="tabpanel">
                        <div class="d-flex justify-content-between align-items-center mb-3">
                            <h5 class="card-title">Estoque de Componentes com Alertas</h5>
                            <!-- 11. BOTÃO RECHECK -->
                            <button id="recheck-button" class="btn btn-primary btn-sm">
                                <span id="recheck-spinner" class="spinner-border spinner-border-sm me-2 d-none" role="status" aria-hidden="true"></span>
                                Verificar Estoque e Gerar POs
                            </button>
                        </div>
                        <p id="recheck-status" class="text-muted"></p>
                        <div class="table-responsive">
                            <table class="table table-striped table-hover">
                                <thead>
                                    <tr><th>SKU</th><th>Nome</th><th>Estoque Atual</th><th>Estoque Mínimo</th><th>Fornecedor</th><th>Lead Time (dias)</th><th>Alerta</th></tr>
                                </thead>
                                <tbody id="stock-table-body">
                                    <tr><td colspan="7" class="text-center">Carregando dados de estoque...</td></tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                    <!-- Tabela de Necessidades -->
                    <div class="tab-pane fade" id="needs" role="tabpanel">
                        <h5 class="card-title">Necessidades de Compra Pendentes</h5>
                        <div class="table-responsive">
                            <table class="table table-striped table-hover">
                                <thead>
                                    <tr><th>SKU</th><th>Nome</th><th>Qtd. Necessária</th><th>Fornecedor</th><th>Lead Time (dias)</th><th>Motivo</th></tr>
                                </thead>
                                <tbody id="needs-table-body">
                                    <tr><td colspan="6" class="text-center">Nenhuma necessidade de compra pendente.</td></tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                    <!-- Tabela de Kits -->
                    <div class="tab-pane fade" id="kits" role="tabpanel">
                        <h5 class="card-title">Kits de Produtos e Estrutura</h5>
                        <div class="table-responsive">
                            <table class="table table-striped table-hover">
                                <thead>
                                    <tr><th>SKU Kit</th><th>Nome Kit</th><th>Preço</th><th>Componentes</th></tr>
                                </thead>
                                <tbody id="kits-table-body">
                                    <tr><td colspan="4" class="text-center">Carregando dados de kits...</td></tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                    
                    <!-- Busca Detalhada -->
                    <div class="tab-pane fade" id="search" role="tabpanel">
                        <h5 class="card-title">Buscar Produto por SKU</h5>
                        <div class="input-group mb-3">
                            <input type="text" class="form-control" id="product-search-sku" placeholder="Digite o SKU do produto (ex: KIT-001)">
                            <button class="btn btn-primary" type="button" id="search-product-button">Buscar</button>
                        </div>
                        <div id="product-search-results" class="mt-4">
                            <p class="text-muted">Use o campo acima para buscar um produto e ver seus detalhes e componentes.</p>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        {% raw %}
        
        const API_BASE = '/api';
        const WS_URL = `ws://${window.location.host}/ws/logs`;
        let logWebSocket;
        let processingChart;

        // a) Funções
        function formatLog(log) {
            const level = log.level;
            const levelClass = `log-level-${level}`;
            return `<div class="log-entry"><span class="${levelClass}">[${log.timestamp}] [${level}]</span> ${log.message}</div>`;
        }

        function updateStatusBadge(isValid, expiresAt) {
            const badge = document.getElementById('status-badge');
            const authLink = document.getElementById('auth-link');
            
            if (isValid) {
                badge.className = 'status-badge bg-success text-white me-2';
                badge.textContent = 'Token Válido';
                authLink.className = 'btn btn-sm btn-outline-light d-none';
            } else {
                badge.className = 'status-badge bg-danger text-white me-2';
                badge.textContent = 'Token Inválido';
                authLink.className = 'btn btn-sm btn-outline-light';
            }
            
            if (expiresAt) {
                const expiry = new Date(expiresAt);
                const now = new Date();
                const diffMinutes = Math.round((expiry - now) / 60000);
                if (diffMinutes < 60 && diffMinutes > 0) {
                    badge.textContent += ` (Expira em ${diffMinutes} min)`;
                    badge.className = 'status-badge bg-warning text-dark me-2';
                }
            }
        }

        function updateStatsKPIs(stats) {
            document.getElementById('kpi-success').textContent = stats.success;
            document.getElementById('kpi-failed').textContent = stats.failed;
            document.getElementById('kpi-ops').textContent = stats.ops_created;
            document.getElementById('kpi-pos').textContent = stats.pos_created;
            document.getElementById('kpi-checks').textContent = stats.stock_checks;
            document.getElementById('kpi-time').textContent = `${stats.elapsed_time_seconds}s`;
        }

        function updateStatsChart(stats) {
            const ctx = document.getElementById('processingChart').getContext('2d');
            const data = [stats.success, stats.failed, stats.ops_created, stats.pos_created];
            
            if (!processingChart) {
                processingChart = new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: ['Sucesso', 'Falhas', 'OPs Criadas', 'POs Criadas'],
                        datasets: [{
                            label: 'Contagem',
                            data: data,
                            backgroundColor: ['#4ec9b0', '#f48771', '#667eea', '#764ba2'],
                            borderColor: ['#4ec9b0', '#f48771', '#667eea', '#764ba2'],
                            borderWidth: 1
                        }]
                    },
                    options: {
                        responsive: true,
                        scales: {
                            y: { beginAtZero: true, ticks: { precision: 0 } }
                        },
                        plugins: { legend: { display: false } }
                    }
                });
            } else {
                processingChart.data.datasets[0].data = data;
                processingChart.update();
            }
        }

        async function fetchStatus() {
            try {
                const response = await fetch(`${API_BASE}/status`);
                const data = await response.json();
                updateStatusBadge(data.authenticated, data.token_expires_at);
                
                const recheckButton = document.getElementById('recheck-button');
                const recheckSpinner = document.getElementById('recheck-spinner');
                if (data.is_running) {
                    recheckButton.disabled = true;
                    recheckSpinner.classList.remove('d-none');
                    document.getElementById('recheck-status').textContent = 'Processamento em andamento...';
                } else {
                    recheckButton.disabled = false;
                    recheckSpinner.classList.add('d-none');
                    document.getElementById('recheck-status').textContent = '';
                }
                
            } catch (error) {
                console.error('Erro ao buscar status:', error);
            }
        }

        async function fetchStats() {
            try {
                const response = await fetch(`${API_BASE}/stats`);
                const stats = await response.json();
                updateStatsKPIs(stats);
                updateStatsChart(stats);
            } catch (error) {
                console.error('Erro ao buscar estatísticas:', error);
            }
        }

        async function fetchStock() {
            try {
                const response = await fetch(`${API_BASE}/stock`);
                const data = await response.json();
                const tbody = document.getElementById('stock-table-body');
                tbody.innerHTML = '';
                
                if (data.stock.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="7" class="text-center">Nenhum componente encontrado.</td></tr>';
                    return;
                }
                
                data.stock.forEach(item => {
                    const row = tbody.insertRow();
                    row.className = item.alert_level === 'danger' ? 'table-danger' : (item.alert_level === 'warning' ? 'table-warning' : '');
                    
                    row.insertCell().textContent = item.sku;
                    row.insertCell().textContent = item.name;
                    row.insertCell().textContent = item.current_stock;
                    row.insertCell().textContent = item.min_stock;
                    row.insertCell().textContent = item.supplier;
                    row.insertCell().textContent = item.lead_time_days;
                    row.insertCell().innerHTML = item.alert_level === 'danger' ? '🚨 Baixo' : (item.alert_level === 'warning' ? '⚠️ Atenção' : '✅ OK');
                });
            } catch (error) {
                console.error('Erro ao buscar estoque:', error);
                document.getElementById('stock-table-body').innerHTML = '<tr><td colspan="7" class="text-center text-danger">Erro ao carregar dados de estoque.</td></tr>';
            }
        }

        async function fetchNeeds() {
            try {
                const response = await fetch(`${API_BASE}/needs`);
                const data = await response.json();
                const tbody = document.getElementById('needs-table-body');
                tbody.innerHTML = '';
                
                if (data.needs.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" class="text-center">Nenhuma necessidade de compra pendente.</td></tr>';
                    return;
                }
                
                data.needs.forEach(item => {
                    const row = tbody.insertRow();
                    row.insertCell().textContent = item.component_sku;
                    row.insertCell().textContent = item.component_name;
                    row.insertCell().textContent = item.quantity_needed;
                    row.insertCell().textContent = item.supplier;
                    row.insertCell().textContent = item.lead_time_days;
                    row.insertCell().textContent = item.reason;
                });
            } catch (error) {
                console.error('Erro ao buscar necessidades:', error);
                document.getElementById('needs-table-body').innerHTML = '<tr><td colspan="6" class="text-center text-danger">Erro ao carregar necessidades de compra.</td></tr>';
            }
        }

        async function fetchKits() {
            try {
                const response = await fetch(`${API_BASE}/kits`);
                const data = await response.json();
                const tbody = document.getElementById('kits-table-body');
                tbody.innerHTML = '';
                
                if (data.kits.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="4" class="text-center">Nenhum kit encontrado.</td></tr>';
                    return;
                }
                
                data.kits.forEach(kit => {
                    const row = tbody.insertRow();
                    row.insertCell().textContent = kit.sku;
                    row.insertCell().textContent = kit.name;
                    row.insertCell().textContent = `R$ ${kit.price.toFixed(2)}`;
                    
                    const componentsCell = row.insertCell();
                    componentsCell.innerHTML = kit.components.map(c => 
                        `${c.name} (${c.sku}) x${c.qty}`
                    ).join('<br>');
                });
            } catch (error) {
                console.error('Erro ao buscar kits:', error);
                document.getElementById('kits-table-body').innerHTML = '<tr><td colspan="4" class="text-center text-danger">Erro ao carregar dados de kits.</td></tr>';
            }
        }
        
        async function fetchProductDetails(sku) {
            const resultsDiv = document.getElementById('product-search-results');
            resultsDiv.innerHTML = '<p class="text-info">Buscando produto...</p>';
            
            try {
                const response = await fetch(`${API_BASE}/produtos?sku=${sku}`);
                const json = await response.json();
                
                if (response.ok) {
                    if (!json.data || json.data.length === 0) {
                        resultsDiv.innerHTML = `<p class="text-danger">Erro: Produto não encontrado.</p>`;
                        return;
                    }
                    const p = json.data[0];
                    renderProductDetails(p);
                } else {
                    resultsDiv.innerHTML = `<p class="text-danger">Erro: ${json.error || 'Produto não encontrado.'}</p>`;
                }
            } catch (error) {
                console.error('Erro ao buscar detalhes do produto:', error);
                resultsDiv.innerHTML = '<p class="text-danger">Erro de conexão ao buscar detalhes do produto.</p>';
            }
        }
        
        function renderProductDetails(p) {
            const resultsDiv = document.getElementById('product-search-results');
            
            // 1. Criar os elementos de exibição (IDs fictícios para o exemplo, pois o HTML não foi fornecido)
            // No código real, esses elementos seriam buscados por ID (ex: document.getElementById('nomeEl'))
            // Como estamos injetando HTML, vamos construir a string completa.
            
            let html = `
                <div class="card bg-light p-3">
                    <div class="row">
                        <div class="col-md-4 text-center">
                            <img id="produtoImagem" src="${p.imagemURL}" class="img-fluid rounded" alt="Imagem do Produto">
                        </div>
                        <div class="col-md-8">
                            <h5>Detalhes do Produto: ${p.nome} (${p.codigo})</h5>
                            <p><strong>Tipo:</strong> ${p.tipo}</p>
                            <p><strong>Situação:</strong> ${p.situacao}</p>
                            <p><strong>Formato:</strong> ${p.formato}</p>
                            <p><strong>Preço:</strong> R$ ${p.preco.toFixed(2)}</p>
                            <p><strong>Preço de Custo:</strong> R$ ${p.precoCusto.toFixed(2)}</p>
                            <p><strong>Estoque:</strong> ${p.estoque.saldoVirtualTotal}</p>
                        </div>
                    </div>
                    <h6 class="mt-3">Descrição Curta:</h6>
                    <div id="descricaoEl" class="card-text"></div>
                </div>
            `;
            
            resultsDiv.innerHTML = html;
            
            // 4. Ajustar descrição (ela é HTML) - Usar innerHTML
            const descricaoEl = document.getElementById('descricaoEl');
            if (descricaoEl) {
                descricaoEl.innerHTML = p.descricaoCurta;
            }
            
            // 3. Ajustar exibição da imagem - Já está no HTML, mas garantindo o src
            const imgProduto = document.getElementById('produtoImagem');
            if (imgProduto) {
                imgProduto.src = p.imagemURL;
            }
        }
        
        function connectWebSocket() {
            const logContent = document.getElementById('logs-content');
            logContent.innerHTML = '<p class="text-white-50">Tentando conectar ao WebSocket...</p>';
            
            logWebSocket = new WebSocket(WS_URL);

            logWebSocket.onopen = () => {
                console.log('WebSocket conectado.');
                logContent.innerHTML = ''; // Limpa a mensagem de conexão
            };

            logWebSocket.onmessage = (event) => {
                const data = JSON.parse(event.data);
                if (data.logs) {
                    data.logs.forEach(log => {
                        logContent.innerHTML += formatLog(log);
                    });
                    // Scroll para o final
                    logContent.scrollTop = logContent.scrollHeight;
                }
            };

            logWebSocket.onclose = (event) => {
                console.warn('WebSocket desconectado. Tentando reconectar em 5s...', event.reason);
                logContent.innerHTML += formatLog({
                    timestamp: new Date().toISOString().slice(0, 19),
                    level: 'WARNING',
                    message: 'Conexão WebSocket perdida. Tentando reconectar...'
                });
                setTimeout(connectWebSocket, 5000); // Reconexão automática
            };

                    logWebSocket.onerror = (err) => {
                        console.error('WebSocket erro:', err);
                        // Não chama close() aqui. Deixa o onclose() tratar a reconexão
                    };
        }
        
        // Handler do botão recheck
        document.getElementById('recheck-button').addEventListener('click', async () => {
            const button = document.getElementById('recheck-button');
            const spinner = document.getElementById('recheck-spinner');
            const statusText = document.getElementById('recheck-status');
            
            button.disabled = true;
            spinner.classList.remove('d-none');
            statusText.textContent = 'Iniciando verificação de estoque e geração de POs...';
            
            try {
                const response = await fetch(`${API_BASE}/recheck`, { method: 'POST' });
                const data = await response.json();
                
                if (response.ok) {
                    statusText.textContent = data.message;
                } else {
                    statusText.textContent = `Erro: ${data.error || 'Falha na requisição.'}`;
                    button.disabled = false;
                    spinner.classList.add('d-none');
                }
                
            } catch (error) {
                console.error('Erro ao chamar /api/recheck:', error);
                statusText.textContent = 'Erro de conexão ao iniciar a verificação.';
                button.disabled = false;
                spinner.classList.add('d-none');
            }
            // O status final será atualizado pelo fetchStatus quando is_running voltar a ser false
        });
        
        // Handler do botão de busca de produto
        document.getElementById('search-product-button').addEventListener('click', () => {
            const skuInput = document.getElementById('product-search-sku');
            const sku = skuInput.value.trim();
            if (sku) {
                fetchProductDetails(sku);
            } else {
                document.getElementById('product-search-results').innerHTML = '<p class="text-warning">Por favor, digite um SKU para buscar.</p>';
            }
        });

        // Handler do botão recheck
        document.getElementById('recheck-button').addEventListener('click', async () => {
            const button = document.getElementById('recheck-button');
            const spinner = document.getElementById('recheck-spinner');
            const statusText = document.getElementById('recheck-status');
            
            button.disabled = true;
            spinner.classList.remove('d-none');
            statusText.textContent = 'Iniciando verificação de estoque e geração de POs...';
            
            try {
                const response = await fetch(`${API_BASE}/recheck`, { method: 'POST' });
                const data = await response.json();
                
                if (response.ok) {
                    statusText.textContent = data.message;
                } else {
                    statusText.textContent = `Erro: ${data.message || 'Falha na requisição.'}`;
                    button.disabled = false;
                    spinner.classList.add('d-none');
                }
                
            } catch (error) {
                console.error('Erro ao chamar /api/recheck:', error);
                statusText.textContent = 'Erro de conexão ao iniciar a verificação.';
                button.disabled = false;
                spinner.classList.add('d-none');
            }
            // O status final será atualizado pelo fetchStatus quando is_running voltar a ser false
        });

        // b) Intervalos
        document.addEventListener('DOMContentLoaded', () => {
            fetchStatus();
            fetchStats();
            fetchStock();
            fetchNeeds();
            fetchKits();
            connectWebSocket();
            
            // Polling otimizado:
            // Status e Estatísticas (leves e importantes para feedback imediato) a cada 10s
            setInterval(fetchStatus, 10000);
            setInterval(fetchStats, 10000);

            // Dados pesados (Estoque, Necessidades, Kits) a cada 60s
            const dataPollingInterval = 60000;
            setInterval(fetchStock, dataPollingInterval);
            setInterval(fetchNeeds, dataPollingInterval);
            setInterval(fetchKits, dataPollingInterval);
        });
        {% endraw %}
    </script>
</body>
</html>
"""

# ============================================================================
# 15. CLI AVANÇADO
# ============================================================================

def run_cli():
    """Função principal para execução via linha de comando."""
    
    parser = argparse.ArgumentParser(description="Sistema de Automação Bling ERP.")
    parser.add_argument('--serve', action='store_true', help='Inicia o servidor web.')
    parser.add_argument('--run', action='store_true', help='Executa o processamento de kits (cria OPs e POs).')
    parser.add_argument('--port', type=int, default=8000, help='Define a porta para o servidor web (padrão: 8000).')
    
    args = parser.parse_args()
    
    if args.serve:
        logger.info("Iniciando Servidor Web")
        
        # 14. Lazy loading com Thread em background
        Thread(target=background_load, daemon=True).start()
        
        # 15. Validação de credenciais antes de iniciar
        if config.CLIENT_ID == 'YOUR_CLIENT_ID' or config.CLIENT_SECRET == 'YOUR_CLIENT_SECRET':
            logger.error("Credenciais BLING_CLIENT_ID ou BLING_CLIENT_SECRET não configuradas.")
            logger.warning("Configure as variáveis de ambiente ou altere a classe Config.")
            
        # Garante que o REDIRECT_URI está correto para a porta
        if args.port != 8000:
            config.REDIRECT_URI = config.REDIRECT_URI.replace(':8000', f':{args.port}')
            logger.info(f"REDIRECT_URI ajustado para a porta {args.port}: {config.REDIRECT_URI}")
            
        # O erro "port founds" (provavelmente "port already in use") é evitado
        # garantindo que a porta seja configurável e que o servidor seja iniciado
        # corretamente.
        try:
            logger.info(f"Servidor rodando em http://127.0.0.1:{args.port}")
            app.run(host='0.0.0.0', port=args.port, debug=False)
        except Exception as e:
            logger.error(f"Falha ao iniciar o servidor na porta {args.port}: {e}")
            error_logger.error(f"Falha ao iniciar o servidor: {e}")
            
    elif args.run:
        logger.info("Iniciando Processamento de Kits (CLI)")
        
        # Carrega dados
        if not orchestrator.load_data():
            logger.error("Falha no carregamento de dados. Verifique a autenticação.")
            return
        
        # Executa verificação de necessidades e criação de ordens
        success = orchestrator.run_purchase_check(create_orders=True)
        
        if success:
            logger.info("Processamento concluído com sucesso.")
            
            # Exibe estatísticas
            stats = orchestrator.stats.to_dict()
            print("\n=== ESTATÍSTICAS ===")
            for key, value in stats.items():
                print(f"{key}: {value}")
        else:
            logger.error("Processamento falhou.")
    else:
        parser.print_help()

# ============================================================================
# 17. PONTO DE ENTRADA
# ============================================================================

if __name__ == "__main__":
    run_cli()