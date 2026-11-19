# app.py
from fastapi import FastAPI, HTTPException, Form, Depends
from fastapi.routing import APIRoute
from sqlalchemy import func, distinct, cast, Date, text, select
from sqlalchemy.orm import Session
from db import SessionLocal
from datetime import datetime, timedelta, date
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from casevalue.router import router as casevalue_router
from VICP_Attorney_Fees.router import router as VICP_Attorney_Fees
from Settlement_Predictor.router import router as Settlement_Predictor
from Injury_Intelligence.router import router as Injury_Intelligence
from Demand_optimizer.router import router as Demand_optimizer
from Demand_qa.router import router as Demand_Quality_Assurance
from Vehicle_loss_Analyzer.router import router as Vehicle_Loss_Analyzer
from Playbook_chatbot.router import router as chatbot_router
# from email2ai.router import router as Email2AI_router
import asyncio
from pathlib import Path
from fastapi.staticfiles import StaticFiles
# from email2ai.router import check_mailbox
from VICP_Prediction.router import router as VICP_Case_Predictor
from FAA_MedCert_Case_Builder.router import router as FAA_MedCert_Case_Builder
from agentic_book_builder.router import router as bookbuilder
from MVA_Platform.router import router as MVA_Lead
from MVA_Platform.outlook_utils import get_latest_leads, neos_daily_status 
from deadlineops.router import router as deadlineops_router
from attorney_tracker.router import router as attorney_tracker_router
from ramoslaw_chatgpt.router import router as ramoslaw_chatgpt_router
# from evidenceflow.router import router as evidenceflow_router
from Cs_dashboard.router import router as cs_dashboard_router
from marketing_dashboard.router import router as marketing_router
from executive_dashboard.router import router as executive_router
from intake_manager.router import router as intake_manager
# from casevalue_tool.router import router as casevalue_tool 
from MVA_Platform.owl_service import owl_daily_review

from error_tracker import (
    unhandled_exception_handler,
    http_exception_handler,
    validation_exception_handler,
    starlette_exception_handler
)
from admin.router import admin_router
from admin.auth import (
    verify_admin_credentials,
    create_admin_token
)
from db import (
    get_db,ActiveSession,PromptLog,LeadIntake,MarketingTraffic, MetaAdsRecord,AttorneyTrackerRecord,AttorneyQuickBookUpload,ComputedDeadline,Matter,ChatHistoryPlaybook, ExitInterview
    )
from PeakTrac.router import router as peaktrac

app = FastAPI(title="LxIntel") 

# Mount static files for casevalue
CASEVALUE_STATIC = Path(__file__).parent / "casevalue" / "static"
app.mount("/casevalue/static", StaticFiles(directory=str(CASEVALUE_STATIC)), name="casevalue_static")

@app.get("/")
async def home():
    return FileResponse("static/index.html")      

@app.get("/adminpanel")
async def admin_panel():
    return FileResponse("static/admin_panel.html")

