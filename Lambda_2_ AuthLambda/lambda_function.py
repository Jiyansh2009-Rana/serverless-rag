import os
import json
import time
import uuid
import logging
import random
import string
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
import jwt
import bcrypt
from supabase import create_client, Client
import boto3
import secrets
import hmac

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Environment Configurations ──
lambda_client = boto3.client('lambda')
JWT_SECRET = os.environ.get("JWT_SECRET_KEY", "your-fallback-secret-key")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET_KEY environment variable is required")
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# Redis Configuration


# SMTP Configuration
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER", "your-email@gmail.com")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "your-app-password")
SMTP_SENDER_EMAIL = os.environ.get("SMTP_SENDER_EMAIL", SMTP_USER)

ACCESS_TOKEN_EXP_MINUTES = 15
REFRESH_TOKEN_EXP_DAYS = 7
OTP_TTL_SECONDS = 300  # 5 minutes

# Initialize Clients
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None



def invoke_redis(payload: dict) -> dict:
    """Helper to communicate with the Redis Proxy Lambda"""
    response = lambda_client.invoke(
        FunctionName="Redis-lambda", 
        InvocationType="RequestResponse",     
        Payload=json.dumps(payload)
    )
    response_payload = json.loads(response['Payload'].read())
    return json.loads(response_payload.get('body', '{}'))

# ── Utility Functions ──
def generate_otp(length=6) -> str:
    """Generates a numeric OTP."""
    
    return "".join(secrets.choice(string.digits) for _ in range(length))

def send_otp_email(to_email: str, otp: str, purpose: str):
    """Sends OTP via standard SMTP."""
    subjects = {
        "signup": "Verify your email to complete signup",
        "reset": "Password reset verification code"
    }
    
    subject = subjects.get(purpose, "Your OTP Code")
    text_body = f"Hi from Rag-Agent Platform. Your verification code is: {otp}\nThis code is valid for 5 minutes. Please do not share it with anyone."
    # 2. Modern HTML UI
    html_body = f"""
    <html>
      <body style="background-color: #f4f4f5; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; padding: 40px 20px; margin: 0;">
        <div style="max-width: 500px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; padding: 40px 30px; text-align: center; box-shadow: 0 4px 15px rgba(0,0,0,0.05);">
          <h2 style="color: #0f766e; margin-top: 0; font-size: 26px;">Rag-Agent Platform</h2>
          <hr style="border: none; border-top: 1px solid #e4e4e7; margin: 20px 0;">
          
          <p style="font-size: 16px; color: #3f3f46; margin-top: 20px;">Hello,</p>
          <p style="font-size: 16px; color: #3f3f46; line-height: 1.5;">Here is your verification code. This code is valid for <strong>5 minutes</strong>.</p>
          
          <div style="margin: 35px auto; padding: 15px; background-color: #f0fdfa; border: 1px solid #5eead4; border-radius: 8px; font-size: 36px; font-weight: bold; letter-spacing: 8px; color: #0f766e; max-width: 250px;">
            {otp}
          </div>
          
          <p style="font-size: 13px; color: #71717a; margin-top: 30px; margin-bottom: 0;">For your security, please do not share this code with anyone.</p>
        </div>
      </body>
    </html>
    """

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = SMTP_SENDER_EMAIL
    msg['To'] = to_email
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype='html')

    try:
        # Connect to the SMTP server and send the email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=5) as server:
            server.starttls()  # Secure the connection
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
            
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        raise e

