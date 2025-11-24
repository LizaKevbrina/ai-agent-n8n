import pytest
import httpx

@pytest.mark.asyncio
async def test_full_pipeline():
    """
    Integration test: полный pipeline от validation до logging
    
    Требует запущенные сервисы (docker-compose up)
    """
    session_id = "integration_test_123"
    question = "Какие квартиры в ЖК Солнечный?"
    
    # 1. Validation
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8001/api/v1/validate",
            json={"chat_input": question, "session_id": session_id}
        )
        assert response.status_code == 200
        assert response.json()["is_valid"] == True
    
    # 2. Intent Classification
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8004/api/v1/classify",
            json={"text": question, "session_id": session_id}
        )
        assert response.status_code == 200
        data = response.json()
        assert "is_real_estate" in data
    
    # 3. RAG Search (if real_estate)
    if data["is_real_estate"]:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "http://localhost:8003/api/v1/search",
                json={
                    "question": question,
                    "session_id": session_id,
                    "match_count": 5
                }
            )
            assert response.status_code == 200
            rag_data = response.json()
            assert "context" in rag_data
    
    # 4. Memory Get
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8006/api/v1/get",
            json={"session_id": session_id, "prompt_version": "v1"}
        )
        assert response.status_code == 200
        memory_data = response.json()
        assert "history" in memory_data
    
    # Test passed if we reach here
    assert True
