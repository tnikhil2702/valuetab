import re
import time
from datetime import datetime
from typing import Dict, List

REDACT_MARKER = "[REDACTED]"

PHI_PATTERNS = {
    "SSN": r"\b\d{3}[- ]?\d{2}[- ]?\d{4}\b",
    "Phone": r"\b(?:\+?1[-.\s]?)?\(?[\d]{3}\)?[-.\s]?[\d]{3}[-.\s]?[\d]{4}\b",
    "MRN": r"\b(?:MRN|Medical Record No\.?|Account No\.?)[:\s]*[\d-]{4,15}\b",
}

NAME_PATTERNS = [
    r"Patient\sName\s[:\-]?\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)",
    r"Name\s*[:\-]?\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)",
    r"Patient\s*[:\-]?\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)",
    # r"\b([A-Z][a-z]+,\s+[A-Z][a-z]+(?:\s[A-Z][a-z]+)*(?:\sJr\.|\sJes\.|))\b",
    r"^\s*([A-Z]{2,}(?:\s+[A-Z]{2,}){1,3})\s*,?\s*$"

]

SENSITIVE_LABELS = {
    "Patient Name", "Name", "Patient", "MRN", "Medical Record Number", "Phone", "Contact Number", "SSN", "Social Security Number", "patient_name"
}

class Redactor:
    def __init__(self):
        self.redacted_items: List[Dict] = []

    def _log_redaction(self, original: str, category: str):
        self.redacted_items.append({
            "original": original.strip(),
            "category": category,
            "timestamp": datetime.utcnow().isoformat()
        })

    def redact(self, text: str, filename: str, custom_terms: List[str] = None, only_custom: bool = False) -> Dict:
        self.redacted_items = []
        start = time.time()
        stats = {"names": 0, "phi": 0, "labels": 0, "custom": 0}

        # name patterns redaction
        for pattern in NAME_PATTERNS:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                name = match.group(1).strip()
                if len(name.split()) >= 2:  # Ensure it's a full name
                    self._log_redaction(name, "Patient Name")
                    text = text.replace(name, REDACT_MARKER)
                    stats["names"] += 1
        
        # Redact regex patterns (SSN, Phone, MRN)
        for key, pattern in PHI_PATTERNS.items():
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches:
                print(f"Found {len(matches)} matches for {key}")
                for m in matches:
                    self._log_redaction(m, key)
                text = re.sub(pattern, REDACT_MARKER, text, flags=re.IGNORECASE)
                stats["phi"] += len(matches)
        
        # Redact sensitive labels and their values
        for label in SENSITIVE_LABELS:
            # Pattern: "Label: value" or "Label - value"
            pattern = rf"({re.escape(label)}\s*[:\-]?\s*)([^\n\r.;,]+)"
            
            for match in re.finditer(pattern, text, re.IGNORECASE):
                label_part = match.group(1)  # e.g., "Patient Name: "
                value_part = match.group(2).strip()  # e.g., "John Doe"
                
                if value_part and len(value_part) > 1:  # Avoid single characters
                    self._log_redaction(value_part, f"{label}")
                    # Replace only the value, keep the label
                    text = text.replace(match.group(0), f"{label_part}{REDACT_MARKER}")
                    stats["labels"] += 1
        
        return {
            "text": text,
            "stats": stats,
            "redacted_items": self.redacted_items,
            "duration": time.time() - start
        }