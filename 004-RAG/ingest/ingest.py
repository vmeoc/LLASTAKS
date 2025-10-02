"""
RAG Manager - Local ingestion script for LLASTA

Purpose
-------
Read PDFs (from S3 or local files), extract/clean/segment into chunks, 
then upsert to the `faiss-wrap` service (which embeds with bge-m3 and indexes in FAISS).
Also writes a manifest.parquet back to S3 for traceability (S3 mode only).

How it works
------------
1. **Input**: PDFs from S3 bucket or local files
2. **Extraction**: Uses pypdf to extract text from each page
3. **Chunking**: 1 page = 1 chunk (simple segmentation strategy)
4. **Cleaning**: Removes whitespace, normalizes text, filters short chunks
5. **Deduplication**: Uses SHA256 hash to remove duplicate content
6. **Output**: Sends chunks to FAISS vector database via faiss-wrap API

Architecture explained for students
-----------------------------------
- **Parallel processing**: Uses ThreadPoolExecutor for concurrent PDF processing
- **Batch upserts**: Groups chunks into batches to reduce HTTP overhead
- **Error handling**: Continues processing even if individual PDFs fail
- **Metadata tracking**: Each chunk includes source URI, page number, token count
- **Dry-run mode**: Preview extracted data before sending to vector database

Available Arguments
-------------------
Input Sources (choose one):
  pdf_files                 Local PDF files to process (positional arguments)
  --s3-input               S3 prefix with PDFs (default: s3://llasta-rag/PDF-Financial/)

Processing Options:
  --dry-run                Show extracted data without sending to FAISS
  --batch-size             Upsert batch size (default: 64)
  --max-parallel           Parallel downloads/processing (default: 4)
  --preview-chars          Characters to preview in logs (default: 80)
  --max-chunks             Limit the number of chunks to upsert (0 = no limit)

FAISS Integration:
  --faiss-wrap-url         faiss-wrap base URL (default: http://localhost:18080)
  --http-timeout           Timeout for /upsert requests (default: 300.0s)

S3 Configuration (S3 mode only):
  --s3-manifest            S3 prefix to write manifests (default: s3://llasta-rag/manifests/)

Usage Examples
--------------
# 1. Dry-run with local PDF (recommended for testing)
python ingest.py --dry-run "path/to/bank_statement.pdf" --preview-chars 200

# 2. Process multiple local PDFs
python ingest.py --dry-run PDFs/*.pdf

# 3. Send local PDF to FAISS (ensure faiss-wrap is running)
#    kubectl -n llasta port-forward svc/faiss-wrap 18080:80
python ingest.py "path/to/bank_statement.pdf" --faiss-wrap-url http://localhost:18080

# 4. Process all PDFs from S3 (original mode)
python ingest.py --s3-input s3://llasta-rag/PDF-Financial/ --batch-size 32

# 5. Dry-run with S3 PDFs
python ingest.py --dry-run --s3-input s3://llasta-rag/PDF-Financial/

Prerequisites
-------------
- For local mode: PDF files accessible on filesystem
- For S3 mode: AWS credentials configured, access to S3 bucket
- For FAISS mode: faiss-wrap service running (port-forward or direct access)
- Python dependencies: pypdf, pandas, requests, boto3

Output Format
-------------
Each chunk contains:
- id: unique identifier (filename#page-XXXX)
- text: cleaned extracted text
- metadata: {doc_id, source_uri, page, lang}
- _row_hash: SHA256 for deduplication
- _token_count: approximate token count

Notes for RAG Implementation
----------------------------
- Simple chunking (1 page = 1 chunk) works well for structured documents like bank statements
- For complex documents, consider semantic chunking or overlapping windows
- Monitor chunk sizes - too small loses context, too large reduces retrieval precision
- The cleaning process removes formatting but preserves numerical data
"""

import argparse
import concurrent.futures
import hashlib
import io
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Iterator, List, Dict, Any, Tuple

import boto3
import pandas as pd
import requests
from botocore.client import Config
from botocore.exceptions import ClientError
from pypdf import PdfReader

