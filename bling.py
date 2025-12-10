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
from functools import wraps

import requests
from requests.exceptions import RequestException
from flask import Flask, request, render_template_string, jsonify, redirect, url_for
from flask_sock import Sock

# ============================================================================
# 13. FUNÇÕES DE SUPORTE AO DASHBOARD
# =============================================================================

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
# 95. FUNÇÕES DE UTILIDADE
# ============================================================================

def buscar_produtos_por_sku_ou_nome(lista: List[Dict[str, Any]], termo_raw: str) -> List[Dict[str, Any]]:
    """
    Busca produtos na lista por SKU exato, nome exato ou nome parcial.
    Retorna uma lista de produtos ordenados por relevância:
    - Score 0: Match exato (SKU ou nome)
    - Score 1: Nome começa com o termo
    - Score 2: Nome contém o termo
    """
    termo = termo_raw.strip().lower()
    
    if not termo:
        return []
    
    results = []
    seen_ids = set()  # Para evitar duplicatas

    # 1) Match exato SKU (prioridade máxima)
    for p in lista:
        if p.get('codigo', '').strip().lower() == termo:
            pid = p.get('id', p.get('codigo'))
            if pid not in seen_ids:
                results.append((0, p))
                seen_ids.add(pid)

    # 2) Match exato nome
    for p in lista:
        if p.get('nome', '').strip().lower() == termo:
            pid = p.get('id', p.get('codigo'))
            if pid not in seen_ids:
                results.append((0, p))
                seen_ids.add(pid)

    # 3) Startswith nome (mais relevante)
    for p in lista:
        n = p.get('nome', '').strip().lower()
        if n.startswith(termo):
            pid = p.get('id', p.get('codigo'))
            if pid not in seen_ids:
                results.append((1, p))
                seen_ids.add(pid)

    # 4) Contains
    for p in lista:
        n = p.get('nome', '').strip().lower()
        if termo in n:
            pid = p.get('id', p.get('codigo'))
            if pid not in seen_ids:
                results.append((2, p))
                seen_ids.add(pid)
    
    # Retorna apenas os dicts ordenados por score (menor = mais relevante)
    results_sorted = [p for _, p in sorted(results, key=lambda x: x[0])]
    return results_sorted

def filtrar_variacoes(lista):
    """
    Remove produtos que parecem ser variações (tamanho, cor) com base em sufixos comuns.
    Usa regex para detectar padrões de variação e deduplica por SKU base.
    """
    import re
    
    # Padrões de variação (tamanhos e cores)
    VAR_SUFFIXES = re.compile(r'(\b(P|PP|M|G|GG|X(L)?|XL|XS)\b|-\b?(P|M|G|GG|PP)\b|-(vm|az|pt|br|brn|rd|bk)\b)$', re.I)
    COLOR_WORDS = re.compile(r'\b(vermelho|azul|preto|branco|verde|amarelo|rosa|cinza|marrom)\b', re.I)
    
    final = []
    seen_bases = set()
    
    for p in lista:
        codigo = p.get('codigo', '').strip()
        nome = p.get('nome', '').strip()
        base = codigo
        
        # Remove sufixos tipo -P, -PT, " P", " M"
        base = re.sub(r'[-\s](P|PP|M|G|GG|XL|XS|XP)$', '', base, flags=re.I)
        # Remove trailing color codes (AZ, VM, PT,...)
        base = re.sub(r'[-\s](AZ|VM|PT|BR|BK|RD)$', '', base, flags=re.I)
        
        base_upper = base.upper()
        
        # Se já vimos este SKU base, pula (deduplicação)
        if base_upper in seen_bases:
            continue
        
        # Marca como visto
        seen_bases.add(base_upper)
        
        # Adiciona à lista final
        final.append(p)
    
    return final

