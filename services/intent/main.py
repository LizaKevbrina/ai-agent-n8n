"""
Intent Classifier Service - Классификация намерений пользователя
Port: 8004
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx
import os
import logging
import time
import json
import hashlib
from typing import Optional
from prometheus_client import Counter, Histogram, generate_latest
from fastapi.responses import Response
import redis.asyncio as redis

# Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Intent Classifier Service",
    version="1.0.0",
    description="Classify user intent using YandexGPT"
)

# Environment
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")

# Redis client
redis_client = None

# Prometheus
intent_requests = Counter(
    'intent_requests_total',
    'Total intent classification requests',
    ['status', 'intent_type', 'cached']
)
intent_duration = Histogram(
    'intent_duration_seconds',
    'Intent classification duration'
)
cache_hits = Counter('intent_cache_hits_total', 'Intent cache hits')
cache_misses = Counter('intent_cache_misses_total', 'Intent cache misses')

@app.on_event("startup")
async def startup():
    global redis_client
    redis_client = await redis.from_url(
        REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        max_connections=20
    )
    logger.info("Redis connected for Intent Service")

@app.on_event("shutdown")
async def shutdown():
    await redis_client.close()

class IntentRequest(BaseModel):
    text: str
    session_id: str

class IntentResponse(BaseModel):
    is_real_estate: bool
    confidence: Optional[float] = None
    cached: bool = False
    session_id: str
    classification_time_ms: float

async def call_yandex_gpt(text: str) -> bool:
    """
    Вызов YandexGPT для классификации намерения
    
    Returns:
        bool: True если это вопрос о недвижимости
    """
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json"
    }
    
    system_prompt = """Ты — интеллектуальный фильтр запроса. Клиенты обращаются в систему, связанную с продажей и арендой жилой недвижимости (новостройки, ЖК, квартиры, планировки, цены, этажи и т.п.).

Ответь **"true"**, если вопрос явно относится к:
- недвижимости (ЖК, квартиры, метраж, этажность, стоимость, планировка, сроки сдачи, бронирование, расположение, фото, и т.п.)
- требует данных из документов, векторной базы или Supabase

Ответь **"false"**, если:
- это приветствие, small talk ("привет", "как дела", "что ты умеешь")
- вопрос общего характера, не связанный с недвижимостью
- запрос на исторические, мировые, научные, культурные данные

Никогда не пытайся угадать ответ или уточнять — просто классифицируй намерение. Выводи **только `true` или `false`**, без пояснений."""
    
    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest",
        "completionOptions": {
            "temperature": 0.1,
            "maxTokens": 10
        },
        "messages": [
            {"role": "system", "text": system_prompt},
            {"role": "user", "text": text}
        ]
    }
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            
            result = response.json()
            answer = result["result"]["alternatives"][0]["message"]["text"].strip().lower()
            
            # Parse true/false
            is_real_estate = "true" in answer
            
            logger.info(f"YandexGPT response: {answer} -> {is_real_estate}")
            return is_real_estate
            
        except httpx.HTTPError as e:
            logger.error(f"YandexGPT API error: {e}")
            # Fallback: проверяем ключевые слова
            keywords = ['квартир', 'жк', 'недвижим', 'комнат', 'этаж', 'цен', 'ипотек', 'стоимост']
            return any(kw in text.lower() for kw in keywords)

@app.post("/api/v1/classify", response_model=IntentResponse)
async def classify_intent(req: IntentRequest):
    """
    Классификация намерения пользователя
    
    Определяет, является ли запрос вопросом о недвижимости.
    Использует кэш в Redis для ускорения повторных запросов.
    """
    start_time = time.time()
    
    # Cache key
    cache_key = f"intent:{hashlib.md5(req.text.encode()).hexdigest()}"
    
    try:
        # Check cache
        cached = await redis_client.get(cache_key)
        
        if cached:
            cache_hits.inc()
            data = json.loads(cached)
            
            duration_ms = round((time.time() - start_time) * 1000, 2)
            
            intent_requests.labels(
                status='success',
                intent_type='real_estate' if data['is_real_estate'] else 'general',
                cached='true'
            ).inc()
            
            logger.info(
                f"Intent cached: session={req.session_id}, "
                f"is_real_estate={data['is_real_estate']}"
            )
            
            return IntentResponse(
                is_real_estate=data['is_real_estate'],
                cached=True,
                session_id=req.session_id,
                classification_time_ms=duration_ms
            )
        
        cache_misses.inc()
        
        # Call YandexGPT
        is_real_estate = await call_yandex_gpt(req.text)
        
        duration_ms = round((time.time() - start_time) * 1000, 2)
        intent_duration.observe(time.time() - start_time)
        
        # Save to cache (1 hour TTL)
        await redis_client.setex(
            cache_key,
            3600,
            json.dumps({"is_real_estate": is_real_estate})
        )
        
        intent_requests.labels(
            status='success',
            intent_type='real_estate' if is_real_estate else 'general',
            cached='false'
        ).inc()
        
        logger.info(
            f"Intent classified: session={req.session_id}, "
            f"is_real_estate={is_real_estate}, duration={duration_ms}ms"
        )
        
        return IntentResponse(
            is_real_estate=is_real_estate,
            cached=False,
            session_id=req.session_id,
            classification_time_ms=duration_ms
        )
        
    except Exception as e:
        intent_requests.labels(
            status='error',
            intent_type='unknown',
            cached='false'
        ).inc()
        logger.error(f"Intent classification error: {e}")
        raise HTTPException(status_code=500, detail=f"Classification failed: {str(e)}")

@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type="text/plain")

@app.get("/health")
async def health():
    try:
        await redis_client.ping()
        redis_status = "ok"
    except:
        redis_status = "error"
    
    return {
        "status": "healthy" if redis_status == "ok" else "degraded",
        "service": "intent",
        "redis": redis_status,
        "version": "1.0.0"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8004)
