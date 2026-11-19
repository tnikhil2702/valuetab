import os
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime
from PIL import Image
import pytesseract
from datetime import datetime, timedelta
import smtplib
from email.message import EmailMessage
from docx import Document
from .config import SMTP_SERVER, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD
from azure.storage.blob.aio import BlobServiceClient
from azure.storage.blob import generate_blob_sas, BlobSasPermissions
from datetime import datetime, timedelta
from io import BytesIO
from email.header import decode_header
from db import Email2AI, SessionLocal


try:
    import torch
    PYTORCH_AVAILABLE = torch.cuda.is_available()
except ImportError:
    PYTORCH_AVAILABLE = False

# === Azure connection functions ===
AZURE_CONN_STR = os.getenv("AZURE_BLOB_CONNECTION_STRING")
AZURE_BLOB_CONTAINER = os.getenv("AZURE_BLOB_CONTAINER_EMAIL2AI")
AZURE_BLOB_ACCOUNT_NAME = os.getenv("AZURE_BLOB_ACCOUNT_NAME")
AZURE_BLOB_ACCOUNT_KEY = os.getenv("AZURE_BLOB_ACCOUNT_KEY")

blob_service = BlobServiceClient.from_connection_string(AZURE_CONN_STR)

async def upload_blob(file_bytes: bytes, filename: str):

    container = blob_service.get_container_client(AZURE_BLOB_CONTAINER)
    blob_client = container.get_blob_client(filename)

    await blob_client.upload_blob(file_bytes, overwrite=True, timeout=2000, connection_timeout=1800, max_concurrency=2)

async def blob_exists(filename: str) -> bool:

    container = blob_service.get_container_client(AZURE_BLOB_CONTAINER)
    blob_client = container.get_blob_client(filename)

    return await blob_client.exists()


async def delete_blob(filename: str):

    container = blob_service.get_container_client(AZURE_BLOB_CONTAINER)
    blob_client = container.get_blob_client(filename)

    if await blob_client.exists():
        await blob_client.delete_blob()


async def list_blobs():

    container = blob_service.get_container_client(AZURE_BLOB_CONTAINER)
    files = []
    async for blob in container.list_blobs():
        files.append({
            "filename": blob.name,
            "size_kb": round(blob.size / 1024, 2) if blob.size else 0,
            "last_modified": blob.last_modified.isoformat() if blob.last_modified else None
        })
    return files

async def generate_blob_sas_url(filename: str, expiry_minutes: int = 720) -> str:
    """
    Generate SAS download URL for a blob
    """
    sas_token = generate_blob_sas(
        account_name=AZURE_BLOB_ACCOUNT_NAME,
        container_name=AZURE_BLOB_CONTAINER,
        blob_name=filename,
        account_key=AZURE_BLOB_ACCOUNT_KEY,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.utcnow() + timedelta(minutes=expiry_minutes),
    )

    return f"https://{AZURE_BLOB_ACCOUNT_NAME}.blob.core.windows.net/{AZURE_BLOB_CONTAINER}/{filename}?{sas_token}"


def ocr_page(page):
    """Extract text from a page: try embedded text first, then OCR if needed."""
    text = page.get_text("text").strip()
    if not text or len(text) < 50:
        # Perform OCR for scanned pages
        pix = page.get_pixmap(dpi=300)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        text = pytesseract.image_to_string(img).strip()
    return text 

async def create_summary_docx(summary_text: str, file_id: str):

    document = Document()
    document.add_heading("Summary", level=1)

    for line in summary_text.split("\n"):
        document.add_paragraph(line)

    file_stream = BytesIO()
    document.save(file_stream)
    file_stream.seek(0)

    filename = f"summaries/{file_id}/summary.docx"

    return filename, file_stream.read()

async def send_email_with_attachment(to_email, subject, body, download_url):

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USERNAME
    msg["To"] = to_email

    msg.set_content(f"""
    {body}

    Download your generated summary here:

    {download_url}

    This secure link will expire automatically.
    """)

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)

        print("Email sent successfully")

    except Exception as e:
        print("SMTP ERROR:", e)  

async def decode_mime_words(s):
    decoded_fragments = decode_header(s)
    return ''.join([str(t[0], t[1] or "utf-8") if isinstance(t[0], bytes) else t[0] for t in decoded_fragments])

async def save_attachment(msg):
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get("Content-Disposition") is None:
            continue

        filename = part.get_filename()
        if filename:
            filename = await decode_mime_words(filename)

            file_bytes = part.get_payload(decode=True)

            return filename, file_bytes
    return None, None

async def authenticate_email(sender_email, birthdate_code):
    db = SessionLocal()

    try:
        user = db.query(Email2AI).filter(
            Email2AI.birthdate_code == birthdate_code,
            (
                (Email2AI.primary_email == sender_email) |
                (Email2AI.secondary_email == sender_email)
            )
        ).first()

        return user

    finally:
        db.close()