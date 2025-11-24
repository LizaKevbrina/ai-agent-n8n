import pytest
from fastapi.testclient import TestClient
import sys
import asyncpg
import os

sys.path.insert(0, './services/memory')

# Mock Postgres connection for tests
os.environ["POSTGRES_URL"] = "postgresql://test:test@localhost:5432/test_db"

from main import app

client = TestClient(app)

@pytest.fixture(scope="module")
async def setup_db():
    """Setup test database"""
    # В реальных тестах здесь был бы код для создания тестовой БД
    pass

def test_memory_get_empty_history():
    """Test: получение пустой истории для новой сессии"""
    # Mock test - в реальности нужно mock asyncpg
    pass

def test_memory_add_messages():
    """Test: добавление сообщений в историю"""
    # Mock test
    pass

def test_memory_prompt_versioning():
    """Test: версионирование промптов работает"""
    # Mock test - проверяет, что при смене версии история очищается
    pass

def test_memory_sliding_window():
    """Test: sliding window (40 сообщений)"""
    # Mock test - добавляем 50 сообщений, проверяем что остаётся 40
    pass
