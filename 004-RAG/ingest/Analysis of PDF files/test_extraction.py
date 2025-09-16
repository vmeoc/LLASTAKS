"""
Test script to analyze PDF extraction issues with banking transaction data.
This will help us understand why numbers are missing from the extracted text.
Reads PDFs from S3 like the main ingest.py script.
"""

import io
import os
import re
from typing import Tuple, List
import boto3
from botocore.client import Config
from pypdf import PdfReader

DEFAULT_S3_INPUT = os.getenv("S3_INPUT_PREFIX", "s3://llasta-rag/PDF-Financial/")

def parse_s3_uri(uri: str) -> Tuple[str, str]:
    """Parse an S3 URI like s3://bucket/prefix into (bucket, prefix)."""
    if not uri.startswith("s3://"):
        raise ValueError(f"Invalid S3 URI: {uri}")
    bucket_key = uri[5:]
    parts = bucket_key.split("/", 1)
    bucket = parts[0]
    prefix = parts[1] if len(parts) > 1 else ""
    return bucket, prefix.rstrip("/")

def list_s3_pdfs(s3_uri: str) -> List[str]:
    """List all PDF object URIs under an S3 prefix."""
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

def test_pdf_extraction(pdf_uri: str, max_pages: int = 3):
    """Test PDF extraction and show detailed output for analysis."""
    print(f"\n=== Testing PDF: {os.path.basename(pdf_uri)} ===")
    
    try:
        # Download PDF from S3
        print("Downloading PDF from S3...")
        pdf_bytes = download_s3_object_to_memory(pdf_uri)
        print(f"Downloaded {len(pdf_bytes)} bytes")
        
        reader = PdfReader(io.BytesIO(pdf_bytes))
        print(f"Total pages: {len(reader.pages)}")
        
        for page_num, page in enumerate(reader.pages[:max_pages], 1):
            print(f"\n--- Page {page_num} ---")
            
            # Test different extraction methods
            try:
                # Method 1: Basic extract_text()
                text_basic = page.extract_text() or ""
                print(f"Basic extraction length: {len(text_basic)} chars")
                
                # Show first 800 chars to analyze structure (increased from 500)
                preview = text_basic[:800].replace('\n', '\\n')
                print(f"Preview (first 800 chars): {preview}")
                
                # Look for numbers in the text
                numbers = re.findall(r'\d+[.,]\d+|\d+', text_basic)
                print(f"Numbers found: {len(numbers)} -> {numbers[:15]}...")  # Show first 15
                
                # Check for currency symbols and common banking terms
                currency_matches = re.findall(r'[€$£]\s*\d+[.,]\d*|\d+[.,]\d*\s*[€$£]', text_basic)
                print(f"Currency amounts: {currency_matches[:10]}...")
                
                # Look for table-like structures (multiple spaces or tabs)
                table_lines = [line for line in text_basic.split('\n') if '\t' in line or '  ' in line]
                print(f"Potential table lines: {len(table_lines)}")
                if table_lines:
                    print(f"Sample table line: {table_lines[0][:150]}...")
                
                # Look for banking-specific patterns
                dates = re.findall(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}', text_basic)
                print(f"Date patterns found: {len(dates)} -> {dates[:5]}...")
                
                # Look for transaction amounts (negative and positive)
                amounts = re.findall(r'[-+]?\d+[.,]\d{2}', text_basic)
                print(f"Transaction amounts: {len(amounts)} -> {amounts[:10]}...")
                
                # Check for common banking terms
                banking_terms = ['DEBIT', 'CREDIT', 'SOLDE', 'BALANCE', 'VIREMENT', 'PRELEVEMENT', 'CARTE']
                found_terms = [term for term in banking_terms if term in text_basic.upper()]
                print(f"Banking terms found: {found_terms}")
                
            except Exception as e:
                print(f"Error extracting page {page_num}: {e}")
                
    except Exception as e:
        print(f"Error processing PDF {pdf_uri}: {e}")

def main():
    """Test extraction on sample banking PDFs from S3."""
    print("Listing PDFs from S3...")
    
    try:
        pdf_uris = list_s3_pdfs(DEFAULT_S3_INPUT)
        if not pdf_uris:
            print("No PDFs found in S3. Check your AWS credentials and S3 bucket access.")
            return
        
        print(f"Found {len(pdf_uris)} PDFs in S3")
        
        # Test first 2 PDFs
        for pdf_uri in pdf_uris[:2]:
            test_pdf_extraction(pdf_uri, max_pages=2)
            
    except Exception as e:
        print(f"Error accessing S3: {e}")
        print("Make sure your AWS credentials are configured and you have access to the S3 bucket.")

if __name__ == "__main__":
    main()
