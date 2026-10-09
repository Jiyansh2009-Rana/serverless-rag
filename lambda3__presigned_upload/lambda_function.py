import os
import json
import hashlib
import uuid
import logging
from datetime import datetime, timezone
import boto3
from botocore.exceptions import ClientError
from supabase import create_client, Client

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Environment Configurations ──
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "global-rag-documents")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")
s3_client = boto3.client("s3", region_name=AWS_REGION)
REDIS_PROXY_LAMBDA_NAME = os.environ.get("REDIS_PROXY_LAMBDA_NAME", "Redis-lambda")
LOCAL_SESSION_TTL = int(os.environ.get("LOCAL_SESSION_TTL", "3600"))
lambda_client = boto3.client("lambda", region_name=AWS_REGION)  

# Initialize Supabase Python Client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

def invoke_redis_proxy(payload: dict) -> dict:
    """Invokes Redis Proxy Lambda (Redis-lambda) synchronously and returns parsed body."""
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
        logger.error(f"Error invoking Redis proxy from Lambda 3: {e}")
        raise

def check_global_upload_permission(user_id: str, org_id: str, role: str) -> bool:
    """Verifies whether the user or organization is permitted to perform global uploads."""
    if role in ["Admin", "Super Admin"]:
        return True

    if not supabase:
        return False

    try:
        # 1. Check organization settings
        org_res = (
            supabase.table("organization_settings")
            .select("allow_user_global_upload")
            .eq("org_id", org_id)
            .execute()
        )
        if org_res.data and org_res.data[0].get("allow_user_global_upload") is True:
            return True

        # 2. Check individual user settings
        user_res = (
            supabase.table("users")
            .select("allow_global_upload")
            .eq("id", user_id)
            .execute()
        )
        if user_res.data and user_res.data[0].get("allow_global_upload") is True:
            return True
    except Exception as e:
        logger.error(f"Error checking upload permission via Supabase: {e}")

    return False

def check_document_status(file_hash_val: str, filename: str, org_id: str) -> dict:
    """Checks if the file is a duplicate, an alias, an update, or entirely new."""
    if not supabase:
        return {"type": "new", "doc_id": f"doc_{uuid.uuid4().hex[:12]}"}

    try:
        # 1. Check for Hash Match (Duplicate or Alias)
        hash_res = supabase.table("document_registry").select("id, file_name, status").eq("file_hash", file_hash_val).eq("org_id", org_id).execute()
        if hash_res.data:
            existing = hash_res.data[0]
            if existing["file_name"] == filename:
                return {"type": "duplicate", "doc_id": existing["id"], "old_name": existing["file_name"]}
            else:
                return {"type": "alias", "doc_id": existing["id"], "old_name": existing["file_name"]}

        # 2. Check for Name Match (Changed File / Delta Update)
        name_res = supabase.table("document_registry").select("id, file_hash, status").eq("file_name", filename).eq("org_id", org_id).execute()
        if name_res.data:
            return {"type": "update", "doc_id": name_res.data[0]["id"]}

    except Exception as e:
        logger.error(f"Error checking document status in Supabase: {e}")

    # 3. Completely New File
    return {"type": "new", "doc_id": f"doc_{uuid.uuid4().hex[:12]}"}

def resolve_local_document_status(user_id: str, org_id: str, filename: str, file_hash_val: str) -> dict:
    """Resolves stable doc_id, duplicate, alias, or update status for local uploads using Redis Proxy."""
    norm_filename_hash = hashlib.sha256(filename.strip().lower().encode("utf-8")).hexdigest()
    docmap_key = f"local:docmap:{user_id}:{norm_filename_hash}"
    filehash_key = f"local:filehash:{user_id}:{file_hash_val}"

    res = invoke_redis_proxy({"action": "get_batch", "keys": [docmap_key, filehash_key]})
    vals = res.get("values", {})

    docmap_raw = vals.get(docmap_key)
    filehash_raw = vals.get(filehash_key)

    docmap_data = json.loads(docmap_raw) if docmap_raw else None
    filehash_data = json.loads(filehash_raw) if filehash_raw else None

    is_duplicate = False
    is_update = False

    if filehash_data:
        existing_doc_id = filehash_data["doc_id"]
        existing_filename = filehash_data["file_name"]

        if existing_filename == filename:
            doc_id = existing_doc_id
            is_duplicate = True
        else:
            doc_id = existing_doc_id
            is_duplicate = True
            audit_key = f"local:alias_audit:{user_id}:{doc_id}:{uuid.uuid4().hex[:8]}"
            invoke_redis_proxy({
                "action": "set",
                "key": audit_key,
                "value": json.dumps({
                    "event_type": "local_alias_detected",
                    "doc_id": doc_id,
                    "user_id": user_id,
                    "org_id": org_id,
                    "alias_filename": filename,
                    "original_filename": existing_filename,
                    "file_hash": file_hash_val,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }),
                "ttl": LOCAL_SESSION_TTL
            })
    elif docmap_data:
        doc_id = docmap_data["doc_id"]
        is_update = True
    else:
        doc_id = f"doc_{uuid.uuid4().hex[:12]}"

    now_iso = datetime.now(timezone.utc).isoformat()
    new_docmap = {
        "doc_id": doc_id,
        "file_name": filename,
        "user_id": user_id,
        "org_id": org_id,
        "file_hash": file_hash_val,
        "created_at": docmap_data.get("created_at", now_iso) if docmap_data else now_iso,
        "updated_at": now_iso
    }

    invoke_redis_proxy({
        "action": "set_batch",
        "items": [
            {"key": docmap_key, "value": json.dumps(new_docmap), "ttl": LOCAL_SESSION_TTL},
            {"key": filehash_key, "value": json.dumps({"doc_id": doc_id, "file_name": filename}), "ttl": LOCAL_SESSION_TTL}
        ]
    })

    return {
        "doc_id": doc_id,
        "is_duplicate": is_duplicate,
        "is_update": is_update
    }

