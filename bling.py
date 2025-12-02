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
        datefmt='%Y-%m-%dT%H:%M:%S'
    ))
    
    # Handler de erro separado
    error_logger = logging.getLogger('error_logger')
    error_logger.setLevel(logging.ERROR)
    error_file_handler = logging.handlers.RotatingFileHandler(
        Config.ERROR_LOG_FILE, maxBytes=1024*1024*5, backupCount=5, encoding='utf-8'
    )
    error_file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S'
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

    def _save_config(self, data: Dict[str, Any]):
        """Salva a configuração no arquivo."""
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except IOError as e:
            logger.error(f"Erro ao salvar {self.file_path}: {e}")
            error_logger.error(f"Erro ao salvar {self.file_path}: {e}")

    def apply_config_to_component(self, component: Component) -> Component:
        """Aplica as configurações locais (defaults e específicas) a um Component."""
        
        # 1. Aplica defaults
        component.supplier = self.defaults.get('supplier', component.supplier)
        component.lead_time_days = self.defaults.get('lead_time_days', component.lead_time_days)
        component.min_stock = self.defaults.get('min_stock', component.min_stock)
        
        # 2. Sobrescreve com configurações específicas do SKU
        sku_config = self.components_map.get(component.sku)
        if sku_config:
            component.supplier = sku_config.get('supplier', component.supplier)
            component.lead_time_days = sku_config.get('lead_time_days', component.lead_time_days)
            component.min_stock = sku_config.get('min_stock', component.min_stock)
            component.unit_cost = sku_config.get('unit_cost', component.unit_cost)
            
        return component

# ============================================================================
# 1. AUTENTICAÇÃO OAUTH 2.0
# ============================================================================

