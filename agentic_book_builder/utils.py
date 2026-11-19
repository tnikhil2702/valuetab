import pdfplumber
from docx import Document
import pytesseract
from pdf2image import convert_from_path
import tempfile
import os
import re
from collections import Counter

# provenance tracking fields
def split_into_blocks(text: str):
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    return paragraphs

def classify_provenance(paragraph: str, retrieved_chunks: list):

    if not retrieved_chunks:
        return {
            "provenance_type": "ai_external_research_synthesis",
            "source_ids": [],
            "chunk_ids": [],
            "confidence": 0.3
        }

    paragraph_l = paragraph.lower()
    paragraph_tokens = set(paragraph_l.split())

    best_score = 0
    tier1_hits = 0
    overlap_scores = []

    chunk_scores = []

    for c in retrieved_chunks:
        chunk_text = c["content"].lower()
        chunk_tokens = set(chunk_text.split())

        score = 0

        if chunk_text[:120] in paragraph_l:
            score += 2.5

        if chunk_tokens:
            overlap_ratio = len(paragraph_tokens & chunk_tokens) / len(chunk_tokens)
        else:
            overlap_ratio = 0

        score += overlap_ratio * 3.0

        # tier influence (soft, not dominating)
        if c.get("tier") == 1:
            score += 0.4
            tier1_hits += 1

        chunk_scores.append((score, c))
        overlap_scores.append(overlap_ratio)

        best_score = max(best_score, score)

    # DIVERSITY SELECTION
    chunk_scores.sort(reverse=True, key=lambda x: x[0])
    selected = [c for _, c in chunk_scores[:5]]

    source_ids = list({c["source_id"] for c in selected})
    chunk_ids = list({c["chunk_id"] for c in selected})

    avg_overlap = sum(overlap_scores) / max(len(overlap_scores), 1)

    # very low overlap: likely author-driven or hallucinated synthesis
    if avg_overlap < 0.05 and best_score < 0.6:
        return {
            "provenance_type": "author_dictated",
            "source_ids": [],
            "chunk_ids": [],
            "confidence": 0.6
        }

    # moderate overlap but weak matching: rewrite behavior
    if avg_overlap < 0.12 and best_score < 1.0:
        p_type = "ai_rewrite_of_author"

    # strong Tier 1 grounding
    elif tier1_hits > 0 and best_score > 2.0:
        p_type = "ai_synthesis_from_tier1"

    # mixed sources
    elif best_score > 1.5:
        p_type = "ai_synthesis_from_tier1_and_tier2"

    # general source summarization
    elif best_score > 0.7:
        p_type = "ai_summary_of_source"

    # fallback (very weak signal)
    else:
        p_type = "ai_external_research_synthesis"

    confidence = min(1.0, (best_score / 3.5) * (1 + avg_overlap))

    return {
        "provenance_type": p_type,
        "source_ids": source_ids,
        "chunk_ids": chunk_ids,
        "confidence": round(confidence, 3)
    }

def compute_ai_density(provenance_blocks: list):

    if not provenance_blocks:
        return 0.0

    weights = {
        "author_original": 0.0,
        "author_dictated": 0.1,
        "ai_rewrite_of_author": 0.3,
        "ai_synthesis_from_tier1": 0.55,
        "ai_synthesis_from_tier1_and_tier2": 0.7,
        "ai_summary_of_source": 0.45,
        "ai_external_research_synthesis": 0.9
    }

    total = 0.0
    count = 0

    for b in provenance_blocks:
        p_type = b.get("provenance_type", "ai_external_research_synthesis")
        total += weights.get(p_type, 0.7)
        count += 1

    if count == 0:
        return 0.0

    return round(total / count, 3)

async def tokenize(text):
    return re.findall(r"\w+", text.lower())

async def keyword_score(chunk, query_tokens):
    chunk_tokens = await tokenize(chunk)
    counter = Counter(chunk_tokens)
    return sum(counter[t] for t in query_tokens)

async def compute_chunk_score(chunk_text, query_tokens, tier):
    chunk_tokens = await tokenize(chunk_text)
    if not chunk_tokens:
        return 0

    counter = Counter(chunk_tokens)

    # keyword match
    keyword_score = sum(counter[t] for t in query_tokens)

    # density
    density = keyword_score / len(chunk_tokens)

    # tier weight
    tier_weight = 2.5 if tier == 1 else 1.5

    return (keyword_score * 1.5 + density * 40) * tier_weight

async def retrieve_relevant_chunks(db, sources, author_seed, max_chunks=10):
    query_tokens = await tokenize(author_seed)

    scored = []

    for source in sources:
        if source.source_tier not in [1, 2]:
            continue

        for chunk in source.chunks:
            score = await compute_chunk_score(
                chunk.content,
                query_tokens,
                source.source_tier
            )

            if score > 0:
                scored.append({
                    "score": score,
                    "content": chunk.content,
                    "source_id": source.id
                })

    scored.sort(key=lambda x: x["score"], reverse=True)

    # diversity control
    selected = []
    per_source = {}

    for item in scored:
        if len(selected) >= max_chunks:
            break

        sid = item["source_id"]
        per_source[sid] = per_source.get(sid, 0)

        if per_source[sid] >= 2:
            continue

        selected.append(item["content"])
        per_source[sid] += 1

    return "\n\n".join(selected)

async def extract_text(file, filename: str):
    if filename.endswith(".txt"):
        return file.file.read().decode("utf-8")

    elif filename.endswith(".pdf"):
        return extract_pdf(file)

    elif filename.endswith(".docx"):
        doc = Document(file.file)
        return "\n".join(p.text for p in doc.paragraphs)

    else:
        raise ValueError("Unsupported file type")


async def chunk_text(text: str, chunk_size: int = 500, overlap: int = 100):
    chunks = []
    start = 0
    index = 0

    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]

        chunks.append({
            "chunk_index": index,
            "content": chunk,
            "token_count": len(chunk.split())
        })

        start += chunk_size - overlap
        index += 1

    return chunks

def extract_pdf(file):
    file.file.seek(0)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(file.file.read())
        tmp_path = tmp.name

    try:
        text = ""
        with pdfplumber.open(tmp_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                text += page_text + "\n"
        # return text

        # Heuristic: if text too small, probably scanned PDF
        if len(text.strip()) < 300:
            ocr_text = extract_with_ocr(tmp_path)
            text = text + "\n" + ocr_text

        return text

    finally:
        os.remove(tmp_path)

def extract_with_ocr(path):
    """
    Converts PDF to images and runs Tesseract OCR
    """
    images = convert_from_path(path)
    ocr_text = ""

    for img in images:
        text = pytesseract.image_to_string(img)
        ocr_text += text + "\n"

    return ocr_text