@app.post("/admin/login")
async def admin_login(
    email: str = Form(...),
    password: str = Form(...)
):
    if not await verify_admin_credentials(email, password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = await create_admin_token()
    return {"access_token": token, "token_type": "bearer"}


@app.get("/editorpanel")
async def admin_panel():
    return FileResponse("static/editors_login.html")

app.include_router(admin_router)
app.include_router(casevalue_router , prefix="/casevalue")
app.include_router(VICP_Attorney_Fees , prefix="/VICP_Attorney_Fees")
app.include_router(Settlement_Predictor , prefix="/Settlement_Predictor")
app.include_router(Injury_Intelligence , prefix="/Injury_Intelligence")
app.include_router(Demand_optimizer , prefix="/Demand_optimizer")
app.include_router(Demand_Quality_Assurance , prefix="/Demand_Quality_Assurance")
app.include_router(Vehicle_Loss_Analyzer , prefix="/Vehicle_Loss_Analyzer")
app.include_router(chatbot_router , prefix="/Playbook_Chatbot")
# app.include_router(Email2AI_router , prefix="/Email2AI")
app.include_router(VICP_Case_Predictor, prefix="/vicp_prediction")
app.include_router(FAA_MedCert_Case_Builder, prefix="/FAA_MedCert_Case_Builder")
app.include_router(bookbuilder, prefix="/bookbuilder")
app.include_router(MVA_Lead, prefix="/intakeops")
app.include_router(deadlineops_router, prefix="/deadlineops")
app.include_router(attorney_tracker_router, prefix="/attorney_tracker")
app.include_router(ramoslaw_chatgpt_router, prefix="/ramoslaw_chatgpt")
# app.include_router(evidenceflow_router, prefix="/evidenceflow")
app.include_router(cs_dashboard_router, prefix="/csdashboard")
app.include_router(marketing_router, prefix="/marketing_dashboard")
app.include_router(executive_router, prefix="/executive_dashboard")
app.include_router(intake_manager, prefix="/intake_manager") 
# app.include_router(casevalue_tool, prefix="/casevalue_tool")
app.include_router(peaktrac, prefix="/peaktrac")

# error handlers
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(StarletteHTTPException, starlette_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # allow_origins=[
    #     "https://medicalsummarytool.lxintel.net",
    #     "https://lxintel.net",
    #     "https://www.lxintel.net",
    # ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# @app.on_event("startup")
# async def start_email_listener():
#     print("Starting Email2AI mailbox listener...")
#     asyncio.create_task(check_mailbox())

# @app.on_event("startup")
# async def start_email_worker():
#     print("Starting Outlook mailbox for new leads")
#     asyncio.create_task(get_latest_leads())

# @app.on_event("startup")
# async def start_neos_worker():
#     print("Starting Midnight Neos Worker...")
#     asyncio.create_task(neos_daily_status())

# @app.on_event("startup")
# async def start_owl_worker():
#     print("Starting Owl scheduled review worker...")
#     asyncio.create_task(owl_daily_review())

@app.get("/operational_dashboard")
async def admin_panel():
    return FileResponse("static/operational_dashboard.html")

@app.get("/debug/routes")
async def debug_routes():
    return sorted([r.path for r in app.routes if isinstance(r, APIRoute)])

@app.get("/health")
async def health_check():

    health_status = {
        "status": "healthy",
        "project": "LxIntel",
        "timestamp": datetime.utcnow().isoformat(),
        "checks": {}
    }
    # DATABASE CHECK
    try:
        db = SessionLocal()

        db.execute(text("SELECT 1"))

        health_status["checks"]["database"] = {
            "status": "healthy"
        }

    except Exception as e:

        health_status["status"] = "unhealthy"

        health_status["checks"]["database"] = {
            "status": "unhealthy",
            "error": str(e)
        }

    finally:
        db.close()

    # ROUTER CHECK
    routers = {
        "casevalue": "/casevalue/",
        "VICP_Attorney_Fees": "/VICP_Attorney_Fees/",
        "Settlement_Predictor": "/Settlement_Predictor/",
        "injury_intelligence": "/Injury_Intelligence/",
        "Demand_optimizer": "/Demand_optimizer/",
        "Demand_Quality_Assurance": "/Demand_Quality_Assurance/",
        "vehicle_loss_analyzer": "/Vehicle_Loss_Analyzer/",
        "playbook_chatbot": "/Playbook_Chatbot/",
        "VICP_Case_Predictor": "/vicp_prediction/",
        "faa_medcert": "/FAA_MedCert_Case_Builder/",
        "intakeops": "/intakeops/",
        "deadlineops": "/deadlineops/",
        "bookbuilder":"/bookbuilder/",
        "ramoslaw_chatgpt": "/ramoslaw_chatgpt/",
        "attorney_tracker": "/attorney_tracker/",
        "marketing_dashboard": "/marketing_dashboard/",
        "executive_dashboard": "/executive_dashboard/",
        "csdashboard": "/csdashboard/"
    }

    for router_name, router_prefix in routers.items():

        route_exists = any(
            isinstance(route, APIRoute)
            and route.path.startswith(router_prefix)
            for route in app.routes
        )

        health_status["checks"][router_name] = {
            "status": "healthy" if route_exists else "unhealthy"
        }

    # FRONTEND FILE CHECK
    frontend_files = {
        "main_home": "static/index.html",
        "admin_panel": "static/admin_panel.html",
        "editor_login": "static/editors_login.html",
        "casevalue": "casevalue/static/index.html",
        "VICP_Attorney_Fees": "VICP_Attorney_Fees/templates/index.html",
        "Settlement_Predictor": "Settlement_Predictor/templates/index.html",
        "injury_intelligence": "Injury_Intelligence/templates/index.html",
        "Demand_optimizer": "Demand_optimizer/templates/index.html",
        "Demand_Quality_Assurance": "Demand_qa/templates/index.html",
        "vehicle_loss_analyzer": "Vehicle_loss_Analyzer/templates/index.html",
        "playbook_chatbot": "Playbook_chatbot/templates/index.html",
        "VICP_Case_Predictor": "VICP_Prediction/templates/index.html",
        "faa_medcert": "FAA_MedCert_Case_Builder/templates/index.html",
        "intakeops": "MVA_Platform/templates/index.html",
        "deadlineops": "deadlineops/templates/index.html",   
        "bookbuilder": "agentic_book_builder/templates/index.html",
        "ramoslaw_chatgpt": "ramoslaw_chatgpt/templates/index.html",
        "attorney_tracker": "attorney_tracker/templates/index.html",
        "marketing_dashboard": "marketing_dashboard/templates/index.html",
        "executive_dashboard": "executive_dashboard/templates/index.html",
        "csdashboard": "Cs_dashboard/templates/index.html"
    }

    frontend_results = {}

    for name, file_path in frontend_files.items():
        file_exists = Path(file_path).exists()
        frontend_results[name] = {
            "file": file_path,
            "status": "healthy" if file_exists else "unhealthy"
        }

    health_status["checks"]["frontend_files"] = frontend_results

    return health_status

# operations dashboard
def _today() -> date:
    return datetime.utcnow().date()

def _mtd_start() -> date:
    d = _today()
    return d.replace(day=1)

def _last_30_start() -> date:
    return _today() - timedelta(days=29)

def _current_quarter_start() -> date:
    """Returns the first day of the current calendar quarter."""
    d = _today()
    quarter_month_start = ((d.month - 1) // 3) * 3 + 1   # 1 → Jan, 4 → Apr, 7 → Jul, 10 → Oct
    return d.replace(month=quarter_month_start, day=1)

def _current_quarter_label() -> str:
    """Returns e.g. '2026Q2'."""
    d = _today()
    q = (d.month - 1) // 3 + 1
    return f"{d.year}Q{q}"
 
  
@app.get("/summary")
def overview_summary(db: Session = Depends(get_db)):
    today      = _today()
    mtd_start  = _mtd_start()
    l30_start  = _last_30_start()
 
    active_tools = [
        "IntakeOps", "Marketing Dashboard", "Executive Dashboard",
        "Ramos Law Chatbot", "Playbook Chatbot", "Attorney Tracker",
        "DeadlineOps", "Client Services Dashboard",
    ]
 
    active_users_today: int = (
        db.query(func.count(distinct(ActiveSession.user_email)))
        .filter(
            cast(ActiveSession.last_activity_timestamp, Date) == today
        )
        .scalar() or 0
    )
 
    intakeops_cases_mtd: int = (
        db.query(func.count(LeadIntake.id))
        .filter(
            func.date(LeadIntake.received_at) >= mtd_start,
            func.date(LeadIntake.received_at) <= today,
        )
        .scalar() or 0
    )
 
    intakeops_active_users: int = (
        db.query(func.count(distinct(ActiveSession.user_email)))
        .filter(
            ActiveSession.project_name.ilike("%intakeops%"),
            cast(ActiveSession.last_activity_timestamp, Date) == today,
        )
        .scalar() or 0
    )
 
    current_q_start = _current_quarter_start()
    current_q_label = _current_quarter_label()

    marketing_sessions_q: int = (
        db.query(func.coalesce(func.sum(MarketingTraffic.sessions), 0))
        .filter(
            func.str_to_date(
                func.substring(MarketingTraffic.date_hour, 1, 8), "%Y%m%d"
            ) >= current_q_start
        )
        .scalar() or 0
    )

    marketing_conversions_q: int = (
        db.query(func.coalesce(func.sum(MarketingTraffic.key_events), 0))
        .filter(
            func.str_to_date(
                func.substring(MarketingTraffic.date_hour, 1, 8), "%Y%m%d"
            ) >= current_q_start
        )
        .scalar() or 0
    )

    current_q_num = (current_q_start.month - 1) // 3 + 1
    meta_q_spend: float = (
        db.query(func.coalesce(func.sum(MetaAdsRecord.amount_spent_usd), 0.0))
        .filter(
            func.year(MetaAdsRecord.reporting_starts) == current_q_start.year,
            func.month(MetaAdsRecord.reporting_starts) >= current_q_start.month,
            func.month(MetaAdsRecord.reporting_starts) <= current_q_start.month + 2,
        )
        .scalar() or 0.0
    )
    
    approved_upload_ids = (
    select(AttorneyQuickBookUpload.id)
    .filter(AttorneyQuickBookUpload.approved_at.isnot(None))
    )
 
    exec_settlements: int = (
        db.query(func.count(AttorneyTrackerRecord.id))
        .filter(
            AttorneyTrackerRecord.upload_id.in_(approved_upload_ids),
            AttorneyTrackerRecord.settlement_value.isnot(None),
        )
        .scalar() or 0
    )
 
    exec_settlement_value: float = (
        db.query(func.coalesce(func.sum(AttorneyTrackerRecord.settlement_value), 0.0))
        .filter(AttorneyTrackerRecord.upload_id.in_(approved_upload_ids))
        .scalar() or 0.0
    )
 
    exec_attorneys: int = (
        db.query(func.count(distinct(AttorneyTrackerRecord.attorney_name)))
        .filter(AttorneyTrackerRecord.upload_id.in_(approved_upload_ids))
        .scalar() or 0
    )
 
    ramos_convos_today: int = (
        db.query(func.count(PromptLog.id))
        .filter(
            PromptLog.project_name.ilike("%ramos%"),
            cast(PromptLog.timestamp, Date) == today,
        )
        .scalar() or 0
    )
 
    ramos_convos_mtd: int = (
        db.query(func.count(PromptLog.id))
        .filter(
            PromptLog.project_name.ilike("%ramos%"),
            cast(PromptLog.timestamp, Date) >= mtd_start,
        )
        .scalar() or 0
    )
 
    ramos_avg_session = None
 
    playbook_queries_today: int = (
        db.query(func.count(ChatHistoryPlaybook.id))
        .filter(
            cast(ChatHistoryPlaybook.created_at, Date) == today,
        )
        .scalar() or 0
    )
 
    playbook_queries_mtd: int = (
        db.query(func.count(ChatHistoryPlaybook.id))
        .filter(
            cast(ChatHistoryPlaybook.created_at, Date) >= mtd_start,
        )
        .scalar() or 0
    )
 
    playbook_active_users: int = (
        db.query(func.count(distinct(ActiveSession.user_email)))
        .filter(
            ActiveSession.project_name.ilike("%playbook%"),
            cast(ActiveSession.last_activity_timestamp, Date) == today,
        )
        .scalar() or 0
    )
 
    playbook_documents: int = (
        db.query(func.count(distinct(PromptLog.prompt_type)))
        .filter(PromptLog.project_name.ilike("%playbook%"))
        .scalar() or 0
    )
 
    at_settlements: int = (
        db.query(func.count(AttorneyTrackerRecord.id))
        .filter(
            AttorneyTrackerRecord.upload_id.in_(approved_upload_ids),
            AttorneyTrackerRecord.settlement_value.isnot(None),
        )
        .scalar() or 0
    )
 
    at_total_value: float = (
        db.query(func.coalesce(func.sum(AttorneyTrackerRecord.settlement_value), 0.0))
        .filter(AttorneyTrackerRecord.upload_id.in_(approved_upload_ids))
        .scalar() or 0.0
    )
 
    at_avg_settlement: float = (
        db.query(func.avg(AttorneyTrackerRecord.settlement_value))
        .filter(
            AttorneyTrackerRecord.upload_id.in_(approved_upload_ids),
            AttorneyTrackerRecord.settlement_value.isnot(None),
        )
        .scalar() or 0.0
    )
 
    week_end = today + timedelta(days=7)
 
    open_deadlines: int = (
        db.query(func.count(ComputedDeadline.id))
        .filter(
            ComputedDeadline.is_approved == False,
            ComputedDeadline.due_date >= today,
        )
        .scalar() or 0
    )
 
    due_this_week: int = (
        db.query(func.count(ComputedDeadline.id))
        .filter(
            ComputedDeadline.due_date >= today,
            ComputedDeadline.due_date <= week_end,
        )
        .scalar() or 0
    )
 
    cases_tracked: int = (
        db.query(func.count(distinct(Matter.id)))
        .scalar() or 0
    )
 
   # Cases Closed MTD (Using Exit Interviews submitted this month)
    cs_cases_closed_mtd = (
        db.query(func.count(ExitInterview.id))
        .filter(
            cast(ExitInterview.submitted_at, Date) >= mtd_start,
            cast(ExitInterview.submitted_at, Date) <= today,
        )
        .scalar() or 0
    )

    # Average Client Rating (Replacement for Avg Closeout Time)
    cs_avg_closeout_time = (
        db.query(func.avg(ExitInterview.rating_overall))
        .filter(
            cast(ExitInterview.submitted_at, Date) >= mtd_start,
            cast(ExitInterview.submitted_at, Date) <= today,
        )
        .scalar() or 0.0
    )

    cs_avg_closeout_time = round(float(cs_avg_closeout_time), 2)

    # Pending Google Reviews (Review not yet sent)
    cs_pending_closure = (
        db.query(func.count(ExitInterview.id))
        .filter(
            ExitInterview.review_sent == False
        )
        .scalar() or 0
    )
 
    daily_active_users_rows = (
        db.query(
            cast(ActiveSession.last_activity_timestamp, Date).label("day"),
            func.count(distinct(ActiveSession.user_email)).label("users"),
        )
        .filter(
            cast(ActiveSession.last_activity_timestamp, Date) >= l30_start,
        )
        .group_by(cast(ActiveSession.last_activity_timestamp, Date))
        .order_by(cast(ActiveSession.last_activity_timestamp, Date))
        .all()
    )
 
    daily_map = {str(row.day): row.users for row in daily_active_users_rows}
    daily_active_users = []
    for i in range(30):
        d = l30_start + timedelta(days=i)
        daily_active_users.append({"date": str(d), "users": daily_map.get(str(d), 0)})
 
    return {
        "generated_at": datetime.utcnow().isoformat(),
 
        # Hero KPIs
        "active_tools_count": len(active_tools),
        "active_tools": active_tools,
        "active_users_today": active_users_today,
 
        # All Tools — Status Snapshot
        "tools": {
            "intakeops": {
                "label": "IntakeOps",
                "cases_mtd": intakeops_cases_mtd,
                "active_users": intakeops_active_users,
            },
            "marketing_dashboard": {
                "label": "Marketing Dashboard",
                "quarter": current_q_label,               # e.g. "2026Q2" — dynamic
                "sessions_q": int(marketing_sessions_q),
                "conversions_q": int(marketing_conversions_q),
                "meta_spend_usd": round(float(meta_q_spend), 2),
            },
            "executive_dashboard": {
                "label": "Executive Dashboard",
                "settlements": exec_settlements,
                "settlement_value_usd": round(float(exec_settlement_value), 2),
                "attorneys": exec_attorneys,
            },
            "ramos_law_chatbot": {
                "label": "Ramos Law (chatbot)",
                "convos_today": ramos_convos_today,
                "convos_mtd": ramos_convos_mtd,
                "avg_session_minutes": ramos_avg_session,  # null — pending auth integration
            },
            "playbook_chatbot": {
                "label": "Playbook Chatbot",
                "queries_today": playbook_queries_today,
                "queries_mtd": playbook_queries_mtd,
                "documents": playbook_documents,
                "active_users": playbook_active_users,
            },
            "attorney_tracker": {
                "label": "Attorney Tracker",
                "settlements": at_settlements,
                "total_value_usd": round(float(at_total_value), 2),
                "avg_settlement_usd": round(float(at_avg_settlement), 2),
            },
            "deadlineops": {
                "label": "DeadlineOps",
                "open_deadlines": open_deadlines,
                "due_this_week": due_this_week,
                "cases_tracked": cases_tracked,
            },
            "client_services_dashboard": {
                "label": "Client Services Dashboard",
                "cases_closed_mtd": cs_cases_closed_mtd,
                "avg_closeout_time": cs_avg_closeout_time,
                "pending_closure": cs_pending_closure            
            },
        },
 
        # Chart — Active Users Daily (Last 30 Days)
        "active_users_daily": daily_active_users,
    } 

@app.get("/active_user")
async def get_active_users(db: Session = Depends(get_db)):
    active_users = db.query(ActiveSession).all()

    return {
        "success": True,
        "count": len(active_users),
        "data": [
            {
                "id": user.id,
                "project_name": user.project_name,
                "user_email": user.user_email,
                "last_activity_timestamp": user.last_activity_timestamp
            }
            for user in active_users
        ],
    }