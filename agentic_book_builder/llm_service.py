from openai import AsyncOpenAI
import os
from dotenv import load_dotenv
import json
from fastapi import UploadFile
from ramoslaw_chatgpt.general_chatbot import build_input_content
load_dotenv()

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

async def refine_thesis(concept_brief: str, working_title: str, author_seed: str = "", user_insights: str="", files: list[UploadFile] = []):
    
    additional_info=""
    if user_insights.strip():
        additional_info = f"User Q&A:\n{user_insights}"
    if files:
        additional_info += "\n\n Knowledge source provided:\n"
        for f in files:
            additional_info += f"- {f.filename}\n"
    
    prompt = f"""
You are helping an expert author refine a book thesis.

WORKING TITLE:
{working_title}

BOOK IDEA:
{concept_brief}

AUTHOR INTENT:
{author_seed}

{additional_info}

TASK:
- Produce a sharp, opinionated, defensible thesis statement
- Make it specific, non-generic
- Ensure it implies tension or argument
- Avoid marketing language

OUTPUT:
Return only the thesis paragraph.
"""
    print("prompt used",prompt)

    # res = await client.chat.completions.create(
    #     model="gpt-5.4-mini",
    #     messages=[
    #         {"role": "system", "content": "Expert book editor."},
    #         {"role": "user", "content": prompt}
    #     ],
    #     temperature=0.3
    # )
    # return res.choices[0].message.content

    input_content = await build_input_content(prompt, files)
    response = await client.responses.create(
        model="gpt-5.4",
        input=[{
            "role": "user",
            "content": input_content
        }],
        temperature=0.3,
        max_output_tokens=800
    )

    return response.output_text


async def generate_followup_questions(concept_brief: str, thesis: str):
    prompt = f"""
You are helping an author shape a serious nonfiction book.

CONCEPT:
{concept_brief}

THESIS:
{thesis}

Generate 5 deep diagnostic questions that explore:
- novelty
- audience clarity
- weakness in argument
- missing angles
- contradiction risks

Return JSON array of strings.
"""

    res = await client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {"role": "system", "content": "Expert editorial strategist."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3
    )

    try:
        return json.loads(res.choices[0].message.content)
    except:
        return [q.strip() for q in res.choices[0].message.content.split("\n") if q.strip()]


async def run_novelty_scan(concept: str, thesis: str):
    prompt = f"""
You are evaluating book originality.

CONCEPT:
{concept}

THESIS:
{thesis}

TASK:
- Identify similar ideas/books/themes
- Identify overlap risk (low/medium/high)
- Identify missing angle
- Suggest differentiation strategy

OUTPUT JSON:
{{
  "overlapping_domains": [],
  "adjacent_works": [],
  "derivative_risk": "",
  "uniqueness_summary": "",
  "gap_analysis":"",
  "recommendations": []
}}
"""

    res = await client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {"role": "system", "content": "Research analyst for nonfiction publishing."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.2
    )

    return json.loads(res.choices[0].message.content)


async def generate_authority_map(
    author_seed: str,
    concept: str,
    files: list[UploadFile] = []
):
    additional_info = ""

    # Add uploaded files for context
    if files:
        additional_info += "\n\nKNOWLEDGE SOURCES FILES PROVIDED:\n"
        for f in files:
            additional_info += f"- {f.filename}\n"

    prompt = f"""
You are mapping author credibility for a book.

AUTHOR:
{author_seed}

BOOK IDEA:
{concept}

{additional_info}

TASK:
- Identify strong authority zones
- Identify weak credibility zones
- Identify claims needing evidence
- Identify experience alignment gaps

OUTPUT JSON:
{{
  "why_author_credible": [],
  "direct_experience_areas": [],
  "weak_areas": [],
  "caution_flags": []
}}
"""

    input_content = await build_input_content(prompt, files)

    for item in input_content:
        if item["type"] == "text":
            item["type"] = "input_text"
        elif item["type"] == "file":
            item["type"] = "input_file"

    response = await client.responses.create(
        model="gpt-5.4",
        input=[
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Expert publishing strategist."
                    }
                ]
            },
            {
                "role": "user",
                "content": input_content
            }
        ],
        temperature=0.2
    )

    return json.loads(response.output_text)


