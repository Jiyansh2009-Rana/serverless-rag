import os
import json
import redis
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ElastiCache Redis Endpoint (e.g., redis://my-cluster.xxxxx.cache.amazonaws.com:6379)
REDIS_URL = os.environ.get("REDIS_URL")

# Initialize connection outside handler for connection reuse
try:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=3.0)
except Exception as e:
    logger.error(f"Failed to initialize Redis: {e}")
    redis_client = None

def handler(event, context):
    if not redis_client:
        return {"statusCode": 500, "body": json.dumps({"error": "No Redis Connection"})}
        
    action = event.get("action")
    key = event.get("key") 
    
    try:
        # ── For Lambda 2 (Auth / OTP) ──
        if action == "set":
            value = event.get("value")
            ttl = event.get("ttl",300)
            if ttl:
                redis_client.setex(key, int(ttl), value)
            else:
                redis_client.set(key, value)
            return {"statusCode": 200, "body": json.dumps({"status": "success"})}
            
        elif action == "get":
            value = redis_client.get(key)
            return {"statusCode": 200, "body": json.dumps({"value": value})}
            
        elif action == "delete":
            redis_client.delete(key)
            return {"statusCode": 200, "body": json.dumps({"status": "success"})}
            
        # ── For Lambda 5 (Batch Vectors from SQS) ──
        elif action == "set_batch":
            items = event.get("items", []) # Expected: [{"key": "chunk:1", "value": "..."}, ...]
            default_ttl = event.get("ttl", 3600) # 1 hour TTL default
            
            # Using pipeline for fast batch inserts
            pipeline = redis_client.pipeline()
            for item in items:
                ttl = item.get("ttl", default_ttl)
                # Ensure value is stringified for Redis
                val = item["value"] if isinstance(item["value"], str) else json.dumps(item["value"])
                pipeline.setex(item["key"], int(ttl), val)
            pipeline.execute()
            
            return {"statusCode": 200, "body": json.dumps({"status": "success", "count": len(items)})}
            
        else:
            return {"statusCode": 400, "body": json.dumps({"error": f"Unknown action: {action}"})}
            
    except Exception as e:
        logger.error(f"Redis operation error: {e}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}