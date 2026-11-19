from fastapi import APIRouter, Depends, HTTPException, Request, File, UploadFile, Body, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordRequestForm
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from typing import Optional, List
from sqlalchemy import func
import os
from dotenv import load_dotenv
from fastapi.responses import StreamingResponse
from docx import Document
from io import BytesIO
load_dotenv()
import uuid
from agentic_book_builder.llm_service import (
    refine_thesis,
    run_novelty_scan,
    generate_authority_map,
    generate_structure_options,
    generate_chapter_cards,
    generate_expanded_outline,
    generate_chapter_draft,
    generate_story_bank,
    generate_principles,
    generate_followup_questions,
    regenerate_chapter_draft,
    rerun_novelty_scan,
    regenerate_principle,
    regenerate_story_bank,
    regenerate_structure,
    regenerate_chapter_card,
    regenerate_expanded_outline,
    regenerate_authority_map
)
from db import get_db, BookProject, Draft, ProvenanceBlock, Principle, ActiveSession, Session, ApiErrorLog, OutlineVersion, ChapterCard, StructureOption, StoryBankItem, AuthorityMap, NoveltyScan, BookConcept, ConceptQA, Whitelist

from auth_utils import create_access_token, get_current_user, ACCESS_TOKEN_EXPIRE_MINUTES, reset_password, generate_otp, verify_otp, SessionLocal, create_password, authenticate_user
from admin.router import admin_router

router = APIRouter()
PROJECT_NAME="Agentic_book_builder"
jinja_templates = Jinja2Templates(directory="agentic_book_builder/templates")

@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return jinja_templates.get_template("index.html").render({"request": request})

ALLOWED_EMAIL = "drdon@ramoslaw.com"

# login and auth endpoints 
@router.post("/login")
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    
    if form_data.username.lower() != ALLOWED_EMAIL:
        raise HTTPException(
            status_code=403,
            detail="This email does not have permissions to access Agentic Book Builder Tool!"
        )

    user = await authenticate_user(
        form_data.username,
        form_data.password
    )

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password"
        )

    access_token = await create_access_token(
        data={
            "sub": user["email"],
            "role": user["role"]
        },
        expires_delta=timedelta(
            minutes=ACCESS_TOKEN_EXPIRE_MINUTES
        )
    )

    active = db.query(ActiveSession).filter(
        ActiveSession.project_name == PROJECT_NAME,
        ActiveSession.user_email == user["email"]
    ).first()

    if active:
        active.last_activity_timestamp = datetime.utcnow()
    else:
        db.add(
            ActiveSession(
                project_name=PROJECT_NAME,
                user_email=user["email"],
                last_activity_timestamp=datetime.utcnow()
            )
        )

    db.commit()
    return {
        "access_token": access_token,
        "token_type": "bearer"
    }

@router.get("/verify-email")
async def verify_email(email: str):

    if email.lower() != ALLOWED_EMAIL:
        raise HTTPException(
            status_code=403,
            detail="Email not authorized!"
        )

    db = SessionLocal()

    try:

        user = db.query(Whitelist).filter(
            Whitelist.email == email.lower()
        ).first()

        if not user:
            raise HTTPException(
                status_code=404,
                detail="Email not authorized"
            )

        return {
            "valid": True,
            "password_created": bool(user.password_hash),
            "role": user.role
        }

    finally:
        db.close()

@router.post("/send/otp")
async def send_otp_api(email: str):
    return await generate_otp(email)

@router.post("/verify/otp")
async def verify_otp_api(
    email: str,
    otp: str
):
    await verify_otp(email, otp)
    return {
        "message": "OTP verified successfully"
    }

@router.post("/create/password")
async def create_password_api(
    email: str,
    password: str
):
    return await create_password(
        email,
        password
    )

@router.post("/forgot/password")
async def forgot_password(email: str):
    return await generate_otp(email)

@router.post("/reset/password")
async def reset_password_api(
    email: str,
    otp: str,
    new_password: str
):
    return await reset_password(
        email,
        otp,
        new_password
    )

