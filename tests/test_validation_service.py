import pytest
from fastapi.testclient import TestClient
import sys
sys.path.insert(0, './services/validation')
from main import app

client = TestClient(app)

def test_validation_success():
    """Test: валидация проходит для нормального input"""
    response = client.post("/api/v1/validate", json={
        "chat_input": "Какие квартиры есть в ЖК Солнечный?",
        "session_id": "test123"
    })
    
    assert response.status_code == 200
    data = response.json()
    assert data["is_valid"] == True
    assert "sanitized_input" in data
    assert data["session_id"] == "test123"

def test_validation_blocks_sql_injection():
    """Test: блокировка SQL injection"""
    response = client.post("/api/v1/validate", json={
        "chat_input": "SELECT * FROM users WHERE id=1",
        "session_id": "test123"
    })
    
    assert response.status_code == 200
    data = response.json()
    assert data["is_valid"] == False
    assert "безопасности" in data["message"]

def test_validation_blocks_xss():
    """Test: блокировка XSS"""
    response = client.post("/api/v1/validate", json={
        "chat_input": "<script>alert('xss')</script>",
        "session_id": "test123"
    })
    
    assert response.status_code == 200
    data = response.json()
    assert data["is_valid"] == False

def test_validation_empty_input():
    """Test: пустой input возвращает 400"""
    response = client.post("/api/v1/validate", json={
        "chat_input": "",
        "session_id": "test123"
    })
    
    assert response.status_code == 400

def test_validation_too_long_input():
    """Test: слишком длинный input блокируется"""
    response = client.post("/api/v1/validate", json={
        "chat_input": "A" * 6000,
        "session_id": "test123"
    })
    
    assert response.status_code == 200
    data = response.json()
    assert data["is_valid"] == False
    assert "too long" in data["message"].lower()

def test_health_check():
    """Test: health check endpoint"""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "validation"

def test_metrics_endpoint():
    """Test: metrics endpoint доступен"""
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "validation_requests_total" in response.text
