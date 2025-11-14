#!/usr/bin/env python3
"""
bling_enhanced.py - Sistema completo de automação Bling com:
- Autenticação automática persistente
- Logs em tempo real via WebSocket
- Interface web sem erros
- Configuração automática de componentes
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
from threading import Lock

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

load_dotenv(dotenv_path='bling_config.env')

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
# CONFIGURAÇÃO DE LOGS
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
# CONFIGURAÇÃO
# ============================================================================

class Config:
    """Configurações globais"""
    CLIENT_ID = os.getenv('BLING_CLIENT_ID', '')
    CLIENT_SECRET = os.getenv('BLING_CLIENT_SECRET', '')
    REDIRECT_URI = os.getenv('BLING_REDIRECT_URI', 'http://localhost:8000/callback')

    CHECK_MIN_STOCK = os.getenv('BLING_CHECK_MIN_STOCK', 'true').lower() == 'true'
    MIN_STOCK_THRESHOLD = int(os.getenv('BLING_MIN_STOCK', '10'))

    REQUEST_TIMEOUT = int(os.getenv('BLING_TIMEOUT', '30'))
    MAX_RETRIES = int(os.getenv('BLING_MAX_RETRIES', '5'))
    BASE_DELAY = float(os.getenv('BLING_BASE_DELAY', '1.0'))
    DEFAULT_BATCH_SIZE = int(os.getenv('BLING_BATCH_SIZE', '10'))
    DELAY_BETWEEN_BATCHES = float(os.getenv('BLING_BATCH_DELAY', '2.0'))

# ============================================================================
# EXCEÇÕES
# ============================================================================

class BlingAuthError(Exception):
    pass

class BlingAPIError(Exception):
    pass

# ============================================================================
# FUNÇÕES DE PRINT
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
# DATACLASSES
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
# AUTENTICAÇÃO BLING
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
        
        # Carrega tokens ao inicializar
        self._initialize_tokens()

    def _initialize_tokens(self):
        """Inicializa tokens de forma inteligente"""
        # 1. Tenta carregar do arquivo local
        if self.load_tokens():
            logger.info("✓ Tokens carregados do arquivo local")
            return
        
        # 2. Tenta carregar do ambiente (produção)
        env_refresh = os.getenv('BLING_REFRESH_TOKEN')
        if env_refresh:
            logger.info("✓ Refresh token encontrado no ambiente")
            self.refresh_token = env_refresh
            try:
                if self.refresh_access_token():
                    logger.info("✓ Token renovado automaticamente com sucesso")
                    return
            except Exception as e:
                logger.error(f"✗ Falha ao renovar token do ambiente: {e}")
        
        logger.warning("⚠ Nenhum token válido encontrado. Autenticação necessária.")

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
            response = requests.post(self.token_url, data=payload, headers=headers, timeout=Config.REQUEST_TIMEOUT)
            if response.status_code not in (200, 201):
                error_logger.error(f"Token exchange failed: {response.status_code} - {response.text}")
                response.raise_for_status()
            data = response.json()
            self._save_tokens(data)
            logger.info("✓ Tokens obtidos e salvos com sucesso!")
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
            'expires_at': self.expires_at,
            'saved_at': datetime.now().isoformat()
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
                return False
            
            with open(token_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            self.access_token = data.get('access_token')
            self.refresh_token = data.get('refresh_token')
            self.expires_at = data.get('expires_at')
            
            # Verifica se o token está válido
            if self.expires_at:
                expires = datetime.fromisoformat(self.expires_at)
                if datetime.now() >= expires - timedelta(minutes=5):
                    logger.info("Token expirado, renovando automaticamente...")
                    return self.refresh_access_token()
            
            return True
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
                return False
            
            data = response.json()
            self._save_tokens(data)
            logger.info("✓ Token renovado automaticamente!")
            return True
        except Exception as e:
            error_logger.error(f"Falha ao renovar token: {e}")
            return False

    def ensure_valid_token(self) -> bool:
        """Garante que existe um token válido"""
        if not self.access_token:
            if not self.load_tokens():
                raise BlingAuthError("Token não encontrado. Autenticação necessária.")
        
        if self.expires_at:
            expires = datetime.fromisoformat(self.expires_at)
            if datetime.now() >= expires - timedelta(minutes=5):
                if not self.refresh_access_token():
                    raise BlingAuthError("Token expirado e falha ao renovar")
        
        return True

    def get_token_info(self) -> Dict:
        """Retorna informações sobre o token atual"""
        if not self.expires_at:
            return {'valid': False, 'message': 'Token não inicializado'}
        
        expires = datetime.fromisoformat(self.expires_at)
        now = datetime.now()
        
        return {
            'valid': now < expires,
            'expires_at': self.expires_at,
            'expires_in_minutes': int((expires - now).total_seconds() / 60),
            'has_refresh_token': bool(self.refresh_token)
        }

# ============================================================================
# API BLING
# ============================================================================

class BlingAPI:
    BASE_URL = 'https://www.bling.com.br/Api/v3'

    def __init__(self, auth: BlingAuth, component_config: Dict = None):
        self.auth = auth
        self.session = requests.Session()
        self.component_config = component_config or {}

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
                    logger.warning(f"Rate limit atingido, aguardando...")
                    time.sleep(Config.BASE_DELAY * (2 ** attempt))
                    continue
                
                if response.status_code >= 500:
                    logger.warning(f"Erro do servidor ({response.status_code}), tentando novamente...")
                    time.sleep(Config.BASE_DELAY * (2 ** attempt))
                    continue
                
                response.raise_for_status()
                return response
            except BlingAuthError:
                raise
            except requests.exceptions.RequestException as e:
                error_logger.error(f"Request failed (attempt {attempt + 1}/{Config.MAX_RETRIES}): {e}")
                if attempt == Config.MAX_RETRIES - 1:
                    raise BlingAPIError(f"API request failed after {Config.MAX_RETRIES} retries: {e}")
                time.sleep(Config.BASE_DELAY * (2 ** attempt))
        return None

    def get_all_products(self, tipo: str = 'P', situacao: str = 'A') -> List[Dict]:
        all_products = []
        page = 1
        while True:
            url = f"{self.BASE_URL}/produtos?pagina={page}&limite=100&tipo={tipo}&situacao={situacao}"
            response = self._request_with_retry('GET', url)
            if not response:
                break
            data = response.json()
            if not data.get('data'):
                break
            all_products.extend(data['data'])
            logger.info(f"Carregados {len(data['data'])} produtos (página {page})")
            page += 1
        return all_products

    def get_product_by_sku(self, sku: str) -> Optional[Dict]:
        url = f"{self.BASE_URL}/produtos?codigo={sku}"
        response = self._request_with_retry('GET', url)
        if response:
            data = response.json()
            if data.get('data'):
                return data['data'][0]
        return None

    def get_product_stock(self, product_id: int) -> Optional[Dict]:
        try:
            url = f"{self.BASE_URL}/produtos/estoques?produtoId={product_id}"
            response = self._request_with_retry('GET', url)
            if response:
                data = response.json()
                if data.get('data') and len(data['data']) > 0:
                    return data['data'][0]
            return {'saldo': 0}  # Retorna estoque zero se não encontrar
        except Exception as e:
            logger.error(f"Erro ao buscar estoque do produto {product_id}: {e}")
            return {'saldo': 0}

    def get_all_kits_and_components(self) -> List[Kit]:
        kits = []
        try:
            products = self.get_all_products()
            for prod in products:
                if prod.get('formato') == 'E':  # Estrutura
                    kit_sku = prod['codigo']
                    kit_name = prod['nome']
                    components = []
                    
                    if prod.get('estrutura') and prod['estrutura'].get('componentes'):
                        for comp_data in prod['estrutura']['componentes']:
                            comp_sku = comp_data['produto']['codigo']
                            comp_name = comp_data['produto']['nome']
                            comp_qty = comp_data['quantidade']
                            
                            config = self.component_config.get(comp_sku, {})
                            defaults = self.component_config.get('component_defaults', {})
                            
                            supplier = config.get('supplier', defaults.get('supplier', "FORNECEDOR_PADRAO"))
                            lead_time_days = config.get('lead_time_days', defaults.get('lead_time_days', 15))
                            min_stock = config.get('min_stock', defaults.get('min_stock', Config.MIN_STOCK_THRESHOLD))
                            
                            component = Component(
                                sku=comp_sku,
                                name=comp_name,
                                qty=int(comp_qty),
                                supplier=supplier,
                                lead_time_days=lead_time_days,
                                min_stock=min_stock
                            )
                            components.append(component)
                        
                        if components:
                            kit = Kit(sku=kit_sku, name=kit_name, components=components)
                            kits.append(kit)
            
            logger.info(f"✓ Carregados {len(kits)} kits com estrutura")
        except Exception as e:
            logger.error(f"Erro ao carregar kits: {e}")
        
        return kits

    def create_production_order(self, kit_sku: str, quantity: int) -> Optional[int]:
        product = self.get_product_by_sku(kit_sku)
        if not product:
            raise BlingAPIError(f"Kit {kit_sku} não encontrado.")
        
        payload = {
            "produto": {"id": product['id']},
            "quantidade": quantity
        }
        url = f"{self.BASE_URL}/producoes"
        response = self._request_with_retry('POST', url, json=payload)
        if response:
            data = response.json()
            return data['data']['id']
        return None

    def create_purchase_order(self, supplier_name: str, items: List[Dict]) -> Optional[int]:
        url_contato = f"{self.BASE_URL}/contatos?pesquisa={supplier_name}"
        resp_contato = self._request_with_retry('GET', url_contato)
        if not resp_contato or not resp_contato.json().get('data'):
            raise BlingAPIError(f"Fornecedor '{supplier_name}' não encontrado.")
        supplier_id = resp_contato.json()['data'][0]['id']

        payload = {
            "contato": {"id": supplier_id},
            "itens": items
        }
        url = f"{self.BASE_URL}/pedidos/compras"
        response = self._request_with_retry('POST', url, json=payload)
        if response:
            data = response.json()
            return data['data']['id']
        return None

# ============================================================================
# GERENCIADOR DE NECESSIDADES DE COMPRA
# ============================================================================

class PurchaseNeedsManager:
    def __init__(self, api: BlingAPI):
        self.api = api
        self.needs: Dict[str, PurchaseNeed] = {}

    def check_min_stock_needs(self, components: List[Component]):
        for comp in components:
            try:
                product = self.api.get_product_by_sku(comp.sku)
                if product:
                    stock_data = self.api.get_product_stock(product['id'])
                    if stock_data:
                        comp.current_stock = stock_data.get('saldo', 0)
                        if comp.current_stock < comp.min_stock:
                            self.add_need(comp, comp.min_stock - comp.current_stock, "Estoque Mínimo")
            except Exception as e:
                logger.error(f"Erro ao verificar estoque de {comp.sku}: {e}")

    def add_need(self, component: Component, quantity: int, reason: str):
        if component.sku not in self.needs:
            self.needs[component.sku] = PurchaseNeed(
                component_sku=component.sku,
                component_name=component.name,
                quantity_needed=quantity,
                supplier=component.supplier,
                lead_time_days=component.lead_time_days,
                reason=reason
            )
        else:
            self.needs[component.sku].quantity_needed += quantity

    def generate_purchase_orders(self) -> List[int]:
        if not self.needs:
            return []

        pos_by_supplier = defaultdict(list)
        for need in self.needs.values():
            try:
                product = self.api.get_product_by_sku(need.component_sku)
                if product:
                    pos_by_supplier[need.supplier].append({
                        "produto": {"id": product['id']},
                        "quantidade": need.quantity_needed
                    })
            except Exception as e:
                logger.error(f"Erro ao preparar PO para {need.component_sku}: {e}")

        created_po_ids = []
        for supplier, items in pos_by_supplier.items():
            try:
                po_id = self.api.create_purchase_order(supplier, items)
                if po_id:
                    created_po_ids.append(po_id)
                    logger.info(f"✓ PO {po_id} criada para {supplier} com {len(items)} itens.")
            except BlingAPIError as e:
                error_logger.error(f"Erro ao criar PO para {supplier}: {e}")
        
        self.needs.clear()
        return created_po_ids

# ============================================================================
# GERENCIADOR DE ESTATÍSTICAS
# ============================================================================

class StatisticsManager:
    def __init__(self):
        self.reset()

    def reset(self):
        self.start_time = None
        self.end_time = None
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

# ============================================================================
# ORQUESTRADOR DE AUTOMAÇÃO
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
        
        # Cria arquivo padrão se não existir
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
        self.stats.reset()
        self.stats.start()
        
        for i in range(0, len(kits), batch_size):
            batch = kits[i:i+batch_size]
            for kit in batch:
                try:
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
        """Executa verificação de estoque e gera POs"""
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

# ============================================================================
# SERVIDOR WEB
# ============================================================================

class WebServer:
    def __init__(self, auth: BlingAuth, orchestrator: AutomationOrchestrator):
        self.app = Flask(__name__)
        
        # Só inicializa Sock se disponível
        if WEBSOCKET_AVAILABLE:
            self.sock = Sock(self.app)
            logger.info("✓ WebSocket disponível")
        else:
            self.sock = None
            logger.warning("⚠ WebSocket não disponível, usando polling")
        
        self.auth = auth
        self.orchestrator = orchestrator
        self._setup_routes()

    def _setup_routes(self):
        @self.app.route("/")
        def index():
            return redirect(url_for("dashboard"))

        @self.app.route("/dashboard")
        def dashboard():
            return render_template_string(DASHBOARD_TEMPLATE)

        @self.app.route("/callback")
        def callback():
            code = request.args.get("code")
            if code and self.auth.exchange_code_for_token(code):
                return render_template_string(SUCCESS_TEMPLATE)
            else:
                return "Erro na autorização. Verifique os logs.", 400

        @self.app.route("/api/status")
        def api_status():
            try:
                token_info = self.auth.get_token_info()
                return jsonify({
                    "token_valid": token_info['valid'],
                    "token_info": token_info
                })
            except Exception as e:
                return jsonify({"token_valid": False, "error": str(e)})

        @self.app.route("/api/stats")
        def api_stats():
            try:
                return jsonify(self.orchestrator.stats.to_dict())
            except Exception as e:
                logger.error(f"Erro ao obter estatísticas: {e}")
                return jsonify({"error": str(e)}), 500

        @self.app.route("/api/stock")
        def api_stock():
            try:
                all_components = []
                kits = self.orchestrator.api.get_all_kits_and_components()
                
                for kit in kits:
                    all_components.extend(kit.components)
                
                unique_comps = {c.sku: c for c in all_components}.values()
                
                items = []
                for comp in unique_comps:
                    try:
                        product = self.orchestrator.api.get_product_by_sku(comp.sku)
                        if product:
                            stock_data = self.orchestrator.api.get_product_stock(product['id'])
                            current_stock = stock_data.get('saldo', 0) if stock_data else 0
                            items.append({
                                "sku": comp.sku,
                                "nome": comp.name,
                                "estoque": current_stock,
                                "minimo": comp.min_stock,
                                "alerta": current_stock < comp.min_stock
                            })
                    except Exception as e:
                        logger.error(f"Erro ao processar componente {comp.sku}: {e}")
                        items.append({
                            "sku": comp.sku,
                            "nome": comp.name,
                            "estoque": 0,
                            "minimo": comp.min_stock,
                            "alerta": True,
                            "erro": str(e)
                        })
                
                return jsonify({"items": items})
            except Exception as e:
                logger.error(f"Erro ao obter estoque: {e}")
                return jsonify({"error": str(e), "items": []}), 500

        @self.app.route("/api/needs")
        def api_needs():
            try:
                needs_list = [asdict(n) for n in self.orchestrator.purchase_manager.needs.values()]
                return jsonify({"needs": needs_list})
            except Exception as e:
                logger.error(f"Erro ao obter necessidades: {e}")
                return jsonify({"error": str(e), "needs": []}), 500

        @self.app.route("/api/kits")
        def api_kits():
            try:
                kits = self.orchestrator.api.get_all_kits_and_components()
                kits_data = []
                for kit in kits:
                    kits_data.append({
                        "sku": kit.sku,
                        "nome": kit.name,
                        "componentes": [{"nome": c.name, "quantidade": c.qty, "sku": c.sku} for c in kit.components]
                    })
                return jsonify({"kits": kits_data})
            except Exception as e:
                logger.error(f"Erro ao obter kits: {e}")
                return jsonify({"error": str(e), "kits": []}), 500

        @self.app.route("/api/logs")
        def api_logs():
            try:
                logs = memory_handler.get_logs(limit=100)
                return jsonify({"logs": logs})
            except Exception as e:
                logger.error(f"Erro ao obter logs: {e}")
                return jsonify({"error": str(e), "logs": []}), 500

        @self.app.route("/api/recheck", methods=['POST'])
        def api_recheck():
            try:
                logger.info("🔄 Verificação manual iniciada via API")
                self.orchestrator.run_purchase_check()
                return jsonify({"status": "ok", "message": "Verificação iniciada com sucesso"})
            except Exception as e:
                logger.error(f"Erro na verificação manual: {e}")
                return jsonify({"status": "error", "error": str(e)}), 500

        @self.app.route('/webhook/bling', methods=['POST'])
        def webhook_bling():
            try:
                data = request.get_json(force=True)
                event_type = data.get('event') or data.get('tipo') or 'unknown'
                logger.info(f"🪝 Webhook recebido: {event_type}")
                
                # Tratamento de eventos de pedido
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
                        self.orchestrator.run_purchase_check()
                        return jsonify({'status': 'ok', 'message': f'Pedido {pedido_id} processado'}), 200
                    else:
                        logger.warning(f"⚠ Webhook de Pedido recebido, mas ID não encontrado")
                        return jsonify({'status': 'warning', 'message': 'ID do pedido não encontrado'}), 200
                
                # Evento de estoque
                if event_type == 'estoque.atualizado' or data.get('tipo') == 'estoque':
                    logger.info(f"📦 Evento estoque.atualizado recebido")
                
                return jsonify({'status': 'ok', 'message': f'Webhook {event_type} recebido'}), 200
            except Exception as e:
                error_logger.error(f"Erro no webhook: {e}")
                return jsonify({'error': str(e)}), 500

        # WebSocket para logs em tempo real (apenas se disponível)
        if WEBSOCKET_AVAILABLE and self.sock:
            @self.sock.route('/ws/logs')
            def ws_logs(ws):
                logger.info("🔌 Cliente conectado ao WebSocket de logs")
                last_log_count = 0
                
                try:
                    while True:
                        logs = memory_handler.get_logs()
                        current_count = len(logs)
                        
                        # Envia apenas novos logs
                        if current_count > last_log_count:
                            new_logs = logs[last_log_count:]
                            ws.send(json.dumps({"logs": new_logs}))
                            last_log_count = current_count
                        
                        time.sleep(1)  # Verifica a cada segundo
                except Exception as e:
                    logger.info(f"🔌 Cliente desconectado do WebSocket: {e}")

    def run(self, host='0.0.0.0', port=8000):
        print_header("SERVIDOR WEB BLING")
        print_info(f"Interface: http://{host}:{port}/dashboard")
        print_info(f"OAuth: {self.auth.get_authorization_url()}")
        print_info(f"Webhook: http://{host}:{port}/webhook/bling\n")
        self.app.run(host=host, port=port, debug=False)

# ============================================================================
# TEMPLATES HTML
# ============================================================================

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
    .navbar { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
    .navbar-brand { font-weight: 700; font-size: 1.5rem; }
    .status-badge { padding: 0.5rem 1rem; border-radius: 20px; font-size: 0.9rem; font-weight: 600; }
    .card { border-radius: 1rem; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.07); border: none; margin-bottom: 1.5rem; }
    .card-title { font-weight: 600; color: #343a40; margin-bottom: 1rem; }
    .kpi-value { font-size: 2.5rem; font-weight: 700; margin-bottom: 0.25rem; }
    .kpi-label { font-size: 0.9rem; color: #6c757d; text-transform: uppercase; letter-spacing: 0.5px; }
    .log-box { 
      font-family: 'Courier New', monospace; 
      font-size: 0.85em; 
      background: #1e1e1e; 
      color: #d4d4d4;
      border-radius: 0.5rem; 
      padding: 1rem;
      max-height: 400px;
      overflow-y: auto;
    }
    .log-entry { 
      padding: 0.25rem 0; 
      border-bottom: 1px solid #333;
    }
    .log-entry:last-child { border-bottom: none; }
    .log-level-INFO { color: #4ec9b0; }
    .log-level-WARNING { color: #dcdcaa; }
    .log-level-ERROR { color: #f48771; }
    .log-level-DEBUG { color: #9cdcfe; }
    .nav-tabs .nav-link { color: #6c757d; font-weight: 500; }
    .nav-tabs .nav-link.active { 
      background-color: #ffffff; 
      border-color: #dee2e6 #dee2e6 #ffffff;
      color: #667eea;
      font-weight: 600;
    }
    .table-danger td { background-color: #f8d7da !important; }
    .table-warning td { background-color: #fff3cd !important; }
    .btn-primary { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border: none; }
    .btn-primary:hover { transform: translateY(-2px); box-shadow: 0 4px 8px rgba(102, 126, 234, 0.4); }
    .spinner-border-sm { width: 1rem; height: 1rem; border-width: 0.15em; }
  </style>
</head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark">
  <div class="container-fluid">
    <a class="navbar-brand" href="#">🚀 Bling Automação ERP</a>
    <div class="d-flex align-items-center">
      <span class="status-badge" id="status-badge">Verificando...</span>
    </div>
  </div>
</nav>

<div class="container my-4">
  <ul class="nav nav-tabs" id="mainTabs" role="tablist">
    <li class="nav-item" role="presentation">
      <a class="nav-link active" id="dashboard-tab" data-bs-toggle="tab" href="#tabDashboard" role="tab">Dashboard</a>
    </li>
    <li class="nav-item" role="presentation">
      <a class="nav-link" id="stock-tab" data-bs-toggle="tab" href="#tabStock" role="tab">Estoque</a>
    </li>
    <li class="nav-item" role="presentation">
      <a class="nav-link" id="needs-tab" data-bs-toggle="tab" href="#tabNeeds" role="tab">Necessidades de Compra</a>
    </li>
    <li class="nav-item" role="presentation">
      <a class="nav-link" id="kits-tab" data-bs-toggle="tab" href="#tabKits" role="tab">Kits</a>
    </li>
  </ul>
  
  <div class="tab-content p-4 bg-white border border-top-0" style="border-radius: 0 0 1rem 1rem;">
    
    <!-- DASHBOARD TAB -->
    <div class="tab-pane fade show active" id="tabDashboard" role="tabpanel">
      <h4 class="mb-4">📊 Visão Geral da Automação</h4>
      
      <div class="row mb-4" id="stats-kpis">
        <div class="col-md-3 mb-3">
          <div class="card bg-light h-100">
            <div class="card-body text-center">
              <div class="spinner-border text-primary" role="status"></div>
              <p class="mt-2 mb-0">Carregando...</p>
            </div>
          </div>
        </div>
      </div>

      <div class="row mb-4">
        <div class="col-md-6">
          <div class="card h-100">
            <div class="card-body">
              <h5 class="card-title">📈 Status de Processamento</h5>
              <canvas id="processingChart"></canvas>
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
                <span class="btn-text">🔄 Re-checar Estoque e Gerar POs</span>
                <span class="spinner-border spinner-border-sm d-none" role="status"></span>
              </button>
              <span id="recheck-status" class="ms-3"></span>
            </div>
          </div>
        </div>
      </div>
    </div>
    
    <!-- ESTOQUE TAB -->
    <div class="tab-pane fade" id="tabStock" role="tabpanel">
      <h4 class="mb-3">📦 Situação do Estoque de Componentes</h4>
      <div class="alert alert-info d-none" id="stock-alert"></div>
      <div class="table-responsive">
        <table class="table table-striped table-hover">
          <thead class="table-light">
            <tr>
              <th>SKU</th>
              <th>Nome</th>
              <th>Estoque Atual</th>
              <th>Estoque Mínimo</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody id="stock-table-body">
            <tr><td colspan="5" class="text-center"><div class="spinner-border text-primary" role="status"></div></td></tr>
          </tbody>
        </table>
      </div>
    </div>
    
    <!-- NECESSIDADES TAB -->
    <div class="tab-pane fade" id="tabNeeds" role="tabpanel">
      <h4 class="mb-3">🛒 Necessidades de Compra (POs)</h4>
      <div class="table-responsive">
        <table class="table table-striped table-hover">
          <thead class="table-light">
            <tr>
              <th>SKU</th>
              <th>Nome</th>
              <th>Quantidade Necessária</th>
              <th>Fornecedor</th>
              <th>Motivo</th>
            </tr>
          </thead>
          <tbody id="needs-table-body">
            <tr><td colspan="5" class="text-center"><div class="spinner-border text-primary" role="status"></div></td></tr>
          </tbody>
        </table>
      </div>
    </div>
    
    <!-- KITS TAB -->
    <div class="tab-pane fade" id="tabKits" role="tabpanel">
      <h4 class="mb-3">📦 Kits e Componentes</h4>
      <div class="table-responsive">
        <table class="table table-striped table-hover">
          <thead class="table-light">
            <tr>
              <th>SKU Kit</th>
              <th>Nome Kit</th>
              <th>Componentes</th>
            </tr>
          </thead>
          <tbody id="kits-table-body">
            <tr><td colspan="3" class="text-center"><div class="spinner-border text-primary" role="status"></div></td></tr>
          </tbody>
        </table>
      </div>
    </div>
    
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
document.addEventListener('DOMContentLoaded', function() {
  const API_BASE = '/api';
  let processingChart;
  let wsConnection;

  // ========== Sistema de Logs (Polling ou WebSocket) ==========
  let lastLogIndex = 0;
  let logsPollingInterval;
  
  function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/logs`;
    
    try {
      const ws = new WebSocket(wsUrl);
      
      ws.onopen = function() {
        console.log('✓ WebSocket conectado');
        // Para polling se WebSocket conectar
        if (logsPollingInterval) {
          clearInterval(logsPollingInterval);
        }
      };
      
      ws.onmessage = function(event) {
        const data = JSON.parse(event.data);
        if (data.logs) {
          appendLogs(data.logs);
        }
      };
      
      ws.onerror = function(error) {
        console.log('WebSocket indisponível, usando polling');
        startLogsPolling();
      };
      
      ws.onclose = function() {
        console.log('WebSocket desconectado, usando polling');
        startLogsPolling();
      };
    } catch (e) {
      console.log('WebSocket não suportado, usando polling');
      startLogsPolling();
    }
  }
  
  function startLogsPolling() {
    if (logsPollingInterval) return; // Já está rodando
    
    console.log('✓ Iniciando polling de logs (a cada 3s)');
    
    // Primeira carga
    loadLogsIncremental();
    
    // Polling a cada 3 segundos
    logsPollingInterval = setInterval(() => {
      loadLogsIncremental();
    }, 3000);
  }
  
  function loadLogsIncremental() {
    fetch(`${API_BASE}/logs`)
      .then(response => response.json())
      .then(data => {
        if (data.logs && data.logs.length > lastLogIndex) {
          const newLogs = data.logs.slice(lastLogIndex);
          appendLogs(newLogs);
          lastLogIndex = data.logs.length;
        }
      })
      .catch(error => console.error('Erro ao carregar logs:', error));
  }

  function appendLogs(logs) {
    const logsElement = document.getElementById('logs-content');
    logs.forEach(log => {
      const logEntry = document.createElement('div');
      logEntry.className = `log-entry log-level-${log.level}`;
      const timestamp = new Date(log.timestamp).toLocaleTimeString('pt-BR');
      logEntry.textContent = `[${timestamp}] [${log.level}] ${log.message}`;
      logsElement.appendChild(logEntry);
    });
    logsElement.scrollTop = logsElement.scrollHeight;
    
    // Limita o número de logs exibidos
    while (logsElement.children.length > 100) {
      logsElement.removeChild(logsElement.firstChild);
    }
  }

  // ========== Funções de carregamento ==========
  function loadStatus() {
    fetch(`${API_BASE}/status`)
      .then(response => response.json())
      .then(data => {
        const badge = document.getElementById('status-badge');
        if (data.token_valid) {
          badge.className = 'status-badge bg-success';
          badge.innerHTML = '✓ Conectado ao Bling';
        } else {
          badge.className = 'status-badge bg-danger';
          badge.innerHTML = '✗ Desconectado';
        }
      })
      .catch(error => {
        console.error('Erro ao carregar status:', error);
        const badge = document.getElementById('status-badge');
        badge.className = 'status-badge bg-warning';
        badge.innerHTML = '⚠ Erro de Conexão';
      });
  }

  function loadStats() {
    fetch(`${API_BASE}/stats`)
      .then(response => response.json())
      .then(data => {
        const totalProcessed = data.success + data.failed;
        const successRate = totalProcessed > 0 ? (data.success / totalProcessed * 100).toFixed(1) : 0;

        const kpis = [
          { label: 'Taxa de Sucesso', value: `${successRate}%`, color: 'success', icon: '✅' },
          { label: 'OPs Criadas', value: data.ops_created, color: 'info', icon: '🏭' },
          { label: 'POs Criadas', value: data.pos_created, color: 'warning', icon: '🛒' },
          { label: 'Tempo Total', value: `${data.elapsed_time_seconds}s`, color: 'secondary', icon: '⏱️' }
        ];

        const kpisHtml = kpis.map(kpi => `
          <div class="col-md-3 mb-3">
            <div class="card bg-light h-100">
              <div class="card-body text-center">
                <div class="kpi-value text-${kpi.color}">${kpi.icon} ${kpi.value}</div>
                <div class="kpi-label">${kpi.label}</div>
              </div>
            </div>
          </div>
        `).join('');
        document.getElementById('stats-kpis').innerHTML = kpisHtml;

        // Atualiza gráfico
        updateProcessingChart(data.success, data.failed);
      })
      .catch(error => console.error('Erro ao carregar estatísticas:', error));
  }

  function updateProcessingChart(success, failed) {
    const ctx = document.getElementById('processingChart');
    if (!ctx) return;
    
    if (processingChart) {
      processingChart.destroy();
    }
    
    processingChart = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['Sucesso', 'Falha'],
        datasets: [{
          data: [success, failed],
          backgroundColor: ['#28a745', '#dc3545'],
          hoverOffset: 4
        }]
      },
      options: {
        responsive: true,
        plugins: {
          legend: { position: 'top' },
          title: {
            display: true,
            text: `Total: ${success + failed} processamentos`
          }
        }
      }
    });
  }

  function loadLogs() {
    fetch(`${API_BASE}/logs`)
      .then(response => response.json())
      .then(data => {
        const logsElement = document.getElementById('logs-content');
        logsElement.innerHTML = '';
        if (data.logs && data.logs.length > 0) {
          appendLogs(data.logs.slice(-50)); // Últimos 50 logs
        } else {
          logsElement.innerHTML = '<div class="text-muted">Nenhum log disponível</div>';
        }
      })
      .catch(error => console.error('Erro ao carregar logs:', error));
  }

  function loadStock() {
    fetch(`${API_BASE}/stock`)
      .then(response => response.json())
      .then(data => {
        const tableBody = document.getElementById('stock-table-body');
        const alertElement = document.getElementById('stock-alert');
        tableBody.innerHTML = '';
        let lowStockCount = 0;

        if (data.error) {
          tableBody.innerHTML = `<tr><td colspan="5" class="text-danger text-center">Erro: ${data.error}</td></tr>`;
          return;
        }

        if (!data.items || data.items.length === 0) {
          tableBody.innerHTML = '<tr><td colspan="5" class="text-center">Nenhum componente encontrado</td></tr>';
          return;
        }

        data.items.forEach(item => {
          const alertClass = item.alerta ? 'table-danger' : '';
          if (item.alerta) lowStockCount++;
          
          const statusBadge = item.alerta 
            ? '<span class="badge bg-danger">⚠ BAIXO</span>' 
            : '<span class="badge bg-success">✓ OK</span>';
          
          const row = `
            <tr class="${alertClass}">
              <td>${item.sku}</td>
              <td>${item.nome}</td>
              <td><strong>${item.estoque}</strong></td>
              <td>${item.minimo}</td>
              <td>${statusBadge}</td>
            </tr>
          `;
          tableBody.innerHTML += row;
        });

        if (lowStockCount > 0) {
          alertElement.className = 'alert alert-danger';
          alertElement.innerHTML = `⚠️ <strong>ALERTA:</strong> ${lowStockCount} componente(s) abaixo do estoque mínimo!`;
          alertElement.classList.remove('d-none');
        } else {
          alertElement.classList.add('d-none');
        }
      })
      .catch(error => {
        console.error('Erro ao carregar estoque:', error);
        document.getElementById('stock-table-body').innerHTML = 
          '<tr><td colspan="5" class="text-danger text-center">Erro ao carregar dados</td></tr>';
      });
  }

  function loadNeeds() {
    fetch(`${API_BASE}/needs`)
      .then(response => response.json())
      .then(data => {
        const tableBody = document.getElementById('needs-table-body');
        tableBody.innerHTML = '';

        if (data.error) {
          tableBody.innerHTML = `<tr><td colspan="5" class="text-danger text-center">Erro: ${data.error}</td></tr>`;
          return;
        }
        
        if (!data.needs || data.needs.length === 0) {
          tableBody.innerHTML = '<tr><td colspan="5" class="text-success text-center">✓ Nenhuma necessidade de compra pendente</td></tr>';
          return;
        }

        data.needs.forEach(item => {
          const row = `
            <tr class="table-warning">
              <td>${item.component_sku}</td>
              <td>${item.component_name}</td>
              <td><strong>${item.quantity_needed}</strong></td>
              <td>${item.supplier}</td>
              <td>${item.reason}</td>
            </tr>
          `;
          tableBody.innerHTML += row;
        });
      })
      .catch(error => {
        console.error('Erro ao carregar necessidades:', error);
        document.getElementById('needs-table-body').innerHTML = 
          '<tr><td colspan="5" class="text-danger text-center">Erro ao carregar dados</td></tr>';
      });
  }

  function loadKits() {
    fetch(`${API_BASE}/kits`)
      .then(response => response.json())
      .then(data => {
        const tableBody = document.getElementById('kits-table-body');
        tableBody.innerHTML = '';

        if (data.error) {
          tableBody.innerHTML = `<tr><td colspan="3" class="text-danger text-center">Erro: ${data.error}</td></tr>`;
          return;
        }
        
        if (!data.kits || data.kits.length === 0) {
          tableBody.innerHTML = '<tr><td colspan="3" class="text-warning text-center">Nenhum kit encontrado</td></tr>';
          return;
        }

        data.kits.forEach(kit => {
          const componentsHtml = kit.componentes.map(comp => 
            `<span class="badge bg-secondary me-1">${comp.nome} (${comp.quantidade}x)</span>`
          ).join('');

          const row = `
            <tr>
              <td><strong>${kit.sku}</strong></td>
              <td>${kit.nome}</td>
              <td>${componentsHtml}</td>
            </tr>
          `;
          tableBody.innerHTML += row;
        });
      })
      .catch(error => {
        console.error('Erro ao carregar kits:', error);
        document.getElementById('kits-table-body').innerHTML = 
          '<tr><td colspan="3" class="text-danger text-center">Erro ao carregar dados</td></tr>';
      });
  }
  
  function recheckStock() {
    const button = document.getElementById('recheck-button');
    const btnText = button.querySelector('.btn-text');
    const spinner = button.querySelector('.spinner-border');
    const statusSpan = document.getElementById('recheck-status');
    
    button.disabled = true;
    btnText.classList.add('d-none');
    spinner.classList.remove('d-none');
    statusSpan.innerHTML = '<span class="text-info">⏳ Processando...</span>';

    fetch(`${API_BASE}/recheck`, { method: 'POST' })
      .then(response => response.json())
      .then(data => {
        if (data.status === 'ok') {
          statusSpan.innerHTML = '<span class="text-success">✅ Verificação iniciada com sucesso!</span>';
          setTimeout(() => {
            loadStock();
            loadNeeds();
            loadStats();
            statusSpan.innerHTML = '';
            button.disabled = false;
            btnText.classList.remove('d-none');
            spinner.classList.add('d-none');
          }, 3000);
        } else {
          statusSpan.innerHTML = `<span class="text-danger">❌ Erro: ${data.error || data.message}</span>`;
          button.disabled = false;
          btnText.classList.remove('d-none');
          spinner.classList.add('d-none');
        }
      })
      .catch(error => {
        statusSpan.innerHTML = `<span class="text-danger">❌ Erro: ${error}</span>`;
        button.disabled = false;
        btnText.classList.remove('d-none');
        spinner.classList.add('d-none');
      });
  }

  // ========== Inicialização ==========
  loadStatus();
  loadStats();
  loadStock();
  loadNeeds();
  loadKits();
  
  // Conecta WebSocket para logs em tempo real
  connectWebSocket();
  
  // Atualização automática a cada 60 segundos
  setInterval(() => {
    loadStatus();
    const activeTab = document.querySelector('.nav-link.active').getAttribute('href');
    if (activeTab === '#tabStock') loadStock();
    if (activeTab === '#tabNeeds') loadNeeds();
    if (activeTab === '#tabDashboard') loadStats();
  }, 60000);
  
  // Event listeners
  document.getElementById('recheck-button').addEventListener('click', recheckStock);
  
  // Carregar dados ao trocar de aba
  document.querySelectorAll('.nav-link').forEach(tab => {
    tab.addEventListener('shown.bs.tab', function(event) {
      const tabId = event.target.getAttribute('href');
      if (tabId === '#tabStock') loadStock();
      if (tabId === '#tabNeeds') loadNeeds();
      if (tabId === '#tabKits') loadKits();
    });
  });
});
</script>
</body>
</html>
"""

