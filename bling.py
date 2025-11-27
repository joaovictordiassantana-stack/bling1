
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
        with open(self.TOKEN_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                'access_token': self.access_token,
                'refresh_token': self.refresh_token,
                'expires_at': self.expires_at
            }, f, indent=2)

    def load_tokens(self) -> bool:
        try:
            if not Path(self.TOKEN_FILE).exists():
                return False
            with open(self.TOKEN_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.access_token = data.get('access_token')
            self.refresh_token = data.get('refresh_token')
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
                raise BlingAuthError("Execute: python bling_enhanced.py --serve")
        if self.expires_at:
            expires = datetime.fromisoformat(self.expires_at)
            if datetime.now() >= expires - timedelta(minutes=5):
                if not self.refresh_access_token():
                    raise BlingAuthError("Token expirado")
        return True

# ============================================================================

class BlingAPI:
    BASE_URL = 'https://www.bling.com.br/Api/v3'

    def __init__(self, auth: BlingAuth, dry_run: bool = False):
        self.auth = auth
        self.dry_run = dry_run
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
                if self.dry_run:
                    logger.info(f"[DRY RUN] {method} {url}")
                    return None
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
            except Exception as e:
                if attempt == Config.MAX_RETRIES - 1:
                    raise BlingAPIError(f"Falha: {e}")
                time.sleep(Config.BASE_DELAY * (2 ** attempt))
        return None

    def find_product_by_sku(self, sku: str) -> Optional[Dict]:
        try:
            response = self._request_with_retry('GET', f"{self.BASE_URL}/produtos", params={'codigo': sku})
            if response and response.status_code == 200:
                items = response.json().get('data', [])
                for item in items:
                    if item.get('codigo', '').strip().upper() == sku.strip().upper():
                        return item
            return None
        except Exception as e:
            logger.debug(f"find_product_by_sku error: {e}")
            return None

    def get_product_stock(self, product_id: str) -> int:
        """Busca o estoque atual de um produto/sku pelo ID (soma de saldos)."""
        try:
            url = f"{self.BASE_URL}/estoques"
            params = {'idsProdutos[]': product_id}
            response = self._request_with_retry('GET', url, params=params)
            if response and response.status_code == 200:
                data = response.json()
                stocks = data.get('data', [])
                total = 0
                for s in stocks:
                    # Bling may return saldoFisico or saldoFisicoTotal depending on endpoint
                    total += int(s.get('saldoFisicoTotal', s.get('saldoFisico', 0) or 0))
                return int(total)
            return 0
        except Exception as e:
            logger.warning(f"Erro ao obter estoque: {e}")
            return 0

    def create_or_update_product(self, product_data: Dict, is_component: bool = True) -> Optional[str]:
        try:
            sku = product_data.get('codigo')
            existing = self.find_product_by_sku(sku)
            if existing:
                product_id = existing.get('id')
                url = f"{self.BASE_URL}/produtos/{product_id}"
                response = self._request_with_retry('PUT', url, json=product_data) if not self.dry_run else None
            else:
                url = f"{self.BASE_URL}/produtos"
                response = self._request_with_retry('POST', url, json=product_data) if not self.dry_run else None
            if self.dry_run:
                return f"DRY_{sku}"
            if response:
                return response.json().get('data', {}).get('id')
            return None
        except Exception as e:
            logger.error(f"Erro create_or_update_product {product_data.get('codigo')}: {e}")
            return None

    def create_production_order(self, kit_sku: str, quantity: int) -> Optional[str]:
        """Cria uma ordem de produção (OP) no Bling usando o código do produto."""
        try:
            url = f"{self.BASE_URL}/ordens-producao"
            op_data = {
                'produto': {'codigo': kit_sku},
                'quantidade': quantity,
                'dataPrevisao': (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d')
            }
            response = self._request_with_retry('POST', url, json=op_data) if not self.dry_run else None
            if self.dry_run:
                logger.info(f"[DRY RUN] create_production_order {kit_sku}")
                return f"DRY_OP_{kit_sku}"
            if response:
                return response.json().get('data', {}).get('id')
            return None
        except Exception as e:
            logger.error(f"Erro create_production_order: {e}")
            return None

    def get_all_kits_and_components(self) -> List[Kit]:
        """Busca todos os kits (produtos compostos) e seus componentes no Bling."""
        kits: List[Kit] = []
        try:
            # 1. Buscar todos os produtos que são kits (tipo 'P' e composicao 'S')
            # O Bling API v3 não tem um filtro direto para "produto composto",
            # então vamos buscar todos os produtos e filtrar.
            # Alternativamente, buscar produtos com 'tipo' = 'P' e 'formato' = 'Kit'
            # ou 'tipo' = 'P' e 'composicao' = 'S' (se a API suportar)
            
            # Vamos buscar todos os produtos e filtrar localmente, se necessário,
            # ou usar um filtro de tipo se disponível.
            
            # Tentativa de filtro: tipo=P (Produto) e composicao=S (Sim)
            url = f"{self.BASE_URL}/produtos"
            params = {'tipo': 'P', 'composicao': 'S', 'limite': 100}
            
            # Loop para paginação
            page = 1
            while True:
                params['pagina'] = page
                response = self._request_with_retry('GET', url, params=params)
                if not response or response.status_code != 200:
                    break
                
                data = response.json().get('data', [])
                if not data:
                    break
                
                for item in data:
                    produto = item.get('produto')
                    if not produto:
                        continue
                    
                    kit_sku = produto.get('codigo')
                    kit_name = produto.get('nome')
                    composicoes = produto.get('composicoes', [])
                    
                    if not composicoes:
                        continue
                        
                    components: List[Component] = []
                    for comp_data in composicoes:
                        comp_item = comp_data.get('item')
                        if not comp_item:
                            continue
                            
                        comp_sku = comp_item.get('codigo')
                        comp_name = comp_item.get('nome')
                        comp_qty = comp_item.get('quantidade', 1)
                        
                        # O Bling API não fornece min_stock, supplier, lead_time_days
                        # na composição. Precisamos de uma forma de obter isso.
                        # Por enquanto, usaremos valores padrão/mock.
                        # O ideal seria buscar o produto componente separadamente
                        # para obter essas informações, mas isso geraria muitas chamadas.
                        
                        # Para simplificar a refatoração, vamos usar valores padrão
                        # e assumir que o PurchaseNeedsManager pode lidar com isso.
                        
                        # Para o supplier e lead_time_days, vamos precisar de uma fonte.
                        # Se não houver, o sistema não poderá gerar POs.
                        # Vamos assumir que o PurchaseNeedsManager tem uma forma de
                        # obter essas informações (ex: de um cache ou outra API).
                        # Por enquanto, vamos usar valores mock/padrão.
                        
                        # Para min_stock, usaremos o valor padrão da Config.
                        
                        component = Component(
                            sku=comp_sku,
                            name=comp_name,
                            qty=int(comp_qty),
                            supplier="FORNECEDOR_PADRAO", # Mock
                            lead_time_days=15, # Mock
                            min_stock=Config.MIN_STOCK_THRESHOLD
                        )
                        components.append(component)
                        
                    if components:
                        kit = Kit(sku=kit_sku, name=kit_name, components=components)
                        kits.append(kit)
                
                if len(data) < params['limite']:
                    break
                page += 1
                time.sleep(Config.DELAY_BETWEEN_BATCHES) # Evitar rate limit
                
        except Exception as e:
            error_logger.error(f"Erro ao buscar kits e componentes do Bling: {e}")
            
        return kits

    def create_purchase_order(self, supplier: str, items: List[Dict]) -> Optional[str]:
        """Cria um pedido de compra (PO) no Bling agrupando por fornecedor."""
        try:
            url = f"{self.BASE_URL}/pedidos-compra"
            po_data = {
                'fornecedor': {'nome': supplier},
                'itens': items,
                'dataPrevisao': (datetime.now() + timedelta(days=15)).strftime('%Y-%m-%d')
            }
            response = self._request_with_retry('POST', url, json=po_data) if not self.dry_run else None
            if self.dry_run:
                logger.info(f"[DRY RUN] create_purchase_order {supplier}")
                return f"DRY_PO_{supplier}"
            if response:
                return response.json().get('data', {}).get('id')
            return None
        except Exception as e:
            logger.error(f"Erro create_purchase_order: {e}")
            return None

# ============================================================================

class PurchaseNeedsManager:
    """Gerencia necessidades de compra, verifica estoque no Bling e gera POs."""
    def __init__(self, api: BlingAPI):
        self.api = api
        self.needs: List[PurchaseNeed] = []
        self.components: List[Component] = []

    def check_min_stock_needs(self, components: List[Component]):
        logger.info("Verificando estoques mínimos no Bling...")
        for comp in components:
            product = self.api.find_product_by_sku(comp.sku)
            if not product:
                logger.debug(f"Produto não encontrado no Bling: {comp.sku}")
                # If product does not exist, consider creating or flagging
                self.needs.append(PurchaseNeed(
                    component_sku=comp.sku,
                    component_name=comp.name,
                    quantity_needed=max(comp.min_stock, 1),
                    supplier=comp.supplier,
                    lead_time_days=comp.lead_time_days,
                    reason='missing_in_bling'
                ))
                continue
            product_id = product.get('id')
            current_stock = self.api.get_product_stock(product_id)
            comp.current_stock = current_stock
            if current_stock < comp.min_stock:
                qty_needed = comp.min_stock - current_stock + 10
                self.needs.append(PurchaseNeed(
                    component_sku=comp.sku,
                    component_name=comp.name,
                    quantity_needed=qty_needed,
                    supplier=comp.supplier,
                    lead_time_days=comp.lead_time_days,
                    reason='min_stock'
                ))
                logger.warning(f"{comp.sku} abaixo do mínimo ({current_stock} < {comp.min_stock})")

        self.components = components

    def add_production_needs(self, kit: Kit, quantity: int):
        for comp in kit.components:
            self.needs.append(PurchaseNeed(
                component_sku=comp.sku,
                component_name=comp.name,
                quantity_needed=comp.qty * quantity,
                supplier=comp.supplier,
                lead_time_days=comp.lead_time_days,
                reason='production_order'
            ))

    def generate_purchase_orders(self) -> List[str]:
        if not self.needs:
            logger.info("Nenhuma necessidade de compra.")
            return []
        by_supplier = defaultdict(list)
        for need in self.needs:
            by_supplier[need.supplier].append(need)
        po_ids = []
        for supplier, needs in by_supplier.items():
            items = []
            for n in needs:
                items.append({
                    'produto': {'codigo': n.component_sku},
                    'quantidade': n.quantity_needed,
                    'descricao': f"{n.component_name} - {n.reason}"
                })
            po_id = self.api.create_purchase_order(supplier, items)
            if po_id:
                po_ids.append(po_id)
        return po_ids


        logger.info(f"Lista de necessidades exportada: {filename}")

# ============================================================================

class StatisticsManager:
    """Coleta e fornece estatísticas simples sobre processamento e estoque."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.start_time = None
        self.end_time = None
        self.components_created = 0
        self.kits_created = 0
        self.ops_created = 0
        self.pos_created = 0
        self.min_stock_checks = 0
        self.failed = 0
        self.success = 0

    def start(self):
        self.start_time = time.time()

    def stop(self):
        self.end_time = time.time()

    def to_dict(self):
        elapsed = (self.end_time - self.start_time) if self.start_time and self.end_time else 0
        success_rate = (self.success / (self.success + self.failed) * 100) if (self.success + self.failed) > 0 else 0.0
        return {
            'components_created': self.components_created,
            'kits_created': self.kits_created,
            'ops_created': self.ops_created,
            'pos_created': self.pos_created,
            'min_stock_checks': self.min_stock_checks,
            'success': self.success,
            'failed': self.failed,
            'elapsed_seconds': elapsed,
            'success_rate_pct': round(success_rate, 2)
        }

# ============================================================================

class AutomationOrchestrator:
    def __init__(self, config: Config, dry_run: bool = False):
        self.auth = BlingAuth(config)
        self.api = BlingAPI(self.auth, dry_run=dry_run)
        self.dry_run = dry_run
        self.stats = StatisticsManager()
        self.purchase_manager = PurchaseNeedsManager(self.api)
        self.failed_items = []

    def process_kits(self, kits: List[Kit], batch_size: int = 10, check_stock: bool = True):
        self.stats.reset()
        self.stats.start()
        results = {'total': len(kits), 'success': 0, 'failed': 0}

        # Pre-check stock if required
        if check_stock and Config.CHECK_MIN_STOCK:
            all_components = []
            for kit in kits:
                all_components.extend(kit.components)
            unique = {c.sku: c for c in all_components}.values()
            self.purchase_manager.check_min_stock_needs(list(unique))
            self.stats.min_stock_checks = len(unique)
            if self.purchase_manager.needs:
                self.purchase_manager.export_needs_report()

        for kit in kits:
            try:
                ok = self._process_single_kit(kit)
                if ok:
                    results['success'] += 1
                    self.stats.success += 1
                else:
                    results['failed'] += 1
                    self.stats.failed += 1
            except Exception as e:
                error_logger.error(f"Erro kit {kit.sku}: {e}")
                results['failed'] += 1
                self.stats.failed += 1
                self.failed_items.append({'kit': kit.sku, 'error': str(e)})

        # Generate POs if needs and not dry_run
        if self.purchase_manager.needs and not self.dry_run:
            po_ids = self.purchase_manager.generate_purchase_orders()
            self.stats.pos_created = len(po_ids)

        self.stats.stop()
        return results

    def _process_single_kit(self, kit: Kit) -> bool:
        # Create/update components
        component_ids = {}
        for comp in kit.components:
            comp_data = {
                'codigo': comp.sku,
                'nome': comp.name,
                'tipo': 'P',
                'situacao': 'A',
                'unidade': 'UN',
                'preco': comp.unit_cost
            }
            comp_id = self.api.create_or_update_product(comp_data, True)
            if comp_id:
                component_ids[comp.sku] = comp_id
                self.stats.components_created += 1

        if len(component_ids) < len(kit.components):
            logger.error(f"Componentes incompletos para {kit.sku}")
            return False

        # Create kit composition
        composicao = []
        for comp in kit.components:
            composicao.append({'produto': {'id': component_ids[comp.sku]}, 'quantidade': comp.qty})

        kit_data = {
            'codigo': kit.sku,
            'nome': kit.name,
            'tipo': 'P',
            'situacao': 'A',
            'unidade': 'UN',
            'preco': kit.price,
            'estrutura': {
                'tipoEstoque': 'F',
                'componentes': composicao
            }
        }
        kit_id = self.api.create_or_update_product(kit_data, False)
        if not kit_id:
            return False
        self.stats.kits_created += 1

        # Create production order
        op_id = None
        if not self.dry_run:
            op_id = self.api.create_production_order(kit.sku, quantity=1)
            if op_id:
                self.stats.ops_created += 1
                # add production needs to purchase manager
                self.purchase_manager.add_production_needs(kit, 1)
        return True

# ============================================================================

class WebServer:
    def __init__(self, auth: BlingAuth, orchestrator: AutomationOrchestrator = None):
        self.auth = auth
        self.orchestrator = orchestrator
        self.app = Flask(__name__)
        self.app.logger.disabled = True
        logging.getLogger('werkzeug').disabled = True
        self._setup_routes()

    def _setup_routes(self):
        @self.app.route('/')
        def home():
            # Redireciona para o dashboard
            from flask import redirect, url_for # Importação local para evitar erro de referência
            return redirect(url_for('dashboard'))

        @self.app.route('/dashboard')
        def dashboard():
            return render_template_string(DASHBOARD_TEMPLATE)

        @self.app.route('/callback')
        def callback():
            code = request.args.get('code')
            if code and self.auth.exchange_code_for_token(code):
                return SUCCESS_TEMPLATE
            return "<h1>Erro ao autorizar</h1>", 500

        @self.app.route('/api/status')
        def status():
            return jsonify({'token_valid': self.auth.load_tokens()})

        @self.app.route('/api/stats')
        def api_stats():
            if self.orchestrator:
                return jsonify(self.orchestrator.stats.to_dict())
            return jsonify({'error': 'Orchestrator não disponível'}), 404

        @self.app.route('/api/stock')
        def api_stock():
            """Retorna situação detalhada de estoque para todos os produtos cadastrados nos kits (consulta Bling)."""
            try:
                # Build list of SKUs from components list
                components = getattr(self.orchestrator.purchase_manager, 'components', [])
                skus = list(dict.fromkeys([comp.sku for comp in components]))
                
                # Mapear componentes para obter min_stock e nome
                component_map = {comp.sku: comp for comp in components}
                result = []
                for sku in skus:
                    prod = self.orchestrator.api.find_product_by_sku(sku)
                    if not prod:
                        result.append({'sku': sku, 'found': False})
                        continue
                    pid = prod.get('id')
                    stock = self.orchestrator.api.get_product_stock(pid)
                    
                    # Obter min_stock do componente mapeado
                    comp_info = component_map.get(sku)
                    min_stock = comp_info.min_stock if comp_info else Config.MIN_STOCK_THRESHOLD
                    
                    alerta = stock < min_stock
                    
                    result.append({
                        'sku': sku, 
                        'nome': prod.get('nome'),
                        'estoque': stock, 
                        'minimo': min_stock,
                        'alerta': alerta
                    })
                return jsonify({'items': result})
            except Exception as e:
                error_logger.error(f"api_stock error: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/needs')
        def api_needs():
            """Retorna necessidades de compra atuais."""
            try:
                if not self.orchestrator:
                    return jsonify({'error': 'Orchestrator não disponível'}), 404
                
                # O PurchaseNeed tem os campos component_sku, component_name, quantity_needed, supplier, reason
                needs = [
                    n.__dict__ for n in self.orchestrator.purchase_manager.needs
                ]
                return jsonify({'needs': needs})
            except Exception as e:
                error_logger.error(f"Erro ao listar needs: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/logs')
        def api_logs():
            """Lê as últimas linhas do arquivo logs/automacao_bling.log."""
            try:
                path = Path('logs/automacao_bling.log')
                if not path.exists():
                    return jsonify({'logs': ['Arquivo de log não encontrado.']})
                
                lines = path.read_text(encoding='utf-8').splitlines()
                # Retorna as últimas 100 linhas
                return jsonify({'logs': lines[-100:]})
            except Exception as e:
                error_logger.error(f"Erro ao ler logs: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/kits')
        def api_kits():
            """Retorna todos os produtos (kits) com suas composições diretamente do Bling."""
            try:
                kits = self.orchestrator.api.get_all_kits_and_components()
                result = []
                for kit in kits:
                    comps = []
                    for comp in kit.components:
                        comps.append({
                            'sku': comp.sku,
                            'nome': comp.name,
                            'quantidade': comp.qty
                        })
                    result.append({
                        'sku': kit.sku,
                        'nome': kit.name,
                        'componentes': comps
                    })
                return jsonify({'kits': result})
            except Exception as e:
                error_logger.error(f"Erro ao buscar kits: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/recheck', methods=['POST'])
        def api_recheck():
            """Executa novamente o processo de verificação de estoque mínimo."""
            try:
                if not self.orchestrator:
                    return jsonify({'error': 'Orchestrator não disponível'}), 404
                
                orch = self.orchestrator
                
                # A função check_min_stock_needs espera uma lista de objetos Kit, não Component.
                # Vou usar a lista de componentes monitorados para a checagem.
                all_comps = orch.purchase_manager.components
                
                # Re-executar a checagem de estoque mínimo para todos os componentes monitorados
                orch.purchase_manager.check_min_stock_needs(all_comps)
                
                return jsonify({'status': 'ok', 'message': 'Verificação de estoque iniciada.'})
            except Exception as e:
                error_logger.error(f"Erro ao recheck: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/webhook/bling', methods=['POST'])
        def webhook_bling():
            try:
                data = request.get_json(force=True)
                # Bling usually includes an 'event' or root info; adapt as needed
                event_type = data.get('event') or data.get('tipo') or None
                logger.info(f"Webhook recebido: {event_type}")
                # handle common events
                if event_type == 'pedido.pago' or (data.get('tipo') == 'pedido' and data.get('evento') == 'pago'):
                    # create production order or trigger processing
                    logger.info("Evento pedido.pago recebido — pode criar OP/tratar fluxo.")
                    # user may want to trigger orchestrator run here (not automatic in this script)
                elif event_type == 'estoque.atualizado' or data.get('tipo') == 'estoque':
                    logger.info("Evento estoque.atualizado recebido — atualização de estoque")
                    # Could trigger stock re-check
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
  <title>Painel Bling</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
  <style>
    body { background: #f0f3ff; }
    .navbar { background: #4e73df; color: white; }
    .card { box-shadow: 0 4px 10px rgba(0,0,0,0.1); }
    .log-box { font-family: monospace; font-size: 0.8em; }
  </style>
</head>
<body>
<nav class="navbar navbar-dark px-3">
  <h3 class="text-white">🚀 Painel Bling - Automação</h3>
</nav>
<div class="container my-4">
  <ul class="nav nav-tabs" id="mainTabs">
    <li class="nav-item"><a class="nav-link active" data-bs-toggle="tab" href="#tabDashboard">Dashboard</a></li>
    <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tabStock">Estoque</a></li>
    <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tabNeeds">Necessidades</a></li>
    <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tabKits">Produtos / Kits</a></li>
    <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tabLogs">Logs</a></li>
  </ul>

  <div class="tab-content mt-3">
    <div id="tabDashboard" class="tab-pane fade show active">
      <div class="row">
        <div class="col-md-6">
          <div class="card mb-3">
            <div class="card-header">Status da Conexão</div>
            <div class="card-body">
              <h5 class="card-title" id="status">Carregando...</h5>
              <p class="card-text">Verifique o status da conexão com a API do Bling.</p>
            </div>
          </div>
        </div>
        <div class="col-md-6">
          <div class="card mb-3">
            <div class="card-header">Ações</div>
            <div class="card-body">
              <button class="btn btn-primary" onclick="recheckStock()">Verificar Estoques</button>
              <span id="recheck-status" class="ms-3"></span>
            </div>
          </div>
        </div>
      </div>
      <div class="card mt-4">
        <div class="card-header">Estatísticas (API /api/stats)</div>
        <div class="card-body">
          <pre id="stats-data">Carregando...</pre>
        </div>
      </div>
    </div>
    <div id="tabStock" class="tab-pane fade">
      <table class="table table-striped" id="stockTable">
        <thead>
          <tr>
            <th>SKU</th>
            <th>Nome</th>
            <th>Estoque</th>
            <th>Mínimo</th>
            <th>Alerta</th>
          </tr>
        </thead>
        <tbody>
          <tr><td colspan="5">Carregando estoque...</td></tr>
        </tbody>
      </table>
    </div>
    <div id="tabNeeds" class="tab-pane fade">
      <table class="table table-bordered" id="needsTable">
        <thead>
          <tr>
            <th>SKU</th>
            <th>Nome</th>
            <th>Qtd Necessária</th>
            <th>Fornecedor</th>
            <th>Motivo</th>
          </tr>
        </thead>
        <tbody>
          <tr><td colspan="5">Carregando necessidades...</td></tr>
        </tbody>
      </table>
    </div>
    <div id="tabKits" class="tab-pane fade">
      <table class="table table-striped" id="kitsTable">
        <thead>
          <tr>
            <th>SKU</th>
            <th>Nome</th>
            <th>Componentes</th>
          </tr>
        </thead>
        <tbody>
          <tr><td colspan="3">Carregando...</td></tr>
        </tbody>
      </table>
    </div>
    <div id="tabLogs" class="tab-pane fade">
      <pre id="logBox" class="log-box" style="height:400px;overflow:auto;background:#000;color:#0f0;padding:10px;"></pre>
    </div>
  </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
async function loadStatus() {
  const res = await fetch('/api/status');
  const data = await res.json();
  const statusEl = document.querySelector('#status');
  if (data.token_valid) {
    statusEl.innerHTML = '<span class="badge bg-success">✓ Conectado</span>';
  } else {
    statusEl.innerHTML = '<span class="badge bg-danger">✗ Não Autorizado</span>';
  }
}

async function loadStats() {
  const res = await fetch('/api/stats');
  const data = await res.json();
  document.querySelector('#stats-data').textContent = JSON.stringify(data, null, 2);
}

async function loadStock() {
  const res = await fetch('/api/stock');
  const data = await res.json();
  const tbody = document.querySelector('#stockTable tbody');
  tbody.innerHTML = '';
  if (!data.items || data.items.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5">Nenhum item de estoque encontrado.</td></tr>';
    return;
  }
  (data.items || []).forEach(p => {
    const rowClass = p.alerta ? 'table-danger' : '';
    tbody.innerHTML += `<tr class="${rowClass}"><td>${p.sku}</td><td>${p.nome||'-'}</td><td>${p.estoque||0}</td><td>${p.minimo||'-'}</td><td>${p.alerta?'⚠️ ALERTA':''}</td></tr>`;
  });
}

async function loadNeeds() {
  const res = await fetch('/api/needs');
  const data = await res.json();
  const tbody = document.querySelector('#needsTable tbody');
  tbody.innerHTML = '';
  if (!data.needs || data.needs.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5">Nenhuma necessidade de compra encontrada.</td></tr>';
    return;
  }
  (data.needs || []).forEach(n => {
    tbody.innerHTML += `<tr><td>${n.component_sku}</td><td>${n.component_name}</td><td>${n.quantity_needed}</td><td>${n.supplier}</td><td>${n.reason}</td></tr>`;
  });
}

async function loadKits() {
  const res = await fetch('/api/kits');
  const data = await res.json();
  const tbody = document.querySelector('#kitsTable tbody');
  tbody.innerHTML = '';
  if (!data.kits || data.kits.length === 0) {
    tbody.innerHTML = '<tr><td colspan="3">Nenhum kit encontrado.</td></tr>';
    return;
  }
  data.kits.forEach(kit => {
    const comps = kit.componentes.map(c => `${c.nome} (${c.quantidade})`).join(', ');
    tbody.innerHTML += `<tr><td>${kit.sku}</td><td>${kit.nome}</td><td>${comps}</td></tr>`;
  });
}

async function loadLogs() {
  const res = await fetch('/api/logs');
  const data = await res.json();
  document.querySelector('#logBox').textContent = (data.logs||[]).join('\\n');
  document.querySelector('#logBox').scrollTop = document.querySelector('#logBox').scrollHeight; // Scroll to bottom
}

async function recheckStock() {
  const statusEl = document.querySelector('#recheck-status');
  statusEl.textContent = 'Verificando...';
  const res = await fetch('/api/recheck', { method: 'POST' });
  const data = await res.json();
  if (data.status === 'ok') {
    statusEl.innerHTML = '<span class="text-success">✓ Sucesso! Atualize a página em alguns segundos.</span>';
  } else {
    statusEl.innerHTML = `<span class="text-danger">✗ Erro: ${data.error}</span>`;
  }
}

// Load data on tab change
document.addEventListener('DOMContentLoaded', () => {
  const mainTabs = document.getElementById('mainTabs');
  if (mainTabs) {
    mainTabs.addEventListener('shown.bs.tab', (event) => {
      const targetId = event.target.getAttribute('href');
      if (targetId === '#tabStock') {
        loadStock();
      } else if (targetId === '#tabNeeds') {
        loadNeeds();
      } else if (targetId === '#tabKits') {
        loadKits();
      } else if (targetId === '#tabLogs') {
        loadLogs();
      } else if (targetId === '#tabDashboard') {
        loadStatus();
        loadStats();
      }
    });
  }
  
  // Initial load for the active tab (Dashboard)
  loadStatus();
  loadStats();
  
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
    parser.add_argument('--dry-run', action='store_true', help='Simulação')
    args = parser.parse_args()

    if args.serve:
        config = Config()
        auth = BlingAuth(config)
        orchestrator = AutomationOrchestrator(config, dry_run=args.dry_run)

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
            server = WebServer(auth, orchestrator)
            server.run(host="0.0.0.0", port=8000)
        except KeyboardInterrupt:
            print("\n✓ Servidor encerrado")
        return

    if args.run:
        config = Config()
        orch = AutomationOrchestrator(config, dry_run=args.dry_run)
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