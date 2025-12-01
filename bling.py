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
from pathlib import Path
from datetime import datetime
from threading import Lock

import requests
from flask import Flask, request, render_template_string, jsonify

# ============================================================================
# CONFIGURAÇÃO
# ============================================================================

BLING_API_KEY = os.environ.get('BLING_API_KEY', '')
BLING_API_URL = 'https://www.bling.com.br/Api/v3'

# ============================================================================
# ROTAS DA API
# ============================================================================

from flask import Flask, request, jsonify
from flask_sock import Sock

app = Flask(__name__)
sock = Sock(app)

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
memory_handler = None
logger = None

@app.route('/')
@app.route('/dashboard')
def dashboard():
    return render_template_string(DASHBOARD_TEMPLATE)

@app.route('/health')
def health_check():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

@app.route('/api/produtos')
def get_produtos():
    try:
        headers = {
            'Authorization': f'Bearer {BLING_API_KEY}',
            'Accept': 'application/json'
        }
        
        params = {}
        if request.args.get('pesquisa'):
            params['pesquisa'] = request.args.get('pesquisa')
        
        response = requests.get(
            f'{BLING_API_URL}/produtos',
            headers=headers,
            params=params,
            timeout=30
        )
        
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({
                'error': f'Erro na API do Bling: {response.status_code}',
                'details': response.text
            }), response.status_code
            
    except Exception as e:
        logger.error(f"Erro ao buscar produtos: {e}") if logger else print(f"Erro ao buscar produtos: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/logs')
def get_logs():
    try:
        logs = memory_handler.get_logs(limit=50)
        return jsonify({'logs': logs})
    except Exception as e:
        logger.error(f"Erro ao buscar logs: {e}") if logger else print(f"Erro ao buscar logs: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/kits')
def get_kits():
    try:
        headers = {
            'Authorization': f'Bearer {BLING_API_KEY}',
            'Accept': 'application/json'
        }
        
        # Busca produtos do tipo Kit (P)
        response = requests.get(
            f'{BLING_API_URL}/produtos',
            headers=headers,
            params={'tipo': 'P'},
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            kits = []
            
            for product in data.get('data', []):
                if product.get('tipo') == 'P' and product.get('estrutura'):
                    componentes = []
                    for item in product['estrutura'].get('componentes', []):
                        comp_data = item.get('produto', {})
                        componentes.append({
                            'sku': comp_data.get('codigo', 'N/A'),
                            'nome': comp_data.get('descricao', 'Sem nome'),
                            'quantidade': item.get('quantidade', 0)
                        })
                    
                    kits.append({
                        'sku': product.get('codigo', 'N/A'),
                        'nome': product.get('descricao', 'Sem nome'),
                        'componentes': componentes
                    })
            
            return jsonify({'kits': kits})
        else:
            return jsonify({
                'error': f'Erro na API do Bling: {response.status_code}'
            }), response.status_code
            
    except Exception as e:
        logger.error(f"Erro ao buscar kits: {e}") if logger else print(f"Erro ao buscar kits: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/recheck', methods=['POST'])
def recheck():
    try:
        logger.info("🔄 Verificação manual iniciada via API") if logger else print("🔄 Verificação manual iniciada via API")
        # Aqui você pode adicionar lógica de verificação de estoque
        return jsonify({"status": "ok", "message": "Verificação iniciada"})
    except Exception as e:
        logger.error(f"Erro na verificação: {e}") if logger else print(f"Erro na verificação: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/webhook/bling', methods=['POST'])
def webhook_bling():
    try:
        data = request.get_json(force=True)
        event_type = data.get('event') or data.get('tipo') or 'unknown'
        logger.info(f"🪝 Webhook recebido: {event_type}") if logger else print(f"🪝 Webhook recebido: {event_type}")
        
        is_order_event = (
            event_type == 'order.created' or 
            event_type == 'pedido.pago' or 
            (data.get('tipo') == 'pedido' and data.get('evento') in ['criado', 'pago'])
        )
        
        if is_order_event:
            pedido_id = data.get('id') or (data.get('retorno', {}).get('pedidos', [{}])[0].get('pedido', {}).get('id'))
            if pedido_id:
                logger.info(f"✅ Pedido ID {pedido_id} identificado") if logger else print(f"✅ Pedido ID {pedido_id} identificado")
                return jsonify({'status': 'ok', 'message': f'Pedido {pedido_id} processado'}), 200
        
        return jsonify({'status': 'ok', 'message': f'Webhook {event_type} recebido'}), 200
    except Exception as e:
        logger.error(f"Erro no webhook: {e}") if logger else print(f"Erro no webhook: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================================
# TEMPLATE HTML (Design completo do Bling 2)
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
        
        .table-responsive {
            background: white;
            border-radius: 15px;
            padding: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,.07);
        }
        
        .table thead th {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 15px;
            font-weight: 600;
        }
        
        .table tbody tr:hover {
            background-color: #f8f9fa;
        }
        
        .table-danger td {
            background-color: #f8d7da !important;
        }
        
        .table-warning td {
            background-color: #fff3cd !important;
        }
        
        .btn-primary {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border: none;
            padding: 12px 30px;
            font-weight: 600;
            border-radius: 10px;
            transition: all 0.3s ease;
        }
        
        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
        }
        
        .btn-secondary {
            padding: 12px 30px;
            font-weight: 600;
            border-radius: 10px;
        }
        
        .search-box {
            display: flex;
            gap: 15px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        
        input[type="text"], select {
            padding: 12px 20px;
            border: 2px solid #e0e0e0;
            border-radius: 12px;
            font-size: 16px;
            transition: all 0.3s ease;
        }
        
        input[type="text"]:focus, select:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        .filters {
            display: flex;
            gap: 15px;
            flex-wrap: wrap;
            align-items: center;
        }
        
        .products-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 25px;
        }
        
        .product-card {
            background: white;
            border-radius: 15px;
            padding: 25px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.1);
            transition: all 0.3s ease;
            position: relative;
            overflow: hidden;
        }
        
        .product-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 4px;
            background: linear-gradient(90deg, #667eea 0%, #764ba2 100%);
        }
        
        .product-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 15px 50px rgba(0, 0, 0, 0.15);
        }
        
        .product-id {
            display: inline-block;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 5px 12px;
            border-radius: 8px;
            font-size: 0.85em;
            font-weight: 600;
            margin-bottom: 10px;
        }
        
        .product-name {
            font-size: 1.3em;
            font-weight: 600;
            color: #333;
            margin-bottom: 5px;
            line-height: 1.4;
        }
        
        .product-sku {
            color: #888;
            font-size: 0.9em;
        }
        
        .product-details {
            margin-top: 15px;
            padding-top: 15px;
            border-top: 2px solid #f0f0f0;
        }
        
        .detail-row {
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            font-size: 0.95em;
        }
        
        .detail-label {
            color: #666;
            font-weight: 500;
        }
        
        .detail-value {
            color: #333;
            font-weight: 600;
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
                <span class="status-badge" id="status-badge">Sistema Online</span>
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
                <a class="nav-link" id="products-tab" data-bs-toggle="tab" href="#tabProducts" role="tab">
                    🛍️ Produtos
                </a>
            </li>
            <li class="nav-item" role="presentation">
                <a class="nav-link" id="stock-tab" data-bs-toggle="tab" href="#tabStock" role="tab">
                    📦 Estoque
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
                    <div class="col-md-3 mb-3">
                        <div class="card h-100">
                            <div class="card-body text-center">
                                <div class="kpi-value text-primary">0</div>
                                <div class="kpi-label">Total de Produtos</div>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-3 mb-3">
                        <div class="card h-100">
                            <div class="card-body text-center">
                                <div class="kpi-value text-success">R$ 0</div>
                                <div class="kpi-label">Valor Total</div>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-3 mb-3">
                        <div class="card h-100">
                            <div class="card-body text-center">
                                <div class="kpi-value text-info">0</div>
                                <div class="kpi-label">Estoque Total</div>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-3 mb-3">
                        <div class="card h-100">
                            <div class="card-body text-center">
                                <div class="kpi-value text-warning">R$ 0</div>
                                <div class="kpi-label">Preço Médio</div>
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

            <!-- Products Tab -->
            <div class="tab-pane fade" id="tabProducts" role="tabpanel">
                <h4 class="mb-4">🛍️ Gestão de Produtos</h4>
                
                <div class="card mb-4">
                    <div class="card-body">
                        <div class="search-box">
                            <input type="text" id="searchInput" placeholder="🔍 Pesquisar produtos por nome ou código..." style="flex: 1; min-width: 250px;">
                            <button onclick="loadProducts()" class="btn btn-primary">Buscar Produtos</button>
                            <button onclick="clearFilters()" class="btn btn-secondary">Limpar Filtros</button>
                        </div>
                        
                        <div class="filters">
                            <div class="d-flex gap-2 align-items-center">
                                <label>Ordenar por:</label>
                                <select id="sortBy" onchange="applySortAndFilter()">
                                    <option value="name">Nome</option>
                                    <option value="price">Preço</option>
                                    <option value="stock">Estoque</option>
                                </select>
                            </div>
                            <div class="d-flex gap-2 align-items-center">
                                <label>Estoque:</label>
                                <select id="stockFilter" onchange="applySortAndFilter()">
                                    <option value="all">Todos</option>
                                    <option value="low">Baixo (&lt;10)</option>
                                    <option value="medium">Médio (10-50)</option>
                                    <option value="high">Alto (&gt;50)</option>
                                </select>
                            </div>
                        </div>
                    </div>
                </div>

                <div id="productsContainer">
                    <div class="empty-state">
                        <div class="empty-state-icon">📦</div>
                        <div class="empty-state-text">Clique em "Buscar Produtos" para começar</div>
                    </div>
                </div>
            </div>

            <!-- Stock Tab -->
            <div class="tab-pane fade" id="tabStock" role="tabpanel">
                <h4 class="mb-4">📦 Estoque de Componentes</h4>
                <p>A tabela abaixo mostra o estoque atual de cada componente, comparado ao estoque mínimo configurado.</p>
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
        let allProducts = [];
        let statsChart = null;

        function formatLog(log) {
            const levelClass = `log-level-${log.level}`;
            return `<div class="log-entry"><span class="${levelClass}">[${log.timestamp.substring(11, 19)}] [${log.level}]</span> ${log.message}</div>`;
        }

        function updateStatusBadge(status) {
            const badge = document.getElementById('status-badge');
            badge.className = 'status-badge ' + (status === 'ok' ? 'bg-success text-white' : 'bg-warning text-dark');
            badge.textContent = status === 'ok' ? 'Sistema Online' : 'Verificando...';
        }

        function updateStatsKPIs(products) {
            const total = products.length;
            const totalValue = products.reduce((sum, p) => sum + (p.preco || 0) * (p.estoque || 0), 0);
            const totalStock = products.reduce((sum, p) => sum + (p.estoque || 0), 0);
            const avgPrice = total > 0 ? products.reduce((sum, p) => sum + (p.preco || 0), 0) / total : 0;

            const kpis = document.querySelectorAll('#stats-kpis .kpi-value');
            if (kpis.length >= 4) {
                kpis[0].textContent = total;
                kpis[1].textContent = `R$ ${totalValue.toFixed(2)}`;
                kpis[2].textContent = totalStock;
                kpis[3].textContent = `R$ ${avgPrice.toFixed(2)}`;
            }
        }

        function updateStatsChart(products) {
            const ctx = document.getElementById('processingChart');
            if (!ctx) return;
            
            const lowStock = products.filter(p => (p.estoque || 0) < 10).length;
            const mediumStock = products.filter(p => {
                const s = p.estoque || 0;
                return s >= 10 && s <= 50;
            }).length;
            const highStock = products.filter(p => (p.estoque || 0) > 50).length;

            if (statsChart) {
                statsChart.destroy();
            }

            statsChart = new Chart(ctx, {
                type: 'doughnut',
                data: {
                    labels: ['Estoque Baixo', 'Estoque Médio', 'Estoque Alto'],
                    datasets: [{
                        data: [lowStock, mediumStock, highStock],
                        backgroundColor: ['#fee2e2', '#fef3c7', '#d1fae5'],
                        borderColor: ['#991b1b', '#92400e', '#065f46'],
                        borderWidth: 2
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'bottom' }
                    }
                }
            });
        }

        async function loadProducts() {
            const container = document.getElementById('productsContainer');
            const searchTerm = document.getElementById('searchInput').value;
            
            container.innerHTML = '<div class="loading"><div class="loading-spinner"></div><p>Carregando produtos...</p></div>';
            
            try {
                const response = await fetch(`${API_BASE}/produtos?` + new URLSearchParams({
                    pesquisa: searchTerm
                }));
                
                if (!response.ok) throw new Error('Erro ao buscar produtos');
                
                const data = await response.json();
                allProducts = data.data || [];
                
                updateStatsKPIs(allProducts);
                updateStatsChart(allProducts);
                updateStockTable(allProducts);
                applySortAndFilter();
                
            } catch (error) {
                container.innerHTML = `<div class="empty-state"><div class="empty-state-icon">❌</div><div class="empty-state-text">Erro: ${error.message}</div></div>`;
            }
        }

        function applySortAndFilter() {
            const sortBy = document.getElementById('sortBy').value;
            const stockFilter = document.getElementById('stockFilter').value;
            
            let filtered = [...allProducts];
            
            if (stockFilter !== 'all') {
                filtered = filtered.filter(product => {
                    const stock = product.estoque || 0;
                    if (stockFilter === 'low') return stock < 10;
                    if (stockFilter === 'medium') return stock >= 10 && stock <= 50;
                    if (stockFilter === 'high') return stock > 50;
                    return true;
                });
            }
            
            filtered.sort((a, b) => {
                if (sortBy === 'name') return (a.nome || '').localeCompare(b.nome || '');
                if (sortBy === 'price') return (b.preco || 0) - (a.preco || 0);
                if (sortBy === 'stock') return (b.estoque || 0) - (a.estoque || 0);
                return 0;
            });
            
            displayProducts(filtered);
        }

        function displayProducts(products) {
            const container = document.getElementById('productsContainer');
            
            if (products.length === 0) {
                container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">🔍</div><div class="empty-state-text">Nenhum produto encontrado</div></div>';
                return;
            }
            
            const html = '<div class="products-grid">' + products.map(product => {
                const stock = product.estoque || 0;
                const stockClass = stock < 10 ? 'stock-low' : stock <= 50 ? 'stock-medium' : 'stock-high';
                const price = product.preco ? `R$ ${product.preco.toFixed(2)}` : 'N/A';
                
                return `
                    <div class="product-card">
                        <div class="product-id">ID: ${product.id}</div>
                        <div class="product-name">${product.nome || 'Sem nome'}</div>
                        <div class="product-sku">SKU: ${product.codigo || 'N/A'}</div>
                        <div class="product-details">
                            <div class="detail-row">
                                <span class="detail-label">Preço:</span>
                                <span class="detail-value price">${price}</span>
                            </div>
                            <div class="detail-row">
                                <span class="detail-label">Estoque:</span>
                                <span class="stock ${stockClass}">${stock} un</span>
                            </div>
                            <div class="detail-row">
                                <span class="detail-label">Tipo:</span>
                                <span class="detail-value">${product.tipo || 'N/A'}</span>
                            </div>
                            <div class="detail-row">
                                <span class="detail-label">Situação:</span>
                                <span class="detail-value">${product.situacao || 'N/A'}</span>
                            </div>
                        </div>
                    </div>
                `;
            }).join('') + '</div>';

            container.innerHTML = html;
        }

        async function fetchLogs() {
            try {
                const response = await fetch(`${API_BASE}/logs`);
                const data = await response.json();
                const logContainer = document.getElementById('logs-content');
                
                if (data.logs && data.logs.length > 0) {
                    logContainer.innerHTML = data.logs.slice(-20).map(formatLog).join('');
                    logContainer.scrollTop = logContainer.scrollHeight;
                }
            } catch (error) {
                console.error('Erro ao buscar logs:', error);
            }
        }

        async function fetchKits() {
            try {
                const response = await fetch(`${API_BASE}/kits`);
                const data = await response.json();
                const tbody = document.getElementById('kits-table-body');
                tbody.innerHTML = '';
                
                if (data.error) {
                    tbody.innerHTML = `<tr><td colspan="3" class="text-center text-danger">Erro: ${data.error}</td></tr>`;
                    return;
                }
                
                if (!data.kits || data.kits.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="3" class="text-center">Nenhum kit encontrado</td></tr>';
                    return;
                }
                
                data.kits.forEach(kit => {
                    const componentsList = kit.componentes.map(c => 
                        `${c.nome} (${c.sku}) x${c.quantidade}`
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
            }
        }

        function clearFilters() {
            document.getElementById('searchInput').value = '';
            document.getElementById('sortBy').value = 'name';
            document.getElementById('stockFilter').value = 'all';
            document.getElementById('productsContainer').innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">📦</div>
                    <div class="empty-state-text">Clique em "Buscar Produtos" para começar</div>
                </div>
            `;
            allProducts = [];
            updateStatsKPIs([]);
        }

        document.getElementById('searchInput').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') loadProducts();
        });

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
            updateStatusBadge('ok');
            fetchLogs();
            fetchKits();
            setInterval(fetchLogs, 5000);
        }

        document.addEventListener('DOMContentLoaded', initDashboard);
        
        // A função updateStockTable estava fora do template, causando SyntaxError.
        // Ela foi movida para dentro do template, como instruído.
        function updateStockTable(products) {
            const tbody = document.getElementById('stock-table-body');
            tbody.innerHTML = '';
            
            if (products.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" class="text-center">Nenhum dado disponível</td></tr>';
                return;
            }
            
            products.forEach(product => {
                const stock = product.estoque || 0;
                const minStock = 10;
                const isAlert = stock < minStock;
                const rowClass = isAlert ? 'table-danger' : '';
                
                tbody.innerHTML += `
                    <tr class="${rowClass}">
                        <td>${product.codigo || 'N/A'}</td>
                        <td>${product.nome || 'Sem nome'}</td>
                        <td>${stock}</td>
                        <td>${minStock}</td>
                        <td>${isAlert ? '🚨 ABAIXO' : 'OK'}</td>
                    </tr>
                `;
            });
        }
    </script>
</body>
</html>
"""

# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":


    logger = logging.getLogger(__name__)

    logger.info("🚀 Iniciando aplicação Bling...")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)