@router.get("/active-today")
async def active_today(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    today = datetime.utcnow().date()
    active = db.query(ActiveSession.user_email, ActiveSession.last_activity_timestamp).filter(
        ActiveSession.project_name == PROJECT_NAME,
        func.date(ActiveSession.last_activity_timestamp) == today
    ).all()
    return [{"email": email, "last_active": ts.isoformat()} for email, ts in active]

# New Endpoints
@router.post("/concept")
async def create_or_update_concept(data: dict, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    project = BookProject(
        id=str(uuid.uuid4()),
        name=data["working_title"],  
        created_by=current_user["email"]
    )
    db.add(project)
    db.flush()

    concept = BookConcept(
        id=str(uuid.uuid4()),
        project_id=project.id,
        working_title=data.get("working_title"),
        concept_brief=data.get("concept_brief"),
        original_thesis=data.get("rough_thesis")
    )

    db.add(concept)
    db.commit()

    return {
        "project_id": project.id,
        "concept_id": concept.id
    }

@router.get("/concepts")
async def list_concepts(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(BookConcept).all()

@router.get("/concept/{project_id}")
async def get_concept(project_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    concept = db.query(BookConcept).filter(
        BookConcept.project_id == project_id
    ).first()

    if not concept:
        raise HTTPException(status_code=404, detail="Concept not found")

    return concept

@router.post("/concept/{project_id}/refine-thesis")
async def generate_refine_thesis(project_id: str, files: Optional[List[UploadFile]] = File(None), db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    concept = db.query(BookConcept).filter(
        BookConcept.project_id == project_id
    ).first()

    if not concept:
        raise HTTPException(status_code=404, detail="Concept not found")
    
    qa_pairs = db.query(ConceptQA).filter(
        ConceptQA.project_id == project_id,
        ConceptQA.is_answered == True
    ).order_by(ConceptQA.created_at.desc()).limit(5).all()

    context_answers = "\n".join([
        f"Q: {q.question}\nA: {q.answer}"
        for q in qa_pairs
    ]) if qa_pairs else ""

    author_seed = concept.original_thesis 
    print("Author seed:", author_seed)

    refined = await refine_thesis(
    concept_brief=concept.concept_brief,
    working_title=concept.working_title,
    author_seed=author_seed,
    user_insights=context_answers,
    files=files or []
    )

    concept.refined_thesis = refined
    concept.status = "refined"

    db.commit()

    return {"refined_thesis": refined}

@router.get("/concept/{project_id}/refine-thesis")
async def get_refined_thesis(project_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    concept = db.query(BookConcept).filter(
        BookConcept.project_id == project_id
    ).first()
    if not concept:
        raise HTTPException(status_code=404, detail="Concept not found")
    thesis = concept.refined_thesis or concept.original_thesis

    return {
        "refined_thesis": thesis
    }

@router.post("/concept/{project_id}/lock")
async def lock_concept(project_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    concept = db.query(BookConcept).filter(
        BookConcept.project_id == project_id
    ).first()

    concept.status = "locked"

    db.commit()

    return {"status": "locked"}


@router.post("/research/{project_id}/novelty-scan")
async def novelty_scan(project_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    concept = db.query(BookConcept).filter(
        BookConcept.project_id == project_id
    ).first()

    if not concept:
        raise HTTPException(status_code=404, detail="Concept not found")

    scan_data = await run_novelty_scan(
        concept=concept.concept_brief,
        thesis=concept.refined_thesis
    )
    print("Scan data:", scan_data)

    scan = NoveltyScan(
        id=str(uuid.uuid4()),
        project_id=project_id,
        status="generated",
        query_context=concept.concept_brief,
        overlapping_titles=scan_data.get("adjacent_works", []),
        adjacent_books=scan_data.get("overlapping_domains", []),
        gap_analysis=scan_data.get("gap_analysis", ""),
        derivative_risk=scan_data.get("derivative_risk", ""), 
        uniqueness_summary=scan_data.get("uniqueness_summary", ""),
        recommendations=scan_data.get("recommendations", "")
    )

    db.add(scan)
    db.commit()
    # db.refresh()
    return {"status": "generated", "scan_data": scan_data}

@router.post("/research/novelty-scan/{novelty_id}/regenerate")
async def regenerate_novelty_scan(
    novelty_id: str,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    user_insights = data.get("user_insights", "").strip()

    if not user_insights:
        raise HTTPException(status_code=400, detail="user_insights is required")

    # exact scan directly
    previous_scan = db.query(NoveltyScan).filter(
        NoveltyScan.id == novelty_id
    ).first()

    if not previous_scan:
        raise HTTPException(status_code=404, detail="Novelty scan not found")

    # need concept
    concept = db.query(BookConcept).filter(
        BookConcept.project_id == previous_scan.project_id
    ).first()

    if not concept:
        raise HTTPException(status_code=404, detail="Concept not found")

    scan_data = await rerun_novelty_scan(
        concept=concept.concept_brief,
        thesis=concept.refined_thesis,
        previous_scan=previous_scan,
        user_insights=user_insights
    )

    new_scan = NoveltyScan(
        id=str(uuid.uuid4()),
        project_id=previous_scan.project_id,
        status="regenerated",
        query_context=concept.concept_brief,
        overlapping_titles=scan_data.get("adjacent_works", []),
        adjacent_books=scan_data.get("overlapping_domains", []),
        gap_analysis=scan_data.get("gap_analysis", ""),
        derivative_risk=scan_data.get("derivative_risk", ""),
        uniqueness_summary=scan_data.get("uniqueness_summary", ""),
        recommendations=scan_data.get("recommendations", "")
    )

    db.add(new_scan)
    db.commit()

    return {"status": "regenerated","scan_data": scan_data}

@router.get("/research/{project_id}/novelty-scan")
def get_novelty_scan(project_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    scan = db.query(NoveltyScan).filter(
        NoveltyScan.project_id == project_id
    ).order_by(NoveltyScan.created_at.desc()).all()

    if not scan:
        raise HTTPException(status_code=404)

    return scan

@router.post("/research/{project_id}/authority-map")
async def create_authority_map(project_id: str, files: Optional[List[UploadFile]] = File(None), db: Session = Depends(get_db),current_user: dict = Depends(get_current_user)):

    concept = db.query(BookConcept).filter(
        BookConcept.project_id == project_id
    ).first()

    if not concept:
        raise HTTPException(status_code=404, detail="Concept not found")

    map_data = await generate_authority_map(
        author_seed=concept.refined_thesis,
        concept=concept.concept_brief,
        files=files or []
    )

    map_obj = AuthorityMap(
        id=str(uuid.uuid4()),
        project_id=project_id,
        status="generated",
        why_author_credible=str(map_data.get("why_author_credible", "")),
        experience_areas=map_data.get("direct_experience_areas", []),
        weak_areas=map_data.get("weak_areas", []),
        caution_flags=str(map_data.get("caution_flags", ""))
    )

    db.add(map_obj)
    db.commit()

    return {"status":"generated", "map_data":map_data}

@router.post("/research/authority-map/{authority_map_id}/regenerate")
async def regenerate_authority_maps(
    authority_map_id: str,
    user_insights: str = Form(...),
    files: Optional[List[UploadFile]] = File(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    # Fetch the existing authority map
    previous_map_obj = db.query(AuthorityMap).filter(
        AuthorityMap.id == authority_map_id
    ).first()

    if not previous_map_obj:
        raise HTTPException(
            status_code=404,
            detail="Authority map not found"
        )

    # Fetch the related book concept
    concept = db.query(BookConcept).filter(
        BookConcept.project_id == previous_map_obj.project_id
    ).first()

    if not concept:
        raise HTTPException(
            status_code=404,
            detail="Concept not found"
        )

    # Convert previous authority map into dict for the LLM
    previous_map = {
        "why_author_credible": previous_map_obj.why_author_credible,
        "direct_experience_areas": previous_map_obj.experience_areas or [],
        "weak_areas": previous_map_obj.weak_areas or [],
        "caution_flags": previous_map_obj.caution_flags,
    }

    # Generate regenerated authority map
    map_data = await regenerate_authority_map(
        author_seed=concept.refined_thesis,
        concept=concept.concept_brief,
        previous_map=previous_map,
        user_insights=user_insights,
        files=files or []
    )

    # Create a new authority map record (preserves history)
    new_map = AuthorityMap(
        id=str(uuid.uuid4()),
        project_id=previous_map_obj.project_id,
        status="regenerated",
        why_author_credible=str(
            map_data.get("why_author_credible", "")
        ),
        experience_areas=map_data.get(
            "direct_experience_areas", []
        ),
        weak_areas=map_data.get(
            "weak_areas", []
        ),
        caution_flags=str(
            map_data.get("caution_flags", "")
        )
    )

    db.add(new_map)
    db.commit()
    db.refresh(new_map)

    return {
        "status": "regenerated",
        "authority_map": map_data
    }

@router.get("/research/{project_id}/authority-map")
async def list_authority_maps(project_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(AuthorityMap).filter(
        AuthorityMap.project_id == project_id
    ).order_by(AuthorityMap.created_at.desc()).all()

# principles + story bank
@router.post("/principles/{project_id}/generate")
async def generate_principles_endpoint(project_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    
    concept = db.query(BookConcept).filter(
        BookConcept.project_id == project_id
    ).first()

    if not concept:
        raise HTTPException(status_code=404, detail="Concept not found")

    principles = await generate_principles(
        thesis=concept.refined_thesis,
        concept=concept.concept_brief
    )

    objs = []
    for p in principles:
        objs.append(
        Principle(
            id=str(uuid.uuid4()),
            project_id=project_id,
            text=p,
            status="generated"
        )
    )

    db.add_all(objs)
    db.commit()

    return {"status":"generated", "principles": principles} 

@router.post("/principles/{principle_id}/regenerate")
async def regenerate_principles(
    principle_id: str,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    user_insights = data.get("user_insights", "").strip()

    if not user_insights:
        raise HTTPException(status_code=400, detail="user_insights is required")

    # Fetch one principle first
    base_principle = db.query(Principle).filter(
        Principle.id == principle_id
    ).first()

    if not base_principle:
        raise HTTPException(status_code=404, detail="Principle not found")

    concept = db.query(BookConcept).filter(
        BookConcept.project_id == base_principle.project_id
    ).first()

    if not concept:
        raise HTTPException(status_code=404, detail="Concept not found")

    principles = await regenerate_principle(
        thesis=concept.refined_thesis,
        concept=concept.concept_brief,
        previous_principle=base_principle.text,
        user_insights=user_insights
    )

    db.query(Principle).filter(
        Principle.project_id == base_principle.project_id
    ).delete()

    objs = [
        Principle(
            id=str(uuid.uuid4()),
            project_id=base_principle.project_id,
            text=p,
            status="regenerated"
        )
        for p in principles
    ]

    db.add_all(objs)
    db.commit()

    return {"status":"regenerated", "principle": principles}


@router.get("/principles/{project_id}")
async def list_principles(project_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(Principle).filter(
        Principle.project_id == project_id
    ).all()

@router.post("/principles/{principle_id}/approve")
async def approve_principle(
    principle_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    principle = db.query(Principle).filter(
        Principle.id == principle_id
    ).first()

    if not principle:
        raise HTTPException(status_code=404)

    db.query(Principle).filter(
        Principle.project_id == principle.project_id,
        Principle.id != principle_id
    ).update({"status": "generated"}) 

    principle.status = "approved"

    db.query(Principle).filter(
        Principle.project_id == principle.project_id,
        Principle.id != principle_id
    ).delete()

    db.commit()

    return {"status": "approved", "id": principle_id}

@router.post("/stories/{project_id}/generate")
async def generate_story_bank_endpoint(project_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    concept = db.query(BookConcept).filter(
        BookConcept.project_id == project_id
    ).first()

    if not concept:
        raise HTTPException(status_code=404)

    stories = await generate_story_bank(
        concept=concept.concept_brief
    )

    objs = []
    for s in stories:
        obj = StoryBankItem(
            id=str(uuid.uuid4()),
            project_id=project_id,
            status="generated",
            title=s["title"],
            type=s["type"],
            content=s.get("short_text", ""),
            approved=False
        )
        objs.append(obj)

    db.add_all(objs)
    db.commit()

    return {"status":"generated", "stories": stories} 

@router.post("/stories/{story_id}/regenerate")
async def regenerate_single_story(
    story_id: str,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
   ):
    """
    Regenerate only one specific story instead of the entire story bank.

    Request Body Example:
    {
        "user_insights": "Make this story more emotional and add a stronger lesson."
    }
    """
    # 1. Validate user feedback
    user_insights = data.get("user_insights", "").strip()

    if not user_insights:
        raise HTTPException(
            status_code=400,
            detail="user_insights is required"
        )

    # 2. Get the existing story
    story = db.query(StoryBankItem).filter(
        StoryBankItem.id == story_id
    ).first()

    if not story:
        raise HTTPException(
            status_code=404,
            detail="Story not found"
        )

    # 3. Get related concept
    concept = db.query(BookConcept).filter(
        BookConcept.project_id == story.project_id
    ).first()

    if not concept:
        raise HTTPException(
            status_code=404,
            detail="Concept not found"
        )

    regenerated_story = await regenerate_story_bank(
            concept=concept.concept_brief,
            previous_story=story,       
            user_insights=user_insights
        )

    if isinstance(regenerated_story, list):
        if not regenerated_story:
            raise HTTPException(
                status_code=500,
                detail="No regenerated story returned"
            )
        regenerated_story = regenerated_story[0]

    story.title = regenerated_story.get("title", story.title)
    story.type = regenerated_story.get("type", story.type)
    story.content = regenerated_story.get(
        "short_text",
        regenerated_story.get("content", story.content)
    )
    story.status = "regenerated"
    story.approved = False

    db.commit()
    db.refresh(story)

    return {
        "status": "regenerated",
        "story": {
            "id": story.id,
            "project_id": story.project_id,
            "title": story.title,
            "type": story.type,
            "content": story.content,
            "approved": story.approved
        }
    }


@router.get("/stories/{project_id}")
async def list_stories(project_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    return db.query(StoryBankItem).filter(
        StoryBankItem.project_id == project_id
    ).all()

@router.post("/stories/{story_id}/approve")
async def approve_story(
    story_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    story = db.query(StoryBankItem).filter(
        StoryBankItem.id == story_id
    ).first()

    if not story:
        raise HTTPException(status_code=404) 
    
    db.query(StoryBankItem).filter(
        StoryBankItem.project_id == story.project_id,
        StoryBankItem.id != story_id
    ).update({"approved": False})

    story.approved = True

    db.query(StoryBankItem).filter(
        StoryBankItem.project_id == story.project_id,
        StoryBankItem.id != story_id
    ).delete()

    db.commit()

    return {"status": "approved", "id": story_id}

@router.put("/stories/{story_id}")
async def update_story(
    story_id: str,
    title: str | None = Body(default=None),
    content: str | None = Body(default=None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    story = db.query(StoryBankItem).filter(
        StoryBankItem.id == story_id
    ).first()

    if not story:
        raise HTTPException(status_code=404, detail="Story not found")

    if not story.approved:
        raise HTTPException(status_code=400, detail="Only approved story can be edited")

    # Update fields if provided
    if title is not None:
        story.title = title

    if content is not None:
        story.content = content

    db.commit()
    db.refresh(story)

    return {
        "status": "updated",
        "id": story.id,
        "title": story.title,
        "content": story.content
    }

# structure layer 
@router.post("/structure/{project_id}/generate")
async def generate_structure(project_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    concept = db.query(BookConcept).filter(
        BookConcept.project_id == project_id
    ).first()

    if not concept:
        raise HTTPException(status_code=404)

    options = await generate_structure_options(
        concept=concept.concept_brief,
        thesis=concept.refined_thesis
    )

    db_options = []
    for o in options:
        obj = StructureOption(
            id=str(uuid.uuid4()),
            project_id=project_id,
            status="generated",
            option_name=o["name"],
            option_summary=o["explanation"],
            chapter_count_estimate=o.get("chapter_count_estimate", 8),
            why_this_structure_fits=o.get("why_it_fits", ""),
            recommended=False
        )
        db_options.append(obj)

    db.add_all(db_options)
    db.commit()

    return {"status":"generated","options":options}

@router.post("/structure/{structure_id}/regenerate")
async def regenerate_structures(
    structure_id: str,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    user_insights = data.get("user_insights", "").strip()

    if not user_insights:
        raise HTTPException(status_code=400, detail="user_insights is required")

    base_option = db.query(StructureOption).filter(
        StructureOption.id == structure_id
    ).first()

    if not base_option:
        raise HTTPException(status_code=404, detail="Structure option not found")

    concept = db.query(BookConcept).filter(
        BookConcept.project_id == base_option.project_id
    ).first()

    if not concept:
        raise HTTPException(status_code=404, detail="Concept not found")

    options = await regenerate_structure(
        concept=concept.concept_brief,
        thesis=concept.refined_thesis,
        previous_options=
            {
                "name": base_option.option_name,
                "explanation": base_option.option_summary,
                "chapter_count_estimate": base_option.chapter_count_estimate,
                "why_it_fits": base_option.why_this_structure_fits
            },
        user_insights=user_insights
    )

    db.query(StructureOption).filter(
        StructureOption.project_id == base_option.project_id
    ).delete()

    objs = [
        StructureOption(
            id=str(uuid.uuid4()),
            project_id=base_option.project_id,
            status="regenerated",
            option_name=o["name"],
            option_summary=o["explanation"],
            chapter_count_estimate=o.get("chapter_count_estimate", 8),
            why_this_structure_fits=o.get("why_it_fits", ""),
            recommended=False
        )
        for o in options
    ]

    db.add_all(objs)
    db.commit()

    return {"status":"regenerated","structure_options": options}

@router.get("/structure/{project_id}")
async def list_structure_options(project_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(StructureOption).filter(
        StructureOption.project_id == project_id
    ).all()

@router.post("/structure/{project_id}/select/{structure_id}")
async def select_structure(project_id: str, structure_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    options = db.query(StructureOption).filter(
        StructureOption.project_id == project_id
    ).all()
    
    selected = None

    for o in options:
        if o.id == structure_id:
            o.recommended = True
            selected = o
        else:
            db.delete(o) 
    
    if not selected:
        raise HTTPException(status_code=404, detail="Structure not found")

    db.commit()

    return {"selected": structure_id}

@router.post("/concept/{project_id}/questions")
async def generate_questions_endpoint(project_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    concept = db.query(BookConcept).filter(
        BookConcept.project_id == project_id
    ).first()

    if not concept:
        raise HTTPException(status_code=404) 
    
    db.query(ConceptQA).delete(synchronize_session=False)

    questions = await generate_followup_questions(
        concept_brief=concept.concept_brief,
        thesis=concept.refined_thesis
    )

    print("questions:", questions)

    saved = []

    for q in questions:
        obj = ConceptQA(
            id=str(uuid.uuid4()),
            project_id=project_id,
            question=q,
            is_answered=False
        )
        saved.append(obj)

    db.add_all(saved)
    db.commit()

    return [
        {
            "id": s.id,
            "question": s.question,
            "is_answered": s.is_answered
        }
        for s in saved
    ]

@router.get("/concept/{project_id}/questions")
async def get_questions(
    project_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    questions = db.query(ConceptQA).filter(
        ConceptQA.project_id == project_id
    ).order_by(ConceptQA.created_at).all()

    return [
        {
            "id": q.id,
            "question": q.question,
            "answer": q.answer,
            "is_answered": q.is_answered,
            "answered_at": q.answered_at.isoformat() if q.answered_at else None
        }
        for q in questions
    ]

@router.post("/concept/{project_id}/answer/{question_id}")
async def answer_question(
    project_id: str,
    question_id: str,
    data: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    qa = db.query(ConceptQA).filter(
        ConceptQA.project_id == project_id,
        ConceptQA.id == question_id
    ).first()

    if not qa:
        raise HTTPException(status_code=404, detail="Question not found")

    qa.answer = data.get("answer")
    qa.is_answered = True
    qa.answered_at = datetime.utcnow()

    db.commit()

    return {
        "status": "answered",
        "question_id": question_id
    }


@router.post("/concept/{project_id}/answers/bulk")
async def bulk_answer_questions(
    project_id: str,
    data: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    answers = data.get("answers")

    if not answers or not isinstance(answers, list):
        raise HTTPException(status_code=400, detail="Invalid or missing 'answers' list")

    # Extract valid question_ids
    question_ids = [
        item.get("question_id")
        for item in answers
        if isinstance(item, dict) and item.get("question_id")
    ]

    if not question_ids:
        raise HTTPException(status_code=400, detail="No valid question_ids provided")

    # Fetch all matching questions in one query
    qas = db.query(ConceptQA).filter(
        ConceptQA.project_id == project_id,
        ConceptQA.id.in_(question_ids)
    ).all()

    qa_map = {qa.id: qa for qa in qas}

    updated = []
    missing = []

    for item in answers:
        if not isinstance(item, dict):
            continue

        qid = item.get("question_id")
        answer = item.get("answer")

        if not qid or qid not in qa_map:
            missing.append(qid)
            continue

        qa = qa_map[qid]
        qa.answer = answer
        qa.is_answered = True
        qa.answered_at = datetime.utcnow()

        updated.append(qid)

    db.commit()

    return {
        "status": "completed",
        "updated_count": len(updated),
        "updated_ids": updated,
        "missing_ids": missing
    }

# Chapter card system 
@router.post("/structure/{project_id}/chapter-cards")
async def create_chapter_cards(project_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    
    existing_cards = db.query(ChapterCard).filter(
        ChapterCard.project_id == project_id
    ).order_by(
        ChapterCard.order_index
    ).all()

    # If cards already exist, return them instead of generating duplicates
    if existing_cards:
        return {
            "already_exists": True,
            "chapter_cards": existing_cards
        }

    concept = db.query(BookConcept).filter(
        BookConcept.project_id == project_id
    ).first()

    structure = db.query(StructureOption).filter(
        StructureOption.project_id == project_id,
        StructureOption.recommended == True
    ).first()

    if not structure:
        raise HTTPException(status_code=400, detail="No structure selected")

    principles = db.query(Principle).filter(
        Principle.project_id == project_id,
        Principle.status == "approved"
    ).all()

    stories = db.query(StoryBankItem).filter(
        StoryBankItem.project_id == project_id,
        StoryBankItem.approved == True
    ).all()

    cards_data = await generate_chapter_cards(
        structure=structure.option_summary,
        principles=[
            {
                "text": p.text,
            } for p in principles
        ],
        stories=[
            {
                "title": s.title,
                "content": s.content,
                "type": s.type
            } for s in stories
        ]
    )

    cards = []

    for i, c in enumerate(cards_data):
        card = ChapterCard(
            id=str(uuid.uuid4()),
            project_id=project_id,
            status="generated",
            title_placeholder=c.get("title_placeholder"),
            mission=c.get("mission"),
            main_claim=c.get("main_claim"),
            rough_word_target=c.get("rough_word_target", 2000),
            linked_story_ids=c.get("linked_story_ids", []),
            linked_principle_ids=c.get("linked_principle_ids", []),
            order_index=i + 1,
            approved=False
        )
        cards.append(card)

    db.add_all(cards)
    db.commit()

    return {"status":"generated","cards_data":cards_data}

@router.post("/structure/{chapter_card_id}/chapter-cards/regenerate")
async def regenerate_chapter_cards(
    chapter_card_id: str,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    user_insights = data.get("user_insights", "").strip()

    if not user_insights:
        raise HTTPException(status_code=400, detail="user_insights is required")

    base_card = db.query(ChapterCard).filter(
        ChapterCard.id == chapter_card_id
    ).first()

    if not base_card:
        raise HTTPException(status_code=404, detail="Chapter card not found")

    project_id = base_card.project_id

    concept = db.query(BookConcept).filter(
        BookConcept.project_id == project_id
    ).first()

    structure = db.query(StructureOption).filter(
        StructureOption.project_id == project_id,
        StructureOption.recommended == True
    ).first()

    if not structure:
        raise HTTPException(status_code=400, detail="No structure selected")

    principles = db.query(Principle).filter(
        Principle.project_id == project_id,
        Principle.status == "approved"
    ).all()

    stories = db.query(StoryBankItem).filter(
        StoryBankItem.project_id == project_id,
        StoryBankItem.approved == True
    ).all()

    existing_cards = db.query(ChapterCard).filter(
        ChapterCard.project_id == project_id
    ).order_by(
        ChapterCard.order_index
    ).all()

    previous_cards = [
        {
            "title_placeholder": c.title_placeholder,
            "mission": c.mission,
            "main_claim": c.main_claim,
            "rough_word_target": c.rough_word_target,
            "order_index": c.order_index
        }
        for c in existing_cards
    ]

    cards_data = await regenerate_chapter_card(
        structure=structure.option_summary,
        principles=[{"text": p.text} for p in principles],
        stories=[{
            "title": s.title,
            "content": s.content,
            "type": s.type
        } for s in stories],
        previous_cards=previous_cards,
        user_insights=user_insights
    )

    db.query(ChapterCard).filter(
        ChapterCard.project_id == project_id
    ).delete()

    for card in existing_cards:
        db.query(OutlineVersion).filter(
            OutlineVersion.chapter_id == card.id
        ).delete()

    new_cards = []

    for i, c in enumerate(cards_data):
        new_cards.append(
            ChapterCard(
                id=str(uuid.uuid4()),
                project_id=project_id,
                status="regenerated",
                title_placeholder=c.get("title_placeholder"),
                mission=c.get("mission"),
                main_claim=c.get("main_claim"),
                rough_word_target=c.get("rough_word_target", 2000),
                linked_story_ids=c.get("linked_story_ids", []),
                linked_principle_ids=c.get("linked_principle_ids", []),
                order_index=i + 1,
                approved=False
            )
        )

    db.add_all(new_cards)
    db.commit()

    for c in new_cards:
        db.refresh(c)

    return {
        "status": "regenerated",
        "user_insights": user_insights,
        "chapter_cards": [
            {
                "id": c.id,
                "title_placeholder": c.title_placeholder,
                "mission": c.mission,
                "main_claim": c.main_claim,
                "rough_word_target": c.rough_word_target,
                "order_index": c.order_index,
                "approved": c.approved
            }
            for c in new_cards
        ]
    }

@router.get("/chapters/{project_id}/cards")
async def get_chapter_cards(project_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    return db.query(ChapterCard).filter(
        ChapterCard.project_id == project_id
    ).order_by(ChapterCard.order_index).all()

@router.post("/chapter-card/{card_id}/approve")
async def approve_card(
    card_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    card = db.query(ChapterCard).filter(
        ChapterCard.id == card_id
    ).first()

    if not card:
        raise HTTPException(status_code=404) 
    
    db.query(ChapterCard).filter(
        ChapterCard.project_id == card.project_id,
        ChapterCard.id != card_id
    ).update({"approved": False})

    card.approved = True

    db.commit()

    return {"status": "approved", "id": card_id}

@router.put("/chapter-card/{card_id}")
async def update_chapter_card(
    card_id: str,
    data: dict = Body(default={}),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Update only the chapter title and summary.

    Request Body Example:
    {
        "title_placeholder": "The New Chapter Title",
        "mission": "Updated chapter summary or description."
    }
    """

    card = db.query(ChapterCard).filter(
        ChapterCard.id == card_id
    ).first()

    if not card:
        raise HTTPException(
            status_code=404,
            detail="Chapter card not found"
        )

    # Update chapter name
    if "title_placeholder" in data and data["title_placeholder"] is not None:
        card.title_placeholder = data["title_placeholder"]

    # Update chapter summary/description
    if "mission" in data and data["mission"] is not None:
        card.mission = data["mission"]

    db.commit()
    db.refresh(card)

    return {
        "status": "updated",
        "id": card.id,
        "title_placeholder": card.title_placeholder,
        "mission": card.mission,
        "approved": card.approved
    }

@router.delete("/chapter-card/{card_id}")
async def delete_chapter_card(
    card_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    card = db.query(ChapterCard).filter(
        ChapterCard.id == card_id
    ).first()

    if not card:
        raise HTTPException(
            status_code=404,
            detail="Chapter card not found"
        )

    db.delete(card)
    db.commit()

    return {
        "status": "deleted",
        "id": card_id,
        "message": "Chapter card deleted successfully"
    }

@router.post("/chapter-card/{card_id}/outline")
async def generate_outline(card_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    card = db.query(ChapterCard).filter(ChapterCard.id == card_id).first()

    if not card:
        raise HTTPException(status_code=404, detail="Chapter card not found")

    principles = db.query(Principle).filter(
        Principle.project_id == card.project_id,
        Principle.status == "approved"
    ).all()

    stories = db.query(StoryBankItem).filter(
        StoryBankItem.project_id == card.project_id,
        StoryBankItem.approved == True
    ).all()

    outline_text = await generate_expanded_outline(
        chapter_card={
            "title": card.title_placeholder,
            "mission": card.mission,
            "claim": card.main_claim
        },
        principles=[
            {
                "text": p.text,
            } for p in principles
        ],
        stories=[
            {
                "title": s.title,
                "content": s.content,
                "type": s.type
            } for s in stories
        ]
    )

    outline = OutlineVersion(
        id=str(uuid.uuid4()),
        chapter_id=card_id,
        status="generated",
        version_number=1,
        expanded_outline_text=outline_text,
        approved=False
    )

    db.add(outline)
    db.commit()

    return {"status":"generated", "outline": outline_text} 

@router.post("/outline/{outline_id}/regenerate")
async def regenerate_outline(
    outline_id: str,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):

    user_insights = data.get("user_insights", "").strip()

    if not user_insights:
        raise HTTPException(
            status_code=400,
            detail="user_insights is required"
        )

    # Get the current outline
    outline = db.query(OutlineVersion).filter(
        OutlineVersion.id == outline_id
    ).first()

    if not outline:
        raise HTTPException(
            status_code=404,
            detail="Outline not found"
        )

    # Get the related chapter card
    card = db.query(ChapterCard).filter(
        ChapterCard.id == outline.chapter_id
    ).first()

    if not card:
        raise HTTPException(
            status_code=404,
            detail="Chapter card not found"
        )

    # Get approved principles
    principles = db.query(Principle).filter(
        Principle.project_id == card.project_id,
        Principle.status == "approved"
    ).all()

    # Get approved stories
    stories = db.query(StoryBankItem).filter(
        StoryBankItem.project_id == card.project_id,
        StoryBankItem.approved == True
    ).all()

    regenerated_outline = await regenerate_expanded_outline(
        chapter_card={
            "title": card.title_placeholder,
            "mission": card.mission,
            "claim": card.main_claim
        },
        principles=[
            {
                "text": p.text,
            } for p in principles
        ],
        stories=[
            {
                "title": s.title,
                "content": s.content,
                "type": s.type
            } for s in stories
        ],
        user_insights=user_insights,
        previous_outline=outline.expanded_outline_text
    )

    # Calculate next version number
    last_version = db.query(
        func.max(OutlineVersion.version_number)
    ).filter(
        OutlineVersion.chapter_id == outline.chapter_id
    ).scalar()

    next_version = (last_version or 0) + 1

    # Save new outline version
    new_outline = OutlineVersion(
        id=str(uuid.uuid4()),
        chapter_id=outline.chapter_id,
        status="regenerated",
        version_number=next_version,
        expanded_outline_text=regenerated_outline,
        approved=False
    )

    db.add(new_outline)
    db.commit()
    db.refresh(new_outline)

    return {
        "status": "regenerated",
        "outline_id": new_outline.id,
        "version": new_outline.version_number,
        "outline": new_outline.expanded_outline_text
    }

@router.get("/chapters/{card_id}/outlines")
async def list_outlines(card_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    return db.query(OutlineVersion).filter(
        OutlineVersion.chapter_id == card_id
    ).order_by(OutlineVersion.version_number.desc()).all()

@router.post("/outline/{outline_id}/approve")
async def approve_outline(outline_id: str, db: Session = Depends(get_db)):

    outline = db.query(OutlineVersion).filter(
        OutlineVersion.id == outline_id
    ).first()

    if not outline:
        raise HTTPException(status_code=404) 
    
    db.query(OutlineVersion).filter(
        OutlineVersion.chapter_id == outline.chapter_id,
        OutlineVersion.id != outline_id
    ).update({"approved": False})

    outline.approved = True

    db.query(OutlineVersion).filter(
        OutlineVersion.chapter_id == outline.chapter_id,
        OutlineVersion.id != outline_id
    ).delete()

    db.commit()

    return {"status": "approved"}

@router.post("/outline/{outline_id}/draft")
async def generate_draft(
    outline_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    # Get approved outline
    outline = db.query(OutlineVersion).filter(
        OutlineVersion.id == outline_id,
        OutlineVersion.approved == True
    ).order_by(OutlineVersion.version_number.desc()).first()

    if not outline:
        raise HTTPException(status_code=400, detail="No approved outline") 
    
    card = db.query(ChapterCard).filter(
        ChapterCard.id == outline.chapter_id,
        ChapterCard.approved == True
    ).first()

    if not card:
        raise HTTPException(status_code=404, detail="Approved chapter card not found")

    # Get approved principles
    principles = db.query(Principle).filter(
        Principle.project_id == card.project_id,
        Principle.status == "approved"
    ).all()

    # Get concept
    concept = db.query(BookConcept).filter(
        BookConcept.project_id == card.project_id
    ).first()

    # Versioning
    last_version = db.query(func.max(Draft.version)).filter(
        Draft.outline_id == outline_id
    ).scalar()

    next_version = (last_version or 0) + 1

    # draft generation
    draft_text = await generate_chapter_draft(
        chapter_title=card.title_placeholder,
        outline=outline.expanded_outline_text,
        author_seed=concept.refined_thesis,
        principles=[p.text for p in principles]
    )

    # Save draft
    draft = Draft(
        id=str(uuid.uuid4()),
        outline_id=outline_id,
        chapter_card_id=card.id,
        version=next_version,
        content=draft_text,
        generation_type="new",
        author_seed_snapshot=concept.refined_thesis,
        outline_snapshot=outline.expanded_outline_text,
        principles_snapshot=[p.text for p in principles]
    )

    db.add(draft)
    db.commit()

    return {
        "draft_id": draft.id,
        "version": next_version,
        "draft": draft_text
    }

@router.get("/outline/{outline_id}/drafts")
async def list_project_drafts(
    outline_id: str,
    db: Session = Depends(get_db)
):
    query = db.query(Draft).filter(
        Draft.outline_id == outline_id
    )
    return query.order_by(Draft.created_at.desc()).all()

@router.get("/draft/{draft_id}")
async def get_draft(draft_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    return db.query(Draft).filter(Draft.id == draft_id).first()

@router.post("/draft/{draft_id}/regenerate")
async def regenerate_draft(
    draft_id: str,
    user_insights: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    
    if not user_insights:
        raise HTTPException(status_code=404, detail="User Suggestions are not found!")

    draft = db.query(Draft).filter(Draft.id == draft_id).first()

    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    card = db.query(ChapterCard).filter(
        ChapterCard.id == draft.chapter_card_id,
        ChapterCard.approved == True
    ).first()

    if not card:
        raise HTTPException(status_code=404, detail="Approved chapter card not found")

    concept = db.query(BookConcept).filter(
        BookConcept.project_id == card.project_id
    ).first()

    # Next version
    last_version = db.query(func.max(Draft.version)).filter(
        Draft.chapter_card_id == card.id
    ).scalar()

    next_version = (last_version or 0) + 1

    # regenerate
    new_text = await regenerate_chapter_draft(
        chapter_title=card.title_placeholder,
        outline=draft.outline_snapshot,
        author_seed=concept.refined_thesis,
        principles=draft.principles_snapshot,
        user_insights=user_insights,
        previous_draft=draft.content
    )

    new_draft = Draft(
        id=str(uuid.uuid4()),
        outline_id=draft.outline_id,
        chapter_card_id=card.id,
        version=next_version,
        content=new_text,
        generation_type="regenerate",
        author_seed_snapshot=concept.refined_thesis,
        outline_snapshot=draft.outline_snapshot,
        principles_snapshot=draft.principles_snapshot
    )

    db.add(new_draft)
    db.commit()

    return {
        "draft_id": new_draft.id,
        "version": next_version,
        "draft": new_text
    }



@router.get("/draft/{draft_id}/download")
async def download_draft(
    draft_id: str,
    db: Session = Depends(get_db),
    # current_user: dict = Depends(get_current_user)
):

    # Fetch draft
    draft = db.query(Draft).filter(
        Draft.id == draft_id
    ).first()

    if not draft:
        raise HTTPException(
            status_code=404,
            detail="Draft not found"
        )

    # Create DOCX document
    doc = Document()

    # Optional title
    doc.add_heading(
        f"Draft Version {draft.version}",
        level=1
    )

    # Add content
    content = draft.content or ""

    # Preserve paragraphs
    for para in content.split("\n"):
        doc.add_paragraph(para)

    # Save into memory buffer
    buffer = BytesIO()
    doc.save(buffer)
    buffer.seek(0)

    filename = f"draft_{draft.version}.docx"

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )

@router.get("/drafts/{draft_id}/provenance")
async def get_provenance(draft_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):

    return db.query(ProvenanceBlock).filter(
        ProvenanceBlock.draft_id == draft_id
    ).all()

# Admin endpoints
@admin_router.get("/bookbuilder/active-today")
async def admin_active_today(db: Session = Depends(get_db)):
    today = datetime.utcnow().date()

    rows = db.query(
        ActiveSession.user_email,
        ActiveSession.last_activity_timestamp
    ).filter(
        ActiveSession.project_name == PROJECT_NAME,
        func.date(ActiveSession.last_activity_timestamp) == today
    ).all()

    return [
        {"email": email, "last_active": ts.isoformat()}
        for email, ts in rows
    ]

@admin_router.get("/bookbuilder/error-logs")
async def get_error_logs(
    category: Optional[str] = None,
    status_code: Optional[int] = None,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    query = db.query(ApiErrorLog).filter(
        ApiErrorLog.tool == PROJECT_NAME
    )

    if category:
        query = query.filter(ApiErrorLog.category == category)

    if status_code:
        query = query.filter(ApiErrorLog.status_code == status_code)

    logs = (
        query
        .order_by(ApiErrorLog.created_at.desc())
        .limit(limit)
        .all()
    )

    if not logs:
        raise HTTPException(status_code=404, detail="No error logs found")

    return [
        {
            "id": log.id,
            "api": log.api,
            "error_type": log.error_type,
            "category": log.category,
            "message": log.message,
            "status_code": log.status_code,
            "request_id": log.request_id,
            "created_at": log.created_at.isoformat()
        }
        for log in logs
    ]