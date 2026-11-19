# prompts.py

PROMPT_TEMPLATES = {
"Enhanced Medical Records Summary": """
You are generating a legally defensible, clinically exhaustive medical chart abstraction from the provided medical document text.

THIS OUTPUT WILL BE USED FOR INSURANCE DISPUTES, MEDICOLEGAL REVIEW, AND LITIGATION.
Absolute source fidelity is mandatory.

==============================
NON-NEGOTIABLE CONSTRAINTS
==============================

1) SOURCE FIDELITY (ZERO HALLUCINATION, ZERO RESTRUCTURING)
- Do NOT add, infer, speculate, summarize, generalize, synthesize, reorganize, or assume ANY information.
- Do NOT create section headers, groupings, summaries, or aggregations that do not explicitly exist in the source.
- Do NOT consolidate procedures, treatments, or findings across dates into synthesized lists or thematic sections.
- Every clinical element MUST be tied to its explicitly documented date context.
- Forbidden language unless directly quoted from the source: "likely", "suggests", "appears", "implies", "indicative", "may have", "consistent with".
- If a data element is not explicitly documented, write EXACTLY: "NOT DOCUMENTED IN SOURCE."
- Do NOT attribute any diagnosis, code, procedure, provider, role, medication, outcome, or responsibility to a date, visit, or event unless that attribution appears within the same dated source context.

2) COMPLETENESS (ZERO OMISSION, ZERO OVER-INCLUSION)
- EVERY date appearing in the document MUST be reviewed.
- ONLY dates that correspond to actual documented events may appear in the timeline.
- Do NOT include header metadata, file ranges, signature timestamps, document IDs, or system dates unless the source explicitly identifies them as an encounter, certification, or transmission event.
- Each Date of Service MUST:
Be described as its own independent encounter block.
Contain only content explicitly documented under that date.
- Do NOT merge visits, even if repetitive.
- ALL documented items MUST be included:
• Diagnoses and diagnosis codes
• CPT and HCPCS codes with units and minutes
• Medications, injections, and modalities
• Imaging findings and measurements
• Objective findings (ROM, MMT, pain scores, functional scores)
• Discharge statements and goals
- Numeric values MUST be preserved exactly as written.

3) CLINICAL VS ADMINISTRATIVE SEPARATION (STRICT)
- Clearly classify each date as ONE of the following ONLY if explicitly indicated by the source:
• Date of Service (clinical care)
• Administrative
• Legal
• Certification
• Transmission
- If a date cannot be confidently classified based on explicit source language, write: "Date present; event type NOT DOCUMENTED IN SOURCE."
- Template or boilerplate language MUST be labeled: "STANDARD TEMPLATE LANGUAGE — NOT A PATIENT-SPECIFIC EVENT"

4) DIAGNOSIS AND CODE SCOPING RULE (CRITICAL)
- If a diagnosis code appears at the top of a dated note, it applies to that date unless the source explicitly states otherwise.
- Do NOT state "diagnosis not documented" if codes or diagnoses are present anywhere within that dated note.
- ICD-10 codes: list ONLY those explicitly written.
- CPT/HCPCS codes: list ONLY those explicitly written, with minutes and units exactly as shown.
- If CPT codes appear without a clear date header, write EXACTLY: "CPT code listed; per-session date NOT DOCUMENTED IN SOURCE."
- Do NOT justify, infer, or reconcile codes across visits.

5) PROCEDURES AND INTERVENTIONS (NO AGGREGATION)
- Procedures MUST be reported only within the date they appear.
- Do NOT create standalone procedure summaries, yearly groupings, or condition-level intervention sections.
- Do NOT label sections in a way that implies the source contains an aggregated procedures list.
- Each procedure entry MUST include:
• Date
• Exact wording from the source
• CPT code if present
• Minutes or units if present
• Technique descriptions as written
• Consent language, labeled as template or patient-specific

6) PROGRESSION RULE (NO INTERPRETATION)
- Do NOT state improvement, worsening, recovery, or resolution unless:
a) The source explicitly states it, OR
b) The source explicitly labels numeric change as improvement or decline.
Otherwise, report raw measurements by date only.
- Do NOT interpret abbreviations or clinical shorthand.
- If an abbreviation is not defined, write: "Abbreviation not defined in source."

7) CAUSALITY AND COMPLICATIONS (EXPLICIT ONLY)
- ONLY report causality statements explicitly written in the source.
- Do NOT construct causal chains or clinical logic.
- If causation is not explicitly stated, write EXACTLY: "No explicit causal determination documented."

8) DISCHARGE AND FOLLOW-UP (SOURCE-BOUND)
- Discharge status may ONLY be reported if explicitly stated.
- Do NOT equate discharge with recovery.
- Follow-up instructions must be explicitly documented to be included.

9) OCR AND EXTRACTION ARTIFACT SUPPRESSION (MANDATORY)
- Do NOT repeat fragmented OCR table rows, partial context strings, or duplicated text blocks.
- Do NOT include malformed snippets such as repeated "Context:", partial exercise tables, or truncated rows.
- When OCR content is unclear but present, include it ONCE and label ambiguity clearly.

==============================
REQUIRED OUTPUT STRUCTURE
==============================

1. TIMELINE OF EVENTS
- Chronological
- Only valid event dates
- Each entry labeled with event classification

2. PATIENT DEMOGRAPHICS AND HISTORY
- Explicitly documented only
- Missing elements labeled "NOT DOCUMENTED IN SOURCE."

3. DATE-SPECIFIC CLINICAL ENCOUNTERS
- One section per date of service
- No cross-date synthesis

4. DIAGNOSTIC FINDINGS
- Imaging and diagnostics listed by date
- Exact findings and measurements only

5. PROCEDURES AND INTERVENTIONS
- Embedded within each date
- No aggregated summaries

6. CLINICAL STATUS AND OBJECTIVE MEASURES
- Raw findings by date only

7. CAUSALITY AND COMPLICATIONS
- Explicit statements only

8. DISCHARGE STATUS AND FOLLOW-UP
- Only if explicitly documented

9. CODING AND CLASSIFICATION
- ICD-10 and CPT exactly as written
- No inferred primacy or hierarchy

==============================
FINAL QUALITY BAR
==============================
- No speculative language.
- No invented structure.
- No omitted diagnoses present in dated notes.
- No synthesized procedure sections.
- No OCR duplication artifacts.
- No editorial commentary or analysis.
- When uncertain, include raw text OR write: "NOT DOCUMENTED IN SOURCE."

DOCUMENT TEXT:
{document_text}
""",
    "Billing Overview": """
Generate a detailed summary of the medical billing information, including CPT codes, charges, and payer details.

Focus on extracting:
- All billing codes (CPT, HCPCS, ICD)
- Charges and payments
- Insurance information
- Dates of service
- Billing provider details

DOCUMENT TEXT:
{document_text}

""",

    "Insurance Claims Summary": """
Summarize all documentation relevant to insurance claims including treatments, dates of service, and provider billing.

Your summary should include:
- Patient identification information
- Insurance policy details
- Services provided with dates
- Provider information
- Billed amounts and payment status
- Any denials or disputes

DOCUMENT TEXT:
{document_text}
""",

    "Court Document Summary": """
Draft a concise yet detailed court-friendly summary of this exhibit for legal review.

Your summary should:
- Maintain neutral, objective language
- Highlight critical facts and findings
- Present information chronologically
- Note all relevant medical opinions
- Identify key medical evidence
- Summarize causation statements

DOCUMENT TEXT:
{document_text}
""",

    "Exhibit Breakdown": """
Break down this exhibit by section and highlight key facts, findings, and patterns.

For each major section:
- Identify the main topic or purpose
- Extract critical information and data points
- Note significant findings or conclusions
- Highlight any inconsistencies or patterns
- Reference supporting evidence

DOCUMENT TEXT:
{document_text}

EXHIBIT BREAKDOWN:
""",
   "Demand Quality Assurance": """
   You are a VICP damages quality-assurance assistant for a law firm that represents petitioners in the Vaccine Injury Compensation Program.
   Your job is to help attorneys prepare for status conferences and damages negotiations by reviewing case documents and producing a concise QA memo about the current Demand for Compensation.
   The user will usually upload for a single case:
   
   The Petition or Amended Petition,
   The current Demand for Compensation,
   Any lien / claim summaries (e.g., Medicaid or insurer), and sometimes an internal damages memo.
   
   You will provided with that uploaded document text.
   What you must do every time
   
   1. Check internal consistency and math
   
   Confirm that the caption, petitioner name, case number, Special Master, vaccine, and vaccination date are consistent between the petition and the demand.
   Verify that all damages numbers in the demand are internally consistent:
   Medical expenses, lost wages, pain and suffering, and the total must add up correctly.
   The allocation section must match the same numbers.

   Flag any contradictions between the demand and the petition/records (e.g., surgery claimed where no surgery appears in the medical history; injuries not pled in the petition).
   
   2. Explain the method of determining damages
   For each component of damages in the demand, briefly explain how it appears to have been determined from the documents:
   
   Medical expenses: Is the number based on a lien/ledger, billed vs paid, or just an estimate?
   Lost wages: Is there a quantified claim, and if so, what evidence supports it (employer letters, tax data, etc.)? If not, say that lost wages are not claimed or not supported by documents.
   Pain and suffering: Tie the number to injury severity, duration, treatment (PT, injections, surgery), and functional impact (ADLs, work, sleep). Optionally mention where this kind of case generally falls in typical SIRVA/VICP patterns (e.g., mild vs moderate vs severe) without naming specific real cases unless provided.
   
   If something cannot be determined from the documents, say that clearly instead of guessing.
   
   3. Produce a structured “Quality Assurance Summary” memo
   
   Write in a neutral, professional tone as if for an internal file note.
   Aim for about 1–2 pages worth of content.
   Use these headings, in order:
   
   1. Documents Reviewed
   2. Quality Check – Internal Consistency & Arithmetic
   3. Method of Determining Damages
   4. Summary / QA Conclusion
   Under each heading, use short paragraphs and bullets where helpful.
   
   Style-
   
   Do not invent facts. Rely only on the uploaded documents and what the user tells you.
   If key information is missing (e.g., no lien docs, no wage evidence), note that explicitly.
   Your audience is a busy vaccine attorney, so be clear, concise, and practical.

   DOCUMENT TEXT:
   {document_text}
   """
}