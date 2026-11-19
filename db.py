# db.py 
import hashlib
import uuid
from datetime import datetime
import os
from dotenv import load_dotenv
load_dotenv()
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Date, func, UniqueConstraint, Float, ForeignKey, DECIMAL, Boolean, Enum, JSON, Numeric, Index, BigInteger
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from sqlalchemy.dialects.mysql import CHAR

MYSQL_DATABASE_URL = os.getenv("DATABASE_URL")
#print("DB URL here -----",MYSQL_DATABASE_URL)

engine = create_engine(
    MYSQL_DATABASE_URL,
    pool_pre_ping=True,
    connect_args={
        "ssl": {
            "ssl_verify_cert": False
        }
    },
    echo=False
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class PromptLog(Base):
    __tablename__ = "prompt_logs"
    id = Column(Integer, primary_key=True, index=True)
    project_name = Column(String(255), index=True, nullable=False)
    user_email = Column(String(255), index=True, nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    prompt_text = Column(Text, nullable=False)
    prompt_type = Column(String(100), index=True)
    model = Column(String(100), nullable=True)           
    input_tokens = Column(Integer, default=0, nullable=False)
    output_tokens = Column(Integer, default=0, nullable=False)
    input_cost_usd = Column(Float, nullable=True)
    output_cost_usd = Column(Float, nullable=True)
    response_feedback = Column(String(20), nullable=True)  # "thumbs_up" or "thumbs_down"

class Template(Base):
    __tablename__ = "templates"
    id = Column(Integer, primary_key=True, index=True)
    project_name = Column(String(255), index=True, nullable=False)
    user_email = Column(String(255), index=True, nullable=False)
    template_name = Column(String(255), index=True, nullable=False)
    description = Column(Text)
    tags = Column(String(255))
    template_text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now()
    )

class ActiveSession(Base):
    __tablename__ = "active_sessions"
    id = Column(Integer, primary_key=True, index=True)
    project_name = Column(String(255), index=True, nullable=False)
    user_email = Column(String(255), index=True, nullable=False)
    last_activity_timestamp = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now()
    )
    session_id = Column(String(255), unique=True, nullable=True)
    device_id = Column(String(255))

class ApiErrorLog(Base):
    __tablename__ = "api_error_logs"

    id = Column(Integer, primary_key=True, index=True)

    tool = Column(String(100), index=True, nullable=False)
    api = Column(String(255), index=True, nullable=False)

    error_type = Column(String(150), nullable=False)
    category = Column(String(100), index=True)

    message = Column(Text)
    stack_trace = Column(Text)

    status_code = Column(Integer)

    request_id = Column(String(255), index=True)

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True
    )

class CaseValueLogs(Base):
    __tablename__ = "case_value_logs"

    id = Column(Integer, primary_key=True)
    project_name = Column(String(255))
    user_email = Column(String(255))

    prompt_type = Column(String(255))
    # NEW FIELDS
    case_number = Column(String(255))
    status = Column(String(50))
    processing_time_seconds = Column(Float) 

    generation_type = Column(String(50), default="pending")

    timestamp = Column(DateTime, default=datetime.utcnow)

class ChatHistoryPlaybook(Base):
    __tablename__ = "chat_history_playbook"

    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String(255), index=True)
    question = Column(Text)
    answer = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

class Email2AI(Base):
    __tablename__ = "email2ai"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    birthdate_code = Column(String(8), nullable=False)
    primary_email = Column(String(255), unique=True, nullable=False)
    secondary_email = Column(String(255), unique=True, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Email2AIRequestLog(Base):
    __tablename__ = "email2ai_requests_log"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("email2ai.id"), nullable=True)
    sender_email = Column(String(255), nullable=False)
    subject = Column(String(500))
    prompt_text = Column(Text, nullable=False)
    attachment_name = Column(String(255))
    attachment_type = Column(String(50))
    attachment_size = Column(Integer) 
    input_tokens = Column(Integer, default=0, nullable=False)
    output_tokens = Column(Integer, default=0, nullable=False)
    input_cost_usd = Column(Float, nullable=True)
    output_cost_usd = Column(Float, nullable=True)    
    response_time = Column(Float)
    status = Column(String(50))  # success / failed / rejected
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    user = relationship("Email2AI")

class LedgerRun(Base):
    __tablename__ = "ledger_runs"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), unique=True, index=True, nullable=False)
    project_name = Column(String(255), index=True)
    user_email = Column(String(255), index=True)
    source_filename = Column(String(255))
    total_rows = Column(Integer)
    generated_rows = Column(Integer)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    entries = relationship("LedgerEntry", back_populates="run")

class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(
        String(64),
        ForeignKey("ledger_runs.run_id", ondelete="CASCADE"),
        index=True
    )
    date = Column(Date)
    timekeeper = Column(String(255))
    source_staff_code = Column(String(50), index=True)
    assigned_staff_code = Column(String(50), index=True)
    assigned_role = Column(String(50))  # "paralegal" / "lead_attorney"
    entry_type = Column(String(20), index=True)  # "direct" / "derived"
    sheet_name = Column(String(255), index=True)
    needs_review = Column(Boolean, default=False)
    description = Column(Text)
    hours = Column(Float)
    category = Column(String(100), index=True)
    source = Column(String(50), default="generated")
    status = Column(String(50), default="pending", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True))
    run = relationship("LedgerRun", back_populates="entries")
    audit = relationship("LedgerEntryAudit", back_populates="entry", uselist=False)
    edit_history = relationship("LedgerEditHistory", back_populates="entry", cascade="all, delete-orphan")

class LedgerEntryAudit(Base):
    __tablename__ = "ledger_entry_audit"

    id = Column(Integer, primary_key=True)
    entry_id = Column(
        Integer,
        ForeignKey("ledger_entries.id", ondelete="CASCADE"),
        index=True
    )
    # original input
    original_memo = Column(Text)
    normalized_text = Column(Text)
    # classification results
    primary_category = Column(String(100))
    secondary_category = Column(String(100))
    confidence = Column(Float)
    # clustering info
    cluster_size = Column(Integer)
    cluster_date = Column(Date)
    mixed = Column(Boolean)
    # generated entry
    generated_description = Column(Text)
    generated_hours = Column(Float)
    generated_category = Column(String(100))
    # quality flags
    flag_reason = Column(String(100))
    entry_type = Column(String(20))
    assigned_role = Column(String(50))
    # optional
    entry = relationship("LedgerEntry", back_populates="audit")

class LedgerEditHistory(Base):
    __tablename__ = "ledger_edit_history"

    id = Column(Integer, primary_key=True)
    entry_id = Column(
        Integer,
        ForeignKey("ledger_entries.id", ondelete="CASCADE"),
        index=True
    )
    field_name = Column(String(100))
    old_value = Column(Text)
    new_value = Column(Text)
    edited_by = Column(String(255))
    edited_at = Column(DateTime(timezone=True), server_default=func.now())
    entry = relationship("LedgerEntry", back_populates="edit_history")

class LedgerSummaryRun(Base):
    __tablename__ = "ledger_summary_runs"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), unique=True, index=True, nullable=False)
    user_email = Column(String(255), index=True, nullable=False)
    petition_filename = Column(String(255))
    exhibits_filename = Column(String(255))
    paralegal_entries_requested = Column(Integer)
    attorney_entries_generated = Column(Integer)
    total_paralegal_hours_requested = Column(Float)
    summary = Column(JSON)  # {total_attorney_hours, total_attorney_fees}
    status = Column(String(50), default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    entries = relationship(
        "LedgerSummaryEntry",
        back_populates="run",
        cascade="all, delete-orphan"
    )


class LedgerSummaryEntry(Base):
    __tablename__ = "ledger_summary_entries"
    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(
        String(64),
        ForeignKey("ledger_summary_runs.run_id", ondelete="CASCADE"),
        index=True
    )
    assigned_role = Column(String(50))  # paralegal / lead_attorney
    task = Column(Text)
    hours = Column(Float)
    category = Column(String(100))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    run = relationship("LedgerSummaryRun", back_populates="entries")

class Whitelist(Base):
    __tablename__ = "whitelist"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=True)
    email = Column(String(255), index=True, nullable=False)
    password_hash = Column(String(255), nullable=True)
    role = Column(
        Enum("user", "editor", "admin", name="whitelist_roles"),
        nullable=False,
        index=True
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("email", "role", name="uq_email_role"),
    )

class OTPVerification(Base):
    __tablename__ = "otp_verifications"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), nullable=False, index=True)
    otp = Column(String(10), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )

