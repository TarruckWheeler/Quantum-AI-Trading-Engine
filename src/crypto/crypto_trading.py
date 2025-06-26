"""
Cryptocurrency Trading Module
============================
Handles multi-exchange crypto trading, DeFi protocols, and MEV strategies.
"""

# Version
__version__ = "1.0.0"

# Import main components
from .crypto_trading import (
    MultiExchangeCryptoHandler,
    DeFiProtocolHandler,
    MEVBot,
    CryptoMLPredictor,
    CryptoTradingSystem
)

# Configuration defaults
DEFAULT_EXCHANGES = ["binance", "coinbase", "kraken", "ftx"]
DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "AVAX/USDT"]

# Module initialization
def initialize_crypto_module():
    """Initialize the cryptocurrency trading module"""
    print(f"Crypto Trading Module v{__version__} initialized")
    return True

# Expose main classes for easy import
__all__ = [
    "MultiExchangeCryptoHandler",
    "DeFiProtocolHandler", 
    "MEVBot",
    "CryptoMLPredictor",
    "CryptoTradingSystem",
    "DEFAULT_EXCHANGES",
    "DEFAULT_SYMBOLS",
    "initialize_crypto_module"
]

# Module metadata
__author__ = "Quantum-AI Trading Team"
__license__ = "MIT"
__description__ = "Advanced cryptocurrency trading with ML and DeFi integration"# Crypto Code
