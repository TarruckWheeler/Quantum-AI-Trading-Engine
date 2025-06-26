"""
Cryptocurrency Trading Engine for Quantum-AI System
==================================================
Handles multi-exchange crypto trading, DeFi protocols, MEV strategies,
and cross-chain arbitrage with ultra-low latency.
"""

import asyncio
import aiohttp
import websockets
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional, Any, Union
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import time
import json
import hmac
import hashlib
from collections import defaultdict, deque
import ccxt.async_support as ccxt
from web3 import Web3, AsyncWeb3
from web3.providers import WebsocketProvider
import eth_account
from eth_account import Account
from flashbots import flashbot
import torch
import torch.nn as nn
from transformers import AutoTokenizer, AutoModelForSequenceClassification
import redis.asyncio as aioredis
from aiokafka import AIOKafkaProducer
import msgpack
import orjson
import logging

logger = logging.getLogger(__name__)

# ===========================
# Data Structures
# ===========================

@dataclass
class CryptoTick:
    """Cryptocurrency market data with nanosecond precision"""
    timestamp_ns: int
    exchange: str
    symbol: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    last_price: Decimal
    volume_24h: Decimal
    funding_rate: Optional[Decimal] = None
    open_interest: Optional[Decimal] = None
    
    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid
    
    @property
    def spread_bps(self) -> Decimal:
        mid = (self.bid + self.ask) / 2
        return (self.spread / mid) * 10000

@dataclass
class DeFiPosition:
    """DeFi protocol position tracking"""
    protocol: str
    pool: str
    token0: str
    token1: str
    amount0: Decimal
    amount1: Decimal
    liquidity_tokens: Decimal
    entry_price_ratio: Decimal
    current_price_ratio: Decimal
    fees_earned: Decimal
    impermanent_loss: Decimal
    
@dataclass
class MEVOpportunity:
    """Maximum Extractable Value opportunity"""
    type: str  # 'arbitrage', 'liquidation', 'sandwich'
    profit_estimate: Decimal
    gas_cost: Decimal
    success_probability: float
    target_block: int
    transactions: List[Dict[str, Any]]
    deadline_ns: int

# ===========================
# Multi-Exchange Crypto Handler
# ===========================

