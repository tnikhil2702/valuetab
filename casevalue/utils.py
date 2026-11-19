import os
import asyncio
from typing import Dict, Tuple, Optional
import concurrent.futures
from dotenv import load_dotenv
load_dotenv()
from pydantic import BaseModel
import fitz
from PIL import Image
from anthropic import Anthropic
import pytesseract
from azure.storage.blob import BlobServiceClient
from fastapi import UploadFile
from redaction import Redactor
import io

casevalue_redactor = Redactor()

# N8n Webhook URL for triggering workflows for case processing
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")  

# Azure Blob Storage setup for storing case summaries
AZURE_CONN_STR = os.getenv("AZURE_BLOB_CONNECTION_STRING")
AZURE_CONTAINER = os.getenv("AZURE_BLOB_CONTAINER_CASEVALUE")

try:
    blob_service = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
    container = blob_service.get_container_client(AZURE_CONTAINER)
except Exception as e:
    raise RuntimeError(f"Azure Connection Failed: {e}")

# Claude configuration
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL")

claude_client = Anthropic(api_key=CLAUDE_API_KEY)

# job tracking dictionaries
JOBS: Dict[str, Dict] = {}
CASE_JOBS: Dict[str, Dict] = {}
executor = concurrent.futures.ThreadPoolExecutor(max_workers=4) 

class SummarizeRequest(BaseModel):
    text: str
    prompt: str 

async def extract_from_pdf(file: UploadFile) -> Dict:
    content = await file.read()
    filename = file.filename or "unknown.pdf"

    print(f"[OCR] → Processing: {filename} ({len(content):,} bytes)")

    # OCR extraction
    text = await asyncio.get_event_loop().run_in_executor(
        executor,
        ocr_pdf_bytes,
        content
    )

    result = casevalue_redactor.redact(
        text,
        filename
    )

    return {
        "filename": filename,
        "redacted_text": result["text"],
        "stats": result["stats"],
        "audit": result["redacted_items"]
    }

def ocr_pdf_bytes(pdf_bytes: bytes) -> str:
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        total_pages = len(doc)
        extracted_text = []

        for page_num in range(total_pages):
            page = doc[page_num]

            text = page.get_text().strip()
            if text and len(text) > 50: 
                extracted_text.append(text)
                continue

            zoom = 2.5  
            matrix = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            img_bytes = pix.tobytes("ppm")
            img = Image.open(io.BytesIO(img_bytes))

            config = r'--oem 3 --psm 6'
            ocr_text = pytesseract.image_to_string(img, lang='eng', config=config)
            extracted_text.append(ocr_text.strip())

        doc.close()
        full_text = "\n\n--- PAGE BREAK ---\n\n".join(extracted_text)
        return full_text if full_text.strip() else "[NO TEXT FOUND IN PDF]"

    except Exception as e:
        return f"[OCR FAILED: {type(e).__name__}: {str(e)}]"


async def check_case_summary_in_blob(case_number: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        blob_list = container.list_blobs(name_starts_with=f"{case_number}/")
        md_files = [blob for blob in blob_list if blob.name.endswith(".md")]
        if not md_files:
            return None, None
        latest_blob = max(md_files, key=lambda x: x.last_modified)
        blob_client = container.get_blob_client(latest_blob.name)
        blob_data = blob_client.download_blob()
        summary_content = blob_data.readall().decode("utf-8")
        
        return summary_content, latest_blob.name
        
    except Exception as e:
        return None, None

CLAUDE_SONNET = {
    "input_per_million": 3.00,
    "output_per_million": 15.00
} 

async def calculate_cost(input_tokens: int, output_tokens: int):
    input_cost = (input_tokens / 1_000_000) * CLAUDE_SONNET["input_per_million"]
    output_cost = (output_tokens / 1_000_000) * CLAUDE_SONNET["output_per_million"]

    return {
        "input_cost": round(input_cost, 6),
        "output_cost": round(output_cost, 6)
    }