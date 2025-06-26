import pytest
import asyncio
from unittest.mock import Mock

@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest.fixture
def mock_market_data():
    """Mock market data for testing"""
    return {
        'symbol': 'BTC/USDT',
        'bid': 50000.0,
        'ask': 50001.0,
        'last': 50000.5,
        'volume': 1000.0
    }
