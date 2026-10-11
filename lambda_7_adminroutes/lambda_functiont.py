import os
import json
import logging
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from supabase import create_client, Client

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Environment Configurations ──
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "global-rag-documents")
REDIS_PROXY_LAMBDA_NAME = os.environ.get("REDIS_PROXY_LAMBDA_NAME", "Redis-lambda")

AUDIT_DEFAULT_LIMIT = 50
AUDIT_MAX_LIMIT = 200

ROLE_USER = "User"
ROLE_ADMIN = "Admin"
ROLE_SUPER_ADMIN = "Super Admin"

# Audit-log columns hidden from each role:
#   Admin       -> ip_address hidden
#   Super Admin -> ip_address and query_text hidden
AUDIT_HIDDEN_FIELDS = {
    ROLE_ADMIN: {"ip_address"},
    ROLE_SUPER_ADMIN: {"ip_address", "query_text"},
}

s3_client = boto3.client("s3", region_name=AWS_REGION)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
class HttpError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def build_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def get_auth_context(event: dict) -> dict:
    """Reads the identity injected by lambda1_authorizer (HTTP API v2 simple or REST format)."""
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    ctx = authorizer.get("lambda") or authorizer.get("context") or {}
    return {
        "user_id": ctx.get("user_id"),
        "org_id": ctx.get("org_id"),
        "role": ctx.get("role", ROLE_USER),
        "email": ctx.get("email", ""),
    }


def require_roles(ctx: dict, allowed: tuple) -> None:
    if ctx["role"] not in allowed:
        raise HttpError(403, f"Access denied. Required role: {list(allowed)}")


def parse_json_body(event: dict) -> dict:
    raw = event.get("body") or "{}"
    try:
        body = json.loads(raw)
    except Exception:
        raise HttpError(400, "Request body must be valid JSON.")
    if not isinstance(body, dict):
        raise HttpError(400, "Request body must be a JSON object.")
    return body


def path_param(event: dict, name: str) -> str:
    value = (event.get("pathParameters") or {}).get(name)
    if not value:
        raise HttpError(400, f"Missing path parameter: {name}")
    return value


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def invoke_redis_delete(key: str) -> None:
    """Best-effort Redis key delete through the Redis proxy Lambda."""
    try:
        lambda_client.invoke(
            FunctionName=REDIS_PROXY_LAMBDA_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps({"action": "delete", "key": key}),
        )
    except Exception as e:
        logger.warning(f"Redis cleanup failed for {key}: {e}")


