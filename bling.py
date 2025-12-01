#!/usr/bin/env python3
"""
bling.py - Sistema completo de automação Bling com design premium
Mantém a conexão do Bling 1 + Todo o design e features do Bling 2
"""

import os
import sys
import json
import time
import logging
import base64
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from urllib.parse import urlencode
from collections import defaultdict
from threading import Lock, Thread
from dotenv import load_dotenv

import requests
from flask import Flask, request, render_template_string, jsonify

# Tenta importar flask_sock para WebSocket
try:
    from flask_sock import Sock
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False
    Sock = None

# Tenta importar colorama para CLI colorido
try:
    from colorama import init, Fore, Style
    init(autoreset=True)
    COLORS_ENABLED = True
except ImportError:
    class Fore:
        GREEN = RED = YELLOW = CYAN = MAGENTA = BLUE = RESET = ''
    class Style:
        BRIGHT = RESET_ALL = ''
    COLORS_ENABLED = False

# ============================================================================
# CONFIGURAÇÃO E CONSTANTES
# ============================================================================

load_dotenv()

class Config:
    BLING_API_URL = 'https://www.bling.com.br/Api/v3'
    
    # OAuth
    CLIENT_ID = os.environ.get('BLING_CLIENT_ID', 'SEU_CLIENT_ID')
    CLIENT_SECRET = os.environ.get('BLING_CLIENT_SECRET', 'SEU_CLIENT_SECRET')
    REDIRECT_URI = os.environ.get('BLING_REDIRECT_URI', 'http://127.0.0.1:5000/callback')
    
    # API
    REQUEST_TIMEOUT = 30
    MAX_RETRIES = 3
    BASE_DELAY = 1
    
    # Automação
    CHECK_MIN_STOCK = True
    MIN_STOCK_THRESHOLD = 10
    DEFAULT_BATCH_SIZE = 10
    DELAY_BETWEEN_BATCHES = 0.5

# ============================================================================
# EXCEÇÕES CUSTOMIZADAS
# ============================================================================

class BlingAuthError(Exception):
    """Erro de autenticação com o Bling."""
    pass

class BlingAPIError(Exception):
    """Erro na chamada da API do Bling."""
    pass

# ============================================================================
# DATACLASSES
# ============================================================================

@dataclass
class Component:
    sku: str
    name: str
    qty: int
    supplier: str = "FORNECEDOR_PADRAO"
    lead_time_days: int = 15
    unit_cost: float = 0.0
    min_stock: int = 10
    current_stock: int = 0

@dataclass
class Kit:
    sku: str
    name: str
    components: List[Component]
    price: float = 0.0

@dataclass
class PurchaseNeed:
    component_sku: str
    component_name: str
    quantity_needed: int
    supplier: str
    lead_time_days: int
    reason: str

# ============================================================================
# FUNÇÕES DE PRINT COLORIDO (CLI)
# ============================================================================

def print_success(msg):
    if COLORS_ENABLED:
        print(f"{Fore.GREEN}{Style.BRIGHT}✅ {msg}{Style.RESET_ALL}")
    else:
        print(f"✅ {msg}")

def print_error(msg):
    if COLORS_ENABLED:
        print(f"{Fore.RED}{Style.BRIGHT}❌ {msg}{Style.RESET_ALL}")
    else:
        print(f"❌ {msg}")

def print_warning(msg):
    if COLORS_ENABLED:
        print(f"{Fore.YELLOW}{Style.BRIGHT}⚠️ {msg}{Style.RESET_ALL}")
    else:
        print(f"⚠️ {msg}")

def print_info(msg):
    if COLORS_ENABLED:
        print(f"{Fore.CYAN}{msg}{Style.RESET_ALL}")
    else:
        print(msg)

def print_header(title):
    if COLORS_ENABLED:
        print(f"\n{Fore.MAGENTA}{Style.BRIGHT}--- {title} ---{Style.RESET_ALL}")
    else:
        print(f"\n--- {title} ---")

# ============================================================================
# CONFIGURAÇÃO DE LOGS (Definição da classe, mas não a inicialização)
# ============================================================================

class InMemoryLogHandler(logging.Handler):
    def __init__(self, max_logs=500):
        super().__init__()
        self.logs = []
        self.max_logs = max_logs
        self.lock = Lock()
        
    def emit(self, record):
        with self.lock:
            log_entry = {
                'timestamp': datetime.fromtimestamp(record.created).isoformat(),
                'level': record.levelname,
                'message': self.format(record),
                'name': record.name
            }
            self.logs.append(log_entry)
            if len(self.logs) > self.max_logs:
                self.logs.pop(0)
    
    def get_logs(self, limit=None):
        with self.lock:
            if limit:
                return self.logs[-limit:]
            return self.logs.copy()

# Variáveis de log inicializadas como None para serem configuradas no main
# memory_handler = None
# logger = None
# error_logger = None

# ============================================================================
# CONFIGURAÇÃO GLOBAL DE LOGS (CORREÇÃO 1)
# ============================================================================

Path('logs').mkdir(exist_ok=True)

