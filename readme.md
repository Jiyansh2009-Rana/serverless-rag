# AskMyDocsAI: Serverless RAG Backend

Multi-tenant document Q&A on AWS. Users upload documents (PDF, DOCX, XLSX, PPTX, HTML, images), the pipeline
parses and embeds them, and users ask questions answered from their organisation's documents (hybrid retrieval,
reranking, LLM answer with sources).

- **Frontend:** `https://askmydocsai.dpdns.org` (React build served by nginx on EC2)
- **API:** same origin, `https://askmydocsai.dpdns.org/api/v1/...` (nginx proxies `/api/` to API Gateway)

## Architecture

```
Browser ──HTTPS──> nginx (EC2) ──/api/──> API Gateway (HTTP API, payload v2)
   │                                          │  Lambda authorizer (Lambda 1) on protected routes
   │                                          ├─> Lambda 2  AuthLambda
   │                                          ├─> Lambda 3  PresignedUploadLambda
   │                                          ├─> Lambda 6  QueryLambda
   │                                          ├─> Lambda 7  AdminLambda
   │                                          └─> Lambda 8  SuperAdminLambda
   │
   └──presigned POST──> S3 ──ObjectCreated──> Lambda 4 (parser, container) ──> SQS ──> Lambda 5 (vector ingest)

Shared: Supabase (users, orgs, document_registry, page_registry, document_chunks, audit_log, organization_settings),
        Redis via Redis-lambda proxy (OTP, local-session docs, chat history), S3 (original files)
```

## Lambdas, routes and triggers

| # | Folder | Trigger | Routes / source | Access |
|---|---|---|---|---|
| 1 | `lambda1_authorizer` | API Gateway Lambda authorizer (simple response) | n/a, attached to protected routes | Validates JWT, injects `user_id`, `org_id`, `role`, `email`; blocks suspended orgs (except Super Admin) |
| 2 | `Lambda_2_ AuthLambda` | HTTP API routes | `POST /api/v1/auth/signup`, `verify-signup`, `login`, `refresh`, `logout`, `forgot-password`, `reset-password` | Public |
| 2 | (same) | HTTP API route | `GET /api/v1/auth/me` | Authorizer attached to this route only |
| 3 | `lambda3__presigned_upload` | HTTP API route | `POST /api/v1/upload/presigned-url` | Authorizer |
| 4 | `lambda4_parser_code` | S3 `s3:ObjectCreated:*` (or `Post`) | n/a (background) | Internal. Container image from ECR |
| 5 | `lambda_5_VectorIngestLambda` | SQS event source mapping | n/a (background) | Internal |
| 6 | `Lambda_6_QueryLambda` | HTTP API route | `POST /api/v1/query` | Authorizer |
| 7 | `Lambda_7_AdminLambda` | HTTP API routes | see Admin routes | Authorizer + role check |
| 8 | `Lambda_8_SuperAdminLambda` | HTTP API routes | see Super Admin routes | Authorizer + Super Admin only |
| - | `redis_lambda_proxy` | Direct `lambda:Invoke` from other Lambdas | actions: set, get, delete, set_batch, get_batch, delete_batch, set_if_not_exists, scan_keys | Internal. Runs in the VPC with Redis |

### Admin routes (Lambda 7)

| Route key | Roles |
|---|---|
| `GET /api/v1/admin/users` | Admin, Super Admin |
| `GET /api/v1/admin/settings/global-upload` | User, Admin, Super Admin |
| `POST /api/v1/admin/settings/global-upload` | Admin, Super Admin |
| `PATCH /api/v1/admin/users/{target_user_id}/permissions` | Admin, Super Admin |
| `DELETE /api/v1/admin/users/{target_user_id}` | Admin, Super Admin |
| `GET /api/v1/admin/documents` | Admin, Super Admin |
| `DELETE /api/v1/admin/documents/{doc_id}` | Admin, Super Admin |
| `GET /api/v1/admin/audit-logs` | Admin (own org), Super Admin (all orgs) |

### Super Admin routes (Lambda 8)

| Route key | Notes |
|---|---|
| `GET /api/v1/super-admin/organizations` | All orgs with admin email, user count, status |
| `POST /api/v1/super-admin/organizations/{org_id}/toggle-status` | Body `{ "is_disabled": true, "reason": "..." }`; emails the org admin |
| `GET /api/v1/super-admin/users` | All users |
| `DELETE /api/v1/super-admin/users/{target_user_id}` | Any user |
| `GET /api/v1/super-admin/documents` | All documents |
| `DELETE /api/v1/super-admin/documents/{doc_id}` | Any document |

Roles: `User`, `Admin`, `Super Admin`. Roles are enforced inside the Lambdas; the authorizer only authenticates.

## Audit logs

`GET /api/v1/admin/audit-logs`: query params `limit` (default 50, max 200), `page`, `event_type`, `user_id`,
`from`, `to` (ISO-8601), `org_id` (Super Admin only).

- Admin: always scoped to its own org (taken from the token). Another `org_id` returns 403. `ip_address` is removed.
- Super Admin: all orgs. `ip_address` and `query_text` are removed.
- Response: `{ logs, page, limit, total, has_more }`, newest first.

## Auth flow (for the frontend)

1. `POST /api/v1/auth/login` returns `access_token` (JWT, 15 min) in the body and sets an HttpOnly `refresh_token`
   cookie (7 days, `Path=/api/v1/auth`). Keep the access token in memory.
