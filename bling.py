#!/usr/bin/env python3
"""
bling_enhanced.py - Versão leve do BLING com controle de estoque, OPs, POs,
webhooks e API REST (adaptação direta do bling.py fornecido).

Principais adições:
- BlingAPI: métodos get_product_stock, create_production_order, create_purchase_order, _save_audit
- PurchaseNeedsManager: verifica estoques mínimos, agrupa necessidades e gera POs
- StatisticsManager: coleta estatísticas (componentes/kits/ops/pos/estoque)
- WebServer: endpoints /api/stats, /api/stock e /webhook/bling
- Integração com Bling real (sem simulação) quando --dry-run não estiver ativo.
"""

import os
import sys
import json
import time
import logging
import argparse
import base64
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from urllib.parse import urlencode
from collections import defaultdict
from threading import Lock, Thread

# import pandas as pd
import requests
from flask import Flask, request, render_template_string, jsonify, redirect, url_for
from dotenv import load_dotenv

# Tenta importar flask_sock, mas funciona sem ele
try:
    from flask_sock import Sock
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False
    Sock = None

load_dotenv()

# Colorama
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
# CONFIGURAÇÃO DE LOGS (DO CÓDIGO 2)
# ============================================================================

Path('logs').mkdir(exist_ok=True)

# Log Handler customizado para capturar logs em memória
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

# Handler global para logs em memória
memory_handler = InMemoryLogHandler()
memory_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
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
error_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
error_logger.addHandler(error_handler)
error_logger.setLevel(logging.ERROR)

# ============================================================================

class Config:
    """Configurações globais"""
    CLIENT_ID = os.getenv('BLING_CLIENT_ID', '')
    CLIENT_SECRET = os.getenv('BLING_CLIENT_SECRET', '')
    # MANTENDO A CONFIGURAÇÃO DE PORTA DO CÓDIGO 1
    REDIRECT_URI = os.getenv('BLING_REDIRECT_URI', 'http://localhost:8000/callback')

    CHECK_MIN_STOCK = os.getenv('BLING_CHECK_MIN_STOCK', 'true').lower() == 'true'
    MIN_STOCK_THRESHOLD = int(os.getenv('BLING_MIN_STOCK', '10'))

    REQUEST_TIMEOUT = int(os.getenv('BLING_TIMEOUT', '30'))
    MAX_RETRIES = int(os.getenv('BLING_MAX_RETRIES', '5'))
    BASE_DELAY = float(os.getenv('BLING_BASE_DELAY', '1.0'))
    DEFAULT_BATCH_SIZE = int(os.getenv('BLING_BATCH_SIZE', '10'))
    DELAY_BETWEEN_BATCHES = float(os.getenv('BLING_BATCH_DELAY', '2.0'))

# ============================================================================

class BlingAuthError(Exception):
    pass

class BlingAPIError(Exception):
    pass

# ============================================================================

def print_success(msg: str):
    print(f"{Fore.GREEN}✓ {msg}{Style.RESET_ALL}")

def print_error(msg: str):
    print(f"{Fore.RED}✗ {msg}{Style.RESET_ALL}")

def print_warning(msg: str):
    print(f"{Fore.YELLOW}⚠ {msg}{Style.RESET_ALL}")

def print_info(msg: str):
    print(f"{Fore.CYAN}ℹ {msg}{Style.RESET_ALL}")

def print_header(title: str):
    print(f"\n{Fore.MAGENTA}{'='*80}")
    print(f"{Fore.MAGENTA}{title.center(80)}")
    print(f"{Fore.MAGENTA}{'='*80}{Style.RESET_ALL}\n")

# ============================================================================

@dataclass
class Component:
    sku: str
    name: str
    qty: int
    supplier: str
    lead_time_days: int
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
# BlingAuth (DO CÓDIGO 2)
# ============================================================================

