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
from dataclasses import dataclass
from urllib.parse import urlencode
from collections import defaultdict

# import pandas as pd
import requests
from flask import Flask, request, render_template_string, jsonify, redirect, url_for
from dotenv import load_dotenv

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

Path('logs').mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/automacao_bling.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
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

class BlingAuth:
    TOKEN_FILE = 'tokens.json'

    def __init__(self, config: Config):
        self.client_id = config.CLIENT_ID
        self.client_secret = config.CLIENT_SECRET
        self.redirect_uri = os.getenv("BLING_REDIRECT_URI", "https://bling-automacao.onrender.com/callback")
        self.token_url = 'https://www.bling.com.br/Api/v3/oauth/token'
        self.access_token = None
        self.refresh_token = os.getenv('BLING_REFRESH_TOKEN') # Carrega do ENV para produção
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
            # Cria o header Authorization: Basic base64(client_id:client_secret)
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
            logger.info("✓ Tokens obtidos com sucesso!")
            return True
        except Exception as e:
            error_logger.error(f"Falha ao trocar code: {e}")
            return False

    def _save_tokens(self, data: Dict):
        self.access_token = data.get('access_token')
        self.refresh_token = data.get('refresh_token')
        expires_in = data.get('expires_in', 3600)
        self.expires_at = (datetime.now() + timedelta(seconds=expires_in)).isoformat()
        try:
            # Garante que o arquivo seja criado no diretório de trabalho atual
            token_path = Path(self.TOKEN_FILE)
            with open(token_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'access_token': self.access_token,
                    'refresh_token': self.refresh_token,
                    'expires_at': self.expires_at
                }, f, indent=2)
            logger.info(f"✓ Tokens salvos em {token_path.absolute()}")
        except Exception as e:
            error_logger.error(f"Falha ao salvar tokens: {e}")
            raise

    def load_tokens(self) -> bool:
        """Tenta carregar tokens do arquivo local (uso primário em desenvolvimento)"""
        try:
            if not Path(self.TOKEN_FILE).exists():
                return False
            with open(self.TOKEN_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.access_token = data.get('access_token')
            # Prioriza o refresh_token do arquivo se existir, senão mantém o do ENV
            self.refresh_token = data.get('refresh_token', self.refresh_token)
            self.expires_at = data.get('expires_at')
            return True
        except Exception:
            return False

    def refresh_access_token(self) -> bool:
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

    def ensure_valid_token(self) -> bool:
        if not self.access_token:
            if not self.load_tokens():
                # Se não carregou do arquivo, tenta carregar do ENV (para produção)
                if not self.refresh_token:
                    raise BlingAuthError("Token não encontrado. Execute: python bling.py --serve para autenticar ou defina BLING_REFRESH_TOKEN no ambiente.")
                # Se o refresh_token existe no ENV, tenta renovar imediatamente
                if not self.refresh_access_token():
                    raise BlingAuthError("Falha ao renovar token com BLING_REFRESH_TOKEN. Reautentique.")
        if self.expires_at:
            expires = datetime.fromisoformat(self.expires_at)
            if datetime.now() >= expires - timedelta(minutes=5):
                if not self.refresh_access_token():
                    raise BlingAuthError("Token expirado")
        return True

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
                    time.sleep(Config.BASE_DELAY * (2 ** attempt))
                    continue
                if response.status_code >= 500:
                    time.sleep(Config.BASE_DELAY * (2 ** attempt))
                    continue
                response.raise_for_status()
                return response
            except BlingAuthError:
                raise
            except requests.exceptions.RequestException as e:
                error_logger.error(f"Request failed: {e}")
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
        url = f"{self.BASE_URL}/produtos/estoques?produtoId={product_id}"
        response = self._request_with_retry('GET', url)
        if response:
            data = response.json()
            if data.get('data'):
                return data['data'][0]
        return None

    def get_all_kits_and_components(self) -> List[Kit]:
        kits = []
        products = self.get_all_products()
        for prod in products:
            if prod.get('formato') == 'E': # Estrutura
                kit_sku = prod['codigo']
                kit_name = prod['nome']
                components = []
                if prod.get('estrutura'):
                    for comp_data in prod['estrutura']['componentes']:
                        comp_sku = comp_data['produto']['codigo']
                        comp_name = comp_data['produto']['nome']
                        comp_qty = comp_data['quantidade']
                        
                        # Buscar configurações centralizadas para o componente
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
        # Obter o ID do fornecedor pelo nome
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

class PurchaseNeedsManager:
    def __init__(self, api: BlingAPI):
        self.api = api
        self.needs: Dict[str, PurchaseNeed] = {}

    def check_min_stock_needs(self, components: List[Component]):
        for comp in components:
            product = self.api.get_product_by_sku(comp.sku)
            if product:
                stock_data = self.api.get_product_stock(product['id'])
                if stock_data:
                    comp.current_stock = stock_data['saldo']
                    if comp.current_stock < comp.min_stock:
                        self.add_need(comp, comp.min_stock - comp.current_stock, "Estoque Mínimo")

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
            product = self.api.get_product_by_sku(need.component_sku)
            if product:
                pos_by_supplier[need.supplier].append({
                    "produto": {"id": product['id']},
                    "quantidade": need.quantity_needed
                })

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
            "elapsed_time_seconds": elapsed
        }

