import os
import json
import logging
import boto3
from typing import Dict, Any, List, Optional
from supabase import create_client, Client

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
REDIS_PROXY_LAMBDA_NAME = os.environ.get("REDIS_PROXY_LAMBDA_NAME", "Redis-lambda")
LOCAL_SESSION_TTL = int(os.environ.get("LOCAL_SESSION_TTL", "3600"))
AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")

lambda_client = boto3.client("lambda", region_name=AWS_REGION)
s3_client = boto3.client("s3", region_name=AWS_REGION)

supabase: Optional[Client] = (
    create_client(SUPABASE_URL, SUPABASE_KEY)
    if SUPABASE_URL and SUPABASE_KEY
    else None
)


def invoke_redis_proxy(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Helper to communicate with Redis Proxy Lambda, compatible with Auth Lambda response structure."""
    try:
        response = lambda_client.invoke(
            FunctionName=REDIS_PROXY_LAMBDA_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )
        payload_bytes = response["Payload"].read()
        res_json = json.loads(payload_bytes.decode("utf-8"))

        if res_json.get("statusCode") != 200:
            err_msg = res_json.get("body", "Unknown Redis Proxy Error")
            raise RuntimeError(f"Redis Proxy error ({res_json.get('statusCode')}): {err_msg}")

        body = res_json.get("body")
        if isinstance(body, str):
            return json.loads(body)
        return body or {}
    except Exception as e:
        logger.error(f"Error invoking Redis proxy: {e}")
        raise


def process_global_chunks(chunks: List[Dict[str, Any]]) -> None:
    """Ingests global document chunks or image records into Supabase idempotently."""
    if not supabase:
        raise RuntimeError("Supabase client is not initialized in Lambda 5.")

    document_chunks_records = []
    image_store_records = []
    page_registry_map = {}

    for item in chunks:
        doc_type = item.get("doc_type", "")
        upload_mode = item.get("upload_mode", "global")

        embedding = item.get("embedding")
        if isinstance(embedding, list):
            embedding_str = f"[{','.join(map(str, embedding))}]"
        else:
            embedding_str = embedding

        if doc_type == "image":
            data_url = item.get("data_url", "")
            s3_bucket = item.get("s3_bucket")
            s3_key = item.get("s3_key")

            if not data_url and s3_bucket and s3_key:
                try:
                    s3_obj = s3_client.get_object(Bucket=s3_bucket, Key=s3_key)
                    img_bytes = s3_obj["Body"].read()
                    import base64, mimetypes
                    mime_type = mimetypes.guess_type(s3_key)[0] or "image/jpeg"
                    b64_str = base64.b64encode(img_bytes).decode("utf-8")
                    data_url = f"data:{mime_type};base64,{b64_str}"
                except Exception as e:
                    logger.error(f"Failed to fetch image from S3 ({s3_key}): {e}")

            image_store_records.append({
                "id": item["chunk_id"],
                "document_id": item["doc_id"],
                "org_id": item["org_id"],
                "file_name": item["file_name"],
                "data_url": data_url or "",
                "embedding": embedding_str,
                "upload_mode": upload_mode,
                "uploaded_by": item["user_id"],
                "user_id": item["user_id"]
            })
        else:
            document_chunks_records.append({
                "id": item["chunk_id"],
                "document_id": item["doc_id"],
                "org_id": item["org_id"],
                "page_number": item["page_number"],
                "chunk_index": item["chunk_index"],
                "text": item["text"],
                "embedding": embedding_str,
                "upload_mode": upload_mode,
                "set_id": item.get("set_id"),
                "user_id": item["user_id"]
            })

            page_num = item["page_number"]
            doc_id = item["doc_id"]
            if page_num not in page_registry_map:
                page_registry_map[page_num] = {
                    "document_id": doc_id,
                    "org_id": item["org_id"],
                    "page_number": page_num,
                    "page_hash": item.get("page_hash", ""),
                    "chunk_count": 0,
                    "page_chunk_count": item.get("page_chunk_count")
                }
            page_registry_map[page_num]["chunk_count"] += 1

    if document_chunks_records:
        supabase.table("document_chunks").upsert(
            document_chunks_records, on_conflict="id"
        ).execute()
    
    if image_store_records:
        supabase.table("image_store").upsert(
            image_store_records, on_conflict="id"
        ).execute()

    if page_registry_map:
        # Save a page's hash only once ALL its chunks are stored; otherwise a failed SQS message
        # would make the next upload skip that page and its chunks would be missing forever.
        complete = []
        for page_num, rec in page_registry_map.items():
            expected = rec.pop("page_chunk_count", None)
            if expected is None:
                complete.append(rec)
                continue
            res = (
                supabase.table("document_chunks").select("id", count="exact")
                .eq("document_id", rec["document_id"]).eq("page_number", page_num).execute()
            )
            if (res.count or 0) >= expected:
                rec["chunk_count"] = expected
                complete.append(rec)
        if complete:
            supabase.table("page_registry").upsert(
                complete, on_conflict="document_id,page_number"
            ).execute()


def process_local_chunks(chunks: List[Dict[str, Any]]) -> None:
    """Ingests local document chunks into Redis, performing page delta updates and cleanup."""
    if not chunks:
        return

    first_item = chunks[0]
    user_id = first_item["user_id"]
    doc_id = first_item["doc_id"]
    org_id = first_item["org_id"]
    filename = first_item["file_name"]
    file_hash_val = first_item.get("file_hash", "")
    total_pages = first_item.get("total_pages", 1)

    pages_dict: Dict[int, List[Dict[str, Any]]] = {}
    for item in chunks:
        p_num = item["page_number"]
        pages_dict.setdefault(p_num, []).append(item)

    page_nums = list(pages_dict.keys())
    hash_keys = [f"local:delta:{user_id}:{doc_id}:page:{p}" for p in page_nums]
    chunk_list_keys = [f"local:delta:{user_id}:{doc_id}:page:{p}:chunks" for p in page_nums]

    hashes_res = invoke_redis_proxy({"action": "get_batch", "keys": hash_keys})
    existing_hashes = hashes_res.get("values", {})

    chunks_res = invoke_redis_proxy({"action": "get_batch", "keys": chunk_list_keys})
    existing_chunk_lists = chunks_res.get("values", {})

    set_batch_items = []
    delete_keys = []

    for p_num, p_chunks in pages_dict.items():
        incoming_page_hash = p_chunks[0].get("page_hash", "")
        hash_key = f"local:delta:{user_id}:{doc_id}:page:{p_num}"
        chunk_list_key = f"local:delta:{user_id}:{doc_id}:page:{p_num}:chunks"

        old_hash = existing_hashes.get(hash_key)
        old_chunks_raw = existing_chunk_lists.get(chunk_list_key)

        if old_hash == incoming_page_hash and old_chunks_raw:
            continue

        if old_chunks_raw:
            try:
                old_chunk_ids = json.loads(old_chunks_raw) if isinstance(old_chunks_raw, str) else old_chunks_raw
                if isinstance(old_chunk_ids, list):
                    for old_cid in old_chunk_ids:
                        delete_keys.append(f"local:{user_id}:{doc_id}:chunk:{old_cid}")
            except Exception as e:
                logger.warning(f"Error parsing old chunk IDs for page {p_num}: {e}")

        new_chunk_ids = []
        for chk in p_chunks:
            cid = chk["chunk_id"]
            new_chunk_ids.append(cid)

            chunk_payload = {
                "chunk_id": cid,
                "doc_id": doc_id,
                "user_id": user_id,
                "org_id": org_id,
                "file_name": filename,
                "doc_type": chk.get("doc_type", ""),
                "page_number": p_num,
                "chunk_index": chk["chunk_index"],
                "text": chk.get("text", ""),
                "embedding": chk["embedding"],
                "upload_mode": "local",
                "set_id": chk.get("set_id", ""),
                "file_hash": file_hash_val,
                "stored_at": chk.get("stored_at", "")
            }

            set_batch_items.append({
                "key": f"local:{user_id}:{doc_id}:chunk:{cid}",
                "value": json.dumps(chunk_payload),
                "ttl": LOCAL_SESSION_TTL
            })

        set_batch_items.append({
            "key": hash_key,
            "value": incoming_page_hash,
            "ttl": LOCAL_SESSION_TTL
        })
        set_batch_items.append({
            "key": chunk_list_key,
            "value": json.dumps(new_chunk_ids),
            "ttl": LOCAL_SESSION_TTL
        })

    if delete_keys:
        invoke_redis_proxy({"action": "delete_batch", "keys": delete_keys})

    if set_batch_items:
        invoke_redis_proxy({"action": "set_batch", "items": set_batch_items, "ttl": LOCAL_SESSION_TTL})

    docmeta_key = f"local:docmeta:{user_id}:{doc_id}"
    docmeta_res = invoke_redis_proxy({"action": "get", "key": docmeta_key})
    existing_meta_raw = docmeta_res.get("value")

    previous_pages = []
    if existing_meta_raw:
        try:
            meta_json = json.loads(existing_meta_raw) if isinstance(existing_meta_raw, str) else existing_meta_raw
            previous_pages = meta_json.get("pages", [])
        except Exception:
            pass

    current_pages = sorted(list(set(previous_pages + page_nums)))
    removed_pages = [p for p in previous_pages if p > total_pages]

    if removed_pages:
        removed_hash_keys = [f"local:delta:{user_id}:{doc_id}:page:{p}" for p in removed_pages]
        removed_chunk_list_keys = [f"local:delta:{user_id}:{doc_id}:page:{p}:chunks" for p in removed_pages]

        rem_chunks_res = invoke_redis_proxy({"action": "get_batch", "keys": removed_chunk_list_keys})
        rem_chunks_map = rem_chunks_res.get("values", {})

        orphan_keys_to_del = list(removed_hash_keys) + list(removed_chunk_list_keys)
        for r_cl_key, r_cids_raw in rem_chunks_map.items():
            if r_cids_raw:
                try:
                    r_cids = json.loads(r_cids_raw) if isinstance(r_cids_raw, str) else r_cids_raw
                    for r_cid in r_cids:
                        orphan_keys_to_del.append(f"local:{user_id}:{doc_id}:chunk:{r_cid}")
                except Exception:
                    pass

        if orphan_keys_to_del:
            invoke_redis_proxy({"action": "delete_batch", "keys": orphan_keys_to_del})

        current_pages = [p for p in current_pages if p <= total_pages]

    from datetime import datetime, timezone
    new_docmeta = {
        "doc_id": doc_id,
        "user_id": user_id,
        "org_id": org_id,
        "file_name": filename,
        "file_hash": file_hash_val,
        "total_pages": total_pages,
        "pages": current_pages,
        "upload_mode": "local",
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

    invoke_redis_proxy({
        "action": "set",
        "key": docmeta_key,
        "value": json.dumps(new_docmeta),
        "ttl": LOCAL_SESSION_TTL
    })


def cleanup_local_s3_file(chunk_batch: List[Dict[str, Any]]) -> None:
    """Deletes temporary local mode S3 files post-ingestion."""
    if not chunk_batch:
        return
    first_item = chunk_batch[0]
    if first_item.get("upload_mode") == "local":
        s3_bucket = first_item.get("s3_bucket")
        s3_key = first_item.get("s3_key")
        if s3_bucket and s3_key:
            try:
                s3_client.delete_object(Bucket=s3_bucket, Key=s3_key)
                logger.info(f"Cleaned up temporary local S3 file: s3://{s3_bucket}/{s3_key}")
            except Exception as e:
                logger.warning(f"Failed to delete local temporary S3 object: {e}")


def handler(event, context):
    logger.info(f"VectorIngestLambda invoked with {len(event.get('Records', []))} record(s).")

    for record in event.get("Records", []):
        body_str = record.get("body", "[]")
        try:
            chunk_batch = json.loads(body_str)
        except Exception as e:
            logger.error(f"Failed to parse SQS record body JSON: {e}")
            continue

        if not chunk_batch or not isinstance(chunk_batch, list):
            continue

        upload_mode = chunk_batch[0].get("upload_mode", "global").lower()

        if upload_mode == "global":
            process_global_chunks(chunk_batch)
            supabase.table("document_registry").update({"status": "ready"}).eq("id", chunk_batch[0]["doc_id"]).execute()
        elif upload_mode == "local":
            process_local_chunks(chunk_batch)
            cleanup_local_s3_file(chunk_batch)
        else:
            logger.error(f"Unrecognized upload_mode: {upload_mode}")

    return {
        "statusCode": 200,
        "body": json.dumps({"status": "vectors_ingested_successfully"})
    }