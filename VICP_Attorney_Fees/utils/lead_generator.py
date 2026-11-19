import json
import re
from openai import AsyncOpenAI
from dotenv import load_dotenv
import os
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# PROMPT BUILDER
async def attorney_prompt(df):

    records = df.sort_values("date").to_dict(orient="records")

    return f"""
You are a senior attorney specializing in Vaccine Injury Compensation Program (VICP) litigation with the U.S. Court of Federal Claims.

You are given a chronological ledger of paralegal activity for a single case.

Your task:
Process the entries IN ORDER and generate lead attorney billing entries at appropriate points in the timeline.

CRITICAL BEHAVIOR:
You MUST follow the chronological order of the data
You MUST evaluate entries sequentially (top to bottom)
Each attorney entry must be based ONLY on the work that has occurred up to that point
DO NOT look ahead or summarize the entire dataset at once

CONTEXT RULE:
After a sequence of related paralegal work (typically 3–6 entries), determine if attorney involvement is warranted
If yes, generate ONE attorney entry reflecting legal work based on that recent activity
The attorney entry must logically follow the preceding paralegal work
The first entry will never be lead attorney entry.

STRICT OUTPUT RULES:
Output ONLY a valid JSON array
No explanations or commentary

Schema:
[
{{
"date": "YYYY-MM-DD",
"hours": float,
"description": "string",
"category": "string"
}}
]

ROLE OF LEAD ATTORNEY:
Include ONLY:
Intake review and evaluation
Legal analysis and strategy
Review of medical records for causation
Petition drafting oversight
Affidavit/exhibit review
Court filing preparation and review
Expert evaluation
Respondent report analysis
Docket or court order review

DO NOT INCLUDE:
Administrative work
Record requests
File organization
Scheduling
Routine client communication

HOURS GUIDELINES:
0.3-0.6 → initial review
0.6-1.2 → moderate legal analysis
1.2-2.0 → significant drafting or strategy

CATEGORY GUIDENLINES:
provide the category from below that best fits the lead attorney work leading up to the paralegal entry. choose only one catgerory per lead attorney entry. the categories are : intake_review, medical_records_review, respondent_report, case_strategy, drafting, court_filing, client_communication. 

PARALEGAL LEDGER (CHRONOLOGICAL ORDER):
{records}
"""

async def llm_lead_attorney(df):

    prompt = await attorney_prompt(df)
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[{"role": "user", "content": prompt}]
    )
    
    # print("LLM raw response:", response)
    return response.choices[0].message.content

# Post-processing LLM output
async def generate_attorney_entries(llm_output):
    try:
        match = re.search(r"\[\s*{.*}\s*\]", llm_output, re.DOTALL)
        if match:
            json_text = match.group()
        else:
            print("JSON not found")
            return []
        actions = json.loads(json_text)
    except Exception as e:
        print("JSON parse error:", e)
        return []

    billing_rows = []
    for action in actions:
        billing_rows.append({
            "date": action["date"],
            "assigned_role": "lead_attorney",
            "timekeeper": "JPR",
            "task": action["description"],
            "hours": action["hours"],
            "category": action["category"]
        })
    return billing_rows

async def generate_paralegal_entry(date, classification, hours, memos=None):
    description =  memos if isinstance(memos, str) else " | ".join(memos or [])
    return {
        "Date": date,
        "Timekeeper": "Paralegal",
        "Task Description": description,
        "Hours": hours,
        "Category": classification["primary_category"]
    }