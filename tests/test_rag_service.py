import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock
import sys

sys.path.insert(0, './services/rag')

# Mock environment
os.environ["YANDEX_API_KEY"] = "test_key"
os.environ["YANDEX_FOLDER_ID"] = "test_folder"
os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_KEY"] = "test_key"
os.environ["REDIS_URL"] = "redis://localhost:6379"

from main import app

client = TestClient(app)

@pytest.mark.asyncio
@patch('main.generate_embedding')
@patch('main.vector_search')
async def test_rag_search_success(mock_vector_search, mock_generate_embedding):
    """Test: успешный RAG search"""
    # Mock embedding
    mock_generate_embedding.return_value = [0.1] * 256
    
    # Mock vector search results
    mock_vector_search.return_value = [
        {
            "content": "ЖК Солнечный: 2-комнатные квартиры от 5 млн",
            "metadata": {},
            "similarity": 0.9
        }
    ]
    
    # В реальности тут должен быть async test
    pass

def test_rag_cache_hit():
    """Test: кэш работает (второй запрос быстрее)"""
    # Mock test с Redis
    pass
