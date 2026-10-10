import os
import json
import redis
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REDIS_URL = os.environ.get("REDIS_URL")
LOCAL_SESSION_TTL = int(os.environ.get("LOCAL_SESSION_TTL", "3600"))

try:
    redis_client = redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_timeout=3.0,
        socket_connect_timeout=3.0
    ) if REDIS_URL else None
except Exception as e:
    logger.error(f"Failed to initialize Redis client: {e}")
    redis_client = None


def handler(event, context):
    if not redis_client:
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "No Redis Connection available"})
        }

    action = event.get("action")
    key = event.get("key")

    try:
        # Auth Lambda (Lambda 2) and general single-key setters
        if action == "set":
            value = event.get("value")
            ttl = event.get("ttl", 300)
            val_str = value if isinstance(value, str) else json.dumps(value)

            if ttl:
                redis_client.setex(key, int(ttl), val_str)
            else:
                redis_client.set(key, val_str)

            return {
                "statusCode": 200,
                "body": json.dumps({"status": "success"})
            }

        # Auth Lambda (Lambda 2) and general single-key getters
        elif action == "get":
            value = redis_client.get(key)
            return {
                "statusCode": 200,
                "body": json.dumps({"value": value})
            }

        # Auth Lambda (Lambda 2) and general single-key deleters
        elif action == "delete":
            redis_client.delete(key)
            return {
                "statusCode": 200,
                "body": json.dumps({"status": "success"})
            }

        # Pipeline-backed batch setter
        elif action == "set_batch":
            items = event.get("items", [])
            default_ttl = event.get("ttl", LOCAL_SESSION_TTL)
            pipeline = redis_client.pipeline()

            for item in items:
                ttl = item.get("ttl", default_ttl)
                val = item["value"] if isinstance(item["value"], str) else json.dumps(item["value"])
                pipeline.setex(item["key"], int(ttl), val)

            pipeline.execute()
            return {
                "statusCode": 200,
                "body": json.dumps({"status": "success", "count": len(items)})
            }

        # Pipeline-backed batch getter
        elif action == "get_batch":
            keys = event.get("keys", [])
            if not keys:
                return {"statusCode": 200, "body": json.dumps({"values": {}})}

            pipeline = redis_client.pipeline()
            for k in keys:
                pipeline.get(k)

            results = pipeline.execute()
            values_map = {k: v for k, v in zip(keys, results)}

            return {
                "statusCode": 200,
                "body": json.dumps({"values": values_map})
            }

        # Pipeline-backed batch deleter
        elif action == "delete_batch":
            keys = event.get("keys", [])
            if keys:
                pipeline = redis_client.pipeline()
                for k in keys:
                    pipeline.delete(k)
                pipeline.execute()

            return {
                "statusCode": 200,
                "body": json.dumps({"status": "success", "count": len(keys)})
            }

        # Atomic conditional setter
        elif action == "set_if_not_exists":
            value = event.get("value")
            ttl = event.get("ttl", LOCAL_SESSION_TTL)
            val_str = value if isinstance(value, str) else json.dumps(value)

            success = redis_client.set(key, val_str, nx=True, ex=int(ttl))
            return {
                "statusCode": 200,
                "body": json.dumps({"success": bool(success)})
            }
                # Key scan by pattern (used by Lambda 6 for local search)
        elif action == "scan_keys":
            pattern = event.get("pattern") or key
            limit = int(event.get("limit", 500))
            found = []
            for k in redis_client.scan_iter(match=pattern, count=200):
                found.append(k)
                if len(found) >= limit:
                    break
            return {
                "statusCode": 200,
                "body": json.dumps({"keys": found})
            }
        else:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": f"Unknown action: {action}"})
            }

    except Exception as e:
        logger.error(f"Redis operation error: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)})
        }