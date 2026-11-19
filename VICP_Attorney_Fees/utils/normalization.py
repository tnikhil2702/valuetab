import re
import json

ABBR_PATH = "VICP_Attorney_Fees/utils/abbreviations.json"

with open(ABBR_PATH) as f:
    ABBREVIATIONS = json.load(f)

async def expand_abbreviations(text):
    for abbr in sorted(ABBREVIATIONS, key=len, reverse=True):
        pattern = r"\b" + re.escape(abbr) + r"\b"
        text = re.sub(pattern, ABBREVIATIONS[abbr], text)
    return text

async def normalize_text(text):
    text = text.lower()

    # punctuation cleanup but keep /
    text = re.sub(r"[^\w\s/]", " ", text)

    # phrase expansion
    text = await expand_abbreviations(text)

    # whitespace normalization
    text = re.sub(r"\s+", " ", text)
    return text.strip()