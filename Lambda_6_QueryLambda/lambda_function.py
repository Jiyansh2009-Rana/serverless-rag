import os
import json
import re
import math
import uuid
import logging
import boto3
import requests
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from supabase import create_client, Client
import groq

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Environment Configurations ──
AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "global-rag-documents")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
JINA_API_KEY = os.environ.get("JINA_API_KEY", "")
REDIS_PROXY_LAMBDA_NAME = os.environ.get("REDIS_PROXY_LAMBDA_NAME", "Redis-lambda")

# ── Clients Initialization ──
s3_client = boto3.client("s3", region_name=AWS_REGION)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)

supabase: Optional[Client] = (
    create_client(SUPABASE_URL, SUPABASE_KEY)
    if SUPABASE_URL and SUPABASE_KEY
    else None
)

groq_client = groq.Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

LANGUAGE_INSTRUCTIONS = {
    "English": "Answer in English.",
    "Hindi": "हिंदी में उत्तर दें।",
    "French": "Répondez en français.",
    "German": "Antworten Sie auf Deutsch.",
    "Spanish": "Responde en español.",
    "Arabic": "أجب باللغة العربية.",
    "Chinese": "用中文回答。",
    "Japanese": "日本語で答えてください。",
}

DEFAULT_SYSTEM_PROMPT = """You are an intelligent document assistant for an enterprise RAG platform.
Answer the user's question using ONLY the context provided below.
If the context does not contain enough information, say so clearly.
Do not fabricate or hallucinate any information not present in the context.
Be concise, accurate, and professional. If the context does not contain enough information, state clearly what is missing instead of answering from outside knowledge."""

