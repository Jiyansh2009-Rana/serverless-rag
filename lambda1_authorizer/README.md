# Lambda 1: LambdaAuthorizer

## Overview
This serverless authorizer intercepts and validates incoming requests at AWS API Gateway (HTTP API v2 or REST API v1).
It decodes and validates HS256 JWT tokens with **zero external dependencies** using Python standard library (`hmac`, `hashlib`, `base64`, `json`, `time`).

## Supported Authentication Channels
1. **HttpOnly Cookie**: Parses `access_token` from the `Cookie` header.
2. **Authorization Header**: Parses `Bearer <token>` from the `Authorization` header.
3. **Query Parameter**: Parses `?token=<jwt>` for WebSocket or direct download routes.

## Output Context
When authorized, the following context variables are injected into downstream Lambdas:
- `event.requestContext.authorizer.lambda.user_id`
- `event.requestContext.authorizer.lambda.org_id`
- `event.requestContext.authorizer.lambda.role`
- `event.requestContext.authorizer.lambda.email`

## Environment Variables
- `JWT_SECRET_KEY` (Required): The secret key used to sign and verify your application's JWT tokens.
- `JWT_ALGORITHM` (Optional): Default is `HS256`.

## AWS Lambda Handler
- Set Handler to: `lambda_function.handler`
- Runtime: `Python 3.11` or `Python 3.12`
