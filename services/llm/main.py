"""
LLM Service - Генерация ответов через YandexGPT
Port: 8005
"""

from fastapi import FastAPI, HTTPException, Request, Header
from pydantic import BaseModel
from typing import List, Optional
import httpx
import os
import logging
import time
import asyncio
from prometheus_client import Counter, Histogram, Gauge, generate_latest
from fastapi.responses import Response
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import uuid
from pybreaker import CircuitBreaker

# Import secrets helper
from secrets_helper import load_secret

# Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="LLM Service",
    version="2.0.0",
    description="LLM with rag_used flag and circuit breaker"
)

# Environment
try:
    YANDEX_API_KEY = load_secret('YANDEX_API_KEY_FILE', 'YANDEX_API_KEY')
    YANDEX_FOLDER_ID = load_secret('YANDEX_FOLDER_ID_FILE', 'YANDEX_FOLDER_ID')
except ValueError as e:
    logger.error(f"Failed to load secrets: {e}")
    YANDEX_API_KEY = None
    YANDEX_FOLDER_ID = None

# Concurrency control
llm_semaphore = asyncio.Semaphore(10)

# Circuit breaker for YandexGPT
yandex_breaker = CircuitBreaker(
    fail_max=5,
    timeout_duration=60,
    name='yandex_gpt'
)

# Prometheus
llm_requests = Counter(
    'llm_requests_total',
    'Total LLM requests',
    ['status']
)
llm_duration = Histogram('llm_duration_seconds', 'LLM generation duration')
llm_tokens = Histogram('llm_tokens_used', 'Tokens used per request', buckets=[100, 500, 1000, 2000, 5000, 10000])
llm_queue_size = Gauge('llm_queue_size', 'Number of requests waiting in queue')
llm_rate_limits = Counter('llm_rate_limits_total', 'Rate limit hits')
llm_circuit_breaker_opens = Counter('llm_circuit_breaker_opens_total', 'Circuit breaker opens')
llm_no_context_generation = Counter('llm_no_context_generations_total', 'Generations without RAG context')

class Message(BaseModel):
    role: str
    content: str

class LLMRequest(BaseModel):
    messages: List[Message]
    system_prompt: str
    context: Optional[str] = ""
    rag_used: Optional[bool] = False  # ← NEW: Флаг что RAG был вызван
    documents_found: Optional[int] = 0  # ← NEW: Сколько документов нашли
    question: str
    session_id: str
    temperature: float = 0.7
    max_tokens: int = 2000
    prompt_version: Optional[str] = "v0"

class LLMResponse(BaseModel):
    text: str
    tokens_used: int
    prompt_version: str
    session_id: str
    generation_time_ms: float
    correlation_id: str
    context_used: bool  # ← NEW: Был ли использован контекст

@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    """Add correlation ID to all requests"""
    correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    
    return response

@retry(
    retry=retry_if_exception_type(httpx.HTTPStatusError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True
)
async def call_yandex_gpt(
    messages: List[dict],
    temperature: float,
    max_tokens: int,
    correlation_id: str
) -> dict:
    """
    Вызов YandexGPT API с retry logic и circuit breaker
    """
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json",
        "X-Correlation-ID": correlation_id
    }
    
    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest",
        "completionOptions": {
            "temperature": temperature,
            "maxTokens": max_tokens
        },
        "messages": messages
    }
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        
        if response.status_code == 429:
            llm_rate_limits.inc()
            logger.warning(f"[{correlation_id}] Rate limit hit")
        
        response.raise_for_status()
        return response.json()

