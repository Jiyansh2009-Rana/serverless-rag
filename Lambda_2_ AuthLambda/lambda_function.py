import os
import json
import time
import logging
from datetime import datetime, timedelta, timezone
import jwt
import bcrypt
from supabase import create_client, Client

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Environment Configurations ──
JWT_SECRET = os.environ.get("JWT_SECRET_KEY", "your-fallback-secret-key")
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

ACCESS_TOKEN_EXP_MINUTES = 15
REFRESH_TOKEN_EXP_DAYS = 7

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None


def create_jwt(payload: dict, expires_in_seconds: int) -> str:
    """Helper to generate signed JWT tokens."""
    to_encode = payload.copy()
    now = datetime.now(timezone.utc)
    to_encode.update({
        "iat": now,
        "exp": now + timedelta(seconds=expires_in_seconds)
    })
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict | None:
    """Helper to verify and decode JWT tokens."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except Exception as e:
        logger.warning(f"JWT decode error: {e}")
        return None


def parse_cookies(headers: dict) -> dict:
    """Extracts raw cookies from request headers."""
    cookie_str = headers.get("cookie") or headers.get("Cookie") or ""
    cookies = {}
    if cookie_str:
        for item in cookie_str.split(";"):
            parts = item.strip().split("=", 1)
            if len(parts) == 2:
                cookies[parts[0]] = parts[1]
    return cookies


def build_response(status_code: int, body: dict, cookies: list = None) -> dict:
    """Builds standard HTTP API Gateway v2 JSON response."""
    res = {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Credentials": "true"
        },
        "body": json.dumps(body)
    }
    if cookies:
        res["cookies"] = cookies
    return res


def handle_login(body: dict) -> dict:
    """Authenticates credentials and sets HttpOnly Refresh Token cookie."""
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")

    if not email or not password:
        return build_response(400, {"detail": "Email and password are required."})

    if not supabase:
        return build_response(500, {"detail": "Database connection not initialized."})

    # Fetch user from Supabase
    res = supabase.table("users").select("*").eq("email", email).execute()
    if not res.data:
        return build_response(401, {"detail": "Invalid email ."})

    user = res.data[0]
    stored_hash = user.get("password_hash", "")

    # Verify password
    if not bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8")):
        return build_response(401, {"detail": "Invalid  password."})

    user_id = user["id"]
    org_id = user["org_id"]
    role = user.get("role", "User")

    # Generate Access Token
    access_payload = {
        "sub": user_id,
        "org_id": org_id,
        "role": role,
        "email": email,
        "type": "access"
    }
    access_token = create_jwt(access_payload, ACCESS_TOKEN_EXP_MINUTES * 60)

    # Generate Refresh Token
    refresh_payload = {
        "sub": user_id,
        "org_id": org_id,
        "type": "refresh"
    }
    refresh_token = create_jwt(refresh_payload, REFRESH_TOKEN_EXP_DAYS * 86400)

    # Prepare HttpOnly Cookie for Refresh Token
    cookie_str = (
        f"refresh_token={refresh_token}; "
        f"HttpOnly; Secure; SameSite=Strict; "
        f"Path=/api/v1/auth; Max-Age={REFRESH_TOKEN_EXP_DAYS * 86400}"
    )

    return build_response(
        200,
        {
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": ACCESS_TOKEN_EXP_MINUTES * 60,
            "user": {
                "id": user_id,
                "email": email,
                "org_id": org_id,
                "role": role
            }
        },
        cookies=[cookie_str]
    )


def handle_refresh(event: dict) -> dict:
    """Reads refresh token from HttpOnly cookie and issues a fresh access token."""
    headers = event.get("headers", {}) or {}
    cookies = parse_cookies(headers)
    refresh_token = cookies.get("refresh_token")

    if not refresh_token:
        return build_response(401, {"detail": "Refresh token missing."})

    payload = decode_jwt(refresh_token)
    if not payload or payload.get("type") != "refresh":
        return build_response(401, {"detail": "Invalid or expired refresh token."})

    user_id = payload.get("sub")
    org_id = payload.get("org_id")

    if not supabase:
        return build_response(500, {"detail": "Database connection error."})

    # Validate active status of user
    res = supabase.table("users").select("id, email, org_id, role, is_active").eq("id", user_id).execute()
    if not res.data or not res.data[0].get("is_active", True):
        return build_response(401, {"detail": "User account inactive or missing."})

    user = res.data[0]

    # Issue new short-lived access token
    new_access_payload = {
        "sub": user["id"],
        "org_id": user["org_id"],
        "role": user.get("role", "User"),
        "email": user.get("email", ""),
        "type": "access"
    }
    new_access_token = create_jwt(new_access_payload, ACCESS_TOKEN_EXP_MINUTES * 60)

    return build_response(
        200,
        {
            "access_token": new_access_token,
            "token_type": "bearer",
            "expires_in": ACCESS_TOKEN_EXP_MINUTES * 60
        }
    )


def handle_logout() -> dict:
    """Clears the HttpOnly Refresh Token cookie."""
    clear_cookie_str = (
        "refresh_token=; HttpOnly; Secure; SameSite=Strict; "
        "Path=/api/v1/auth; Max-Age=0"
    )
    return build_response(200, {"detail": "Successfully logged out."}, cookies=[clear_cookie_str])


def handler(event, context):
    """
    Lambda 2: AuthLambda
    Routes POST requests for /api/v1/auth/login, /api/v1/auth/refresh, /api/v1/auth/logout
    """
    raw_path = event.get("rawPath", "") or event.get("path", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "POST")

    try:
        body = json.loads(event.get("body", "{}") or "{}")
    except Exception:
        body = {}

    if "/login" in raw_path:
        return handle_login(body)
    elif "/refresh" in raw_path:
        return handle_refresh(event)
    elif "/logout" in raw_path:
        return handle_logout()

    return build_response(404, {"detail": "Auth endpoint not found."})