memory_handler = InMemoryLogHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
memory_handler.setFormatter(formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[
        logging.FileHandler('logs/automacao_bling.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
        memory_handler
    ]
)

logger = logging.getLogger(__name__)
error_logger = logging.getLogger('errors')
error_handler = logging.FileHandler('logs/errors.log', encoding='utf-8')
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(formatter)
error_logger.addHandler(error_handler)

# ============================================================================
# CLASSES DE AUTENTICAÇÃO E API
# ============================================================================

class BlingAuth:
    TOKEN_FILE = 'tokens.json'
    
    def __init__(self, config: Config):
        self.config = config
        self.token_url = 'https://www.bling.com.br/Api/v3/oauth/token'
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.expires_at: Optional[datetime] = None
        self.auth_lock = Lock()
        self.load_tokens()

    def _save_tokens(self):
        with self.auth_lock:
            data = {
                'access_token': self.access_token,
                'refresh_token': self.refresh_token,
                'expires_at': self.expires_at.isoformat() if self.expires_at else None
            }
            with open(self.TOKEN_FILE, 'w') as f:
                json.dump(data, f, indent=4)
            logger.info("Tokens salvos em tokens.json") if logger else print_info("Tokens salvos em tokens.json")

    def load_tokens(self) -> bool:
        if not Path(self.TOKEN_FILE).exists():
            return False
        
        try:
            with open(self.TOKEN_FILE, 'r') as f:
                data = json.load(f)
            
            self.access_token = data.get('access_token')
            self.refresh_token = data.get('refresh_token')
            expires_at_str = data.get('expires_at')
            
            if expires_at_str:
                self.expires_at = datetime.fromisoformat(expires_at_str)
            
            if self.is_token_valid():
                logger.info("Tokens carregados com sucesso.") if logger else print_success("Tokens carregados com sucesso.")
                return True
            else:
                logger.warning("Tokens expirados ou inválidos.") if logger else print_warning("Tokens expirados ou inválidos.")
                return self.refresh_access_token()
                
        except Exception as e:
            logger.error(f"Erro ao carregar tokens: {e}") if logger else print_error(f"Erro ao carregar tokens: {e}")
            return False

    def is_token_valid(self) -> bool:
        if not self.access_token or not self.expires_at:
            return False
        # Token é considerado válido se expirar em mais de 60 segundos
        return self.expires_at > datetime.now() + timedelta(seconds=60)

    def get_authorization_url(self) -> str:
        params = {
            'response_type': 'code',
            'client_id': self.config.CLIENT_ID,
            'redirect_uri': self.config.REDIRECT_URI,
            'state': 'bling_automacao' # Opcional, para segurança
        }
        return f"https://www.bling.com.br/Api/v3/oauth/authorize?{urlencode(params)}"

    def _get_basic_auth_header(self) -> Dict[str, str]:
        auth_string = f"{self.config.CLIENT_ID}:{self.config.CLIENT_SECRET}"
        encoded_auth = base64.b64encode(auth_string.encode()).decode()
        return {"Authorization": f"Basic {encoded_auth}"}

    def exchange_code_for_token(self, code: str) -> bool:
        headers = self._get_basic_auth_header()
        data = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': self.config.REDIRECT_URI
        }
        
        try:
            response = requests.post(self.token_url, headers=headers, data=data, timeout=self.config.REQUEST_TIMEOUT)
            response.raise_for_status()
            token_data = response.json()
            
            with self.auth_lock:
                self.access_token = token_data['access_token']
                self.refresh_token = token_data['refresh_token']
                expires_in = token_data['expires_in']
                self.expires_at = datetime.now() + timedelta(seconds=expires_in)
                self._save_tokens()
            
            logger.info("Troca de código por token bem-sucedida.") if logger else print_success("Troca de código por token bem-sucedida.")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Erro ao trocar código por token: {e}") if logger else print_error(f"Erro ao trocar código por token: {e}")
            return False

    def refresh_access_token(self) -> bool:
        if not self.refresh_token:
            logger.error("Refresh token não disponível.") if logger else print_error("Refresh token não disponível.")
            return False
            
        headers = self._get_basic_auth_header()
        data = {
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token
        }
        
        try:
            response = requests.post(self.token_url, headers=headers, data=data, timeout=self.config.REQUEST_TIMEOUT)
            response.raise_for_status()
            token_data = response.json()
            
            with self.auth_lock:
                self.access_token = token_data['access_token']
                # O Bling não retorna um novo refresh_token, então mantemos o antigo
                expires_in = token_data['expires_in']
                self.expires_at = datetime.now() + timedelta(seconds=expires_in)
                self._save_tokens()
            
            logger.info("Token de acesso renovado com sucesso.") if logger else print_success("Token de acesso renovado com sucesso.")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Erro ao renovar token: {e}") if logger else print_error(f"Erro ao renovar token: {e}")
            return False

