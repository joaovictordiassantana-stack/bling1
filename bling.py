#!/usr/bin/env python3
\n"""
\nbling.py - Sistema completo de automação Bling com design premium
\nImplementa OAuth 2.0, API robusta, gerenciamento de estoque/compras e dashboard web.
\n"""
\n
\nimport os
\nimport sys
\nimport json
\nimport time
\nimport logging
\nimport logging.handlers
\nimport base64
\nimport argparse
\n
\nfrom pathlib import Path
\nfrom datetime import datetime, timedelta
\nfrom threading import Lock, Thread
\nfrom typing import List, Optional, Dict, Any
\nfrom dataclasses import dataclass, field
\n
\nimport requests
\nfrom requests.exceptions import RequestException
\nfrom flask import Flask, request, render_template_string, jsonify, redirect, url_for
\nfrom flask_sock import Sock
\n
\n
\n
\n
\n# ============================================================================
\n# 0. FUNÇÕES DE PERSISTÊNCIA DE TOKENS (RE-ADICIONADAS)
\n# ============================================================================
\n
\ndef load_tokens():
\n    if not os.path.exists("tokens.json"):
\n        return None
\n    try:
\n        with open("tokens.json", "r", encoding="utf-8") as file:
\n            return json.load(file)
\n    except Exception as e:
\n        print(f"Erro ao carregar tokens: {e}")
\n        return None
\n
\n
\ndef save_tokens(data):
\n    try:
\n        with open("tokens.json", "w", encoding="utf-8") as file:
\n            json.dump(data, file, indent=4, ensure_ascii=False)
\n        print("INFO: Tokens salvos com sucesso.")
\n    except Exception as e:
\n        print(f"Erro ao salvar tokens: {e}")
\n
\n
\n
\ndef is_token_valid(token_data):
\n    if not token_data:
\n        return False
\n    expires_at = token_data.get("expires_at")
\n    if not expires_at:
\n        return False
\n    # Subtrai 20 segundos para garantir que o token não expire durante a requisição
\n    return time.time() < float(expires_at) - 20
\n
\ndef refresh_access_token():
\n    token_data = load_tokens()
\n    if not token_data or "refresh_token" not in token_data:
\n        print("ERRO: refresh_token não encontrado.")
\n        return None
\n
\n    refresh_token = token_data["refresh_token"]
\n
\n    client_id = Config.CLIENT_ID
\n    client_secret = Config.CLIENT_SECRET
\n        
\n    url = "https://www.bling.com.br/Api/v3/oauth/token"
\n    payload = {
\n        "grant_type": "refresh_token",
\n        "refresh_token": refresh_token,
\n        "client_id": client_id,
\n        "client_secret": client_secret
\n    }
\n
\n    try:
\n        response = requests.post(url, data=payload)
\n        new_data = response.json()
\n
\n        new_data["expires_at"] = time.time() + new_data.get("expires_in", 3600)
\n
\n        save_tokens(new_data)
\n
\n        print("Token renovado com sucesso.")
\n        return new_data
\n
\n    except Exception as e:
\n        print("Erro ao renovar token:", e)
\n        return None
\n
\n
\n# ============================================================================
\n# 16. EXCEÇÕES CUSTOMIZADAS
\n# ============================================================================
\n
\nclass BlingAuthError(Exception):
\n    """Erro relacionado à autenticação OAuth do Bling."""
\n    pass
\n
\nclass BlingAPIError(Exception):
\n    """Erro geral na comunicação com a API do Bling."""
\n    pass
\n
\n# ============================================================================
\n# 19. CONFIGURAÇÕES
\n# ============================================================================
\n
\nclass Config:
\n    """Configurações globais da aplicação."""
\n    
\n    # Bling OAuth
\n    CLIENT_ID: str = os.environ.get('BLING_CLIENT_ID', 'YOUR_CLIENT_ID')
\n    CLIENT_SECRET: str = os.environ.get('BLING_CLIENT_SECRET', 'YOUR_CLIENT_SECRET')
\n    REDIRECT_URI: str = os.environ.get('BLING_REDIRECT_URI', 'http://localhost:8000/callback')
\n    
\n    @staticmethod
\n    def validate_credentials():
\n        if Config.CLIENT_ID == 'YOUR_CLIENT_ID' or Config.CLIENT_SECRET == 'YOUR_CLIENT_SECRET':
\n            raise ValueError("As credenciais BLING_CLIENT_ID e BLING_CLIENT_SECRET devem ser configuradas. Verifique as variáveis de ambiente ou a classe Config.")
\n    
\n    # API
\n    BLING_API_URL: str = 'https://www.bling.com.br/Api/v3'
\n    TOKEN_URL: str = 'https://www.bling.com.br/Api/v3/oauth/token'
\n    
\n    # Retry e Timeout
\n    REQUEST_TIMEOUT: int = 30
\n    MAX_RETRIES: int = 3 # Reduzido de 5 para 3, conforme instruído.
\n    BASE_DELAY: float = 1.0 # Delay inicial para backoff exponencial
\n    
\n    # Automação
\n    CHECK_MIN_STOCK: bool = True
\n    MIN_STOCK_THRESHOLD: int = 10 # Estoque mínimo padrão se não configurado
\n    DEFAULT_BATCH_SIZE: int = 10
\n    DELAY_BETWEEN_BATCHES: float = 0.5 # Delay entre chamadas de API em lote
\n    
\n    # Arquivos
\n    TOKENS_FILE: Path = Path('tokens.json')
\n    COMPONENT_CONFIG_FILE: Path = Path('component_config.json')
\n    LOGS_DIR: Path = Path('logs')
\n    LOG_FILE: Path = LOGS_DIR / 'automacao_bling.log'
\n    ERROR_LOG_FILE: Path = LOGS_DIR / 'errors.log'
\n
\n# ============================================================================
\n# 2. DATACLASSES E ESTRUTURAS
\n# ============================================================================
\n
\n@dataclass
\nclass Component:
\n    """Representa um componente (produto) no Bling."""
\n    sku: str
\n    name: str
\n    qty: int # Quantidade necessária para o Kit
\n    supplier: str = 'N/A'
\n    lead_time_days: int = 0
\n    unit_cost: float = 0.0
\n    min_stock: int = Config.MIN_STOCK_THRESHOLD
\n    current_stock: int = 0
\n    
\n    def __post_init__(self):
\n        # Garante que min_stock seja pelo menos 0
\n        self.min_stock = max(0, self.min_stock)
\n
\n@dataclass
\nclass Kit:
\n    """Representa um Kit (produto composto) no Bling."""
\n    sku: str
\n    name: str
\n    components: List[Component] = field(default_factory=list)
\n    price: float = 0.0
\n
\n@dataclass
\nclass PurchaseNeed:
\n    """Representa uma necessidade de compra de um componente."""
\n    component_sku: str
\n    component_name: str
\n    quantity_needed: int
\n    supplier: str
\n    lead_time_days: int
\n    reason: str
\n
\n# ============================================================================
\n# 9. LOGS AVANÇADOS
\n# ============================================================================
\n
\nclass InMemoryLogHandler(logging.Handler):
\n    """Handler de log que armazena os registros em memória."""
\n    def __init__(self, max_logs=500):
\n        super().__init__()
\n        self.logs = []
\n        self.max_logs = max_logs
\n
\n        self.formatter = logging.Formatter(
\n            '%(asctime)s - %(levelname)s - %(message)s',
\n            datefmt='%Y-%m-%dT%H:%M:%S'
\n        )
\n        
\n    def emit(self, record):
\n
\n        log_entry = {
\n        'timestamp': self.formatter.formatTime(record),
\n        'level': record.levelname,
\n        'message': self.format(record),
\n        'name': record.name
\n        }
\n        self.logs.append(log_entry)
\n        if len(self.logs) > self.max_logs:
\n            self.logs.pop(0)
\n    
\n    def get_logs(self, limit: Optional[int] = None) -> List[Dict[str, str]]:
\n        """Retorna os logs armazenados, limitados pelo parâmetro."""
\n
\n        if limit:
\n            return self.logs[-limit:]
\n        return self.logs.copy()
\n
\n# Configuração inicial de logs
\ndef setup_logging():
\n    """Configura o sistema de logging com handlers de arquivo e memória."""
\n    Config.LOGS_DIR.mkdir(exist_ok=True)
\n    
\n    global memory_handler
\n    memory_handler = InMemoryLogHandler()
\n    
\n    # Logger principal
\n    logger = logging.getLogger('bling_automacao')
\n    logger.setLevel(logging.INFO)
\n    
\n    # Handler de arquivo principal
\n    file_handler = logging.handlers.RotatingFileHandler(
\n        Config.LOG_FILE, maxBytes=1024*1024*5, backupCount=5, encoding='utf-8'
\n    )
\n    file_handler.setFormatter(logging.Formatter(
\n        '%(asctime)s - %(levelname)s - %(message)s',
\n        datefmt='%Y-%m-%dT%H:%M:%S'
\n    ))
\n    
\n    # Handler de erro separado
\n    error_logger = logging.getLogger('error_logger')
\n    error_logger.setLevel(logging.ERROR)
\n    error_file_handler = logging.handlers.RotatingFileHandler(
\n        Config.ERROR_LOG_FILE, maxBytes=1024*1024*5, backupCount=5, encoding='utf-8'
\n    )
\n    error_file_handler.setFormatter(logging.Formatter(
\n        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
\n        datefmt='%Y-%m-%dT%H:%M:%S'
\n    ))
\n    error_logger.addHandler(error_file_handler)
\n    
\n    # Adiciona handlers ao logger principal
\n    logger.addHandler(file_handler)
\n    logger.addHandler(memory_handler)
\n    
\n    # Adiciona handler de console para CLI
\n    if not os.environ.get('FLASK_ENV'): # Não adiciona console handler se estiver rodando em ambiente Flask/WSGI
\n        console_handler = logging.StreamHandler(sys.stdout)
\n        console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
\n        logger.addHandler(console_handler)
\n        
\n    return logger, error_logger
\n
\nlogger, error_logger = setup_logging()
\n
\n# ============================================================================
\n# 3. CONFIGURAÇÃO DE COMPONENTES
\n# ============================================================================
\n
\nclass ComponentConfigManager:
\n    """Gerencia as configurações locais de componentes (min_stock, fornecedor, etc)."""
\n    
\n    def __init__(self, file_path: Path):
\n        self.file_path = file_path
\n        self.config: Dict[str, Any] = self._load_or_create_config()
\n        self.defaults: Dict[str, Any] = self.config.get('component_defaults', {})
\n        self.components_map: Dict[str, Dict[str, Any]] = {
\n            c['sku']: c for c in self.config.get('components', [])
\n        }
\n        
\n    def _load_or_create_config(self) -> Dict[str, Any]:
\n        """Carrega a configuração do arquivo ou cria um novo com valores padrão."""
\n        if self.file_path.exists():
\n            try:
\n                with open(self.file_path, 'r', encoding='utf-8') as f:
\n                    return json.load(f)
\n            except (json.JSONDecodeError, IOError) as e:
\n                logger.error(f"Erro ao carregar {self.file_path}: {e}. Criando arquivo padrão.")
\n                error_logger.error(f"Erro ao carregar {self.file_path}: {e}")
\n        
\n        default_config = {
\n            "component_defaults": {
\n                "supplier": "Fornecedor Padrão",
\n                "lead_time_days": 7,
\n                "min_stock": Config.MIN_STOCK_THRESHOLD
\n            },
\n            "components": []
\n        }
\n        self._save_config(default_config)
\n        return default_config
\n
\n    def _save_config(self, data: Dict[str, Any]):
\n        """Salva a configuração no arquivo."""
\n        try:
\n            with open(self.file_path, 'w', encoding='utf-8') as f:
\n                json.dump(data, f, indent=2, ensure_ascii=False)
\n        except IOError as e:
\n            logger.error(f"Erro ao salvar {self.file_path}: {e}")
\n            error_logger.error(f"Erro ao salvar {self.file_path}: {e}")
\n
\n    def apply_config_to_component(self, component: Component) -> Component:
\n        """Aplica as configurações locais (defaults e específicas) a um Component."""
\n        
\n        # 1. Aplica defaults
\n        component.supplier = self.defaults.get('supplier', component.supplier)
\n        component.lead_time_days = self.defaults.get('lead_time_days', component.lead_time_days)
\n        component.min_stock = self.defaults.get('min_stock', component.min_stock)
\n        
\n        # 2. Sobrescreve com configurações específicas do SKU
\n        sku_config = self.components_map.get(component.sku)
\n        if sku_config:
\n            component.supplier = sku_config.get('supplier', component.supplier)
\n            component.lead_time_days = sku_config.get('lead_time_days', component.lead_time_days)
\n            component.min_stock = sku_config.get('min_stock', component.min_stock)
\n            component.unit_cost = sku_config.get('unit_cost', component.unit_cost)
\n            
\n        return component
\n
\n# ============================================================================
\n# 1. AUTENTICAÇÃO OAUTH 2.0
\n# ============================================================================
\n
\nclass BlingAuth:
\n    """Gerencia o fluxo OAuth 2.0 e a persistência de tokens."""
\n    
\n    def __init__(self, config: Config):
\n        self.config = config
\n        self.token_url = config.TOKEN_URL
\n        self.access_token: Optional[str] = None
\n        self.refresh_token: Optional[str] = None
\n        self.expires_at: Optional[float] = None # Timestamp UNIX (float)
\n        self.lock = Lock() # Re-adicionado para garantir thread safety na manipulação de tokens.
\n        
\n    def _save_tokens(self):
\n        """Persiste os tokens e a data de expiração no arquivo tokens.json de forma atômica."""
\n        with self.lock:
\n            # A escrita é feita para um arquivo temporário e depois renomeada para garantir atomicidade.
\n            data = {
\n            'access_token': self.access_token,
\n            'refresh_token': self.refresh_token,
\n            'expires_at': self.expires_at if self.expires_at else None
\n        }
\n        
\n        temp_file = self.config.TOKENS_FILE.with_suffix('.tmp')
\n        
\n        try:
\n            with open(temp_file, 'w', encoding='utf-8') as f:
\n                json.dump(data, f, indent=2)
\n            
\n            # Renomeia o arquivo temporário para o arquivo final (operação atômica)
\n            temp_file.rename(self.config.TOKENS_FILE)
\n            logger.info("Tokens salvos com sucesso.")
\n        except IOError as e:
\n            logger.error(f"Erro ao salvar tokens: {e}")
\n            error_logger.error(f"Erro ao salvar tokens: {e}")
\n
\n    def load_tokens(self) -> bool:
\n        """Carrega os tokens do arquivo tokens.json."""
\n        with self.lock:
\n            if self.config.TOKENS_FILE.exists():
\n                try:
\n                    with open(self.config.TOKENS_FILE, 'r', encoding='utf-8') as f:
\n                        data = json.load(f)
\n                        self.access_token = data.get('access_token')
\n                        self.refresh_token = data.get('refresh_token')
\n                        expires_at_val = data.get('expires_at')
\n                        
\n                        if not self.refresh_token:
\n                            logger.warning("Arquivo tokens.json incompleto (refresh_token ausente). Necessário reautenticar.")
\n                            self.access_token = None
\n                            self.expires_at = None
\n                            return False
\n                            
\n                        if expires_at_val:
\n                            # Garante que expires_at seja um float (timestamp UNIX)
\n                            try:
\n                                self.expires_at = float(expires_at_val)
\n                            except (ValueError, TypeError):
\n                                logger.error(f"Valor inválido para expires_at: {expires_at_val}. Tratando como expirado.")
\n                                self.expires_at = 0.0
\n                        
\n                        if self.access_token:
\n                            logger.info("Tokens carregados com sucesso.")
\n                            return True
\n                except (json.JSONDecodeError, IOError) as e:
\n                    logger.error(f"Erro ao carregar tokens: {e}")
\n                    error_logger.error(f"Erro ao carregar tokens: {e}")
\n        
\n        logger.warning("Tokens não encontrados ou inválidos. Necessário autenticar.")
\n        return False
\n
\n    def is_token_valid(self) -> bool:
\n        """Verifica se o token de acesso é válido e não expirou (com margem de 5 minutos)."""
\n
\n        if not self.access_token or not self.expires_at:
\n            return False
\n        # Verifica se o token expira nos próximos 5 minutos (300 segundos)
\n        return self.expires_at > time.time() + 300
\n
\n    def get_authorization_url(self) -> str:
\n        """Retorna a URL para iniciar o fluxo de autorização OAuth."""
\n        return (
\n            f"https://www.bling.com.br/Api/v3/oauth/authorize?"
\n            f"response_type=code&"
\n            f"client_id={self.config.CLIENT_ID}&"
\n            f"state=random_string_for_security&"
\n            f"redirect_uri={self.config.REDIRECT_URI}"
\n        )
\n
\n    def _get_basic_auth_header(self) -> Dict[str, str]:
\n        """Gera o cabeçalho Basic Authentication para o token endpoint."""
\n        auth_string = f"{self.config.CLIENT_ID}:{self.config.CLIENT_SECRET}"
\n        encoded_auth = base64.b64encode(auth_string.encode()).decode()
\n        return {"Authorization": f"Basic {encoded_auth}"}
\n
\n    def exchange_code_for_token(self, code: str):
\n        """Troca o código de autorização por tokens de acesso e refresh."""
\n        logger.info("Trocando código de autorização por tokens...")
\n        
\n        payload = {
\n            'grant_type': 'authorization_code',
\n            'code': code,
\n            'redirect_uri': self.config.REDIRECT_URI # O Bling V3 exige o redirect_uri no payload
\n        }
\n        
\n        try:
\n            response = requests.post(
\n                self.token_url,
\n                data=payload,
\n                headers=self._get_basic_auth_header(),
\n                timeout=self.config.REQUEST_TIMEOUT
\n            )
\n            response.raise_for_status()
\n            data = response.json()
\n            
\n            self.access_token = data['access_token']
\n            self.refresh_token = data['refresh_token']
\n            # O Bling retorna expires_in em segundos (padrão 3600s = 1h)
\n            expires_in = data.get('expires_in', 3600)
\n            self.expires_at = time.time() + expires_in
\n            self._save_tokens()
\n            
\n            logger.info("Autenticação OAuth concluída com sucesso!")
\n            return True
\n            
\n        except RequestException as e:
\n            msg = f"Erro ao trocar código por token: {e}"
\n            logger.error(msg)
\n            logger.error(msg)
\n            error_logger.error(msg)
\n            raise BlingAuthError(msg) from e
\n
\n    def refresh_access_token(self):
\n        """Renova o token de acesso usando o refresh token."""
\n        logger.info("Tentando renovar o token de acesso...")
\n        
\n        if not self.refresh_token:
\n            raise BlingAuthError("Refresh token não disponível. Necessário reautenticar.")
\n            
\n        payload = {
\n            'grant_type': 'refresh_token',
\n            'refresh_token': self.refresh_token,
\n            'redirect_uri': self.config.REDIRECT_URI # O Bling V3 exige o redirect_uri no payload
\n        }
\n        
\n        try:
\n            response = requests.post(
\n                self.token_url,
\n                data=payload,
\n                headers=self._get_basic_auth_header(),
\n                timeout=self.config.REQUEST_TIMEOUT
\n            )
\n            response.raise_for_status()
\n            data = response.json()
\n            
\n            self.access_token = data['access_token']
\n            # O refresh token pode mudar, então atualizamos
\n            self.refresh_token = data.get('refresh_token', self.refresh_token)
\n            expires_in = data.get('expires_in', 3600)
\n            self.expires_at = time.time() + expires_in
\n            self._save_tokens()
\n            
\n            logger.info("Token de acesso renovado com sucesso!")
\n            return True
\n            
\n        except RequestException as e:
\n            msg = f"Erro ao renovar token: {e}. Necessário reautenticar."
\n            logger.error(msg)
\n            logger.error(msg)
\n            error_logger.error(msg)
\n            raise BlingAuthError(msg) from e
\n
\n# ============================================================================
\n
\ndef get_bling_product_by_sku(sku):
\n    token_data = load_tokens()
\n
\n    # Se o token estiver expirado → renova automaticamente
\n    if not is_token_valid(token_data):
\n        token_data = refresh_access_token()
\n
\n    access_token = token_data["access_token"]
\n
\n    url = f"https://www.bling.com.br/Api/v3/produtos?codigo={sku}"
\n
\n    headers = {
\n        "Authorization": f"Bearer {access_token}",
\n        "Accept": "application/json"
\n    }
\n
\n    response = requests.get(url, headers=headers)
\n
\n    try:
\n        return response.json()
\n    except Exception:
\n        return {"error": "Falha ao interpretar resposta do Bling", "raw": response.text}
\n
\n# ============================================================================
\n# 4. CLASSE BlingAPI COMPLETA
\n# ============================================================================
\n
\nclass BlingAPI:
\n    """Cliente robusto para a API do Bling, com retry e renovação de token."""
\n    
\n    def __init__(self, auth: BlingAuth, config: Config):
\n        self.auth = auth
\n        self.config = config
\n        self.base_url = config.BLING_API_URL
\n        self._stock_cache: Dict[int, Dict[str, Any]] = {} # {product_id: {'stock': int, 'expiry': datetime}
\n        self._cache_ttl = timedelta(minutes=5)
\n        
\n    def _request_with_retry(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
\n        """
\n        Executa uma requisição HTTP com retry e backoff exponencial.
\n        Trata erros 401 com renovação automática de token.
\n        """
\n        url = f"{self.base_url}/{endpoint}"
\n        
\n        for attempt in range(self.config.MAX_RETRIES):
\n            try:
\n                # 1. Verifica e renova o token se necessário
\n                if not self.auth.access_token:
\n                    if not self.auth.refresh_token:
\n                        raise BlingAuthError("Aplicação não autorizada. Acesse /auth para configurar.")
\n                    # Se tiver refresh token, tenta renovar antes de prosseguir
\n                    self.auth.refresh_access_token()
\n                
\n                # 2. Adiciona cabeçalhos de autorização
\n                headers = kwargs.pop('headers', {})
\n                headers['Authorization'] = f'Bearer {self.auth.access_token}'
\n                headers['Accept'] = 'application/json'
\n                kwargs['headers'] = headers
\n                
\n                # 3. Executa a requisição
\n                response = requests.request(
\n                    method, 
\n                    url, 
\n                    timeout=self.config.REQUEST_TIMEOUT, 
\n                    **kwargs
\n                )
\n                
\n                # 4. Trata status codes
\n                if response.status_code == 200 or response.status_code == 201:
\n                    return response.json()
\n                
\n                # 5. Trata 401 (Não Autorizado) - Força a renovação e tenta novamente
\n                if response.status_code == 401:
\n                    logger.warning("Token expirado ou inválido (401). Tentando renovar...")
\n                    self.auth.refresh_access_token()
\n                    # Força a próxima iteração do loop com o novo token
\n                    continue 
\n                
\n                # 6. Trata outros erros da API
\n                response.raise_for_status()
\n                
\n            except BlingAuthError:
\n                # Se a renovação falhar, o erro é fatal
\n                raise
\n            except RequestException as e:
\n                # Trata erros de conexão, timeout, etc.
\n                if attempt < self.config.MAX_RETRIES - 1:
\n                    delay = self.config.BASE_DELAY * (2 ** attempt)
\n                    logger.warning(f"Tentativa {attempt + 1} falhou. Erro: {e}. Tentando novamente em {delay:.2f}s...")
\n                    time.sleep(delay)
\n                else:
\n                    msg = f"Falha na requisição após {self.config.MAX_RETRIES} tentativas para {url}: {e}"
\n                    error_logger.error(msg)
\n                    raise BlingAPIError(msg) from e
\n            except Exception as e:
\n                msg = f"Erro inesperado na requisição para {url}: {e}"
\n                error_logger.error(msg)
\n                raise BlingAPIError(msg) from e
\n                
\n        # Se o loop terminar sem sucesso (o que não deve acontecer se o 401 for tratado)
\n        raise BlingAPIError(f"Falha desconhecida na requisição para {url}")
\n
\n    def get_product_by_sku(self, sku: str) -> Optional[Dict[str, Any]]:
\n        """Busca um produto pelo SKU."""
\n        try:
\n            response = self._request_with_retry(
\n                'GET', 
\n                'produtos', 
\n                params={'codigo': sku}
\n            )
\n            # A API retorna uma lista, pegamos o primeiro
\n            return response.get('data', [{}])[0].get('produto')
\n        except BlingAPIError as e:
\n            logger.error(f"Erro ao buscar produto SKU {sku}: {e}")
\n            return None
\n
\n    def get_product_stock(self, product_id: int) -> int:
\n        """Busca o estoque atual de um produto pelo ID, usando cache com TTL de 5 minutos."""
\n        
\n        # 1. Verifica o cache
\n        if product_id in self._stock_cache:
\n            cache_entry = self._stock_cache[product_id]
\n            if datetime.now() < cache_entry['expiry']:
\n                return cache_entry['stock']
\n            # Cache expirado, remove
\n            del self._stock_cache[product_id]
\n            
\n        # 2. Busca na API
\n        try:
\n            response = self._request_with_retry(
\n                'GET', 
\n                f'estoques/produtos/{product_id}'
\n            )
\n            # A API retorna o estoque em um formato específico
\n            stock = int(response.get('data', {}).get('estoque', {}).get('estoqueAtual', 0))
\n            
\n            # 3. Atualiza o cache
\n            self._stock_cache[product_id] = {
\n                'stock': stock,
\n                'expiry': datetime.now() + self._cache_ttl
\n            }
\n            
\n            return stock
\n        except BlingAPIError as e:
\n            logger.error(f"Erro ao buscar estoque do produto ID {product_id}: {e}")
\n            return 0
\n
\n    def get_all_kits_and_components(self, config_manager: ComponentConfigManager) -> List[Kit]:
\n        """Busca todos os Kits e seus Componentes, aplicando configurações locais."""
\n        logger.info("Buscando todos os Kits e Componentes no Bling...")
\n        kits: List[Kit] = []
\n        pagina = 1
\n        MAX_PAGES = 100 # Limite de segurança para evitar loop infinito em caso de bug na API
\n        
\n        while pagina <= MAX_PAGES:
\n            try:
\n                response = self._request_with_retry(
\n                    'GET', 
\n                    'produtos', 
\n                    params={'tipo': 'P', 'pagina': pagina}
\n                )
\n                
\n                data = response.get('data', [])
\n                if not data:
\n                    break # Fim da paginação
\n                
\n                pagina += 1 # Incrementa a página para a próxima iteração
\n                
\n                for item in data:
\n                    product = item.get('produto', {})
\n                    if product.get('tipo') == 'P' and product.get('estrutura'):
\n                        
\n                        componentes: List[Component] = []
\n                        for comp_item in product['estrutura'].get('componentes', []):
\n                            comp_data = comp_item.get('produto', {})
\n                            
\n                            # 1. Cria o objeto Component
\n                            component = Component(
\n                                sku=comp_data.get('codigo', 'N/A'),
\n                                name=comp_data.get('descricao', 'Sem nome'),
\n                                qty=int(comp_item.get('quantidade', 0)),
\n                                unit_cost=float(comp_data.get('precoCusto', 0.0))
\n                            )
\n                            
\n                            # 2. Aplica configurações locais (fornecedor, min_stock, lead_time)
\n                            component = config_manager.apply_config_to_component(component)
\n                            
\n                            # 3. Busca estoque atual (requer o ID do produto)
\n                            product_id = comp_data.get('id')
\n                            if product_id:
\n                                component.current_stock = self.get_product_stock(product_id)
\n                            
\n                            componentes.append(component)
\n                            
\n                        kits.append(Kit(
\n                            sku=product.get('codigo', 'N/A'),
\n                            name=product.get('descricao', 'Sem nome'),
\n                            components=componentes,
\n                            price=float(product.get('preco', 0.0))
\n                        ))
\n                
\n                pagina += 1
\n                time.sleep(self.config.DELAY_BETWEEN_BATCHES) # Delay entre batches
\n                
\n            except BlingAPIError as e:
\n                logger.error(f"Erro na paginação de Kits: {e}")
\n                break
\n                
\n        logger.info(f"Busca de Kits concluída. {len(kits)} Kits encontrados.")
\n        return kits
\n
\n    def get_supplier_by_name(self, name: str) -> Optional[Dict[str, Any]]:
\n        """Busca um fornecedor pelo nome."""
\n        try:
\n            response = self._request_with_retry(
\n                'GET', 
\n                'fornecedores', 
\n                params={'pesquisa': name}
\n            )
\n            # A API retorna uma lista, tentamos encontrar uma correspondência exata
\n            for item in response.get('data', []):
\n                supplier = item.get('fornecedor', {})
\n                if supplier.get('nome') == name:
\n                    return supplier
\n            return None
\n        except BlingAPIError as e:
\n            logger.error(f"Erro ao buscar fornecedor {name}: {e}")
\n            return None
\n
\n    def create_production_order(self, kit_sku: str, quantity: int) -> Optional[int]:
\n        """Cria uma Ordem de Produção (OP) no Bling."""
\n        logger.info(f"Criando OP para Kit {kit_sku} (Qtd: {quantity})...")
\n        
\n        payload = {
\n            "data": {
\n                "produto": {
\n                    "codigo": kit_sku
\n                },
\n                "quantidade": quantity
\n            }
\n        }
\n        
\n        try:
\n            response = self._request_with_retry(
\n                'POST', 
\n                'producao/ordens', 
\n                json=payload
\n            )
\n            op_id = response.get('data', {}).get('id')
\n            if op_id:
\n                logger.info(f"OP criada com sucesso! ID: {op_id} para Kit {kit_sku} (Qtd: {quantity})")
\n                return op_id
\n            else:
\n                raise BlingAPIError(f"Resposta da API não contém ID da OP: {response}")
\n        except BlingAPIError as e:
\n            logger.error(f"Falha ao criar OP para Kit {kit_sku}: {e}")
\n            return None
\n
\n    def create_purchase_order(self, supplier_name: str, items: List[PurchaseNeed]) -> Optional[int]:
\n        """Cria uma Ordem de Compra (PO) no Bling."""
\n        logger.info(f"Criando PO para Fornecedor {supplier_name} com {len(items)} itens...")
\n        
\n        supplier = self.get_supplier_by_name(supplier_name)
\n        if not supplier:
\n            logger.error(f"Fornecedor '{supplier_name}' não encontrado no Bling. PO não criada.")
\n            return None
\n            
\n        supplier_id = supplier['id']
\n        
\n        payload = {
\n            "data": {
\n                "fornecedor": {
\n                    "id": supplier_id
\n                },
\n                "itens": [
\n                    {
\n                        "produto": {
\n                            "codigo": item.component_sku
\n                        },
\n                        "quantidade": item.quantity_needed,
\n                        "observacoes": f"Motivo: {item.reason}"
\n                    }
\n                    for item in items
\n                ]
\n            }
\n        }
\n        
\n        try:
\n            response = self._request_with_retry(
\n                'POST', 
\n                'compras/pedidos', 
\n                json=payload
\n            )
\n            po_id = response.get('data', {}).get('id')
\n            if po_id:
\n                logger.info(f"PO criada com sucesso! ID: {po_id} para {supplier_name} com {len(items)} itens.")
\n                return po_id
\n            else:
\n                raise BlingAPIError(f"Resposta da API não contém ID da PO: {response}")
\n        except BlingAPIError as e:
\n            logger.error(f"Falha ao criar PO para {supplier_name}: {e}")
\n            return None
\n
\n# ============================================================================
\n# 5. SISTEMA DE ESTATÍSTICAS
\n# ============================================================================
\n
\nclass StatisticsManager:
\n    """Gerencia e coleta estatísticas de execução da automação."""
\n    
\n    def __init__(self):
\n        self.lock = Lock()
\n
\n        self.reset()
\n        
\n    def reset(self):
\n        """Reseta todas as estatísticas."""
\n        with self.lock:
\n            self.success: int = 0
\n            self.failed: int = 0
\n            self.ops_created: int = 0
\n            self.pos_created: int = 0
\n            self.min_stock_checks: int = 0
\n            self.start_time: Optional[datetime] = None
\n            self.end_time: Optional[datetime] = None
\n            
\n    def start(self):
\n        """Inicia a contagem de tempo."""
\n        with self.lock:
\n            self.start_time = datetime.now()
\n            self.end_time = None
\n            
\n    def stop(self):
\n        """Para a contagem de tempo."""
\n        with self.lock:
\n            self.end_time = datetime.now()
\n            
\n    def increment(self, counter: str, value: int = 1):
\n        """Incrementa um contador específico."""
\n        with self.lock:
\n            if hasattr(self, counter):
\n                setattr(self, counter, getattr(self, counter) + value)
\n            
\n    @property
\n    def elapsed_time_seconds(self) -> float:
\n        """Calcula o tempo decorrido em segundos."""
\n        # Não precisa de lock aqui, pois é uma propriedade que só lê
\n        # e é chamada dentro de to_dict, que já tem o lock.
\n        if self.start_time:
\n            end = self.end_time if self.end_time else datetime.now()
\n            return (end - self.start_time).total_seconds()
\n        return 0.0
\n
\n    def to_dict(self) -> Dict[str, Any]:
\n        """Retorna as estatísticas em formato de dicionário."""
\n        with self.lock:
\n            return {
\n                'success': self.success,
\n                'failed': self.failed,
\n                'ops_created': self.ops_created,
\n                'pos_created': self.pos_created,
\n                'min_stock_checks': self.min_stock_checks,
\n                'elapsed_time_seconds': round(self.elapsed_time_seconds, 2),
\n                'total_processed': self.success + self.failed
\n            }
\n
\n# ============================================================================
\n# 6. GESTÃO DE COMPRAS (PO)
\n# ============================================================================
\n
\nclass NeedsManager:
\n    """Gerencia as necessidades de compra e a criação de Ordens de Compra (POs)."""
\n    
\n    def __init__(self, api: BlingAPI, stats: StatisticsManager):
\n        self.api = api
\n        self.stats = stats
\n        # needs: Dict[supplier_name, List[PurchaseNeed]]
\n        self.needs: Dict[str, List[PurchaseNeed]] = {}
\n        self.lock = Lock()
\n
\n    def reset(self):
\n        """Limpa todas as necessidades de compra."""
\n        with self.lock:
\n            self.needs = {}
\n
\n    def add_need(self, component: Component, quantity: int, reason: str):
\n        """Adiciona uma necessidade de compra."""
\n        with self.lock:
\n            if quantity <= 0:
\n                return
\n                
\n            need = PurchaseNeed(
\n                component_sku=component.sku,
\n                component_name=component.name,
\n                quantity_needed=quantity,
\n                supplier=component.supplier,
\n                lead_time_days=component.lead_time_days,
\n                reason=reason
\n            )
\n            
\n            if need.supplier not in self.needs:
\n                self.needs[need.supplier] = []
\n            self.needs[need.supplier].append(need)
\n            logger.info(f"Necessidade adicionada: {need.component_name} ({need.quantity_needed} un.) para {need.supplier}")
\n
\n    def check_min_stock_needs(self, components: List[Component]):
\n        """Verifica o estoque mínimo de uma lista de componentes e adiciona necessidades."""
\n        logger.info("Verificação de Estoque Mínimo")
\n        
\n        for component in components:
\n            self.stats.increment('min_stock_checks')
\n            
\n            if component.current_stock < component.min_stock:
\n                quantity_needed = component.min_stock - component.current_stock
\n                self.add_need(
\n                    component, 
\n                    quantity_needed, 
\n                    f"Estoque atual ({component.current_stock}) abaixo do mínimo ({component.min_stock})"
\n                )
\n                logger.warning(f"ALERTA: {component.name} ({component.sku}) precisa de {quantity_needed} un.")
\n            else:
\n                logger.debug(f"Estoque OK: {component.name} ({component.sku}) - {component.current_stock}/{component.min_stock}")
\n
\n    def generate_purchase_orders(self) -> List[int]:
\n        """Gera Ordens de Compra (POs) no Bling, agrupando por fornecedor."""
\n        with self.lock:
\n            logger.info("Geração de Ordens de Compra (POs)")
\n            
\n            if not self.needs:
\n                logger.info("Nenhuma necessidade de compra pendente.")
\n                return []
\n            
\n            po_ids: List[int] = []
\n            needs_to_process = self.needs.copy()
\n            self.needs = {} # Limpa as necessidades após copiar para processamento
\n            
\n            for supplier_name, items in needs_to_process.items():
\n                po_id = self.api.create_purchase_order(supplier_name, items)
\n                if po_id:
\n                    po_ids.append(po_id)
\n                    self.stats.increment('pos_created')
\n                
\n        logger.info(f"Geração de POs concluída. {len(po_ids)} PO(s) criada(s).")
\n        return po_ids
\n
\n# ============================================================================
\n# 7. ORQUESTRADOR DE AUTOMAÇÃO
\n# ============================================================================
\n
\nclass AutomationOrchestrator:
\n    """Orquestra o fluxo de automação: OPs, verificação de estoque e POs."""
\n    
\n    def __init__(self, api: BlingAPI, stats: StatisticsManager, needs_manager: NeedsManager, config_manager: ComponentConfigManager, auth: BlingAuth):
\n        self.api = api
\n        self.stats = stats
\n        self.needs_manager = needs_manager
\n        self.config_manager = config_manager
\n        self.auth = auth
\n        self.kits: List[Kit] = []
\n        self.failed_items: List[Dict[str, Any]] = []
\n        self.is_running: bool = False
\n
\n        
\n    def load_data(self):
\n        """Carrega todos os kits e componentes do Bling."""
\n        logger.info("Carregamento Inicial de Dados")
\n        try:
\n            self.kits = self.api.get_all_kits_and_components(self.config_manager)
\n            self.run_purchase_check(force_po_creation=False) # Verifica estoque inicial
\n            logger.info("Dados carregados e verificação inicial de estoque concluída.")
\n            return True
\n        except BlingAuthError:
\n            logger.error("Falha na autenticação. Não foi possível carregar os dados.")
\n            return False
\n        except BlingAPIError as e:
\n            logger.error(f"Falha ao carregar dados da API: {e}")
\n            return False
\n
\n    def process_kits(self, kits_to_process: List[Kit], batch_size: int, check_stock: bool = True, quantity: int = 1) -> Dict[str, Any]:
\n        """Processa uma lista de kits: cria OPs e verifica estoque de componentes. Implementa processamento em lotes."""
\n        
\n        if batch_size <= 0:
\n            batch_size = 1
\n        if self.is_running:
\n            logger.warning("Processamento já em andamento. Ignorando nova requisição.")
\n            return {"status": "warning", "message": "Processamento já em andamento."}
\n        
\n        self.is_running = True
\n        self.stats.reset()
\n        self.needs_manager.reset()
\n        self.failed_items = []
\n        self.stats.start()
\n            
\n        logger.info(f"Iniciando Processamento de {len(kits_to_process)} Kits em Lotes de {batch_size}")
\n        
\n        try:
\n            for i, kit in enumerate(kits_to_process):
\n                op_id = self.api.create_production_order(kit.sku, quantity)
\n                
\n                if op_id:
\n                    self.stats.increment('ops_created')
\n                    self.stats.increment('success')
\n                    
\n                    if check_stock:
\n                        # Verifica o estoque de todos os componentes do kit
\n                        self.needs_manager.check_min_stock_needs(kit.components)
\n                else:
\n                    self.stats.increment('failed')
\n                    self.failed_items.append({
\n                        "sku": kit.sku,
\n                        "name": kit.name,
\n                        "reason": "Falha ao criar Ordem de Produção"
\n                    })
\n                
\n                # Otimização de Rate Limiting: Pausa após cada item para respeitar o limite de requisições.
\n                # O delay é distribuído pelo tamanho do lote para evitar sleeps longos e desnecessários.
\n                if self.config.DELAY_BETWEEN_BATCHES > 0:
\n                    delay_per_item = self.config.DELAY_BETWEEN_BATCHES / batch_size
\n                    time.sleep(delay_per_item)
\n                    
\n            # Após processar todos os kits, gera as POs
\n            self.needs_manager.generate_purchase_orders()
\n            
\n        except BlingAPIError as e:
\n            msg = f"Erro de API durante o processamento de kits: {e}"
\n            logger.error(msg)
\n            error_logger.error(msg)
\n        except Exception as e:
\n            msg = f"Erro inesperado durante o processamento de kits: {e}"
\n            logger.error(msg)
\n            error_logger.error(msg)
\n        finally:
\n            self.stats.stop()
\n            self.is_running = False
\n            
\n        return {"status": "success", "stats": self.stats.to_dict()}
\n
\n    def run_purchase_check(self, force_po_creation: bool = True):
\n        """Executa apenas a verificação de estoque e, opcionalmente, a criação de POs."""
\n        if self.is_running:
\n            logger.warning("Processamento já em andamento. Ignorando nova requisição.")
\n            return {"status": "warning", "message": "Processamento já em andamento."}
\n        
\n        self.is_running = True
\n        self.needs_manager.reset()
\n        self.stats.start()
\n            
\n        logger.info("Iniciando Verificação de Estoque e Compras")
\n        
\n        try:
\n            # 1. Coleta todos os componentes únicos de todos os kits
\n            all_components: Dict[str, Component] = {}
\n            for kit in self.kits:
\n                for component in kit.components:
\n                    all_components[component.sku] = component
\n            
\n            # 2. Verifica o estoque mínimo de todos os componentes
\n            self.needs_manager.check_min_stock_needs(list(all_components.values()))
\n            
\n            # 3. Gera as POs se forçado
\n            if force_po_creation:
\n                self.needs_manager.generate_purchase_orders()
\n                
\n        except BlingAPIError as e:
\n            msg = f"Erro de API durante a verificação de estoque: {e}"
\n            logger.error(msg)
\n            error_logger.error(msg)
\n        except Exception as e:
\n            msg = f"Erro inesperado durante a verificação de estoque: {e}"
\n            logger.error(msg)
\n            error_logger.error(msg)
\n        finally:
\n            self.stats.stop()
\n            self.is_running = False
\n            
\n        logger.info("Verificação Concluída")
\n        return {"status": "success", "stats": self.stats.to_dict()}
\n
\n# ============================================================================
\n# INSTÂNCIAS GLOBAIS
\n# ============================================================================
\n
\n# 21. ESTRUTURA DE ARQUIVOS (Garantida pelo setup_logging e ComponentConfigManager)
\n# 19# 1. CONFIGURAÇÕES (Instância)
\nconfig = Config()
\nconfig.validate_credentials() # Chamada de validação adicionada
\n
\n# 2. AUTENTICAÇÃO (Instância)
\nauth = BlingAuth(config)
\n
\n# 3. API (Instância)
\napi = BlingAPI(auth, config) # Ordem corrigida (auth, config)
\n
\n# 4. CONFIGURAÇÃO DE COMPONENTES (Instância)
\nconfig_manager = ComponentConfigManager(config.COMPONENT_CONFIG_FILE)
\n
\n# 5. SISTEMA DE ESTATÍSTICAS (Instância)
\nstats_manager = StatisticsManager()
\n
\n# 6. GESTÃO DE COMPRAS (Instância)
\nneeds_manager = NeedsManager(api, stats_manager)
\n
\n# 7. ORQUESTRADOR DE AUTOMAÇÃO (Instância)
\norchestrator = AutomationOrchestrator(api, stats_manager, needs_manager, config_manager, auth)
\n
\n
\n
\n
\n# ============================================================================
\n# 14. DEPLOY E SERVIDOR (Estrutura da Classe WebServer)
\n
\n# PARTE 4 — TEMPLATE HTML DO FRONT-END (INTERFACE DO USUÁRIO)
\nDASHBOARD_TEMPLATE = """
\n<!DOCTYPE html>
\n<html lang="pt-BR">
\n<head>
\n    <meta charset="UTF-8">
\n    <meta name="viewport" content="width=device-width, initial-scale=1.0">
\n    <title>Consulta de Produto Bling</title>
\n    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
\n    <style>
\n        body {{ background-color: #f8f9fa; }
\n        .container {{ max-width: 800px; margin-top: 50px; }
\n        .product-card {{ border: 1px solid #dee2e6; border-radius: 0.5rem; padding: 20px; background-color: #fff; box-shadow: 0 0.125rem 0.25rem rgba(0, 0, 0, 0.075); }
\n        .product-image {{ max-width: 100%; height: auto; border-radius: 0.25rem; margin-bottom: 15px; }
\n        .product-detail {{ margin-bottom: 5px; }
\n        .product-detail strong {{ display: inline-block; width: 120px; }
\n        #descricao {{ border-top: 1px solid #eee; padding-top: 15px; margin-top: 15px; }
\n        .hidden {{ display: none; }
\n    </style>
\n</head>
\n<body>
\n    <div class="container">
\n        <h1 class="mb-4 text-center">Consulta de Produto Bling</h1>
\n        
\n        <div class="input-group mb-3">
\n            <input type="text" class="form-control" id="skuInput" placeholder="Digite o SKU do produto" aria-label="SKU do produto">
\n            <button class="btn btn-primary" type="button" id="searchButton">Buscar</button>
\n        </div>
\n
\n        <div id="loading" class="alert alert-info hidden" role="alert">
\n            Buscando produto...
\n        </div>
\n
\n        <div id="errorAlert" class="alert alert-danger hidden" role="alert">
\n            Produto não encontrado. Verifique o SKU.
\n        </div>
\n
\n        <div id="productDetails" class="product-card hidden">
\n            <div class="row">
\n                <div class="col-md-4 text-center">
\n                    <img id="imgProduto" src="/placeholder.png" alt="Imagem do Produto" class="product-image">
\n                </div>
\n                <div class="col-md-8">
\n                    <h2 id="nome" class="mb-3"></h2>
\n                    <div class="product-detail"><strong>Código:</strong> <span id="codigo"></span></div>
\n                    <div class="product-detail"><strong>Tipo:</strong> <span id="tipo"></span></div>
\n                    <div class="product-detail"><strong>Situação:</strong> <span id="situacao"></span></div>
\n                    <div class="product-detail"><strong>Formato:</strong> <span id="formato"></span></div>
\n                    <div class="product-detail"><strong>Preço:</strong> <span id="preco"></span></div>
\n                    <div class="product-detail"><strong>Preço Custo:</strong> <span id="precoCusto"></span></div>
\n                    <div class="product-detail"><strong>Estoque:</strong> <span id="estoque"></span></div>
\n                </div>
\n            </div>
\n            <div id="descricao">
\n                <h4>Descrição</h4>
\n                <!-- A descrição será inserida aqui com innerHTML -->
\n            </div>
\n        </div>
\n    </div>
\n
\n    <script>
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
    </script>\n</body>
\n</html>
\n"""
\n
\nclass WebServer:
\n    """Gerencia o servidor Flask, rotas e websocket."""
\n    
\n    def __init__(self, app: Flask, orchestrator: AutomationOrchestrator):
\n        self.app = app
\n        self.orchestrator = orchestrator
\n        self.sock = Sock(app)
\n        self.setup_routes()
\n        self.setup_websocket()
\n
\n    def setup_routes(self):
\n        """Configura todas as rotas da API e do Dashboard."""
\n        
\n        # 1. Dashboard e Páginas de Auth
\n
\n
\n        # PARTE 5 — ROTA DO FRONT-END
\n        @self.app.route("/")
\n        def dashboard():
\n            """Rota principal que serve o dashboard de consulta de produto."""
\n            return render_template_string(DASHBOARD_TEMPLATE)
\n
\n        @self.app.route('/callback')
\n        def callback():
\n            code = request.args.get('code')
\n            error = request.args.get('error')
\n            
\n            if error:
\n                return render_template_string(ERROR_TEMPLATE, message=f"Erro de Autorização: {error}")
\n            
\n            if code:
\n                try:
\n                    self.orchestrator.auth.exchange_code_for_token(code)
\n                    return render_template_string(SUCCESS_TEMPLATE, message="Autenticação concluída com sucesso!")
\n                except BlingAuthError as e:
\n                    return render_template_string(ERROR_TEMPLATE, message=f"Falha na troca de código: {e}")
\n            
\n            return render_template_string(ERROR_TEMPLATE, message="Parâmetros de callback inválidos.")
\n
\n        # 2. Rotas de Status e Estatísticas
\n        @self.app.route('/api/status')
\n        def api_status():
\n            is_valid = self.orchestrator.auth.is_token_valid()
\n            return jsonify({
\n                "authenticated": is_valid,
\n                "auth_url": self.orchestrator.auth.get_authorization_url(),
\n                "token_expires_at": (
\n                    datetime.fromtimestamp(self.orchestrator.auth.expires_at).isoformat()
\n                    if self.orchestrator.auth.expires_at
\n                    else None
\n                ),
\n                "data_loaded": True, # Assume True, pois o carregamento é feito por worker/processo
\n                "is_running": self.orchestrator.is_running
\n            })
\n
\n        @self.app.route('/api/stats')
\n        def api_stats():
\n            return jsonify(self.orchestrator.stats.to_dict())
\n
\n        # 3. Rotas de Dados
\n        @self.app.route("/api/produtos", methods=["GET"])
\n        def api_produtos():
\n            sku = request.args.get("sku")
\n
\n            if not sku:
\n                return jsonify({"error": "SKU não informado"}), 400
\n
\n            print("Consulta de produto recebida:", sku, flush=True)
\n
\n            data = get_bling_product_by_sku(sku)
\n
\n            return jsonify(data), 200
\n
\n        @self.app.route('/api/kits')
\n        def api_kits():
\n            kits_data = [
\n                {
\n                    "sku": k.sku,
\n                    "name": k.name,
\n                    "price": k.price,
\n                    "components": [
\n                        {
\n                            "sku": c.sku,
\n                            "name": c.name,
\n                            "qty": c.qty,
\n                            "supplier": c.supplier,
\n                            "lead_time_days": c.lead_time_days,
\n                            "unit_cost": c.unit_cost
\n                        } for c in k.components
\n                    ]
\n                } for k in self.orchestrator.kits
\n            ]
\n            return jsonify({"kits": kits_data})
\n
\n        @self.app.route('/api/stock')
\n        def api_stock():
\n            all_components: Dict[str, Component] = {}
\n            for kit in self.orchestrator.kits:
\n                for component in kit.components:
\n                    all_components[component.sku] = component
\n            
\n            stock_data = [
\n                {
\n                    "sku": c.sku,
\n                    "name": c.name,
\n                    "current_stock": c.current_stock,
\n                    "min_stock": c.min_stock,
\n                    "supplier": c.supplier,
\n                    "lead_time_days": c.lead_time_days,
\n                    "alert_level": "danger" if c.current_stock < c.min_stock else ("warning" if c.current_stock < c.min_stock * 1.5 else "ok")
\n                } for c in all_components.values()
\n            ]
\n            return jsonify({"stock": stock_data})
\n
\n        @self.app.route('/api/needs')
\n        def api_needs():
\n            needs_list = []
\n            for supplier, needs in self.orchestrator.needs_manager.needs.items():
\n                for need in needs:
\n                    needs_list.append({
\n                        "component_sku": need.component_sku,
\n                        "component_name": need.component_name,
\n                        "quantity_needed": need.quantity_needed,
\n                        "supplier": need.supplier,
\n                        "lead_time_days": need.lead_time_days,
\n                        "reason": need.reason
\n                    })
\n            return jsonify({"needs": needs_list})
\n
\n        # 4. Rotas de Ação
\n        @self.app.route('/api/recheck', methods=['POST'])
\n        def api_recheck():
\n            if self.orchestrator.is_running:
\n                return jsonify({"status": "warning", "message": "Processamento já em andamento."}), 409
\n            
\n            # Executa a verificação em uma thread para não bloquear a requisição HTTP
\n            Thread(target=self.orchestrator.run_purchase_check, args=(True,), daemon=True).start()
\n            
\n            return jsonify({"status": "ok", "message": "Verificação de estoque e POs iniciada em background."})
\n
\n        @self.app.route('/api/process_kits', methods=['POST'])
\n        def api_process_kits():
\n            if self.orchestrator.is_running:
\n                return jsonify({"status": "warning", "message": "Processamento já em andamento."}), 409
\n                
\n            data = request.get_json(silent=True) or {}
\n            sku_list = data.get('skus', [])
\n            quantity = data.get('quantity', 1)
\n            batch_size = data.get('batch_size', self.orchestrator.config.DEFAULT_BATCH_SIZE)
\n            
\n            kits_to_process = [k for k in self.orchestrator.kits if k.sku in sku_list]
\n            
\n            if not kits_to_process:
\n                return jsonify({"status": "error", "message": "Nenhum kit encontrado com os SKUs fornecidos."}), 404
\n            
\n            # Executa o processamento em uma thread
\n            Thread(target=self.orchestrator.process_kits, args=(kits_to_process, batch_size, True, quantity), daemon=True).start()
\n            
\n            return jsonify({"status": "ok", "message": f"Processamento de {len(kits_to_process)} kits iniciado em background."})
\n
\n        # 5. Webhook
\n        @self.app.route("/webhook/bling", methods=["POST"])
\n        def webhook_bling():
\n            try:
\n                data = request.get_json(silent=True)
\n            except Exception:
\n                data = None
\n
\n            print("WEBHOOK RECEBIDO:", data, flush=True)
\n
\n            return jsonify({"status": "ok"}), 200
\n
\n    # 10. WEBSOCKET PARA LOGS
\n    def setup_websocket(self):
\n        """Configura a rota WebSocket para logs em tempo real."""
\n        
\n        @self.sock.route('/ws/logs')
\n        def ws_logs(ws):
\n            logger.info("Cliente WebSocket conectado para logs.")
\n            last_log_count = 0
\n            
\n            # O loop continua enquanto a conexão WebSocket estiver aberta.
\n            # O fechamento da conexão pelo cliente irá levantar uma exceção, que será capturada.
\n            while not ws.closed:
\n                try:
\n                    # Envia todos os logs novos desde a última verificação
\n                    current_logs = memory_handler.get_logs()
\n                    new_logs = current_logs[last_log_count:]
\n                    
\n                    if new_logs:
\n                        ws.send(json.dumps({"logs": new_logs}))
\n                        last_log_count = len(current_logs)
\n                        
\n                    time.sleep(1) # Envia a cada 1 segundo
\n                except Exception as e:
\n                    logger.warning(f"Erro no WebSocket: {e}. Fechando conexão.")
\n                    break
\n            logger.info("Cliente WebSocket desconectado.")
\n
\n# 14. DEPLOY E SERVIDOR (Função factory e background task)
\ndef background_load():
\n    """Função executada em thread para carregar dados em background."""
\n    
\n    # Delay desnecessário removido conforme instruído.
\n    # O carregamento de dados deve ser otimizado dentro de orchestrator.load_data()
\n    # para evitar o carregamento de TODOS os kits na inicialização.
\n    
\n    logger.info("Iniciando Carregamento de Dados em Background")
\n    
\n    try:
\n        # 1. Tenta carregar tokens
\n        if not auth.load_tokens():
\n            logger.warning("Tokens não carregados. Necessário autenticar via dashboard.")
\n            return
\n            
\n        # 2. Busca kits e componentes (inclui estoque)
\n        if orchestrator.load_data():
\n            logger.info("Carregamento de dados em background concluído com sucesso.")
\n        else:
\n            logger.error("Falha no carregamento de dados em background.")
\n            
\n    except Exception as e:
\n        logger.error(f"Erro crítico no background_load: {e}")
\n        error_logger.error(f"Erro crítico no background_load: {e}")
\n        
\n    
\n
\ndef create_app() -> Flask:
\n    """Função factory para criar a aplicação Flask."""
\n    app = Flask(__name__, template_folder='.')
\n    # A WebServer inicializa as rotas e o websocket
\n    WebServer(app, orchestrator)
\n    return app
\n
\n# Variável global para WSGI
\napp = create_app()
\n
\n# ============================================================================
\n# 18. TEMPLATES HTML (Mínimos para Auth)
\n# ============================================================================
\n
\nSUCCESS_TEMPLATE = """
\n<!DOCTYPE html>
\n<html lang="pt-br">
\n<head>
\n    <meta charset="utf-8">
\n    <meta name="viewport" content="width=device-width, initial-scale=1.0">
\n    <title>Sucesso!</title>
\n    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
\n</head>
\n<body class="bg-light">
\n    <div class="container d-flex justify-content-center align-items-center" style="min-height: 100vh;">
\n        <div class="card shadow-lg p-5 text-center">
\n            <h1 class="text-success">✅ Sucesso!</h1>
\n            <p class="lead">{{ message }</p>
\n            <p>Você pode fechar esta janela e voltar ao dashboard.</p>
\n            <a href="/" class="btn btn-primary">Voltar ao Dashboard</a>
\n        </div>
\n    </div>
\n</body>
\n</html>
\n"""
\n
\nERROR_TEMPLATE = """
\n<!DOCTYPE html>
\n<html lang="pt-br">
\n<head>
\n    <meta charset="utf-8">
\n    <meta name="viewport" content="width=device-width, initial-scale=1.0">
\n    <title>Erro!</title>
\n    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
\n</head>
\n<body class="bg-light">
\n    <div class="container d-flex justify-content-center align-items-center" style="min-height: 100vh;">
\n        <div class="card shadow-lg p-5 text-center">
\n            <h1 class="text-danger">❌ Erro!</h1>
\n            <p class="lead">{{ message }</p>
\n            <p>Verifique o log para mais detalhes ou tente novamente.</p>
\n            <a href="/" class="btn btn-primary">Voltar ao Dashboard</a>
\n        </div>
\n    </div>
\n</body>
\n</html>
\n"""
\n
\n# 11. DASHBOARD HTML COMPLETO (Será minificado e completo na próxima fase)
\n# Por enquanto, apenas o esqueleto com o CSS e JS embutidos
\nDASHBOARD_TEMPLATE = """
\n<!DOCTYPE html>
\n<html lang="pt-br">
\n<head>
\n    <meta charset="utf-8">
\n    <meta name="viewport" content="width=device-width, initial-scale=1.0">
\n    <title>Painel Bling - Automação ERP</title>
\n    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
\n    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
\n    <style>
\n
\n        body {{ background: #f8f9fa; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
\n        .navbar {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; box-shadow: 0 4px 6px rgba(0,0,0,.1); }
\n        .navbar-brand {{ font-weight: 700; font-size: 1.5rem; }
\n        .status-badge {{ padding: .5rem 1rem; border-radius: 20px; font-size: .9rem; font-weight: 600; }
\n        .card {{ border-radius: 1rem; box-shadow: 0 4px 6px rgba(0,0,0,.07); border: none; margin-bottom: 1.5rem; transition: transform 0.3s ease, box-shadow 0.3s ease; }
\n        .card:hover {{ transform: translateY(-5px); box-shadow: 0 8px 15px rgba(0,0,0,.1); }
\n        .card-title {{ font-weight: 600; color: #343a40; margin-bottom: 1rem; }
\n        .kpi-value {{ font-size: 2.5rem; font-weight: 700; margin-bottom: .25rem; }
\n        .kpi-label {{ font-size: .9rem; color: #6c757d; text-transform: uppercase; letter-spacing: .5px; }
\n        .log-box {{ font-family: 'Courier New', monospace; font-size: .85em; background: #1e1e1e; color: #d4d4d4; border-radius: .5rem; padding: 1rem; max-height: 400px; overflow-y: auto; }
\n        .log-entry {{ padding: .25rem 0; border-bottom: 1px solid #333; }
\n        .log-entry:last-child {{ border-bottom: none; }
\n        .log-level-INFO {{ color: #4ec9b0; }
\n        .log-level-WARNING {{ color: #dcdcaa; }
\n        .log-level-ERROR {{ color: #f48771; }
\n        .log-level-DEBUG {{ color: #9cdcfe; }
\n        .nav-tabs .nav-link {{ color: #6c757d; font-weight: 500; }
\n        .nav-tabs .nav-link.active {{ background-color: #fff; border-color: #dee2e6 #dee2e6 #fff; color: #667eea; font-weight: 600; }
\n        .table-danger td {{ background-color: #f8d7da !important; }
\n        .table-warning td {{ background-color: #fff3cd !important; }
\n        .btn-primary {{ background: linear-gradient(45deg, #667eea, #764ba2); border: none; transition: all 0.3s ease; }
\n        .btn-primary:hover {{ transform: translateY(-2px); box-shadow: 0 4px 8px rgba(102, 126, 234, 0.4); }
\n        .spinner-border-sm {{ width: 1rem; height: 1rem; border-width: .15em; }
\n    </style>
\n</head>
\n<body>
\n    <nav class="navbar navbar-expand-lg">
\n        <div class="container-fluid">
\n            <a class="navbar-brand text-white" href="#">Bling Automação</a>
\n            <div class="d-flex">
\n                <span id="status-badge" class="status-badge bg-secondary text-white me-2">Carregando...</span>
\n                <a id="auth-link" href="{{{{ auth_url }}" class="btn btn-sm btn-outline-light">Autenticar Bling</a>
\n            </div>
\n        </div>
\n    </nav>
\n
\n    <div class="container mt-4">
\n        <div class="row">
\n            <!-- 5. SISTEMA DE ESTATÍSTICAS (KPIs) -->
\n            <div class="col-md-2"><div class="card text-center"><div class="card-body"><div id="kpi-success" class="kpi-value text-success">0</div><div class="kpi-label">Sucesso ✅</div></div></div></div>
\n            <div class="col-md-2"><div class="card text-center"><div class="card-body"><div id="kpi-failed" class="kpi-value text-danger">0</div><div class="kpi-label">Falhas ❌</div></div></div></div>
\n            <div class="col-md-2"><div class="card text-center"><div class="card-body"><div id="kpi-ops" class="kpi-value text-primary">0</div><div class="kpi-label">OPs Criadas 🏭</div></div></div></div>
\n            <div class="col-md-2"><div class="card text-center"><div class="card-body"><div id="kpi-pos" class="kpi-value text-info">0</div><div class="kpi-label">POs Criadas 🛒</div></div></div></div>
\n            <div class="col-md-2"><div class="card text-center"><div class="card-body"><div id="kpi-checks" class="kpi-value text-secondary">0</div><div class="kpi-label">Checks Estoque 🔍</div></div></div></div>
\n            <div class="col-md-2"><div class="card text-center"><div class="card-body"><div id="kpi-time" class="kpi-value text-dark">0s</div><div class="kpi-label">Tempo Total ⏱️</div></div></div></div>
\n        </div>
\n
\n        <div class="row">
\n            <div class="col-md-6">
\n                <div class="card">
\n                    <div class="card-body">
\n                        <h5 class="card-title">Gráfico de Processamento</h5>
\n                        <!-- 5. SISTEMA DE ESTATÍSTICAS (Gráfico) -->
\n                        <canvas id="processingChart"></canvas>
\n                    </div>
\n                </div>
\n            </div>
\n            <div class="col-md-6">
\n                <div class="card">
\n                    <div class="card-body">
\n                        <h5 class="card-title">Logs em Tempo Real</h5>
\n                        <!-- 5. SISTEMA DE ESTATÍSTICAS (Logs) -->
\n                        <div id="logs-content" class="log-box">
\n                            <p class="text-white-50">Aguardando conexão com o WebSocket...</p>
\n                        </div>
\n                    </div>
\n                </div>
\n            </div>
\n        </div>
\n
\n        <div class="card mt-4">
\n            <div class="card-header">
\n                <ul class="nav nav-tabs card-header-tabs" id="myTab" role="tablist">
\n                    <li class="nav-item"><a class="nav-link active" id="stock-tab" data-bs-toggle="tab" href="#stock" role="tab">Estoque de Componentes</a></li>
\n                    <li class="nav-item"><a class="nav-link" id="needs-tab" data-bs-toggle="tab" href="#needs" role="tab">Necessidades de Compra</a></li>
\n                    <li class="nav-item"><a class="nav-link" id="kits-tab" data-bs-toggle="tab" href="#kits" role="tab">Kits e Estrutura</a></li>
\n                    <li class="nav-item"><a class="nav-link" id="search-tab" data-bs-toggle="tab" href="#search" role="tab">Busca Detalhada</a></li>
\n                </ul>
\n            </div>
\n            <div class="card-body">
\n                <div class="tab-content" id="myTabContent">
\n                    <!-- Tabela de Estoque -->
\n                    <div class="tab-pane fade show active" id="stock" role="tabpanel">
\n                        <div class="d-flex justify-content-between align-items-center mb-3">
\n                            <h5 class="card-title">Estoque de Componentes com Alertas</h5>
\n                            <!-- 11. BOTÃO RECHECK -->
\n                            <button id="recheck-button" class="btn btn-primary btn-sm">
\n                                <span id="recheck-spinner" class="spinner-border spinner-border-sm me-2 d-none" role="status" aria-hidden="true"></span>
\n                                Verificar Estoque e Gerar POs
\n                            </button>
\n                        </div>
\n                        <p id="recheck-status" class="text-muted"></p>
\n                        <div class="table-responsive">
\n                            <table class="table table-striped table-hover">
\n                                <thead>
\n                                    <tr><th>SKU</th><th>Nome</th><th>Estoque Atual</th><th>Estoque Mínimo</th><th>Fornecedor</th><th>Lead Time (dias)</th><th>Alerta</th></tr>
\n                                </thead>
\n                                <tbody id="stock-table-body">
\n                                    <tr><td colspan="7" class="text-center">Carregando dados de estoque...</td></tr>
\n                                </tbody>
\n                            </table>
\n                        </div>
\n                    </div>
\n                    <!-- Tabela de Necessidades -->
\n                    <div class="tab-pane fade" id="needs" role="tabpanel">
\n                        <h5 class="card-title">Necessidades de Compra Pendentes</h5>
\n                        <div class="table-responsive">
\n                            <table class="table table-striped table-hover">
\n                                <thead>
\n                                    <tr><th>SKU</th><th>Nome</th><th>Qtd. Necessária</th><th>Fornecedor</th><th>Lead Time (dias)</th><th>Motivo</th></tr>
\n                                </thead>
\n                                <tbody id="needs-table-body">
\n                                    <tr><td colspan="6" class="text-center">Nenhuma necessidade de compra pendente.</td></tr>
\n                                </tbody>
\n                            </table>
\n                        </div>
\n                    </div>
\n                    <!-- Tabela de Kits -->
\n                    <div class="tab-pane fade" id="kits" role="tabpanel">
\n                        <h5 class="card-title">Kits de Produtos e Estrutura</h5>
\n                        <div class="table-responsive">
\n                            <table class="table table-striped table-hover">
\n                                <thead>
\n                                    <tr><th>SKU Kit</th><th>Nome Kit</th><th>Preço</th><th>Componentes</th></tr>
\n                                </thead>
\n                                <tbody id="kits-table-body">
\n                                    <tr><td colspan="4" class="text-center">Carregando dados de kits...</td></tr>
\n                                </tbody>
\n                            </table>
\n                        </div>
\n                    </div>
\n                    
\n                    <!-- Busca Detalhada -->
\n                    <div class="tab-pane fade" id="search" role="tabpanel">
\n                        <h5 class="card-title">Buscar Produto por SKU</h5>
\n                        <div class="input-group mb-3">
\n                            <input type="text" class="form-control" id="product-search-sku" placeholder="Digite o SKU do produto (ex: KIT-001)">
\n                            <button class="btn btn-primary" type="button" id="search-product-button">Buscar</button>
\n                        </div>
\n                        <div id="product-search-results" class="mt-4">
\n                            <p class="text-muted">Use o campo acima para buscar um produto e ver seus detalhes e componentes.</p>
\n                        </div>
\n                    </div>
\n                </div>
\n            </div>
\n        </div>
\n    </div>
\n
\n    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
\n    <script>
\n
\n        
\n        const API_BASE = '/api';
\n        const WS_URL = `ws://${window.location.host}/ws/logs`;
\n        let logWebSocket;
\n        let processingChart;
\n
\n        // a) Funções
\n        function formatLog(log) {{
\n            const level = log.level;
\n            const levelClass = `log-level-${level}`;
\n            return `<div class="log-entry"><span class="${levelClass}">[${log.timestamp}] [${level}]</span> ${log.message}</div>`;
\n        }
\n
\n        function updateStatusBadge(isValid, expiresAt) {{
\n            const badge = document.getElementById('status-badge');
\n            const authLink = document.getElementById('auth-link');
\n            
\n            if (isValid) {{
\n                badge.className = 'status-badge bg-success text-white me-2';
\n                badge.textContent = 'Token Válido';
\n                authLink.className = 'btn btn-sm btn-outline-light d-none';
\n            } else {{
\n                badge.className = 'status-badge bg-danger text-white me-2';
\n                badge.textContent = 'Token Inválido';
\n                authLink.className = 'btn btn-sm btn-outline-light';
\n            }
\n            
\n            if (expiresAt) {{
\n                const expiry = new Date(expiresAt);
\n                const now = new Date();
\n                const diffMinutes = Math.round((expiry - now) / 60000);
\n                if (diffMinutes < 60 && diffMinutes > 0) {{
\n                    badge.textContent += ` (Expira em ${diffMinutes} min)`;
\n                    badge.className = 'status-badge bg-warning text-dark me-2';
\n                }
\n            }
\n        }
\n
\n        function updateStatsKPIs(stats) {{
\n            document.getElementById('kpi-success').textContent = stats.success;
\n            document.getElementById('kpi-failed').textContent = stats.failed;
\n            document.getElementById('kpi-ops').textContent = stats.ops_created;
\n            document.getElementById('kpi-pos').textContent = stats.pos_created;
\n            document.getElementById('kpi-checks').textContent = stats.min_stock_checks;
\n            document.getElementById('kpi-time').textContent = `${stats.elapsed_time_seconds}s`;
\n        }
\n
\n        function updateStatsChart(stats) {{
\n            const ctx = document.getElementById('processingChart').getContext('2d');
\n            const data = [stats.success, stats.failed, stats.ops_created, stats.pos_created];
\n            
\n            if (!processingChart) {{
\n                processingChart = new Chart(ctx, {{
\n                    type: 'bar',
\n                    data: {{
\n                        labels: ['Sucesso', 'Falhas', 'OPs Criadas', 'POs Criadas'],
\n                        datasets: [{{
\n                            label: 'Contagem',
\n                            data: data,
\n                            backgroundColor: ['#4ec9b0', '#f48771', '#667eea', '#764ba2'],
\n                            borderColor: ['#4ec9b0', '#f48771', '#667eea', '#764ba2'],
\n                            borderWidth: 1
\n                        }]
\n                    },
\n                    options: {{
\n                        responsive: true,
\n                        scales: {{
\n                            y: {{ beginAtZero: true, ticks: {{ precision: 0 } }
\n                        },
\n                        plugins: {{ legend: {{ display: false } }
\n                    }
\n                });
\n            } else {{
\n                processingChart.data.datasets[0].data = data;
\n                processingChart.update();
\n            }
\n        }
\n
\n        async function fetchStatus() {{
\n            try {{
\n                const response = await fetch(`${API_BASE}/status`);
\n                const data = await response.json();
\n                updateStatusBadge(data.authenticated, data.token_expires_at);
\n                
\n                const recheckButton = document.getElementById('recheck-button');
\n                const recheckSpinner = document.getElementById('recheck-spinner');
\n                if (data.is_running) {{
\n                    recheckButton.disabled = true;
\n                    recheckSpinner.classList.remove('d-none');
\n                    document.getElementById('recheck-status').textContent = 'Processamento em andamento...';
\n                } else {{
\n                    recheckButton.disabled = false;
\n                    recheckSpinner.classList.add('d-none');
\n                    document.getElementById('recheck-status').textContent = '';
\n                }
\n                
\n            } catch (error) {{
\n                console.error('Erro ao buscar status:', error);
\n            }
\n        }
\n
\n        async function fetchStats() {{
\n            try {{
\n                const response = await fetch(`${API_BASE}/stats`);
\n                const stats = await response.json();
\n                updateStatsKPIs(stats);
\n                updateStatsChart(stats);
\n            } catch (error) {{
\n                console.error('Erro ao buscar estatísticas:', error);
\n            }
\n        }
\n
\n        async function fetchStock() {{
\n            try {{
\n                const response = await fetch(`${API_BASE}/stock`);
\n                const data = await response.json();
\n                const tbody = document.getElementById('stock-table-body');
\n                tbody.innerHTML = '';
\n                
\n                if (data.stock.length === 0) {{
\n                    tbody.innerHTML = '<tr><td colspan="7" class="text-center">Nenhum componente encontrado.</td></tr>';
\n                    return;
\n                }
\n                
\n                data.stock.forEach(item => {{
\n                    const row = tbody.insertRow();
\n                    row.className = item.alert_level === 'danger' ? 'table-danger' : (item.alert_level === 'warning' ? 'table-warning' : '');
\n                    
\n                    row.insertCell().textContent = item.sku;
\n                    row.insertCell().textContent = item.name;
\n                    row.insertCell().textContent = item.current_stock;
\n                    row.insertCell().textContent = item.min_stock;
\n                    row.insertCell().textContent = item.supplier;
\n                    row.insertCell().textContent = item.lead_time_days;
\n                    row.insertCell().innerHTML = item.alert_level === 'danger' ? '🚨 Baixo' : (item.alert_level === 'warning' ? '⚠️ Atenção' : '✅ OK');
\n                });
\n            } catch (error) {{
\n                console.error('Erro ao buscar estoque:', error);
\n                document.getElementById('stock-table-body').innerHTML = '<tr><td colspan="7" class="text-center text-danger">Erro ao carregar dados de estoque.</td></tr>';
\n            }
\n        }
\n
\n        async function fetchNeeds() {{
\n            try {{
\n                const response = await fetch(`${API_BASE}/needs`);
\n                const data = await response.json();
\n                const tbody = document.getElementById('needs-table-body');
\n                tbody.innerHTML = '';
\n                
\n                if (data.needs.length === 0) {{
\n                    tbody.innerHTML = '<tr><td colspan="6" class="text-center">Nenhuma necessidade de compra pendente.</td></tr>';
\n                    return;
\n                }
\n                
\n                data.needs.forEach(item => {{
\n                    const row = tbody.insertRow();
\n                    row.insertCell().textContent = item.component_sku;
\n                    row.insertCell().textContent = item.component_name;
\n                    row.insertCell().textContent = item.quantity_needed;
\n                    row.insertCell().textContent = item.supplier;
\n                    row.insertCell().textContent = item.lead_time_days;
\n                    row.insertCell().textContent = item.reason;
\n                });
\n            } catch (error) {{
\n                console.error('Erro ao buscar necessidades:', error);
\n                document.getElementById('needs-table-body').innerHTML = '<tr><td colspan="6" class="text-center text-danger">Erro ao carregar necessidades de compra.</td></tr>';
\n            }
\n        }
\n
\n        async function fetchKits() {{
\n            try {{
\n                const response = await fetch(`${API_BASE}/kits`);
\n                const data = await response.json();
\n                const tbody = document.getElementById('kits-table-body');
\n                tbody.innerHTML = '';
\n                
\n                if (data.kits.length === 0) {{
\n                    tbody.innerHTML = '<tr><td colspan="4" class="text-center">Nenhum kit encontrado.</td></tr>';
\n                    return;
\n                }
\n                
\n                data.kits.forEach(kit => {{
\n                    const row = tbody.insertRow();
\n                    row.insertCell().textContent = kit.sku;
\n                    row.insertCell().textContent = kit.name;
\n                    row.insertCell().textContent = `R$ ${kit.price.toFixed(2)}`;
\n                    
\n                    const componentsCell = row.insertCell();
\n                    componentsCell.innerHTML = kit.components.map(c => 
\n                        `${c.name} (${c.sku}) x${c.qty}`
\n                    ).join('<br>');
\n                });
\n            } catch (error) {{
\n                console.error('Erro ao buscar kits:', error);
\n                document.getElementById('kits-table-body').innerHTML = '<tr><td colspan="4" class="text-center text-danger">Erro ao carregar dados de kits.</td></tr>';
\n            }
\n        }
\n        
\n        async function fetchProductDetails(sku) {{
\n            const resultsDiv = document.getElementById('product-search-results');
\n            resultsDiv.innerHTML = '<p class="text-info">Buscando produto...</p>';
\n            
\n            try {{
\n                const response = await fetch(`${API_BASE}/produtos?sku=${sku}`);
\n                const json = await response.json();
\n                
\n                if (response.ok) {{
\n                    if (!json.data || json.data.length === 0) {{
\n                        resultsDiv.innerHTML = `<p class="text-danger">Erro: Produto não encontrado.</p>`;
\n                        return;
\n                    }
\n                    const p = json.data[0];
\n                    renderProductDetails(p);
\n                } else {{
\n                    resultsDiv.innerHTML = `<p class="text-danger">Erro: ${json.error || 'Produto não encontrado.'}</p>`;
\n                }
\n            } catch (error) {{
\n                console.error('Erro ao buscar detalhes do produto:', error);
\n                resultsDiv.innerHTML = '<p class="text-danger">Erro de conexão ao buscar detalhes do produto.</p>';
\n            }
\n        }
\n        
\n        function renderProductDetails(p) {{
\n            const resultsDiv = document.getElementById('product-search-results');
\n            
\n            // 1. Criar os elementos de exibição (IDs fictícios para o exemplo, pois o HTML não foi fornecido)
\n            // No código real, esses elementos seriam buscados por ID (ex: document.getElementById('nomeEl'))
\n            // Como estamos injetando HTML, vamos construir a string completa.
\n            
\n            let html = `
\n                <div class="card bg-light p-3">
\n                    <div class="row">
\n                        <div class="col-md-4 text-center">
\n                            <img id="produtoImagem" src="${p.imagemURL}" class="img-fluid rounded" alt="Imagem do Produto">
\n                        </div>
\n                        <div class="col-md-8">
\n                            <h5>Detalhes do Produto: ${p.nome} (${p.codigo})</h5>
\n                            <p><strong>Tipo:</strong> ${p.tipo}</p>
\n                            <p><strong>Situação:</strong> ${p.situacao}</p>
\n                            <p><strong>Formato:</strong> ${p.formato}</p>
\n                            <p><strong>Preço:</strong> R$ ${p.preco.toFixed(2)}</p>
\n                            <p><strong>Preço de Custo:</strong> R$ ${p.precoCusto.toFixed(2)}</p>
\n                            <p><strong>Estoque:</strong> ${p.estoque.saldoVirtualTotal}</p>
\n                        </div>
\n                    </div>
\n                    <h6 class="mt-3">Descrição Curta:</h6>
\n                    <div id="descricaoEl" class="card-text"></div>
\n                </div>
\n            `;
\n            
\n            resultsDiv.innerHTML = html;
\n            
\n            // 4. Ajustar descrição (ela é HTML) - Usar innerHTML
\n            const descricaoEl = document.getElementById('descricaoEl');
\n            if (descricaoEl) {{
\n                descricaoEl.innerHTML = p.descricaoCurta;
\n            }
\n            
\n            // 3. Ajustar exibição da imagem - Já está no HTML, mas garantindo o src
\n            const imgProduto = document.getElementById('produtoImagem');
\n            if (imgProduto) {{
\n                imgProduto.src = p.imagemURL;
\n            }
\n        }
\n        
\n        function connectWebSocket() {{
\n            const logContent = document.getElementById('logs-content');
\n            logContent.innerHTML = '<p class="text-white-50">Tentando conectar ao WebSocket...</p>';
\n            
\n            logWebSocket = new WebSocket(WS_URL);
\n
\n            logWebSocket.onopen = () => {{
\n                console.log('WebSocket conectado.');
\n                logContent.innerHTML = ''; // Limpa a mensagem de conexão
\n            };
\n
\n            logWebSocket.onmessage = (event) => {{
\n                const data = JSON.parse(event.data);
\n                if (data.logs) {{
\n                    data.logs.forEach(log => {{
\n                        logContent.innerHTML += formatLog(log);
\n                    });
\n                    // Scroll para o final
\n                    logContent.scrollTop = logContent.scrollHeight;
\n                }
\n            };
\n
\n            logWebSocket.onclose = (event) => {{
\n                console.warn('WebSocket desconectado. Tentando reconectar em 5s...', event.reason);
\n                logContent.innerHTML += formatLog({{
\n                    timestamp: new Date().toISOString().slice(0, 19),
\n                    level: 'WARNING',
\n                    message: 'Conexão WebSocket perdida. Tentando reconectar...'
\n                });
\n                setTimeout(connectWebSocket, 5000); // Reconexão automática
\n            };
\n
\n                    logWebSocket.onerror = (err) => {{
\n                        console.error('WebSocket erro:', err);
\n                        // Não chama close() aqui. Deixa o onclose() tratar a reconexão
\n                    };
\n        }
\n        
\n        // Handler do botão recheck
\n        document.getElementById('recheck-button').addEventListener('click', async () => {{
\n            const button = document.getElementById('recheck-button');
\n            const spinner = document.getElementById('recheck-spinner');
\n            const statusText = document.getElementById('recheck-status');
\n            
\n            button.disabled = true;
\n            spinner.classList.remove('d-none');
\n            statusText.textContent = 'Iniciando verificação de estoque e geração de POs...';
\n            
\n            try {{
\n                const response = await fetch(`${API_BASE}/recheck`, {{ method: 'POST' });
\n                const data = await response.json();
\n                
\n                if (response.ok) {{
\n                    statusText.textContent = data.message;
\n                } else {{
\n                    statusText.textContent = `Erro: ${data.error || 'Falha na requisição.'}`;
\n                    button.disabled = false;
\n                    spinner.classList.add('d-none');
\n                }
\n                
\n            } catch (error) {{
\n                console.error('Erro ao chamar /api/recheck:', error);
\n                statusText.textContent = 'Erro de conexão ao iniciar a verificação.';
\n                button.disabled = false;
\n                spinner.classList.add('d-none');
\n            }
\n            // O status final será atualizado pelo fetchStatus quando is_running voltar a ser false
\n        });
\n        
\n        // Handler do botão de busca de produto
\n        document.getElementById('search-product-button').addEventListener('click', () => {{
\n            const skuInput = document.getElementById('product-search-sku');
\n            const sku = skuInput.value.trim();
\n            if (sku) {{
\n                fetchProductDetails(sku);
\n            } else {{
\n                document.getElementById('product-search-results').innerHTML = '<p class="text-warning">Por favor, digite um SKU para buscar.</p>';
\n            }
\n        });
\n
\n        // Handler do botão recheck
\n        document.getElementById('recheck-button').addEventListener('click', async () => {{
\n            const button = document.getElementById('recheck-button');
\n            const spinner = document.getElementById('recheck-spinner');
\n            const statusText = document.getElementById('recheck-status');
\n            
\n            button.disabled = true;
\n            spinner.classList.remove('d-none');
\n            statusText.textContent = 'Iniciando verificação de estoque e geração de POs...';
\n            
\n            try {{
\n                const response = await fetch(`${API_BASE}/recheck`, {{ method: 'POST' });
\n                const data = await response.json();
\n                
\n                if (response.ok) {{
\n                    statusText.textContent = data.message;
\n                } else {{
\n                    statusText.textContent = `Erro: ${data.message || 'Falha na requisição.'}`;
\n                    button.disabled = false;
\n                    spinner.classList.add('d-none');
\n                }
\n                
\n            } catch (error) {{
\n                console.error('Erro ao chamar /api/recheck:', error);
\n                statusText.textContent = 'Erro de conexão ao iniciar a verificação.';
\n                button.disabled = false;
\n                spinner.classList.add('d-none');
\n            }
\n            // O status final será atualizado pelo fetchStatus quando is_running voltar a ser false
\n        });
\n
\n        // b) Intervalos
\n        document.addEventListener('DOMContentLoaded', () => {{
\n            fetchStatus();
\n            fetchStats();
\n            fetchStock();
\n            fetchNeeds();
\n            fetchKits();
\n            connectWebSocket();
\n            
\n            // Polling otimizado:
\n            // Status e Estatísticas (leves e importantes para feedback imediato) a cada 10s
\n            setInterval(fetchStatus, 10000);
\n            setInterval(fetchStats, 10000);
\n
\n            // Dados pesados (Estoque, Necessidades, Kits) a cada 60s
\n            const dataPollingInterval = 60000;
\n            setInterval(fetchStock, dataPollingInterval);
\n            setInterval(fetchNeeds, dataPollingInterval);
\n            setInterval(fetchKits, dataPollingInterval);
\n        });
\n    </script>
\n</body>
\n</html>
\n"""
\n
\n# ============================================================================
\n# 15. CLI AVANÇADO
\n# ============================================================================
\n
\ndef run_cli():
\n    """Função principal para execução via linha de comando."""
\n    
\n    parser = argparse.ArgumentParser(description="Sistema de Automação Bling ERP.")
\n    parser.add_argument('--serve', action='store_true', help='Inicia o servidor web.')
\n    parser.add_argument('--run', action='store_true', help='Executa o processamento de kits (cria OPs e POs).')
\n    parser.add_argument('--port', type=int, default=8000, help='Define a porta para o servidor web (padrão: 8000).')
\n    
\n    args = parser.parse_args()
\n    
\n    if args.serve:
\n        logger.info("Iniciando Servidor Web")
\n        
\n        # 14. Lazy loading com Thread em background
\n        Thread(target=background_load, daemon=True).start()
\n        
\n        # 15. Validação de credenciais antes de iniciar
\n        if config.CLIENT_ID == 'YOUR_CLIENT_ID' or config.CLIENT_SECRET == 'YOUR_CLIENT_SECRET':
\n            logger.error("Credenciais BLING_CLIENT_ID ou BLING_CLIENT_SECRET não configuradas.")
\n            logger.warning("Configure as variáveis de ambiente ou altere a classe Config.")
\n            
\n        # Garante que o REDIRECT_URI está correto para a porta
\n        if args.port != 8000:
\n            config.REDIRECT_URI = config.REDIRECT_URI.replace(':8000', f':{args.port}')
\n            logger.info(f"REDIRECT_URI ajustado para a porta {args.port}: {config.REDIRECT_URI}")
\n            
\n        # O erro "port founds" (provavelmente "port already in use") é evitado
\n        # garantindo que a porta seja configurável e que o servidor seja iniciado
\n        # corretamente.
\n        try:
\n            logger.info(f"Servidor rodando em http://127.0.0.1:{args.port}")
\n            app.run(host='0.0.0.0', port=args.port, debug=False)
\n        except Exception as e:
\n            logger.error(f"Falha ao iniciar o servidor na porta {args.port}: {e}")
\n            error_logger.error(f"Falha ao iniciar o servidor: {e}")
\n            
\n    elif args.run:
\n        logger.info("Iniciando Processamento de Kits (CLI)")
\n        
\n        if not auth.load_tokens():
\n            logger.error("Não foi possível carregar tokens. Execute --serve e autentique primeiro.")
\n            return
\n            
\n        if not orchestrator.load_data():
\n            logger.error("Não foi possível carregar dados do Bling. Verifique a conexão e o token.")
\n            return
\n            
\n        # Processa todos os kits encontrados com quantidade 1 e batch_size padrão
\n        orchestrator.process_kits(
\n            orchestrator.kits, 
\n            batch_size=config.DEFAULT_BATCH_SIZE, 
\n            check_stock=True, 
\n            quantity=1
\n        )
\n        
\n    else:
\n        parser.print_help()
\n
\nif __name__ == '__main__':
\n    # 21. ESTRUTURA DE ARQUIVOS (Criação de logs/ garantida pelo setup_logging)
\n    # 13. JAVASCRIPT FALTANDO (Os intervalos e handlers estão no template)
\n    # 12. CSS FALTANDO (O CSS está no template)
\n    
\n    # O código base já tinha a estrutura de logs em memória, agora está completa.
\n    # O código base tinha rotas simples, agora estão completas e na classe WebServer.
\n    
\n    run_cli()