class MultiExchangeCryptoHandler:
    """
    Handles connections to multiple crypto exchanges with
    ultra-low latency order routing and arbitrage detection
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.exchanges = {}
        self.orderbooks = defaultdict(dict)
        self.websockets = {}
        self.best_prices = {}
        self.arbitrage_opportunities = deque(maxlen=1000)
        
        # Initialize exchanges
        self.exchange_configs = {
            'binance': {
                'apiKey': config['binance_api_key'],
                'secret': config['binance_secret'],
                'enableRateLimit': True,
                'options': {'defaultType': 'future'}
            },
            'coinbase': {
                'apiKey': config['coinbase_api_key'],
                'secret': config['coinbase_secret'],
                'password': config['coinbase_passphrase'],
                'enableRateLimit': True
            },
            'ftx': {
                'apiKey': config['ftx_api_key'],
                'secret': config['ftx_secret'],
                'enableRateLimit': True
            },
            'kraken': {
                'apiKey': config['kraken_api_key'],
                'secret': config['kraken_secret'],
                'enableRateLimit': True
            }
        }
        
    async def initialize(self):
        """Initialize all exchange connections"""
        # Create exchange instances
        for exchange_id, config in self.exchange_configs.items():
            exchange_class = getattr(ccxt, exchange_id)
            self.exchanges[exchange_id] = exchange_class(config)
        
        # Start WebSocket connections for real-time data
        await self._initialize_websockets()
        
        # Start arbitrage monitor
        asyncio.create_task(self._monitor_arbitrage())
        
        logger.info("Multi-exchange crypto handler initialized")
        
    async def _initialize_websockets(self):
        """Initialize WebSocket connections for each exchange"""
        ws_endpoints = {
            'binance': 'wss://fstream.binance.com/ws',
            'coinbase': 'wss://ws-feed.exchange.coinbase.com',
            'kraken': 'wss://ws.kraken.com'
        }
        
        for exchange, endpoint in ws_endpoints.items():
            asyncio.create_task(self._handle_websocket(exchange, endpoint))
            
    async def _handle_websocket(self, exchange: str, endpoint: str):
        """Handle WebSocket connection for an exchange"""
        while True:
            try:
                async with websockets.connect(endpoint) as ws:
                    self.websockets[exchange] = ws
                    
                    # Subscribe to orderbook updates
                    if exchange == 'binance':
                        subscribe_msg = {
                            "method": "SUBSCRIBE",
                            "params": [
                                "btcusdt@depth20@100ms",
                                "ethusdt@depth20@100ms"
                            ],
                            "id": 1
                        }
                    elif exchange == 'coinbase':
                        subscribe_msg = {
                            "type": "subscribe",
                            "channels": [
                                {"name": "level2", "product_ids": ["BTC-USD", "ETH-USD"]}
                            ]
                        }
                    
                    await ws.send(json.dumps(subscribe_msg))
                    
                    # Process messages
                    async for message in ws:
                        await self._process_ws_message(exchange, message)
                        
            except Exception as e:
                logger.error(f"WebSocket error for {exchange}: {e}")
                await asyncio.sleep(5)  # Reconnect after 5 seconds
                
    async def _process_ws_message(self, exchange: str, message: str):
        """Process WebSocket message with nanosecond precision"""
        timestamp_ns = time.perf_counter_ns()
        data = json.loads(message)
        
        if exchange == 'binance' and 'stream' in data:
            stream_data = data['data']
            symbol = stream_data['s'].lower()
            
            # Update orderbook
            self.orderbooks[exchange][symbol] = {
                'bids': [(Decimal(p), Decimal(q)) for p, q in stream_data['bids']],
                'asks': [(Decimal(p), Decimal(q)) for p, q in stream_data['asks']],
                'timestamp_ns': timestamp_ns
            }
            
        elif exchange == 'coinbase' and data.get('type') == 'l2update':
            symbol = data['product_id'].lower().replace('-', '')
            
            # Update orderbook incrementally
            if symbol not in self.orderbooks[exchange]:
                self.orderbooks[exchange][symbol] = {'bids': {}, 'asks': {}}
                
            for change in data['changes']:
                side, price, size = change
                price = Decimal(price)
                size = Decimal(size)
                
                book_side = self.orderbooks[exchange][symbol]['bids' if side == 'buy' else 'asks']
                if size == 0:
                    book_side.pop(price, None)
                else:
                    book_side[price] = size
                    
        # Check for arbitrage opportunities
        await self._check_arbitrage(symbol)
        
    async def _check_arbitrage(self, symbol: str):
        """Check for arbitrage opportunities across exchanges"""
        best_bid = Decimal('0')
        best_bid_exchange = None
        best_bid_size = Decimal('0')
        
        best_ask = Decimal('999999999')
        best_ask_exchange = None
        best_ask_size = Decimal('0')
        
        # Find best bid and ask across all exchanges
        for exchange, books in self.orderbooks.items():
            if symbol in books:
                book = books[symbol]
                
                if isinstance(book.get('bids'), list) and book['bids']:
                    bid_price = book['bids'][0][0]
                    bid_size = book['bids'][0][1]
                    if bid_price > best_bid:
                        best_bid = bid_price
                        best_bid_exchange = exchange
                        best_bid_size = bid_size
                        
                if isinstance(book.get('asks'), list) and book['asks']:
                    ask_price = book['asks'][0][0]
                    ask_size = book['asks'][0][1]
                    if ask_price < best_ask:
                        best_ask = ask_price
                        best_ask_exchange = exchange
                        best_ask_size = ask_size
                        
        # Calculate arbitrage profit
        if best_bid > best_ask and best_bid_exchange and best_ask_exchange:
            size = min(best_bid_size, best_ask_size)
            gross_profit = (best_bid - best_ask) * size
            
            # Estimate fees (0.1% taker fee on both sides)
            fee_cost = (best_bid * size * Decimal('0.001')) + (best_ask * size * Decimal('0.001'))
            net_profit = gross_profit - fee_cost
            
            if net_profit > Decimal('10'):  # Minimum $10 profit
                opportunity = {
                    'timestamp_ns': time.perf_counter_ns(),
                    'symbol': symbol,
                    'buy_exchange': best_ask_exchange,
                    'sell_exchange': best_bid_exchange,
                    'buy_price': best_ask,
                    'sell_price': best_bid,
                    'size': size,
                    'gross_profit': gross_profit,
                    'net_profit': net_profit,
                    'profit_bps': (net_profit / (best_ask * size)) * 10000
                }
                
                self.arbitrage_opportunities.append(opportunity)
                await self._execute_arbitrage(opportunity)
                
    async def _execute_arbitrage(self, opportunity: Dict[str, Any]):
        """Execute arbitrage trade with optimal routing"""
        try:
            # Place simultaneous orders
            buy_task = self.exchanges[opportunity['buy_exchange']].create_order(
                symbol=opportunity['symbol'].upper(),
                type='limit',
                side='buy',
                amount=float(opportunity['size']),
                price=float(opportunity['buy_price'])
            )
            
            sell_task = self.exchanges[opportunity['sell_exchange']].create_order(
                symbol=opportunity['symbol'].upper(),
                type='limit',
                side='sell',
                amount=float(opportunity['size']),
                price=float(opportunity['sell_price'])
            )
            
            # Execute both orders simultaneously
            buy_order, sell_order = await asyncio.gather(buy_task, sell_task)
            
            logger.info(f"Arbitrage executed: {opportunity['net_profit']} profit on {opportunity['symbol']}")
            
        except Exception as e:
            logger.error(f"Arbitrage execution failed: {e}")
            
    async def get_best_execution_price(self, symbol: str, side: str, amount: Decimal) -> Dict[str, Any]:
        """Get best execution price across all exchanges"""
        best_price = Decimal('999999999') if side == 'buy' else Decimal('0')
        best_exchange = None
        
        for exchange_id, exchange in self.exchanges.items():
            try:
                orderbook = await exchange.fetch_order_book(symbol.upper())
                
                if side == 'buy':
                    # Calculate weighted average price for market buy
                    remaining = amount
                    total_cost = Decimal('0')
                    
                    for ask_price, ask_size in orderbook['asks']:
                        ask_price = Decimal(str(ask_price))
                        ask_size = Decimal(str(ask_size))
                        
                        fill_size = min(remaining, ask_size)
                        total_cost += ask_price * fill_size
                        remaining -= fill_size
                        
                        if remaining <= 0:
                            break
                            
                    if remaining <= 0:
                        avg_price = total_cost / amount
                        if avg_price < best_price:
                            best_price = avg_price
                            best_exchange = exchange_id
                            
            except Exception as e:
                logger.error(f"Error fetching orderbook from {exchange_id}: {e}")
                
        return {
            'exchange': best_exchange,
            'price': best_price,
            'symbol': symbol,
            'side': side,
            'amount': amount
        }

# ===========================
# DeFi Protocol Handler
# ===========================

class DeFiProtocolHandler:
    """
    Handles DeFi protocol interactions including
    yield farming, liquidity provision, and lending
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.w3 = AsyncWeb3(WebsocketProvider(config['eth_node_ws']))
        self.account = Account.from_key(config['private_key'])
        self.flashbot_provider = flashbot(self.w3, self.account)
        
        # Protocol addresses
        self.protocols = {
            'uniswap_v3': {
                'router': '0xE592427A0AEce92De3Edee1F18E0157C05861564',
                'factory': '0x1F98431c8aD98523631AE4a59f267346ea31F984',
                'positions_nft': '0xC36442b4a4522E871399CD717aBDD847Ab11FE88'
            },
            'aave_v3': {
                'pool': '0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2',
                'oracle': '0x54586bE62E3c3580375aE3723C145253060Ca0C2'
            },
            'compound_v3': {
                'comet': '0xc3d688B66703497DAA19211EEdff47f25384cdc3'
            }
        }
        
        # Load ABIs
        self.abis = self._load_abis()
        
    def _load_abis(self) -> Dict[str, Any]:
        """Load protocol ABIs"""
        # In production, load from files
        return {
            'uniswap_router': [...],  # Uniswap V3 Router ABI
            'aave_pool': [...],       # Aave V3 Pool ABI
            'compound_comet': [...]   # Compound V3 Comet ABI
        }
        
    async def find_yield_opportunities(self) -> List[Dict[str, Any]]:
        """Find best yield opportunities across DeFi protocols"""
        opportunities = []
        
        # Check Aave lending rates
        aave_rates = await self._get_aave_rates()
        for asset, rate in aave_rates.items():
            if rate['supply_apy'] > Decimal('5'):  # 5% APY threshold
                opportunities.append({
                    'protocol': 'aave_v3',
                    'type': 'lending',
                    'asset': asset,
                    'apy': rate['supply_apy'],
                    'tvl': rate['total_supplied']
                })
                
        # Check Uniswap V3 pool returns
        uni_pools = await self._get_uniswap_pools()
        for pool in uni_pools:
            if pool['fee_apy'] > Decimal('10'):  # 10% APY threshold
                opportunities.append({
                    'protocol': 'uniswap_v3',
                    'type': 'liquidity_provision',
                    'pair': f"{pool['token0']}/{pool['token1']}",
                    'apy': pool['fee_apy'],
                    'tvl': pool['tvl'],
                    'volume_24h': pool['volume_24h']
                })
                
        # Sort by APY
        opportunities.sort(key=lambda x: x['apy'], reverse=True)
        
        return opportunities
        
    async def _get_aave_rates(self) -> Dict[str, Dict[str, Decimal]]:
        """Get current Aave lending rates"""
        pool_contract = self.w3.eth.contract(
            address=self.protocols['aave_v3']['pool'],
            abi=self.abis['aave_pool']
        )
        
        rates = {}
        assets = ['USDC', 'USDT', 'DAI', 'WETH', 'WBTC']
        
        for asset in assets:
            # Get reserve data
            reserve_data = await pool_contract.functions.getReserveData(asset).call()
            
            # Calculate APY from ray units (1e27)
            supply_rate = Decimal(reserve_data[3]) / Decimal(1e27)
            supply_apy = ((1 + supply_rate) ** 365 - 1) * 100
            
            rates[asset] = {
                'supply_apy': supply_apy,
                'borrow_apy': Decimal(reserve_data[4]) / Decimal(1e27) * 365 * 100,
                'total_supplied': Decimal(reserve_data[0]) / Decimal(1e18)
            }
            
        return rates
        
    async def _get_uniswap_pools(self) -> List[Dict[str, Any]]:
        """Get Uniswap V3 pool data"""
        # Query The Graph for pool data
        query = """
        {
            pools(first: 20, orderBy: totalValueLockedUSD, orderDirection: desc) {
                id
                token0 { symbol }
                token1 { symbol }
                feeTier
                totalValueLockedUSD
                volumeUSD
                feesUSD
            }
        }
        """
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                'https://api.thegraph.com/subgraphs/name/uniswap/uniswap-v3',
                json={'query': query}
            ) as response:
                data = await response.json()
                
        pools = []
        for pool in data['data']['pools']:
            # Calculate fee APY
            daily_fees = Decimal(pool['feesUSD'])
            tvl = Decimal(pool['totalValueLockedUSD'])
            fee_apy = (daily_fees / tvl) * 365 * 100 if tvl > 0 else Decimal('0')
            
            pools.append({
                'id': pool['id'],
                'token0': pool['token0']['symbol'],
                'token1': pool['token1']['symbol'],
                'fee_tier': int(pool['feeTier']) / 10000,
                'fee_apy': fee_apy,
                'tvl': tvl,
                'volume_24h': Decimal(pool['volumeUSD'])
            })
            
        return pools
        
    async def execute_defi_strategy(self, strategy: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a DeFi strategy"""
        if strategy['type'] == 'liquidity_provision':
            return await self._provide_liquidity(strategy)
        elif strategy['type'] == 'lending':
            return await self._lend_asset(strategy)
        elif strategy['type'] == 'yield_farming':
            return await self._yield_farm(strategy)
            
    async def _provide_liquidity(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Provide liquidity to Uniswap V3"""
        router = self.w3.eth.contract(
            address=self.protocols['uniswap_v3']['router'],
            abi=self.abis['uniswap_router']
        )
        
        # Calculate optimal range based on volatility
        price_range = await self._calculate_optimal_range(
            params['token0'],
            params['token1'],
            params['volatility']
        )
        
        # Build transaction
        tx = await router.functions.mint({
            'token0': params['token0_address'],
            'token1': params['token1_address'],
            'fee': params['fee_tier'],
            'tickLower': price_range['tick_lower'],
            'tickUpper': price_range['tick_upper'],
            'amount0Desired': int(params['amount0'] * 1e18),
            'amount1Desired': int(params['amount1'] * 1e18),
            'amount0Min': 0,
            'amount1Min': 0,
            'recipient': self.account.address,
            'deadline': int(time.time() + 300)
        }).build_transaction({
            'from': self.account.address,
            'gas': 500000,
            'gasPrice': await self._get_optimal_gas_price(),
            'nonce': await self.w3.eth.get_transaction_count(self.account.address)
        })
        
        # Sign and send transaction
        signed_tx = self.account.sign_transaction(tx)
        tx_hash = await self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        
        return {
            'tx_hash': tx_hash.hex(),
            'position_id': await self._get_position_id(tx_hash),
            'estimated_apy': params['estimated_apy']
        }

# ===========================
# MEV (Maximum Extractable Value) Bot
# ===========================

class MEVBot:
    """
    Sophisticated MEV bot for arbitrage, liquidations,
    and sandwich attacks with flashloan integration
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.w3 = AsyncWeb3(WebsocketProvider(config['eth_node_ws']))
        self.account = Account.from_key(config['private_key'])
        self.flashbot_provider = flashbot(self.w3, self.account, config['flashbots_relay'])
        
        # MEV strategies
        self.strategies = {
            'arbitrage': self._execute_arbitrage,
            'liquidation': self._execute_liquidation,
            'sandwich': self._execute_sandwich
        }
        
        # Mempool monitor
        self.pending_txs = deque(maxlen=10000)
        self.profitable_opportunities = asyncio.Queue()
        
    async def start(self):
        """Start MEV bot"""
        # Start mempool monitoring
        asyncio.create_task(self._monitor_mempool())
        
        # Start opportunity processor
        asyncio.create_task(self._process_opportunities())
        
        # Start block listener
        asyncio.create_task(self._listen_blocks())
        
        logger.info("MEV bot started")
        
    async def _monitor_mempool(self):
        """Monitor mempool for MEV opportunities"""
        async def handle_pending_tx(tx_hash):
            try:
                tx = await self.w3.eth.get_transaction(tx_hash)
                self.pending_txs.append(tx)
                
                # Analyze transaction for MEV opportunities
                opportunities = await self._analyze_transaction(tx)
                for opportunity in opportunities:
                    await self.profitable_opportunities.put(opportunity)
                    
            except Exception as e:
                logger.error(f"Error handling pending tx: {e}")
                
        # Subscribe to pending transactions
        pending_filter = await self.w3.eth.filter('pending')
        while True:
            try:
                for tx_hash in await pending_filter.get_new_entries():
                    asyncio.create_task(handle_pending_tx(tx_hash))
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"Mempool monitoring error: {e}")
                
    async def _analyze_transaction(self, tx: Dict[str, Any]) -> List[MEVOpportunity]:
        """Analyze transaction for MEV opportunities"""
        opportunities = []
        
        # Decode transaction input
        if tx['to'] and len(tx['input']) > 10:
            # Check if it's a DEX trade
            if await self._is_dex_trade(tx):
                # Check for sandwich opportunity
                sandwich_opp = await self._check_sandwich_opportunity(tx)
                if sandwich_opp:
                    opportunities.append(sandwich_opp)
                    
                # Check for arbitrage after trade
                arb_opp = await self._check_arbitrage_after_trade(tx)
                if arb_opp:
                    opportunities.append(arb_opp)
                    
        return opportunities
        
    async def _check_sandwich_opportunity(self, victim_tx: Dict[str, Any]) -> Optional[MEVOpportunity]:
        """Check if transaction can be sandwiched profitably"""
        # Decode swap parameters
        swap_data = await self._decode_swap_data(victim_tx)
        if not swap_data:
            return None
            
        # Calculate sandwich profit
        token_in = swap_data['token_in']
        token_out = swap_data['token_out']
        amount_in = swap_data['amount_in']
        
        # Get current reserves
        reserves = await self._get_pool_reserves(token_in, token_out)
        
        # Calculate optimal sandwich size
        sandwich_amount = self._calculate_optimal_sandwich_size(
            amount_in,
            reserves['reserve_in'],
            reserves['reserve_out']
        )
        
        # Estimate profit
        front_run_out = self._get_amount_out(
            sandwich_amount,
            reserves['reserve_in'],
            reserves['reserve_out']
        )
        
        # After victim trade
        new_reserve_in = reserves['reserve_in'] + sandwich_amount + amount_in
        new_reserve_out = reserves['reserve_out'] - front_run_out - swap_data['min_amount_out']
        
        # Back run to sell
        back_run_out = self._get_amount_out(
            front_run_out,
            new_reserve_out,
            new_reserve_in
        )
        
        profit = back_run_out - sandwich_amount
        gas_cost = Decimal('0.01')  # Estimated gas cost in ETH
        
        if profit > gas_cost * Decimal('2'):  # 2x gas cost minimum
            return MEVOpportunity(
                type='sandwich',
                profit_estimate=profit,
                gas_cost=gas_cost,
                success_probability=0.7,
                target_block=await self.w3.eth.block_number + 1,
                transactions=[
                    self._build_front_run_tx(swap_data, sandwich_amount),
                    victim_tx,
                    self._build_back_run_tx(swap_data, front_run_out)
                ],
                deadline_ns=time.perf_counter_ns() + 12_000_000_000  # 12 second deadline
            )
            
        return None
        
    def _calculate_optimal_sandwich_size(self, victim_amount: Decimal,
                                       reserve_in: Decimal,
                                       reserve_out: Decimal) -> Decimal:
        """Calculate optimal sandwich attack size"""
        # Optimal sandwich formula derived from Uniswap V2 math
        # This maximizes profit while accounting for price impact
        k = reserve_in * reserve_out
        optimal = (np.sqrt(float(k * (victim_amount + reserve_in))) - float(reserve_in))
        return Decimal(str(optimal))
        
    def _get_amount_out(self, amount_in: Decimal,
                       reserve_in: Decimal,
                       reserve_out: Decimal) -> Decimal:
        """Calculate output amount for Uniswap V2 style AMM"""
        amount_in_with_fee = amount_in * Decimal('997')  # 0.3% fee
        numerator = amount_in_with_fee * reserve_out
        denominator = (reserve_in * Decimal('1000')) + amount_in_with_fee
        return numerator / denominator
        
    async def _execute_sandwich(self, opportunity: MEVOpportunity) -> Dict[str, Any]:
        """Execute sandwich attack using Flashbots"""
        bundle = []
        
        for tx_data in opportunity.transactions:
            if isinstance(tx_data, dict) and 'hash' not in tx_data:
                # Build our transaction
                tx = await self._build_transaction(tx_data)
                signed_tx = self.account.sign_transaction(tx)
                bundle.append({'signedTransaction': signed_tx.rawTransaction})
            else:
                # Include victim transaction
                bundle.append({'hash': tx_data['hash']})
                
        # Send bundle to Flashbots
        try:
            result = await self.flashbot_provider.send_bundle(
                bundle,
                target_block_number=opportunity.target_block
            )
            
            # Simulate bundle
            simulation = await self.flashbot_provider.simulate_bundle(
                bundle,
                target_block_number=opportunity.target_block
            )
            
            if simulation['totalGasUsed'] > 0:
                return {
                    'success': True,
                    'bundle_hash': result['bundleHash'],
                    'profit': opportunity.profit_estimate,
                    'gas_used': simulation['totalGasUsed']
                }
                
        except Exception as e:
            logger.error(f"Sandwich execution failed: {e}")
            
        return {'success': False}

# ===========================
# Crypto ML Predictor
# ===========================

class CryptoMLPredictor:
    """
    Advanced ML models specifically for cryptocurrency prediction
    including on-chain analytics and social sentiment
    """
    
    def __init__(self):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        
        # Load pre-trained transformer for crypto price prediction
        self.price_transformer = self._build_price_transformer()
        
        # Sentiment analysis model
        self.sentiment_model = AutoModelForSequenceClassification.from_pretrained(
            'ElKulako/cryptobert',
            num_labels=3  # Bullish, Neutral, Bearish
        )
        self.sentiment_tokenizer = AutoTokenizer.from_pretrained('ElKulako/cryptobert')
        
        # On-chain analytics model
        self.onchain_model = self._build_onchain_model()
        
    def _build_price_transformer(self) -> nn.Module:
        """Build transformer model for price prediction"""
        class CryptoPriceTransformer(nn.Module):
            def __init__(self, d_model=512, nhead=8, num_layers=6):
                super().__init__()
                self.d_model = d_model
                
                # Input embeddings
                self.price_embedding = nn.Linear(7, d_model)  # OHLCV + volume + market cap
                self.position_encoding = self._create_position_encoding(1000, d_model)
                
                # Transformer
                encoder_layer = nn.TransformerEncoderLayer(
                    d_model=d_model,
                    nhead=nhead,
                    dim_feedforward=2048,
                    dropout=0.1,
                    activation='gelu'
                )
                self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)
                
                # Output heads
                self.price_head = nn.Sequential(
                    nn.Linear(d_model, 256),
                    nn.ReLU(),
                    nn.Dropout(0.1),
                    nn.Linear(256, 64),
                    nn.ReLU(),
                    nn.Linear(64, 1)  # Next price prediction
                )
                
                self.volatility_head = nn.Sequential(
                    nn.Linear(d_model, 128),
                    nn.ReLU(),
                    nn.Linear(128, 1)  # Volatility prediction
                )
                
            def _create_position_encoding(self, max_len, d_model):
                pe = torch.zeros(max_len, d_model)
                position = torch.arange(0, max_len).unsqueeze(1).float()
                
                div_term = torch.exp(torch.arange(0, d_model, 2).float() *
                                   -(np.log(10000.0) / d_model))
                
                pe[:, 0::2] = torch.sin(position * div_term)
                pe[:, 1::2] = torch.cos(position * div_term)
                
                return pe.unsqueeze(0)
                
            def forward(self, x):
                # x shape: (batch, seq_len, features)
                batch_size, seq_len, _ = x.shape
                
                # Embed prices
                x = self.price_embedding(x)
                
                # Add position encoding
                x = x + self.position_encoding[:, :seq_len, :].to(x.device)
                
                # Transformer expects (seq_len, batch, features)
                x = x.transpose(0, 1)
                x = self.transformer(x)
                
                # Use last timestep for prediction
                x = x[-1]  # (batch, d_model)
                
                price_pred = self.price_head(x)
                vol_pred = self.volatility_head(x)
                
                return price_pred, vol_pred
                
        return CryptoPriceTransformer().to(self.device)
        
    def _build_onchain_model(self) -> nn.Module:
        """Build model for on-chain analytics"""
        class OnChainAnalyticsModel(nn.Module):
            def __init__(self, input_dim=50):
                super().__init__()
                
                self.feature_extractor = nn.Sequential(
                    nn.Linear(input_dim, 256),
                    nn.BatchNorm1d(256),
                    nn.ReLU(),
                    nn.Dropout(0.3),
                    
                    nn.Linear(256, 128),
                    nn.BatchNorm1d(128),
                    nn.ReLU(),
                    nn.Dropout(0.2),
                    
                    nn.Linear(128, 64),
                    nn.BatchNorm1d(64),
                    nn.ReLU()
                )
                
                # Outputs
                self.whale_activity = nn.Linear(64, 1)  # Whale accumulation score
                self.network_health = nn.Linear(64, 1)  # Network health score
                self.defi_momentum = nn.Linear(64, 1)   # DeFi activity momentum
                
            def forward(self, x):
                features = self.feature_extractor(x)
                
                whale_score = torch.sigmoid(self.whale_activity(features))
                health_score = torch.sigmoid(self.network_health(features))
                defi_score = torch.sigmoid(self.defi_momentum(features))
                
                return {
                    'whale_activity': whale_score,
                    'network_health': health_score,
                    'defi_momentum': defi_score
                }
                
        return OnChainAnalyticsModel().to(self.device)
        
    async def predict_price_movement(self, 
                                   symbol: str,
                                   timeframe: str = '1h',
                                   horizon: int = 24) -> Dict[str, Any]:
        """Predict crypto price movement using ensemble of models"""
        
        # Get historical data
        ohlcv_data = await self._get_ohlcv_data(symbol, timeframe, limit=1000)
        
        # Get on-chain data
        onchain_data = await self._get_onchain_data(symbol)
        
        # Get social sentiment
        sentiment_score = await self._analyze_social_sentiment(symbol)
        
        # Prepare features for transformer
        price_features = self._prepare_price_features(ohlcv_data)
        price_tensor = torch.FloatTensor(price_features).unsqueeze(0).to(self.device)
        
        # Price prediction
        with torch.no_grad():
            price_pred, vol_pred = self.price_transformer(price_tensor)
            
        # On-chain prediction
        onchain_features = self._prepare_onchain_features(onchain_data)
        onchain_tensor = torch.FloatTensor(onchain_features).unsqueeze(0).to(self.device)
        
        with torch.no_grad():
            onchain_scores = self.onchain_model(onchain_tensor)
            
        # Combine predictions
        current_price = ohlcv_data[-1]['close']
        predicted_price = current_price * (1 + price_pred.item())
        
        confidence = self._calculate_prediction_confidence(
            vol_pred.item(),
            sentiment_score,
            onchain_scores
        )
        
        return {
            'symbol': symbol,
            'current_price': current_price,
            'predicted_price': predicted_price,
            'predicted_change_pct': price_pred.item() * 100,
            'predicted_volatility': vol_pred.item(),
            'confidence': confidence,
            'sentiment_score': sentiment_score,
            'whale_activity': onchain_scores['whale_activity'].item(),
            'network_health': onchain_scores['network_health'].item(),
            'defi_momentum': onchain_scores['defi_momentum'].item(),
            'timeframe': timeframe,
            'horizon_hours': horizon
        }
        
    async def _analyze_social_sentiment(self, symbol: str) -> float:
        """Analyze social media sentiment for crypto"""
        # Fetch recent tweets/posts about the symbol
        texts = await self._fetch_social_data(symbol, limit=100)
        
        if not texts:
            return 0.5  # Neutral
            
        sentiments = []
        
        for text in texts:
            inputs = self.sentiment_tokenizer(
                text,
                padding=True,
                truncation=True,
                max_length=128,
                return_tensors='pt'
            ).to(self.device)
            
            with torch.no_grad():
                outputs = self.sentiment_model(**inputs)
                probs = torch.softmax(outputs.logits, dim=-1)
                
                # Convert to sentiment score: 0 (bearish) to 1 (bullish)
                sentiment = probs[0][2].item() - probs[0][0].item()  # Bullish - Bearish
                sentiment = (sentiment + 1) / 2  # Normalize to [0, 1]
                sentiments.append(sentiment)
                
        return np.mean(sentiments)
        
    def _calculate_prediction_confidence(self,
                                       predicted_volatility: float,
                                       sentiment_score: float,
                                       onchain_scores: Dict[str, torch.Tensor]) -> float:
        """Calculate overall prediction confidence"""
        # Lower volatility = higher confidence
        vol_confidence = 1 / (1 + predicted_volatility)
        
        # Strong sentiment (far from 0.5) = higher confidence
        sentiment_confidence = abs(sentiment_score - 0.5) * 2
        
        # Strong on-chain signals = higher confidence
        onchain_confidence = np.mean([
            onchain_scores['whale_activity'].item(),
            onchain_scores['network_health'].item(),
            onchain_scores['defi_momentum'].item()
        ])
        
        # Weighted average
        confidence = (
            vol_confidence * 0.3 +
            sentiment_confidence * 0.3 +
            onchain_confidence * 0.4
        )
        
        return min(max(confidence, 0), 1)  # Clamp to [0, 1]

# ===========================
# Main Crypto Trading System
# ===========================

class CryptoTradingSystem:
    """
    Main system orchestrating all crypto trading components
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # Initialize components
        self.exchange_handler = MultiExchangeCryptoHandler(config)
        self.defi_handler = DeFiProtocolHandler(config)
        self.mev_bot = MEVBot(config)
        self.ml_predictor = CryptoMLPredictor()
        
        # Trading state
        self.positions = {}
        self.performance_metrics = defaultdict(float)
        
    async def initialize(self):
        """Initialize all crypto trading components"""
        await self.exchange_handler.initialize()
        await self.mev_bot.start()
        
        logger.info("Crypto trading system initialized")
        
    async def run(self):
        """Main trading loop"""
        tasks = [
            asyncio.create_task(self._run_spot_trading()),
            asyncio.create_task(self._run_defi_strategies()),
            asyncio.create_task(self._run_mev_strategies()),
            asyncio.create_task(self._monitor_performance())
        ]
        
        await asyncio.gather(*tasks)
        
    async def _run_spot_trading(self):
        """Run spot and derivatives trading"""
        symbols = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'AVAX/USDT']
        
        while True:
            try:
                for symbol in symbols:
                    # Get ML prediction
                    prediction = await self.ml_predictor.predict_price_movement(
                        symbol.replace('/', ''),
                        timeframe='5m',
                        horizon=1
                    )
                    
                    # Generate trading signal
                    signal = self._generate_trading_signal(prediction)
                    
                    if signal:
                        await self._execute_trade(signal)
                        
                await asyncio.sleep(5)  # Check every 5 seconds
                
            except Exception as e:
                logger.error(f"Spot trading error: {e}")
                await asyncio.sleep(30)
                
    def _generate_trading_signal(self, prediction: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Generate trading signal from ML prediction"""
        confidence_threshold = 0.7
        min_profit_pct = 0.5
        
        if prediction['confidence'] < confidence_threshold:
            return None
            
        predicted_change = prediction['predicted_change_pct']
        
        if abs(predicted_change) < min_profit_pct:
            return None
            
        # Check on-chain signals
        if (prediction['whale_activity'] > 0.7 and 
            prediction['network_health'] > 0.6 and
            predicted_change > 0):
            # Strong bullish signal
            return {
                'symbol': prediction['symbol'],
                'side': 'buy',
                'confidence': prediction['confidence'],
                'target_profit_pct': predicted_change,
                'stop_loss_pct': -1.0,
                'size_pct': min(prediction['confidence'] * 0.1, 0.05)  # Max 5% of capital
            }
        elif (prediction['whale_activity'] < 0.3 and 
              predicted_change < 0):
            # Strong bearish signal
            return {
                'symbol': prediction['symbol'],
                'side': 'sell',
                'confidence': prediction['confidence'],
                'target_profit_pct': abs(predicted_change),
                'stop_loss_pct': -1.0,
                'size_pct': min(prediction['confidence'] * 0.1, 0.05)
            }
            
        return None
        
    async def _execute_trade(self, signal: Dict[str, Any]):
        """Execute crypto trade with optimal routing"""
        # Get best execution price across exchanges
        best_execution = await self.exchange_handler.get_best_execution_price(
            signal['symbol'],
            signal['side'],
            Decimal('10000')  # $10k order size
        )
        
        if not best_execution['exchange']:
            logger.warning(f"No exchange available for {signal['symbol']}")
            return
            
        # Place order on best exchange
        exchange = self.exchange_handler.exchanges[best_execution['exchange']]
        
        try:
            order = await exchange.create_order(
                symbol=signal['symbol'],
                type='limit',
                side=signal['side'],
                amount=float(Decimal('10000') / best_execution['price']),
                price=float(best_execution['price'])
            )
            
            logger.info(f"Order placed: {order['id']} on {best_execution['exchange']}")
            
            # Track position
            self.positions[order['id']] = {
                'symbol': signal['symbol'],
                'side': signal['side'],
                'amount': order['amount'],
                'price': order['price'],
                'exchange': best_execution['exchange'],
                'target_price': order['price'] * (1 + signal['target_profit_pct'] / 100),
                'stop_price': order['price'] * (1 + signal['stop_loss_pct'] / 100),
                'timestamp': time.time()
            }
            
        except Exception as e:
            logger.error(f"Order execution failed: {e}")
            
    async def _run_defi_strategies(self):
        """Run DeFi yield strategies"""
        while True:
            try:
                # Find yield opportunities
                opportunities = await self.defi_handler.find_yield_opportunities()
                
                for opp in opportunities[:3]:  # Top 3 opportunities
                    if opp['apy'] > Decimal('20'):  # 20% APY threshold
                        # Execute strategy
                        result = await self.defi_handler.execute_defi_strategy({
                            'type': opp['type'],
                            'protocol': opp['protocol'],
                            'asset': opp.get('asset') or opp.get('pair'),
                            'amount': Decimal('50000'),  # $50k allocation
                            'estimated_apy': opp['apy']
                        })
                        
                        logger.info(f"DeFi strategy executed: {result}")
                        
                await asyncio.sleep(300)  # Check every 5 minutes
                
            except Exception as e:
                logger.error(f"DeFi strategy error: {e}")
                await asyncio.sleep(60)
                
    async def _run_mev_strategies(self):
        """Process MEV opportunities"""
        while True:
            try:
                opportunity = await self.mev_bot.profitable_opportunities.get()
                
                # Execute MEV strategy
                result = await self.mev_bot.strategies[opportunity.type](opportunity)
                
                if result['success']:
                    self.performance_metrics['mev_profit'] += float(opportunity.profit_estimate)
                    logger.info(f"MEV executed: {opportunity.profit_estimate} ETH profit")
                    
            except Exception as e:
                logger.error(f"MEV execution error: {e}")
                
    async def _monitor_performance(self):
        """Monitor and report performance"""
        while True:
            await asyncio.sleep(60)
            
            total_pnl = Decimal('0')
            open_positions = 0
            
            # Calculate P&L
            for position_id, position in self.positions.items():
                if position['side'] == 'buy':
                    # Get current price
                    current_price = await self._get_current_price(
                        position['symbol'],
                        position['exchange']
                    )
                    pnl = (current_price - Decimal(str(position['price']))) * Decimal(str(position['amount']))
                    total_pnl += pnl
                    
                    # Check stop loss / take profit
                    if current_price >= position['target_price'] or current_price <= position['stop_price']:
                        await self._close_position(position_id)
                    else:
                        open_positions += 1
                        
            report = {
                'timestamp': datetime.utcnow().isoformat(),
                'total_pnl': float(total_pnl),
                'mev_profit': self.performance_metrics['mev_profit'],
                'open_positions': open_positions,
                'arbitrage_count': len(self.exchange_handler.arbitrage_opportunities)
            }
            
            logger.info(f"Performance Report: {report}")

# ===========================
# Configuration
# ===========================

def create_crypto_config() -> Dict[str, Any]:
    """Create configuration for crypto trading"""
    return {
        # Exchange API keys (use environment variables in production)
        'binance_api_key': os.getenv('BINANCE_API_KEY'),
        'binance_secret': os.getenv('BINANCE_SECRET'),
        'coinbase_api_key': os.getenv('COINBASE_API_KEY'),
        'coinbase_secret': os.getenv('COINBASE_SECRET'),
        'coinbase_passphrase': os.getenv('COINBASE_PASSPHRASE'),
        'ftx_api_key': os.getenv('FTX_API_KEY'),
        'ftx_secret': os.getenv('FTX_SECRET'),
        'kraken_api_key': os.getenv('KRAKEN_API_KEY'),
        'kraken_secret': os.getenv('KRAKEN_SECRET'),
        
        # Ethereum configuration
        'eth_node_ws': os.getenv('ETH_NODE_WS', 'wss://mainnet.infura.io/ws/v3/YOUR_KEY'),
        'private_key': os.getenv('ETH_PRIVATE_KEY'),
        'flashbots_relay': 'https://relay.flashbots.net',
        
        # Trading parameters
        'max_position_size': 100000,  # $100k max per position
        'max_positions': 20,
        'risk_limit': 0.02,  # 2% max risk per trade
    }

# ===========================
# Main Entry Point
# ===========================

async def main():
    """Main entry point for crypto trading"""
    config = create_crypto_config()
    
    # Initialize crypto trading system
    crypto_system = CryptoTradingSystem(config)
    await crypto_system.initialize()
    
    # Run trading system
    await crypto_system.run()

if __name__ == "__main__":
    asyncio.run(main())
