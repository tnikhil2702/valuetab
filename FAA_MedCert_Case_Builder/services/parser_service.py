import io
from pypdf import PdfReader
from docx import Document
from pdf2image import convert_from_bytes
import pytesseract
from zipfile import BadZipFile
from PIL import Image

def ocr_page(page):
    text = page.get_text("text")

    # If text is too small → fallback to OCR
    if not text or len(text.strip()) < 50:
        pix = page.get_pixmap(dpi=200)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        text = pytesseract.image_to_string(img)

    return text.strip()


def extract_text(file_bytes: bytes, filename: str):

    filename = filename.lower()

    if filename.lower().endswith(".pdf"):
        return _extract_pdf(file_bytes)

    elif filename.lower().endswith(".docx"):
        try:
            return _extract_docx(file_bytes)
        except BadZipFile:
            raise ValueError("Invalid DOCX file. File may be corrupted or not a real .docx.")

    elif filename.lower().endswith(".doc"):
        raise ValueError("Old .doc format not supported. Please convert to .docx.")

    else:
        raise ValueError("Unsupported file format")

def _extract_pdf(file_bytes):

    reader = PdfReader(io.BytesIO(file_bytes))
    text = ""

    for page in reader.pages:
        content = page.extract_text()
        if content:
            text += content + "\n"

    # OCR fallback
    if len(text.strip()) < 100:
        images = convert_from_bytes(file_bytes)
        for img in images:
            text += pytesseract.image_to_string(img)

    return text


def _extract_docx(file_bytes):
    doc = Document(io.BytesIO(file_bytes))

    content = []

    # Extract paragraphs
    for p in doc.paragraphs:
        if p.text.strip():
            content.append(p.text.strip())

    # Extract tables
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]

            # Skip empty rows
            if any(cells):
                content.append(" | ".join(cells))

    return "\n".join(content)