class IntakeSpecialist(Base):
    __tablename__ = "intake_specialists"

    id = Column(Integer, primary_key=True, index=True)
    practice_area = Column(String(50), index=True, nullable=False)
    position = Column(Integer, nullable=False)  
    email = Column(String(255), nullable=False)

    __table_args__ = (
        UniqueConstraint("practice_area", "position", name="uq_practice_position"),
    )

class MedCertFiles(Base):
    __tablename__ = "medcert_files"

    id = Column(Integer, primary_key=True, index=True)
    zip_filename = Column(String(255))
    session_id = Column(String(36), index=True)
    user_email = Column(String(255), index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class MedCertRuns(Base):
    __tablename__ = "medcert_case_runs"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(100), index=True)
    user_email = Column(String(255), index=True)

    case_header = Column(JSON)
    addressee_logic = Column(JSON)
    procedural_stage = Column(JSON)
    documents = Column(JSON)
    taxonomy = Column(JSON)
    issue_stack = Column(JSON)
    fact_model = Column(JSON)
    timeline = Column(JSON)
    missing_items = Column(JSON)
    guideline_checklists = Column(JSON)
    draft_outline = Column(JSON)

    created_at = Column(DateTime, server_default=func.now())

class BookProject(Base):
    __tablename__ = "book_projects"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255))
    created_by = Column(String(255), index=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    chapter_cards = relationship("ChapterCard", backref="project", cascade="all, delete-orphan")
    # sources = relationship("Source", back_populates="project", cascade="all, delete-orphan")

class BookConcept(Base):
    __tablename__ = "book_concepts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String(36), ForeignKey("book_projects.id"), index=True)

    working_title = Column(String(255))
    concept_brief = Column(Text)

    original_thesis = Column(Text) 
    refined_thesis = Column(Text)

    why_now = Column(Text)
    target_reader = Column(Text)
    author_qualification = Column(Text)

    novelty_claim = Column(Text)
    anti_goals = Column(Text)

    status = Column(String(50), default="draft")  # draft / refined / locked

    created_at = Column(DateTime(timezone=True), server_default=func.now())

class NoveltyScan(Base):
    __tablename__ = "novelty_scans"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String(36), ForeignKey("book_projects.id"))
    status = Column(String(50), nullable= False, default="pending") 

    query_context = Column(Text)

    overlapping_titles = Column(JSON)
    adjacent_books = Column(JSON)

    gap_analysis = Column(JSON)
    derivative_risk = Column(Text)
    uniqueness_summary = Column(Text)
    recommendations = Column(JSON)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

class AuthorityMap(Base):
    __tablename__ = "authority_maps"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String(36), ForeignKey("book_projects.id"))
    status = Column(String(50), nullable= False, default="pending") 
    why_author_credible = Column(Text)
    experience_areas = Column(JSON)
    weak_areas = Column(JSON)
    caution_flags = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

class StoryBankItem(Base):
    __tablename__ = "story_bank_items"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String(36), ForeignKey("book_projects.id"))
    status = Column(String(50), nullable= False, default="pending") 

    title = Column(String(255))
    type = Column(String(50))  # story / analogy / case / scene / metaphor

    content = Column(Text)

    linked_chapter_id = Column(String(36), ForeignKey("chapter_cards.id", ondelete="SET NULL"), nullable=True)

    approved = Column(Boolean, default=False)

    tags = Column(JSON)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

class StructureOption(Base):
    __tablename__ = "structure_options"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String(36), ForeignKey("book_projects.id"))
    status = Column(String(50), nullable= False, default="pending") 

    option_name = Column(String(255))
    option_summary = Column(Text)

    chapter_count_estimate = Column(String(255))

    why_this_structure_fits = Column(Text)

    recommended = Column(Boolean, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

class ChapterCard(Base):
    __tablename__ = "chapter_cards"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String(36), ForeignKey("book_projects.id"))
    status = Column(String(50), nullable= False, default="pending") 
    title_placeholder = Column(String(255))

    mission = Column(Text)
    main_claim = Column(Text)

    rough_word_target = Column(Integer)

    linked_story_ids = Column(JSON)
    linked_principle_ids = Column(JSON)

    order_index = Column(Integer)

    approved = Column(Boolean, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    outlines = relationship("OutlineVersion",backref="chapter",cascade="all, delete-orphan",  passive_deletes=True)

class OutlineVersion(Base):
    __tablename__ = "outline_versions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    chapter_id = Column(String(36), ForeignKey("chapter_cards.id", ondelete="CASCADE"))
    status = Column(String(50), nullable= False, default="pending") 

    version_number = Column(Integer)

    expanded_outline_text = Column(Text)

    approved = Column(Boolean, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

class ConceptQA(Base):
    __tablename__ = "concept_qa"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String(36), ForeignKey("book_projects.id", ondelete="CASCADE"))

    question = Column(Text)
    answer = Column(Text)
    is_answered = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    answered_at = Column(DateTime, default=datetime.utcnow)

class Draft(Base):
    __tablename__ = "drafts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    outline_id = Column(
        String(36),
        ForeignKey("outline_versions.id", ondelete="CASCADE"),
        index=True,
        nullable=False
    )
    chapter_card_id = Column(
        String(36),
        ForeignKey("chapter_cards.id", ondelete="CASCADE"),
        index=True
    )

    version = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    author_seed_snapshot = Column(Text)
    outline_snapshot = Column(Text)   
    principles_snapshot = Column(JSON)

    generation_type = Column(String(20))  # new / regenerate
    ai_density = Column(Float, default=0.0)

    retrieval_context = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    chapter_card = relationship("ChapterCard", backref="drafts")
    outline = relationship("OutlineVersion", backref="drafts")
    provenance_blocks = relationship("ProvenanceBlock", backref="draft", cascade="all, delete-orphan")
    __table_args__ = (
    UniqueConstraint("chapter_card_id", "version", name="uq_chapter_card_draft_version"),
    )