async def rerun_novelty_scan(
    concept: str,
    thesis: str,
    previous_scan,
    user_insights: str
):
    if not user_insights or not user_insights.strip():
        raise ValueError("user_insights is required")

    previous_scan_data = {
        "overlapping_titles": getattr(previous_scan, "overlapping_titles"),
        "adjacent_books": getattr(previous_scan, "adjacent_books"),
        "gap_analysis": getattr(previous_scan, "gap_analysis"),
        "derivative_risk": getattr(previous_scan, "derivative_risk"),
        "uniqueness_summary": getattr(previous_scan, "uniqueness_summary"),
        "recommendations": getattr(previous_scan, "recommendations")
    } if previous_scan else {}

    prompt = f"""
You are reevaluating the originality and market positioning of a nonfiction book.

CONCEPT:
{concept}

THESIS:
{thesis}

PREVIOUS EXISTING NOVELTY SCAN:
{json.dumps(previous_scan_data, indent=2, default=str)}

USER REVISION REQUEST:
{user_insights}

TASK:
Revise the previous novelty scan according to the user's feedback.

Analyze:
- Similar books and adjacent works
- Overlapping domains
- Derivative risk (low / medium / high)
- Gaps in the current framing
- Differentiation opportunities
- Recommendations to increase originality

RETURN JSON ONLY:
{{
  "overlapping_domains": [],
  "adjacent_works": [],
  "derivative_risk": "",
  "uniqueness_summary": "",
  "gap_analysis": "",
  "recommendations": []
}}
"""
    res = await client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {"role": "system", "content": "Expert publishing strategist."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.2
    )

    return json.loads(res.choices[0].message.content)

async def regenerate_authority_map(
    author_seed: str,
    concept: str,
    previous_map: dict,
    user_insights: str = "",
    files: list[UploadFile] = []
):
    additional_info = ""

    if user_insights.strip():
        additional_info += f"\n\nUSER FEEDBACK:\n{user_insights}"

    if files:
        additional_info += "\n\nKNOWLEDGE SOURCES PROVIDED:\n"
        for f in files:
            additional_info += f"- {f.filename}\n"

    additional_info += f"""

PREVIOUS AUTHORITY MAP:
{json.dumps(previous_map, indent=2)}
"""

    prompt = f"""
You are refining an author's authority map for a book.

AUTHOR:
{author_seed}

BOOK IDEA:
{concept}

{additional_info}

TASK:
- Improve the previous authority map using the user's feedback.
- Incorporate any uploaded knowledge sources such as CVs, resumes,
  credentials, biographies, case studies, and research papers.
- Identify strong authority zones.
- Identify weak credibility zones.
- Identify claims that need supporting evidence.
- Identify experience alignment gaps.

OUTPUT JSON:
{{
  "why_author_credible": [],
  "direct_experience_areas": [],
  "weak_areas": [],
  "caution_flags": []
}}
"""

    print("Authority Map Regenerate Prompt:", prompt)

    # Build input content and convert for Responses API
    input_content = await build_input_content(prompt, files)

    for item in input_content:
        if item["type"] == "text":
            item["type"] = "input_text"
        elif item["type"] == "file":
            item["type"] = "input_file"

    response = await client.responses.create(
        model="gpt-5.4",
        input=[
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Expert publishing strategist."
                    }
                ]
            },
            {
                "role": "user",
                "content": input_content
            }
        ],
        temperature=0.2,
    )

    return json.loads(response.output_text)


async def generate_principles(thesis: str, concept: str):
    prompt = f"""
Create guiding principles for a nonfiction book.

THESIS:
{thesis}

CONCEPT:
{concept}

Return 5 principles that:
- constrain thinking
- guide argument
- prevent generic writing

Return Output schema:
["principle1", "principle2"]
"""

    res = await client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {"role": "system", "content": "Nonfiction structuring expert."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3
    )

    content = res.choices[0].message.content

    try:
        principles = json.loads(content)
    except Exception:
        # fallback cleanup (VERY IMPORTANT for production stability)
        content = content.strip().replace("```json", "").replace("```", "")
        principles = json.loads(content)

    return principles

async def regenerate_principle(
    thesis: str,
    concept: str,
    previous_principle: str,
    user_insights: str
):
    """
    Regenerate guiding principles based on previous principles and user feedback.
    """

    if not user_insights or not user_insights.strip():
        raise ValueError("user_insights is required")

    prompt = f"""
You are a nonfiction structuring expert.

THESIS:
{thesis}

CONCEPT:
{concept}

PREVIOUS PRINCIPLES:
{previous_principle}

USER REVISION REQUEST:
{user_insights}

TASK:
Rewrite the guiding principle based on user feedback.

RULES:
- Regenerate the existing principle based on user insights..
- Generate exactly one principle.
- Avoid generic advice.

RETURN JSON ONLY:
[
  " Revised Principle"
]
"""
    res = await client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {
                "role": "system",
                "content": "Nonfiction structuring expert."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.3
    )

    content = res.choices[0].message.content.strip()

    try:
        return json.loads(content)
    except Exception:
        content = content.replace("```json", "").replace("```", "").strip()
        return json.loads(content)