def handler(event, context):
    """
    Lambda 3: PresignedUploadLambda (using official supabase module)
    Triggered by HTTP POST /api/v1/upload/presigned-url
    """
    logger.info("PresignedUploadLambda invoked")

    # 1. Extract context from LambdaAuthorizer
    authorizer_ctx = event.get("requestContext", {}).get("authorizer", {}).get("lambda", {})
    user_id = authorizer_ctx.get("user_id")
    org_id = authorizer_ctx.get("org_id")
    role = authorizer_ctx.get("role", "User")

    if not user_id:
        authorizer_ctx = event.get("requestContext", {}).get("authorizer", {}).get("context", {})
        user_id = authorizer_ctx.get("user_id")
        org_id = authorizer_ctx.get("org_id")
        role = authorizer_ctx.get("role", "User")

    if not user_id or not org_id:
        return {
            "statusCode": 401,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"detail": "Unauthorized: Missing authentication context"})
        }

    # 2. Parse request body
    try:
        body = json.loads(event.get("body", "{}") or "{}")
    except Exception:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"detail": "Invalid JSON payload"})
        }

    filename = body.get("filename", "").strip()
    file_hash_val = body.get("file_hash", "").strip()
    upload_mode = body.get("upload_mode", "global").lower()
    confirmed = body.get("confirmed", True)

    if not filename:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"detail": "Missing required field: filename"})
        }

    if not confirmed:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"detail": "Upload requires explicit user consent confirmation"})
        }

    # 3. Check upload permissions
    if upload_mode == "global" and not check_global_upload_permission(user_id, org_id, role):
        return {
            "statusCode": 403,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"detail": "Global upload permission is disabled for your organization/account."})
        }

    # 4. Check for duplicate content (Delta / Registry Check)
    if upload_mode == "global":
        if not check_global_upload_permission(user_id, org_id, role):
            return {
                "statusCode": 403,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"detail": "Global upload permission disabled."})
            }
        is_duplicate = False
        is_update = False
        doc_id = f"doc_{uuid.uuid4().hex[:12]}"
        if supabase:
            res = supabase.table("document_registry").select("id, file_name").eq("file_hash", file_hash_val).eq("org_id", org_id).execute()
            if res.data:
                doc_id = res.data[0]["id"]
                is_duplicate = True
            else:
                res_name = supabase.table("document_registry").select("id").eq("file_name", filename).eq("org_id", org_id).execute()
                if res_name.data:
                    doc_id = res_name.data[0]["id"]
                    is_update = True
    else:
        local_status = resolve_local_document_status(user_id, org_id, filename, file_hash_val)
        doc_id = local_status["doc_id"]
        is_duplicate = local_status["is_duplicate"]
        is_update = local_status["is_update"]

    s3_key = f"{org_id}/{doc_id}/{filename}"

    # 5. Pre-register / Update document in Supabase
    if supabase and not is_duplicate:
        try:
            doc_record = {
                "id": doc_id,
                "org_id": org_id,
                "file_name": filename,
                "file_hash": file_hash_val,
                "uploaded_by": user_id,
                "status": "uploading",
                "s3_bucket": S3_BUCKET_NAME,
                "s3_key": s3_key,
                "uploaded_at": datetime.now(timezone.utc).isoformat()
            }
            supabase.table("document_registry").upsert(doc_record).execute()

            # Record audit log for new/updated upload
            event_type = "presigned_upload_updated" if is_update else "presigned_upload_generated"
            supabase.table("audit_log").insert({
                "event_type": event_type,
                "user_id": user_id,
                "org_id": org_id,
                "doc_id": doc_id,
                "file_name": filename,
                "file_hash": file_hash_val,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }).execute()
        except Exception as e:
            logger.error(f"Error persisting to Supabase: {e}")

    # 6. Generate S3 Presigned POST payload
    try:
        presigned_post = s3_client.generate_presigned_post(
            Bucket=S3_BUCKET_NAME,
            Key=s3_key,
            Fields={
                "acl": "private",
                "x-amz-meta-org-id": org_id,
                "x-amz-meta-doc-id": doc_id,
                "x-amz-meta-user-id": user_id,
                "x-amz-meta-upload-mode": upload_mode,
                "x-amz-meta-file-hash": file_hash_val,
                "x-amz-meta-is-update": str(is_update).lower()  # Pass update flag to Lambda 4
            },
            Conditions=[
                ["content-length-range", 1, 100 * 1024 * 1024],  # 1 Byte to 100 MB
                {"acl": "private"},
                {"x-amz-meta-org-id": org_id},
                {"x-amz-meta-doc-id": doc_id},
                {"x-amz-meta-user-id": user_id},
                {"x-amz-meta-upload-mode": upload_mode},
                {"x-amz-meta-file-hash": file_hash_val},
                {"x-amz-meta-is-update": str(is_update).lower()}
            ],
            ExpiresIn=480  # 8 minutes
        )
    except ClientError as e:
        logger.error(f"Error generating presigned post: {e}")
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"detail": f"Failed to generate S3 upload credentials: {str(e)}"})
        }

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*"
        },
        "body": json.dumps({
            "doc_id": doc_id,
            "s3_key": s3_key,
            "is_duplicate": is_duplicate,
            "is_update": is_update,
            "upload_data": presigned_post
        })
    }