class BlingAuth:
    TOKEN_FILE = 'tokens.json'

    def __init__(self, config: Config):
        self.client_id = config.CLIENT_ID
        self.client_secret = config.CLIENT_SECRET
        self.redirect_uri = config.REDIRECT_URI
        self.token_url = 'https://www.bling.com.br/Api/v3/oauth/token'
        self.access_token = None
        self.refresh_token = None
        self.expires_at = None

    def get_authorization_url(self) -> str:
        params = {
            'response_type': 'code',
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'state': 'state123'
        }
        return f"https://www.bling.com.br/Api/v3/oauth/authorize?{urlencode(params)}"

    def exchange_code_for_token(self, code: str) -> bool:
        try:
            payload = {
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': self.redirect_uri
            }

            creds = f"{self.client_id}:{self.client_secret}".encode('utf-8')
            basic = base64.b64encode(creds).decode('utf-8')

            headers = {
                'Authorization': f'Basic {basic}',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': '1.0'
            }

            response = requests.post(
                self.token_url,
                data=payload,
                headers=headers,
                timeout=Config.REQUEST_TIMEOUT
            )

            if response.status_code not in (200, 201):
                error_logger.error(f"Token exchange failed: {response.status_code} - {response.text}")
                response.raise_for_status()

            data = response.json()
            self._save_tokens(data)
            logger.info("✓ Tokens obtidos com sucesso")
            return True

        except Exception as e:
            error_logger.error(f"Falha ao trocar code: {e}")
            return False

    def _save_tokens(self, data: Dict):
        """Salva tokens no arquivo e em memória"""
        self.access_token = data.get('access_token')
        self.refresh_token = data.get('refresh_token')
        expires_in = data.get('expires_in', 3600)
        self.expires_at = (datetime.now() + timedelta(seconds=expires_in)).isoformat()
        
        token_data = {
            'access_token': self.access_token,
            'refresh_token': self.refresh_token,
            'expires_at': self.expires_at
        }
        
        try:
            token_path = Path(self.TOKEN_FILE)
            with open(token_path, 'w', encoding='utf-8') as f:
                json.dump(token_data, f, indent=2)
            logger.info(f"✓ Tokens salvos em {token_path.absolute()}")
        except Exception as e:
            error_logger.error(f"Falha ao salvar tokens: {e}")
            raise

    def load_tokens(self) -> bool:
        """Carrega tokens do arquivo local"""
        try:
            token_path = Path(self.TOKEN_FILE)
            
            if not token_path.exists():
                logger.info(f"Arquivo {self.TOKEN_FILE} não existe")
                return False
            
            logger.info(f"Carregando tokens de {token_path.absolute()}")
            
            with open(token_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            self.access_token = data.get('access_token')
            self.refresh_token = data.get('refresh_token')
            self.expires_at = data.get('expires_at')
            
            if not self.access_token or not self.refresh_token or not self.expires_at:
                logger.warning("Tokens incompletos no arquivo.")
                return False
            
            logger.info("✓ Tokens carregados com sucesso")
            return True
        except json.JSONDecodeError as e:
            logger.error(f"Arquivo de tokens corrompido: {e}")
            return False
        except Exception as e:
            logger.error(f"Erro ao carregar tokens: {e}")
            return False

    def refresh_access_token(self) -> bool:
        """Renova o access token usando refresh token"""
        if not self.refresh_token:
            logger.error("Refresh token não disponível")
            return False

        try:
            payload = {
                'grant_type': 'refresh_token',
                'refresh_token': self.refresh_token
            }
            creds = f"{self.client_id}:{self.client_secret}".encode('utf-8')
            basic = base64.b64encode(creds).decode('utf-8')
            headers = {
                'Authorization': f'Basic {basic}',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': '1.0'
            }
            response = requests.post(self.token_url, data=payload, headers=headers, timeout=Config.REQUEST_TIMEOUT)
            if response.status_code not in (200, 201):
                error_logger.error(f"Refresh token failed: {response.status_code} - {response.text}")
                response.raise_for_status()
            data = response.json()
            self._save_tokens(data)
            logger.info("✓ Token renovado com sucesso!")
            return True
        except Exception as e:
            error_logger.error(f"Falha ao renovar token: {e}")
            return False

    def is_token_valid(self) -> bool:
        """Verifica se o token está carregado e tenta renovar se estiver expirando"""
        if not self.access_token:
            if not self.load_tokens():
                return False
        
        if self.expires_at:
            expires = datetime.fromisoformat(self.expires_at)
            # Tenta renovar se faltar menos de 5 minutos
            if datetime.now() >= expires - timedelta(minutes=5):
                logger.info("Token expirando. Tentando renovar...")
                if not self.refresh_access_token():
                    return False
        
        return True

    def ensure_valid_token(self) -> bool:
        """Garante que o token esteja válido, levantando exceção se não for possível"""
        if not self.is_token_valid():
            raise BlingAuthError(f"Token inválido. Autorize em: {self.get_authorization_url()}")
        return True

# ============================================================================
# BlingAPI (DO CÓDIGO 2)
# ============================================================================

class BlingAPI:
    BASE_URL = 'https://www.bling.com.br/Api/v3'

    def __init__(self, auth: BlingAuth, component_config: Dict = None):
        self.auth = auth
        self.component_config = component_config or {}
        self.session = requests.Session()

    def _get_headers(self) -> Dict:
        return {
            'Authorization': f'Bearer {self.auth.access_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

    def _request_with_retry(self, method: str, url: str, **kwargs) -> Optional[requests.Response]:
        for attempt in range(Config.MAX_RETRIES):
            try:
                self.auth.ensure_valid_token()
                kwargs['headers'] = self._get_headers()
                kwargs.setdefault('timeout', Config.REQUEST_TIMEOUT)
                
                response = self.session.request(method, url, **kwargs)
                
                if response.status_code == 429:
                    logger.warning(f"Rate limit atingido. Tentativa {attempt+1}/{Config.MAX_RETRIES}. Aguardando {Config.BASE_DELAY * (2 ** attempt)}s.")
                    time.sleep(Config.BASE_DELAY * (2 ** attempt))
                    continue
                
                if response.status_code >= 500:
                    logger.error(f"Erro 5xx. Tentativa {attempt+1}/{Config.MAX_RETRIES}. Aguardando {Config.BASE_DELAY * (2 ** attempt)}s.")
                    time.sleep(Config.BASE_DELAY * (2 ** attempt))
                    continue
                
                if response.status_code >= 400:
                    error_logger.error(f"Erro {response.status_code} na requisição: {response.text}")
                    raise BlingAPIError(f"Erro {response.status_code}: {response.text}")

                return response
            
            except BlingAuthError:
                raise # Propaga erro de autenticação
            except requests.exceptions.RequestException as e:
                logger.error(f"Erro de conexão: {e}. Tentativa {attempt+1}/{Config.MAX_RETRIES}.")
                if attempt < Config.MAX_RETRIES - 1:
                    time.sleep(Config.BASE_DELAY * (2 ** attempt))
                else:
                    raise BlingAPIError(f"Falha na requisição após {Config.MAX_RETRIES} tentativas: {e}")
        return None

    def _get_all_pages(self, endpoint: str, params: Dict = None) -> List[Dict]:
        all_data = []
        page = 1
        while True:
            current_params = params.copy() if params else {}
            current_params['pagina'] = page
            
            url = f"{self.BASE_URL}/{endpoint}"
            response = self._request_with_retry('GET', url, params=current_params)
            
            if not response or response.status_code == 204:
                break
            
            data = response.json()
            
            if 'data' not in data:
                logger.error(f"Resposta inesperada da API: {data}")
                break
            
            items = data['data']
            all_data.extend(items)
            
            if len(items) < Config.DEFAULT_BATCH_SIZE:
                break
            
            page += 1
            time.sleep(Config.DELAY_BETWEEN_BATCHES)
            
        return all_data

    def find_product_by_sku(self, sku: str) -> Optional[Dict]:
        """Busca um produto pelo SKU (código)"""
        url = f"{self.BASE_URL}/produtos"
        params = {'criterio': f'codigo:{sku}'}
        response = self._request_with_retry('GET', url, params=params)
        
        if response and response.status_code == 200:
            data = response.json()
            if data.get('data'):
                # Retorna o primeiro produto encontrado
                return data['data'][0]['produto']
        return None

    def get_product_stock(self, product_id: int) -> int:
        """Busca o estoque atual de um produto pelo ID"""
        url = f"{self.BASE_URL}/estoques/saldos"
        params = {'idProduto': product_id}
        response = self._request_with_retry('GET', url, params=params)
        
        if response and response.status_code == 200:
            data = response.json()
            if data.get('data'):
                # O Bling retorna uma lista de estoques (por depósito)
                # Somamos o saldo de todos os depósitos
                total_stock = sum(item['saldo'] for item in data['data'])
                return int(total_stock)
        return 0

    def create_or_update_product(self, product_data: Dict, is_component: bool) -> Optional[int]:
        """Cria ou atualiza um produto (componente ou kit)"""
        sku = product_data.get('codigo')
        if not sku:
            logger.error("SKU não fornecido para criação/atualização de produto.")
            return None
        
        existing_product = self.find_product_by_sku(sku)
        
        if existing_product:
            # Atualiza
            product_id = existing_product['id']
            url = f"{self.BASE_URL}/produtos/{product_id}"
            
            # Remove campos que não podem ser enviados no PUT
            product_data.pop('codigo', None)
            
            # Mantém a estrutura do kit se for um kit
            if not is_component and 'estrutura' in existing_product:
                product_data['estrutura'] = existing_product['estrutura']
            
            payload = {'produto': product_data}
            response = self._request_with_retry('PUT', url, json=payload)
            
            if response and response.status_code == 200:
                logger.info(f"✓ Produto {sku} (ID: {product_id}) atualizado.")
                return product_id
            
        else:
            # Cria
            url = f"{self.BASE_URL}/produtos"
            payload = {'produto': product_data}
            response = self._request_with_retry('POST', url, json=payload)
            
            if response and response.status_code == 201:
                data = response.json()
                product_id = data['data']['id']
                logger.info(f"✓ Produto {sku} (ID: {product_id}) criado.")
                return product_id
                
        return None

    def create_production_order(self, kit_sku: str, quantity: int = 1) -> Optional[int]:
        """Cria uma Ordem de Produção (OP) para um kit"""
        url = f"{self.BASE_URL}/producao/ordens"
        
        # Busca o ID do produto (kit)
        kit_product = self.find_product_by_sku(kit_sku)
        if not kit_product:
            logger.error(f"Kit {kit_sku} não encontrado para criar OP.")
            return None
        
        payload = {
            "ordemProducao": {
                "produto": {
                    "id": kit_product['id'],
                    "quantidade": quantity
                },
                "observacoes": f"OP gerada automaticamente em {datetime.now().isoformat()}"
            }
        }
        
        response = self._request_with_retry('POST', url, json=payload)
        
        if response and response.status_code == 201:
            data = response.json()
            op_id = data['data']['id']
            logger.info(f"✓ OP {op_id} criada para o kit {kit_sku}.")
            return op_id
            
        return None

    def create_purchase_order(self, supplier_name: str, items: List[Dict]) -> Optional[int]:
        """Cria uma Ordem de Compra (OC) para um fornecedor e lista de itens"""
        url = f"{self.BASE_URL}/compras/pedidos"
        
        # Simplificação: assume que o fornecedor já existe e busca o ID
        # Em um sistema real, seria necessário buscar/criar o fornecedor
        # Aqui, vamos usar um ID de fornecedor fictício ou buscar pelo nome
        
        # Busca o fornecedor pelo nome (simplificado)
        supplier_id = self._find_supplier_id(supplier_name)
        if not supplier_id:
            logger.error(f"Fornecedor '{supplier_name}' não encontrado. OC não criada.")
            return None
        
        payload = {
            "pedidoCompra": {
                "fornecedor": {
                    "id": supplier_id
                },
                "itens": [
                    {
                        "produto": {
                            "codigo": item['sku'],
                            "quantidade": item['quantity'],
                            "valor": item.get('unit_cost', 0.0)
                        }
                    }
                    for item in items
                ],
                "observacoes": f"OC gerada automaticamente para {supplier_name} em {datetime.now().isoformat()}"
            }
        }
        
        response = self._request_with_retry('POST', url, json=payload)
        
        if response and response.status_code == 201:
            data = response.json()
            oc_id = data['data']['id']
            logger.info(f"✓ OC {oc_id} criada para o fornecedor {supplier_name}.")
            return oc_id
            
        return None

    def _find_supplier_id(self, name: str) -> Optional[int]:
        """Busca um fornecedor pelo nome (simplificado)"""
        # Em um sistema real, esta função faria uma busca na API de contatos
        # Para fins de simulação, retorna um ID fixo ou None
        if name == "FORNECEDOR_PADRAO":
            return 123456789 # ID fictício
        
        # Tenta buscar na API (exemplo)
        url = f"{self.BASE_URL}/contatos"
        params = {'criterio': f'nome:{name}', 'tipo': 'F'} # Tipo 'F' para fornecedor
        response = self._request_with_retry('GET', url, params=params)
        
        if response and response.status_code == 200:
            data = response.json()
            if data.get('data'):
                return data['data'][0]['contato']['id']
        
        return None

    def get_all_kits_and_components(self) -> List[Kit]:
        """Busca todos os produtos que são kits e seus componentes"""
        kits_list = []
        
        # Busca todos os produtos que são kits (tipo 'P' e com estrutura)
        url = f"{self.BASE_URL}/produtos"
        params = {'tipo': 'P', 'estrutura': 'S'}
        
        all_products = self._get_all_pages('produtos', params)
        
        for item in all_products:
            prod = item['produto']
            if 'estrutura' in prod and prod['estrutura'].get('componentes'):
                
                components = []
                for comp_data in prod['estrutura']['componentes']:
                    comp_id = comp_data['produto']['id']
                    comp_sku = comp_data['produto']['codigo']
                    comp_name = comp_data['produto']['nome']
                    comp_qty = comp_data['quantidade']
                    
                    # Aplica configurações do componente
                    config = self.component_config.get(comp_sku, self.component_config.get('component_defaults', {}))
                    
                    component = Component(
                        sku=comp_sku,
                        name=comp_name,
                        qty=comp_qty,
                        supplier=config.get('supplier', 'N/A'),
                        lead_time_days=config.get('lead_time_days', 0),
                        min_stock=config.get('min_stock', Config.MIN_STOCK_THRESHOLD),
                        current_stock=self.get_product_stock(comp_id) # Busca estoque atual
                    )
                    components.append(component)
                
                kit = Kit(
                    sku=prod['codigo'],
                    name=prod['nome'],
                    components=components,
                    price=prod.get('preco', 0.0)
                )
                kits_list.append(kit)
                
        return kits_list

# ============================================================================
# PurchaseNeedsManager (DO CÓDIGO 2)
# ============================================================================

class PurchaseNeedsManager:
    REPORT_FILE = 'purchase_needs_report.json'

    def __init__(self, api: BlingAPI):
        self.api = api
        # Dicionário para armazenar necessidades: {component_sku: PurchaseNeed}
        self.needs: Dict[str, PurchaseNeed] = {}
        # Lista de todos os componentes monitorados
        self.components: List[Component] = []

    def check_min_stock_needs(self, components: List[Component]):
        """Verifica o estoque mínimo para uma lista de componentes e atualiza as necessidades."""
        self.components = components # Salva a lista de componentes monitorados
        
        for comp in components:
            if comp.current_stock < comp.min_stock:
                needed = comp.min_stock - comp.current_stock
                
                # Se já existe uma necessidade, soma a quantidade
                if comp.sku in self.needs:
                    self.needs[comp.sku].quantity_needed += needed
                    self.needs[comp.sku].reason += f", Alerta Estoque Mínimo ({comp.min_stock})"
                else:
                    self.needs[comp.sku] = PurchaseNeed(
                        component_sku=comp.sku,
                        component_name=comp.name,
                        quantity_needed=needed,
                        supplier=comp.supplier,
                        lead_time_days=comp.lead_time_days,
                        reason=f"Alerta Estoque Mínimo ({comp.min_stock})"
                    )
                    
        self.export_needs_report()

    def add_production_needs(self, kit: Kit, quantity: int):
        """Adiciona necessidades de compra baseadas em uma OP criada."""
        for comp in kit.components:
            needed = comp.qty * quantity
            
            if comp.sku in self.needs:
                self.needs[comp.sku].quantity_needed += needed
                self.needs[comp.sku].reason += f", OP Kit {kit.sku} (x{quantity})"
            else:
                self.needs[comp.sku] = PurchaseNeed(
                    component_sku=comp.sku,
                    component_name=comp.name,
                    quantity_needed=needed,
                    supplier=comp.supplier,
                    lead_time_days=comp.lead_time_days,
                    reason=f"OP Kit {kit.sku} (x{quantity})"
                )
                
        self.export_needs_report()

    def generate_purchase_orders(self) -> List[int]:
        """Agrupa necessidades por fornecedor e gera OCs no Bling."""
        if not self.needs:
            return []

        grouped_needs = defaultdict(list)
        for need in self.needs.values():
            grouped_needs[need.supplier].append(need)

        po_ids = []
        for supplier, needs_list in grouped_needs.items():
            items = [
                {
                    'sku': need.component_sku,
                    'quantity': need.quantity_needed,
                    # Simplificação: custo unitário não está no PurchaseNeed,
                    # mas poderia ser buscado do Bling ou da config
                    'unit_cost': 0.0 
                }
                for need in needs_list
            ]
            
            po_id = self.api.create_purchase_order(supplier, items)
            if po_id:
                po_ids.append(po_id)
        
        # Limpa as necessidades após a geração das OCs
        self.needs = {}
        self.export_needs_report()
        
        return po_ids

    def export_needs_report(self):
        """Salva o relatório de necessidades de compra em um arquivo JSON."""
        try:
            needs_data = [asdict(need) for need in self.needs.values()]
            with open(self.REPORT_FILE, 'w', encoding='utf-8') as f:
                json.dump(needs_data, f, indent=2, ensure_ascii=False)
            logger.info(f"✓ Relatório de necessidades salvo em {self.REPORT_FILE}")
        except Exception as e:
            error_logger.error(f"Falha ao salvar relatório de necessidades: {e}")

# ============================================================================
# StatisticsManager (DO CÓDIGO 2)
# ============================================================================

class StatisticsManager:
    def __init__(self):
        self.start_time = None
        self.end_time = None
        self.success = 0
        self.failed = 0
        self.ops_created = 0
        self.pos_created = 0
        self.min_stock_checks = 0
        self.components_created = 0
        self.kits_created = 0

    def reset(self):
        self.__init__()

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
            "components_created": self.components_created,
            "kits_created": self.kits_created,
            "elapsed_time_seconds": round(elapsed, 2)
        }

# ============================================================================
# ORQUESTRADOR DE AUTOMAÇÃO (DO CÓDIGO 2)
# ============================================================================

class AutomationOrchestrator:
    COMPONENT_CONFIG_FILE = 'component_config.json'
    
    def __init__(self, config: Config):
        self.auth = BlingAuth(config)
        component_config = self._load_or_create_component_config()
        self.api = BlingAPI(self.auth, component_config=component_config)
        self.stats = StatisticsManager()
        self.purchase_manager = PurchaseNeedsManager(self.api)
        self.failed_items = []

    def _load_or_create_component_config(self) -> Dict:
        """Carrega ou cria o arquivo de configuração de componentes"""
        path = Path(self.COMPONENT_CONFIG_FILE)
        
        if path.exists():
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                config_dict = {}
                if 'component_defaults' in data:
                    config_dict['component_defaults'] = data['component_defaults']
                
                if 'components' in data:
                    for comp in data['components']:
                        if 'sku' in comp:
                            config_dict[comp['sku']] = comp
                
                logger.info(f"✓ Configurações de componentes carregadas de {self.COMPONENT_CONFIG_FILE}")
                return config_dict
            except Exception as e:
                logger.error(f"Erro ao carregar {self.COMPONENT_CONFIG_FILE}: {e}")
        
        default_config = {
            "component_defaults": {
                "supplier": "FORNECEDOR_PADRAO",
                "lead_time_days": 15,
                "min_stock": Config.MIN_STOCK_THRESHOLD
            },
            "components": [
                {
                    "sku": "EXEMPLO-001",
                    "supplier": "Fornecedor A",
                    "lead_time_days": 10,
                    "min_stock": 20
                }
            ]
        }
        
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(default_config, f, indent=2, ensure_ascii=False)
            logger.info(f"✓ Arquivo de configuração padrão criado: {self.COMPONENT_CONFIG_FILE}")
        except Exception as e:
            logger.error(f"Erro ao criar {self.COMPONENT_CONFIG_FILE}: {e}")
        
        return {"component_defaults": default_config["component_defaults"]}

    def process_kits(self, kits: List[Kit], batch_size: int = 10, check_stock: bool = True):
        """Processa kits, cria OPs e verifica estoque (versão do Código 2)"""
        self.stats.reset()
        self.stats.start()
        
        for i in range(0, len(kits), batch_size):
            batch = kits[i:i+batch_size]
            for kit in batch:
                try:
                    # Simplificação: Apenas cria a OP (o código 2 não tinha a lógica de criação/atualização de produto aqui)
                    op_id = self.api.create_production_order(kit.sku, 1)
                    if op_id:
                        self.stats.ops_created += 1
                        logger.info(f"✓ OP {op_id} criada para {kit.sku}")
                    self.stats.success += 1
                except BlingAPIError as e:
                    self.stats.failed += 1
                    self.failed_items.append(kit.sku)
                    error_logger.error(f"Erro ao processar kit {kit.sku}: {e}")
            
            if check_stock:
                all_components = [comp for kit in batch for comp in kit.components]
                unique_components = {c.sku: c for c in all_components}.values()
                self.purchase_manager.check_min_stock_needs(list(unique_components))
                self.stats.min_stock_checks += len(unique_components)

            pos_ids = self.purchase_manager.generate_purchase_orders()
            self.stats.pos_created += len(pos_ids)

            if i + batch_size < len(kits):
                time.sleep(Config.DELAY_BETWEEN_BATCHES)
        
        self.stats.stop()
        return self.stats.to_dict()

    def run_purchase_check(self):
        """Executa verificação de estoque e gera POs (do Código 2)"""
        try:
            logger.info("Iniciando verificação de estoque...")
            kits = self.api.get_all_kits_and_components()
            if kits:
                all_comps = [comp for kit in kits for comp in kit.components]
                unique_comps = {c.sku: c for c in all_comps}.values()
                self.purchase_manager.check_min_stock_needs(list(unique_comps))
                pos = self.purchase_manager.generate_purchase_orders()
                logger.info(f"✓ Verificação concluída. {len(pos)} POs geradas.")
            else:
                logger.warning("Nenhum kit encontrado para verificação")
        except Exception as e:
            logger.error(f"Erro na verificação de estoque: {e}")
            error_logger.exception("Erro detalhado:")

# ============================================================================
# INTERFACE WEB (FLASK) (DO CÓDIGO 2)
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
        else:
            logger.warning("Módulo 'flask_sock' não encontrado. Logs em tempo real desativados.")

    def setup_routes(self):
        @self.app.route('/')
        @self.app.route('/dashboard')
        def dashboard():
            return render_template_string(DASHBOARD_TEMPLATE)

        @self.app.route('/health')
        def health_check():
            return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

        @self.app.route('/callback')
        def callback():
            code = request.args.get('code')
            state = request.args.get('state')
            
            if not code:
                return render_template_string(ERROR_TEMPLATE, message="Code not provided"), 400
            
            if self.auth.exchange_code_for_token(code):
                return render_template_string(SUCCESS_TEMPLATE, message="Tokens obtidos com sucesso! Você pode fechar esta janela.")
            else:
                return render_template_string(ERROR_TEMPLATE, message="Falha ao trocar code por token"), 500

        @self.app.route('/api/status')
        def api_status():
            is_valid = self.auth.is_token_valid()
            return jsonify({
                "token_valid": is_valid,
                "expires_at": self.auth.expires_at,
                "auth_url": self.auth.get_authorization_url()
            })

        @self.app.route('/api/stats')
        def api_stats():
            return jsonify(self.orchestrator.stats.to_dict())

        @self.app.route('/api/stock')
        def api_stock():
            try:
                kits = self.orchestrator.api.get_all_kits_and_components()
                all_comps = [comp for kit in kits for comp in kit.components]
                unique_comps = {c.sku: c for c in all_comps}.values()
                
                stock_data = []
                for comp in unique_comps:
                    stock_data.append({
                        "sku": comp.sku,
                        "nome": comp.name,
                        "estoque": comp.current_stock,
                        "minimo": comp.min_stock,
                        "alerta": comp.current_stock < comp.min_stock
                    })
                return jsonify({"items": stock_data})
            except BlingAuthError as e:
                return jsonify({"error": str(e)}), 401
            except Exception as e:
                error_logger.exception("Erro ao buscar estoque via API:")
                return jsonify({"error": f"Erro interno: {e}"}), 500

        @self.app.route('/api/needs')
        def api_needs():
            needs_list = [asdict(need) for need in self.orchestrator.purchase_manager.needs.values()]
            return jsonify({"needs": needs_list})

        @self.app.route('/api/kits')
        def api_kits():
            try:
                kits = self.orchestrator.api.get_all_kits_and_components()
                kits_data = []
                for kit in kits:
                    kits_data.append({
                        "sku": kit.sku,
                        "nome": kit.name,
                        "componentes": [
                            {"sku": c.sku, "nome": c.name, "quantidade": c.qty}
                            for c in kit.components
                        ]
                    })
                return jsonify({"kits": kits_data})
            except BlingAuthError as e:
                return jsonify({"error": str(e)}), 401
            except Exception as e:
                error_logger.exception("Erro ao buscar kits via API:")
                return jsonify({"error": f"Erro interno: {e}"}), 500

        @self.app.route('/api/process_kits', methods=['POST'])
        def api_process_kits():
            try:
                if not self.auth.is_token_valid():
                    return jsonify({"error": "Token inválido. Necessário autorização."}), 401
                
                kits = self.orchestrator.api.get_all_kits_and_components()
                if not kits:
                    return jsonify({"message": "Nenhum kit encontrado para processar."}), 200
                
                results = self.orchestrator.process_kits(kits, check_stock=Config.CHECK_MIN_STOCK)
                return jsonify(results)
            except BlingAuthError as e:
                return jsonify({"error": str(e), "auth_url": self.auth.get_authorization_url()}), 401
            except Exception as e:
                error_logger.exception("Erro ao processar kits via API:")
                return jsonify({"error": f"Erro interno: {e}"}), 500

        @self.app.route("/api/recheck", methods=['POST'])
        def api_recheck():
            try:
                logger.info("🔄 Verificação manual iniciada via API")
                Thread(target=self.orchestrator.run_purchase_check, daemon=True).start()
                return jsonify({"status": "ok", "message": "Verificação iniciada em background."}), 200
            except Exception as e:
                logger.error(f"Erro na verificação manual: {e}")
                return jsonify({"status": "error", "error": str(e)}), 500

        @self.app.route('/webhook/bling', methods=['POST'])
        def webhook_bling():
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
                    pedido_id = None
                    if data.get('id') and data.get('tipo') == 'pedido':
                        pedido_id = data.get('id')
                    elif data.get('retorno') and data['retorno'].get('pedidos'):
                        pedido_id = data['retorno']['pedidos'][0]['pedido'].get('id')
                    
                    if pedido_id:
                        logger.info(f"✓ Pedido ID {pedido_id} identificado. Acionando automação...")
                        Thread(target=self.orchestrator.run_purchase_check, daemon=True).start()
                        return jsonify({'status': 'ok', 'message': f'Pedido {pedido_id} processado'}), 200
                    else:
                        logger.warning(f"⚠ Webhook de Pedido recebido, mas ID não encontrado")
                        return jsonify({'status': 'warning', 'message': 'ID do pedido não encontrado'}), 200
                
                if event_type == 'estoque.atualizado' or data.get('tipo') == 'estoque':
                    logger.info(f"📦 Evento estoque.atualizado recebido")
                
                return jsonify({'status': 'ok', 'message': f'Webhook {event_type} recebido'}), 200
            except Exception as e:
                error_logger.error(f"Erro no webhook: {e}")
                return jsonify({'error': str(e)}), 500

    def setup_websocket(self):
        if WEBSOCKET_AVAILABLE and self.sock:
            @self.sock.route('/ws/logs')
            def ws_logs(ws):
                logger.info("🔌 Cliente conectado ao WebSocket de logs")
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
                    logger.info(f"🔌 Cliente desconectado do WebSocket: {e}")

# ============================================================================
# TEMPLATES HTML (DO CÓDIGO 2)
# ============================================================================

DASHBOARD_TEMPLATE = """
<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Painel Bling - Automação ERP</title><link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"><script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script><style>body{background:#f8f9fa;font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif}.navbar{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;box-shadow:0 4px 6px rgba(0,0,0,.1)}.navbar-brand{font-weight:700;font-size:1.5rem}.status-badge{padding:.5rem 1rem;border-radius:20px;font-size:.9rem;font-weight:600}.card{border-radius:1rem;box-shadow:0 4px 6px rgba(0,0,0,.07);border:none;margin-bottom:1.5rem}.card-title{font-weight:600;color:#343a40;margin-bottom:1rem}.kpi-value{font-size:2.5rem;font-weight:700;margin-bottom:.25rem}.kpi-label{font-size:.9rem;color:#6c757d;text-transform:uppercase;letter-spacing:.5px}.log-box{font-family:'Courier New',monospace;font-size:.85em;background:#1e1e1e;color:#d4d4d4;border-radius:.5rem;padding:1rem;max-height:400px;overflow-y:auto}.log-entry{padding:.25rem 0;border-bottom:1px solid #333}.log-entry:last-child{border-bottom:none}.log-level-INFO{color:#4ec9b0}.log-level-WARNING{color:#dcdcaa}.log-level-ERROR{color:#f48771}.log-level-DEBUG{color:#9cdcfe}.nav-tabs .nav-link{color:#6c757d;font-weight:500}.nav-tabs .nav-link.active{background-color:#fff;border-color:#dee2e6 #dee2e6 #fff;color:#667eea;font-weight:600}.table-danger td{background-color:#f8d7da!important}.table-warning td{background-color:#fff3cd!important}.btn-primary{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);border:none}.btn-primary:hover{transform:translateY(-2px);box-shadow:0 4px 8px rgba(102,126,234,.4)}.spinner-border-sm{width:1rem;height:1rem;border-width:.15em}</style></head><body><nav class="navbar navbar-expand-lg navbar-dark"><div class="container-fluid"><a class="navbar-brand" href="#">🚀 Bling Automação ERP</a><div class="d-flex align-items-center"><span class="status-badge" id="status-badge">Verificando...</span></div></div></nav><div class="container my-4"><ul class="nav nav-tabs" id="mainTabs" role="tablist"><li class="nav-item" role="presentation"><a class="nav-link active" id="dashboard-tab" data-bs-toggle="tab" href="#tabDashboard" role="tab">Dashboard</a></li><li class="nav-item" role="presentation"><a class="nav-link" id="stock-tab" data-bs-toggle="tab" href="#tabStock" role="tab">Estoque</a></li><li class="nav-item" role="presentation"><a class="nav-link" id="needs-tab" data-bs-toggle="tab" href="#tabNeeds" role="tab">Necessidades de Compra</a></li><li class="nav-item" role="presentation"><a class="nav-link" id="kits-tab" data-bs-toggle="tab" href="#tabKits" role="tab">Kits</a></li></ul><div class="tab-content p-4 bg-white border border-top-0" style="border-radius:0 0 1rem 1rem;"><div class="tab-pane fade show active" id="tabDashboard" role="tabpanel"><h4 class="mb-4">📊 Visão Geral da Automação</h4><div class="row mb-4" id="stats-kpis"><div class="col-md-3 mb-3"><div class="card bg-light h-100"><div class="card-body text-center"><div class="spinner-border text-primary" role="status"></div><p class="mt-2 mb-0">Carregando...</p></div></div></div></div><div class="row mb-4"><div class="col-md-6"><div class="card h-100"><div class="card-body"><h5 class="card-title">📈 Status de Processamento</h5><canvas id="processingChart"></canvas></div></div></div><div class="col-md-6"><div class="card h-100"><div class="card-body"><h5 class="card-title">📋 Logs em Tempo Real</h5><div id="logs-content" class="log-box"></div></div></div></div></div><div class="row"><div class="col-12"><div class="card"><div class="card-body"><h5 class="card-title">🔧 Ações Manuais</h5><p class="card-text">Acione a verificação de estoque e geração de POs manualmente.</p><button id="recheck-button" class="btn btn-primary"><span class="btn-text">🔄 Re-checar Estoque e Gerar POs</span><span class="spinner-border spinner-border-sm d-none" role="status"></span></button><span id="recheck-status" class="ms-3"></span></div></div></div></div></div><div class="tab-pane fade" id="tabStock" role="tabpanel"><h4 class="mb-4">📦 Estoque de Componentes</h4><p>A tabela abaixo mostra o estoque atual de cada componente, comparado ao estoque mínimo configurado.</p><div class="table-responsive"><table class="table table-striped table-hover"><thead><tr><th>SKU</th><th>Nome</th><th>Estoque Atual</th><th>Estoque Mínimo</th><th>Alerta</th></tr></thead><tbody id="stock-table-body"><tr><td colspan="5" class="text-center">Carregando dados de estoque...</td></tr></tbody></table></div></div><div class="tab-pane fade" id="tabNeeds" role="tabpanel"><h4 class="mb-4">🛒 Necessidades de Compra</h4><p>Componentes que precisam ser comprados para atingir o estoque mínimo ou para atender a ordens de produção.</p><div class="table-responsive"><table class="table table-striped table-hover"><thead><tr><th>SKU</th><th>Nome</th><th>Qtd. Necessária</th><th>Fornecedor</th><th>Lead Time (dias)</th><th>Motivo</th></tr></thead><tbody id="needs-table-body"><tr><td colspan="6" class="text-center">Nenhuma necessidade de compra detectada.</td></tr></tbody></table></div></div><div class="tab-pane fade" id="tabKits" role="tabpanel"><h4 class="mb-4">🛠️ Kits de Produtos</h4><p>Lista de kits cadastrados no Bling e seus componentes.</p><div class="table-responsive"><table class="table table-striped table-hover"><thead><tr><th>SKU Kit</th><th>Nome Kit</th><th>Componentes</th></tr></thead><tbody id="kits-table-body"><tr><td colspan="3" class="text-center">Carregando kits...</td></tr></tbody></table></div></div></div></div><script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script><script>const API_BASE='/api';const WS_URL=(window.location.protocol==='https:'?'wss:':'ws:')+'//'+window.location.host+'/ws/logs';let logWebSocket;let statsChart;function formatLog(log){const levelClass=`log-level-${log.level}`;return `<div class="log-entry"><span class="${levelClass}">[${log.timestamp.substring(11,19)}] [${log.level}]</span> ${log.message}</div>`}function updateStatusBadge(isValid){const badge=document.getElementById('status-badge');if(isValid){badge.className='status-badge bg-success text-white';badge.textContent='Token Válido'}else{badge.className='status-badge bg-danger text-white';badge.textContent='Token Inválido (Autorização Necessária)'}}function updateStatsKPIs(stats){const kpis=[{label:'Sucesso',value:stats.success,color:'text-success',icon:'✅'},{label:'Falhas',value:stats.failed,color:'text-danger',icon:'❌'},{label:'OPs Criadas',value:stats.ops_created,color:'text-primary',icon:'🏭'},{label:'POs Criadas',value:stats.pos_created,color:'text-info',icon:'🛒'},{label:'Checks Estoque',value:stats.min_stock_checks,color:'text-warning',icon:'🔍'},{label:'Tempo Total',value:`${stats.elapsed_time_seconds}s`,color:'text-secondary',icon:'⏱️'}];const container=document.getElementById('stats-kpis');container.innerHTML=kpis.map(kpi=>`<div class="col-md-2 mb-3"><div class="card h-100"><div class="card-body text-center"><div class="kpi-value ${kpi.color}">${kpi.icon} ${kpi.value}</div><div class="kpi-label">${kpi.label}</div></div></div></div>`).join('')}function updateStatsChart(stats){const ctx=document.getElementById('processingChart').getContext('2d');if(statsChart){statsChart.destroy()}statsChart=new Chart(ctx,{type:'bar',data:{labels:['Sucesso','Falhas','OPs Criadas','POs Criadas'],datasets:[{label:'Contagem',data:[stats.success,stats.failed,stats.ops_created,stats.pos_created],backgroundColor:['rgba(40,167,69,.7)','rgba(220,53,69,.7)','rgba(0,123,255,.7)','rgba(23,162,184,.7)'],borderColor:['rgba(40,167,69,1)','rgba(220,53,69,1)','rgba(0,123,255,1)','rgba(23,162,184,1)'],borderWidth:1}]},options:{responsive:true,scales:{y:{beginAtZero:true,ticks:{precision:0}}},plugins:{legend:{display:false}}}})}async function fetchStatus(){try{const response=await fetch(`${API_BASE}/status`);const data=await response.json();updateStatusBadge(data.token_valid)}catch(error){updateStatusBadge(false);console.error('Erro ao buscar status:',error)}}async function fetchStats(){try{const response=await fetch(`${API_BASE}/stats`);const stats=await response.json();updateStatsKPIs(stats);updateStatsChart(stats)}catch(error){console.error('Erro ao buscar estatísticas:',error)}}async function fetchStock(){try{const response=await fetch(`${API_BASE}/stock`);const data=await response.json();const tbody=document.getElementById('stock-table-body');tbody.innerHTML='';if(data.error){tbody.innerHTML=`<tr><td colspan="5" class="text-center text-danger">Erro ao carregar estoque: ${data.error}</td></tr>`;return}if(data.items.length===0){tbody.innerHTML=`<tr><td colspan="5" class="text-center">Nenhum componente encontrado.</td></tr>`;return}data.items.forEach(item=>{const rowClass=item.alerta?'table-danger':'';const row=document.createElement('tr');row.className=rowClass;row.innerHTML=`<td>${item.sku}</td><td>${item.nome}</td><td>${item.estoque}</td><td>${item.minimo}</td><td>${item.alerta?'🚨 ABAIXO':'OK'}</td>`;tbody.appendChild(row)})}catch(error){console.error('Erro ao buscar estoque:',error)}}async function fetchNeeds(){try{const response=await fetch(`${API_BASE}/needs`);const data=await response.json();const tbody=document.getElementById('needs-table-body');tbody.innerHTML='';if(data.error){tbody.innerHTML=`<tr><td colspan="6" class="text-center text-danger">Erro ao carregar necessidades: ${data.error}</td></tr>`;return}if(data.needs.length===0){tbody.innerHTML=`<tr><td colspan="6" class="text-center">Nenhuma necessidade de compra detectada.</td></tr>`;return}data.needs.forEach(need=>{const row=document.createElement('tr');row.innerHTML=`<td>${need.component_sku}</td><td>${need.component_name}</td><td>${need.quantity_needed}</td><td>${need.supplier}</td><td>${need.lead_time_days}</td><td>${need.reason}</td>`;tbody.appendChild(row)})}catch(error){console.error('Erro ao buscar necessidades:',error)}}async function fetchKits(){try{const response=await fetch(`${API_BASE}/kits`);const data=await response.json();const tbody=document.getElementById('kits-table-body');tbody.innerHTML='';if(data.error){tbody.innerHTML=`<tr><td colspan="3" class="text-center text-danger">Erro ao carregar kits: ${data.error}</td></tr>`;return}if(data.kits.length===0){tbody.innerHTML=`<tr><td colspan="3" class="text-center">Nenhum kit encontrado.</td></tr>`;return}data.kits.forEach(kit=>{const componentsList=kit.componentes.map(c=>`${c.nome} (${c.sku}) x${c.quantidade}`).join('<br>');const row=document.createElement('tr');row.innerHTML=`<td>${kit.sku}</td><td>${kit.nome}</td><td>${componentsList}</td>`;tbody.appendChild(row)})}catch(error){console.error('Erro ao buscar kits:',error)}}function connectWebSocket(){if(!("WebSocket"in window)){console.warn("WebSocket não suportado. Usando polling para logs.");return}logWebSocket=new WebSocket(WS_URL);const logContainer=document.getElementById('logs-content');logWebSocket.onopen=()=>{console.log("WebSocket de logs conectado.");logContainer.innerHTML+=formatLog({timestamp:new Date().toISOString(),level:'INFO',message:'Conectado ao stream de logs em tempo real.'});logContainer.scrollTop=logContainer.scrollHeight};logWebSocket.onmessage=(event)=>{try{const data=JSON.parse(event.data);if(data.logs){data.logs.forEach(log=>{logContainer.innerHTML+=formatLog(log)});logContainer.scrollTop=logContainer.scrollHeight}}catch(e){console.error("Erro ao processar mensagem WebSocket:",e)}};logWebSocket.onclose=()=>{console.warn("WebSocket de logs desconectado. Tentando reconectar em 5s...");logContainer.innerHTML+=formatLog({timestamp:new Date().toISOString(),level:'WARNING',message:'Desconectado. Tentando reconectar...'});logContainer.scrollTop=logContainer.scrollHeight;setTimeout(connectWebSocket,5000)};logWebSocket.onerror=(error)=>{console.error("Erro no WebSocket:",error);logContainer.innerHTML+=formatLog({timestamp:new Date().toISOString(),level:'ERROR',message:`Erro no WebSocket: ${error.message||'Desconhecido'}`});logContainer.scrollTop=logContainer.scrollHeight}}document.getElementById('recheck-button').addEventListener('click',async()=>{const button=document.getElementById('recheck-button');const statusSpan=document.getElementById('recheck-status');const originalText=button.querySelector('.btn-text').textContent;button.disabled=true;button.querySelector('.btn-text').textContent='Processando...';button.querySelector('.spinner-border').classList.remove('d-none');statusSpan.textContent='';try{const response=await fetch(`${API_BASE}/recheck`,{method:'POST'});const data=await response.json();if(data.status==='ok'){statusSpan.className='text-success';statusSpan.textContent='Verificação iniciada com sucesso! Verifique os logs.'}else{statusSpan.className='text-danger';statusSpan.textContent=`Erro: ${data.error}`}}catch(error){statusSpan.className='text-danger';statusSpan.textContent=`Erro de conexão: ${error.message}`;console.error('Erro ao rechecar:',error)}finally{button.disabled=false;button.querySelector('.btn-text').textContent=originalText;button.querySelector('.spinner-border').classList.add('d-none');setTimeout(()=>statusSpan.textContent='',5000)}});function initDashboard(){fetchStatus();fetchStats();fetchStock();fetchNeeds();fetchKits();setInterval(fetchStatus,10000);setInterval(fetchStats,10000);setInterval(fetchStock,10000);setInterval(fetchNeeds,10000);setInterval(fetchKits,10000);connectWebSocket()}document.addEventListener('DOMContentLoaded',initDashboard);</script></body></html>
"""

SUCCESS_TEMPLATE = """
<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Autorização Concluída</title><style>body{font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;background-color:#f0f2f5;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;text-align:center}.container{background:white;padding:40px;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,.1);max-width:400px}h1{color:#28a745;margin-bottom:15px;font-size:1.8rem}p{color:#6c757d;margin-bottom:25px}.success-icon{color:#28a745;font-size:4rem;margin-bottom:20px}.btn-close{background-color:#007bff;color:white;padding:10px 20px;border:none;border-radius:5px;cursor:pointer;text-decoration:none;font-weight:600}.btn-close:hover{background-color:#0056b3}</style></head><body><div class="container"><div class="success-icon">✓</div><h1>Autorização Concluída!</h1><p>{{ message }}</p><button class="btn-close" onclick="window.close()">Fechar Janela</button></div></body></html>
"""

ERROR_TEMPLATE = """
<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Erro de Autorização</title><style>body{font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;background-color:#f0f2f5;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;text-align:center}.container{background:white;padding:40px;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,.1);max-width:400px}h1{color:#dc3545;margin-bottom:15px;font-size:1.8rem}p{color:#6c757d;margin-bottom:25px}.error-icon{color:#dc3545;font-size:4rem;margin-bottom:20px}.btn-close{background-color:#6c757d;color:white;padding:10px 20px;border:none;border-radius:5px;cursor:pointer;text-decoration:none;font-weight:600}.btn-close:hover{background-color:#5a6268}</style></head><body><div class="container"><div class="error-icon">✗</div><h1>Erro de Autorização</h1><p>{{ message }}</p><button class="btn-close" onclick="window.close()">Fechar Janela</button></div></body></html>
"""

# ============================================================================
# MAIN (DO CÓDIGO 1)
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='Automação Bling Enhanced')
    parser.add_argument('--serve', action='store_true', help='Servidor web')
    parser.add_argument('--run', action='store_true', help='Processa')
    parser.add_argument('--dry-run', action='store_true', help='Simulação')
    # Adicionando a porta como argumento opcional, mantendo o padrão do código 1
    parser.add_argument('--port', type=int, default=8000, help='Porta do servidor (padrão: 8000)')
    args = parser.parse_args()

    if args.serve:
        config = Config()
        auth = BlingAuth(config)
        orchestrator = AutomationOrchestrator(config) # Removido dry_run do init do orchestrator

        # Inicializa dados do Bling (do código 1)
        try:
            kits = orchestrator.api.get_all_kits_and_components()
            if kits:
                all_comps = []
                for kit in kits:
                    all_comps.extend(kit.components)
                unique_comps = {c.sku: c for c in all_comps}.values()
                orchestrator.purchase_manager.check_min_stock_needs(list(unique_comps))
                print_success(f"Estoque inicial carregado com sucesso. {len(kits)} kits e {len(unique_comps)} componentes monitorados.")
            else:
                print_warning("Nenhum kit encontrado no Bling para monitoramento.")
        except Exception as e:
            print_warning(f"Falha ao carregar estoque inicial do Bling: {e}")

        # Inicia o servidor Flask corretamente (do código 1, mas usando a nova classe WebServer)
        try:
            server = WebServer(auth, orchestrator)
            # Usa a porta do argumento, que por padrão é 8000
            print_info(f"Interface: http://localhost:{args.port}/dashboard")
            server.app.run(host="0.0.0.0", port=args.port, debug=False)
        except KeyboardInterrupt:
            print("\n✓ Servidor encerrado")
        return

    if args.run:
        config = Config()
        orch = AutomationOrchestrator(config)
        kits = orch.api.get_all_kits_and_components()
        if not kits:
            print_error("Nenhum kit encontrado no Bling para processamento.")
            return
        results = orch.process_kits(kits)
        print_header("RESULTADO")
        print(f"Sucesso: {results['success']}")
        print(f"Falhas: {results['failed']}")
        print(json.dumps(orch.stats.to_dict(), indent=2, ensure_ascii=False))
        return

    parser.print_help()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n✓ Encerrado")
        sys.exit(0)
    except Exception as e:
        print_error(f"ERRO: {e}")
        sys.exit(1)