class ProvenanceBlock(Base):
    __tablename__ = "provenance_blocks"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    draft_id = Column(String(36), ForeignKey("drafts.id", ondelete="CASCADE"))
    chapter_id = Column(String(36), ForeignKey("chapter_cards.id", ondelete="CASCADE"))

    block_index = Column(Integer, nullable=False)
    text = Column(Text)

    provenance_type = Column(String(50), nullable=False)
    # e.g.
    # author_original
    # author_dictated
    # ai_rewrite_of_author
    # ai_synthesis_from_tier1
    # ai_synthesis_from_tier1_and_tier4
    # ai_summary_of_source
    # ai_external_research_synthesis

    source_chunk_ids = Column(JSON)  # list of chunk IDs used
    source_ids = Column(JSON)        # list of source IDs used
    ai_confidence = Column(Float, default=1.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Principle(Base):
    __tablename__ = "principles"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    project_id = Column(String(36), ForeignKey("book_projects.id"), nullable=True)

    text = Column(Text, nullable=False)
    status = Column(String(20), default="draft")  

    created_by = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    project = relationship("BookProject", backref="principles")

#-------- Analytics sheet data -------------# 

class AnalyticsReport(Base):
    __tablename__ = "analytics_reports"
 
    id = Column(Integer, primary_key=True)
    month = Column(Integer)
    year = Column(Integer)
    created_at = Column(DateTime)
 
 
class AnalyticsExecutiveSummary(Base):
    __tablename__ = "analytics_executive_summary"
 
    id = Column(Integer, primary_key=True)
    report_id = Column(
        Integer,
        ForeignKey("analytics_reports.id")
    )
    team = Column(String(100))
    mva22_signup = Column(Integer)
    mva22_total = Column(Integer)
    mva22_rate = Column(Float)
    total_edits = Column(Integer)
    total_created = Column(Integer)
    total_calls = Column(Integer)
    talk_time = Column(String(50))
    overall_signup = Column(Integer)
    overall_total = Column(Integer)
    overall_rate = Column(Float)
 
 
class AnalyticsWeeklyPerformance(Base):
    __tablename__ = "analytics_weekly_performance"
 
    id = Column(Integer, primary_key=True)
    report_id = Column(
        Integer,
        ForeignKey("analytics_reports.id")
    )
    week_number = Column(Integer)
    week_start = Column(Date)
    week_end = Column(Date)
    team = Column(String(100))
    mva22_signup = Column(Integer)
    mva22_total = Column(Integer)
    mva22_rate = Column(Float)
    total_edits = Column(Integer)
    total_created = Column(Integer)
    total_calls = Column(Integer)
    talk_time = Column(String(50))
    overall_signup = Column(Integer)
    overall_total = Column(Integer)
    overall_rate = Column(Float)


class LeadIntake(Base):
    __tablename__ = "leads_intake"


    id = Column(Integer, primary_key=True, index=True)


    email_id = Column(Integer, ForeignKey("emails_raw.id", ondelete="CASCADE"))


    # personal info
    first_name = Column(String(255))
    last_name = Column(String(255))
    email = Column(String(255), index=True)
    phone = Column(String(50))


    # accident info
    state_of_incident = Column(String(255))
    case_type = Column(String(255))
    accident_date = Column(String(255))


    fault = Column(String(255))
    injury_severity = Column(Text)
    medical_treatment = Column(String(255))
    insurance = Column(String(255))
    source = Column(String(255))


    case_description = Column(Text)
    language = Column(String, nullable=True)


    # NAFI output (ENUM in MySQL)
    nafi_tool_recommendation = Column(
        Enum("likely_nafi", "not_nafi", "rejected")
    )


    nafi_tool_reason = Column(Text)


    # workflow status (ENUM in MySQL)
    status = Column(
        Enum("pending", "proceed", "declined", "escalated"),
        index=True
    )
    decline_type = Column(Text, nullable=True)


    reviewer_note = Column(Text)
    reviewed_by = Column(String(255))
    reviewed_at = Column(DateTime)


    # MySQL tinyint(1)
    email_sent = Column(Boolean)


    received_at = Column(DateTime)


    practice_area = Column(String(50))   # MVA, GPI, CONSUMER
    assigned_group = Column(Integer)     # 1 or 2
    assigned_users = Column(JSON)        # list of 3 emails


    lead_accepted = Column(Boolean, default=False)
    lead_accepted_by = Column(String(255), nullable=True)
    lead_accepted_at = Column(DateTime, nullable=True)


    lead_email_sent_at = Column(DateTime, nullable=True)
    lead_accept_time_seconds = Column(Integer, nullable=True)
    neos_updated = Column(Boolean, default=False, nullable=False)
    neos_case_number = Column(String(50), nullable=True)
    neos_attempt_count = Column(Integer, default=0)
    neos_status = Column(String(255), nullable=True)  # "Stage | Status" text can exceed 50 chars
    agree_disagree = Column(String(50), nullable=True, default=None)
    assigned_to = Column(String(50), nullable=True) 


    agreement_sent = Column(Boolean, default=False, nullable=False, index=True)
    agreement_sent_at = Column(DateTime, nullable=True)
    agreement_signed = Column(Boolean, default=False, nullable=False, index=True)
    agreement_signed_at = Column(DateTime, nullable=True)
    agreement_signed_neos_number = Column(String(50), nullable=True)
    agreement_signed_needs_review = Column(Boolean, default=False, nullable=False) 


    # Reviewer decision tracking
    reviewer_decision = Column(String(20), nullable=True)
    reviewer_answer_at = Column(DateTime, nullable=True)
    agent_status_at_answer = Column(String(20), nullable=True)
    agent_version_at_answer = Column(String(20), nullable=True) 


    name_check = Column(String(20), nullable=True, index=True)
    name_check_reason = Column(Text, nullable=True)
    needs_name_review = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )


    # Neos Direct: intakes staff typed straight into Neos that never came through a
    # web form / email / call feed. NULL channel = normal IntakeOps lead.
    # Columns are added to the live table by MVA_Platform/migrate_neos_direct.py.
    intake_channel = Column(String(30), nullable=True, index=True)          # "neos_direct" | NULL
    neos_intake_id = Column(String(64), nullable=True, unique=True)         # upsert key for Neos Direct rows
    scoring_status = Column(String(20), nullable=True)                      # "not_scored" for Neos Direct
    write_neos_notes = Column(Boolean, nullable=False, default=True, server_default="1")
    send_emails = Column(Boolean, nullable=False, default=True, server_default="1")
    link_review = Column(Boolean, nullable=False, default=False, server_default="0")
    link_candidate_lead_ids = Column(String(255), nullable=True)
    neos_intake_meta = Column(JSON, nullable=True)                          # stage, taken_by, referred_by, ...
    neos_direct_loaded_at = Column(DateTime, nullable=True)


    # relationship
    email_record = relationship("EmailRaw")

class LeadStatusHistory(Base):                      
    __tablename__ = "lead_status_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    lead_id = Column(Integer, ForeignKey("leads_intake.id"), nullable=False, index=True)  
    old_status = Column(String(20), nullable=True)   
    new_status = Column(String(20), nullable=False)
    prompt_version = Column(String(20), nullable=True)
    changed_by = Column(String(255), nullable=True)
    changed_at = Column(DateTime, nullable=False, default=datetime.utcnow)

class EmailRaw(Base):
    __tablename__ = "emails_raw"

    id = Column(Integer, primary_key=True, index=True)

    message_id = Column(String(500))
    subject = Column(String(500))
    sender = Column(String(255))

    recipients = Column(JSON)
    received_at = Column(DateTime)
    sent_at = Column(DateTime)

    conversation_id = Column(String(500), unique=True, index=True)

    body_text = Column(Text)
    body_html = Column(Text)

    created_at = Column(DateTime)

class Referral(Base):
    __tablename__ = "referrals"

    id = Column(Integer, primary_key=True, index=True)

    practice_area = Column(String(100), index=True)

    organization_name = Column(String(100), nullable=False)
    contact_name = Column(String(100), nullable=True)

    phone = Column(String(100), nullable=True)
    email = Column(String(100), nullable=True)

    is_active = Column(Boolean, default=True)
    last_updated = Column(DateTime(timezone=True), server_default=func.now())

class LeadReferral(Base):
    __tablename__ = "lead_referrals"

    id = Column(Integer, primary_key=True)

    lead_id = Column(Integer, ForeignKey("leads_intake.id"))
    referral_id = Column(Integer, ForeignKey("referrals.id"))

    selected_by = Column(Text)
    selected_at = Column(DateTime, default=datetime.utcnow)
    referral = relationship("Referral")
    lead = relationship("LeadIntake")
    __table_args__ = (
        UniqueConstraint('lead_id', 'referral_id', name='uq_lead_referral'),
    )

class AgreementSignedUnmatched(Base):
    __tablename__ = "agreement_signed_unmatched"

    id = Column(Integer, primary_key=True)
    neos_number = Column(String(50))
    first_name = Column(String(255))
    last_name = Column(String(255))
    case_type = Column(String(255))
    signed_at = Column(DateTime, nullable=True)
    reason = Column(String(50))
    matched_lead_id = Column(Integer, nullable=True)
    imported_at = Column(DateTime, default=datetime.utcnow)

class LeadOwlReview(Base):
    __tablename__ = "lead_owl_reviews"
    __table_args__ = (UniqueConstraint("lead_id", name="uq_owl_review_lead"),)   
    id = Column(Integer, primary_key=True, autoincrement=True)
    lead_id = Column(Integer, ForeignKey("leads_intake.id"), nullable=False, index=True)
    verdict = Column(String(10), nullable=False)          
    reason = Column(Text, nullable=True)                  
    confidence = Column(Integer, nullable=True)           
    rules_version = Column(String(20), nullable=True)    
    owl_decision = Column(JSON, nullable=True)           
 
    agent_status_at_review = Column(String(20), nullable=True)
    agent_decline_type_at_review = Column(String(40), nullable=True)
    agent_version_at_review = Column(String(20), nullable=True)
    lead_version = Column(DateTime, nullable=True)       
    overridden = Column(Boolean, nullable=False, default=False, server_default="0")
    override_verdict = Column(String(10), nullable=True)
    override_reason = Column(Text, nullable=True)
    override_confidence = Column(Integer, nullable=True)
    overridden_by = Column(String(255), nullable=True)
    overridden_at = Column(DateTime, nullable=True)
    edit_history = Column(JSON, nullable=True)          
 
    reviewer = Column(String(20), nullable=False, default="owl")
    started_by = Column(String(255), nullable=True)      
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

class LeadKeilaReview(Base):
    __tablename__ = "lead_keila_reviews"

    id = Column(Integer, primary_key=True, index=True)

    lead_id = Column(
        Integer,
        ForeignKey("leads_intake.id"),
        nullable=False,
        index=True
    )

    keila_email = Column(
        String(255),
        nullable=False
    )

    agrees_with_owl = Column(
        Boolean,
        nullable=False
    )

    keila_decision = Column(
        String(100),
        nullable=True
    )

    reason = Column(
        Text,
        nullable=True
    )

    reviewed_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )

    __table_args__ = (
        Index("ix_keila_lead_reviewed", "lead_id", "reviewed_at"),
    )