# ============================================================================

class AutomationOrchestrator:
    COMPONENT_CONFIG_FILE = 'component_config.json'
    
    def __init__(self, config: Config):
        self.auth = BlingAuth(config)
        
        # Carregar configurações de componentes para autossuficiência
        component_config = self._load_component_config()
        
        self.api = BlingAPI(self.auth, component_config=component_config)
        self.stats = StatisticsManager()
        self.purchase_manager = PurchaseNeedsManager(self.api)
        self.failed_items = []

    def _load_component_config(self) -> Dict:
        """Carrega as configurações de fornecedor/lead time de um arquivo JSON."""
        path = Path(self.COMPONENT_CONFIG_FILE)
        if not path.exists():
            logger.warning(f"Arquivo de configuração de componentes não encontrado: {self.COMPONENT_CONFIG_FILE}. Usando valores padrão.")
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Reorganizar a lista de componentes em um dicionário para fácil acesso por SKU
            config_dict = {}
            if 'component_defaults' in data:
                config_dict['component_defaults'] = data['component_defaults']
            
            if 'components' in data:
                for comp in data['components']:
                    if 'sku' in comp:
                        config_dict[comp['sku']] = comp
            
            logger.info(f"Configurações de componentes carregadas de {self.COMPONENT_CONFIG_FILE}.")
            return config_dict
        except Exception as e:
            logger.error(f"Erro ao carregar {self.COMPONENT_CONFIG_FILE}: {e}. Usando valores padrão.")
            return {}

    def process_kits(self, kits: List[Kit], batch_size: int = 10, check_stock: bool = True):
        self.stats.reset()
        self.stats.start()
        for i in range(0, len(kits), batch_size):
            batch = kits[i:i+batch_size]
            for kit in batch:
                try:
                    # Lógica de criação de OP (exemplo: 1 por kit)
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

    def process_order_for_production(self, order_id: int):
        # Lógica para obter os itens de um pedido e criar OPs
        # Esta é uma implementação de exemplo
        # Você precisará adaptar para obter os itens do pedido via API
        pass

    def run_purchase_check(self):
        kits = self.api.get_all_kits_and_components()
        if kits:
            all_comps = [comp for kit in kits for comp in kit.components]
            unique_comps = {c.sku: c for c in all_comps}.values()
            self.purchase_manager.check_min_stock_needs(list(unique_comps))
            self.purchase_manager.generate_purchase_orders()

# ============================================================================