class BlingAuth:
    """Gerencia o fluxo OAuth 2.0 e a persistência de tokens."""
    
    def __init__(self, config: Config):
        self.config = config
        self.token_url = config.TOKEN_URL
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.expires_at: Optional[datetime] = None
 # Re-adicionado para corrigir AttributeError. Nota: Em ambientes multi-processo (Gunicorn), pode ser necessário um lock de processo (ex: multiprocessing.Lock) ou um lock distribuído.
        
    def _save_tokens(self):
        """Persiste os tokens e a data de expiração no arquivo tokens.json de forma atômica."""
        # Remove o lock de thread, pois não funciona entre processos (workers do Gunicorn).
        # A escrita é feita para um arquivo temporário e depois renomeada para garantir atomicidade.
        data = {
            'access_token': self.access_token,
            'refresh_token': self.refresh_token,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None
        }
        
        temp_file = self.config.TOKENS_FILE.with_suffix('.tmp')
        
        try:
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            
            # Renomeia o arquivo temporário para o arquivo final (operação atômica)
            temp_file.rename(self.config.TOKENS_FILE)
            logger.info("Tokens salvos com sucesso.")
        except IOError as e:
            logger.error(f"Erro ao salvar tokens: {e}")
            error_logger.error(f"Erro ao salvar tokens: {e}")

    def load_tokens(self) -> bool:
        """Carrega os tokens do arquivo tokens.json."""
        # Remove o lock de thread, pois não funciona entre processos (workers do Gunicorn).
        if self.config.TOKENS_FILE.exists():
            try:
                with open(self.config.TOKENS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.access_token = data.get('access_token')
                    self.refresh_token = data.get('refresh_token')
                    expires_at_str = data.get('expires_at')
                    if expires_at_str:
                        self.expires_at = datetime.fromisoformat(expires_at_str)
                    
                    if self.access_token and self.refresh_token:
                        logger.info("Tokens carregados com sucesso.")
                        return True
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Erro ao carregar tokens: {e}")
                error_logger.error(f"Erro ao carregar tokens: {e}")
        
        logger.warning("Tokens não encontrados ou inválidos. Necessário autenticar.")
        return False

    def is_token_valid(self) -> bool:
        """Verifica se o token de acesso é válido e não expirou (com margem de 5 minutos)."""

        if not self.access_token or not self.expires_at:
            return False
        # Verifica se o token expira nos próximos 5 minutos
        return self.expires_at > datetime.now() + timedelta(minutes=5)

    def get_authorization_url(self) -> str:
        """Retorna a URL para iniciar o fluxo de autorização OAuth."""
        return (
            f"https://www.bling.com.br/Api/v3/oauth/authorize?"
            f"response_type=code&"
            f"client_id={self.config.CLIENT_ID}&"
            f"state=random_string_for_security&"
            f"redirect_uri={self.config.REDIRECT_URI}"
        )

    def _get_basic_auth_header(self) -> Dict[str, str]:
        """Gera o cabeçalho Basic Authentication para o token endpoint."""
        auth_string = f"{self.config.CLIENT_ID}:{self.config.CLIENT_SECRET}"
        encoded_auth = base64.b64encode(auth_string.encode()).decode()
        return {"Authorization": f"Basic {encoded_auth}"}

    def exchange_code_for_token(self, code: str):
        """Troca o código de autorização por tokens de acesso e refresh."""
        logger.info("Trocando código de autorização por tokens...")
        
        payload = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': self.config.REDIRECT_URI # O Bling V3 exige o redirect_uri no payload
        }
        
        try:
            response = requests.post(
                self.token_url,
                data=payload,
                headers=self._get_basic_auth_header(),
                timeout=self.config.REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()
            
            self.access_token = data['access_token']
            self.refresh_token = data['refresh_token']
            # O Bling retorna expires_in em segundos (padrão 3600s = 1h)
            expires_in = data.get('expires_in', 3600)
            self.expires_at = datetime.now() + timedelta(seconds=expires_in)
            self._save_tokens()
            
            logger.info("Autenticação OAuth concluída com sucesso!")
            return True
            
        except RequestException as e:
            msg = f"Erro ao trocar código por token: {e}"
            logger.error(msg)
            logger.error(msg)
            error_logger.error(msg)
            raise BlingAuthError(msg) from e

    def refresh_access_token(self):
        """Renova o token de acesso usando o refresh token."""
        logger.info("Tentando renovar o token de acesso...")
        
        if not self.refresh_token:
            raise BlingAuthError("Refresh token não disponível. Necessário reautenticar.")
            
        payload = {
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token,
            'redirect_uri': self.config.REDIRECT_URI # O Bling V3 exige o redirect_uri no payload
        }
        
        try:
            response = requests.post(
                self.token_url,
                data=payload,
                headers=self._get_basic_auth_header(),
                timeout=self.config.REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()
            
            self.access_token = data['access_token']
            # O refresh token pode mudar, então atualizamos
            self.refresh_token = data.get('refresh_token', self.refresh_token)
            expires_in = data.get('expires_in', 3600)
            self.expires_at = datetime.now() + timedelta(seconds=expires_in)
            self._save_tokens()
            
            logger.info("Token de acesso renovado com sucesso!")
            return True
            
        except RequestException as e:
            msg = f"Erro ao renovar token: {e}. Necessário reautenticar."
            logger.error(msg)
            logger.error(msg)
            error_logger.error(msg)
            raise BlingAuthError(msg) from e

# ============================================================================
# 4. CLASSE BlingAPI COMPLETA
# ============================================================================

class BlingAPI:
    """Cliente robusto para a API do Bling, com retry e renovação de token."""
    
    def __init__(self, auth: BlingAuth, config: Config):
        self.auth = auth
        self.config = config
        self.base_url = config.BLING_API_URL
        self._stock_cache: Dict[int, Dict[str, Any]] = {} # {product_id: {'stock': int, 'expiry': datetime}}
        self._cache_ttl = timedelta(minutes=5)
        
    def _request_with_retry(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """
        Executa uma requisição HTTP com retry e backoff exponencial.
        Trata erros 401 com renovação automática de token.
        """
        url = f"{self.base_url}/{endpoint}"
        
        for attempt in range(self.config.MAX_RETRIES):
            try:
                # 1. Verifica e renova o token se necessário
                if not self.auth.is_token_valid():
                    self.auth.refresh_access_token()
                
                # 2. Adiciona cabeçalhos de autorização
                headers = kwargs.pop('headers', {})
                headers['Authorization'] = f'Bearer {self.auth.access_token}'
                headers['Accept'] = 'application/json'
                kwargs['headers'] = headers
                
                # 3. Executa a requisição
                response = requests.request(
                    method, 
                    url, 
                    timeout=self.config.REQUEST_TIMEOUT, 
                    **kwargs
                )
                
                # 4. Trata status codes
                if response.status_code == 200 or response.status_code == 201:
                    return response.json()
                
                # 5. Trata 401 (Não Autorizado) - Força a renovação e tenta novamente
                if response.status_code == 401:
                    logger.warning("Token expirado ou inválido (401). Tentando renovar...")
                    self.auth.refresh_access_token()
                    # Força a próxima iteração do loop com o novo token
                    continue 
                
                # 6. Trata outros erros da API
                response.raise_for_status()
                
            except BlingAuthError:
                # Se a renovação falhar, o erro é fatal
                raise
            except RequestException as e:
                # Trata erros de conexão, timeout, etc.
                if attempt < self.config.MAX_RETRIES - 1:
                    delay = self.config.BASE_DELAY * (2 ** attempt)
                    logger.warning(f"Tentativa {attempt + 1} falhou. Erro: {e}. Tentando novamente em {delay:.2f}s...")
                    time.sleep(delay)
                else:
                    msg = f"Falha na requisição após {self.config.MAX_RETRIES} tentativas para {url}: {e}"
                    error_logger.error(msg)
                    raise BlingAPIError(msg) from e
            except Exception as e:
                msg = f"Erro inesperado na requisição para {url}: {e}"
                error_logger.error(msg)
                raise BlingAPIError(msg) from e
                
        # Se o loop terminar sem sucesso (o que não deve acontecer se o 401 for tratado)
        raise BlingAPIError(f"Falha desconhecida na requisição para {url}")

    def get_product_by_sku(self, sku: str) -> Optional[Dict[str, Any]]:
        """Busca um produto pelo SKU."""
        try:
            response = self._request_with_retry(
                'GET', 
                'produtos', 
                params={'codigo': sku}
            )
            # A API retorna uma lista, pegamos o primeiro
            return response.get('data', [{}])[0].get('produto')
        except BlingAPIError as e:
            logger.error(f"Erro ao buscar produto SKU {sku}: {e}")
            return None

    def get_product_stock(self, product_id: int) -> int:
        """Busca o estoque atual de um produto pelo ID, usando cache com TTL de 5 minutos."""
        
        # 1. Verifica o cache
        if product_id in self._stock_cache:
            cache_entry = self._stock_cache[product_id]
            if datetime.now() < cache_entry['expiry']:
                return cache_entry['stock']
            # Cache expirado, remove
            del self._stock_cache[product_id]
            
        # 2. Busca na API
        try:
            response = self._request_with_retry(
                'GET', 
                f'estoques/produtos/{product_id}'
            )
            # A API retorna o estoque em um formato específico
            stock = int(response.get('data', {}).get('estoque', {}).get('estoqueAtual', 0))
            
            # 3. Atualiza o cache
            self._stock_cache[product_id] = {
                'stock': stock,
                'expiry': datetime.now() + self._cache_ttl
            }
            
            return stock
        except BlingAPIError as e:
            logger.error(f"Erro ao buscar estoque do produto ID {product_id}: {e}")
            return 0

    def get_all_kits_and_components(self, config_manager: ComponentConfigManager) -> List[Kit]:
        """Busca todos os Kits e seus Componentes, aplicando configurações locais."""
        logger.info("Buscando todos os Kits e Componentes no Bling...")
        kits: List[Kit] = []
        pagina = 1
        MAX_PAGES = 100 # Limite de segurança para evitar loop infinito em caso de bug na API
        
        while pagina <= MAX_PAGES:
            try:
                response = self._request_with_retry(
                    'GET', 
                    'produtos', 
                    params={'tipo': 'P', 'pagina': pagina}
                )
                
                data = response.get('data', [])
                if not data:
                    break # Fim da paginação
                
                pagina += 1 # Incrementa a página para a próxima iteração
                
                for item in data:
                    product = item.get('produto', {})
                    if product.get('tipo') == 'P' and product.get('estrutura'):
                        
                        componentes: List[Component] = []
                        for comp_item in product['estrutura'].get('componentes', []):
                            comp_data = comp_item.get('produto', {})
                            
                            # 1. Cria o objeto Component
                            component = Component(
                                sku=comp_data.get('codigo', 'N/A'),
                                name=comp_data.get('descricao', 'Sem nome'),
                                qty=int(comp_item.get('quantidade', 0)),
                                unit_cost=float(comp_data.get('precoCusto', 0.0))
                            )
                            
                            # 2. Aplica configurações locais (fornecedor, min_stock, lead_time)
                            component = config_manager.apply_config_to_component(component)
                            
                            # 3. Busca estoque atual (requer o ID do produto)
                            product_id = comp_data.get('id')
                            if product_id:
                                component.current_stock = self.get_product_stock(product_id)
                            
                            componentes.append(component)
                            
                        kits.append(Kit(
                            sku=product.get('codigo', 'N/A'),
                            name=product.get('descricao', 'Sem nome'),
                            components=componentes,
                            price=float(product.get('preco', 0.0))
                        ))
                
                pagina += 1
                time.sleep(self.config.DELAY_BETWEEN_BATCHES) # Delay entre batches
                
            except BlingAPIError as e:
                logger.error(f"Erro na paginação de Kits: {e}")
                logger.error(f"Erro na paginação de Kits: {e}")
                break
                
        logger.info(f"Busca de Kits concluída. {len(kits)} Kits encontrados.")
        return kits

    def get_supplier_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Busca um fornecedor pelo nome."""
        try:
            response = self._request_with_retry(
                'GET', 
                'fornecedores', 
                params={'pesquisa': name}
            )
            # A API retorna uma lista, tentamos encontrar uma correspondência exata
            for item in response.get('data', []):
                supplier = item.get('fornecedor', {})
                if supplier.get('nome') == name:
                    return supplier
            return None
        except BlingAPIError as e:
            logger.error(f"Erro ao buscar fornecedor {name}: {e}")
            return None

    def create_production_order(self, kit_sku: str, quantity: int) -> Optional[int]:
        """Cria uma Ordem de Produção (OP) no Bling."""
        logger.info(f"Criando OP para Kit {kit_sku} (Qtd: {quantity})...")
        
        payload = {
            "data": {
                "produto": {
                    "codigo": kit_sku
                },
                "quantidade": quantity
            }
        }
        
        try:
            response = self._request_with_retry(
                'POST', 
                'producao/ordens', 
                json=payload
            )
            op_id = response.get('data', {}).get('id')
            if op_id:
                logger.info(f"OP criada com sucesso! ID: {op_id}")
                logger.info(f"OP criada: ID {op_id} para Kit {kit_sku} (Qtd: {quantity})")
                return op_id
            else:
                raise BlingAPIError(f"Resposta da API não contém ID da OP: {response}")
        except BlingAPIError as e:
            logger.error(f"Falha ao criar OP para Kit {kit_sku}: {e}")
            logger.error(f"Falha ao criar OP para Kit {kit_sku}: {e}")
            return None

    def create_purchase_order(self, supplier_name: str, items: List[PurchaseNeed]) -> Optional[int]:
        """Cria uma Ordem de Compra (PO) no Bling."""
        logger.info(f"Criando PO para Fornecedor {supplier_name} com {len(items)} itens...")
        
        supplier = self.get_supplier_by_name(supplier_name)
        if not supplier:
            logger.error(f"Fornecedor '{supplier_name}' não encontrado no Bling. PO não criada.")
            return None
            
        supplier_id = supplier['id']
        
        payload = {
            "data": {
                "fornecedor": {
                    "id": supplier_id
                },
                "itens": [
                    {
                        "produto": {
                            "codigo": item.component_sku
                        },
                        "quantidade": item.quantity_needed,
                        "observacoes": f"Motivo: {item.reason}"
                    }
                    for item in items
                ]
            }
        }
        
        try:
            response = self._request_with_retry(
                'POST', 
                'compras/pedidos', 
                json=payload
            )
            po_id = response.get('data', {}).get('id')
            if po_id:
                logger.info(f"PO criada com sucesso! ID: {po_id} para {supplier_name}")
                logger.info(f"PO criada: ID {po_id} para {supplier_name} com {len(items)} itens.")
                return po_id
            else:
                raise BlingAPIError(f"Resposta da API não contém ID da PO: {response}")
        except BlingAPIError as e:
            logger.error(f"Falha ao criar PO para {supplier_name}: {e}")
            logger.error(f"Falha ao criar PO para {supplier_name}: {e}")
            return None

# ============================================================================
# 5. SISTEMA DE ESTATÍSTICAS
# ============================================================================

class StatisticsManager:
    """Gerencia e coleta estatísticas de execução da automação."""
    
    def __init__(self):
        self.lock = Lock()

        self.reset()
        
    def reset(self):
        """Reseta todas as estatísticas."""
        with self.lock:
            self.success: int = 0
            self.failed: int = 0
            self.ops_created: int = 0
            self.pos_created: int = 0
            self.min_stock_checks: int = 0
            self.start_time: Optional[datetime] = None
            self.end_time: Optional[datetime] = None
            
    def start(self):
        """Inicia a contagem de tempo."""
        with self.lock:
            self.start_time = datetime.now()
            self.end_time = None
            
    def stop(self):
        """Para a contagem de tempo."""
        with self.lock:
            self.end_time = datetime.now()
            
    def increment(self, counter: str, value: int = 1):
        """Incrementa um contador específico."""
        with self.lock:
            if hasattr(self, counter):
                setattr(self, counter, getattr(self, counter) + value)
            
    @property
    def elapsed_time_seconds(self) -> float:
        """Calcula o tempo decorrido em segundos."""
        # Não precisa de lock aqui, pois é uma propriedade que só lê
        # e é chamada dentro de to_dict, que já tem o lock.
        if self.start_time:
            end = self.end_time if self.end_time else datetime.now()
            return (end - self.start_time).total_seconds()
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Retorna as estatísticas em formato de dicionário."""
        with self.lock:
            return {
                'success': self.success,
                'failed': self.failed,
                'ops_created': self.ops_created,
                'pos_created': self.pos_created,
                'min_stock_checks': self.min_stock_checks,
                'elapsed_time_seconds': round(self.elapsed_time_seconds, 2),
                'total_processed': self.success + self.failed
            }

# ============================================================================
# 6. GESTÃO DE COMPRAS (PO)
# ============================================================================

class NeedsManager:
    """Gerencia as necessidades de compra e a criação de Ordens de Compra (POs)."""
    
    def __init__(self, api: BlingAPI, stats: StatisticsManager):
        self.api = api
        self.stats = stats
        # needs: Dict[supplier_name, List[PurchaseNeed]]
        self.needs: Dict[str, List[PurchaseNeed]] = {}
        self.lock = Lock()        
    def reset(self):
        """Limpa todas as necessidades de compra."""
        with self.lock:
            self.needs = {}

    def add_need(self, component: Component, quantity: int, reason: str):
        """Adiciona uma necessidade de compra."""
        with self.lock:
            if quantity <= 0:
                return
            
	        need = PurchaseNeed(
	            component_sku=component.sku,
	            component_name=component.name,
	            quantity_needed=quantity,
	            supplier=component.supplier,
	            lead_time_days=component.lead_time_days,
	            reason=reason
	        )
	        
	        if need.supplier not in self.needs:
	            self.needs[need.supplier] = []
	        self.needs[need.supplier].append(need)
	        logger.info(f"Necessidade adicionada: {need.component_name} ({need.quantity_needed} un.) para {need.supplier}")

    def check_min_stock_needs(self, components: List[Component]):
        """Verifica o estoque mínimo de uma lista de componentes e adiciona necessidades."""
        logger.info("Verificação de Estoque Mínimo")
        
        for component in components:
            self.stats.increment('min_stock_checks')
            
            if component.current_stock < component.min_stock:
                quantity_needed = component.min_stock - component.current_stock
                self.add_need(
                    component, 
                    quantity_needed, 
                    f"Estoque atual ({component.current_stock}) abaixo do mínimo ({component.min_stock})"
                )
                logger.warning(f"ALERTA: {component.name} ({component.sku}) precisa de {quantity_needed} un.")
            else:
                logger.debug(f"Estoque OK: {component.name} ({component.sku}) - {component.current_stock}/{component.min_stock}")

     def generate_purchase_orders(self) -> List[int]:
        """Gera Ordens de Compra (POs) no Bling, agrupando por fornecedor."""
        with self.lock:
            logger.info("Geração de Ordens de Compra (POs)")
            
            if not self.needs:
                logger.info("Nenhuma necessidade de compra pendente.")
                return []
            
            po_ids: List[int] = []
            needs_to_process = self.needs.copy()
            self.needs = {} # Limpa as necessidades após copiar para processamento
            
            for supplier_name, items in needs_to_process.items():
                po_id = self.api.create_purchase_order(supplier_name, items)
                if po_id:
                    po_ids.append(po_id)
                    self.stats.increment('pos_created')
                
        logger.info(f"Geração de POs concluída. {len(po_ids)} PO(s) criada(s).")
        return po_ids

# ============================================================================
# 7. ORQUESTRADOR DE AUTOMAÇÃO
# ============================================================================

class AutomationOrchestrator:
    """Orquestra o fluxo de automação: OPs, verificação de estoque e POs."""
    
    def __init__(self, api: BlingAPI, stats: StatisticsManager, needs_manager: PurchaseNeedsManager, config_manager: ComponentConfigManager, auth: BlingAuth):
        self.api = api
        self.stats = stats
        self.needs_manager = needs_manager
        self.config_manager = config_manager
        self.auth = auth
        self.kits: List[Kit] = []
        self.failed_items: List[Dict[str, Any]] = []
        self.is_running: bool = False

        
    def load_data(self):
        """Carrega todos os kits e componentes do Bling."""
        logger.info("Carregamento Inicial de Dados")
        try:
            self.kits = self.api.get_all_kits_and_components(self.config_manager)
            self.run_purchase_check(force_po_creation=False) # Verifica estoque inicial
            logger.info("Dados carregados e verificação inicial de estoque concluída.")
            return True
        except BlingAuthError:
            logger.error("Falha na autenticação. Não foi possível carregar os dados.")
            return False
        except BlingAPIError as e:
            logger.error(f"Falha ao carregar dados da API: {e}")
            return False

    def process_kits(self, kits_to_process: List[Kit], batch_size: int, check_stock: bool = True, quantity: int = 1) -> Dict[str, Any]:
        """Processa uma lista de kits: cria OPs e verifica estoque de componentes. Implementa processamento em lotes."""
        
        if batch_size <= 0:
            batch_size = 1
        if self.is_running:
            logger.warning("Processamento já em andamento. Ignorando nova requisição.")
            return {"status": "warning", "message": "Processamento já em andamento."}
        
        self.is_running = True
        self.stats.reset()
        self.needs_manager.reset()
        self.failed_items = []
        self.stats.start()
            
        logger.info(f"Iniciando Processamento de {len(kits_to_process)} Kits em Lotes de {batch_size}")
        
        try:
            for i, kit in enumerate(kits_to_process):
                op_id = self.api.create_production_order(kit.sku, quantity)
                
                if op_id:
                    self.stats.increment('ops_created')
                    self.stats.increment('success')
                    
                    if check_stock:
                        # Verifica o estoque de todos os componentes do kit
                        self.needs_manager.check_min_stock_needs(kit.components)
                else:
                    self.stats.increment('failed')
                    self.failed_items.append({
                        "sku": kit.sku,
                        "name": kit.name,
                        "reason": "Falha ao criar Ordem de Produção"
                    })
                
                # Otimização de Rate Limiting: Pausa após cada item para respeitar o limite de requisições.
                # O delay é distribuído pelo tamanho do lote para evitar sleeps longos e desnecessários.
                if self.config.DELAY_BETWEEN_BATCHES > 0:
                    delay_per_item = self.config.DELAY_BETWEEN_BATCHES / batch_size
                    time.sleep(delay_per_item)
                    
            # Após processar todos os kits, gera as POs
            self.needs_manager.generate_purchase_orders()
            
        except BlingAPIError as e:
            msg = f"Erro de API durante o processamento de kits: {e}"
            logger.error(msg)
            error_logger.error(msg)
        except Exception as e:
            msg = f"Erro inesperado durante o processamento de kits: {e}"
            logger.error(msg)
            error_logger.error(msg)
        finally:
            self.stats.stop()
            self.is_running = False
            
        return {"status": "success", "stats": self.stats.to_dict()}

    def run_purchase_check(self, force_po_creation: bool = True):
        """Executa apenas a verificação de estoque e, opcionalmente, a criação de POs."""
        if self.is_running:
            logger.warning("Processamento já em andamento. Ignorando nova requisição.")
            return {"status": "warning", "message": "Processamento já em andamento."}
        
        self.is_running = True
        self.needs_manager.reset()
        self.stats.start()
            
        logger.info("Iniciando Verificação de Estoque e Compras")
        
        try:
            # 1. Coleta todos os componentes únicos de todos os kits
            all_components: Dict[str, Component] = {}
            for kit in self.kits:
                for component in kit.components:
                    all_components[component.sku] = component
            
            # 2. Verifica o estoque mínimo de todos os componentes
            self.needs_manager.check_min_stock_needs(list(all_components.values()))
            
            # 3. Gera as POs se forçado
            if force_po_creation:
                self.needs_manager.generate_purchase_orders()
                
        except BlingAPIError as e:
            msg = f"Erro de API durante a verificação de estoque: {e}"
            logger.error(msg)
            error_logger.error(msg)
        except Exception as e:
            msg = f"Erro inesperado durante a verificação de estoque: {e}"
            logger.error(msg)
            error_logger.error(msg)
        finally:
            self.stats.stop()
            self.is_running = False
            
        logger.info("Verificação Concluída")
        return {"status": "success", "stats": self.stats.to_dict()}

# ============================================================================
# INSTÂNCIAS GLOBAIS
# ============================================================================

# 21. ESTRUTURA DE ARQUIVOS (Garantida pelo setup_logging e ComponentConfigManager)
# 19. CONFIGURAÇÕES (Instância)
config = Config()

# 1. AUTENTICAÇÃO (Instância)
auth = BlingAuth(config)

# 3. CONFIGURAÇÃO DE COMPONENTES (Instância)
config_manager = ComponentConfigManager(config.COMPONENT_CONFIG_FILE)

# 4. CLASSE BlingAPI (Instância)
api = BlingAPI(config, auth)

# 5. SISTEMA DE ESTATÍSTICAS (Instância)
stats_manager = StatisticsManager()

# 6. GESTÃO DE COMPRAS (Instância)
needs_manager = PurchaseNeedsManager(api, stats_manager)

# 7. ORQUESTRADOR DE AUTOMAÇÃO (Instância)
orchestrator = AutomationOrchestrator(api, stats_manager, needs_manager, config_manager, auth)




# ============================================================================
# 14. DEPLOY E SERVIDOR (Estrutura da Classe WebServer)
# ============================================================================

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
        @self.app.route('/')
        @self.app.route('/dashboard')
        def dashboard():
            auth_url = self.orchestrator.auth.get_authorization_url()
            return render_template_string(DASHBOARD_TEMPLATE, auth_url=auth_url)

        @self.app.route('/callback')
        def callback():
            code = request.args.get('code')
            error = request.args.get('error')
            
            if error:
                return render_template_string(ERROR_TEMPLATE, message=f"Erro de Autorização: {error}")
            
            if code:
                try:
                    self.orchestrator.auth.exchange_code_for_token(code)
                    return render_template_string(SUCCESS_TEMPLATE, message="Autenticação concluída com sucesso!")
                except BlingAuthError as e:
                    return render_template_string(ERROR_TEMPLATE, message=f"Falha na troca de código: {e}")
            
            return render_template_string(ERROR_TEMPLATE, message="Parâmetros de callback inválidos.")

        # 2. Rotas de Status e Estatísticas
        @self.app.route('/api/status')
        def api_status():
            is_valid = self.orchestrator.auth.is_token_valid()
            return jsonify({
                "authenticated": is_valid,
                "auth_url": self.orchestrator.auth.get_authorization_url(),
                "token_expires_at": self.orchestrator.auth.expires_at.isoformat() if self.orchestrator.auth.expires_at else None,
                "data_loaded": True, # Assume True, pois o carregamento é feito por worker/processo
                "is_running": self.orchestrator.is_running
            })

        @self.app.route('/api/stats')
        def api_stats():
            return jsonify(self.orchestrator.stats.to_dict())

        # 3. Rotas de Dados
        @self.app.route('/api/produtos')
        def api_produtos():
            sku = request.args.get('sku')
            name = request.args.get('name')
            
            if not sku and not name:
                return jsonify({"error": "Parâmetro 'sku' ou 'name' é obrigatório."}), 400
                
            # A busca por SKU é mais precisa
            if sku:
                product_data = self.orchestrator.api.get_product_by_sku(sku)
            else:
                # A API do Bling V3 não tem uma busca direta por nome que retorne a estrutura.
                # Para simplificar, vamos usar a busca por kits se for um kit, ou buscar o produto.
                # Como a busca por SKU é a mais eficiente e a que o usuário provavelmente usará,
                # vamos focar nela. Se o usuário buscar por nome, ele pode usar a aba Kits.
                return jsonify({"error": "Busca por nome ainda não implementada para estrutura detalhada. Use a aba Kits ou busque por SKU."}), 400
                
            if not product_data:
                return jsonify({"error": "Produto não encontrado."}), 404
                
            # Se for um Kit, retorna a estrutura
            if product_data.get('tipo') == 'P' and product_data.get('estrutura'):
                components = []
                for comp_item in product_data['estrutura'].get('componentes', []):
                    comp_data = comp_item.get('produto', {})
                    components.append({
                        'sku': comp_data.get('codigo', 'N/A'),
                        'name': comp_data.get('descricao', 'Sem nome'),
                        'qty': int(comp_item.get('quantidade', 0)),
                        'unit_cost': float(comp_data.get('precoCusto', 0.0)),
                        'current_stock': self.orchestrator.api.get_product_stock(comp_data.get('id')) if comp_data.get('id') else 0
                    })
                
                return jsonify({
                    "sku": product_data.get('codigo', 'N/A'),
                    "name": product_data.get('descricao', 'Sem nome'),
                    "type": "Kit",
                    "components": components
                })
            
            # Se for um produto simples, retorna os dados básicos
            return jsonify({
                "sku": product_data.get('codigo', 'N/A'),
                "name": product_data.get('descricao', 'Sem nome'),
                "type": "Produto Simples",
                "current_stock": self.orchestrator.api.get_product_stock(product_data.get('id')) if product_data.get('id') else 0
            })

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
        @self.app.route('/webhook/bling', methods=['POST'])
        def webhook_bling():
            try:
                data = request.get_json(force=True)
                event_type = data.get('event') or data.get('tipo') or 'unknown'
                logger.info(f"🪝 Webhook recebido: {event_type}")
                
                # 20. WEBHOOK MELHORADO
                is_order_event = (
                    event_type == 'order.created' or 
                    event_type == 'pedido.pago' or 
                    (data.get('tipo') == 'pedido' and data.get('evento') in ['criado', 'pago'])
                )
                
                is_stock_event = (
                    event_type == 'estoque.atualizado' or
                    (data.get('tipo') == 'estoque' and data.get('evento') == 'atualizado')
                )
                
                if is_order_event:
                    # Extração robusta do pedido_id
                    pedido_id = data.get('id') or data.get('retorno', {}).get('pedidos', [{}])[0].get('pedido', {}).get('id')
                    if pedido_id:
                        logger.info(f"✅ Pedido ID {pedido_id} identificado. Disparando verificação de compras.")
                        # Disparo de run_purchase_check() em Thread
                        Thread(target=self.orchestrator.run_purchase_check, args=(True,), daemon=True).start()
                        return jsonify({'status': 'ok', 'message': f'Pedido {pedido_id} processado. Verificação de compras iniciada.'}), 200
                
                if is_stock_event:
                    logger.info("✅ Evento de estoque atualizado. Disparando verificação de compras.")
                    Thread(target=self.orchestrator.run_purchase_check, args=(True,), daemon=True).start()
                    return jsonify({'status': 'ok', 'message': 'Estoque atualizado. Verificação de compras iniciada.'}), 200
                
                return jsonify({'status': 'ok', 'message': f'Webhook {event_type} recebido e ignorado.'}), 200
            except Exception as e:
                logger.error(f"Erro no webhook: {e}")
                error_logger.error(f"Erro no webhook: {e}")
                return jsonify({'error': str(e)}), 500

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
    
    # 1. Tenta carregar tokens
    if not auth.load_tokens():
        logger.warning("Tokens não carregados. Necessário autenticar via dashboard.")
        return
        
    # 2. Busca kits e componentes (inclui estoque)
    if orchestrator.load_data():
        logger.info("Carregamento de dados em background concluído com sucesso.")
    else:
        logger.error("Falha no carregamento de dados em background.")
        
    

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
            document.getElementById('kpi-checks').textContent = stats.min_stock_checks;
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
                const data = await response.json();
                
                if (response.ok) {
                    renderProductDetails(data);
                } else {
                    resultsDiv.innerHTML = `<p class="text-danger">Erro: ${data.error || 'Produto não encontrado.'}</p>`;
                }
            } catch (error) {
                console.error('Erro ao buscar detalhes do produto:', error);
                resultsDiv.innerHTML = '<p class="text-danger">Erro de conexão ao buscar detalhes do produto.</p>';
            }
        }
        
        function renderProductDetails(data) {
            const resultsDiv = document.getElementById('product-search-results');
            let html = `
                <div class="card bg-light p-3">
                    <h5>Detalhes do Produto: ${data.name} (${data.sku})</h5>
                    <p><strong>Tipo:</strong> ${data.type}</p>
            `;
            
            if (data.type === 'Kit') {
                html += `
                    <p><strong>Componentes Necessários:</strong></p>
                    <div class="table-responsive">
                        <table class="table table-sm table-bordered">
                            <thead>
                                <tr>
                                    <th>SKU</th>
                                    <th>Nome</th>
                                    <th>Qtd.</th>
                                    <th>Estoque Atual</th>
                                    <th>Fornecedor</th>
                                </tr>
                            </thead>
                            <tbody>
                `;
                data.components.forEach(c => {
                    html += `
                        <tr>
                            <td>${c.sku}</td>
                            <td>${c.name}</td>
                            <td>${c.qty}</td>
                            <td>${c.current_stock}</td>
                            <td>${c.supplier}</td>
                        </tr>
                    `;
                });
                html += `
                            </tbody>
                        </table>
                    </div>
                `;
            } else {
                html += `<p><strong>Estoque Atual:</strong> ${data.current_stock}</p>`;
            }
            
            html += `</div>`;
            resultsDiv.innerHTML = html;
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
                logWebSocket.close();
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
        
        if not auth.load_tokens():
            logger.error("Não foi possível carregar tokens. Execute --serve e autentique primeiro.")
            return
            
        if not orchestrator.load_data():
            logger.error("Não foi possível carregar dados do Bling. Verifique a conexão e o token.")
            return
            
        # Processa todos os kits encontrados com quantidade 1 e batch_size padrão
        orchestrator.process_kits(
            orchestrator.kits, 
            batch_size=config.DEFAULT_BATCH_SIZE, 
            check_stock=True, 
            quantity=1
        )
        
    else:
        parser.print_help()

if __name__ == '__main__':
    # 21. ESTRUTURA DE ARQUIVOS (Criação de logs/ garantida pelo setup_logging)
    # 13. JAVASCRIPT FALTANDO (Os intervalos e handlers estão no template)
    # 12. CSS FALTANDO (O CSS está no template)
    
    # O código base já tinha a estrutura de logs em memória, agora está completa.
    # O código base tinha rotas simples, agora estão completas e na classe WebServer.
    
    run_cli()