# (Existing JWT & Cookie Helpers...)
def create_jwt(payload: dict, expires_in_seconds: int) -> str:
    to_encode = payload.copy()
    now = datetime.now(timezone.utc)
    to_encode.update({"iat": now, "exp": now + timedelta(seconds=expires_in_seconds)})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_jwt(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except Exception as e:
        logger.warning(f"JWT decode error: {e}")
        return None

def parse_cookies(headers: dict) -> dict:
    cookie_str = headers.get("cookie") or headers.get("Cookie") or ""
    cookies = {}
    if cookie_str:
        for item in cookie_str.split(";"):
            parts = item.strip().split("=", 1)
            if len(parts) == 2:
                cookies[parts[0]] = parts[1]
    return cookies

def build_response(status_code: int, body: dict, cookies: list = None) -> dict:
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


# ── Signup & Email Verification ──
def handle_signup(body: dict) -> dict:
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    username = body.get("username", "").strip()
    org_id = body.get("org_id", "default_org") 
    
    if not email or not password or not username:
        return build_response(400, {"detail": "Email, password, and username are required."})

    existing = supabase.table("users").select("id, email, username").or_(f"email.eq.{email},username.eq.{username}").execute()
    if existing.data:
        for u in existing.data:
            if u.get("email") == email:
                return build_response(400, {"detail": "Email is already registered."})

    hashed_pw = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    otp = generate_otp()

    redis_key = f"signup:{email}"
    pending_user_data = {
        "email": email,
        "password_hash": hashed_pw,
        "org_id": org_id,
        "username": username,
        "otp": otp
    }
    invoke_redis({
    "action": "set",
    "key": redis_key,
    "value": json.dumps(pending_user_data),
    "ttl": OTP_TTL_SECONDS
    })

    try:
        send_otp_email(email, otp, "signup")
    except Exception:
        return build_response(500, {"detail": "Failed to send verification email."})

    return build_response(200, {"detail": "OTP sent to email. Please verify within 5 minutes."})

def handle_verify_signup(body: dict) -> dict:
    email = body.get("email", "").strip().lower()
    provided_otp = body.get("otp", "").strip()

    if not email or not provided_otp:
        return build_response(400, {"detail": "Email and OTP are required."})

    redis_key = f"signup:{email}"
    stored_data_str = invoke_redis({"action": "get", "key": redis_key}).get("value")
    

    if not stored_data_str:
        return build_response(400, {"detail": "OTP expired or invalid email."})

    stored_data = json.loads(stored_data_str)
    
    if stored_data["otp"] != provided_otp:
        return build_response(400, {"detail": "Incorrect OTP."})

    user_id = str(uuid.uuid4())
    new_user = {
        "id": user_id,
        "email": stored_data["email"],
        "password_hash": stored_data["password_hash"],
        "org_id": stored_data["org_id"],
        "username": stored_data.get("username"),
        "role": "User",
        "is_active": True
    }
    
    supabase.table("users").insert(new_user).execute()
    invoke_redis({"action": "delete", "key": redis_key})

    return build_response(201, {"detail": "Signup successful. You can now log in."})


# ── Forgot Password Flow ──
def handle_forgot_password(body: dict) -> dict:
    email = body.get("email", "").strip().lower()
    
    if not email:
        return build_response(400, {"detail": "Email is required."})

    user = supabase.table("users").select("id").eq("email", email).execute()
    if not user.data:
        return build_response(200, {"detail": "If an account exists, an OTP has been sent."})

    otp = generate_otp()
    invoke_redis({
    "action": "set",
    "key": f"reset:{email}",
    "value": json.dumps(otp),
    "ttl": OTP_TTL_SECONDS
    })

    try:
        send_otp_email(email, otp, "reset")
    except Exception:
        return build_response(500, {"detail": "Failed to send reset email."})

    return build_response(200, {"detail": "If an account exists, an OTP has been sent."})

def handle_reset_password(body: dict) -> dict:
    email = body.get("email", "").strip().lower()
    provided_otp = body.get("otp", "").strip()
    new_password = body.get("new_password", "")

    if not all([email, provided_otp, new_password]):
        return build_response(400, {"detail": "Email, OTP, and new password are required."})

    redis_key = f"reset:{email}"
    stored_otp = invoke_redis({
    "action": "get",
    "key": redis_key}).get("value")

    if not stored_otp or stored_otp != provided_otp:
        return build_response(400, {"detail": "Invalid or expired OTP."})

    hashed_pw = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    
    supabase.table("users").update({"password_hash": hashed_pw}).eq("email", email).execute()
    invoke_redis({"action": "delete", "key": redis_key})

    return build_response(200, {"detail": "Password successfully reset. You can now log in."})


# ── EXISTING: Login, Refresh, Logout ──
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
        return build_response(401, {"detail": "Invalid email or if you new user please signup first"})

    user = res.data[0]
    if not user.get("is_active", True):
        return build_response(403, {"detail": "Account is inactive."})
    stored_hash = user.get("password_hash", "")

    # Verify password
    if not bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8")):
        return build_response(401, {"detail": "Invalid password."})

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


# ── Main Handler Routing ──
def handler(event, context):
    raw_path = event.get("rawPath", "") or event.get("path", "")
    
    try:
        body = json.loads(event.get("body", "{}") or "{}")
    except Exception:
        body = {}

    if "/signup" in raw_path:
        return handle_signup(body)
    elif "/verify-signup" in raw_path:
        return handle_verify_signup(body)
    elif "/forgot-password" in raw_path:
        return handle_forgot_password(body)
    elif "/reset-password" in raw_path:
        return handle_reset_password(body)
    elif "/login" in raw_path:
        return handle_login(body)
    elif "/refresh" in raw_path:
        return handle_refresh(event)
    elif "/logout" in raw_path:
        return handle_logout()

    return build_response(404, {"detail": "Auth endpoint not found."})