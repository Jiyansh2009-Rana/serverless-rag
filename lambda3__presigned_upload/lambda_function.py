import os
import json
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

# Initialize AWS S3 Client
s3_client = boto3.client("s3", region_name=AWS_REGION)

# Initialize Supabase Python Client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

def check_global_upload_permission(user_id: str, org_id: str, role: str) -> bool:
    """Verifies whether the user or organization is permitted to perform global uploads."""
    if role in ["Admin", "Super Admin"]:
        return True

    if not supabase:
        return True

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

def check_file_duplicate(file_hash_val: str, org_id: str) -> dict | None:
    """Checks if a file with identical hash exists in document_registry."""
    if not supabase or not file_hash_val:
        return None
    try:
        res = (
            supabase.table("document_registry")
            .select("id, file_name, status")
            .eq("file_hash", file_hash_val)
            .eq("org_id", org_id)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"Error checking file duplicate in Supabase: {e}")
        return None

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
    existing_doc = None
    is_duplicate = False
    if upload_mode == "global" and file_hash_val:
        existing_doc = check_file_duplicate(file_hash_val, org_id)
        if existing_doc:
            doc_id = existing_doc["id"]
            is_duplicate = True
            logger.info(f"Duplicate file detected. Reusing doc_id: {doc_id}")
        else:
            doc_id = f"doc_{uuid.uuid4().hex[:12]}"
    else:
        doc_id = f"doc_{uuid.uuid4().hex[:12]}"

    # Deterministic S3 Key
    s3_key = f"{org_id}/{doc_id}/{filename}"

    # 5. Pre-register / Update document in Supabase
    if supabase:
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

            # Record audit log
            supabase.table("audit_log").insert({
                "event_type": "presigned_upload_generated",
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
                "x-amz-meta-file-hash": file_hash_val
            },
            Conditions=[
                ["content-length-range", 1, 100 * 1024 * 1024],  # 1 Byte to 100 MB
                {"acl": "private"},
                {"x-amz-meta-org-id": org_id},
                {"x-amz-meta-doc-id": doc_id},
                {"x-amz-meta-user-id": user_id},
                {"x-amz-meta-upload-mode": upload_mode},
                {"x-amz-meta-file-hash": file_hash_val}
            ],
            ExpiresIn=480  # 5 minutes
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
            "upload_data": presigned_post
        })
    }