# ============================================================================
# 96. EXCEÇÕES CUSTOMIZADAS
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
    MIN_STOCK_THRESHOLD: int = 10
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
    unit: str = '' # Unidade de medida
    supplier: str = 'N/A'
    lead_time_days: int = 0
    unit_cost: float = 0.0
    min_stock: int = 0
    current_stock: int = 0

    def __post_init__(self):
        self.min_stock = max(0, self.min_stock)

@dataclass
class Kit:
    """Representa um Kit (produto composto) no Bling."""
    sku: str
    name: str
    components: List[Component] = field(default_factory=list)
    price: float = 0.0

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
        self.state: Optional[str] = None # Armazena o state gerado para validação no callback
        
    def get_authorization_url(self) -> str:
        """Retorna a URL de autorização OAuth, gerando e armazenando o parâmetro state se ainda não existir."""
        if self.state is None:
            self.state = secrets.token_urlsafe(16) # Gera um state seguro
        return f"{self.auth_url_base}?client_id={self.client_id}&redirect_uri={self.redirect_uri}&response_type=code&state={self.state}"
    
    def exchange_code_for_token(self, code: str) -> bool:
        """Troca o código de autorização por tokens de acesso."""
        try:
            # 1. Criar o cabeçalho Authorization corretamente
            client = f"{self.client_id}:{self.client_secret}"
            auth_header = base64.b64encode(client.encode()).decode()
            
            headers = {
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            # 2. Enviar o body como form-data (não JSON)
            payload = {
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': self.redirect_uri
            }
            
            response = requests.post(self.config.TOKEN_URL, data=payload, headers=headers, timeout=self.config.REQUEST_TIMEOUT)
            
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

# =# ============================================================================
# 15. EXECUÇÃO PRINCIPAL
# ============================================================================

class AutomationOrchestrator:
    """Orquestrador principal que coordena todas as operações."""
    
    def __init__(self, config: Config):
        self.config = config
        self.auth = BlingAuth(config)
        self.api_client = BlingAPIClient(config)
        self.component_config = ComponentConfigManager(config.COMPONENT_CONFIG_FILE)
        # self.needs_manager = PurchaseNeedsManager() # Removido: Funcionalidade de Needs desativada
        self.stats = ProcessingStats()
        
        # Estado
        self.kits: List[Kit] = []
        self.products: List[Dict[str, Any]] = [] # Novo atributo para armazenar todos os produtos não-variação
        self.is_running: bool = False
        self.lock = Lock()
        
        # Inicia o carregamento dos dados em uma thread separada
        Thread(target=self.load_data_worker, daemon=True).start()
    
    def load_data_worker(self):
        """Worker que carrega os dados iniciais (kits, produtos e estoque) em loop."""
        while True:
            try:
                token = self.auth.get_valid_token()
                if token:
                    self._load_products_and_kits(token)
                    logger.info("Dados iniciais carregados com sucesso.")
                else:
                    logger.warning("Não foi possível carregar dados: Token de acesso indisponível.")
                
                # Espera 1 hora antes de recarregar
                time.sleep(3600)
                
            except Exception as e:
                logger.error(f"Erro no worker de carregamento de dados: {e}")
                time.sleep(60) # Espera 1 minuto em caso de erro
                
    def load_data(self) -> bool:
        """Função de carregamento de dados que será removida ou adaptada, mantida por compatibilidade."""
        logger.warning("load_data() foi substituída por load_data_worker em uma thread separada.")
        return True # Retorna True para não bloquear o startup.
    
    def _load_products_and_kits(self, access_token: str):
        """Carrega todos os produtos e kits do Bling, aplicando o filtro de variações."""
        logger.info("Carregando produtos e kits do Bling...")
        self.kits.clear()
        self.products.clear()
        kits_loaded = 0
        products_loaded = 0
        page = 1
        
        while True:
            try:
                # Busca produtos da API
                response_data = self.api_client.get_products(access_token, page=page, limit=100)
                
                products_raw = response_data.get('data', [])
                
                if not products_raw:
                    break
                
                # Extrai a lista de produtos
                products_list = [p.get('produto', {}) for p in products_raw]
                
                # 1. Aplica o filtro de variações
                products_filtered = filtrar_variacoes(products_list)
                
                for product in products_filtered:
                    
                    if self._is_kit_product(product):
                        kit = self._create_kit_from_product(product)
                        if kit:
                            self.kits.append(kit)
                            kits_loaded += 1
                    else:
                        # Armazena produtos que não são kits (e já foram filtrados de variações)
                        self.products.append(product)
                        products_loaded += 1
                
                page += 1
                
                # Delay entre páginas para não sobrecarregar a API
                time.sleep(self.config.DELAY_BETWEEN_BATCHES)
                
            except Exception as e:
                logger.error(f"Erro ao carregar página {page} de produtos/kits: {e}")
                break
        
        logger.info(f"{kits_loaded} kits e {products_loaded} produtos carregados.")
    
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
            unit = comp_data.get('unidade', '') # Adicionando a unidade
            
            if not sku:
                return None
            
            # Busca configurações locais do componente
            config = self.component_config.get_component_config(sku)
            
            component = Component(
                sku=sku,
                name=name,
                qty=qty,
                unit=unit, # Adicionando a unidade
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
        """Atualiza o estoque atual de todos os componentes (Funcionalidade desativada)."""
        logger.warning("Atualização de estoque de componentes desativada.")
        pass
    
    def _update_component_stock_by_sku(self, sku: str, current_stock: int):
        """Atualiza o estoque atual de um componente específico em todos os kits."""
        for kit in self.kits:
            for component in kit.components:
                if component.sku == sku:
                    component.current_stock = current_stock
    
    def run_purchase_check(self, create_orders: bool = False) -> bool:
        """Funcionalidade de verificação de necessidades de compra desativada."""
        logger.warning("A funcionalidade de verificação de necessidades de compra está desativada.")
        return False

    def get_all_products(self) -> List[Dict[str, Any]]:
        """Retorna a lista de todos os produtos carregados (não-variações)."""
        return self.products

    def get_all_kits(self) -> List[Kit]:
        """Retorna a lista de todos os kits carregados."""
        return self.kits

    def get_kit_by_sku(self, sku: str) -> Optional[Kit]:
        """Busca um kit pelo SKU."""
        for kit in self.kits:
            if kit.sku == sku:
                return kit
        return None
    
    def _check_kit_needs(self, kit: Kit):
        """Funcionalidade de verificação de necessidades de compra desativada."""
        pass
    
    def _create_purchase_orders(self, access_token: str):
        """Funcionalidade de criação de ordens de compra desativada."""
        pass
    
    def _build_purchase_order_data(self, supplier: str, needs: List[Any]) -> Dict[str, Any]:
        """Funcionalidade de construção de dados de ordem de compra desativada."""
        return {}
    
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
                        
                        # self._check_kit_needs(kit) # Removido: Funcionalidade de Needs desativada
                        
                        self.stats.success += 1
                        
                    except Exception as e:
                        self.stats.failed += 1
                        logger.error(f"Erro ao processar kit {kit.sku}: {e}")
                
                # Delay entre lotes
                if i + batch_size < len(kits):
                    time.sleep(self.config.DELAY_BETWEEN_BATCHES)
            
            # # Cria POs se necessário
            # if create_orders:
            #     self._create_purchase_orders(token) # Removido: Funcionalidade de Needs desativada
            
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
# 10. INSTÂNCIAS GLOBAIS
# ============================================================================

# Configuração
config = Config()

# Orquestrador principal
orchestrator = AutomationOrchestrator(config)

# Autenticação (referência para compatibilidade)
auth = orchestrator.auth

# ============================================================================
# 11. DECORADORES
# ============================================================================

def token_required(f):
    """Decorador para verificar se o token de acesso está disponível."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not orchestrator.auth.is_authenticated():
            return jsonify({
                "status": "not_authenticated",
                "message": "Authorize the application via OAuth"
            }), 401
        
        # Passa o token para a função decorada, se necessário
        return f(token=orchestrator.auth.access_token, *args, **kwargs)
    return decorated

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
            <h1 class="mb-4 text-center">Bling Automação Dashboard</h1>
        <script>
            {% raw %}
            
            document.getElementById('search-product-button').addEventListener('click', buscarProdutos);
            document.getElementById('product-search-sku').addEventListener('keypress', function(e) {
                if (e.key === 'Enter') {
                    buscarProdutos();
                }
            });
    
            function showElement(id) { document.getElementById(id).classList.remove('hidden'); }
            function hideElement(id) { document.getElementById(id).classList.add('hidden'); }
    
            function exibirErro(mensagem) {
                hideElement('product-details-container');
                document.getElementById('search-results-list').innerHTML = `<div class="alert alert-danger" role="alert">${mensagem}</div>`;
            }
    
            function limparDetalhes() {
                document.getElementById('search-results-list').innerHTML = '';
                hideElement('product-details-container');
            }
    
            async function buscarProdutos() {
                const termo = document.getElementById('product-search-sku').value.trim();
                if (!termo) {
                    exibirErro("Por favor, digite um SKU ou nome.");
                    return;
                }
    
                limparDetalhes();
                document.getElementById('search-results-list').innerHTML = '<div class="alert alert-info" role="alert">Buscando produtos...</div>';
    
                try {
                    const response = await fetch(`/api/product/search?q=${termo}`);
                    const results = await response.json();
                    
                    document.getElementById('search-results-list').innerHTML = ''; // Limpa o "Buscando..."
    
                    if (!results || results.length === 0) {
                        exibirErro("Nenhum produto encontrado com o termo fornecido.");
                        return;
                    }
    
                    // Exibe a lista de resultados
                    results.forEach(p => {
                        const item = document.createElement('a');
                        item.href = "#";
                        item.className = "list-group-item list-group-item-action";
                        item.innerHTML = `<strong>${p.nome || 'Sem Nome'}</strong> <span class="badge bg-secondary">${p.sku || 'N/D'}</span> <span class="badge bg-info">${p.formato || 'Produto'}</span>`;
                        item.addEventListener('click', (e) => {
                            e.preventDefault();
                            exibirDetalhesProduto(p);
                        });
                        document.getElementById('search-results-list').appendChild(item);
                    });
    
                    // Exibe o primeiro resultado automaticamente
                    exibirDetalhesProduto(results[0]);
    
                } catch (error) {
                    console.error('Erro ao buscar produtos:', error);
                    exibirErro("Ocorreu um erro ao comunicar com a API.");
                }
            }
            
            function exibirDetalhesProduto(p) {
                const container = document.getElementById('product-details-container');
                container.innerHTML = ''; // Limpa o conteúdo anterior
                
                // Cria a estrutura de detalhes (a mesma que estava no template original)
                const detailsHTML = `
                    <div id="productDetails" class="product-card">
                        <div class="row">
                            <div class="col-md-4 text-center">
                                <img id="imgProduto" src="${p.imagemURL || '/placeholder.png'}" alt="Imagem do Produto" class="product-image">
                            </div>
                            <div class="col-md-8">
                                <h2 id="nome" class="mb-3">${p.nome || "Sem nome"}</h2>
                                <div class="product-detail"><strong>Código:</strong> <span id="codigo">${p.sku || "N/D"}</span></div>
                                <div class="product-detail"><strong>Tipo:</strong> <span id="tipo">${p.tipo || "N/D"}</span></div>
                                <div class="product-detail"><strong>Situação:</strong> <span id="situacao">${p.situacao || "N/D"}</span></div>
                                <div class="product-detail"><strong>Formato:</strong> <span id="formato">${p.formato || "N/D"}</span></div>
                                <div class="product-detail"><strong>Preço:</strong> <span id="preco">${p.preco ? \`R$ \${parseFloat(p.preco).toFixed(2).replace('.', ',')}\` : "N/D"}</span></div>
                                <div class="product-detail"><strong>Preço Custo:</strong> <span id="precoCusto">${p.formato !== 'Kit' ? (p.precoCusto ? \`R$ \${parseFloat(p.precoCusto).toFixed(2).replace('.', ',')}\` : "N/D") : "N/A (Kit)"}</span></div>
                                <div class="product-detail"><strong>Estoque:</strong> <span id="estoque">${p.formato !== 'Kit' ? (p.estoque?.saldoVirtualTotal ?? "0") : "N/A (Kit)"}</span></div>
                            </div>
                        </div>
                        <div id="estrutura" class="mt-4 ${p.formato !== 'Kit' ? 'hidden' : ''}">
                            <h4>Estrutura do Kit</h4>
                            <table class="table table-sm table-bordered">
                                <thead>
                                    <tr>
                                        <th>SKU</th>
                                        <th>Nome</th>
                                        <th>Qtd.</th>
                                        <th>Un.</th>
                                    </tr>
                                </thead>
                                <tbody id="estrutura-body">
                                    ${p.estrutura && p.estrutura.length > 0 ? p.estrutura.map(comp => `
                                        <tr>
                                            <td>${comp.sku}</td>
                                            <td>${comp.nome}</td>
                                            <td>${comp.quantidade}</td>
                                            <td>${comp.unidade}</td>
                                        </tr>
                                    `).join('') : `
                                        <tr><td colspan="4">Nenhum componente listado.</td></tr>
                                    `}
                                </tbody>
                            </table>
                        </div>
                        <div id="descricao">
                            <h4>Descrição</h4>
                            ${p.descricaoCurta || "Sem descrição."}
                        </div>
                    </div>
                `;
                
                container.innerHTML = detailsHTML;
                showElement('product-details-container');
            }
            
            // Função de busca de imagem (para ser usada no frontend)
            async function getProductImage(productId) {
                // Como a rota de busca já traz a imagemURL, esta função não é mais necessária
                // Mas se fosse, seria implementada aqui.
                return null;
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
            if expected_state is not None and (not received_state or received_state != expected_state):
                # Se o expected_state existe, mas o recebido não bate, é um erro de segurança.
                return render_template_string(ERROR_TEMPLATE, message="Erro de Segurança: Parâmetro 'state' inválido ou ausente."), 400
            
            if expected_state is None and received_state is not None:
                # Se o expected_state é None (servidor reiniciou), aceitamos o state recebido e seguimos.
                # Isso é um workaround para ambientes stateless como o Render.
                logger.warning("State esperado não encontrado (servidor reiniciou?). Aceitando o state recebido.")
            
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

        # 3. Rotas de Dado        @self.app.route("/api/all_products", methods=["GET"])
        @token_required
        def api_all_products(token):
            """Retorna a lista de todos os produtos (não-variações)."""
            # A lista self.orchestrator.products já está filtrada de variações
            return jsonify(self.orchestrator.products)

        # 3. Rotas de Dados
        @self.app.route("/api/all_products", methods=["GET"])
        @token_required
        def api_all_products(token):
            """Retorna a lista de todos os produtos carregados (não-variações)."""
            # A lista self.orchestrator.products já está filtrada de variações
            return jsonify(self.orchestrator.products)

        @self.app.route('/api/product/search', methods=["GET"])
        @token_required
        def api_product_search(token):
            """Busca produtos por SKU ou nome na lista de produtos carregados e retorna uma lista."""
            termo = request.args.get("q")
            
            if not termo:
                return jsonify([])

            # 1. Busca na lista de produtos (não-kits e não-variações)
            products_found = buscar_produtos_por_sku_ou_nome(self.orchestrator.products, termo)
            
            # 2. Busca na lista de kits
            kits_found_raw = buscar_produtos_por_sku_ou_nome(self.orchestrator.kits, termo)
            
            # Processa os kits encontrados para incluir a estrutura
            kits_found = []
            for kit_raw in kits_found_raw:
                kit_achado = self.orchestrator.get_kit_by_sku(kit_raw.get("codigo"))
                if kit_achado:
                    kit_data = {
                        "id": kit_raw.get("id"),
                        "sku": kit_achado.sku,
                        "nome": kit_achado.name,
                        "tipo": "Kit/Composto",
                        "situacao": "Ativo", # Assumindo ativo para kits carregados
                        "formato": "Kit",
                        "preco": kit_achado.price,
                        "estrutura": [{'sku': c.sku, 'nome': c.name, 'quantidade': c.qty, 'unidade': c.unit} for c in kit_achado.components]
                    }
                    kits_found.append(kit_data)
            
            # Combina os resultados e formata os produtos simples
            all_results = []
            
            # Adiciona produtos simples formatados
            for achado in products_found:
                all_results.append({
                    "id": achado.get("id"),
                    "sku": achado.get("codigo"),
                    "nome": achado.get("nome"),
                    "tipo": achado.get("tipo"),
                    "situacao": achado.get("situacao"),
                    "formato": achado.get("formato"),
                    "preco": achado.get("preco"),
                    "precoCusto": achado.get("precoCusto"),
                    "imagemURL": achado.get("imagemURL"),
                    "estoque": achado.get("estoque"),
                    "descricaoCurta": achado.get("descricaoCurta")
                })
                
            # Adiciona kits processados
            all_results.extend(kits_found)
            
            # TODO: Adicionar busca de imagem para produtos simples e kits
            # A busca de imagem será feita no frontend para cada item da lista.
            
            return jsonify(all_results)

        @self.app.route('/api/kits', methods=["GET"])
        @token_required
        def api_kits(token):
            """Retorna a lista de kits com a estrutura simplificada de componentes."""
            kits_data = []
            for k in self.orchestrator.kits:
                kit = {
                    "sku": k.sku,
                    "nome": k.name,
                    "preco": k.price,
                    "componentes": [
                        {
                            "sku": c.sku,
                            "nome": c.name,
                            "quantidade": c.qty,
                            "unidade": c.unit
                        } for c in k.components
                    ]
                }
                kits_data.append(kit)
            
            return jsonify(kits_data) # Retorna a lista diretamente, sem a chave "kits" extra.

            # 4. Webhook
            @self.app.route("/webhook/bling", methods=["POST"])
            def webhook_bling():
                """
                Webhook do Bling. Apenas loga o payload e retorna 200 OK.
                Toda a lógica de processamento de estoque/compra foi removida.
                """
                try:
                    data = request.get_json(silent=True)
                except Exception:
                    data = None
    
                logger.info(f"WEBHOOK RECEBIDO: {data}")
    
                # Retorna 200 OK imediatamente, conforme instruído.
                return jsonify({"status": "ok", "message": "Webhook recebido e logado."}), 200

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
# 14. TEMPLATES HTML
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
                            <h5 class="card-title">Buscar Produto por SKU ou Nome</h5>
                            <div class="input-group mb-3">
                                <input type="text" class="form-control" id="product-search-sku" placeholder="Digite o SKU ou Nome do produto (ex: KIT-001 ou Camiseta)">
                                <button class="btn btn-primary" type="button" id="search-product-button">Buscar</button>
                            </div>
                            <div id="product-search-results" class="mt-4">
                                <p class="text-muted">Use o campo acima para buscar um produto e ver seus detalhes e componentes.</p>
                                
                                <!-- Novo container para a lista de resultados -->
                                <div id="search-results-list" class="list-group mt-3">
                                    <!-- Resultados da busca serão inseridos aqui -->
                                </div>
                                
                                <!-- Container para os detalhes do produto selecionado -->
                                <div id="product-details-container" class="mt-4 hidden">
                                    <!-- Detalhes do produto selecionado serão inseridos aqui -->
                                </div>
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
# 12. WEB SERVER (FLASK)
# ============================================================================
if __name__ == "__main__":
    run_cli()