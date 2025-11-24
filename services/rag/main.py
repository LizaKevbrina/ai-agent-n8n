"""
RAG Service - Retrieval-Augmented Generation
Port: 8003
"""

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import httpx
import os
import logging
import time
import json
import hashlib
from prometheus_client import Counter, Histogram, Gauge, generate_latest
from fastapi.responses import Response
import redis.asyncio as redis
from secrets_helper import load_secret

# Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="RAG Service",
    version="2.0.0",
    description="RAG with similarity filtering and rag_used flag"
)

# Environment
try:
    YANDEX_API_KEY = load_secret('YANDEX_API_KEY_FILE', 'YANDEX_API_KEY')
    YANDEX_FOLDER_ID = load_secret('YANDEX_FOLDER_ID_FILE', 'YANDEX_FOLDER_ID')
    SUPABASE_URL = load_secret('SUPABASE_URL_FILE', 'SUPABASE_URL')
    SUPABASE_KEY = load_secret('SUPABASE_KEY_FILE', 'SUPABASE_KEY')
except ValueError as e:
    logger.error(f"Failed to load secrets: {e}")
    YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
    YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")

# Configuration
SIMILARITY_THRESHOLD = float(os.getenv("RAG_SIMILARITY_THRESHOLD", "0.7"))

# Redis
redis_client = None

# Prometheus
rag_requests = Counter(
    'rag_requests_total',
    'Total RAG requests',
    ['status', 'cached']
)
rag_duration = Histogram('rag_duration_seconds', 'RAG request duration')
embedding_duration = Histogram('rag_embedding_duration_seconds', 'Embedding duration')
vector_search_duration = Histogram('rag_vector_search_duration_seconds', 'Vector search duration')
cache_hits = Counter('rag_cache_hits_total', 'RAG cache hits')
cache_misses = Counter('rag_cache_misses_total', 'RAG cache misses')
low_similarity_results = Counter(
    'rag_low_similarity_total',
    'Searches with no results above threshold'
)
documents_filtered = Histogram(
    'rag_documents_filtered',
    'Number of documents filtered by similarity',
    buckets=[0, 1, 2, 3, 5, 10]
)

@app.on_event("startup")
async def startup():
    global redis_client
    redis_client = await redis.from_url(
        REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        max_connections=20
    )
    logger.info("Redis connected for RAG Service")

@app.on_event("shutdown")
async def shutdown():
    await redis_client.close()

class Document(BaseModel):
    content: str
    metadata: Dict[str, Any] = {}
    similarity: Optional[float] = None

class RAGRequest(BaseModel):
    question: str
    session_id: str
    match_count: int = 5
    similarity_threshold: Optional[float] = None  # Override default

class RAGResponse(BaseModel):
    context: str
    documents: List[Document]
    embedding: List[float]
    cached: bool = False
    rag_used: bool = True  # ← NEW: Флаг что RAG был вызван
    documents_found: int   # ← NEW: Сколько документов нашли
    documents_filtered: int  # ← NEW: Сколько отфильтровали
    session_id: str
    question: str
    search_time_ms: float

async def generate_embedding(text: str, correlation_id: str) -> List[float]:
    """Генерация эмбеддинга через YandexGPT"""
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/textEmbedding"
    
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json",
        "X-Correlation-ID": correlation_id
    }
    
    payload = {
        "modelUri": f"emb://{YANDEX_FOLDER_ID}/text-search-doc/latest",
        "text": text
    }
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            
            result = response.json()
            embedding = result.get("embedding", [])
            
            if not embedding:
                raise ValueError("Empty embedding received")
            
            return embedding
            
        except Exception as e:
            logger.error(f"[{correlation_id}] Embedding generation failed: {e}")
            raise

async def vector_search(
    embedding: List[float],
    match_count: int,
    correlation_id: str
) -> List[Dict[str, Any]]:
    """Поиск по векторной базе в Supabase"""
    url = f"{SUPABASE_URL}/rest/v1/rpc/match_documents"
    
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "X-Correlation-ID": correlation_id
    }
    
    payload = {
        "query_embedding": embedding,
        "match_count": match_count
    }
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            
            results = response.json()
            return results
            
        except Exception as e:
            logger.error(f"[{correlation_id}] Vector search failed: {e}")
            raise