def delete_document_data(doc_id: str, ctx: dict) -> None:
    """
    Deletes a document everywhere (AWS layout):
      Supabase document_chunks -> page_registry -> document_registry -> S3 object -> audit_log.
    Admin is restricted to its own organisation; Super Admin may delete any document.
    """
    doc_res = supabase.table("document_registry").select("id, org_id, file_name").eq("id", doc_id).execute()
    if not doc_res.data:
        raise HttpError(404, "Document not found.")

    doc = doc_res.data[0]
    doc_org_id = doc["org_id"]
    file_name = doc.get("file_name", "")

    if ctx["role"] != ROLE_SUPER_ADMIN and doc_org_id != ctx["org_id"]:
        raise HttpError(403, "Access denied: Document belongs to another organization.")

    try:
        supabase.table("document_chunks").delete().eq("document_id", doc_id).execute()
    except Exception as e:
        logger.error(f"Failed to delete document chunks: {e}")
        raise HttpError(500, f"Failed to delete document chunks from DB: {e}")

    try:
        supabase.table("page_registry").delete().eq("document_id", doc_id).execute()
        supabase.table("document_registry").delete().eq("id", doc_id).execute()
    except Exception as e:
        logger.error(f"Failed to clear document registry: {e}")
        raise HttpError(500, f"Failed to clear document registry: {e}")

    if file_name:
        try:
            s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=f"{doc_org_id}/{doc_id}/{file_name}")
        except ClientError as e:
            logger.warning(f"Could not remove file from S3: {e}")

    try:
        supabase.table("audit_log").insert({
            "event_type": "global_document_deleted",
            "user_id": ctx["user_id"],
            "org_id": doc_org_id,
            "doc_id": doc_id,
            "file_name": file_name,
            "timestamp": now_iso(),
        }).execute()
    except Exception as e:
        logger.warning(f"Audit log write failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Route handlers
# ─────────────────────────────────────────────────────────────────────────────
def list_org_users(event, ctx):
    require_roles(ctx, (ROLE_ADMIN, ROLE_SUPER_ADMIN))
    query = supabase.table("users").select("id, username, email, role, org_id, allow_global_upload, created_at")
    if ctx["org_id"]:
        query = query.eq("org_id", ctx["org_id"])
    res = query.order("created_at", desc=True).execute()
    return 200, {"users": res.data}


def get_global_upload_setting(event, ctx):
    # Any authenticated role may read its org's setting.
    org_id = ctx["org_id"] or ""
    allowed = False
    if org_id:
        res = (
            supabase.table("organization_settings")
            .select("allow_user_global_upload")
            .eq("org_id", org_id)
            .execute()
        )
        if res.data:
            allowed = bool(res.data[0].get("allow_user_global_upload", False))
    return 200, {"org_id": org_id, "allow_user_global_upload": allowed}


def set_global_upload_setting(event, ctx):
    require_roles(ctx, (ROLE_ADMIN, ROLE_SUPER_ADMIN))
    body = parse_json_body(event)
    value = body.get("allow_user_global_upload")
    if not isinstance(value, bool):
        raise HttpError(400, "'allow_user_global_upload' must be a boolean.")
    if not ctx["org_id"]:
        raise HttpError(400, "No organisation associated with this account.")

    supabase.table("organization_settings").upsert({
        "org_id": ctx["org_id"],
        "allow_user_global_upload": value,
        "updated_at": now_iso(),
    }).execute()
    return 200, {
        "message": "Global upload configuration updated successfully.",
        "allow_user_global_upload": value,
    }


def update_user_permission(event, ctx):
    require_roles(ctx, (ROLE_ADMIN, ROLE_SUPER_ADMIN))
    target_user_id = path_param(event, "target_user_id")
    body = parse_json_body(event)
    value = body.get("allow_global_upload")
    if not isinstance(value, bool):
        raise HttpError(400, "'allow_global_upload' must be a boolean.")

    check = supabase.table("users").select("id, org_id").eq("id", target_user_id).execute()
    if not check.data:
        raise HttpError(404, "User not found.")
    if ctx["role"] != ROLE_SUPER_ADMIN and check.data[0]["org_id"] != ctx["org_id"]:
        raise HttpError(403, "Access denied.")

    supabase.table("users").update({"allow_global_upload": value}).eq("id", target_user_id).execute()
    return 200, {
        "message": f"Global upload permission {'enabled' if value else 'disabled'}.",
        "user_id": target_user_id,
        "allow_global_upload": value,
    }


def delete_org_user(event, ctx):
    require_roles(ctx, (ROLE_ADMIN, ROLE_SUPER_ADMIN))
    target_user_id = path_param(event, "target_user_id")
    if ctx["user_id"] == target_user_id:
        raise HttpError(400, "You cannot delete your own admin account.")

    check = supabase.table("users").select("id, org_id").eq("id", target_user_id).execute()
    if not check.data:
        raise HttpError(404, "User not found.")
    if ctx["role"] != ROLE_SUPER_ADMIN and check.data[0]["org_id"] != ctx["org_id"]:
        raise HttpError(403, "Access denied: User belongs to another organization.")

    supabase.table("users").delete().eq("id", target_user_id).execute()
    invoke_redis_delete(f"user:{target_user_id}:meta")
    return 200, {"message": f"User {target_user_id} removed successfully."}


def list_org_documents(event, ctx):
    require_roles(ctx, (ROLE_ADMIN, ROLE_SUPER_ADMIN))
    query = supabase.table("document_registry").select("*")
    if ctx["org_id"]:
        query = query.eq("org_id", ctx["org_id"])
    res = query.order("uploaded_at", desc=True).execute()
    return 200, {"documents": res.data}


def delete_org_document(event, ctx):
    require_roles(ctx, (ROLE_ADMIN, ROLE_SUPER_ADMIN))
    doc_id = path_param(event, "doc_id")
    delete_document_data(doc_id, ctx)
    return 200, {"message": f"Document {doc_id} and all related chunks successfully deleted."}


def _parse_int(value, default: int, minimum: int, maximum: int, name: str) -> int:
    if value in (None, ""):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise HttpError(400, f"'{name}' must be an integer.")
    return max(minimum, min(n, maximum))


def _parse_iso(value: str, name: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except Exception:
        raise HttpError(400, f"'{name}' must be an ISO-8601 date/time, e.g. 2026-10-01T00:00:00Z.")


def get_audit_logs(event, ctx):
    """
    GET /api/v1/admin/audit-logs
      Super Admin -> all organisations (optional ?org_id= filter)
      Admin       -> only its own organisation (org_id taken from the JWT, never from the client)
    Query params: limit (default 50, max 200), page (default 1), event_type, user_id, from, to, org_id (Super Admin only)
    """
    require_roles(ctx, (ROLE_ADMIN, ROLE_SUPER_ADMIN))
    params = event.get("queryStringParameters") or {}

    limit = _parse_int(params.get("limit"), AUDIT_DEFAULT_LIMIT, 1, AUDIT_MAX_LIMIT, "limit")
    page = _parse_int(params.get("page"), 1, 1, 1_000_000, "page")
    offset = (page - 1) * limit

    query = supabase.table("audit_log").select("*", count="exact")

    requested_org = (params.get("org_id") or "").strip()
    if ctx["role"] == ROLE_SUPER_ADMIN:
        if requested_org:
            query = query.eq("org_id", requested_org)
    else:
        if not ctx["org_id"]:
            raise HttpError(403, "No organisation associated with this account.")
        if requested_org and requested_org != ctx["org_id"]:
            raise HttpError(403, "Access denied: you can only view your own organization's logs.")
        query = query.eq("org_id", ctx["org_id"])

    if params.get("event_type"):
        query = query.eq("event_type", params["event_type"].strip())
    if params.get("user_id"):
        query = query.eq("user_id", params["user_id"].strip())
    if params.get("from"):
        query = query.gte("timestamp", _parse_iso(params["from"], "from"))
    if params.get("to"):
        query = query.lte("timestamp", _parse_iso(params["to"], "to"))

    res = query.order("timestamp", desc=True).range(offset, offset + limit - 1).execute()

    hidden = AUDIT_HIDDEN_FIELDS[ctx["role"]]
    logs = [{k: v for k, v in row.items() if k not in hidden} for row in (res.data or [])]
    total = res.count if res.count is not None else len(logs)

    return 200, {
        "logs": logs,
        "page": page,
        "limit": limit,
        "total": total,
        "has_more": offset + len(logs) < total,
    }


ROUTES = {
    "GET /api/v1/admin/users": list_org_users,
    "GET /api/v1/admin/settings/global-upload": get_global_upload_setting,
    "POST /api/v1/admin/settings/global-upload": set_global_upload_setting,
    "PATCH /api/v1/admin/users/{target_user_id}/permissions": update_user_permission,
    "DELETE /api/v1/admin/users/{target_user_id}": delete_org_user,
    "GET /api/v1/admin/documents": list_org_documents,
    "DELETE /api/v1/admin/documents/{doc_id}": delete_org_document,
    "GET /api/v1/admin/audit-logs": get_audit_logs,
}


# ─────────────────────────────────────────────────────────────────────────────
# Main handler
# ─────────────────────────────────────────────────────────────────────────────
def handler(event, context):
    route_key = event.get("routeKey", "")
    logger.info(f"AdminLambda invoked: {route_key}")

    ctx = get_auth_context(event)
    if not ctx["user_id"]:
        return build_response(401, {"detail": "Unauthorized: Missing authentication context"})

    route = ROUTES.get(route_key)
    if not route:
        return build_response(404, {"detail": "Admin endpoint not found."})

    if not supabase:
        return build_response(500, {"detail": "Supabase client not initialized"})

    try:
        status, body = route(event, ctx)
        return build_response(status, body)
    except HttpError as e:
        return build_response(e.status, {"detail": e.detail})
    except Exception as e:
        logger.exception(f"Unhandled error on {route_key}")
        return build_response(500, {"detail": f"Internal error: {e}"})