import os
import json
import logging
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

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

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "Global RAG Platform")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", SMTP_USER)

ROLE_SUPER_ADMIN = "Super Admin"
PAGE_SIZE = 1000 

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
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    ctx = authorizer.get("lambda") or authorizer.get("context") or {}
    return {
        "user_id": ctx.get("user_id"),
        "org_id": ctx.get("org_id"),
        "role": ctx.get("role", "User"),
        "email": ctx.get("email", ""),
    }


def parse_json_body(event: dict) -> dict:
    try:
        body = json.loads(event.get("body") or "{}")
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


def fetch_all(table: str, columns: str = "*", order_by: Optional[str] = None, desc: bool = False) -> list:
    """Reads every row of a table, paging past the 1000-row PostgREST limit."""
    rows, start = [], 0
    while True:
        q = supabase.table(table).select(columns)
        if order_by:
            q = q.order(order_by, desc=desc)
        batch = q.range(start, start + PAGE_SIZE - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            return rows
        start += PAGE_SIZE


def invoke_redis_delete(key: str) -> None:
    try:
        lambda_client.invoke(
            FunctionName=REDIS_PROXY_LAMBDA_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps({"action": "delete", "key": key}),
        )
    except Exception as e:
        logger.warning(f"Redis cleanup failed for {key}: {e}")


def send_organization_disabled_email(admin_email: str, org_id: str, reason: str) -> bool:
    """Synchronous SMTP send. Never raises: a mail failure must not fail the status change."""
    if not SMTP_USER or not SMTP_PASSWORD:
        logger.warning("SMTP credentials not configured. Skipping email dispatch.")
        return False

    subject = f"Notice: Access Suspended for Organisation '{org_id}'"
    html_body = f"""
    <html><body style="font-family: Arial, sans-serif; color: #27272a;">
      <h2>Organisation Access Suspended</h2>
      <p>Dear Administrator (<strong>{admin_email}</strong>),</p>
      <p>Access to the AskmydocsAI RAG platform for your organisation <strong>{org_id}</strong>
         has been <strong>DISABLED</strong> by the Super Administrator.</p>
      <p><strong>Reason / Notes:</strong></p>
      <blockquote>{reason}</blockquote>
      <p><strong>Impact of this action:</strong></p>
      <ul>
        <li>All members of <strong>{org_id}</strong> are temporarily restricted from using the platform.</li>
        <li>Document querying, indexing, and uploads are paused.</li>
        <li>Your data and indexed vector embeddings remain secure and preserved.</li>
      </ul>
      <p>If you believe this is in error or wish to reactivate your organisation,
         please contact platform support or your Super Admin.</p>
      <hr><p style="font-size: 12px; color: #71717a;">AskmyDocsAI RAG Platform</p>
    </body></html>
    """
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
        msg["To"] = admin_email
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=5) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM_EMAIL, [admin_email], msg.as_string())
        logger.info(f"Suspension notice sent to {admin_email} for org {org_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to send suspension email: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Route handlers (all Super Admin only; enforced in handler())
# ─────────────────────────────────────────────────────────────────────────────
def get_all_organizations(event, ctx):
    users = fetch_all("users", "id, email, role, org_id")
    settings_map = {s["org_id"]: s for s in fetch_all("organization_settings", "*")}

    orgs = {}
    for u in users:
        org = u.get("org_id")
        if not org:
            continue
        if org not in orgs:
            setting = settings_map.get(org, {})
            orgs[org] = {
                "org_id": org,
                "admin_email": None,
                "total_users": 0,
                "is_disabled": setting.get("is_disabled", False),
                "disabled_at": setting.get("disabled_at"),
                "disabled_reason": setting.get("disabled_reason"),
            }
        orgs[org]["total_users"] += 1
        if u.get("role") in ("Admin", "Super Admin") and not orgs[org]["admin_email"]:
            orgs[org]["admin_email"] = u.get("email")

    return 200, {"organizations": list(orgs.values())}


def toggle_org_status(event, ctx):
    org_id = path_param(event, "org_id")
    body = parse_json_body(event)

    is_disabled = body.get("is_disabled")
    if not isinstance(is_disabled, bool):
        raise HttpError(400, "'is_disabled' must be a boolean.")
    reason = body.get("reason") or "Administrative policy decision"

    admin_res = (
        supabase.table("users").select("email").eq("org_id", org_id).eq("role", "Admin").limit(1).execute()
    )
    admin_email = admin_res.data[0]["email"] if admin_res.data else None

    supabase.table("organization_settings").upsert({
        "org_id": org_id,
        "is_disabled": is_disabled,
        "disabled_at": now_iso() if is_disabled else None,
        "disabled_reason": reason if is_disabled else None,
        "updated_at": now_iso(),
    }).execute()

    supabase.table("audit_log").insert({
        "event_type": "organization_disabled" if is_disabled else "organization_enabled",
        "user_id": ctx["user_id"],
        "org_id": org_id,
        "role": ctx["role"],
        "note": f"Org Admin: {admin_email}. Reason: {reason}",
        "timestamp": now_iso(),
    }).execute()

    email_sent = False
    if is_disabled and admin_email:
        email_sent = send_organization_disabled_email(admin_email, org_id, reason)

    return 200, {
        "message": f"Organisation '{org_id}' access has been {'DISABLED' if is_disabled else 'ACTIVATED'}.",
        "org_id": org_id,
        "admin_email": admin_email,
        "is_disabled": is_disabled,
        "email_sent": email_sent,
    }


def get_all_users(event, ctx):
    return 200, {"users": fetch_all("users", "id, email, role, org_id, created_at")}


def delete_any_user(event, ctx):
    target_user_id = path_param(event, "target_user_id")
    if ctx["user_id"] == target_user_id:
        raise HttpError(400, "You cannot delete your own Super Admin account.")

    check = supabase.table("users").select("id").eq("id", target_user_id).execute()
    if not check.data:
        raise HttpError(404, "User not found.")

    supabase.table("users").delete().eq("id", target_user_id).execute()
    invoke_redis_delete(f"user:{target_user_id}:meta")
    return 200, {"message": f"User {target_user_id} removed successfully from the platform."}


def get_all_documents(event, ctx):
    return 200, {"documents": fetch_all("document_registry", "*", order_by="uploaded_at", desc=True)}


def delete_any_document(event, ctx):
    doc_id = path_param(event, "doc_id")

    doc_res = supabase.table("document_registry").select("id, org_id, file_name").eq("id", doc_id).execute()
    if not doc_res.data:
        raise HttpError(404, "Document not found on the platform.")

    doc = doc_res.data[0]
    org_id = doc["org_id"]
    file_name = doc.get("file_name", "")

    try:
        supabase.table("document_chunks").delete().eq("document_id", doc_id).execute()
    except Exception as e:
        logger.error(f"Failed to delete document chunks: {e}")
        raise HttpError(500, f"Failed to delete document chunks globally from DB: {e}")

    try:
        supabase.table("page_registry").delete().eq("document_id", doc_id).execute()
        supabase.table("document_registry").delete().eq("id", doc_id).execute()
    except Exception as e:
        logger.error(f"Failed to clear document registry: {e}")
        raise HttpError(500, f"Failed to clear document registry globally: {e}")

    if file_name:
        try:
            s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=f"{org_id}/{doc_id}/{file_name}")
        except ClientError as e:
            logger.warning(f"Could not remove file from S3: {e}")

    try:
        supabase.table("audit_log").insert({
            "event_type": "super_admin_global_document_deleted",
            "user_id": ctx["user_id"],
            "org_id": org_id,
            "doc_id": doc_id,
            "file_name": file_name,
            "timestamp": now_iso(),
        }).execute()
    except Exception as e:
        logger.warning(f"Super admin audit log failed: {e}")

    return 200, {"message": f"Document {doc_id} and all related chunks successfully deleted globally."}


ROUTES = {
    "GET /api/v1/super-admin/organizations": get_all_organizations,
    "POST /api/v1/super-admin/organizations/{org_id}/toggle-status": toggle_org_status,
    "GET /api/v1/super-admin/users": get_all_users,
    "DELETE /api/v1/super-admin/users/{target_user_id}": delete_any_user,
    "GET /api/v1/super-admin/documents": get_all_documents,
    "DELETE /api/v1/super-admin/documents/{doc_id}": delete_any_document,
}


# ─────────────────────────────────────────────────────────────────────────────
# Main handler
# ─────────────────────────────────────────────────────────────────────────────
def handler(event, context):
    route_key = event.get("routeKey", "")
    logger.info(f"SuperAdminLambda invoked: {route_key}")

    ctx = get_auth_context(event)
    if not ctx["user_id"]:
        return build_response(401, {"detail": "Unauthorized: Missing authentication context"})

    # Every route in this Lambda is Super Admin only.
    if ctx["role"] != ROLE_SUPER_ADMIN:
        return build_response(403, {"detail": f"Access denied. Required role: ['{ROLE_SUPER_ADMIN}']"})

    route = ROUTES.get(route_key)
    if not route:
        return build_response(404, {"detail": "Super admin endpoint not found."})

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