class Matter(Base):
    __tablename__ = "matters"

    id = Column(Integer, primary_key=True, index=True)
    case_name = Column(String(500), nullable=False)
    jurisdiction = Column(String(100), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    events = relationship("MatterEvent", back_populates="matter", cascade="all, delete-orphan")
    deadlines = relationship("ComputedDeadline", back_populates="matter", cascade="all, delete-orphan")

class MatterEvent(Base):
    __tablename__ = "matter_events"

    id = Column(Integer, primary_key=True, index=True)
    matter_id = Column(Integer, ForeignKey("matters.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String(255), nullable=False, index=True)
    event_date = Column(Date, nullable=False, index=True)
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    filing_status = Column(String(10), nullable=True)
    matter = relationship("Matter", back_populates="events")
    computed_deadlines = relationship("ComputedDeadline", back_populates="source_event", cascade="all, delete-orphan")
    
    __table_args__ = (
    UniqueConstraint(
        "matter_id",
        "event_type",
        "event_date",
        name="uq_matter_event"
    ),
    )

class ComputedDeadline(Base):
    __tablename__ = "computed_deadlines"

    id = Column(Integer, primary_key=True, index=True)
    matter_id = Column(Integer, ForeignKey("matters.id", ondelete="CASCADE"), nullable=False, index=True)
    source_event_id = Column(Integer, ForeignKey("matter_events.id", ondelete="CASCADE"), nullable=True, index=True)
    rule_id = Column(String(255), nullable=False, index=True)
    label = Column(String(500), nullable=False)
    due_date = Column(Date, nullable=False, index=True)
    color = Column(String(50))
    notes = Column(Text)
    reminders_days = Column(JSON)
    computation_snapshot = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Approval workflow fields
    is_approved = Column(Boolean, default=False, index=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    approved_by = Column(String(255), nullable=True)
    filing_status = Column(String(10), nullable=True)

    matter = relationship("Matter", back_populates="deadlines")
    source_event = relationship("MatterEvent", back_populates="computed_deadlines")
    attorney_syncs = relationship("DeadlineAttorney", back_populates="deadline", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint(
            "matter_id",
            "rule_id",
            "due_date",
            name="uq_deadline_unique"
        ),
    )

class DeadlineAttorney(Base):
    __tablename__ = "deadline_attorneys"

    id = Column(Integer, primary_key=True, index=True)

    deadline_id = Column(Integer, ForeignKey("computed_deadlines.id", ondelete="CASCADE"), nullable=False, index=True)
    attorney_email = Column(String(255), nullable=False, index=True)
    graph_event_id = Column(String(255), nullable=True)
    synced_at = Column(DateTime(timezone=True), server_default=func.now())
    deadline = relationship("ComputedDeadline", back_populates="attorney_syncs")

    __table_args__ = (
        UniqueConstraint("deadline_id", "attorney_email", name="uq_deadline_attorney"),
    )

# lxintel Attorney Tracker
class AttorneyQuickBookUpload(Base):
    __tablename__ = "attorney_quickbook_uploads"

    id          = Column(Integer, primary_key=True)
    upload_uuid = Column(String(100), nullable=False, unique=True, index=True)
    file_name   = Column(String(255), nullable=False)
    source_type = Column(String(20), nullable=False, index=True)
    detected_format = Column(String(50), nullable=True)
    sheet_name = Column(String(255), nullable=True)
    threshold = Column(Numeric(18, 2), default=500000, nullable=True)
    total_rows      = Column(Integer, default=0)
    status          = Column(String(50), default="PENDING_REVIEW")
    formula_version = Column(String(20), default="v1", nullable=False)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
 
 
class AttorneyTrackerRecord(Base):
    __tablename__ = "attorney_tracker_records"
 
    id = Column(Integer, primary_key=True)
    upload_id = Column(
        Integer,
        ForeignKey("attorney_quickbook_uploads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
 
    # Case-level fields
    case_id         = Column(String(100), nullable=True)
    settlement_date = Column(Date, nullable=True)
    legal_fee_value = Column(Numeric(18, 2), nullable=True)
    case_type       = Column(String(100), nullable=True)
 
    # Shared / identity fields
    reporting_month = Column(Date, nullable=True)
    attorney_name   = Column(String(255), nullable=True)
    division_name   = Column(String(100), nullable=True)
    settlement_value = Column(Numeric(18, 2), nullable=True)
    is_very_large_case = Column(Boolean, default=False)
    source_sheet    = Column(String(255), nullable=True)
    source_file     = Column(String(512), nullable=True)
    notes           = Column(Text, nullable=True)
 
    # Monthly metric fields
    year                = Column(Integer, nullable=True)
    month_number        = Column(Integer, nullable=True)
 
    legal_fees_collected = Column(Numeric(15, 2), nullable=True)
    cases_distributed    = Column(Integer, nullable=True)
    monthly_fee_reduction = Column(Numeric(15, 2), nullable=True)
    active_caseload_snapshot = Column(Integer, nullable=True)
    total_settlement_value   = Column(Numeric(15, 2), nullable=True)
    adjusted_value_excluding_large_cases = Column(Numeric(15, 2), nullable=True)
    large_case_count    = Column(Integer, nullable=True)
    large_case_value    = Column(Numeric(15, 2), nullable=True)
 
    detected_format = Column(String(50), nullable=True)   # NEW
 
    # Validation / workflow fields
    is_valid           = Column(Boolean, default=True)
    validation_errors  = Column(Text, nullable=True)
    is_locked          = Column(Boolean, default=False)
    created_at         = Column(DateTime(timezone=True), server_default=func.now())

# QuickBooks 
class QuickbookTransaction(Base):
    __tablename__ = "quickbook_transactions"
    id = Column(Integer, primary_key=True)
    upload_id = Column(
        Integer,
        ForeignKey("attorney_quickbook_uploads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Transaction Information
    type = Column(String(100), nullable=True)
    date = Column(Date, nullable=True)
    num = Column(String(100), nullable=True)
    name = Column(String(255), nullable=True)
    memo = Column(Text, nullable=True)
    class_ = Column("class", String(255), nullable=True)
    clr = Column(String(50), nullable=True)
    split = Column(String(255), nullable=True)

    # Financial Fields
    debit = Column(Numeric(18, 2), nullable=True)
    credit = Column(Numeric(18, 2), nullable=True)
    balance = Column(Numeric(18, 2), nullable=True)

    # Extracted Fields
    case_number = Column(String(100), nullable=True)
    case_type_description = Column(Text, nullable=True)
    attorney = Column(String(255), nullable=True)
    office = Column(String(255), nullable=True)
    case_type_merged = Column(String(255), nullable=True)

    # Validation / Workflow
    is_valid = Column(Boolean, default=True)
    validation_errors = Column(Text, nullable=True)
    is_locked = Column(Boolean, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

# evidence flow database models
# class Case(Base):
#     __tablename__ = "cases"

#     # Identity
#     id = Column(
#         String(36),
#         primary_key=True,
#         default=lambda: str(uuid.uuid4())
#     )
#     title = Column(String(500), nullable=False)
#     description = Column(Text)

#     # open | closed | archived
#     status = Column(String(50), default="open")

#     # Case Overview
#     case_type = Column(String(100))
#     jurisdiction = Column(String(200))
#     petitioner = Column(String(500))
#     respondent = Column(String(500))
#     minor_child = Column(String(500))
#     attorney = Column(String(500))
#     opposing_counsel = Column(String(500))

#     # Example: ["Portuguese", "English"]
#     languages = Column(JSON, default=list)
#     client_safety_notes = Column(Text)

#     # Reference dates
#     protective_order_date = Column(DateTime)
#     attorney_notice_date = Column(DateTime)
#     next_court_deadline = Column(DateTime)

#     # Exhibit counter
#     exhibit_counter = Column(Integer, default=0, nullable=False)

#     # Timestamps
#     created_at = Column(DateTime, server_default=func.now())
#     updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

# # CASE PERSONS
# class CasePerson(Base):
#     __tablename__ = "case_persons"

#     id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
#     case_id = Column(CHAR(36), ForeignKey("cases.id"), nullable=False, index=True)

#     # Display / Search
#     display_name = Column(String(255))
#     full_name = Column(String(500))
#     short_name = Column(String(100), index=True)

#     # ["Rick", "Ric", "Ricky"]
#     aliases = Column(JSON, default=list)

#     # Role / Relationship
#     role = Column(String(100), default="Unknown")
#     party_label = Column(String(100))
#     relationship = Column(String(200))

#     # Channel Identifiers
#     channels = Column(JSON, default=list)

#     # Confidence
#     confidence = Column(Float, default=0.0, nullable=False)
#     confirmed_by_user = Column(Boolean, default=False, nullable=False)

#     # explicit | inferred | manual
#     source = Column(
#         String(20),
#         default="manual",
#         nullable=False
#     )

#     # Attorney Review
#     attorney_reviewed = Column(Boolean, default=False, nullable=False)
#     reviewed_by = Column(CHAR(36), nullable=True)
#     reviewed_at = Column(DateTime)
#     notes = Column(Text)

#     # Merge / Split Audit
#     merged_into_id = Column(CHAR(36), ForeignKey("case_persons.id"), nullable=True)
#     split_from_id = Column(CHAR(36), ForeignKey("case_persons.id"), nullable=True)
#     audit_log = Column(JSON, default=list)

#     created_at = Column(DateTime, server_default=func.now())
#     updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

# # EVIDENCE ITEMS
# class EvidenceItem(Base):
#     __tablename__ = "evidence_items"

#     id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
#     case_id = Column(String(36), ForeignKey("cases.id"), nullable=True, index=True)
#     original_filename = Column(String(500), nullable=False)
#     stored_filename = Column(String(500), nullable=False)
#     file_path = Column(Text, nullable=False)
#     file_type = Column(String(50), nullable=False)
#     mime_type = Column(String(255))
#     file_size = Column(BigInteger)
#     extracted_text = Column(Text)
#     ocr_status = Column(String(50), default="pending")
#     detected_language = Column(String(20))
#     translated_text = Column(Text)
#     structured_data = Column(JSON)
#     attorney_summary = Column(JSON)
#     attorney_summary_generated_at = Column(DateTime)
#     exhibitid = Column(String(100))
#     upload_source = Column(String(50))
#     created_at = Column(DateTime, server_default=func.now())
#     updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


# # EVIDENCE VERSIONS
# class EvidenceVersion(Base):
#     __tablename__ = "evidence_versions"

#     id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
#     evidence_id = Column(String(36), ForeignKey("evidence_items.id"))
#     version_name = Column(String(100))
#     version = Column(Integer, default=1)
#     processing_stage = Column(String(100))
#     processed_output = Column(JSON)
#     raw_text = Column(Text)
#     cleaned_text = Column(Text)
#     translated_text = Column(Text)
#     detected_language = Column(String(20))
#     entities = Column(JSON)
#     tags = Column(JSON)
#     timeline_data = Column(JSON)
#     summary = Column(Text)
#     exhibitid = Column(String(100))
#     confidence_score = Column(Float)
#     created_at = Column(DateTime, server_default=func.now())

# # CONVERSATION ENTRIES
# class ConversationEntry(Base):
#     __tablename__ = "conversation_entries"

#     id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
#     evidence_id = Column(String(36), ForeignKey("evidence_items.id", ondelete="CASCADE"), nullable=False, index=True)
#     case_id = Column(String(36), ForeignKey("cases.id"), nullable=True, index=True)
#     position = Column(Integer, nullable=False, default=0)
#     page_index = Column(Integer)
#     sender = Column(String(255))
#     recipient = Column(String(255))
#     date_time_text = Column(String(50))
#     normalized_dt = Column(DateTime, index=True)
#     mode_tone = Column(String(255))
#     original_language = Column(String(50))
#     translated_language = Column(String(50))
#     role = Column(String(100))
#     tag = Column(JSON)
#     original_message = Column(Text, nullable=False)
#     translated_message = Column(Text)
#     proposed_event_summary = Column(Text)
#     proposed_legal_relevance = Column(Text)
#     attorney_reviewed = Column(Boolean, default=False, nullable=False)
#     attorney_edited_message = Column(Text)
#     attorney_notes = Column(Text)
#     reviewed_by = Column(String(36))
#     reviewed_at = Column(DateTime)
#     created_at = Column(DateTime, server_default=func.now())
#     updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

# class ExportPacket(Base):
#     __tablename__ = "export_packets"
#     id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
#     case_id = Column(CHAR(36), ForeignKey("cases.id"), nullable=False, index=True)
#     created_by = Column(CHAR(36), nullable=True)
#     exhibit_count = Column(Integer, default=0)

#     # SHA256 hash = 64 chars
#     content_hash = Column(String(64))
#     packet_json = Column(JSON)
#     audit_log = Column(JSON, default=list)
#     created_at = Column(DateTime, server_default=func.now())
#     downloaded_at = Column(DateTime, nullable=True)
#     downloaded_by = Column(CHAR(36), nullable=True)


# Custom Service Dashboard
class ExitInterview(Base):
    __tablename__ = "exit_interviews"

    id                    = Column(Integer, primary_key=True, index=True)   
    file_id = Column(Integer,ForeignKey("uploaded_files.id"),index=True)
    source_row_id = Column(Integer, index=True)
    submitted_at          = Column(DateTime(timezone=True), index=True)     
    submitter_email       = Column(String(255), index=True)                 
    submitter_name        = Column(String(255))                              
    case_name             = Column(String(500), index=True)                  
    neos_id               = Column(Integer, index=True)                      
    lead_attorney         = Column(String(255), index=True)                 
    attorney_code         = Column(String(50), index=True)                   
    paralegal             = Column(String(255), index=True)                
    paralegal_code        = Column(String(50), index=True)
    rating_attorney_comm  = Column(Integer)  
    rating_paralegal_comm = Column(Integer)  
    rating_case_outcome   = Column(Integer) 
    rating_overall        = Column(Integer)  
    willing_to_refer      = Column(String(100), index=True) 
    suggestions           = Column(Text, nullable=True)       

    google_review_status  = Column(
        Enum(
            "Yes - Link sent",
            "No - Client Response (Link not sent)",
            "No - You determined not to send the link",
            name="google_review_status_enum"
        ),
        index=True,
        nullable=True
    )

    month                 = Column(String(20), index=True)   
    year                  = Column(String(10), index=True)   

    avg_rating            = Column(Float, nullable=True)
    sentiment_flag        = Column(
        Enum("positive", "neutral", "negative", name="sentiment_flag_enum"),
        nullable=True, index=True
    )
    referral_flag         = Column(Boolean, default=False, index=True)
    review_sent           = Column(Boolean, default=False)
    review_declined       = Column(Boolean, default=False)
    review_withheld       = Column(Boolean, default=False)

    created_at            = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_exit_year_month", "year", "month"),
        Index("ix_exit_attorney_year", "attorney_code", "year"),
    )

class SyncLog(Base):
    __tablename__ = "csd_sync_logs"

    id            = Column(Integer, primary_key=True, index=True)
    sync_type     = Column(
        Enum("graph_api", "manual_upload", name="sync_type_enum"),
        nullable=False
    )
    status        = Column(
        Enum("success", "partial", "failed", name="sync_status_enum"),
        nullable=False, index=True
    )
    rows_fetched  = Column(Integer, default=0)
    rows_inserted = Column(Integer, default=0)
    rows_skipped  = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    triggered_by  = Column(String(255), nullable=True)   # email if manual
    synced_at     = Column(DateTime(timezone=True), server_default=func.now(), index=True)

class CustomServiceFile(Base):
    __tablename__ = "uploaded_files"
    id = Column(Integer, primary_key=True)
    file_name = Column(String(500))
    uploaded_by = Column(String(255))
    rows_fetched = Column(Integer)
    uploaded_at = Column(DateTime(timezone=True),server_default=func.now())

# Marketing Dashboard 
class MarketingUpload(Base):
    __tablename__ = "marketing_uploads"

    id = Column(Integer, primary_key=True)
    file_name = Column(String(500), nullable=False)
    file_type = Column(String(50), nullable=False) 
    uploaded_by = Column(String(255), nullable=True)
    total_records = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now()) 


class MarketingTraffic(Base):
    __tablename__ = "marketing_traffic"
    id = Column(Integer, primary_key=True)
    upload_id = Column(
        Integer,
        ForeignKey("marketing_uploads.id"),
        nullable=True
    )
    source_medium = Column(String(255), nullable=False)
    date_hour = Column(String(20), nullable=False)
    sessions = Column(Integer, default=0)
    key_events = Column(Integer, default=0)
    key_events_per_active_user = Column(Float, default=0)
    avg_engagement_time = Column(Float, default=0)
    avg_session_duration = Column(Float, default=0)
    bounce_rate = Column(Float, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    upload = relationship(
        "MarketingUpload",
        backref="traffic_records"
    )

class GoogleAdsRecord(Base):
    __tablename__ = "google_ads_records"

    id         = Column(Integer, primary_key=True)
    upload_id  = Column(Integer, ForeignKey("marketing_uploads.id"), nullable=True)
    campaign   = Column(String(500), nullable=False, index=True)
    ad_group   = Column(String(500), nullable=True)
    clicks          = Column(Integer,  default=0)
    impressions     = Column(Integer,  default=0)
    cost            = Column(Float,    default=0.0)   # USD
    ctr             = Column(Float,    default=0.0)   # e.g. 4.92  (percent string → float)
    avg_cpc         = Column(Float,    default=0.0)
    phone_calls     = Column(Integer,  default=0)
    conversions     = Column(Float,    default=0.0)
    cost_per_conv   = Column(Float,    default=0.0)
    conv_rate       = Column(Float,    default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    upload     = relationship("MarketingUpload", backref="google_ads_records")
 
class MetaAdsRecord(Base):
    __tablename__ = "meta_ads_records"
 
    id         = Column(Integer, primary_key=True)
    upload_id  = Column(Integer, ForeignKey("marketing_uploads.id"), nullable=True)
    account_name    = Column(String(255), nullable=True)
    campaign_name   = Column(String(500), nullable=False, index=True)
    platform        = Column(String(50),  nullable=True, index=True) 
    reach                   = Column(Integer, default=0)
    impressions             = Column(Integer, default=0)
    frequency               = Column(Float,   default=0.0)
    amount_spent_usd        = Column(Float,   default=0.0)
    link_clicks             = Column(Integer, default=0)
    cpc_link                = Column(Float,   default=0.0)
    clicks_all              = Column(Integer, default=0)
    ctr_all                 = Column(Float,   default=0.0)
    cost_per_1k_reached     = Column(Float,   default=0.0)
    leads                   = Column(Integer, default=0)
    cost_per_lead           = Column(Float,   default=0.0)
    website_leads           = Column(Integer, default=0)
    meta_leads              = Column(Integer, default=0)
    website_lead_submissions= Column(Integer, default=0)
    click_to_call           = Column(Integer, default=0)
    reporting_starts        = Column(Date,    nullable=True)
    reporting_ends          = Column(Date,    nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    upload     = relationship("MarketingUpload", backref="meta_ads_records")
 
class OttAllCampaignsRecord(Base):
    __tablename__ = "ott_all_campaigns"
 
    id         = Column(Integer, primary_key=True)
    upload_id  = Column(Integer, ForeignKey("marketing_uploads.id"), nullable=True)
    day                         = Column(Date,    nullable=True, index=True)
    spend                       = Column(Float,   default=0.0)
    impressions                 = Column(Integer, default=0)
    clicks                      = Column(Integer, default=0)
    cpm                         = Column(Float,   default=0.0)
    ctr                         = Column(Float,   default=0.0)
    completions                 = Column(Integer, default=0)
    vcr                         = Column(Float,   default=0.0) 
    reach                       = Column(Integer, default=0)
    frequency                   = Column(Float,   default=0.0)
    leads                       = Column(Integer, default=0)
    lead_conversion_rate        = Column(Float,   default=0.0)
    lead_cpa                    = Column(Float,   default=0.0)
    lead_perf_multiplier        = Column(Float,   default=0.0)
    lead_time_to_convert_hrs    = Column(Float,   default=0.0)
    lead_total_exposures        = Column(Float,   default=0.0)
    page_visits                 = Column(Integer, default=0)
    page_visit_conv_rate        = Column(Float,   default=0.0)
    page_visit_cpa              = Column(Float,   default=0.0)
    page_visit_perf_multiplier  = Column(Float,   default=0.0)
    page_visit_time_to_convert  = Column(Float,   default=0.0)
    page_visit_total_exposures  = Column(Float,   default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    upload     = relationship("MarketingUpload", backref="ott_all_records")
 
class OttCampaignRecord(Base):
    __tablename__ = "ott_campaign_records"
    id         = Column(Integer, primary_key=True)
    upload_id  = Column(Integer, ForeignKey("marketing_uploads.id"), nullable=True)
    campaign_name   = Column(String(100), nullable=False, index=True)  
    media_type      = Column(String(100), nullable=True)  
    creative        = Column(String(500), nullable=True)
    publisher       = Column(String(200), nullable=True)
    audience        = Column(String(200), nullable=True)
    ad_group        = Column(String(200), nullable=True)
    device_type     = Column(String(50),  nullable=True)  
    state           = Column(String(100), nullable=True,  index=True)
    dma             = Column(String(200), nullable=True,  index=True)
    zip_code        = Column(String(20),  nullable=True)
    day             = Column(Date, nullable=True, index=True) 
    spend                   = Column(Float,   default=0.0)
    impressions             = Column(Integer, default=0)
    clicks                  = Column(Integer, default=0)
    cpm                     = Column(Float,   default=0.0)
    ctr                     = Column(Float,   default=0.0)
    vcr                     = Column(Float,   default=0.0)
    reach                   = Column(Integer, default=0)
    frequency               = Column(Float,   default=0.0)
    leads                   = Column(Integer, default=0)
    lead_conversion_rate    = Column(Float,   default=0.0)
    lead_cpa                = Column(Float,   default=0.0)
    lead_perf_multiplier    = Column(Float,   default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    upload     = relationship("MarketingUpload", backref="ott_campaign_records")

class OttConsultTvMonthlyRecord(Base):
    __tablename__ = "ott_consult_tv_monthly"
    id              = Column(Integer, primary_key=True)
    upload_id       = Column(Integer, ForeignKey("marketing_uploads.id"), nullable=True)
    sheet_name      = Column(String(50),  nullable=True, index=True)  
    advertiser      = Column(String(255), nullable=True)
    campaign_id     = Column(String(50),  nullable=True, index=True)  
    campaign_name   = Column(String(500), nullable=False, index=True)
    market          = Column(String(255), nullable=True, index=True)  
    ad_type         = Column(String(10),  nullable=True, index=True)  
    audience        = Column(String(50),  nullable=True)              
    month           = Column(String(20),  nullable=True, index=True)  
    month_date      = Column(Date,        nullable=True, index=True)  
    completed_views = Column(Integer, default=0)
    video_completion_rate   = Column(Float,   default=0.0)  
    impressions              = Column(Integer, default=0)
    spend                    = Column(Float,   default=0.0)
    cpm                      = Column(Float,   default=0.0)  
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    upload     = relationship("MarketingUpload", backref="ott_consult_tv_monthly_records")

    __table_args__ = (
        UniqueConstraint("campaign_id", "month", name="uq_ott_consult_tv_campaign_month"),
    )

class NeosWbRecord(Base):
    __tablename__ = "neos_wb_records"


    id = Column(Integer, primary_key=True, autoincrement=True)
    case_number = Column(String(32), nullable=False, unique=True)
    record_kind = Column(String(10), nullable=False)          # "intake" | "case" (Neos search object)
    intake_date = Column(Date, nullable=True, index=True)
    date_opened = Column(Date, nullable=True, index=True)
    status = Column(String(120), nullable=True)
    case_type_raw = Column(String(40), nullable=True)
    office_raw = Column(String(120), nullable=True)
    # Effective raw source: the Neos "Referred By" contact name (resolved from
    # referred_by_id), else the Neos Source field, else blank.
    source_raw = Column(String(255), nullable=True, index=True)
    referred_by_id = Column(String(64), nullable=True, index=True)
    last_sync_id = Column(Integer, nullable=True, index=True)
    first_seen_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)




class NeosWbReferrer(Base):
    """Neos "Referred By" contact id -> display name. Resolving a name through the
    Neos API is slow (~2 s each), so each id is looked up once and cached here."""
    __tablename__ = "neos_wb_referrers"


    id = Column(Integer, primary_key=True, autoincrement=True)
    referrer_id = Column(String(64), nullable=False, unique=True)
    name = Column(String(255), nullable=True)
    resolved_at = Column(DateTime, server_default=func.now(), nullable=False)




class NeosWbSourceMap(Base):
    __tablename__ = "neos_wb_source_map"


    id = Column(Integer, primary_key=True, autoincrement=True)
    raw_source = Column(String(255), nullable=False, unique=True)
    clean_source = Column(String(255), nullable=True)
    source_group = Column(String(80), nullable=True)
    status = Column(String(12), nullable=False, default="proposed")   # proposed | approved
    origin = Column(String(20), nullable=True)                        # ai | prototype_seed | manual
    ai_reason = Column(Text, nullable=True)
    reviewed_by = Column(String(255), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)




class NeosWbProviderReview(Base):
    __tablename__ = "neos_wb_provider_reviews"

    id = Column(Integer, primary_key=True, autoincrement=True)
    reviewed_by = Column(String(255), nullable=True)
    provider_count = Column(Integer, nullable=True)
    reviewed_at = Column(DateTime, server_default=func.now(), nullable=False)

class NeosWbReminderSent(Base):
    __tablename__ = "neos_wb_reminders_sent"

    id = Column(Integer, primary_key=True, autoincrement=True)
    period = Column(String(7), nullable=False, unique=True)     # YYYY-MM
    recipients = Column(Text, nullable=True)
    provider_count = Column(Integer, nullable=True)
    sent_at = Column(DateTime, server_default=func.now(), nullable=False)

class NeosWbMonthConfirmation(Base):
    __tablename__ = "neos_wb_month_confirmations"
    id = Column(Integer, primary_key=True, autoincrement=True)
    month = Column(String(7), nullable=False, unique=True)      # YYYY-MM
    confirmed_by = Column(String(255), nullable=True)
    confirmed_at = Column(DateTime, server_default=func.now(), nullable=False)
    intakes = Column(Integer, nullable=True)
    cases = Column(Integer, nullable=True)

class NeosWbImportLog(Base):
    __tablename__ = "neos_wb_import_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime, server_default=func.now(), nullable=False)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(12), nullable=False, default="running")    # running | success | failed
    triggered_by = Column(String(255), nullable=True)
    window_start = Column(Date, nullable=True)
    data_through = Column(Date, nullable=True)
    intake_rows = Column(Integer, nullable=True)
    case_rows = Column(Integer, nullable=True)
    flag_no_status = Column(Integer, nullable=True)
    flag_opened_before_intake = Column(Integer, nullable=True)
    flag_case_no_date_opened = Column(Integer, nullable=True)
    new_unmapped_sources = Column(Integer, nullable=True)
    ai_proposed = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
class PeakTracAccount(Base):
    __tablename__ = "peaktrac_accounts"
    id = Column(Integer, primary_key=True, index=True)
    neos_case_id = Column(String(100), nullable=False, unique=True, index=True)
    case_number = Column(String(100), nullable=True, index=True)
    client_name = Column(String(500), nullable=True)
    phone_number = Column(String(50), nullable=True)  # not supplied by Cases v2 search
    attorney_name = Column(String(255), nullable=True)  # optional later enrichment
    attorney_id = Column(String(100), nullable=True, index=True)
    primary_staff_code = Column(String(100), nullable=True, index=True)
    case_type = Column(String(255), nullable=True)
    case_type_id = Column(String(100), nullable=True, index=True)
    case_status = Column(String(100), nullable=True, index=True)
    case_phase = Column(String(255), nullable=True, index=True)
    practice_area = Column(String(255), nullable=True, index=True)
    first_party_is_our_client = Column(Boolean, nullable=True)
    date_of_incident = Column(Date, nullable=True, index=True)
    date_opened = Column(Date, nullable=True, index=True)
    neos_last_updated_at = Column(DateTime(timezone=True), nullable=True)
    # PT-4: details pulled from Neos at import (see migrations/2026_10_pt4_neos_case_details.sql)
    office = Column(String(255), nullable=True)
    sol_date = Column(Date, nullable=True)
    policy_limit_max = Column(Numeric(14, 2), nullable=True)
    limit_above_policy = Column(Boolean, nullable=False, default=False)
    neos_synced_at = Column(DateTime(timezone=True), nullable=True)
    rpc_1_5_disclosure = Column(Boolean, nullable=False, default=False)
    approved_by = Column(String(255), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    credit_limit = Column(Numeric(12, 2), nullable=False, default=0)
    available_credit = Column(Numeric(12, 2), nullable=False, default=0)
    principal = Column(Numeric(12, 2), nullable=False, default=0)
    accrued_interest = Column(Numeric(12, 2), nullable=False, default=0)
    payoff_amount = Column(Numeric(12, 2), nullable=False, default=0)
    utilization_percent = Column(Numeric(5, 2), nullable=False, default=0)
    interest_rate = Column(Numeric(5, 2), nullable=False, default=8.50)
    total_draws = Column(Integer, nullable=False, default=0)
    settlement_phase = Column(Boolean, nullable=False, default=False, server_default="0")
    closed_unrecovered = Column(Boolean, nullable=False, default=False, server_default="0")
    neos_is_closed = Column(Boolean, nullable=True)
    neos_date_closed = Column(Date, nullable=True)
    neos_settlement_amount = Column(Numeric(14, 2), nullable=True)
    neos_due_to_firm = Column(Numeric(14, 2), nullable=True)
    neos_case_expenses_on_statement = Column(Numeric(14, 2), nullable=True)
    neos_settlement_checked_at = Column(DateTime, nullable=True)
    account_status = Column(String(100), nullable=False, default="Pending Approval", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class NeosExpenseLine(Base):
    __tablename__ = "neos_expense_lines"
    is_current = Column(Boolean, nullable=False, default=True, server_default="1")

    __table_args__ = (
        UniqueConstraint(
            "neos_value_id",
            name="uq_neos_expense_lines_value_id",
        ),
        UniqueConstraint(
            "matched_draw_id",
            name="uq_neos_expense_lines_matched_draw_id",
        ),
        Index(
            "ix_neos_expense_account_code",
            "account_id",
            "code",
        ),
    )

    # Keep your other existing columns unchanged.


    id = Column(Integer, primary_key=True, autoincrement=True)
    neos_value_id = Column(String(128), nullable=False, index=True)

    account_id = Column(
        Integer,
        ForeignKey("peaktrac_accounts.id"),
        nullable=False,
        index=True,
    )

    code_type = Column(String(128), nullable=True)
    code = Column(String(128), nullable=True)
    payee_name = Column(String(512), nullable=True)

    amount = Column(Numeric(14, 2), nullable=False, default=0)

    due = Column(Date, nullable=True)
    service_date = Column(Date, nullable=True)

    neos_created = Column(DateTime(timezone=True), nullable=True)
    neos_modified = Column(DateTime(timezone=True), nullable=True)

    approval_status = Column(String(128), nullable=True)
    billed = Column(String(32), nullable=True)
    reference = Column(String(512), nullable=True)
    matched_draw_id = Column(
        Integer,
        ForeignKey("peaktrac_draws.id"),
        nullable=True,
        index=True,
    )
    match_source = Column(String(10), nullable=True)               # 'auto' | 'manual'
    manual_unlinked = Column(Boolean, nullable=False, default=False, server_default="0")

    last_seen_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
    ) 

class NeosCode(Base):
    __tablename__ = 'peaktrac_neos_codes'
    key = Column(String(160), primary_key=True)
    kind = Column(String(20), nullable=False)
    code = Column(String(128), nullable=False)
    refreshed_at = Column(DateTime, nullable=False)


class CategoryMapping(Base):
    __tablename__ = 'peaktrac_category_mapping'
    category = Column(String(100), primary_key=True)
    neos_code = Column(String(128), nullable=True)
    updated_by = Column(String(255))
    updated_at = Column(DateTime)


class ProjectionInput(Base):
    __tablename__ = 'projection_inputs'
    id = Column(Integer, primary_key=True)
    computed_at = Column(DateTime, nullable=False)
    n_cases = Column(Integer, nullable=False)
    data = Column(JSON, nullable=False)


class PayoffReview(Base):
    __tablename__ = 'peaktrac_payoff_reviews'
    account_id = Column(Integer, primary_key=True)
    note = Column(Text, nullable=False)
    reviewed_by = Column(String(255), nullable=False)
    reviewed_at = Column(DateTime, nullable=False)
    # Bind approval to the exact figures being exported.
    fingerprint = Column(String(64), nullable=False)


class Draw(Base):
    __tablename__ = "peaktrac_draws"
    neos_code = Column(String(128), nullable=True)

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("peaktrac_accounts.id"), nullable=False, index=True)
    payee = Column(String(255), nullable=False)
    category = Column(String(100), nullable=False)
    expense_category = Column(String(100), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    memo = Column(String(500), nullable=True)
    draw_status = Column(String(50), nullable=True, default="Pending Approval")
    requested_by = Column(String(255), nullable=True)
    requested_at = Column(DateTime(timezone=True), server_default=func.now())
    approved_by = Column(String(255), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    denied_reason = Column(String(500), nullable=True)
    funded_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    account = relationship("PeakTracAccount", backref="draws")

class PeakTracHelpReport(Base):
    __tablename__ = "peaktrac_help_reports"

    id = Column(Integer, primary_key=True)
    report_id = Column(String(32), unique=True, nullable=False, index=True)
    conversation_id = Column(String(64), index=True)
    created_at = Column(DateTime, nullable=False, index=True)
    user_email = Column(String(255), index=True)
    category = Column(String(32), index=True)
    severity = Column(String(16), index=True)
    status = Column(String(16), nullable=False, default="new")
    record = Column(JSON, nullable=False)
    
class FirmOperatingDraw(Base):
    __tablename__ = "firm_operating_draws"

    id = Column(Integer, primary_key=True, index=True)
    description = Column(String(500), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    requested_by = Column(String(255), nullable=False)
    approved_by = Column(String(255), nullable=True)
    status = Column(String(100), default="Pending Approval", nullable=False)
    funded_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

# Executive dashboard-
class DashboardRole(Base):
    __tablename__ = "dashboard_roles"

    id = Column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4())
    )
    email = Column(
        String(255),
        unique=True,
        nullable=False,
        index=True
    )
    revenue_access = Column(
        Boolean,
        default=False,
        nullable=False
    )
    attorney_full_access = Column(
        Boolean,
        default=False,
        nullable=False
    )
    created_at = Column(
        DateTime,
        server_default=func.now(),
        nullable=False
    )
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )

class NeosToken(Base):
    __tablename__ = "neos_tokens"

    id = Column(Integer, primary_key=True)
    token = Column(String(5000), nullable=False)
    created_by = Column(String(255), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )

class GoogleOAuthToken(Base):
    __tablename__ = "google_oauth_tokens"

    id = Column(Integer, primary_key=True)

    access_token = Column(Text, nullable=True)
    refresh_token = Column(Text, nullable=True)
    token_url = Column(Text, nullable=True)
    scopes = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    meta_access_token = Column(Text, nullable=True)
    analytic_access_token = Column(Text,nullable=True)
    google_ads_developer_token = Column(Text, nullable=True)

    created_at = Column(
        DateTime,
        server_default=func.now(),
        nullable=False
    )

    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )

# ── Marketing dashboard: Referral Partners tab ─────────────────────────────
class NeosRpCase(Base):
    """Neos cases opened on/after RP_CASES_FROM (2024-01-01), for the Referral Partners tab."""
    __tablename__ = "neos_rp_cases"


    id = Column(Integer, primary_key=True, autoincrement=True)
    case_number = Column(String(32), nullable=False, unique=True)
    date_opened = Column(Date, nullable=True, index=True)
    case_type_raw = Column(String(40), nullable=True)
    is_closed = Column(Boolean, nullable=True)
    referred_by_id = Column(String(64), nullable=True, index=True)   # Neos "Referred By" contact id
    source_raw = Column(String(255), nullable=True)                  # Neos Source field (fallback only)
    # Normalised client last name, used server-side only to verify the ledger join.
    # Never returned by the API (dev note section 6: no client names in this tab).
    client_last = Column(String(120), nullable=True)
    last_sync_id = Column(Integer, nullable=True, index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)




class RpSyncLog(Base):
    __tablename__ = "rp_sync_log"


    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime, server_default=func.now(), nullable=False)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(12), nullable=False, default="running")   # running | success | failed
    triggered_by = Column(String(255), nullable=True)
    case_rows = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)




class RpLedgerUpload(Base):
    """One uploaded QuickBooks Legal Fee Income (401xx) detail export."""
    __tablename__ = "rp_ledger_uploads"


    id = Column(Integer, primary_key=True, autoincrement=True)
    file_name = Column(String(255), nullable=False)
    uploaded_by = Column(String(255), nullable=True)
    date_from = Column(Date, nullable=True)
    date_to = Column(Date, nullable=True)
    row_count = Column(Integer, nullable=True)
    total_amount = Column(Numeric(14, 2), nullable=True)
    replaced_rows = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)




class RpLedgerRow(Base):
    __tablename__ = "rp_ledger_rows"


    id = Column(Integer, primary_key=True, autoincrement=True)
    upload_id = Column(Integer, ForeignKey("rp_ledger_uploads.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    account = Column(String(255), nullable=True)       # e.g. "40105 · Legal Fee Income - B Calandra"
    attorney = Column(String(120), nullable=True)      # fee-account attorney, from the account name
    txn_type = Column(String(60), nullable=True)
    txn_date = Column(Date, nullable=True, index=True)
    num = Column(String(60), nullable=True)
    case_number = Column(String(32), nullable=True, index=True)
    name_last = Column(String(120), nullable=True)     # normalised client last name (join check only)
    debit = Column(Numeric(14, 2), nullable=True)
    credit = Column(Numeric(14, 2), nullable=True)
    amount = Column(Numeric(14, 2), nullable=False)    # credit - debit




class RpPartnerFlag(Base):
    """Confirmed internal / not-internal flag on a partner; overrides the name-match guess."""
    __tablename__ = "rp_partner_flags"


    id = Column(Integer, primary_key=True, autoincrement=True)
    partner = Column(String(255), nullable=False, unique=True)
    internal = Column(Boolean, nullable=False)
    set_by = Column(String(255), nullable=True)
    set_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

Base.metadata.create_all(bind=engine)


async def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def log_prompt_and_update_activity(
    db: Session,
    project_name: str,
    user_email: str,
    prompt_text: str,
    prompt_type: str,
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    input_cost_usd: float | None = None,
    output_cost_usd: float | None = None,
    response_feedback: str | None = None,
    
):
    prompt_log = PromptLog(
        project_name=project_name,
        user_email=user_email,
        prompt_text=prompt_text,
        prompt_type = prompt_type,
        model = model,        
        input_tokens = input_tokens,
        output_tokens = output_tokens,
        input_cost_usd = input_cost_usd,
        output_cost_usd = output_cost_usd,
        response_feedback=response_feedback  
    )
    db.add(prompt_log)
    db.flush() # Get the prompt_log.id before commit

    session = db.query(ActiveSession).filter(
        ActiveSession.project_name == project_name,
        ActiveSession.user_email == user_email
    ).first()

    if session:
        session.last_activity_timestamp = datetime.utcnow()
    else:
        session = ActiveSession(
            project_name=project_name,
            user_email=user_email,
            session_id=str(uuid.uuid4()),
            device_id="unknown"
        )
        db.add(session)

    db.commit()
    db.refresh(prompt_log)
    return prompt_log 


async def log_api_error(
    db: Session,
    *,
    tool: str,
    api: str,
    error_type: str,
    category: str,
    message: str | None = None,
    stack_trace: str | None = None,
    status_code: int | None = None,
    request_id: str | None = None
):
    error = ApiErrorLog(
        tool=tool,
        api=api,
        error_type=error_type,
        category=category,
        message=message,
        stack_trace=stack_trace,
        status_code=status_code,
        request_id=request_id
    )

    db.add(error)
    db.commit()

async def save_chat_history(db, email, question, answer):

    # insert new record
    new_chat = ChatHistoryPlaybook(
        user_email=email,
        question=question,
        answer=answer
    )

    db.add(new_chat)
    db.commit()

    # fetch chats for this user
    chats = (
        db.query(ChatHistoryPlaybook)
        .filter(ChatHistoryPlaybook.user_email == email)
        .order_by(ChatHistoryPlaybook.created_at.desc())
        .all()
    )

    # if more than 3 → delete oldest
    if len(chats) > 3:
        for chat in chats[3:]:
            db.delete(chat)

        db.commit()

def save_neos_token(
    db: Session,
    token: str,
    user_email: str | None = None
):
    row = db.query(NeosToken).first()

    if row:
        row.token = token
        row.created_by = user_email
    else:
        row = NeosToken(
            token=token,
            created_by=user_email
        )
        db.add(row)

    db.commit()
    db.refresh(row)

    return row


def get_neos_token(db: Session):
    return db.query(NeosToken).first()


def delete_neos_token(db: Session):
    row = db.query(NeosToken).first()

    if row:
        db.delete(row)
        db.commit()