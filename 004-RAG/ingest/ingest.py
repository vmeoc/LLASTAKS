"""
RAG Manager - Local ingestion script for LLASTA

Purpose
-------
Read PDFs from S3, extract/clean/segment into chunks (V1: 1 page = 1 chunk),
then upsert to the `faiss-wrap` service (which embeds with bge-m3 and indexes in FAISS).
Also writes a manifest.parquet back to S3 for traceability.

Run locally from your laptop. Keep it simple and reliable.

Usage (example)
---------------
# Ensure port-forward to faiss-wrap (in another terminal):
#   kubectl -n llasta port-forward svc/faiss-wrap 18080:80

python 004-RAG/ingest/ingest.py \
  --s3-input s3://llasta-rag/PDF-Financial/ \
  --s3-manifest s3://llasta-rag/manifests/ \
  --faiss-wrap-url http://localhost:18080 \
  --batch-size 64 \
  --max-parallel 4

Notes for the student (what/why)
--------------------------------
- We keep segmentation simple: 1 page -> 1 chunk. This matches Stage_Readme V1.
- Cleaning removes boilerplate (whitespace-only, very short text) and normalizes spaces.
- Dedupe: we hash the cleaned text; if two chunks have the same hash, we keep the first.
- We send chunks in batches to reduce HTTP overhead (`/upsert`).
- The manifest records the ingestion details for audits and rebuilds.
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
    args = parser.parse_args()

    print(f"Listing PDFs from: {args.s3_input}")
    pdf_uris = list_s3_pdfs(args.s3_input)
    if not pdf_uris:
        print("No PDFs found. Exiting.")
        return

    print(f"Found {len(pdf_uris)} PDFs. Starting ingestion…")

    all_chunks: List[Dict[str, Any]] = []

    # Parallel download and process
    # We define an inner function so it can capture `args` and helpers in scope.
    # Each worker:
    #   1) downloads one PDF from S3
    #   2) extracts text per page
    #   3) converts pages -> cleaned chunks
    # If an error happens, we log and return an empty list so the pipeline continues.
    def process_one(uri: str) -> List[Dict[str, Any]]:
        try:
            pdf_bytes = download_s3_object_to_memory(uri)
            pages = extract_pages(pdf_bytes)
            # doc_id from filename
            doc_id = os.path.splitext(os.path.basename(uri))[0]
            return make_chunks(doc_id=doc_id, source_uri=uri, pages=pages)
        except Exception as e:
            print(f"Error processing {uri}: {e}")
            return []

    # Use a thread pool to parallelize I/O-bound steps (S3 download, PDF parsing).
    # `max_workers` controls the parallelism level; keep small to avoid overloading S3.
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_parallel) as ex:
        futures = [ex.submit(process_one, uri) for uri in pdf_uris]
        # as_completed yields futures as they finish, giving streaming progress.
        for fut in concurrent.futures.as_completed(futures):
            chunks = fut.result()
            all_chunks.extend(chunks)

    print(f"Prepared {len(all_chunks)} chunks after cleaning/dedupe.")

    # Upsert in batches
    # We batch to keep requests smaller and more resilient.
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

    # Write manifest
    # After successful upserts, we persist a manifest for traceability and rebuilds.
    manifest_uri = write_manifest(args.s3_manifest, all_chunks)
    print(f"Wrote manifest: {manifest_uri}")


if __name__ == "__main__":
    main()