# ─────────────────────────────────────────────────────────────────────────────
# REDIS PROXY HELPER
# ─────────────────────────────────────────────────────────────────────────────
def invoke_redis_proxy(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronously invokes Redis Proxy Lambda (Redis-lambda)."""
    try:
        response = lambda_client.invoke(
            FunctionName=REDIS_PROXY_LAMBDA_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )
        payload_bytes = response["Payload"].read()
        res_json = json.loads(payload_bytes.decode("utf-8"))
        if res_json.get("statusCode") != 200:
            raise RuntimeError(f"Redis Proxy error: {res_json.get('body')}")
        body = res_json.get("body")
        return json.loads(body) if isinstance(body, str) else (body or {})
    except Exception as e:
        logger.error(f"Failed to invoke Redis Proxy from Lambda 6: {e}")
        raise

# ─────────────────────────────────────────────────────────────────────────────
# 1. QUERY VECTORIZATION
# ─────────────────────────────────────────────────────────────────────────────
def get_jina_query_embedding(query_text: str) -> List[float]:
    """Generates 1024-dimensional query embedding using Jina CLIP v2."""
    if not JINA_API_KEY:
        raise ValueError("JINA_API_KEY environment variable is not configured.")

    url = "https://api.jina.ai/v1/embeddings"
    headers = {
        "Authorization": f"Bearer {JINA_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "jina-clip-v2",
        "normalized": True,
        "dimension": 1024,
        "input": [{"text": query_text}]
    }
    res = requests.post(url, json=payload, headers=headers, timeout=30)
    res.raise_for_status()
    data = res.json().get("data", [])
    if not data:
        raise ValueError("Failed to retrieve query embedding from Jina API.")
    return data[0]["embedding"]

# ─────────────────────────────────────────────────────────────────────────────
# 2. HYBRID RETRIEVAL (SUPABASE & REDIS)
# ─────────────────────────────────────────────────────────────────────────────
def retrieve_global_supabase_hybrid(
    query_text: str,
    query_embedding: List[float],
    org_id: str,
    match_count: int = 10
) -> List[Dict[str, Any]]:
    """Executes Supabase RPC match_document_chunks_hybrid (70% Vector + 30% Keyword)."""
    if not supabase:
        logger.warning("Supabase client is uninitialized.")
        return []

    try:
        embedding_str = f"[{','.join(map(str, query_embedding))}]"
        rpc_res = supabase.rpc(
            "match_document_chunks_hybrid",
            {
                "query_text": query_text,
                "query_embedding": embedding_str,
                "match_count": match_count,
                "filter_org_id": org_id
            }
        ).execute()

        raw_chunks = rpc_res.data or []
        doc_ids = list(set([c["document_id"] for c in raw_chunks if c.get("document_id")]))

        # Fetch document filenames from registry
        doc_names = {}
        if doc_ids:
            reg_res = supabase.table("document_registry").select("id, file_name").in_("id", doc_ids).execute()
            if reg_res.data:
                doc_names = {r["id"]: r["file_name"] for r in reg_res.data}

        results = []
        for c in raw_chunks:
            doc_id = c.get("document_id", "")
            results.append({
                "chunk_id": c.get("chunk_id") or c.get("id", ""),
                "document_id": doc_id,
                "document_name": doc_names.get(doc_id, doc_id),
                "org_id": c.get("org_id", org_id),
                "page_number": c.get("page_number", 1),
                "chunk_index": c.get("chunk_index", 0),
                "text": c.get("text", ""),
                "similarity_score": float(c.get("similarity_score", 0.0)),
                "upload_mode": "global",
            })
        return results
    except Exception as e:
        logger.error(f"Error in Supabase hybrid retrieval: {e}")
        return []


def compute_keyword_score(query: str, text: str) -> float:
    """Computes keyword match score for Redis chunks."""
    q_tokens = set(re.findall(r'\w+', query.lower()))
    t_tokens = re.findall(r'\w+', text.lower())
    if not q_tokens or not t_tokens:
        return 0.0
    matches = sum(1 for token in t_tokens if token in q_tokens)
    return min(1.0, matches / math.sqrt(len(t_tokens)))


def retrieve_local_redis_hybrid(
    query_text: str,
    query_embedding: List[float],
    user_id: str,
    org_id: str,
    match_count: int = 10
) -> List[Dict[str, Any]]:
    """70% vector + 30% keyword over this user's local chunks stored in Redis."""
    try:
        scan = invoke_redis_proxy({
            "action": "scan_keys",
            "pattern": f"local:{user_id}:*:chunk:*",
            "limit": 300,
        })
        chunk_keys = scan.get("keys", [])
        if not chunk_keys:
            return []

        scored_chunks = []
        for i in range(0, len(chunk_keys), 50):   # keep each Lambda response far below 6 MB
            res = invoke_redis_proxy({"action": "get_batch", "keys": chunk_keys[i:i + 50]})
            for raw_val in res.get("values", {}).values():
                if not raw_val:
                    continue
                item = json.loads(raw_val) if isinstance(raw_val, str) else raw_val
                if item.get("org_id") != org_id or item.get("user_id") != user_id:
                    continue
                stored_emb = item.get("embedding", [])
                if not stored_emb:
                    continue
                vec_score = sum(a * b for a, b in zip(query_embedding, stored_emb))
                kw_score = compute_keyword_score(query_text, item.get("text", ""))
                scored_chunks.append({
                    "chunk_id": item.get("chunk_id", ""),
                    "document_id": item.get("doc_id", ""),
                    "document_name": item.get("file_name", "local_doc"),
                    "org_id": item.get("org_id", org_id),
                    "page_number": item.get("page_number", 1),
                    "chunk_index": item.get("chunk_index", 0),
                    "text": item.get("text", ""),
                    "similarity_score": float(0.7 * vec_score + 0.3 * kw_score),
                    "upload_mode": "local",
                })
        scored_chunks.sort(key=lambda x: x["similarity_score"], reverse=True)
        return scored_chunks[:match_count]
    except Exception as e:
        logger.error(f"Error in Redis hybrid retrieval: {e}")
        return []

# ─────────────────────────────────────────────────────────────────────────────
# 3. CONTEXT RERANKING (JINA RERANKER V3)
# ─────────────────────────────────────────────────────────────────────────────
def rerank_context_chunks(
    query_text: str,
    chunks: List[Dict[str, Any]],
    top_n: int = 5
) -> List[Dict[str, Any]]:
    """Reranks retrieved context chunks down to top_n using Jina Reranker v3."""
    if not chunks:
        return []

    if len(chunks) <= top_n:
        return chunks

    if not JINA_API_KEY:
        logger.warning("JINA_API_KEY not configured. Skipping reranking.")
        return chunks[:top_n]

    url = "https://api.jina.ai/v1/rerank"
    headers = {
        "Authorization": f"Bearer {JINA_API_KEY}",
        "Content-Type": "application/json"
    }

    doc_texts = [c.get("text", "") for c in chunks]
    payload = {
        "model": "jina-reranker-v3",
        "query": query_text,
        "documents": doc_texts,
        "top_n": top_n
    }

    try:
        res = requests.post(url, json=payload, headers=headers, timeout=15)
        res.raise_for_status()
        reranked_data = res.json().get("results", [])

        reranked_chunks = []
        for item in reranked_data:
            idx = item["index"]
            chunk = chunks[idx].copy()
            chunk["rerank_score"] = float(item.get("relevance_score", chunk["similarity_score"]))
            reranked_chunks.append(chunk)

        return reranked_chunks
    except Exception as e:
        logger.error(f"Jina Reranker v3 error: {e}. Falling back to top {top_n} hybrid results.")
        return chunks[:top_n]

# ─────────────────────────────────────────────────────────────────────────────
# 4 & 5. RESPONSE GENERATION & CITATION MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────
def generate_presigned_citation_url(org_id: str, doc_id: str, filename: str, page_number: int) -> str:
    """Generates presigned S3 URL mapped to retrieved global document."""
    s3_key = f"{org_id}/{doc_id}/{filename}"
    try:
        url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET_NAME, "Key": s3_key},
            ExpiresIn=3600
        )
        if filename.lower().endswith(".pdf"):
            url += f"#page={page_number}"
        return url
    except Exception as e:
        logger.error(f"Failed to generate presigned citation URL for {s3_key}: {e}")
        return ""