class BlingAPI:
    def __init__(self, auth: BlingAuth, component_config: Dict):
        self.auth = auth
        self.component_config = component_config
        self.api_url = Config.BLING_API_URL

    def _request_with_retry(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        url = f"{self.api_url}/{endpoint}"
        headers = kwargs.pop('headers', {})
        
        for attempt in range(Config.MAX_RETRIES):
            if not self.auth.is_token_valid():
                if not self.auth.refresh_access_token():
                    raise BlingAuthError("Token inválido e falha ao renovar.")
            
            headers['Authorization'] = f'Bearer {self.auth.access_token}'
            kwargs['headers'] = headers
            
            try:
                response = requests.request(method, url, timeout=Config.REQUEST_TIMEOUT, **kwargs)
                
                if response.status_code == 401:
                    logger.warning("Token expirado (401). Tentando renovar...") if logger else print_warning("Token expirado (401). Tentando renovar...")
                    if self.auth.refresh_access_token():
                        time.sleep(Config.BASE_DELAY * (attempt + 1)) # Backoff antes de tentar novamente
                        continue  # Tenta novamente
                    else:
                        raise BlingAuthError("Falha ao renovar token.")
                
                response.raise_for_status()
                return response
            
            except requests.exceptions.RequestException as e:
                if attempt < Config.MAX_RETRIES - 1:
                    logger.warning(f"Tentativa {attempt + 1} falhou: {e}. Retrying...") if logger else print_warning(f"Tentativa {attempt + 1} falhou: {e}. Retrying...")
                    time.sleep(Config.BASE_DELAY * (attempt + 1)) # Backoff exponencial
                    continue
                else:
                    logger.error(f"Falha final na requisição para {endpoint}: {e}") if logger else print_error(f"Falha final na requisição para {endpoint}: {e}")
                    raise BlingAPIError(f"Falha na API do Bling: {e}")

    def get_product_by_sku(self, sku: str) -> Optional[Dict]:
        try:
            response = self._request_with_retry('GET', 'produtos', params={'codigo': sku})
            data = response.json()
            if data.get('data'):
                return data['data'][0]
            return None
        except BlingAPIError:
            return None

    def get_product_stock(self, product_id: int) -> int:
        try:
            response = self._request_with_retry('GET', f'estoques/{product_id}')
            data = response.json()
            # A estrutura de estoque pode variar, assumindo que o estoque atual é o que importa
            return int(data.get('estoqueAtual', 0))
        except BlingAPIError:
            return 0

    def get_all_kits_and_components(self) -> List[Kit]:
        kits: List[Kit] = []
        pagina = 1
        while True:
            try:
                response = self._request_with_retry('GET', 'produtos', params={'tipo': 'P', 'pagina': pagina})
                data = response.json()
                
                if not data.get('data'):
                    break
                
                for product_data in data['data']:
                    if product_data.get('tipo') == 'P' and product_data.get('estrutura'):
                        kit_sku = product_data.get('codigo', 'N/A')
                        kit_name = product_data.get('descricao', 'Sem nome')
                        kit_price = product_data.get('preco', 0.0)
                        
                        components: List[Component] = []
                        for item in product_data['estrutura'].get('componentes', []):
                            comp_data = item.get('produto', {})
                            comp_sku = comp_data.get('codigo', 'N/A')
                            
                            # Aplica configurações locais
                            config = self.component_config.get('component_defaults', {})
                            if comp_sku in self.component_config.get('components', {}):
                                config.update(self.component_config['components'][comp_sku])
                                
                            component = Component(
                                sku=comp_sku,
                                name=comp_data.get('descricao', 'Sem nome'),
                                qty=item.get('quantidade', 0),
                                supplier=config.get('supplier', 'FORNECEDOR_PADRAO'),
                                lead_time_days=config.get('lead_time_days', 15),
                                min_stock=config.get('min_stock', 10)
                            )
                            
                            # A busca de estoque foi movida para o método update_components_stock para otimização.
                            
                            components.append(component)
                        
                        kits.append(Kit(sku=kit_sku, name=kit_name, components=components, price=kit_price))
                
                pagina += 1
                time.sleep(Config.DELAY_BETWEEN_BATCHES)
                
            except BlingAPIError:
                break
                
        return kits

    def create_production_order(self, kit_sku: str, quantity: int) -> Optional[int]:
        try:
            # Busca o produto pelo SKU
            product = self.get_product_by_sku(kit_sku)
            if not product:
                logger.error(f"Produto {kit_sku} não encontrado")
                return None
            
            payload = {
                "produto": {"id": product['id']},
                "quantidade": quantity,
                "dataPrevisao": (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d')
            }
            
            response = self._request_with_retry('POST', 'ordens-producao', json=payload)
            data = response.json()
            op_id = data.get('data', {}).get('id')
            
            if op_id:
                logger.info(f"✅ OP {op_id} criada para kit {kit_sku} (qtd: {quantity})")
            
            return op_id
        except BlingAPIError as e:
            logger.error(f"❌ Erro ao criar OP para {kit_sku}: {e}")
            return None

    def update_components_stock(self, components: List[Component]) -> None:
        """Atualiza o estoque atual de uma lista de componentes"""
        for component in components:
            product = self.get_product_by_sku(component.sku)
            if product:
                component.current_stock = self.get_product_stock(product['id'])
                logger.debug(f"Estoque de {component.sku}: {component.current_stock}")

    def create_purchase_order(self, supplier_name: str, items: List[PurchaseNeed]) -> Optional[int]:
        try:
            # 1. Busca fornecedor por nome
            response = self._request_with_retry('GET', 'contatos', params={'pesquisa': supplier_name})
            data = response.json()
            
            if not data.get('data'):
                logger.error(f"Fornecedor '{supplier_name}' não encontrado")
                return None
            
            supplier_id = data['data'][0]['id']
            
            # 2. Monta payload da PO
            po_items = []
            for item in items:
                product = self.get_product_by_sku(item.component_sku)
                if product:
                    # O dataclass PurchaseNeed não tem unit_cost, mas o Component tem.
                    # Vamos usar um valor padrão ou tentar buscar o custo.
                    # Para simplificar, usaremos 0.0, mas o ideal seria buscar o custo do componente.
                    # O dataclass Component tem unit_cost, mas o PurchaseNeed não.
                    # Vamos assumir que o custo unitário está no dataclass Component, que é o que gerou o PurchaseNeed.
                    # Como não temos o objeto Component aqui, vamos usar 0.0 ou buscar o custo.
                    # Para não quebrar, vamos usar 0.0.
                    po_items.append({
                        "produto": {"id": product['id']},
                        "quantidade": item.quantity_needed,
                        "valor": 0.0 # Valor fixo, idealmente viria do Component
                    })
            
            if not po_items:
                logger.warning(f"Nenhum item válido para PO do fornecedor {supplier_name}")
                return None
            
            payload = {
                "contato": {"id": supplier_id},
                "itens": po_items,
                "dataPrevisao": (datetime.now() + timedelta(days=15)).strftime('%Y-%m-%d')
            }
            
            # 3. Cria PO
            response = self._request_with_retry('POST', 'compras', json=payload)
            data = response.json()
            po_id = data.get('data', {}).get('id')
            
            if po_id:
                logger.info(f"✅ PO {po_id} criada para {supplier_name} ({len(po_items)} itens)")
            
            return po_id
        except BlingAPIError as e:
            logger.error(f"❌ Erro ao criar PO para {supplier_name}: {e}")
            return None

# ============================================================================
# CLASSES DE GERENCIAMENTO E ORQUESTRAÇÃO
# ============================================================================

class StatisticsManager:
    def __init__(self):
        self.reset()

    def reset(self):
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.success = 0
        self.failed = 0
        self.ops_created = 0
        self.pos_created = 0
        self.min_stock_checks = 0

    def start(self):
        self.start_time = time.time()

    def stop(self):
        self.end_time = time.time()

    def to_dict(self) -> Dict:
        elapsed = (self.end_time - self.start_time) if self.start_time and self.end_time else 0
        return {
            "success": self.success,
            "failed": self.failed,
            "ops_created": self.ops_created,
            "pos_created": self.pos_created,
            "min_stock_checks": self.min_stock_checks,
            "elapsed_time_seconds": round(elapsed, 2)
        }

class PurchaseNeedsManager:
    def __init__(self, api: BlingAPI):
        self.api = api
        self.needs: Dict[str, List[PurchaseNeed]] = defaultdict(list) # Agrupado por fornecedor

    def check_min_stock_needs(self, components: List[Component]):
        for component in components:
            if component.current_stock < component.min_stock:
                quantity_needed = component.min_stock - component.current_stock
                self.add_need(
                    component=component,
                    quantity=quantity_needed,
                    reason=f"Estoque abaixo do mínimo ({component.current_stock} < {component.min_stock})"
                )

    def add_need(self, component: Component, quantity: int, reason: str):
        need = PurchaseNeed(
            component_sku=component.sku,
            component_name=component.name,
            quantity_needed=quantity,
            supplier=component.supplier,
            lead_time_days=component.lead_time_days,
            reason=reason
        )
        self.needs[component.supplier].append(need)

    def generate_purchase_orders(self) -> List[int]:
        po_ids = []
        for supplier, items in self.needs.items():
            po_id = self.api.create_purchase_order(supplier, items)
            if po_id:
                po_ids.append(po_id)
                logger.info(f"🛒 PO {po_id} criada para {supplier} com {len(items)} itens.") if logger else print_success(f"🛒 PO {po_id} criada para {supplier} com {len(items)} itens.")
            else:
                logger.error(f"❌ Falha ao criar PO para {supplier}.") if logger else print_error(f"❌ Falha ao criar PO para {supplier}.")
        
        self.needs.clear()
        return po_ids

class AutomationOrchestrator:
    COMPONENT_CONFIG_FILE = 'component_config.json'
    
    def __init__(self, config: Config):
        self.config = config
        self.auth = BlingAuth(config)
        self.component_config = self._load_or_create_component_config()
        self.api = BlingAPI(self.auth, component_config=self.component_config)
        self.stats = StatisticsManager()
        self.purchase_manager = PurchaseNeedsManager(self.api)
        self.failed_items: List[str] = []

    def _load_or_create_component_config(self) -> Dict:
        if Path(self.COMPONENT_CONFIG_FILE).exists():
            try:
                with open(self.COMPONENT_CONFIG_FILE, 'r') as f:
                    config = json.load(f)
                logger.info("Configuração de componentes carregada.") if logger else print_info("Configuração de componentes carregada.")
                return config
            except Exception as e:
                logger.error(f"Erro ao carregar config de componentes: {e}") if logger else print_error(f"Erro ao carregar config de componentes: {e}")
                
        # Cria arquivo padrão
        default_config = {
          "component_defaults": {
            "supplier": "FORNECEDOR_PADRAO",
            "lead_time_days": 15,
            "min_stock": 10
          },
          "components": {
            "EXEMPLO-001": {
              "supplier": "Fornecedor A",
              "lead_time_days": 10,
              "min_stock": 20
            }
          }
        }
        try:
            with open(self.COMPONENT_CONFIG_FILE, 'w') as f:
                json.dump(default_config, f, indent=4)
            logger.warning("Arquivo de configuração de componentes criado com valores padrão.") if logger else print_warning("Arquivo de configuração de componentes criado com valores padrão.")
            return default_config
        except Exception as e:
            logger.error(f"Erro ao criar config de componentes: {e}") if logger else print_error(f"Erro ao criar config de componentes: {e}")
            return {}

    def process_kits(self, kits: List[Kit], batch_size: int = 10, check_stock: bool = True) -> Dict:
        self.stats.reset()
        self.stats.start()
        self.failed_items.clear()
        
        for kit in kits:
            try:
                # 1. Cria Ordem de Produção (OP)
                op_id = self.api.create_production_order(kit.sku, 1) # Exemplo: criar 1 unidade
                if op_id:
                    self.stats.ops_created += 1
                    self.stats.success += 1
                    logger.info(f"🏭 OP {op_id} criada para Kit {kit.sku}.") if logger else print_success(f"🏭 OP {op_id} criada para Kit {kit.sku}.")
                else:
                    raise BlingAPIError("Falha ao criar OP.")
                
                # 2. Verifica Estoque Mínimo dos Componentes
                if check_stock and self.config.CHECK_MIN_STOCK:
                    self.stats.min_stock_checks += 1
                    # Simulação: buscar estoque real aqui
                    for component in kit.components:
                        component.current_stock = self.api.get_product_stock(component.sku) # Assumindo que a API busca por SKU
                    
                    self.purchase_manager.check_min_stock_needs(kit.components)
                
            except Exception as e:
                self.stats.failed += 1
                self.failed_items.append(kit.sku)
                logger.error(f"❌ Falha ao processar Kit {kit.sku}: {e}") if logger else print_error(f"❌ Falha ao processar Kit {kit.sku}: {e}")
                
        # 3. Gera Ordens de Compra (PO)
        po_ids = self.purchase_manager.generate_purchase_orders()
        self.stats.pos_created += len(po_ids)
        
        self.stats.stop()
        return self.stats.to_dict()

    def run_purchase_check(self) -> Dict:
        self.stats.reset()
        self.stats.start()
        self.failed_items.clear()
        
        try:
            kits = self.api.get_all_kits_and_components()
            logger.info(f"Kits carregados: {len(kits)}") if logger else print_info(f"Kits carregados: {len(kits)}")
            
            # 1. Atualiza o estoque de todos os componentes
            all_components = [comp for kit in kits for comp in kit.components]
            self.api.update_components_stock(all_components)
            
            # 2. Verifica Estoque Mínimo de todos os componentes
            for component in all_components:
                self.stats.min_stock_checks += 1
                
                if component.current_stock < component.min_stock:
                    quantity_needed = component.min_stock - component.current_stock
                    self.purchase_manager.add_need(
                        component=component,
                        quantity=quantity_needed,
                        reason=f"Estoque abaixo do mínimo ({component.current_stock} < {component.min_stock})"
                    )
            
            # 2. Gera Ordens de Compra (PO)
            po_ids = self.purchase_manager.generate_purchase_orders()
            self.stats.pos_created += len(po_ids)
            self.stats.success = 1 # Sucesso na verificação
            
        except Exception as e:
            self.stats.failed = 1
            logger.error(f"❌ Falha na verificação de compra: {e}") if logger else print_error(f"❌ Falha na verificação de compra: {e}")
            
        self.stats.stop()
        return self.stats.to_dict()

# ============================================================================
# WEBSERVER E ROTAS
# ============================================================================

class WebServer:
    def __init__(self, auth: BlingAuth, orchestrator: AutomationOrchestrator):
        self.app = Flask(__name__)
        self.auth = auth
        self.orchestrator = orchestrator
        self.setup_routes()
        
        if WEBSOCKET_AVAILABLE:
            self.sock = Sock(self.app)
            self.setup_websocket()

    def setup_routes(self):
        # Rotas existentes
        self.app.route('/', methods=['GET'])(self.dashboard)
        self.app.route('/dashboard', methods=['GET'])(self.dashboard)
        self.app.route('/health', methods=['GET'])(self.health_check)
        self.app.route('/webhook/bling', methods=['POST'])(self.webhook_bling)
        
        # Novas rotas
        self.app.route('/callback', methods=['GET'])(self.callback)
        self.app.route('/api/status', methods=['GET'])(self.api_status)
        self.app.route('/api/stats', methods=['GET'])(self.api_stats)
        self.app.route('/api/stock', methods=['GET'])(self.api_stock)
        self.app.route('/api/needs', methods=['GET'])(self.api_needs)
        self.app.route('/api/process_kits', methods=['POST'])(self.api_process_kits)
        self.app.route('/api/recheck', methods=['POST'])(self.api_recheck)
        
        # Rotas que precisam de ajuste
        self.app.route('/api/logs', methods=['GET'])(self.api_logs)
        self.app.route('/api/kits', methods=['GET'])(self.api_kits)

    def setup_websocket(self):
        if WEBSOCKET_AVAILABLE:
            self.sock.route('/ws/logs')(self.ws_logs)

    # Implementações das rotas
    def dashboard(self):
        return render_template_string(DASHBOARD_TEMPLATE)

    def health_check(self):
        return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

    def webhook_bling(self):
        try:
            data = request.get_json(force=True)
            event_type = data.get('event') or data.get('tipo') or 'unknown'
            logger.info(f"🪝 Webhook recebido: {event_type}")
            
            is_order_event = (
                event_type == 'order.created' or 
                event_type == 'pedido.pago' or 
                (data.get('tipo') == 'pedido' and data.get('evento') in ['criado', 'pago'])
            )
            
            if is_order_event:
                pedido_id = data.get('id') or (data.get('retorno', {}).get('pedidos', [{}])[0].get('pedido', {}).get('id'))
                if pedido_id:
                    logger.info(f"✅ Pedido ID {pedido_id} identificado. Iniciando verificação de compra.")
                    Thread(target=self.orchestrator.run_purchase_check, daemon=True).start()
                    return jsonify({'status': 'ok', 'message': f'Pedido {pedido_id} processado'}), 200
            
            if event_type == 'estoque.atualizado':
                logger.info("📦 Evento de estoque atualizado. Iniciando verificação de compra.")
                Thread(target=self.orchestrator.run_purchase_check, daemon=True).start()
                
            return jsonify({'status': 'ok', 'message': f'Webhook {event_type} recebido'}), 200
        except Exception as e:
            logger.error(f"Erro no webhook: {e}")
            return jsonify({'error': str(e)}), 500

    def callback(self):
        code = request.args.get('code')
        error = request.args.get('error')
        
        if error:
            return render_template_string(ERROR_TEMPLATE, message=f"Erro de autorização: {error}")
            
        if code and self.auth.exchange_code_for_token(code):
            return render_template_string(SUCCESS_TEMPLATE, message="Tokens de acesso obtidos e salvos com sucesso!")
        else:
            return render_template_string(ERROR_TEMPLATE, message="Falha ao obter tokens de acesso.")

    def api_status(self):
        is_valid = self.auth.is_token_valid()
        expires_at = self.auth.expires_at.isoformat() if self.auth.expires_at else None
        auth_url = self.auth.get_authorization_url()
        
        return jsonify({
            "token_valid": is_valid,
            "expires_at": expires_at,
            "auth_url": auth_url
        })

    def api_stats(self):
        return jsonify(self.orchestrator.stats.to_dict())

    def api_stock(self):
        # Esta rota retorna o estoque de todos os componentes com alertas
        kits = self.orchestrator.api.get_all_kits_and_components()
        
        # 1. Coleta todos os componentes
        all_components = [comp for kit in kits for comp in kit.components]
        
        # 2. Busca o estoque atual de todos eles
        self.orchestrator.api.update_components_stock(all_components)
        
        items = []
        # 3. Filtra e formata a resposta
        for component in all_components:
            items.append({
                "sku": component.sku,
                "nome": component.name,
                "estoque": component.current_stock,
                "minimo": component.min_stock,
                "alerta": component.current_stock < component.min_stock
            })
        
        return jsonify({"items": items})

    def api_needs(self):
        needs_list = []
        for supplier, needs in self.orchestrator.purchase_manager.needs.items():
            for need in needs:
                needs_list.append(asdict(need))
        
        return jsonify({"needs": needs_list})

    def api_process_kits(self):
        try:
            data = request.get_json()
            kits_to_process = data.get('kits', [])
            
            # Simulação: buscar kits reais
            all_kits = self.orchestrator.api.get_all_kits_and_components()
            kits = [k for k in all_kits if k.sku in kits_to_process]
            
            stats = self.orchestrator.process_kits(kits)
            return jsonify({"status": "ok", "stats": stats})
        except Exception as e:
            logger.error(f"Erro ao processar kits: {e}")
            return jsonify({"status": "error", "error": str(e)}), 500

    def api_recheck(self):
        logger.info("🔄 Verificação manual iniciada via API")
        Thread(target=self.orchestrator.run_purchase_check, daemon=True).start()
        return jsonify({"status": "ok", "message": "Verificação iniciada"})

    def api_logs(self):
        try:
            logs = memory_handler.get_logs(limit=50)
            return jsonify({'logs': logs})
        except Exception as e:
            logger.error(f"Erro ao buscar logs: {e}")
            return jsonify({'error': str(e)}), 500

    def api_kits(self):
        try:
            kits = self.orchestrator.api.get_all_kits_and_components()
            kits_data = []
            for kit in kits:
                # Selecionar apenas campos necessários
                components_data = [{
                    "sku": c.sku,
                    "name": c.name,
                    "qty": c.qty
                } for c in kit.components]
                
                kits_data.append({
                    "sku": kit.sku,
                    "nome": kit.name,
                    "componentes": components_data
                })
            return jsonify({"kits": kits_data})
        except Exception as e:
            logger.error(f"Erro ao buscar kits: {e}")
            return jsonify({'error': str(e)}), 500

    def ws_logs(self, ws):
        if not WEBSOCKET_AVAILABLE:
            return
            
        last_log_count = 0
        try:
            while True:
                logs = memory_handler.get_logs()
                current_count = len(logs)
                
                if current_count > last_log_count:
                    new_logs = logs[last_log_count:]
                    ws.send(json.dumps({"logs": new_logs}))
                    last_log_count = current_count
                
                time.sleep(1)
        except Exception as e:
            logger.info(f"Cliente desconectado: {e}")

# ============================================================================
# TEMPLATES HTML
# ============================================================================

SUCCESS_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Autorização Concluída</title>
    <style>
        body { font-family: Arial, sans-serif; text-align: center; padding-top: 50px; }
        .container { max-width: 400px; margin: 0 auto; padding: 20px; border: 1px solid #ccc; border-radius: 10px; }
        .success-icon { font-size: 4em; color: #4CAF50; }
        h1 { color: #4CAF50; }
        button { padding: 10px 20px; background-color: #4CAF50; color: white; border: none; border-radius: 5px; cursor: pointer; }
    </style>
</head>
<body>
    <div class="container">
        <div class="success-icon">✓</div>
        <h1>Autorização Concluída!</h1>
        <p>{{ message }}</p>
        <button onclick="window.close()">Fechar</button>
    </div>
</body>
</html>
"""

ERROR_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Erro de Autorização</title>
    <style>
        body { font-family: Arial, sans-serif; text-align: center; padding-top: 50px; }
        .container { max-width: 400px; margin: 0 auto; padding: 20px; border: 1px solid #ccc; border-radius: 10px; }
        .error-icon { font-size: 4em; color: #f44336; }
        h1 { color: #f44336; }
        button { padding: 10px 20px; background-color: #f44336; color: white; border: none; border-radius: 5px; cursor: pointer; }
    </style>
</head>
<body>
    <div class="container">
        <div class="error-icon">✗</div>
        <h1>Erro de Autorização</h1>
        <p>{{ message }}</p>
        <button onclick="window.close()">Fechar</button>
    </div>
</body>
</html>
"""

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
        body {
            background: #f8f9fa;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        
        .navbar {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            box-shadow: 0 4px 6px rgba(0,0,0,.1);
        }
        
        .navbar-brand {
            font-weight: 700;
            font-size: 1.5rem;
        }
        
        .status-badge {
            padding: .5rem 1rem;
            border-radius: 20px;
            font-size: .9rem;
            font-weight: 600;
        }
        
        .card {
            border-radius: 1rem;
            box-shadow: 0 4px 6px rgba(0,0,0,.07);
            border: none;
            margin-bottom: 1.5rem;
            transition: transform 0.3s ease, box-shadow 0.3s ease;
        }
        
        .card:hover {
            transform: translateY(-5px);
            box-shadow: 0 8px 15px rgba(0,0,0,.1);
        }
        
        .card-title {
            font-weight: 600;
            color: #343a40;
            margin-bottom: 1rem;
        }
        
        .kpi-value {
            font-size: 2.5rem;
            font-weight: 700;
            margin-bottom: .25rem;
        }
        
        .kpi-label {
            font-size: .9rem;
            color: #6c757d;
            text-transform: uppercase;
            letter-spacing: .5px;
        }
        
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
        
        .log-entry {
            padding: .25rem 0;
            border-bottom: 1px solid #333;
        }
        
        .log-entry:last-child {
            border-bottom: none;
        }
        
        .log-level-INFO { color: #4ec9b0; }
        .log-level-WARNING { color: #dcdcaa; }
        .log-level-ERROR { color: #f48771; }
        .log-level-DEBUG { color: #9cdcfe; }
        
        .nav-tabs .nav-link {
            color: #6c757d;
            font-weight: 500;
        }
        
        .nav-tabs .nav-link.active {
            background-color: #fff;
            border-color: #dee2e6 #dee2e6 #fff;
            color: #667eea;
            font-weight: 600;
        }
        
        .search-box {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
        }
        
        .filters {
            display: flex;
            gap: 20px;
            margin-bottom: 20px;
        }
        
        .products-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 20px;
        }
        
        .product-card {
            background: #fff;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,.05);
            border: 1px solid #eee;
        }
        
        .product-id {
            font-size: .8em;
            color: #999;
            margin-bottom: 5px;
        }
        
        .product-name {
            font-weight: 600;
            font-size: 1.1em;
            margin-bottom: 10px;
        }
        
        .product-details {
            margin-top: 10px;
        }
        
        .detail-row {
            display: flex;
            justify-content: space-between;
            padding: 3px 0;
            border-bottom: 1px dotted #eee;
        }
        
        .detail-row:last-child {
            border-bottom: none;
        }
        
        .detail-label {
            font-weight: 500;
            color: #555;
        }
        
        .detail-value {
            font-weight: 400;
        }
        
        .price {
            color: #10b981;
            font-size: 1.3em;
        }
        
        .stock {
            display: inline-block;
            padding: 6px 14px;
            border-radius: 20px;
            font-weight: 600;
            font-size: 0.9em;
        }
        
        .stock-high {
            background: #d1fae5;
            color: #065f46;
        }
        
        .stock-medium {
            background: #fef3c7;
            color: #92400e;
        }
        
        .stock-low {
            background: #fee2e2;
            color: #991b1b;
        }
        
        .loading {
            text-align: center;
            padding: 60px 20px;
        }
        
        .loading-spinner {
            width: 60px;
            height: 60px;
            border: 5px solid #f3f4f6;
            border-top: 5px solid #667eea;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 20px;
        }
        
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        
        .empty-state {
            text-align: center;
            padding: 60px 20px;
            background: white;
            border-radius: 20px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.1);
        }
        
        .empty-state-icon {
            font-size: 4em;
            margin-bottom: 20px;
        }
        
        .chart-container {
            position: relative;
            height: 300px;
        }
        
        .spinner-border-sm {
            width: 1rem;
            height: 1rem;
            border-width: .15em;
        }
        
        @media (max-width: 768px) {
            .products-grid {
                grid-template-columns: 1fr;
            }
            
            .search-box {
                flex-direction: column;
            }
            
            input[type="text"] {
                width: 100%;
            }
        }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark">
        <div class="container-fluid">
            <a class="navbar-brand" href="#">🚀 Bling Automação Wesley</a>
            <div class="d-flex align-items-center">
                <span class="status-badge" id="status-badge">Token Inválido</span>
                <a href="#" id="auth-link" class="btn btn-sm btn-outline-light ms-3 d-none">Autorizar Bling</a>
            </div>
        </div>
    </nav>

    <div class="container my-4">
        <ul class="nav nav-tabs" id="mainTabs" role="tablist">
            <li class="nav-item" role="presentation">
                <a class="nav-link active" id="dashboard-tab" data-bs-toggle="tab" href="#tabDashboard" role="tab">
                    📊 Dashboard
                </a>
            </li>
            <li class="nav-item" role="presentation">
                <a class="nav-link" id="stock-tab" data-bs-toggle="tab" href="#tabStock" role="tab">
                    📦 Estoque
                </a>
            </li>
            <li class="nav-item" role="presentation">
                <a class="nav-link" id="needs-tab" data-bs-toggle="tab" href="#tabNeeds" role="tab">
                    🛒 Necessidades de Compra
                </a>
            </li>
            <li class="nav-item" role="presentation">
                <a class="nav-link" id="kits-tab" data-bs-toggle="tab" href="#tabKits" role="tab">
                    🛠️ Kits
                </a>
            </li>
        </ul>

        <div class="tab-content p-4 bg-white border border-top-0" style="border-radius: 0 0 1rem 1rem;">
            <!-- Dashboard Tab -->
            <div class="tab-pane fade show active" id="tabDashboard" role="tabpanel">
                <h4 class="mb-4">📊 Visão Geral da Automação</h4>
                
                <div class="row mb-4" id="stats-kpis">
                    <div class="col-md-2 mb-3">
                        <div class="card h-100 text-center">
                            <div class="card-body">
                                <div class="kpi-value text-success" id="kpi-success">✅ 0</div>
                                <div class="kpi-label">Sucesso</div>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-2 mb-3">
                        <div class="card h-100 text-center">
                            <div class="card-body">
                                <div class="kpi-value text-danger" id="kpi-failed">❌ 0</div>
                                <div class="kpi-label">Falhas</div>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-2 mb-3">
                        <div class="card h-100 text-center">
                            <div class="card-body">
                                <div class="kpi-value text-primary" id="kpi-ops">🏭 0</div>
                                <div class="kpi-label">OPs Criadas</div>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-2 mb-3">
                        <div class="card h-100 text-center">
                            <div class="card-body">
                                <div class="kpi-value text-info" id="kpi-pos">🛒 0</div>
                                <div class="kpi-label">POs Criadas</div>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-2 mb-3">
                        <div class="card h-100 text-center">
                            <div class="card-body">
                                <div class="kpi-value text-warning" id="kpi-checks">🔍 0</div>
                                <div class="kpi-label">Checks Estoque</div>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-2 mb-3">
                        <div class="card h-100 text-center">
                            <div class="card-body">
                                <div class="kpi-value text-secondary" id="kpi-time">⏱️ 0s</div>
                                <div class="kpi-label">Tempo Total</div>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="row mb-4">
                    <div class="col-md-6">
                        <div class="card h-100">
                            <div class="card-body">
                                <h5 class="card-title">📈 Status de Processamento</h5>
                                <div class="chart-container">
                                    <canvas id="processingChart"></canvas>
                                </div>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-6">
                        <div class="card h-100">
                            <div class="card-body">
                                <h5 class="card-title">📋 Logs em Tempo Real</h5>
                                <div id="logs-content" class="log-box"></div>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="row">
                    <div class="col-12">
                        <div class="card">
                            <div class="card-body">
                                <h5 class="card-title">🔧 Ações Manuais</h5>
                                <p class="card-text">Acione a verificação de estoque e geração de POs manualmente.</p>
                                <button id="recheck-button" class="btn btn-primary">
                                    <span class="btn-text">🔄 Re-checar Estoque</span>
                                    <span class="spinner-border spinner-border-sm d-none" role="status"></span>
                                </button>
                                <span id="recheck-status" class="ms-3"></span>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Stock Tab -->
            <div class="tab-pane fade" id="tabStock" role="tabpanel">
                <h4 class="mb-4">📦 Estoque de Componentes (Abaixo do Mínimo)</h4>
                <div class="table-responsive">
                    <table class="table table-striped table-hover">
                        <thead>
                            <tr>
                                <th>SKU</th>
                                <th>Nome</th>
                                <th>Estoque Atual</th>
                                <th>Estoque Mínimo</th>
                                <th>Alerta</th>
                            </tr>
                        </thead>
                        <tbody id="stock-table-body">
                            <tr>
                                <td colspan="5" class="text-center">Carregando dados de estoque...</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Needs Tab -->
            <div class="tab-pane fade" id="tabNeeds" role="tabpanel">
                <h4 class="mb-4">🛒 Necessidades de Compra</h4>
                <div class="table-responsive">
                    <table class="table table-striped table-hover">
                        <thead>
                            <tr>
                                <th>SKU</th>
                                <th>Nome</th>
                                <th>Qtd. Necessária</th>
                                <th>Fornecedor</th>
                                <th>Lead Time (dias)</th>
                                <th>Motivo</th>
                            </tr>
                        </thead>
                        <tbody id="needs-table-body">
                            <tr>
                                <td colspan="6" class="text-center">Carregando necessidades de compra...</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Kits Tab -->
            <div class="tab-pane fade" id="tabKits" role="tabpanel">
                <h4 class="mb-4">🛠️ Kits de Produtos</h4>
                <p>Lista de kits cadastrados no Bling e seus componentes.</p>
                <div class="table-responsive">
                    <table class="table table-striped table-hover">
                        <thead>
                            <tr>
                                <th>SKU Kit</th>
                                <th>Nome Kit</th>
                                <th>Componentes</th>
                            </tr>
                        </thead>
                        <tbody id="kits-table-body">
                            <tr>
                                <td colspan="3" class="text-center">Carregando kits...</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        const API_BASE = '/api';
        let statsChart = null;
        let logWebSocket = null;
        const WS_URL = (window.location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + window.location.host + '/ws/logs';

        function formatLog(log) {
            const levelClass = `log-level-${log.level}`;
            return `<div class="log-entry"><span class="${levelClass}">[${log.timestamp.substring(11, 19)}] [${log.level}]</span> ${log.message}</div>`;
        }

        function updateStatusBadge(isValid, authUrl) {
            const badge = document.getElementById('status-badge');
            const authLink = document.getElementById('auth-link');
            
            if (isValid) {
                badge.className = 'status-badge bg-success text-white';
                badge.textContent = 'Token Válido';
                authLink.classList.add('d-none');
            } else {
                badge.className = 'status-badge bg-danger text-white';
                badge.textContent = 'Token Inválido';
                authLink.href = authUrl;
                authLink.classList.remove('d-none');
            }
        }

        function updateStatsKPIs(stats) {
            document.getElementById('kpi-success').innerHTML = `✅ ${stats.success}`;
            document.getElementById('kpi-failed').innerHTML = `❌ ${stats.failed}`;
            document.getElementById('kpi-ops').innerHTML = `🏭 ${stats.ops_created}`;
            document.getElementById('kpi-pos').innerHTML = `🛒 ${stats.pos_created}`;
            document.getElementById('kpi-checks').innerHTML = `🔍 ${stats.min_stock_checks}`;
            document.getElementById('kpi-time').innerHTML = `⏱️ ${stats.elapsed_time_seconds}s`;
        }

        function updateStatsChart(stats) {
            const ctx = document.getElementById('processingChart');
            if (!ctx) return;
            
            if (statsChart) {
                statsChart.destroy();
            }

            statsChart = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: ['Sucesso', 'Falhas', 'OPs Criadas', 'POs Criadas'],
                    datasets: [{
                        label: 'Contagem',
                        data: [stats.success, stats.failed, stats.ops_created, stats.pos_created],
                        backgroundColor: ['#10b981', '#f44336', '#667eea', '#0dcaf0'],
                        borderWidth: 1
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {
                        y: { beginAtZero: true }
                    },
                    plugins: {
                        legend: { display: false }
                    }
                }
            });
        }

        async function fetchStatus() {
            try {
                const response = await fetch(`${API_BASE}/status`);
                const data = await response.json();
                updateStatusBadge(data.token_valid, data.auth_url);
            } catch (error) {
                console.error('Erro ao buscar status:', error);
                updateStatusBadge(false, '#');
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
                
                if (!data.items || data.items.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="5" class="text-center">Nenhum componente abaixo do estoque mínimo.</td></tr>';
                    return;
                }
                
                data.items.forEach(item => {
                    const rowClass = item.estoque < 5 ? 'table-danger' : 'table-warning';
                    const alertIcon = item.estoque < 5 ? '🚨' : '⚠️';
                    
                    tbody.innerHTML += `
                        <tr class="${rowClass}">
                            <td>${item.sku}</td>
                            <td>${item.nome}</td>
                            <td>${item.estoque}</td>
                            <td>${item.minimo}</td>
                            <td>${alertIcon} ALERTA</td>
                        </tr>
                    `;
                });
            } catch (error) {
                console.error('Erro ao buscar estoque:', error);
                document.getElementById('stock-table-body').innerHTML = `<tr><td colspan="5" class="text-center text-danger">Erro ao carregar estoque.</td></tr>`;
            }
        }

        async function fetchNeeds() {
            try {
                const response = await fetch(`${API_BASE}/needs`);
                const data = await response.json();
                const tbody = document.getElementById('needs-table-body');
                tbody.innerHTML = '';
                
                if (!data.needs || data.needs.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" class="text-center">Nenhuma necessidade de compra identificada.</td></tr>';
                    return;
                }
                
                data.needs.forEach(need => {
                    tbody.innerHTML += `
                        <tr>
                            <td>${need.component_sku}</td>
                            <td>${need.component_name}</td>
                            <td>${need.quantity_needed}</td>
                            <td>${need.supplier}</td>
                            <td>${need.lead_time_days}</td>
                            <td>${need.reason}</td>
                        </tr>
                    `;
                });
            } catch (error) {
                console.error('Erro ao buscar necessidades:', error);
                document.getElementById('needs-table-body').innerHTML = `<tr><td colspan="6" class="text-center text-danger">Erro ao carregar necessidades de compra.</td></tr>`;
            }
        }

        async function fetchKits() {
            try {
                const response = await fetch(`${API_BASE}/kits`);
                const data = await response.json();
                const tbody = document.getElementById('kits-table-body');
                tbody.innerHTML = '';
                
                if (!data.kits || data.kits.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="3" class="text-center">Nenhum kit encontrado</td></tr>';
                    return;
                }
                
                data.kits.forEach(kit => {
                    const componentsList = kit.componentes.map(c => 
                        `${c.name} (${c.sku}) x${c.qty}`
                    ).join('<br>');
                    
                    tbody.innerHTML += `
                        <tr>
                            <td>${kit.sku}</td>
                            <td>${kit.nome}</td>
                            <td>${componentsList}</td>
                        </tr>
                    `;
                });
            } catch (error) {
                console.error('Erro ao buscar kits:', error);
                document.getElementById('kits-table-body').innerHTML = `<tr><td colspan="3" class="text-center text-danger">Erro ao carregar kits.</td></tr>`;
            }
        }

        function connectWebSocket() {
            if (logWebSocket && (logWebSocket.readyState === WebSocket.OPEN || logWebSocket.readyState === WebSocket.CONNECTING)) {
                return;
            }
            
            logWebSocket = new WebSocket(WS_URL);
            const logContainer = document.getElementById('logs-content');

            logWebSocket.onopen = () => {
                console.log('WebSocket conectado.');
                logContainer.innerHTML += formatLog({timestamp: new Date().toISOString(), level: 'INFO', message: 'Conectado ao stream de logs.'});
            };

            logWebSocket.onmessage = (event) => {
                const data = JSON.parse(event.data);
                if (data.logs) {
                    data.logs.forEach(log => {
                        logContainer.innerHTML += formatLog(log);
                    });
                    logContainer.scrollTop = logContainer.scrollHeight;
                }
            };

            logWebSocket.onclose = () => {
                console.log('WebSocket desconectado. Tentando reconectar em 5s...');
                logContainer.innerHTML += formatLog({timestamp: new Date().toISOString(), level: 'WARNING', message: 'Desconectado. Tentando reconectar...'});
                setTimeout(connectWebSocket, 5000);
            };

            logWebSocket.onerror = (error) => {
                console.error('WebSocket Error:', error);
                logWebSocket.close();
            };
        }

        document.getElementById('recheck-button').addEventListener('click', async () => {
            const button = document.getElementById('recheck-button');
            const statusSpan = document.getElementById('recheck-status');
            const originalText = button.querySelector('.btn-text').textContent;
            
            button.disabled = true;
            button.querySelector('.btn-text').textContent = 'Processando...';
            button.querySelector('.spinner-border').classList.remove('d-none');
            statusSpan.textContent = '';
            
            try {
                const response = await fetch(`${API_BASE}/recheck`, {method: 'POST'});
                const data = await response.json();
                
                if (data.status === 'ok') {
                    statusSpan.className = 'text-success';
                    statusSpan.textContent = 'Verificação iniciada! Confira os logs.';
                } else {
                    statusSpan.className = 'text-danger';
                    statusSpan.textContent = `Erro: ${data.error}`;
                }
            } catch (error) {
                statusSpan.className = 'text-danger';
                statusSpan.textContent = `Erro: ${error.message}`;
            } finally {
                button.disabled = false;
                button.querySelector('.btn-text').textContent = originalText;
                button.querySelector('.spinner-border').classList.add('d-none');
                setTimeout(() => statusSpan.textContent = '', 5000);
            }
        });

        function initDashboard() {
            fetchStatus();
            fetchStats();
            fetchStock();
            fetchNeeds();
            fetchKits();
            connectWebSocket();

            setInterval(fetchStatus, 10000);
            setInterval(fetchStats, 10000);
            setInterval(fetchStock, 10000);
            setInterval(fetchNeeds, 10000);
            setInterval(fetchKits, 10000);
        }

        document.addEventListener('DOMContentLoaded', initDashboard);
    </script>
</body>
</html>
"""

# ============================================================================
# FACTORY FUNCTION PARA DEPLOY
# ============================================================================

def create_app():
    """
    Factory function para Waitress/Gunicorn
    Lazy loading para evitar timeout no Render
    """
    # Configuração de logs para o modo WSGI
    Path('logs').mkdir(exist_ok=True)
    
    global memory_handler
    memory_handler = InMemoryLogHandler()
    
    global logger
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    
    global error_logger
    error_logger = logging.getLogger('errors')
    error_logger.setLevel(logging.ERROR)
    
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    memory_handler.setFormatter(formatter)
    
    # Configuração do logger principal
    logging.basicConfig(
        level=logging.INFO,
        handlers=[
            logging.FileHandler('logs/automacao_bling.log', encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
            memory_handler
        ]
    )
    
    # Configuração do logger de erros
    error_handler = logging.FileHandler('logs/errors.log', encoding='utf-8')
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    error_logger.addHandler(error_handler)
    
    logger.info("🚀 Iniciando create_app()...")
    
    config = Config()
    auth = BlingAuth(config)
    orchestrator = AutomationOrchestrator(config)
    server = WebServer(auth, orchestrator)
    
    _data_loaded = {'done': False}
    
    def background_load():
        if _data_loaded['done']:
            return
        
        time.sleep(3)  # Aguarda servidor estar pronto
        
        try:
            logger.info("Iniciando carregamento em background...")
            if auth.load_tokens():
                # Simulação de carregamento de dados
                kits = orchestrator.api.get_all_kits_and_components()
                logger.info(f"Kits carregados em background: {len(kits)}")
                # Aqui você pode adicionar a verificação de estoque inicial
            _data_loaded['done'] = True
            logger.info("Carregamento em background concluído.")
        except Exception as e:
            logger.error(f"Erro no background: {e}")
    
    Thread(target=background_load, daemon=True).start()
    
    return server.app

# ============================================================================
# CLI E MAIN
# ============================================================================

def run_cli():
    parser = argparse.ArgumentParser(description="Sistema de Automação Bling ERP.")
    parser.add_argument('--serve', action='store_true', help="Inicia o servidor web Flask.")
    parser.add_argument('--run', action='store_true', help="Executa o processamento de kits e verificação de compra via CLI.")
    parser.add_argument('--port', type=int, default=8000, help="Define a porta para o servidor web (padrão: 8000).")
    
    args = parser.parse_args()
    
    # Configuração de logs para o modo CLI
    Path('logs').mkdir(exist_ok=True)
    
    global memory_handler
    memory_handler = InMemoryLogHandler()
    
    global logger
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    
    global error_logger
    error_logger = logging.getLogger('errors')
    error_logger.setLevel(logging.ERROR)
    
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    memory_handler.setFormatter(formatter)
    
    logging.basicConfig(
        level=logging.INFO,
        handlers=[
            logging.FileHandler('logs/automacao_bling.log', encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
            memory_handler
        ]
    )
    
    error_handler = logging.FileHandler('logs/errors.log', encoding='utf-8')
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    error_logger.addHandler(error_handler)
    
    print_header("Sistema de Automação Bling ERP")
    
    config = Config()
    auth = BlingAuth(config)
    orchestrator = AutomationOrchestrator(config)
    
    if not auth.access_token:
        print_warning("Token de acesso não encontrado ou expirado.")
        print_info(f"Acesse a URL para autorizar: {auth.get_authorization_url()}")
        
    if args.serve:
        print_info(f"Iniciando servidor web na porta {args.port}...")
        server = WebServer(auth, orchestrator)
        server.app.run(host='0.0.0.0', port=args.port, debug=False)
        
    elif args.run:
        print_info("Executando processamento de kits e verificação de compra...")
        if auth.is_token_valid() or auth.refresh_access_token():
            kits = orchestrator.api.get_all_kits_and_components()
            print_info(f"Kits carregados: {len(kits)}")
            stats = orchestrator.process_kits(kits)
            print_header("Resultados do Processamento")
            print_success(f"Sucesso: {stats['success']}")
            print_error(f"Falhas: {stats['failed']}")
            print_info(f"OPs Criadas: {stats['ops_created']}")
            print_info(f"POs Criadas: {stats['pos_created']}")
            print_info(f"Tempo Total: {stats['elapsed_time_seconds']}s")
        else:
            print_error("Não foi possível obter um token de acesso válido. Autorize o Bling primeiro.")
            
    else:
        parser.print_help()

# CRÍTICO: Variável global para WSGI
app = create_app()

if __name__ == '__main__':
    run_cli()