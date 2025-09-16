"""
Simple test to understand PDF character encoding issues.
Uses only pypdf to test different extraction approaches.
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

def download_s3_object_to_memory(s3_uri: str) -> bytes:
    """Download an S3 object fully into memory and return its bytes."""
    bucket, key = parse_s3_uri(s3_uri)
    s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
    buf = io.BytesIO()
    s3.download_fileobj(bucket, key, buf)
    return buf.getvalue()

def analyze_pdf_encoding(pdf_bytes: bytes):
    """Analyze PDF encoding and character mapping issues."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    
    print(f"PDF has {len(reader.pages)} pages")
    
    for page_num, page in enumerate(reader.pages[:2], 1):
        print(f"\n--- Page {page_num} Analysis ---")
        
        # Method 1: Basic extraction
        text = page.extract_text() or ""
        print(f"Basic text length: {len(text)}")
        
        # Analyze character frequencies
        char_counts = {}
        for char in text:
            char_counts[char] = char_counts.get(char, 0) + 1
        
        # Show most frequent characters
        sorted_chars = sorted(char_counts.items(), key=lambda x: x[1], reverse=True)
        print("Most frequent characters:")
        for char, count in sorted_chars[:15]:
            if char == '\n':
                print(f"  '\\n': {count}")
            elif char == ' ':
                print(f"  'SPACE': {count}")
            elif char == '.':
                print(f"  '.': {count}")
            else:
                print(f"  '{char}': {count}")
        
        # Look for suspicious patterns
        dot_sequences = re.findall(r'\.{2,}', text)
        print(f"Dot sequences (2+): {len(dot_sequences)} -> {dot_sequences[:5]}")
        
        # Check for potential number placeholders
        potential_numbers = re.findall(r'[^\w\s][.]{1,3}[^\w\s]', text)
        print(f"Potential number patterns: {potential_numbers[:10]}")
        
        # Show raw bytes for a small section to understand encoding
        if hasattr(page, '_contents'):
            print("Raw content analysis available")
        
        # Try to find font information
        if '/Font' in page:
            print("Font information found in page")
        
        # Show a detailed character-by-character analysis of a problematic section
        lines = text.split('\n')
        for i, line in enumerate(lines[:10]):
            if '.' in line and len(line) < 100:
                print(f"Line {i}: '{line}'")
                # Show character codes
                char_codes = [f"{ord(c):04x}" for c in line[:20]]
                print(f"  Hex codes: {' '.join(char_codes)}")

def main():
    """Test PDF encoding analysis."""
    try:
        # Get first PDF
        bucket, prefix = parse_s3_uri(DEFAULT_S3_INPUT)
        s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
        
        response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        if 'Contents' not in response:
            print("No PDFs found")
            return
        
        # Find first non-empty PDF file
        first_pdf_key = None
        for obj in response['Contents']:
            if obj['Key'].lower().endswith('.pdf') and obj['Size'] > 0:
                first_pdf_key = obj['Key']
                break
        
        if not first_pdf_key:
            print("No valid PDF files found")
            return
            
        pdf_uri = f"s3://{bucket}/{first_pdf_key}"
        
        print(f"Analyzing: {os.path.basename(pdf_uri)}")
        pdf_bytes = download_s3_object_to_memory(pdf_uri)
        
        analyze_pdf_encoding(pdf_bytes)
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