async def generate_story_bank(concept: str):
    prompt = f"""
Generate story ideas for a nonfiction book.

CONCEPT:
{concept}

Return JSON:
[
  {{
    "title": "",
    "type": "personal_story | analogy | case_study | metaphor | scene",
    "short_text": ""
  }}
]
"""

    res = await client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {"role": "system", "content": "Creative nonfiction assistant."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.5
    )

    return json.loads(res.choices[0].message.content)


async def regenerate_story_bank(
    concept: str,
    previous_story: object,   # Single StoryBankItem object
    user_insights: str
):

    if not user_insights or not user_insights.strip():
        raise ValueError("user_insights is required")

    if not previous_story:
        raise ValueError("previous_story is required")

    previous_story_data = {
        "title": previous_story.title,
        "type": previous_story.type,
        "content": previous_story.content
    }

    prompt = f"""
You are a creative nonfiction assistant.

CONCEPT:
{concept}

CURRENT STORY TO REWRITE:
{json.dumps(previous_story_data, indent=2)}

USER REVISION REQUEST:
{user_insights}

TASK:
Rewrite this story according to the user's feedback.

RULES:
- Improve clarity, emotional impact, and relevance to the concept strictly to given user feedbacks.
- change the title, type, and content if needed.
- Types allowed:
  - personal_story
  - analogy
  - case_study
  - metaphor
  - scene
- Return exactly ONE story object.

RETURN JSON ONLY:
{{
  "title": "",
  "type": "personal_story | analogy | case_study | metaphor | scene",
  "short_text": ""
}}
"""

    print("generate_story_bank_regenerate prompt:", prompt)

    res = await client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {
                "role": "system",
                "content": "Creative nonfiction assistant."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.4
    )

    content = res.choices[0].message.content.strip()

    try:
        return json.loads(content)
    except Exception:
        content = content.replace("```json", "").replace("```", "").strip()
        return json.loads(content)


async def generate_structure_options(concept: str, thesis: str):
    prompt = f"""
Suggest book structures.

CONCEPT:
{concept}

THESIS:
{thesis}

Return 2–4 structure options:
- name
- explanation
- chapter_count_estimate
- why_it_fits

OUTPUT JSON array
"""

    res = await client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {"role": "system", "content": "Book structure strategist."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.4
    )

    return json.loads(res.choices[0].message.content)

async def regenerate_structure(
    concept: str,
    thesis: str,
    previous_options: list,
    user_insights: str
):
    """
    Regenerate structure options based on previous options and user feedback.
    """

    if not user_insights or not user_insights.strip():
        raise ValueError("user_insights is required")

    previous_structure_data = previous_options

    prompt = f"""
You are a book structure strategist.

CONCEPT:
{concept}

THESIS:
{thesis}

PREVIOUS STRUCTURE OPTIONS:
{json.dumps(previous_structure_data, indent=2)}

USER REVISION REQUEST:
{user_insights}

TASK:
Regenerate improved structure options based on user feedback for existing structures.

RULES:
- Return exactly one rewritten structure option.
- Each option must include:
  - name
  - explanation
  - chapter_count_estimate
  - why_it_fits
- Incorporate the user's requested changes.

RETURN JSON ONLY:
[
  {{
    "name": "",
    "explanation": "",
    "chapter_count_estimate": 8,
    "why_it_fits": ""
  }}
]
"""

    res = await client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {
                "role": "system",
                "content": "Book structure strategist."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.1
    )

    content = res.choices[0].message.content.strip()

    try:
        return json.loads(content)
    except Exception:
        content = content.replace("```json", "").replace("```", "").strip()
        return json.loads(content)
    

async def generate_chapter_cards(structure: str, stories: list, principles: list):
    prompt = f"""
Create chapter cards. DO not create duplicates or same chapter cards multiple times.

STRUCTURE:
{structure}

STORIES:
{stories}

PRINCIPLES:
{principles}

Return JSON:
[
  {{
    "title_placeholder": "",
    "mission": "",
    "main_claim": "",
    "rough_word_target": 0,
    "linked_story_ids": [],
    "linked_principle_ids": []
  }}
]
"""
    print("generating chapter card prompt:", prompt)

    res = await client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {"role": "system", "content": "Book architect assistant."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.5
    )

    return json.loads(res.choices[0].message.content)