SUCCESS_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Autorização Concluída - Bling</title>
  <style>
    body {
      font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
      text-align: center;
      padding: 50px;
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      color: #fff;
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .container {
      background: white;
      color: #333;
      padding: 3rem;
      border-radius: 1rem;
      box-shadow: 0 10px 40px rgba(0,0,0,0.2);
      max-width: 500px;
    }
    .success-icon {
      font-size: 5rem;
      margin-bottom: 1rem;
      animation: bounce 1s ease-in-out;
    }
    @keyframes bounce {
      0%, 100% { transform: translateY(0); }
      50% { transform: translateY(-20px); }
    }
    h1 {
      color: #28a745;
      margin-bottom: 1rem;
    }
    p {
      font-size: 1.1rem;
      color: #666;
    }
    .btn {
      display: inline-block;
      margin-top: 2rem;
      padding: 0.75rem 2rem;
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      color: white;
      text-decoration: none;
      border-radius: 0.5rem;
      font-weight: 600;
      transition: transform 0.2s;
    }
    .btn:hover {
      transform: translateY(-2px);
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="success-icon">✓</div>
    <h1>Autorização Concluída!</h1>
    <p>Tokens salvos com sucesso.</p>
    <p>Você pode fechar esta janela e voltar ao terminal ou acessar o dashboard.</p>
    <a href="/dashboard" class="btn">🚀 Ir para o Dashboard</a>
  </div>
</body>
</html>
"""

# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Automação Bling Enhanced - Sistema completo de automação ERP',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos de uso:
  python bling_enhanced.py --serve          Inicia o servidor web (padrão)
  python bling_enhanced.py --run            Executa processamento de kits
  python bling_enhanced.py --serve --port 5000  Servidor em porta customizada
        """
    )
    parser.add_argument('--serve', action='store_true', help='Inicia servidor web')
    parser.add_argument('--run', action='store_true', help='Processa kits e componentes')
    parser.add_argument('--port', type=int, default=None, help='Porta do servidor (padrão: 8000 ou PORT env)')

    args = parser.parse_args()

    # Se nenhuma flag, assume modo servidor (produção)
    if not args.serve and not args.run:
        args.serve = True

    config = Config()

    if args.serve:
        print_header("BLING AUTOMAÇÃO - MODO SERVIDOR")
        
        auth = BlingAuth(config)
        orchestrator = AutomationOrchestrator(config)

        # Carrega dados iniciais do Bling
        try:
            print_info("Carregando dados iniciais do Bling...")
            kits = orchestrator.api.get_all_kits_and_components()
            if kits:
                all_comps = [comp for kit in kits for comp in kit.components]
                unique_comps = {c.sku: c for c in all_comps}.values()
                orchestrator.purchase_manager.check_min_stock_needs(list(unique_comps))
                print_success(f"✓ Estoque inicial carregado: {len(kits)} kits, {len(unique_comps)} componentes")
            else:
                print_warning("⚠ Nenhum kit encontrado no Bling")
        except BlingAuthError as e:
            print_warning(f"⚠ Autenticação necessária: {e}")
            print_info(f"Acesse: {auth.get_authorization_url()}")
        except Exception as e:
            print_warning(f"⚠ Falha ao carregar dados iniciais: {e}")

        # Inicia servidor
        try:
            port = args.port or int(os.environ.get("PORT", 8000))
            server = WebServer(auth, orchestrator)
            server.run(host="0.0.0.0", port=port)
        except KeyboardInterrupt:
            print("\n✓ Servidor encerrado pelo usuário")
        except Exception as e:
            print_error(f"Erro ao iniciar servidor: {e}")
            sys.exit(1)
        return

    if args.run:
        print_header("BLING AUTOMAÇÃO - MODO PROCESSAMENTO")
        
        try:
            orch = AutomationOrchestrator(config)
            print_info("Carregando kits do Bling...")
            kits = orch.api.get_all_kits_and_components()
            
            if not kits:
                print_error("✗ Nenhum kit encontrado no Bling")
                return
            
            print_success(f"✓ {len(kits)} kits carregados")
            print_info("Iniciando processamento...")
            
            results = orch.process_kits(kits, check_stock=Config.CHECK_MIN_STOCK)
            
            print_header("RESULTADO DO PROCESSAMENTO")
            print(f"✓ Sucesso: {results['success']}")
            print(f"✗ Falhas: {results['failed']}")
            print(f"🏭 OPs Criadas: {results['ops_created']}")
            print(f"🛒 POs Criadas: {results['pos_created']}")
            print(f"⏱️  Tempo Total: {results['elapsed_time_seconds']}s")
            
            if orch.failed_items:
                print_warning(f"\n⚠ Itens com falha: {', '.join(orch.failed_items)}")
            
        except BlingAuthError as e:
            print_error(f"✗ Erro de autenticação: {e}")
            print_info("Execute: python bling_enhanced.py --serve")
            sys.exit(1)
        except Exception as e:
            print_error(f"✗ Erro durante processamento: {e}")
            error_logger.exception("Erro detalhado:")
            sys.exit(1)
        return

    parser.print_help()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n✓ Encerrado pelo usuário")
        sys.exit(0)
    except Exception as e:
        print_error(f"ERRO FATAL: {e}")
        error_logger.exception("Erro fatal:")
        sys.exit(1)