def build_sources_citation(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Builds clickable citations for global chunks and text previews for local chunks."""
    sources = []
    for c in chunks:
        upload_mode = c.get("upload_mode", "global")
        doc_id = c.get("document_id", "")
        doc_name = c.get("document_name", "")
        org_id = c.get("org_id", "")
        page_num = c.get("page_number", 1)
        raw_text = c.get("text", "")

        is_image = isinstance(raw_text, str) and raw_text.startswith("data:image/")
        preview_text = f"[Image Document: {doc_name}]" if is_image else raw_text[:300]

        if upload_mode == "global":
            doc_url = generate_presigned_citation_url(org_id, doc_id, doc_name, page_num)
        else:
            # Local mode: Just preview chunk content, no presigned S3 URL
            doc_url = None

        sources.append({
            "chunk_id": c.get("chunk_id"),
            "document_id": doc_id,
            "document_name": doc_name,
            "page_number": page_num,
            "chunk_index": c.get("chunk_index", 0),
            "similarity_score": round(c.get("rerank_score", c.get("similarity_score", 0.0)), 4),
            "text_preview": preview_text,
            "org_id": org_id,
            "upload_mode": upload_mode,
            "document_url": doc_url,
            "is_image": is_image,
        })
    return sources

# ─────────────────────────────────────────────────────────────────────────────
# 6. STATE & AUDIT LOGGING
# ─────────────────────────────────────────────────────────────────────────────
def save_chat_history(user_id: str, org_id: str, session_id: str, query: str, answer: str, query_mode: str) -> None:
    if not supabase:
        return
    try:
        supabase.table("chat_history").insert({
            "user_id": user_id,
            "session_id": session_id,
            "org_id": org_id,
            "query": query,
            "answer": answer,
            "query_mode": query_mode,
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"Failed to record chat history: {e}")


def log_query_audit(user_id: str, org_id: str, query: str, query_mode: str, sources_found: int, ip_address: str) -> None:
    if not supabase:
        return
    try:
        supabase.table("audit_log").insert({
            "event_type": "rag_query",
            "user_id": user_id,
            "org_id": org_id,
            "query_text": query[:500],
            "query_mode": query_mode,
            "sources_found": sources_found,
            "ip_address": ip_address,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"Failed to record audit log: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN LAMBDA HANDLER
# ─────────────────────────────────────────────────────────────────────────────
def handler(event, context):
    logger.info("Lambda 6 QueryLambda invoked")

    # Authorizer Context Extraction
    authorizer_ctx = event.get("requestContext", {}).get("authorizer", {}).get("lambda", {})
    user_id = authorizer_ctx.get("user_id")
    org_id = authorizer_ctx.get("org_id")
    role = authorizer_ctx.get("role", "User")
    email = authorizer_ctx.get("email", "user")

    if not user_id:
        authorizer_ctx = event.get("requestContext", {}).get("authorizer", {}).get("context", {})
        user_id = authorizer_ctx.get("user_id")
        org_id = authorizer_ctx.get("org_id")
        role = authorizer_ctx.get("role", "User")
        email = authorizer_ctx.get("email", "user")

    if not user_id or not org_id:
        return {
            "statusCode": 401,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"detail": "Unauthorized: Missing authentication context"})
        }

    # Request Body Parsing
    try:
        body = json.loads(event.get("body", "{}") or "{}")
    except Exception:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"detail": "Invalid JSON payload"})
        }

    user_query = body.get("query", "").strip()
    upload_mode = body.get("upload_mode", "global").lower()
    session_id = body.get("session_id") or f"session_{uuid.uuid4().hex[:12]}"
    language = body.get("language", "English")
    system_prompt = body.get("system_prompt")
    ip_address = event.get("requestContext", {}).get("http", {}).get("sourceIp", "0.0.0.0")

    if not user_query:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"detail": "Query text cannot be empty."})
        }

    # 1. Query Vectorization
    try:
        query_embedding = get_jina_query_embedding(user_query)
    except Exception as e:
        logger.error(f"Query embedding failed: {e}")
        return {
            "statusCode": 502,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"detail": "Embedding service unavailable. Please retry."})
        }

    # 2. Hybrid Retrieval according to mode
    if upload_mode == "global":
        retrieved_chunks = retrieve_global_supabase_hybrid(user_query, query_embedding, org_id, match_count=10)
    elif upload_mode == "local":
        retrieved_chunks = retrieve_local_redis_hybrid(user_query, query_embedding, user_id, org_id, match_count=10)
    elif upload_mode == "both":
        supabase_chunks = retrieve_global_supabase_hybrid(user_query, query_embedding, org_id, match_count=5)
        redis_chunks = retrieve_local_redis_hybrid(user_query, query_embedding, user_id, org_id, match_count=5)
        retrieved_chunks = supabase_chunks + redis_chunks
    else:
        return {
            "statusCode": 400,
            "body": json.dumps({"detail": f"Invalid upload_mode: {upload_mode}"})
        }

    # 3. Context Reranking with Jina Reranker v3 (Top 10 -> Top 5)
    reranked_chunks = rerank_context_chunks(user_query, retrieved_chunks, top_n=5)
        # Nothing relevant found: answer without calling the LLM (prevents made-up answers)
    if not reranked_chunks:
        no_ctx_answer = "I could not find anything relevant to this question in the available documents."
        save_chat_history(user_id, org_id, session_id, user_query, no_ctx_answer, upload_mode)
        log_query_audit(user_id, org_id, user_query, upload_mode, 0, ip_address)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"session_id": session_id, "answer": no_ctx_answer, "sources": []})
        }

    # 4 & 5. Citation Management
    sources = build_sources_citation(reranked_chunks)

    # Context formatting for LLM
    context_parts = []
    for idx, chunk in enumerate(reranked_chunks, start=1):
        context_parts.append(
            f"[Source {idx} | {chunk['document_name']} | Page {chunk['page_number']}]\n{chunk['text']}"
        )
    context_text = "\n\n---\n\n".join(context_parts)

    user_name = email.split("@")[0] if email else "user"
    lang_inst = LANGUAGE_INSTRUCTIONS.get(language, "Answer in English.")
    # Only admins may override the system prompt
    base_prompt = system_prompt if (system_prompt and role in ("Admin", "Super Admin")) else DEFAULT_SYSTEM_PROMPT    
    full_system_prompt = (
        f"{base_prompt}\n\n"
        f"User Identity: You are speaking with {user_name}.\n"
        f"Language instruction: {lang_inst}\n\n"
        f"Context from documents:\n{context_text}"
    )

    # Stream generation via Groq API
    models_to_try = ["llama-3.3-70b-versatile", "qwen/qwen3.6-27b"]
    llm_stream = None
    if not groq_client:
        return {
            "statusCode": 503,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"detail": "LLM is not configured (GROQ_API_KEY missing)."})
        }
    for model_name in models_to_try:
        try:
            llm_stream = groq_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": full_system_prompt},
                    {"role": "user", "content": user_query},
                ],
                temperature=0.3,
                stream=True,
            )
            break
        except Exception as e:
            logger.warning(f"Failed to initialize stream with {model_name}: {e}")

    if not llm_stream:
        return {
            "statusCode": 502,
            "body": json.dumps({"detail": "All LLM models failed or hit token limits."})
        }

    # Synthesize LLM answer and log
    full_answer = ""
    try:
        for chunk in llm_stream:
            token = chunk.choices[0].delta.content
            if token:
                full_answer += token
    except Exception as e:
        logger.error(f"LLM stream interrupted: {e}")
        if not full_answer:
            return {
                "statusCode": 502,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"detail": "LLM generation failed. Please retry."})
            }

    # 6. Record State & Audit Logging
    save_chat_history(user_id, org_id, session_id, user_query, full_answer, upload_mode)
    log_query_audit(user_id, org_id, user_query, upload_mode, len(reranked_chunks), ip_address)

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*"
        },
        "body": json.dumps({
            "session_id": session_id,
            "answer": full_answer,
            "sources": sources
        })
    }