async def regenerate_chapter_card(
    structure: str,
    stories: list,
    principles: list,
    previous_cards: list,
    user_insights: str
):

    if not user_insights or not user_insights.strip():
        raise ValueError("user_insights is required")

    prompt = f"""
You are an expert nonfiction book architect.

Your task is to revise an existing set of chapter cards based on user feedback.

STRUCTURE:
{structure}

APPROVED STORIES:
{json.dumps(stories, indent=2)}

APPROVED PRINCIPLES:
{json.dumps(principles, indent=2)}

EXISTING CHAPTER CARD:
{json.dumps(previous_cards, indent=2)}

USER REVISION REQUEST:
{user_insights}

TASK:
- Rewrite the chapter card using the user's feedback.
- Preserve the overall structure and progression unless the user explicitly requests major changes.
- Remove redundancy.
- Keep the number of chapters approximately the same unless user requests otherwise.
- Avoid duplicate chapters.

RETURN JSON ONLY:
[
  {{
    "title_placeholder": "Chapter title",
    "mission": "What this chapter accomplishes",
    "main_claim": "The core argument of this chapter",
    "rough_word_target": 2000,
    "linked_story_ids": [],
    "linked_principle_ids": []
  }}
]
"""

    res = await client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {
                "role": "system",
                "content": "You are a world-class nonfiction book architect."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.3
    )

    content = res.choices[0].message.content.strip()

    try:
        return json.loads(content)
    except Exception:
        content = content.replace("```json", "").replace("```", "").strip()
        return json.loads(content)

async def generate_expanded_outline(chapter_card, principles, stories):
    prompt = f"""
Expand a chapter into a structured outline.

CHAPTER:
{chapter_card}

PRINCIPLES:
{principles}

STORIES:
{stories}

Return structured outline:
- intro
- key arguments
- subpoints
- story placements
- conclusion logic

1–2 pages max.
"""

    res = await client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {"role": "system", "content": "Expert nonfiction outline designer."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3
    )

    return res.choices[0].message.content

#--- regenerate ---- # 
async def regenerate_expanded_outline(
    chapter_card,
    principles,
    stories,
    user_insights: str,
    previous_outline: str
):
    if not user_insights or not user_insights.strip():
        raise ValueError("user_insights is required")

    prompt = f"""
    Your are tasked to regenerate a structured outline.

    CHAPTER:
    {chapter_card}

    PRINCIPLES:
    {principles}

    STORIES:
    {stories}

    EXISTING OUTLINE:
    {previous_outline}

    USER REVISION REQUEST:
    {user_insights}

    Revise the existing outline strictly based on the user's feedback.

    Return structured outline:
    - intro
    - key arguments
    - subpoints
    - story placements
    - conclusion logic

    1–2 pages max.
    """

    res = await client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {"role": "system", "content": "Expert nonfiction outline designer."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3
    )

    return res.choices[0].message.content
# F. DRAFTING LAYER 

async def generate_chapter_draft(chapter_title: str, outline: str, author_seed: str, principles: list = None):

    principles_text = "\n".join([f"- {p}" for p in (principles or [])])

    prompt = f"""
    You are writing a nonfiction book chapter.

    CHAPTER:
    {chapter_title}

    AUTHOR INTENT:
    {author_seed}

    PRINCIPLES:
    {principles_text}

    OUTLINE:
    {outline}

    RULES:
    - Follow outline strictly
    - Maintain narrative flow
    - Use principles as constraints
    - No generic writing
    """

    res = await client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {"role": "system", "content": "Expert nonfiction author."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3
    )

    return res.choices[0].message.content

async def regenerate_chapter_draft(chapter_title: str, outline: str, author_seed: str, principles: list = None, user_insights: str ="", previous_draft: str = ""):

    principles_text = "\n".join([f"- {p}" for p in (principles)])

    prompt = f"""
    You are writing a nonfiction book chapter. Write a fresh rewritten version with of Previous draft with given latest user suggestions.

    CHAPTER:
    {chapter_title}

    AUTHOR INTENT:
    {author_seed}

    PRINCIPLES:
    {principles_text}

    OUTLINE:
    {outline}

    Previous Draft:
    {previous_draft}

    User Required Changes and Suggestions:
    {user_insights}

    RULES:
    - Follow Users given changes strictly
    - Follow outline strictly
    - Maintain narrative flow
    - Use principles as constraints
    - No generic writing
    """

    res = await client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {"role": "system", "content": "Expert nonfiction author."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.2
    )

    return res.choices[0].message.content