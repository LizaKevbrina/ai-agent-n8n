"""
Memory Service - Управление историей диалогов с версионированием
Port: 8006
"""

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import List, Optional
import asyncpg
import os
import logging
import time
from prometheus_client import Counter, Histogram, generate_latest
from fastapi.responses import Response
from secrets_helper import load_secret

# Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Memory Service",
    version="2.0.0",
    description="Chat history with prompt versioning and pessimistic locking"
)

# Environment
try:
    POSTGRES_PASSWORD = load_secret('POSTGRES_PASSWORD_FILE', 'POSTGRES_PASSWORD')
    DATABASE_URL = f"postgresql://ai_user:{POSTGRES_PASSWORD}@postgres:5432/ai_db"
except ValueError as e:
    logger.error(f"Failed to load secrets: {e}")
    DATABASE_URL = os.getenv("POSTGRES_URL")

pool = None

# Prometheus
memory_requests = Counter(
    'memory_requests_total',
    'Total memory requests',
    ['operation', 'status']
)
memory_duration = Histogram('memory_duration_seconds', 'Memory operation duration')
version_conflicts = Counter('memory_version_conflicts_total', 'Version conflict errors')

@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=5,
        max_size=20,
        command_timeout=60
    )
    logger.info("Postgres connection pool created")

@app.on_event("shutdown")
async def shutdown():
    await pool.close()

class Message(BaseModel):
    role: str
    content: str

class MemoryGetRequest(BaseModel):
    session_id: str
    prompt_version: Optional[str] = "v0"

class MemoryAddRequest(BaseModel):
    session_id: str
    messages: List[Message]
    prompt_version: Optional[str] = "v0"

class MemoryResponse(BaseModel):
    history: List[Message]
    window_size: int
    prompt_version: str
    version_changed: bool = False
    session_id: str

async def check_and_clear_if_version_changed(
    conn,
    session_id: str,
    new_version: str
) -> tuple[bool, Optional[str]]:
    """
    Проверяет версию промпта с блокировкой.
    Возвращает: (version_changed, current_version)
    """
    # Блокируем строку для чтения (pessimistic lock)
    row = await conn.fetchrow(
        """
        SELECT version FROM prompt_versions
        WHERE session_id = $1
        FOR UPDATE  -- ← Блокировка до конца транзакции
        """,
        session_id
    )
    
    stored_version = row['version'] if row else None
    
    # Если версия изменилась → очищаем историю
    if stored_version is not None and stored_version != new_version:
        logger.info(
            f"Prompt version changed for {session_id}: "
            f"{stored_version} → {new_version}"
        )
        
        # Очищаем историю
        await conn.execute(
            "DELETE FROM chat_memory WHERE session_id = $1",
            session_id
        )
        
        # Обновляем версию
        await conn.execute(
            """
            UPDATE prompt_versions
            SET version = $2, updated_at = NOW()
            WHERE session_id = $1
            """,
            session_id, new_version
        )
        
        return True, stored_version  # Версия изменилась
    
    # Если версии нет → создаём
    if stored_version is None:
        await conn.execute(
            """
            INSERT INTO prompt_versions (session_id, version, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (session_id) DO UPDATE
            SET version = $2, updated_at = NOW()
            """,
            session_id, new_version
        )
    
    return False, stored_version  # Версия не изменилась

@app.post("/api/v1/get", response_model=MemoryResponse)
async def get_history(
    req: MemoryGetRequest,
    x_correlation_id: Optional[str] = Header(None)
):
    """
    Получить историю диалога (последние 40 сообщений)
    
    - Проверяет версию промпта с блокировкой
    - Если версия изменилась → очищает историю
    - Возвращает последние 40 сообщений (sliding window)
    """
    start_time = time.time()
    correlation_id = x_correlation_id or "unknown"
    
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():  # ← Транзакция
                # Проверяем версию с блокировкой
                version_changed, old_version = await check_and_clear_if_version_changed(
                    conn,
                    req.session_id,
                    req.prompt_version
                )
                
                # Если версия изменилась → история уже очищена
                if version_changed:
                    memory_requests.labels(operation='get', status='version_changed').inc()
                    
                    logger.info(
                        f"[{correlation_id}] History cleared for {req.session_id} "
                        f"due to version change: {old_version} → {req.prompt_version}"
                    )
                    
                    return MemoryResponse(
                        history=[],
                        window_size=0,
                        prompt_version=req.prompt_version,
                        version_changed=True,
                        session_id=req.session_id
                    )
                
                # Получаем историю (последние 40)
                rows = await conn.fetch(
                    """
                    SELECT role, content
                    FROM chat_memory
                    WHERE session_id = $1
                    ORDER BY created_at DESC
                    LIMIT 40
                    """,
                    req.session_id
                )
                
                # Reverse для правильного порядка
                history = [
                    Message(role=row['role'], content=row['content'])
                    for row in reversed(rows)
                ]
                
                memory_duration.observe(time.time() - start_time)
                memory_requests.labels(operation='get', status='success').inc()
                
                logger.info(
                    f"[{correlation_id}] History retrieved: session={req.session_id}, "
                    f"messages={len(history)}"
                )
                
                return MemoryResponse(
                    history=history,
                    window_size=len(history),
                    prompt_version=req.prompt_version,
                    version_changed=False,
                    session_id=req.session_id
                )
            
    except Exception as e:
        memory_requests.labels(operation='get', status='error').inc()
        logger.error(f"[{correlation_id}] Get history error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get history: {str(e)}")

