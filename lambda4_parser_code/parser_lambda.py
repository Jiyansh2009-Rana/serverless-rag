import os
import io
import json
import uuid
import hashlib
import logging
import urllib.parse
from datetime import datetime, timezone
import boto3
import requests
import pandas as pd
from bs4 import BeautifulSoup
from docx import Document as DocxDocument
from pptx import Presentation as PptxPresentation
from pypdf import PdfReader
from pdf2image import convert_from_bytes
import pytesseract
from PIL import Image
from langchain_text_splitters import RecursiveCharacterTextSplitter
from supabase import create_client, Client

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── AWS & External Clients ──
s3_client = boto3.client("s3")
sqs_client = boto3.client("sqs")

SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL")
JINA_API_KEY = os.environ.get("JINA_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# ── 1. Document Extraction by Type ──

def detect_document_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    mapping = {
        ".pdf": "pdf",
        ".docx": "docx",
        ".doc": "docx",
        ".xlsx": "xlsx",
        ".xls": "xlsx",
        ".pptx": "pptx",
        ".ppt": "pptx",
        ".html": "html",
        ".htm": "html",
        ".txt": "txt",
        ".md": "txt",
        ".png": "image",
        ".jpg": "image",
        ".jpeg": "image",
        ".tiff": "image",
        ".bmp": "image"
    }
    return mapping.get(ext, "unknown")

def extract_pdf(file_bytes: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(file_bytes))
    pages_text = []
    for page_idx, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        # Fallback to OCR if embedded text is sparse
        if len(text.strip()) < 50:
            try:
                images = convert_from_bytes(file_bytes, dpi=200, first_page=page_idx + 1, last_page=page_idx + 1)
                if images:
                    ocr_text = pytesseract.image_to_string(images[0], lang="eng").strip()
                    if ocr_text:
                        text = ocr_text
            except Exception as ocr_err:
                logger.warning(f"OCR fallback failed on page {page_idx + 1}: {ocr_err}")
        pages_text.append(text)
    return pages_text

def extract_docx(file_bytes: bytes) -> list[str]:
    doc = DocxDocument(io.BytesIO(file_bytes))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    table_texts = []
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join([cell.text.strip() for cell in row.cells if cell.text.strip()])
            if row_text:
                table_texts.append(row_text)
    combined = "\n".join(paragraphs + table_texts)
    return [combined] if combined.strip() else []

def extract_xlsx(file_bytes: bytes) -> list[str]:
    excel_file = pd.ExcelFile(io.BytesIO(file_bytes))
    sheets_text = []
    for sheet_name in excel_file.sheet_names:
        df = pd.read_excel(excel_file, sheet_name=sheet_name)
        df = df.dropna(how="all")
        if df.empty:
            continue
        text_repr = df.to_string(index=False)
        sheets_text.append(f"Sheet: {sheet_name}\n{text_repr}")
    return sheets_text

def extract_pptx(file_bytes: bytes) -> list[str]:
    prs = PptxPresentation(io.BytesIO(file_bytes))
    slides_text = []
    for idx, slide in enumerate(prs.slides):
        slide_parts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    if paragraph.text.strip():
                        slide_parts.append(paragraph.text.strip())
        slides_text.append(f"Slide {idx + 1}:\n" + "\n".join(slide_parts))
    return slides_text

def extract_html(file_bytes: bytes) -> list[str]:
    soup = BeautifulSoup(file_bytes, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(separator="\n").strip()
    return [text] if text else []

def extract_image(file_bytes: bytes) -> list[str]:
    img = Image.open(io.BytesIO(file_bytes))
    text = pytesseract.image_to_string(img, lang="eng").strip()
    return [text] if text else []

def extract_pages(file_bytes: bytes, doc_type: str) -> list[str]:
    if doc_type == "pdf":
        return extract_pdf(file_bytes)
    elif doc_type == "docx":
        return extract_docx(file_bytes)
    elif doc_type == "xlsx":
        return extract_xlsx(file_bytes)
    elif doc_type == "pptx":
        return extract_pptx(file_bytes)
    elif doc_type == "html":
        return extract_html(file_bytes)
    elif doc_type == "image":
        return extract_image(file_bytes)
    else:
        # Default txt / markdown
        return [file_bytes.decode("utf-8", errors="ignore")]

# ── 2. Chunking & Embeddings ──

def chunk_content(content: str, doc_type: str) -> list[str]:
    chunk_size = 800 if doc_type in ["xlsx", "table"] else 1000
    chunk_overlap = 150
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    return splitter.split_text(content)

def get_jina_embeddings(texts: list[str]) -> list[list[float]]:
    if not JINA_API_KEY:
        raise ValueError("JINA_API_KEY environment variable is not configured")
    
    url = "https://api.jina.ai/v1/embeddings"
    headers = {
        "Authorization": f"Bearer {JINA_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "jina-clip-v2",
        "normalized": True,
        "dimension": 1024,
        "input": [{"text": t} for t in texts]
    }
    response = requests.post(url, json=payload, headers=headers, timeout=45)
    response.raise_for_status()
    data = response.json().get("data", [])
    return [item["embedding"] for item in data]

def compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

# ── 3. Main Lambda Handler ──

def handler(event, context):
    """
    Lambda 4: DocumentParserLambda
    Triggered by S3 ObjectCreated event.
    """
    logger.info(f"Received event: {json.dumps(event)}")

    if not SQS_QUEUE_URL:
        raise ValueError("SQS_QUEUE_URL is required")

    records = event.get("Records", [])
    for record in records:
        bucket = record["s3"]["bucket"]["name"]
        raw_key = record["s3"]["object"]["key"]
        s3_key = urllib.parse.unquote_plus(raw_key)

        logger.info(f"Processing object s3://{bucket}/{s3_key}")

        # 1. Fetch metadata stored on S3 object
        head = s3_client.head_object(Bucket=bucket, Key=s3_key)
        metadata = head.get("Metadata", {})
        org_id = metadata.get("org-id")
        doc_id = metadata.get("doc-id")
        user_id = metadata.get("user-id")
        upload_mode = metadata.get("upload-mode", "global")
        file_hash_val = metadata.get("file-hash", "")

        # Fallback extraction from key: {org_id}/{doc_id}/{filename}
        key_parts = s3_key.split("/")
        if len(key_parts) >= 3:
            org_id = org_id or key_parts[0]
            doc_id = doc_id or key_parts[1]
            filename = key_parts[-1]
        else:
            filename = os.path.basename(s3_key)
            doc_id = doc_id or f"doc_{uuid.uuid4().hex[:12]}"
            org_id = org_id or "default_org"

        # 2. Download file to Lambda /tmp
        tmp_file_path = f"/tmp/{uuid.uuid4().hex}_{filename}"
        s3_client.download_file(bucket, s3_key, tmp_file_path)

        try:
            with open(tmp_file_path, "rb") as f:
                file_bytes = f.read()
        finally:
            if os.path.exists(tmp_file_path):
                os.remove(tmp_file_path)

        doc_type = detect_document_type(filename)
        pages = extract_pages(file_bytes, doc_type)
        total_pages = len(pages)

        # 3. Update Supabase document_registry status to 'processing'
        if supabase:
            supabase.table("document_registry").update({
                "status": "processing",
                "total_pages": total_pages
            }).eq("id", doc_id).execute()

        # 4. Chunk each page and prepare chunk payloads
        all_chunks = []
        page_registry_entries = []

        for page_idx, page_text in enumerate(pages, start=1):
            cleaned_text = page_text.strip()
            if not cleaned_text:
                continue

            page_hash_val = compute_hash(cleaned_text)
            chunks = chunk_content(cleaned_text, doc_type)
            page_chunk_ids = []

            for chunk_idx, chunk_text in enumerate(chunks):
                chunk_id = f"chk_{uuid.uuid4().hex[:12]}"
                page_chunk_ids.append(chunk_id)
                all_chunks.append({
                    "chunk_id": chunk_id,
                    "doc_id": doc_id,
                    "org_id": org_id,
                    "page_number": page_idx,
                    "chunk_index": chunk_idx,
                    "text": chunk_text,
                    "upload_mode": upload_mode
                })

            page_registry_entries.append({
                "document_id": doc_id,
                "org_id": org_id,
                "page_number": page_idx,
                "page_hash": page_hash_val,
                "chunk_count": len(chunks)
            })

        # Save page metadata to Supabase page_registry
        if supabase and page_registry_entries:
            try:
                supabase.table("page_registry").upsert(page_registry_entries).execute()
            except Exception as e:
                logger.warning(f"Failed to record page_registry: {e}")

        # 5. Embed in batches and dispatch to Amazon SQS
        batch_size = 20
        total_chunks = len(all_chunks)
        logger.info(f"Total chunks generated: {total_chunks}")

        for i in range(0, total_chunks, batch_size):
            chunk_batch = all_chunks[i:i + batch_size]
            texts = [c["text"] for c in chunk_batch]
            
            # Fetch embeddings from Jina AI
            embeddings = get_jina_embeddings(texts)
            for item, emb in zip(chunk_batch, embeddings):
                item["embedding"] = emb

            # Send batch payload to SQS queue
            sqs_client.send_message(
                QueueUrl=SQS_QUEUE_URL,
                MessageBody=json.dumps(chunk_batch)
            )

        logger.info(f"Successfully processed and queued {total_chunks} chunks for doc_id {doc_id}")

    return {
        "statusCode": 200,
        "body": json.dumps({"status": "parsed_and_enqueued", "records_count": len(records)})
    }