@app.post("/api/v1/search", response_model=RAGResponse)
async def rag_search(
    req: RAGRequest,
    x_correlation_id: Optional[str] = Header(None)
):
    """
    RAG поиск с векторной базой
    
    NEW Features:
    - Фильтрация по similarity threshold (default 0.7)
    - Флаг rag_used для LLM Service
    - Метрики фильтрации документов
    """
    start_time = time.time()
    correlation_id = x_correlation_id or "unknown"
    
    # Определяем порог
    threshold = req.similarity_threshold or SIMILARITY_THRESHOLD
    
    # Cache key (включаем threshold!)
    cache_key = f"rag:{hashlib.md5(req.question.encode()).hexdigest()}:mc={req.match_count}:th={threshold}"
    
    try:
        # Check cache
        cached = await redis_client.get(cache_key)
        
        if cached:
            cache_hits.inc()
            data = json.loads(cached)
            
            duration_ms = round((time.time() - start_time) * 1000, 2)
            
            rag_requests.labels(status='success', cached='true').inc()
            
            logger.info(
                f"[{correlation_id}] RAG cached: session={req.session_id}, "
                f"docs={len(data['documents'])}"
            )
            
            return RAGResponse(
                context=data['context'],
                documents=[Document(**doc) for doc in data['documents']],
                embedding=data['embedding'],
                cached=True,
                rag_used=True,
                documents_found=data['documents_found'],
                documents_filtered=data['documents_filtered'],
                session_id=req.session_id,
                question=req.question,
                search_time_ms=duration_ms
            )
        
        cache_misses.inc()
        
        # Generate embedding
        embed_start = time.time()
        embedding = await generate_embedding(req.question, correlation_id)
        embedding_duration.observe(time.time() - embed_start)
        
        logger.info(f"[{correlation_id}] Embedding generated: dimension={len(embedding)}")
        
        # Vector search
        search_start = time.time()
        results = await vector_search(embedding, req.match_count, correlation_id)
        vector_search_duration.observe(time.time() - search_start)
        
        logger.info(f"[{correlation_id}] Vector search completed: found {len(results)} documents")
        
        # Build documents with filtering
        documents = []
        filtered_count = 0
        
        for item in results:
            similarity = item.get("similarity", 0)
            content = item.get("content", "").strip()
            
            if not content:
                continue
            
            # Filter by similarity threshold
            if similarity >= threshold:
                documents.append(Document(
                    content=content,
                    metadata=item.get("metadata", {}),
                    similarity=similarity
                ))
            else:
                filtered_count += 1
        
        # Log if nothing passed threshold
        if not documents and results:
            low_similarity_results.inc()
            logger.warning(
                f"[{correlation_id}] No documents above threshold {threshold}. "
                f"Best similarity: {max(r.get('similarity', 0) for r in results):.3f}"
            )
        
        # Build context
        context = "\n\n".join(doc.content for doc in documents)
        
        duration_ms = round((time.time() - start_time) * 1000, 2)
        rag_duration.observe(time.time() - start_time)
        documents_filtered.observe(filtered_count)
        
        # Cache result (1 hour)
        cache_data = {
            "context": context,
            "documents": [doc.dict() for doc in documents],
            "embedding": embedding,
            "documents_found": len(results),
            "documents_filtered": filtered_count
        }
        await redis_client.setex(cache_key, 3600, json.dumps(cache_data))
        
        rag_requests.labels(status='success', cached='false').inc()
        
        logger.info(
            f"[{correlation_id}] RAG search completed: session={req.session_id}, "
            f"found={len(results)}, passed_threshold={len(documents)}, "
            f"filtered={filtered_count}, duration={duration_ms}ms"
        )
        
        return RAGResponse(
            context=context,
            documents=documents,
            embedding=embedding,
            cached=False,
            rag_used=True,
            documents_found=len(results),
            documents_filtered=filtered_count,
            session_id=req.session_id,
            question=req.question,
            search_time_ms=duration_ms
        )
        
    except Exception as e:
        rag_requests.labels(status='error', cached='false').inc()
        logger.error(f"[{correlation_id}] RAG search error: {e}")
        raise HTTPException(status_code=500, detail=f"RAG search failed: {str(e)}")

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
    
    # Test Supabase connection
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
        "status": "healthy" if redis_status == "ok" and supabase_status == "ok" else "degraded",
        "service": "rag",
        "redis": redis_status,
        "supabase": supabase_status,
        "similarity_threshold": SIMILARITY_THRESHOLD,
        "version": "2.0.0"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)