# Optional: pyarrow for parquet is used by pandas under the hood. Ensure installed.

DEFAULT_FAISS_WRAP_URL = os.getenv("FAISS_WRAP_URL", "http://localhost:18080")
DEFAULT_S3_INPUT = os.getenv("S3_INPUT_PREFIX", "s3://llasta-rag/PDF-Financial/")
DEFAULT_S3_MANIFEST = os.getenv("S3_MANIFEST_PREFIX", "s3://llasta-rag/manifests/")


# -------------------------
# Helpers
# -------------------------

def parse_s3_uri(uri: str) -> Tuple[str, str]:
    """Parse an S3 URI like s3://bucket/prefix into (bucket, prefix).

    - Validates the scheme starts with "s3://".
    - Returns the bucket and a prefix without a trailing slash.
    """
    if not uri.startswith("s3://"):
        raise ValueError(f"Invalid S3 URI: {uri}")
    bucket_key = uri[5:]
    parts = bucket_key.split("/", 1)
    bucket = parts[0]
    prefix = parts[1] if len(parts) > 1 else ""
    return bucket, prefix.rstrip("/")


def list_s3_pdfs(s3_uri: str) -> List[str]:
    """List all PDF object URIs under an S3 prefix.

    Uses a paginator to walk keys under the given prefix and filters those
    that end with .pdf (case-insensitive). Returns full s3:// URIs.
    """
    bucket, prefix = parse_s3_uri(s3_uri)
    s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
    paginator = s3.get_paginator("list_objects_v2")
    keys: List[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(".pdf"):
                keys.append(key)
    return [f"s3://{bucket}/{k}" for k in keys]


def download_s3_object_to_memory(s3_uri: str) -> bytes:
    """Download an S3 object fully into memory and return its bytes."""
    bucket, key = parse_s3_uri(s3_uri)
    s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
    buf = io.BytesIO()
    s3.download_fileobj(bucket, key, buf)
    return buf.getvalue()


def extract_pages(pdf_bytes: bytes) -> List[str]:
    """Extract text per page from a PDF byte buffer.

    We use pypdf's PdfReader to iterate pages and call extract_text().
    Some PDFs may have pages where extraction fails or returns None;
    in those cases we store an empty string.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages: List[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        pages.append(text)
    return pages


def clean_text(t: str) -> str:
    """Normalize text for ingestion.

    - Replace non-breaking spaces with regular spaces.
    - Remove tabs/carriage/form-feeds.
    - Collapse multiple whitespace to single spaces and trim.
    """
    # Normalize whitespace, remove stray non-breaking spaces, collapse multiple spaces
    t = t.replace("\xa0", " ")
    t = re.sub(r"[\t\r\f]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def valid_chunk(t: str) -> bool:
    """Heuristic filter to drop empty or too-short chunks."""
    # Filter out extremely short or empty chunks
    return len(t) >= 20


def sha256_hex(s: str) -> str:
    """Deterministic content hash used for de-duplication."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def make_chunks(doc_id: str, source_uri: str, pages: List[str]) -> List[Dict[str, Any]]:
    """Turn a list of page texts into cleaned, deduplicated chunks.

    Strategy V1:
    - 1 page -> 1 chunk if it passes `valid_chunk()`.
    - Add metadata for traceability and a `_row_hash` for dedupe.
    - After building, dedupe by `_row_hash`, keeping the first occurrence.
    """
    chunks: List[Dict[str, Any]] = []
    for idx, raw in enumerate(pages, start=1):
        cleaned = clean_text(raw)
        if not valid_chunk(cleaned):
            continue
        row_hash = sha256_hex(cleaned)
        chunk_id = f"{doc_id}#page-{idx:04d}"
        chunks.append({
            "id": chunk_id,
            "text": cleaned,
            "metadata": {
                "doc_id": doc_id,
                "source_uri": f"{source_uri}#page={idx}",
                "page": idx,
                "lang": "fr"
            },
            "_row_hash": row_hash,
            "_token_count": len(cleaned.split())  # rough proxy
        })
    # Dedupe by row_hash, keep first occurrence
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for c in chunks:
        h = c["_row_hash"]
        if h in seen:
            continue
        seen.add(h)
        deduped.append(c)
    return deduped


def batched(iterable: List[Any], n: int) -> Iterator[List[Any]]:
    """Yield successive lists of size n from an input list."""
    for i in range(0, len(iterable), n):
        yield iterable[i:i+n]


def upsert_batch(faiss_wrap_url: str, items: List[Dict[str, Any]], timeout: float = 60.0) -> None:
    """Send a batch of chunks to faiss-wrap `/upsert` endpoint.

    The payload contains minimal fields required by the service:
      - items: [{id, text, metadata}]
    """
    t0 = time.time()
    # Log a short preview to help debug without flooding the console
    print(f"[Upsert] Preparing batch size={len(items)}")
    payload_items = [
        {"id": it["id"], "text": it["text"], "metadata": it["metadata"]}
        for it in items
    ]
    body = {"items": payload_items}
    r = requests.post(f"{faiss_wrap_url}/upsert", json=body, timeout=timeout)
    r.raise_for_status()
    dt = time.time() - t0
    print(f"[Upsert] Completed HTTP 200 in {dt:.1f}s (size={len(items)})")


def write_manifest(s3_uri_prefix: str, rows: List[Dict[str, Any]], embedding_model: str = "bge-m3", dim: int = 1024) -> str:
    """Write a parquet manifest of ingested chunks to S3 and return its URI.

    The manifest captures provenance and parameters for audit/repro:
    - doc_id, chunk_id, hash, model metadata, timestamps, source_uri, page, etc.
    """
    bucket, prefix = parse_s3_uri(s3_uri_prefix)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"{prefix.rstrip('/')}/manifest_{ts}.parquet"

    records = []
    for r in rows:
        records.append({
            "doc_id": r["metadata"]["doc_id"],
            "chunk_id": r["id"],
            "row_hash": r["_row_hash"],
            "embedding_model": embedding_model,
            "dim": dim,
            "ts": ts,
            "source_uri": r["metadata"]["source_uri"],
            "page": r["metadata"]["page"],
            "token_count": r["_token_count"],
            "lang": r["metadata"].get("lang", "fr"),
            "schema_version": 1,
        })

    df = pd.DataFrame.from_records(records)
    # Write to local buffer then upload to S3
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    buf.seek(0)

    s3 = boto3.client("s3")
    s3.upload_fileobj(buf, bucket, key)
    return f"s3://{bucket}/{key}"


# -------------------------
# Main
# -------------------------

def main():
    parser = argparse.ArgumentParser(description="LLASTA RAG Manager - ingest PDFs to faiss-wrap")
    parser.add_argument("--s3-input", default=DEFAULT_S3_INPUT, help="S3 prefix with PDFs, e.g. s3://bucket/prefix/")
    parser.add_argument("--s3-manifest", default=DEFAULT_S3_MANIFEST, help="S3 prefix to write manifests")
    parser.add_argument("--faiss-wrap-url", default=DEFAULT_FAISS_WRAP_URL, help="faiss-wrap base URL")
    parser.add_argument("--batch-size", type=int, default=64, help="Upsert batch size")
    parser.add_argument("--max-parallel", type=int, default=4, help="Parallel downloads")
    parser.add_argument("--http-timeout", type=float, default=300.0, help="Timeout in seconds for each /upsert HTTP request")
    parser.add_argument("--preview-chars", type=int, default=80, help="Number of characters to preview from each text in logs")
    parser.add_argument("--max-chunks", type=int, default=0, help="Maximum number of chunks to upsert (0 means no limit)")
    parser.add_argument("--dry-run", action="store_true", help="Show extracted data without sending to FAISS")
    parser.add_argument("pdf_files", nargs="*", help="Local PDF files to process (alternative to S3)")
    args = parser.parse_args()

    # Determine input source: local files or S3
    if args.pdf_files:
        print(f"Processing {len(args.pdf_files)} local PDF files...")
        pdf_sources = args.pdf_files
        use_local = True
    else:
        print(f"Listing PDFs from: {args.s3_input}")
        pdf_sources = list_s3_pdfs(args.s3_input)
        use_local = False
        
    if not pdf_sources:
        print("No PDFs found. Exiting.")
        return

    print(f"Found {len(pdf_sources)} PDFs. Starting {'dry-run analysis' if args.dry_run else 'ingestion'}…")

    all_chunks: List[Dict[str, Any]] = []

    # Process function for both local and S3 files
    def process_one(source: str) -> List[Dict[str, Any]]:
        try:
            if use_local:
                # Local file processing
                with open(source, 'rb') as f:
                    pdf_bytes = f.read()
                doc_id = os.path.splitext(os.path.basename(source))[0]
                source_uri = f"file://{os.path.abspath(source)}"
            else:
                # S3 processing (original logic)
                pdf_bytes = download_s3_object_to_memory(source)
                doc_id = os.path.splitext(os.path.basename(source))[0]
                source_uri = source
                
            pages = extract_pages(pdf_bytes)
            return make_chunks(doc_id=doc_id, source_uri=source_uri, pages=pages)
        except Exception as e:
            print(f"Error processing {source}: {e}")
            return []

    # Use a thread pool to parallelize I/O-bound steps
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_parallel) as ex:
        futures = [ex.submit(process_one, source) for source in pdf_sources]
        for fut in concurrent.futures.as_completed(futures):
            chunks = fut.result()
            all_chunks.extend(chunks)

    print(f"Prepared {len(all_chunks)} chunks after cleaning/dedupe.")

    # Apply optional cap on number of chunks to send (non-dry-run only)
    if not args.dry_run and args.max_chunks and args.max_chunks > 0:
        if len(all_chunks) > args.max_chunks:
            print(f"Limiting to the first {args.max_chunks} chunks (out of {len(all_chunks)}) before upsert…")
            all_chunks = all_chunks[: args.max_chunks]
        else:
            print(f"--max-chunks set to {args.max_chunks}, but only {len(all_chunks)} chunks prepared. Proceeding with all.")

    # Dry-run mode: show extracted data without sending to FAISS
    if args.dry_run:
        print("\n" + "="*80)
        print("DRY-RUN MODE: Showing ALL extracted data (not sending to FAISS)")
        print("="*80)
        
        # Statistics summary first
        total_tokens = sum(chunk['_token_count'] for chunk in all_chunks)
        avg_tokens = total_tokens / len(all_chunks) if all_chunks else 0
        print(f"\n📊 STATISTICS:")
        print(f"   Total chunks: {len(all_chunks)}")
        print(f"   Total tokens: {total_tokens}")
        print(f"   Average tokens per chunk: {avg_tokens:.1f}")
        print(f"   Min tokens: {min(chunk['_token_count'] for chunk in all_chunks) if all_chunks else 0}")
        print(f"   Max tokens: {max(chunk['_token_count'] for chunk in all_chunks) if all_chunks else 0}")
        
        # Show ALL chunks with complete data
        for i, chunk in enumerate(all_chunks):
            print(f"\n{'='*50} CHUNK {i+1}/{len(all_chunks)} {'='*50}")
            
            # Core identifiers
            print(f"🆔 ID: {chunk['id']}")
            print(f"📄 Source: {chunk['metadata']['source_uri']}")
            print(f"📖 Page: {chunk['metadata']['page']}")
            print(f"🏷️  Language: {chunk['metadata'].get('lang', 'N/A')}")
            print(f"🔢 Token count: {chunk['_token_count']}")
            print(f"🔐 Hash: {chunk['_row_hash'][:16]}...")
            
            # Complete metadata
            print(f"\n📋 COMPLETE METADATA:")
            for key, value in chunk['metadata'].items():
                print(f"   {key}: {value}")
            
            # Text analysis
            text = chunk['text']
            print(f"\n📝 TEXT ANALYSIS:")
            print(f"   Length: {len(text)} characters")
            print(f"   Lines: {text.count(chr(10)) + 1}")
            print(f"   Words: {len(text.split())}")
            
            # Find all numbers (financial data detection)
            import re
            # Enhanced number patterns for financial data
            currency_pattern = r'[-+]?\d{1,3}(?:[.,]\d{3})*[.,]\d{2}\s*(?:EUR|€|USD|\$)?'
            number_pattern = r'[-+]?\d+[.,]\d+|\d+'
            dates_pattern = r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}'
            
            currencies = re.findall(currency_pattern, text)
            numbers = re.findall(number_pattern, text)
            dates = re.findall(dates_pattern, text)
            
            print(f"\n💰 FINANCIAL DATA DETECTED:")
            if currencies:
                print(f"   Currency amounts: {currencies[:5]}{'...' if len(currencies) > 5 else ''}")
                print(f"   Total currency amounts found: {len(currencies)}")
            else:
                print("   No currency amounts found")
                
            if numbers:
                print(f"   All numbers: {numbers[:10]}{'...' if len(numbers) > 10 else ''}")
                print(f"   Total numbers found: {len(numbers)}")
            else:
                print("   No numbers found")
                
            if dates:
                print(f"   Dates: {dates[:5]}{'...' if len(dates) > 5 else ''}")
                print(f"   Total dates found: {len(dates)}")
            else:
                print("   No dates found")
            
            # Full text content (with line numbers for debugging)
            print(f"\n📄 COMPLETE TEXT CONTENT:")
            print("-" * 60)
            lines = text.split('\n')
            for line_num, line in enumerate(lines, 1):
                if line.strip():  # Only show non-empty lines
                    print(f"{line_num:3d}: {line}")
            print("-" * 60)
            
            # What would be sent to FAISS (exact payload)
            faiss_payload = {
                "id": chunk["id"], 
                "text": chunk["text"], 
                "metadata": chunk["metadata"]
            }
            print(f"\n🔄 FAISS PAYLOAD (what would be sent):")
            print(f"   Payload size: {len(str(faiss_payload))} characters")
            print(f"   Keys: {list(faiss_payload.keys())}")
            print(f"   Text length in payload: {len(faiss_payload['text'])}")
            
            # Separator between chunks
            if i < len(all_chunks) - 1:
                print(f"\n{'⬇️ ' * 20}")
        
        print(f"\n{'='*80}")
        print(f"✅ DRY-RUN COMPLETE")
        print(f"📊 Analyzed {len(all_chunks)} chunks from {len(set(chunk['metadata']['doc_id'] for chunk in all_chunks))} documents")
        print(f"💾 Total data ready for FAISS: ~{sum(len(str(chunk)) for chunk in all_chunks)} characters")
        print(f"🚀 Use without --dry-run to send to FAISS at: {args.faiss_wrap_url}")
        print("="*80)
        return

    # Normal mode: upsert to FAISS
    total = 0
    ingest_t0 = time.time()
    for batch in batched(all_chunks, args.batch_size):
        # Show a small sample of the batch for observability
        sample_n = min(3, len(batch))
        print("[Upsert] Sample items:")
        for i in range(sample_n):
            preview = batch[i]["text"][: args.preview_chars].replace("\n", " ")
            print(f"  - id={batch[i]['id']} | text[:{args.preview_chars}]='{preview}...' ")
        upsert_batch(args.faiss_wrap_url, batch, timeout=args.http_timeout)
        total += len(batch)
    print(f"Upserted {total} chunks to {args.faiss_wrap_url}.")

    # Overall timing summary
    ingest_dt = time.time() - ingest_t0
    per_item = (ingest_dt / total) if total else 0.0
    print(f"[Summary] Upsert phase took {ingest_dt:.1f}s for {total} items ({per_item:.2f}s/item avg)")

    # Write manifest (only for S3 mode)
    if not use_local:
        manifest_uri = write_manifest(args.s3_manifest, all_chunks)
        print(f"Wrote manifest: {manifest_uri}")
    else:
        print("Skipping manifest write for local files")


if __name__ == "__main__":
    main()
