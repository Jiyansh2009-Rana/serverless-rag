# Lambda 4: DocumentParserLambda

## Overview
Asynchronously processes files uploaded to Amazon S3. Performs OCR and specialized text extraction across PDF, DOCX, XLSX, PPTX, HTML, and Images, generates 1024-dimension embeddings via Jina AI, and dispatches batches to Amazon SQS for safe database insertion.

## Runtime Architecture
Deployed as a **Docker Container Image on AWS Lambda** via Amazon ECR, because system binaries (`tesseract`, `poppler-utils`) and scientific libraries exceed the 250MB zip file threshold.

## Environment Variables
- `SQS_QUEUE_URL` (Required): Target Amazon SQS Queue URL for chunks.
- `JINA_API_KEY` (Required): Jina AI API key for `jina-clip-v2` embeddings.
- `SUPABASE_URL` (Required): Supabase project endpoint.
- `SUPABASE_SERVICE_ROLE_KEY` (Required): Supabase service role key.

## S3 Trigger Setup
- **Source**: Amazon S3 Bucket
- **Event**: `s3:ObjectCreated:*` or `s3:ObjectCreated:Post`
