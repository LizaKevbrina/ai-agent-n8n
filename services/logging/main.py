"""
Logging Service - Логирование взаимодействий + ошибок workflow в Supabase
Port: 8007
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import httpx
import os
import logging
import time
import json
from prometheus_client import Counter, Histogram, generate_latest
from fastapi.responses import Response

# Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Logging Service",
    version="2.0.0",
    description="Interaction and error logging to Supabase"
)

# Environment
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Prometheus
log_requests = Counter(
    'log_requests_total',
    'Total log requests',
    ['type', 'status']  # type: interaction или error
)
log_duration = Histogram('log_duration_seconds', 'Log operation duration')

# ============================================
# MODELS
# ============================================

class LogRequest(BaseModel):
    """Модель для логирования взаимодействий с AI"""
    session_id: str
    question: str
    answer: str
    intent_type: Optional[str] = "unknown"
    cached: bool = False
    response_time_ms: Optional[int] = None
    embedding: Optional[List[float]] = None
    context_used: bool = False
    documents_found: int = 0
    input_type: Optional[str] = None  # NEW: voice или text
    stt_duration_ms: Optional[int] = None  # NEW: для голосовых
    chunks_count: Optional[int] = None  # NEW: для голосовых

class ErrorLogRequest(BaseModel):
    """Модель для логирования ошибок workflow"""
    error_message: str
    error_code: int
    node_name: str
    session_id: str
    correlation_id: str
    timestamp: str
    raw_error: Optional[str] = None
    input_type: Optional[str] = None  # NEW
    severity: Optional[str] = "error"  # info, warning, error, critical

class LogResponse(BaseModel):
    status: str
    log_id: Optional[str] = None
    timestamp: str

# ============================================
# HELPER FUNCTIONS
# ============================================

async def save_to_supabase(table: str, data: dict) -> dict:
    """
    Универсальная функция для сохранения в Supabase
    
    Args:
        table: имя таблицы (logs или errors)
        data: данные для вставки
    """
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(url, headers=headers, json=data)
            response.raise_for_status()
            
            result = response.json()
            return result[0] if result else {}
            
        except httpx.HTTPStatusError as e:
            logger.error(f"Supabase save error ({table}): {e.response.status_code} - {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"Supabase save error ({table}): {e}")
            raise

# ============================================
# ENDPOINTS: INTERACTION LOGGING
# ============================================

@app.post("/api/v1/log", response_model=LogResponse)
async def log_interaction(req: LogRequest):
    """
    Логирование взаимодействия пользователя с AI
    
    Сохраняет в таблицу `logs`:
    - Вопрос и ответ
    - Тип намерения (real_estate/general)
    - Информацию о кэшировании
    - Время ответа
    - Эмбеддинг (для аналитики)
    - Таймстемп
    - NEW: input_type (voice/text)
    - NEW: stt_duration_ms (для голосовых)
    """
    start_time = time.time()
    
    try:
        from datetime import datetime
        timestamp = datetime.utcnow().isoformat()
        
        # Подготовка данных для логирования
        log_data = {
            "session_id": req.session_id,
            "question": req.question,
            "answer": req.answer,
            "intent_type": req.intent_type,
            "cached": req.cached,
            "response_time_ms": req.response_time_ms,
            "embedding": json.dumps(req.embedding) if req.embedding else None,
            "context_used": req.context_used,
            "documents_found": req.documents_found,
            "timestamp": timestamp,
            # NEW fields
            "input_type": req.input_type,
            "stt_duration_ms": req.stt_duration_ms,
            "chunks_count": req.chunks_count
        }
        
        # Сохраняем в Supabase
        result = await save_to_supabase("logs", log_data)
        
        duration_ms = round((time.time() - start_time) * 1000, 2)
        log_duration.observe(time.time() - start_time)
        log_requests.labels(type='interaction', status='success').inc()
        
        logger.info(
            f"Interaction logged: session={req.session_id}, "
            f"intent={req.intent_type}, input={req.input_type}, "
            f"cached={req.cached}, duration={duration_ms}ms"
        )
        
        return LogResponse(
            status="logged",
            log_id=str(result.get("id")),
            timestamp=timestamp
        )
        
    except Exception as e:
        log_requests.labels(type='interaction', status='error').inc()
        logger.error(f"Logging error: {e}")
        
        # НЕ поднимаем HTTPException - логирование не должно ломать основной flow
        return LogResponse(
            status="failed",
            timestamp=timestamp
        )

# ============================================
# ENDPOINTS: ERROR LOGGING (NEW)
# ============================================

@app.post("/api/v1/errors", response_model=LogResponse)
async def log_error(req: ErrorLogRequest):
    """
    Логирование ошибок workflow в Supabase
    
    Сохраняет в таблицу `errors`:
    - Сообщение об ошибке
    - HTTP код
    - Имя ноды где произошла ошибка
    - Session ID и Correlation ID
    - Raw error (полный стектрейс)
    - Severity (info/warning/error/critical)
    
    Используется Error Router workflow для централизованного логирования
    """
    start_time = time.time()
    
    try:
        # Подготовка данных
        error_data = {
            "error_message": req.error_message,
            "error_code": req.error_code,
            "node_name": req.node_name,
            "session_id": req.session_id,
            "correlation_id": req.correlation_id,
            "timestamp": req.timestamp,
            "raw_error": req.raw_error,
            "input_type": req.input_type,
            "severity": req.severity or "error"
        }
        
        # Сохраняем в Supabase
        result = await save_to_supabase("errors", error_data)
        
        duration_ms = round((time.time() - start_time) * 1000, 2)
        log_duration.observe(time.time() - start_time)
        log_requests.labels(type='error', status='success').inc()
        
        logger.info(
            f"Error logged: node={req.node_name}, "
            f"code={req.error_code}, session={req.session_id}, "
            f"correlation={req.correlation_id}, duration={duration_ms}ms"
        )
        
        return LogResponse(
            status="logged",
            log_id=str(result.get("id")),
            timestamp=req.timestamp
        )
        
    except Exception as e:
        log_requests.labels(type='error', status='error').inc()
        logger.error(f"Error logging failed: {e}")
        
        # Критично: если не можем залогировать ошибку - пишем в stderr
        import sys
        print(f"CRITICAL: Failed to log error to Supabase: {e}", file=sys.stderr)
        print(f"Error data: {req.dict()}", file=sys.stderr)
        
        # Не ломаем workflow даже если логирование упало
        return LogResponse(
            status="failed",
            timestamp=req.timestamp
        )

# ============================================
# ENDPOINTS: ANALYTICS
# ============================================

@app.get("/api/v1/logs/{session_id}")
async def get_session_logs(
    session_id: str,
    limit: int = 50
):
    """Получить историю логов для сессии"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/logs"
        
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}"
        }
        
        params = {
            "session_id": f"eq.{session_id}",
            "order": "timestamp.desc",
            "limit": limit
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            
            logs = response.json()
            
            return {
                "session_id": session_id,
                "logs": logs,
                "count": len(logs)
            }
            
    except Exception as e:
        logger.error(f"Get logs error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get logs: {str(e)}"
        )

@app.get("/api/v1/errors/{session_id}")
async def get_session_errors(
    session_id: str,
    limit: int = 50
):
    """Получить историю ошибок для сессии"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/errors"
        
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}"
        }
        
        params = {
            "session_id": f"eq.{session_id}",
            "order": "timestamp.desc",
            "limit": limit
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            
            errors = response.json()
            
            return {
                "session_id": session_id,
                "errors": errors,
                "count": len(errors)
            }
            
    except Exception as e:
        logger.error(f"Get errors error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get errors: {str(e)}"
        )

@app.get("/api/v1/stats")
async def get_stats():
    """Получить общую статистику"""
    try:
        # Вызов RPC функции в Supabase
        url = f"{SUPABASE_URL}/rest/v1/rpc/get_logs_stats"
        
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=headers, json={})
            
            if response.status_code == 200:
                stats = response.json()
                return {
                    "status": "ok",
                    "stats": stats
                }
            else:
                # Fallback если RPC не настроен
                return {
                    "status": "error",
                    "message": "Stats RPC not configured",
                    "stats": {}
                }
            
    except Exception as e:
        logger.error(f"Get stats error: {e}")
        return {
            "status": "error",
            "stats": {
                "total_logs": 0,
                "unique_sessions": 0,
                "avg_response_time_ms": 0
            }
        }

@app.get("/api/v1/error-stats")
async def get_error_stats(days: int = 7):
    """Получить статистику ошибок за последние N дней"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/rpc/get_error_stats"
        
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url, 
                headers=headers, 
                json={"days": days}
            )
            
            if response.status_code == 200:
                stats = response.json()
                return {
                    "status": "ok",
                    "period_days": days,
                    "stats": stats
                }
            else:
                return {
                    "status": "error",
                    "message": "Error stats RPC not configured"
                }
            
    except Exception as e:
        logger.error(f"Get error stats error: {e}")
        return {
            "status": "error",
            "message": str(e)
        }

# ============================================
# HEALTH & METRICS
# ============================================

@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type="text/plain")

@app.get("/health")
async def health():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/",
                headers={"apikey": SUPABASE_KEY}
            )
            supabase_status = "ok" if response.status_code < 500 else "error"
    except:
        supabase_status = "error"
    
    return {
        "status": "healthy" if supabase_status == "ok" else "degraded",
        "service": "logging",
        "supabase": supabase_status,
        "features": [
            "interaction_logging",
            "error_logging",
            "analytics",
            "session_history"
        ],
        "version": "2.0.0"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8007)