2. Send `Authorization: Bearer <access_token>` on every protected call.
3. On a 401, call `POST /api/v1/auth/refresh` (the cookie is sent automatically) to get a new access token.
4. `GET /api/v1/auth/me` returns `{ user_id, username, email, role, org_id, allow_global_upload, created_at }`.
5. Use relative URLs (`/api/v1/...`), never the raw `execute-api` URL, or the cookie will not be sent.

## Upload flow

1. `POST /api/v1/upload/presigned-url` with
   `{ "filename": "...", "file_hash": "<sha256>", "upload_mode": "global" | "local", "confirmed": true }`.
2. The response has `doc_id`, `s3_key`, `is_duplicate`, `is_update` and `upload_data` (a presigned POST with
   `url` and `fields`). If `is_duplicate` is true, `upload_data` is null and nothing needs uploading.
3. The browser POSTs `multipart/form-data` straight to S3 with all `fields` first and the `file` field last.
   Limit 100 MB, link valid for 8 minutes.
4. S3 triggers Lambda 4 (parse, OCR, embed with Jina), which queues chunks to SQS, and Lambda 5 stores them
   (Supabase for `global`, Redis for `local`). The upload call itself does not wait for indexing.

## Query flow

`POST /api/v1/query` with
`{ "query": "...", "upload_mode": "global" | "local" | "both", "session_id": "optional", "language": "English", "system_prompt": "optional" }`
returns `{ session_id, answer, sources }`.

The answer is generated in full and returned as one JSON response (no streaming). API Gateway HTTP APIs cut
integrations at 30 seconds, so keep Lambda 6's timeout at or below that.

## Environment variables

| Lambda | Variables |
|---|---|
| 1 Authorizer | `JWT_SECRET_KEY`, `JWT_ALGORITHM` (HS256); optional for suspension check: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `ORG_STATUS_CACHE_TTL` (30) |
| 2 Auth | `JWT_SECRET_KEY` (required), `JWT_ALGORITHM`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_SENDER_EMAIL`, `APP_NAME` |
| 3 Presigned | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `S3_BUCKET_NAME`, `REDIS_PROXY_LAMBDA_NAME`, `LOCAL_SESSION_TTL` (3600), `AWS_REGION` |
| 4 Parser | `SQS_QUEUE_URL`, `JINA_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `REDIS_PROXY_LAMBDA_NAME`, `LOCAL_SESSION_TTL` |
| 5 Vector ingest | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `REDIS_PROXY_LAMBDA_NAME`, `LOCAL_SESSION_TTL` |
| 6 Query | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `JINA_API_KEY`, `GROQ_API_KEY`, `S3_BUCKET_NAME`, `REDIS_PROXY_LAMBDA_NAME` |
| 7 Admin | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `S3_BUCKET_NAME`, `REDIS_PROXY_LAMBDA_NAME` |
| 8 Super Admin | same as 7, plus `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM_NAME`, `SMTP_FROM_EMAIL`, `APP_NAME` |
| Redis proxy | `REDIS_URL`, `LOCAL_SESSION_TTL` |

## IAM and infrastructure notes

- Lambdas 3, 7 and 8 need S3 access (`s3:PutObject` for presigned POST on Lambda 3, `s3:DeleteObject` on 7 and 8).
- Lambdas 2, 3, 4, 5, 6, 7 and 8 need `lambda:InvokeFunction` on the Redis proxy Lambda.
- Lambda 4 needs `sqs:SendMessage` on the queue; Lambda 5 needs the SQS receive and delete permissions.
- Lambda 4 is a container image (Tesseract and Poppler); build and push it to ECR from `lambda4_parser_code/Dockerfile`.
- S3 bucket needs the event notification to Lambda 4 and a CORS rule allowing `https://askmydocsai.dpdns.org` (POST, GET).
- Only the Redis proxy runs inside the VPC. The other Lambdas reach Supabase and S3 over the internet.
- Runtime: Python 3.11 or 3.12 for zip Lambdas. Region defaults to `ap-south-1`.

## Deploying the frontend (EC2 + nginx)

1. Point an `A` record for `askmydocsai.dpdns.org` to the EC2 Elastic IP; open ports 80 and 443.
2. Install nginx and certbot, then get a certificate: `sudo certbot certonly --nginx -d askmydocsai.dpdns.org`.
3. Build the frontend and copy `dist/` to `/var/www/askmydocsai`.
4. Add `/etc/nginx/conf.d/askmydocsai.conf`: serve the SPA with `try_files $uri /index.html`, and proxy `/api/` to
   the API Gateway invoke URL, forwarding `X-Forwarded-For $remote_addr`.
5. `sudo nginx -t && sudo systemctl reload nginx`.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Login works but `/refresh` returns 401 | The frontend is calling the raw `execute-api` URL instead of `/api/...` on the same domain |
| Upload to S3 fails with a CORS error | Bucket CORS missing the `https://askmydocsai.dpdns.org` origin |
| Every audit log shows the same IP | Lambda 6 not using `X-Forwarded-For`, or nginx not setting it |
| 504 from `/api/v1/query` | The query exceeded 30 seconds; check Jina and Groq latency |
| 403 on all routes for one org | The org is suspended (`organization_settings.is_disabled`) |