import os
import json
import base64
import hmac
import hashlib
import time

JWT_SECRET = os.environ.get("JWT_SECRET_KEY", "")
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")

def base64url_decode(input_str: str) -> bytes:
    rem = len(input_str) % 4
    if rem > 0:
        input_str += "=" * (4 - rem)
    return base64.urlsafe_b64decode(input_str)

def verify_and_decode_jwt(token: str, secret: str) -> dict:
    """
    Decodes and verifies an HS256 JWT using Python standard library (zero external dependencies).
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT token structure")

    header_b64, payload_b64, signature_b64 = parts
    
    header = json.loads(base64url_decode(header_b64).decode("utf-8"))
    alg = header.get("alg", "HS256")
    if alg != "HS256":
        raise ValueError(f"Unsupported algorithm: {alg}")

    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    expected_sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    actual_sig = base64url_decode(signature_b64)

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise ValueError("Signature mismatch")

    payload = json.loads(base64url_decode(payload_b64).decode("utf-8"))
    
    # Check expiration claim
    exp = payload.get("exp")
    if exp and time.time() > exp:
        raise ValueError("Token has expired")

    return payload

def parse_cookie_token(cookie_header: str) -> str | None:
    if not cookie_header:
        return None
    for cookie in cookie_header.split(";"):
        parts = cookie.strip().split("=", 1)
        if len(parts) == 2 and parts[0] == "access_token":
            return parts[1]
    return None

def extract_token(event: dict) -> str | None:
    headers = event.get("headers", {}) or {}
    for c in (event.get("cookies") or []):
        for part in c.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "access_token" and v:
                return v
    # Case-insensitive header check
    cookie_str = headers.get("cookie") or headers.get("Cookie") or ""
    token = parse_cookie_token(cookie_str)
    if token:
        return token

    auth_header = headers.get("authorization") or headers.get("Authorization") or ""
    if auth_header.startswith("Bearer "):
        return auth_header.split(" ", 1)[1].strip()

    # Query string token support if configured
    query_params = event.get("queryStringParameters", {}) or {}
    if "token" in query_params:
        return query_params["token"]

    return None

def generate_policy(principal_id: str, effect: str, resource: str, context: dict = None) -> dict:
    policy = {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource
                }
            ]
        }
    }
    if context:
        policy["context"] = context
    return policy

def handler(event, context):
    """
    Supports both API Gateway HTTP API v2 (simple payload) and REST API (IAM policy).
    """
    secret = os.environ.get("JWT_SECRET_KEY")
    if not secret:
        # Configuration error
        return {"isAuthorized": False}

    token = extract_token(event)
    method_arn = event.get("methodArn")

    if not token:
        if method_arn:
            return generate_policy("anonymous", "Deny", method_arn)
        return {"isAuthorized": False}

    try:
        payload = verify_and_decode_jwt(token, secret)
        if payload.get("type") != "access":   # refresh tokens must not authorize API calls
            raise ValueError("Wrong token type")
        user_id = str(payload.get("sub") or payload.get("user_id") or "")
        org_id = str(payload.get("org_id") or "")
        role = str(payload.get("role") or "User")
        email = str(payload.get("email") or "")

        if not user_id or not org_id:
            if method_arn:
                return generate_policy("anonymous", "Deny", method_arn)
            return {"isAuthorized": False}

        auth_context = {
            "user_id": user_id,
            "org_id": org_id,
            "role": role,
            "email": email
        }

        # If invoked by REST API authorizer expecting IAM policy
        if method_arn:
            return generate_policy(user_id, "Allow", method_arn, auth_context)

        # HTTP API Gateway v2 Simple format
        return {
            "isAuthorized": True,
            "context": auth_context
        }

    except Exception:
        if method_arn:
            return generate_policy("anonymous", "Deny", method_arn)
        return {"isAuthorized": False}


