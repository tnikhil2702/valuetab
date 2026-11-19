import json
import re
import math
from openai import AsyncOpenAI
import os
import pdfplumber
import docx
import pytesseract
from pdf2image import convert_from_bytes
from io import BytesIO
from dotenv import load_dotenv
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

async def generate_combined_ledger(context_text, paralegal_count, total_hours, attorney_count):
    print("Generating combined ledger (strict chronological logic)...")

    prompt = f"""
You are a senior litigation team working on Vaccine Injury Compensation Program (VICP) cases
before the U.S. Court of Federal Claims.

You must generate a SINGLE chronological billing ledger that includes BOTH:
- Paralegal entries
- Lead attorney entries

========================
GLOBAL REQUIREMENTS
========================

- Total Paralegal Entries: {paralegal_count}
- Total Lead Attorney Entries: {attorney_count}
- Total Paralegal Hours ≈ {total_hours}
- Entries must be strictly chronological
- FIRST entry MUST be paralegal
- NEVER start with attorney
- The entries must follow the given numbers: {paralegal_count} paralegal entries, {attorney_count} lead attorney entries, and total paralegal hours of approximately {total_hours}.

========================
CRITICAL SEQUENTIAL LOGIC
========================

You MUST simulate real workflow step-by-step:

1. Generate paralegal work entries in order
2. After a sequence of related paralegal work (typically 3–6 entries):
   - Decide IF attorney involvement is warranted
   - If YES → insert ONE lead attorney entry
3. Attorney entry MUST ONLY be based on work completed SO FAR
4. DO NOT look ahead
5. DO NOT summarize entire dataset
6. Each entry must have a single role (paralegal OR lead_attorney), never both

This is a rolling evaluation process.

========================
PARALEGAL RULES
========================

Allowed tasks:
- requesting medical records
- organizing exhibits
- reviewing records
- preparing exhibits
- assisting with petition drafting
- preparing filings

Paralegal hours:
- 0.2–0.4 → quick/admin support
- 0.4–0.8 → moderate work
- 0.8–1.5 → complex work

Paralegal categories (use one):
- records_request
- records_review
- exhibit_preparation
- filing_preparation
- drafting_support

Paralegal entries MUST:
- be grounded in the provided document
- reflect realistic legal workflow
- NOT be repetitive or templated

========================
LEAD ATTORNEY RULES
========================

Allowed work ONLY:
- intake review and evaluation
- legal analysis and strategy
- medical record review for causation
- petition drafting oversight
- affidavit/exhibit review
- court filing preparation/review
- expert evaluation
- respondent report analysis
- docket/court order review

DO NOT INCLUDE:
- administrative work
- record requests
- file organization
- scheduling
- routine client communication

Attorney hours:
- 0.3–0.6 → initial review
- 0.6–1.2 → moderate analysis
- 1.2–2.0 → significant legal work

Attorney categories (choose ONE):
- intake_review
- medical_records_review
- respondent_report
- case_strategy
- drafting
- court_filing
- client_communication

========================
OUTPUT FORMAT (STRICT)
========================

Return ONLY JSON array:

[
  {{
    "assigned_role": "paralegal" | "lead_attorney",
    "task": "string",
    "hours": float,
    "category": "string"
  }}
]

========================
DOCUMENT CONTEXT
========================

{context_text}
"""
    response = await client.chat.completions.create(
        model="gpt-5-mini-2025-08-07",
        # model="gpt-4o-mini",
        # temperature=0,
        messages=[{"role": "user", "content": prompt}]
    )
    # print("raw response:", response)
    content = response.choices[0].message.content

    # Extract JSON array from response
    match = re.search(r"\[[\s\S]*?\]", content)

    if not match:
        return []
    entries = json.loads(match.group())
    return entries

async def generate_full_ledger(context_text, paralegal_entries, total_hours, attorney_entries_count):

    ledger = await generate_combined_ledger(
        context_text,
        paralegal_entries,
        total_hours,
        attorney_entries_count
    )
    return {
        "raw_ledger": ledger
    }

async def extract_context(files):
    context_text = ""
    for file in files:
        file_bytes = file["bytes"]
        filename = file["name"]

        text = ""
        try:
            if filename.endswith(".pdf"):
                # Try normal extraction
                with pdfplumber.open(BytesIO(file_bytes)) as pdf:
                    for page in pdf.pages:
                        text += page.extract_text() or ""
                # OCR fallback
                if not text.strip():
                    print("OCR used:", filename)
                    images = convert_from_bytes(file_bytes)
                    for img in images:
                        text += pytesseract.image_to_string(img)

            elif filename.endswith(".docx"):
                doc = docx.Document(BytesIO(file_bytes))
                for para in doc.paragraphs:
                    text += para.text + "\n"
        except Exception as e:
            print("Extraction error:", e)
        context_text += "\n\n" + text
    return context_text.strip()

async def attorney_summary_metrices(ledger, attorney_rate=150.0):
    attorney_hours = sum(
        e["hours"] for e in ledger if e["assigned_role"] == "lead_attorney"
    )

    attorney_fees = attorney_hours * attorney_rate

    return {
        "total_attorney_hours": round(attorney_hours, 2),
        "total_attorney_fees": round(attorney_fees, 2)
    }