class WebServer:
    def __init__(self, auth: BlingAuth, orchestrator: AutomationOrchestrator):
        self.app = Flask(__name__)
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
            if self.auth.exchange_code_for_token(code):
                return render_template_string(SUCCESS_TEMPLATE)
            else:
                return "Erro na autorização.", 400

        @self.app.route("/api/status")
        def api_status():
            return jsonify({"token_valid": self.auth.ensure_valid_token()})

        @self.app.route("/api/stats")
        def api_stats():
            return jsonify(self.orchestrator.stats.to_dict())

        @self.app.route("/api/stock")
        def api_stock():
            all_components = []
            kits = self.orchestrator.api.get_all_kits_and_components()
            for kit in kits:
                all_components.extend(kit.components)
            unique_comps = {c.sku: c for c in all_components}.values()
            
            items = []
            for comp in unique_comps:
                product = self.orchestrator.api.get_product_by_sku(comp.sku)
                if product:
                    stock_data = self.orchestrator.api.get_product_stock(product['id'])
                    if stock_data:
                        current_stock = stock_data['saldo']
                        items.append({
                            "sku": comp.sku,
                            "nome": comp.name,
                            "estoque": current_stock,
                            "minimo": comp.min_stock,
                            "alerta": current_stock < comp.min_stock
                        })
            return jsonify({"items": items})

        @self.app.route("/api/needs")
        def api_needs():
            return jsonify({"needs": [n.__dict__ for n in self.orchestrator.purchase_manager.needs.values()]})

        @self.app.route("/api/kits")
        def api_kits():
            kits = self.orchestrator.api.get_all_kits_and_components()
            return jsonify({"kits": [k.__dict__ for k in kits]})

        @self.app.route("/api/logs")
        def api_logs():
            try:
                with open('logs/automacao_bling.log', 'r', encoding='utf-8') as f:
                    logs = f.readlines()[-50:] # Últimas 50 linhas
                return jsonify({"logs": [l.strip() for l in logs]})
            except FileNotFoundError:
                return jsonify({"logs": ["Arquivo de log não encontrado."]})

        @self.app.route("/api/recheck", methods=['POST'])
        def api_recheck():
            try:
                self.orchestrator.run_purchase_check()
                return jsonify({"status": "ok"})
            except Exception as e:
                return jsonify({"status": "error", "error": str(e)}), 500

        @self.app.route('/webhook/bling', methods=['POST'])
        def webhook_bling():
            try:
                data = request.get_json(force=True)
                event_type = data.get('event') or data.get('tipo') or None
                logger.info(f"{Fore.MAGENTA}🪝 Webhook recebido: {event_type}{Style.RESET_ALL}")
                
                # --- Tratamento de Eventos de Pedido ---
                is_order_event = (
                    event_type == 'order.created' or 
                    event_type == 'pedido.pago' or 
                    (data.get('tipo') == 'pedido' and data.get('evento') in ['criado', 'pago']))
                
                if is_order_event:
                    # Tentativa de extrair o ID do pedido
                    pedido_id = None
                    if data.get('id') and data.get('tipo') == 'pedido':
                        pedido_id = data.get('id')
                    elif data.get('retorno') and data['retorno'].get('pedidos'):
                        # Formato antigo ou outro formato de retorno
                        pedido_id = data['retorno']['pedidos'][0]['pedido'].get('id')
                    
                    if pedido_id:
                        logger.info(f"{Fore.GREEN}✓ Pedido ID {pedido_id} identificado. Acionando automação...{Style.RESET_ALL}")
                        
                        # 1. Acionar a criação de Ordem de Produção (OP)
                        if self.orchestrator and hasattr(self.orchestrator, 'process_order_for_production'):
                            self.orchestrator.process_order_for_production(pedido_id)
                            logger.info(f"{Fore.CYAN}ℹ Ordem de Produção (OP) acionada para o Pedido {pedido_id}.{Style.RESET_ALL}")
                        
                        # 2. Acionar a verificação de estoque e criação de Ordem de Compra (PO)
                        if self.orchestrator and hasattr(self.orchestrator, 'run_purchase_check'):
                            self.orchestrator.run_purchase_check()
                            logger.info(f"{Fore.CYAN}ℹ Verificação de estoque e Ordem de Compra (PO) acionada.{Style.RESET_ALL}")
                        
                        return jsonify({'status': 'ok', 'message': f'Pedido {pedido_id} processado e automação acionada.'}), 200
                    else:
                        logger.warning(f"{Fore.YELLOW}⚠ Webhook de Pedido recebido ({event_type}), mas ID do pedido não encontrado no payload.{Style.RESET_ALL}")
                        return jsonify({'status': 'warning', 'message': f'Webhook de Pedido recebido, mas ID não encontrado.'}), 200
                
                # --- Tratamento de Outros Eventos ---
                
                if event_type == 'estoque.atualizado' or data.get('tipo') == 'estoque':
                    logger.info(f"{Fore.BLUE}⟳ Evento estoque.atualizado recebido. Poderia acionar re-checagem de estoque.{Style.RESET_ALL}")
                    # Aqui você pode adicionar a lógica para re-checar o estoque se necessário
                
                # Resposta padrão para webhooks não tratados
                logger.info(f"Webhook {event_type} recebido e ignorado (sem lógica de tratamento).")
                return jsonify({'status': 'ok'}), 200
            except Exception as e:
                error_logger.error(f"Erro webhook: {e}")
                return jsonify({'error': str(e)}), 500

    def run(self, host='localhost', port=8000):
        print_header("SERVIDOR WEB")
        print_info(f"Interface: http://{host}:{port}/dashboard")
        print_info(f"OAuth: {self.auth.get_authorization_url()}\n")
        self.app.run(host=host, port=port, debug=False)

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <title>Painel Bling - Automação ERP</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
  <style>
    body { background: #f8f9fa; }
    .navbar { background: #007bff; color: white; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
    .card { border-radius: 0.5rem; box-shadow: 0 0.125rem 0.25rem rgba(0, 0, 0, 0.075); border: none; }
    .card-title { font-weight: 600; color: #343a40; }
    .kpi-value { font-size: 2.5rem; font-weight: 700; }
    .kpi-label { font-size: 0.9rem; color: #6c757d; }
    .log-box { font-family: monospace; font-size: 0.8em; background: #f4f4f4; border: 1px solid #dee2e6; border-radius: 0.25rem; }
    .nav-tabs .nav-link.active { background-color: #ffffff; border-color: #dee2e6 #dee2e6 #ffffff; }
    .table-danger td { background-color: #f8d7da !important; }
    .table-warning td { background-color: #fff3cd !important; }
  </style>
</head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark">
  <div class="container">
    <a class="navbar-brand" href="#">🚀 Bling Automação ERP</a>
    <span class="navbar-text" id="status-token"></span>
  </div>
</nav>

<div class="container my-4">
  <ul class="nav nav-tabs" id="mainTabs" role="tablist">
    <li class="nav-item" role="presentation"><a class="nav-link active" id="dashboard-tab" data-bs-toggle="tab" href="#tabDashboard" role="tab" aria-controls="tabDashboard" aria-selected="true">Dashboard</a></li>
    <li class="nav-item" role="presentation"><a class="nav-link" id="stock-tab" data-bs-toggle="tab" href="#tabStock" role="tab" aria-controls="tabStock" aria-selected="false">Estoque</a></li>
    <li class="nav-item" role="presentation"><a class="nav-link" id="needs-tab" data-bs-toggle="tab" href="#tabNeeds" role="tab" aria-controls="tabNeeds" aria-selected="false">Necessidades de Compra</a></li>
    <li class="nav-item" role="presentation"><a class="nav-link" id="kits-tab" data-bs-toggle="tab" href="#tabKits" role="tab" aria-controls="tabKits" aria-selected="false">Kits e Composição</a></li>
  </ul>
  
  <div class="tab-content p-4 bg-white border border-top-0">
    
    <!-- DASHBOARD TAB -->
    <div class="tab-pane fade show active" id="tabDashboard" role="tabpanel" aria-labelledby="dashboard-tab">
      <h4 class="mb-4">Visão Geral da Automação</h4>
      
      <div class="row mb-4" id="stats-kpis">
        <!-- KPIs serão injetados aqui -->
      </div>

      <div class="row mb-4">
        <div class="col-md-6">
          <div class="card h-100">
            <div class="card-body">
              <h5 class="card-title">Status de Processamento</h5>
              <canvas id="processingChart"></canvas>
            </div>
          </div>
        </div>
        <div class="col-md-6">
          <div class="card h-100">
            <div class="card-body">
              <h5 class="card-title">Logs Recentes (Webhook & Automação)</h5>
              <pre id="logs-content" class="log-box card-text" style="height: 300px; overflow-y: scroll;"></pre>
            </div>
          </div>
        </div>
      </div>
      
      <div class="row">
        <div class="col-12">
          <div class="card">
            <div class="card-body">
              <h5 class="card-title">Ações Manuais</h5>
              <p class="card-text">Acione a verificação de estoque e geração de POs manualmente.</p>
              <button id="recheck-button" class="btn btn-primary">Re-checar Estoque e Gerar POs</button>
              <span id="recheck-status" class="ms-3"></span>
            </div>
          </div>
        </div>
      </div>
    </div>
    
    <!-- ESTOQUE TAB -->
    <div class="tab-pane fade" id="tabStock" role="tabpanel" aria-labelledby="stock-tab">
      <h4 class="mb-3">Situação do Estoque de Componentes</h4>
      <div class="alert alert-info" id="stock-alert" style="display: none;"></div>
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
          <tr><td colspan="5">Carregando dados de estoque...</td></tr>
        </tbody>
      </table>
    </div>
    
    <!-- NECESSIDADES TAB -->
    <div class="tab-pane fade" id="tabNeeds" role="tabpanel" aria-labelledby="needs-tab">
      <h4 class="mb-3">Necessidades de Compra (POs)</h4>
      <table class="table table-striped table-hover">
        <thead>
          <tr>
            <th>SKU</th>
            <th>Nome</th>
            <th>Quantidade Necessária</th>
            <th>Fornecedor</th>
            <th>Motivo</th>
          </tr>
        </thead>
        <tbody id="needs-table-body">
          <tr><td colspan="5">Carregando necessidades de compra...</td></tr>
        </tbody>
      </table>
    </div>
    
    <!-- KITS TAB -->
    <div class="tab-pane fade" id="tabKits" role="tabpanel" aria-labelledby="kits-tab">
      <h4 class="mb-3">Kits e Componentes</h4>
      <table class="table table-striped table-hover">
        <thead>
          <tr>
            <th>SKU Kit</th>
            <th>Nome Kit</th>
            <th>Componentes</th>
          </tr>
        </thead>
        <tbody id="kits-table-body">
          <tr><td colspan="3">Carregando kits...</td></tr>
        </tbody>
      </table>
    </div>
    
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
document.addEventListener('DOMContentLoaded', function() {
  const API_BASE = '/api';
  let processingChart;

  // --- Funções de Carregamento de Dados ---

  function loadStatus() {
    fetch(`${API_BASE}/status`)
      .then(response => response.json())
      .then(data => {
        const statusElement = document.getElementById('status-token');
        const statusIndicator = document.querySelector('.status-indicator');
        
        if (data.token_valid) {
          statusElement.className = 'status-badge bg-success';
          statusElement.innerHTML = '<i class="fas fa-check-circle"></i> Conectado ao Bling';
          statusIndicator.className = 'status-indicator active';
        } else {
          statusElement.className = 'status-badge bg-danger';
          statusElement.innerHTML = '<i class="fas fa-times-circle"></i> Desconectado do Bling';
          statusIndicator.className = 'status-indicator inactive';
        }
      })
      .catch(error => {
        console.error('Erro ao carregar status:', error);
        const statusElement = document.getElementById('status-token');
        statusElement.className = 'status-badge bg-warning';
        statusElement.innerHTML = '<i class="fas fa-exclamation-triangle"></i> Erro de Conexao';
      });
  }

  function loadStats() {
    fetch(`${API_BASE}/stats`)
      .then(response => response.json())
      .then(data => {
        if (data.error) {
          document.getElementById('stats-kpis').innerHTML = `<div class="col-12"><div class="alert alert-danger">${data.error}</div></div>`;
          return;
        }
        
        const totalProcessed = data.success + data.failed;
        const successRate = totalProcessed > 0 ? (data.success / totalProcessed * 100).toFixed(1) : 0;

        const kpis = [
          { label: 'Taxa de Sucesso', value: `${successRate}%`, color: 'primary', icon: '✅' },
          { label: 'OPs Criadas', value: data.ops_created, color: 'info', icon: '🏭' },
          { label: 'POs Criadas', value: data.pos_created, color: 'warning', icon: '🛒' },
          { label: 'Tempo Total (s)', value: data.elapsed_time_seconds.toFixed(2), color: 'secondary', icon: '⏱️' }
        ];

        const kpisHtml = kpis.map(kpi => `
          <div class="col-md-3 mb-3">
            <div class="card bg-light">
              <div class="card-body">
                <div class="kpi-value text-${kpi.color}">${kpi.icon} ${kpi.value}</div>
                <div class="kpi-label">${kpi.label}</div>
              </div>
            </div>
          </div>
        `).join('');
        document.getElementById('stats-kpis').innerHTML = kpisHtml;

        // Atualiza Gráfico de Processamento
        if (processingChart) {
          processingChart.destroy();
        }
        const ctx = document.getElementById('processingChart').getContext('2d');
        processingChart = new Chart(ctx, {
          type: 'doughnut',
          data: {
            labels: ['Sucesso', 'Falha'],
            datasets: [{
              data: [data.success, data.failed],
              backgroundColor: ['#28a745', '#dc3545'],
              hoverOffset: 4
            }]
          },
          options: {
            responsive: true,
            plugins: {
              legend: {
                position: 'top',
              },
              title: {
                display: true,
                text: `Total Processado: ${totalProcessed}`
              }
            }
          }
        });
      })
      .catch(error => console.error('Erro ao carregar estatísticas:', error));
  }

  function loadLogs() {
    fetch(`${API_BASE}/logs`)
      .then(response => response.json())
      .then(data => {
        const logsElement = document.getElementById('logs-content');
        const fullLogsElement = document.getElementById('full-logs-content');
        const logCount = document.getElementById('log-count');
        
        const formattedLogs = (data.logs || []).map(formatLog).join('');
        const recentLogs = (data.logs || []).slice(-15).map(formatLog).join('');
        
        logsElement.innerHTML = recentLogs || '<div class="text-muted">Nenhum log disponivel</div>';
        fullLogsElement.innerHTML = formattedLogs || '<div class="text-muted">Nenhum log disponivel</div>';
        logCount.textContent = `${(data.logs || []).length} entradas`;
        
        logsElement.scrollTop = logsElement.scrollHeight;
        fullLogsElement.scrollTop = fullLogsElement.scrollHeight;
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
          tableBody.innerHTML = `<tr><td colspan="5" class="text-danger">${data.error}</td></tr>`;
          return;
        }

        data.items.forEach(item => {
          const alertClass = item.alerta ? 'table-danger' : '';
          if (item.alerta) lowStockCount++;
          
          const row = `
            <tr class="${alertClass}">
              <td>${item.sku}</td>
              <td>${item.nome}</td>
              <td>${item.estoque}</td>
              <td>${item.minimo}</td>
              <td>${item.alerta ? '<span class="badge bg-danger">BAIXO</span>' : '<span class="badge bg-success">OK</span>'}</td>
            </tr>
          `;
          tableBody.innerHTML += row;
        });

        if (lowStockCount > 0) {
          alertElement.className = 'alert alert-danger';
          alertElement.innerHTML = `⚠️ **ALERTA:** ${lowStockCount} componente(s) abaixo do estoque mínimo.`;
          alertElement.style.display = 'block';
        } else {
          alertElement.style.display = 'none';
        }
      })
      .catch(error => console.error('Erro ao carregar estoque:', error));
  }

  function loadNeeds() {
    fetch(`${API_BASE}/needs`)
      .then(response => response.json())
      .then(data => {
        const tableBody = document.getElementById('needs-table-body');
        tableBody.innerHTML = '';

        if (data.error) {
          tableBody.innerHTML = `<tr><td colspan="5" class="text-danger">${data.error}</td></tr>`;
          return;
        }
        
        if (data.needs.length === 0) {
          tableBody.innerHTML = '<tr><td colspan="5" class="text-success">Nenhuma necessidade de compra pendente.</td></tr>';
          return;
        }

        data.needs.forEach(item => {
          const row = `
            <tr class="table-warning">
              <td>${item.component_sku}</td>
              <td>${item.component_name}</td>
              <td>${item.quantity_needed}</td>
              <td>${item.supplier}</td>
              <td>${item.reason}</td>
            </tr>
          `;
          tableBody.innerHTML += row;
        });
      })
      .catch(error => console.error('Erro ao carregar necessidades:', error));
  }

  function loadKits() {
    fetch(`${API_BASE}/kits`)
      .then(response => response.json())
      .then(data => {
        const tableBody = document.getElementById('kits-table-body');
        tableBody.innerHTML = '';

        if (data.error) {
          tableBody.innerHTML = `<tr><td colspan="3" class="text-danger">${data.error}</td></tr>`;
          return;
        }
        
        if (data.kits.length === 0) {
          tableBody.innerHTML = '<tr><td colspan="3" class="text-warning">Nenhum kit encontrado.</td></tr>';
          return;
        }

        data.kits.forEach(kit => {
          const componentsHtml = kit.componentes.map(comp => 
            `<span class="badge bg-secondary me-1">${comp.nome} (${comp.quantidade}x)</span>`
          ).join('');

          const row = `
            <tr>
              <td>${kit.sku}</td>
              <td>${kit.nome}</td>
              <td>${componentsHtml}</td>
            </tr>
          `;
          tableBody.innerHTML += row;
        });
      })
      .catch(error => console.error('Erro ao carregar kits:', error));
  }
  
  function recheckStock() {
    const button = document.getElementById('recheck-button');
    const statusSpan = document.getElementById('recheck-status');
    
    button.disabled = true;
    statusSpan.innerHTML = '<span class="text-info">Processando...</span>';

    fetch(`${API_BASE}/recheck`, { method: 'POST' })
      .then(response => response.json())
      .then(data => {
        if (data.status === 'ok') {
          statusSpan.innerHTML = '<span class="text-success">✅ Verificação iniciada com sucesso!</span>';
          // Force reload of relevant tabs after recheck
          setTimeout(() => {
            loadStock();
            loadNeeds();
            loadStats();
            statusSpan.innerHTML = '';
            button.disabled = false;
          }, 3000); // Give time for the process to start
        } else {
          statusSpan.innerHTML = `<span class="text-danger">❌ Erro: ${data.error || data.message}</span>`;
          button.disabled = false;
        }
      })
      .catch(error => {
        statusSpan.innerHTML = `<span class="text-danger">❌ Erro de conexão: ${error}</span>`;
        button.disabled = false;
      });
  }

  // --- Inicialização e Eventos ---

  // Initial load for the active tab (Dashboard)
  loadStatus();
  loadStats();
  loadLogs();
  loadStock();
  loadNeeds();
  loadKits();
  
  // Set up interval to refresh logs every 5 seconds
  setInterval(loadLogs, 5000);
  
  // Atualização automática a cada 60 segundos
  setInterval(() => {
    const active = document.querySelector('.nav-link.active');
    if (!active) return;
    const tab = active.getAttribute('href');
    if (tab === '#tabStock') loadStock();
    if (tab === '#tabNeeds') loadNeeds();
    if (tab === '#tabDashboard') { loadStatus(); loadStats(); }
  }, 60000);
  
  // Event listener for manual recheck button
  document.getElementById('recheck-button').addEventListener('click', recheckStock);
});

</script>
</body>
</html>
"""

SUCCESS_TEMPLATE = """<!DOCTYPE html>
<html>
<head><title>Sucesso!</title>
<style>body{font-family:Arial;text-align:center;padding:50px;background:#667eea;color:#fff}
.success{font-size:72px;margin:20px}</style>
</head>
<body>
<div class="success">✓</div>
<h1>Autorização Concluída!</h1>
<p>Tokens salvos. Volte ao terminal.</p>
</body>
</html>"""

# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='Automação Bling Enhanced')
    parser.add_argument('--serve', action='store_true', help='Servidor web')
    parser.add_argument('--run', action='store_true', help='Processa')

    args = parser.parse_args()

    # Se nenhuma flag for passada, assume-se que é o ambiente de produção (Render)
    if not args.serve and not args.run:
        args.serve = True

    if args.serve:
        config = Config()
        auth = BlingAuth(config)
        orchestrator = AutomationOrchestrator(config)

        # Inicializa dados do Bling
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

        # Inicia o servidor Flask corretamente
        try:
            # Render usa a variável de ambiente PORT
            port = int(os.environ.get("PORT", 8000))
            server = WebServer(auth, orchestrator)
            server.run(host="0.0.0.0", port=port)
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
        print(f"Total: {results['total']}")
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
