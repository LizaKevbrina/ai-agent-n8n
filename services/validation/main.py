"""
Validation Service - Проверка и фильтрация входных данных
Port: 8001
"""

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, validator
from typing import Optional
import logging
import time
from prometheus_client import Counter, Histogram, generate_latest
from fastapi.responses import Response
import re

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Validation Service",
    version="1.0.0",
    description="Security validation and input sanitization"
)

# Prometheus metrics
validation_requests = Counter(
    'validation_requests_total',
    'Total validation requests',
    ['status', 'reason']
)
validation_duration = Histogram(
    'validation_duration_seconds',
    'Validation request duration'
)

# Security blacklist
BLACKLIST_PATTERNS = [
    r'select\s+.*from',
    r'drop\s+table',
    r'union\s+select',
    r'--',
    r'<script',
    r'javascript:',
    r'http://',
    r'https://',
    r'file://',
    r'base64,',
    r'eval\(',
    r'exec\(',
    r'\bor\b.*=.*',
    r'\band\b.*=.*',
    r'\.\./',
    r'<iframe',
    r'onerror=',
    r'onload='
]

class ValidationRequest(BaseModel):
    chat_input: str
    session_id: str
    
    @validator('chat_input')
    def validate_input(cls, v):
        if not v or not v.strip():
            raise ValueError('Input cannot be empty')
        if len(v) > 5000:
            raise ValueError('Input too long (max 5000 chars)')
        return v.strip()
    
    @validator('session_id')
    def validate_session(cls, v):
        if not v or len(v) < 3:
            raise ValueError('Invalid session_id')
        return v

class ValidationResponse(BaseModel):
    is_valid: bool
    message: str
    sanitized_input: Optional[str] = None
    session_id: str
    validation_time_ms: float

def check_security_threats(text: str) -> tuple[bool, str]:
    """
    Проверка на SQL injection, XSS, и другие угрозы
    
    Returns:
        (is_safe, reason)
    """
    text_lower = text.lower()
    
    # Проверка длины
    if len(text) < 2:
        return False, "Input too short"
    
    if len(text) > 5000:
        return False, "Input too long"
    
    # Проверка blacklist
    for pattern in BLACKLIST_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            logger.warning(f"Security threat detected: {pattern}")
            return False, f"Security threat detected: potential injection"
    
    # Проверка на слишком много спецсимволов
    special_chars = sum(1 for c in text if not c.isalnum() and c not in ' .,!?-—()[]{}"\':;')
    if special_chars > len(text) * 0.3:
        return False, "Too many special characters"
    
    return True, "OK"

def sanitize_input(text: str) -> str:
    """Очистка входных данных"""
    # Убираем лишние пробелы
    text = ' '.join(text.split())
    
    # Убираем опасные символы (но оставляем пунктуацию)
    text = re.sub(r'[<>{}]', '', text)
    
    return text.strip()

@app.post("/api/v1/validate", response_model=ValidationResponse)
async def validate_input(req: ValidationRequest):
    """
    Валидация и санитизация входных данных
    
    Проверяет:
    - Длину input (2-5000 символов)
    - SQL injection
    - XSS атаки
    - Подозрительные паттерны
    """
    start_time = time.time()
    
    try:
        # Security check
        is_safe, reason = check_security_threats(req.chat_input)
        
        if not is_safe:
            validation_requests.labels(status='blocked', reason=reason).inc()
            logger.warning(
                f"Validation blocked: session={req.session_id}, "
                f"reason={reason}, input={req.chat_input[:50]}..."
            )
            
            return ValidationResponse(
                is_valid=False,
                message=f"Запрос заблокирован по соображениям безопасности: {reason}",
                session_id=req.session_id,
                validation_time_ms=round((time.time() - start_time) * 1000, 2)
            )
        
        # Sanitize
        sanitized = sanitize_input(req.chat_input)
        
        validation_requests.labels(status='success', reason='passed').inc()
        validation_duration.observe(time.time() - start_time)
        
        logger.info(
            f"Validation passed: session={req.session_id}, "
            f"input_length={len(sanitized)}"
        )
        
        return ValidationResponse(
            is_valid=True,
            message="Validation passed",
            sanitized_input=sanitized,
            session_id=req.session_id,
            validation_time_ms=round((time.time() - start_time) * 1000, 2)
        )
        
    except ValueError as e:
        validation_requests.labels(status='error', reason='validation_error').inc()
        raise HTTPException(status_code=400, detail=str(e))
    
    except Exception as e:
        validation_requests.labels(status='error', reason='internal_error').inc()
        logger.error(f"Validation error: {e}")
        raise HTTPException(status_code=500, detail="Internal validation error")

@app.get("/metrics")
async def metrics():
    """Prometheus metrics"""
    return Response(generate_latest(), media_type="text/plain")

@app.get("/health")
async def health():
    """Health check"""
    return {
        "status": "healthy",
        "service": "validation",
        "version": "1.0.0"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