@app.post("/api/v1/generate", response_model=LLMResponse)
async def generate(
    req: LLMRequest,
    request: Request,
    x_correlation_id: Optional[str] = Header(None)
):
    """
    Генерация ответа через YandexGPT
    
    NEW Features:
    - Обработка rag_used флага
    - Разные промпты для случаев с/без контекста
    - Метрики использования контекста
    """
    start_time = time.time()
    correlation_id = request.state.correlation_id
    
    llm_queue_size.inc()
    
    try:
        async with llm_semaphore:
            llm_queue_size.dec()
            
            # Build messages
            messages = []
            
            # 1. System prompt with context handling
            system_text = req.system_prompt
            context_used = False
            
            if req.context and req.context.strip():
                # RAG нашёл документы
                system_text += f"\n\n**Контекст из базы знаний:**\n{req.context}"
                context_used = True
                
                logger.info(
                    f"[{correlation_id}] Using RAG context: {len(req.context)} chars, "
                    f"documents_found={req.documents_found}"
                )
                
            elif req.rag_used:
                # RAG был вызван, но ничего не нашёл
                llm_no_context_generation.inc()
                
                system_text += (
                    "\n\n**Внимание:** Поиск в базе знаний не дал результатов по этому запросу. "
                    "Используйте общие знания о недвижимости и продажах, но честно сообщите клиенту, "
                    "если для точного ответа нужна конкретная информация из базы данных. "
                    "Предложите связаться с менеджером для уточнения деталей."
                )
                
                logger.warning(
                    f"[{correlation_id}] RAG used but no context found. "
                    f"Will generate without specific knowledge."
                )
            else:
                # RAG не был вызван (general intent)
                logger.info(
                    f"[{correlation_id}] Generating without RAG (general intent)"
                )
            
            messages.append({"role": "system", "text": system_text})
            
            # 2. History
            for msg in req.messages:
                messages.append({"role": msg.role, "text": msg.content})
            
            # 3. Current question
            if not req.messages or req.messages[-1].content != req.question:
                messages.append({"role": "user", "text": req.question})
            
            logger.info(
                f"[{correlation_id}] Generating response: session={req.session_id}, "
                f"history_length={len(req.messages)}, context_used={context_used}"
            )
            
            # Call YandexGPT with circuit breaker
            try:
                result = yandex_breaker.call(
                    call_yandex_gpt,
                    messages,
                    req.temperature,
                    req.max_tokens,
                    correlation_id
                )
                # Wait for coroutine
                if asyncio.iscoroutine(result):
                    result = await result
                    
            except Exception as e:
                if "CircuitBreakerError" in str(type(e).__name__):
                    llm_circuit_breaker_opens.inc()
                    logger.error(f"[{correlation_id}] Circuit breaker open!")
                    raise HTTPException(
                        status_code=503,
                        detail="LLM service temporarily unavailable. Please try again later."
                    )
                raise
            
            # Extract response
            answer = result["result"]["alternatives"][0]["message"]["text"]
            tokens = result["result"]["usage"]["totalTokens"]
            
            duration_ms = round((time.time() - start_time) * 1000, 2)
            
            llm_duration.observe(time.time() - start_time)
            llm_tokens.observe(tokens)
            llm_requests.labels(status='success').inc()
            
            logger.info(
                f"[{correlation_id}] Response generated: session={req.session_id}, "
                f"tokens={tokens}, duration={duration_ms}ms, context_used={context_used}"
            )
            
            return LLMResponse(
                text=answer,
                tokens_used=tokens,
                prompt_version=req.prompt_version,
                session_id=req.session_id,
                generation_time_ms=duration_ms,
                correlation_id=correlation_id,
                context_used=context_used
            )
    
    except httpx.HTTPStatusError as e:
        llm_requests.labels(status='error').inc()
        
        if e.response.status_code == 429:
            logger.error(f"[{correlation_id}] Rate limit exceeded after retries")
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Please try again later."
            )
        elif e.response.status_code == 401:
            logger.error(f"[{correlation_id}] Authentication failed")
            raise HTTPException(
                status_code=500,
                detail="LLM authentication error"
            )
        else:
            logger.error(f"[{correlation_id}] HTTP error: {e.response.status_code}")
            raise HTTPException(
                status_code=500,
                detail=f"LLM generation failed: {str(e)}"
            )
    
    except Exception as e:
        llm_requests.labels(status='error').inc()
        logger.error(f"[{correlation_id}] Generation error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"LLM generation failed: {str(e)}"
        )
    finally:
        if llm_queue_size._value._value > 0:
            llm_queue_size.dec()

@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type="text/plain")

@app.get("/health")
async def health():
    if not YANDEX_API_KEY:
        return {
            "status": "unhealthy",
            "service": "llm",
            "yandex_gpt": "error",
            "error": "API key not configured",
            "version": "2.0.0"
        }
    
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                "https://llm.api.cloud.yandex.net/",
                headers={"Authorization": f"Api-Key {YANDEX_API_KEY}"}
            )
            yandex_status = "ok" if response.status_code < 500 else "error"
    except:
        yandex_status = "error"
    
    return {
        "status": "healthy" if yandex_status == "ok" else "degraded",
        "service": "llm",
        "yandex_gpt": yandex_status,
        "circuit_breaker": yandex_breaker.current_state,
        "concurrent_limit": 10,
        "current_queue": llm_queue_size._value._value,
        "version": "2.0.0"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)