@app.post("/api/v1/add", response_model=MemoryResponse)
async def add_messages(
    req: MemoryAddRequest,
    x_correlation_id: Optional[str] = Header(None)
):
    """
    Добавить сообщения в историю
    
    - Проверяет версию с блокировкой (FOR UPDATE)
    - Если версия не совпадает → возвращает 409 Conflict
    - Поддерживает sliding window (40 сообщений)
    - N8N должен retry при 409
    """
    start_time = time.time()
    correlation_id = x_correlation_id or "unknown"
    
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():  # ← Транзакция
                # Проверяем версию с блокировкой
                current_version = await conn.fetchval(
                    """
                    SELECT version FROM prompt_versions
                    WHERE session_id = $1
                    FOR UPDATE  -- ← Блокировка
                    """,
                    req.session_id
                )
                
                # Если версия не совпадает → 409 Conflict
                if current_version and current_version != req.prompt_version:
                    version_conflicts.inc()
                    
                    logger.warning(
                        f"[{correlation_id}] Version conflict for {req.session_id}: "
                        f"expected={req.prompt_version}, actual={current_version}"
                    )
                    
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "version_mismatch",
                            "expected": req.prompt_version,
                            "actual": current_version,
                            "message": "Prompt version changed. Please retry with new version."
                        }
                    )
                
                # Если версии нет → создаём
                if not current_version:
                    await conn.execute(
                        """
                        INSERT INTO prompt_versions (session_id, version, updated_at)
                        VALUES ($1, $2, NOW())
                        ON CONFLICT (session_id) DO UPDATE
                        SET version = $2, updated_at = NOW()
                        """,
                        req.session_id, req.prompt_version
                    )
                
                # Сохраняем сообщения
                for msg in req.messages:
                    await conn.execute(
                        """
                        INSERT INTO chat_memory (session_id, role, content, created_at)
                        VALUES ($1, $2, $3, NOW())
                        """,
                        req.session_id, msg.role, msg.content
                    )
                
                # Получаем обновлённую историю (последние 40)
                rows = await conn.fetch(
                    """
                    SELECT role, content
                    FROM chat_memory
                    WHERE session_id = $1
                    ORDER BY created_at DESC
                    LIMIT 40
                    """,
                    req.session_id
                )
                
                history = [
                    Message(role=row['role'], content=row['content'])
                    for row in reversed(rows)
                ]
                
                # Если сообщений больше 40 → удаляем старые
                total_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM chat_memory WHERE session_id = $1",
                    req.session_id
                )
                
                if total_count > 40:
                    await conn.execute(
                        """
                        DELETE FROM chat_memory
                        WHERE session_id = $1
                        AND created_at < (
                            SELECT created_at FROM chat_memory
                            WHERE session_id = $1
                            ORDER BY created_at DESC
                            LIMIT 1 OFFSET 40
                        )
                        """,
                        req.session_id
                    )
                
                memory_duration.observe(time.time() - start_time)
                memory_requests.labels(operation='add', status='success').inc()
                
                logger.info(
                    f"[{correlation_id}] Messages added: session={req.session_id}, "
                    f"added={len(req.messages)}, total={len(history)}"
                )
                
                return MemoryResponse(
                    history=history,
                    window_size=len(history),
                    prompt_version=req.prompt_version,
                    version_changed=False,
                    session_id=req.session_id
                )
            
    except HTTPException:
        raise  # Re-raise 409
    except Exception as e:
        memory_requests.labels(operation='add', status='error').inc()
        logger.error(f"[{correlation_id}] Add messages error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to add messages: {str(e)}")

@app.delete("/api/v1/clear/{session_id}")
async def clear_history(
    session_id: str,
    x_correlation_id: Optional[str] = Header(None)
):
    """Очистить всю историю для сессии"""
    correlation_id = x_correlation_id or "unknown"
    
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM chat_memory WHERE session_id = $1",
                    session_id
                )
                await conn.execute(
                    "DELETE FROM prompt_versions WHERE session_id = $1",
                    session_id
                )
        
        memory_requests.labels(operation='clear', status='success').inc()
        
        logger.info(f"[{correlation_id}] History cleared for session={session_id}")
        
        return {
            "status": "cleared",
            "session_id": session_id
        }
        
    except Exception as e:
        memory_requests.labels(operation='clear', status='error').inc()
        logger.error(f"[{correlation_id}] Clear history error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to clear history: {str(e)}")

@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type="text/plain")

@app.get("/health")
async def health():
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_status = "ok"
    except:
        db_status = "error"
    
    return {
        "status": "healthy" if db_status == "ok" else "unhealthy",
        "service": "memory",
        "database": db_status,
        "version": "2.0.0",
        "features": ["pessimistic_locking", "version_conflicts_409"]
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8006)
