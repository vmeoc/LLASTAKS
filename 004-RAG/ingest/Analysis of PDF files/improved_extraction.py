"""
Improved PDF extraction for banking documents with better number handling.
This script tests multiple extraction methods to handle PDFs where numbers
appear as dots or special characters.
"""

import io
import os
import re
from typing import Tuple, List, Dict, Any
import boto3
from botocore.client import Config
from pypdf import PdfReader
import pdfplumber

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

def extract_with_pypdf(pdf_bytes: bytes) -> List[str]:
    """Extract text using pypdf (original method)."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
            pages.append(text)
        except Exception:
            pages.append("")
    return pages

def extract_with_pdfplumber(pdf_bytes: bytes) -> List[str]:
    """Extract text using pdfplumber - better for tables and structured data."""
    pages = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                try:
                    # Try extracting as text first
                    text = page.extract_text() or ""
                    
                    # If text extraction fails or produces dots, try table extraction
                    if not text or text.count('.') > len(text) * 0.3:  # Too many dots
                        tables = page.extract_tables()
                        if tables:
                            # Convert tables to text format
                            table_text = ""
                            for table in tables:
                                for row in table:
                                    if row:
                                        clean_row = [str(cell) if cell else "" for cell in row]
                                        table_text += " | ".join(clean_row) + "\n"
                            text = table_text
                    
                    pages.append(text)
                except Exception as e:
                    print(f"Error extracting page with pdfplumber: {e}")
                    pages.append("")
    except Exception as e:
        print(f"Error opening PDF with pdfplumber: {e}")
        return []
    
    return pages

def extract_with_character_mapping(pdf_bytes: bytes) -> List[str]:
    """Try to extract with character mapping to handle special encodings."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    
    for page in reader.pages:
        try:
            # Get the raw text with character mapping
            text = ""
            if "/Contents" in page:
                # Try to access the raw content stream
                content = page.extract_text()
                
                # Try different encoding approaches
                if content:
                    # Replace common problematic characters
                    content = content.replace('\uf8ff', '')  # Apple logo
                    content = content.replace('\uf020', ' ')  # Special space
                    
                    # Try to detect and fix number patterns
                    # Look for patterns like ". . ,." which should be numbers
                    content = re.sub(r'(\. ){2,}', ' [NUMBER] ', content)
                    content = re.sub(r'\.([,.])', r'[NUMBER]\1', content)
                    
                text = content or ""
            
            pages.append(text)
        except Exception as e:
            print(f"Error with character mapping: {e}")
            pages.append("")
    
    return pages

def analyze_extraction_results(pdf_uri: str, max_pages: int = 2):
    """Compare different extraction methods for a PDF."""
    print(f"\n=== Analyzing PDF: {os.path.basename(pdf_uri)} ===")
    
    try:
        pdf_bytes = download_s3_object_to_memory(pdf_uri)
        print(f"Downloaded {len(pdf_bytes)} bytes")
        
        # Test different extraction methods
        methods = {
            "pypdf": extract_with_pypdf,
            "pdfplumber": extract_with_pdfplumber,
            "character_mapping": extract_with_character_mapping
        }
        
        results = {}
        for method_name, method_func in methods.items():
            try:
                print(f"\n--- Testing {method_name} ---")
                pages = method_func(pdf_bytes)
                results[method_name] = pages[:max_pages]
                
                for i, page_text in enumerate(pages[:max_pages], 1):
                    print(f"Page {i} - Length: {len(page_text)} chars")
                    
                    # Analyze content
                    numbers = re.findall(r'\d+[.,]\d+|\d+', page_text)
                    dots = page_text.count('.')
                    
                    print(f"  Numbers found: {len(numbers)}")
                    print(f"  Dots count: {dots}")
                    print(f"  Sample: {page_text[:200]}...")
                    
                    if numbers:
                        print(f"  First numbers: {numbers[:5]}")
                    
            except Exception as e:
                print(f"Error with {method_name}: {e}")
                results[method_name] = []
        
        # Compare results
        print(f"\n--- Comparison Summary ---")
        for method_name, pages in results.items():
            if pages:
                total_numbers = sum(len(re.findall(r'\d+[.,]\d+|\d+', page)) for page in pages)
                total_dots = sum(page.count('.') for page in pages)
                print(f"{method_name}: {total_numbers} numbers, {total_dots} dots")
        
        return results
        
    except Exception as e:
        print(f"Error analyzing PDF {pdf_uri}: {e}")
        return {}

def test_improved_extraction():
    """Test improved extraction methods on banking PDFs."""
    print("Testing improved PDF extraction methods...")
    
    try:
        # Get first PDF for testing
        bucket, prefix = parse_s3_uri(DEFAULT_S3_INPUT)
        s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
        
        # List first PDF
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
        
        results = analyze_extraction_results(pdf_uri, max_pages=2)
        
        # Recommend best method
        best_method = None
        best_score = 0
        
        for method_name, pages in results.items():
            if pages:
                score = sum(len(re.findall(r'\d+[.,]\d+|\d+', page)) for page in pages)
                if score > best_score:
                    best_score = score
                    best_method = method_name
        
        print(f"\n=== RECOMMENDATION ===")
        if best_method:
            print(f"Best extraction method: {best_method} (found {best_score} numbers)")
        else:
            print("No method successfully extracted numbers. May need OCR or manual processing.")
            
    except Exception as e:
        print(f"Error in test: {e}")

if __name__ == "__main__":
    test_improved_extraction()
