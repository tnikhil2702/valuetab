# marketing_dashboard/router.py
import re
import os
import requests
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Request, Body
from db import (get_db, Session, MarketingUpload, ActiveSession, Whitelist, OttAllCampaignsRecord, OttCampaignRecord, OttConsultTvMonthlyRecord,GoogleOAuthToken)
from typing import Optional, Literal
import pandas as pd
import numpy as np
from fastapi.security import OAuth2PasswordRequestForm
from auth_utils import (authenticate_user, create_access_token, ACCESS_TOKEN_EXPIRE_MINUTES,get_current_user, reset_password, generate_otp, verify_otp, SessionLocal, create_password,)
from datetime import timedelta, datetime, date
from sqlalchemy import func
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv
import base64
import json
from marketing_dashboard.generate_token import get_valid_access_token, get_auth_url, exchange_code_for_tokens, NeedsReAuth
from collections import defaultdict
import asyncio
import base64
from datetime import timedelta, datetime, date
from openai import AsyncOpenAI
from fastapi.responses import RedirectResponse

load_dotenv()

router = APIRouter()
PROJECT_NAME = "Marketing_Dashboard"

def _safe_float(val, default: float = 0.0) -> float:
    try:
        v = float(pd.to_numeric(val, errors="coerce") or default)
        return v if not np.isnan(v) else default
    except Exception:
        return default

def _safe_int(val, default: int = 0) -> int:
    try:
        return int(pd.to_numeric(val, errors="coerce") or default)
    except Exception:
        return default

def _safe_date(val) -> Optional[date]:
    try:
        d = pd.to_datetime(val, errors="coerce")
        return d.date() if pd.notna(d) else None
    except Exception:
        return None

def _raise_for_google_status(response: requests.Response, context: str) -> None:
    """
    Inspect a Google API response.
    - 401 → hint the frontend to redirect to /auth/google/url.
    - Any other non-200 → propagate as-is.
    """
    if response.status_code == 401:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "google_token_expired",
                "message": (
                    f"Google access token rejected by {context}. "
                    "Please re-authenticate via GET /auth/google/url."
                ),
                "reauth_url": "/auth/google/url",
            },
        )
    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail={"context": context, "error": response.json()},
        )

def _ingest_ott_all(file_obj, upload: MarketingUpload, db: Session) -> int:
    df = pd.read_excel(file_obj, sheet_name=0, header=7)
    df.columns = df.columns.str.strip()
    records = [
        OttAllCampaignsRecord(
            upload_id=upload.id,
            day=_safe_date(row.get("Day")),
            spend=_safe_float(row.get("Spend")),
            impressions=_safe_int(row.get("Impressions")),
            clicks=_safe_int(row.get("Clicks")),
            cpm=_safe_float(row.get("CPM")),
            ctr=_safe_float(row.get("CTR")),
            completions=_safe_int(row.get("Completions")),
            vcr=_safe_float(row.get("VCR")),
            reach=_safe_int(row.get("Reach")),
            frequency=_safe_float(row.get("Frequency")),
            leads=_safe_int(row.get("Lead")),
            lead_conversion_rate=_safe_float(row.get("(%)Lead Conversion Rate")),
            lead_cpa=_safe_float(row.get("($)Lead CPA")),
            lead_perf_multiplier=_safe_float(row.get("Lead Performance Multiplier")),
            lead_time_to_convert_hrs=_safe_float(row.get("Lead Time to Convert (Hrs)")),
            lead_total_exposures=_safe_float(row.get("Lead Total Exposures")),
            page_visits=_safe_int(row.get("Page Visit")),
            page_visit_conv_rate=_safe_float(row.get("(%)Page Visit Conversion Rate")),
            page_visit_cpa=_safe_float(row.get("($)Page Visit CPA")),
            page_visit_perf_multiplier=_safe_float(row.get("Page Visit Performance Multiplier")),
            page_visit_time_to_convert=_safe_float(row.get("Page Visit Time to Convert (Hrs)")),
            page_visit_total_exposures=_safe_float(row.get("Page Visit Total Exposures")),
        )
        for _, row in df.iterrows()
    ]
    db.bulk_save_objects(records)
    db.commit()
    return len(records)


def _ingest_ott_campaign(file_obj, upload: MarketingUpload, db: Session, campaign_name: str) -> int:
    df = pd.read_excel(file_obj, sheet_name=0, header=7)
    df.columns = df.columns.str.strip()
    records = [
        OttCampaignRecord(
            upload_id=upload.id,
            campaign_name=campaign_name,
            media_type=str(row.get("Media Type", "")).strip() or None,
            creative=str(row.get("Creative", "")).strip() or None,
            publisher=str(row.get("Publisher", "")).strip() or None,
            audience=str(row.get("Audience", "")).strip() or None,
            ad_group=str(row.get("Ad Group", "")).strip() or None,
            device_type=str(row.get("Device Type", "")).strip() or None,
            state=str(row.get("State", "")).strip() or None,
            dma=str(row.get("DMA", "")).strip() or None,
            zip_code=str(row.get("Zip Code", "")).strip() or None,
            day=_safe_date(row.get("Day")),
            spend=_safe_float(row.get("Spend")),
            impressions=_safe_int(row.get("Impressions")),
            clicks=_safe_int(row.get("Clicks")),
            cpm=_safe_float(row.get("CPM")),
            ctr=_safe_float(row.get("CTR")),
            vcr=_safe_float(row.get("VCR")),
            reach=_safe_int(row.get("Reach")),
            frequency=_safe_float(row.get("Frequency")),
            leads=_safe_int(row.get("Lead")),
            lead_conversion_rate=_safe_float(row.get("(%)Lead Conversion Rate")),
            lead_cpa=_safe_float(row.get("($)Lead CPA")),
            lead_perf_multiplier=_safe_float(row.get("Lead Performance Multiplier")),
        )
        for _, row in df.iterrows()
    ]
    db.bulk_save_objects(records)
    db.commit()
    return len(records)

_OTT_CAMPAIGN_SUFFIX_RE = re.compile(r"\s+Q\d{1,2}(?:26)?\s+(?P<audience>Hispanic\s+)?(?P<ad_type>OLV|CTV)\s*$",re.IGNORECASE,)

def _parse_ott_campaign_name(advertiser: str, name: str) -> dict:
    campaign = str(name or "").strip()
    adv = str(advertiser or "").strip()

    m = _OTT_CAMPAIGN_SUFFIX_RE.search(campaign)
    if not m:
        return {"market": None, "ad_type": None, "audience": "General"}

    market = campaign[: m.start()].strip()
    if adv and market.lower().startswith(adv.lower()):
        market = market[len(adv):].strip(" -")

    return {"market": market or None,"ad_type": m.group("ad_type").upper(),"audience": "Hispanic" if m.group("audience") else "General",}

def _month_label_to_date(month_str: str) -> Optional[date]:
    s = str(month_str or "").strip()
    if not s or s.lower() == "nan":
        return None
    for fmt in ("%b-%y", "%b%y", "%b-%Y", "%B-%y", "%B %Y", "%b %Y"):
        try:
            return datetime.strptime(s, fmt).date().replace(day=1)
        except ValueError:
            continue
    d = _safe_date(s)
    return d.replace(day=1) if d else None

def _ingest_ott_consult_tv(file_obj, upload: MarketingUpload, db: Session) -> int:
    sheets = pd.read_excel(file_obj, sheet_name=None) 
    count = 0
    for sheet_name, df in sheets.items():
        if df is None or df.empty:
            continue
        df.columns = df.columns.astype(str).str.strip()

        for _, row in df.iterrows():
            campaign_name = str(row.get("Campaign", "")).strip()
            if not campaign_name or campaign_name.lower() == "nan":
                continue

            campaign_id = str(row.get("CampaignId", "")).strip()
            campaign_id = campaign_id if campaign_id and campaign_id.lower() != "nan" else None

            month_val = str(row.get("Month", "")).strip()
            month_val = month_val if month_val and month_val.lower() != "nan" else sheet_name

            impressions = _safe_int(row.get("Impressions"))
            spend = _safe_float(row.get("Spend"))
            cpm = _safe_float(row.get("CPM"))
            if not cpm and impressions:
                cpm = round(spend / impressions * 1000, 2)

            advertiser_val = str(row.get("Advertiser", "")).strip() or None
            parsed = _parse_ott_campaign_name(advertiser_val, campaign_name)

            values = dict(
                upload_id=upload.id,
                sheet_name=sheet_name,
                advertiser=advertiser_val,
                campaign_id=campaign_id,
                campaign_name=campaign_name,
                market=parsed["market"],
                ad_type=parsed["ad_type"],
                audience=parsed["audience"],
                month=month_val,
                month_date=_month_label_to_date(month_val),
                completed_views=_safe_int(row.get("CompletedViews")),
                video_completion_rate=_safe_float(row.get("VideoCompletionRate")),
                impressions=impressions,
                spend=spend,
                cpm=cpm,
            )

            existing = None
            if campaign_id:
                existing = (
                    db.query(OttConsultTvMonthlyRecord)
                    .filter(
                        OttConsultTvMonthlyRecord.campaign_id == campaign_id,
                        OttConsultTvMonthlyRecord.month == month_val,
                    )
                    .first()
                )

            if existing:
                for k, v in values.items():
                    setattr(existing, k, v)
            else:
                db.add(OttConsultTvMonthlyRecord(**values))

            count += 1

    db.commit()
    return count

def _pct_chg(current, prior):
    if prior in (None, 0):
        return None
    return round((current - prior) / prior * 100, 1)

def _delta(current, prior):
    if current is None or prior is None:
        return None
    return round(current - prior, 4)

def _month_bounds_for(start_date: str | None, end_date: str | None):
    from datetime import date
    try:
        s = date.fromisoformat(start_date)
        e = date.fromisoformat(end_date)
    except Exception:
        today = date.today()
        s = today - timedelta(days=30)
        e = today
    delta_days = (e - s).days or 1
    prior_end   = s - timedelta(days=1)
    prior_start = prior_end - timedelta(days=delta_days - 1)
    return prior_start.isoformat(), prior_end.isoformat()

jinja_templates = Jinja2Templates(directory="marketing_dashboard/templates")
#  HOME
@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return jinja_templates.get_template("index.html").render({"request": request})

#  AUTH 
@router.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = await authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    access_token = await create_access_token(
        data={"sub": user["email"], "role": user["role"]},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    active = db.query(ActiveSession).filter(
        ActiveSession.project_name == PROJECT_NAME,
        ActiveSession.user_email == user["email"],
    ).first()
    if active:
        active.last_activity_timestamp = datetime.utcnow()
    else:
        db.add(ActiveSession(
            project_name=PROJECT_NAME,
            user_email=user["email"],
            last_activity_timestamp=datetime.utcnow(),
        ))
    db.commit()
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/verify-email")
async def verify_email(email: str):
    db = SessionLocal()
    try:
        user = db.query(Whitelist).filter(Whitelist.email == email.lower()).first()
        if not user:
            raise HTTPException(status_code=404, detail="Email not authorized")
        return {"valid": True, "password_created": bool(user.password_hash), "role": user.role}
    finally:
        db.close()


@router.post("/send/otp")
async def send_otp_api(email: str):
    return await generate_otp(email)

@router.post("/verify/otp")
async def verify_otp_api(email: str, otp: str):
    await verify_otp(email, otp)
    return {"message": "OTP verified successfully"}

@router.post("/create/password")
async def create_password_api(email: str, password: str):
    return await create_password(email, password)

@router.post("/forgot/password")
async def forgot_password(email: str):
    return await generate_otp(email)

@router.post("/reset/password")
async def reset_password_api(email: str, otp: str, new_password: str):
    return await reset_password(email, otp, new_password)

@router.get("/active-today")
async def active_today(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    today = datetime.utcnow().date()
    active = db.query(ActiveSession.user_email, ActiveSession.last_activity_timestamp).filter(
        ActiveSession.project_name == PROJECT_NAME,
        func.date(ActiveSession.last_activity_timestamp) == today,
    ).all()
    return [{"email": email, "last_active": ts.isoformat()} for email, ts in active]

@router.post("/upload")
async def upload_marketing_file(
    file: UploadFile = File(...),
    file_type: Literal["ott_all", "ott_colorado", "ott_phoenix", "ott_wyoming", "ott_consult_tv"] = Query(
        "ott_all",
        description="Type of data file being uploaded",
    ),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    if not file.filename.endswith((".csv", ".xlsx")):
        raise HTTPException(status_code=400, detail="Only CSV and XLSX files are allowed")

    try:
        dispatch = {
            "ott_all":      _ingest_ott_all,
            "ott_colorado": lambda f, u, d: _ingest_ott_campaign(f, u, d, "colorado"),
            "ott_phoenix":  lambda f, u, d: _ingest_ott_campaign(f, u, d, "phoenix"),
            "ott_wyoming":  lambda f, u, d: _ingest_ott_campaign(f, u, d, "wyoming"),
            "ott_consult_tv": _ingest_ott_consult_tv,
        }

        upload = MarketingUpload(file_name=file.filename, file_type=file_type, total_records=0)
        db.add(upload)
        db.commit()
        db.refresh(upload)

        count = dispatch[file_type](file.file, upload, db)
        upload.total_records = count
        db.commit()

        return {
            "success": True,
            "message": "File uploaded successfully",
            "upload_id": upload.id,
            "file_name": file.filename,
            "file_type": file_type,
            "total_records": count,
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

##----------------Google Ads 4 -----------## 
PROPERTY_ID = "315404882"
def get_ga4_report(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
):
    try:
        access_token = get_valid_access_token()
    except NeedsReAuth as e:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "google_reauth_required",
                "message": str(e),
                "reauth_url": "/auth/google/url",
            },
        )

    db = SessionLocal()

    try:
        token = db.query(GoogleOAuthToken).first()

        if not token:
            raise HTTPException(
                status_code=404,
                detail="Google OAuth token not found."
            )

        access_token = token.access_token

    finally:
        db.close()

    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{PROPERTY_ID}:runReport"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    date_range = [{
    "startDate": start_date if start_date else "30daysAgo",
    "endDate": end_date if end_date else "today"
}]

    payloads = {
        "sessions": {
            "dateRanges": date_range,
            "dimensions": [{"name": "date"}],
            "metrics": [
                {"name": "sessions"},
                {"name": "activeUsers"},
                {"name": "newUsers"}
            ]
        },
        "traffic": {
            "dateRanges": date_range,
            "dimensions": [
                {"name": "sessionSource"},
                {"name": "sessionMedium"}
            ],
            "metrics": [
                {"name": "sessions"},
                {"name": "activeUsers"},
                {"name": "bounceRate"}
            ]
        },
        "landing_pages": {
            "dateRanges": date_range,
            "dimensions": [{"name": "landingPage"}],
            "metrics": [
                {"name": "sessions"},
                {"name": "bounceRate"},
                {"name": "averageSessionDuration"}
            ],
            "limit": 10
        },
        "device": {
            "dateRanges": date_range,
            "dimensions": [{"name": "deviceCategory"}],
            "metrics": [
                {"name": "sessions"},
                {"name": "activeUsers"}
            ]
        }
    }

    results = {}

    for report_name, payload in payloads.items():
        response = requests.post(
            url,
            headers=headers,
            json=payload
        )

        if response.status_code != 200:
            _raise_for_google_status(response, f"GA4:{report_name}")

        results[report_name] = response.json()

    # Summary

    summary = {
        "sessions": 0,
        "active_users": 0,
        "new_users": 0,
    }

    for row in results["sessions"].get("rows", []):

        metrics = row.get("metricValues", [])

        summary["sessions"] += int(metrics[0]["value"])
        summary["active_users"] += int(metrics[1]["value"])
        summary["new_users"] += int(metrics[2]["value"])

    # Traffic Sources

    traffic = []

    for row in results["traffic"].get("rows", []):

        dimensions = row["dimensionValues"]
        metrics = row["metricValues"]

        traffic.append({
            "source": dimensions[0]["value"],
            "medium": dimensions[1]["value"],
            "sessions": int(metrics[0]["value"]),
            "active_users": int(metrics[1]["value"]),
            "bounce_rate": round(float(metrics[2]["value"]) * 100, 2)
        })

    # Landing Pages

    landing_pages = []

    for row in results["landing_pages"].get("rows", []):

        dimensions = row["dimensionValues"]
        metrics = row["metricValues"]

        landing_pages.append({
            "landing_page": dimensions[0]["value"],
            "sessions": int(metrics[0]["value"]),
            "bounce_rate": round(float(metrics[1]["value"]) * 100, 2),
            "average_session_duration": float(metrics[2]["value"])
        })

    # Devices

    devices = []

    for row in results["device"].get("rows", []):

        dimensions = row["dimensionValues"]
        metrics = row["metricValues"]

        devices.append({
            "device": dimensions[0]["value"],
            "sessions": int(metrics[0]["value"]),
            "active_users": int(metrics[1]["value"])
        })

    # Calculate Overall Bounce Rate

    total_sessions = sum(t["sessions"] for t in traffic)

    weighted_bounce = 0

    for t in traffic:

        weighted_bounce += (
            t["sessions"] * t["bounce_rate"]
        )

    overall_bounce_rate = (
        round(weighted_bounce / total_sessions, 2)
        if total_sessions else 0
    )

    # Final Response

    return {
        "summary": {
            "sessions": summary["sessions"],
            "active_users": summary["active_users"],
            "new_users": summary["new_users"],
            "bounce_rate": overall_bounce_rate,
            "message": "Overall GA4 totals for the selected date range: sessions, users, and bounce rate.",
        },

        "traffic_sources": traffic,

        "landing_pages": landing_pages,

        "devices": devices,

        "message": {
            "summary": "Shows the overall website traffic and user activity.",
            "traffic_sources": "Shows where your website visitors came from (Google, Direct, Social, etc.).",
            "landing_pages": "Shows which pages visitors landed on first and how they performed.",
            "devices": "Shows which devices visitors used to access your website (Mobile, Desktop, Tablet)."
        }
    }

#----- google Ads ------# 
GOOGLE_ADS_CUSTOMER_ID = "2688745352"
GOOGLE_ADS_API_VERSION = os.getenv("GOOGLE_ADS_API_VERSION", "v24")
def get_google_ads_report(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
):
    try:
        access_token = get_valid_access_token()
    except NeedsReAuth as e:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "google_reauth_required",
                "message": str(e),
                "reauth_url": "/auth/google/url",
            },
        )

    db: Session = SessionLocal()

    try:
        token = db.query(GoogleOAuthToken).first()

        if not token:
            raise HTTPException(status_code=404, detail="Google OAuth token not found.")
        if not token.access_token:
            raise HTTPException(status_code=404, detail="Google Ads access token not found.")
        if not token.google_ads_developer_token:
            raise HTTPException(status_code=404, detail="Google Ads developer token not found.")

        access_token = token.access_token
        developer_token = token.google_ads_developer_token

    finally:
        db.close()

    url = (
        f"https://googleads.googleapis.com/{GOOGLE_ADS_API_VERSION}/"
        f"customers/{GOOGLE_ADS_CUSTOMER_ID}/googleAds:search"
    )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": developer_token,
        "Content-Type": "application/json",
    }

    if start_date and end_date:
        date_filter = f"segments.date BETWEEN '{start_date}' AND '{end_date}'"
    else:
        date_filter = "segments.date DURING LAST_30_DAYS"

    # Summary Query
    summary_query = {
        "query": f"""
        SELECT
            metrics.cost_micros,
            metrics.impressions,
            metrics.clicks,
            metrics.conversions,
            metrics.average_cpc
        FROM customer
        WHERE
            {date_filter}
        """
    }

    # Campaign Query
    campaign_query = {
        "query": f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            campaign.advertising_channel_type,
            metrics.cost_micros,
            metrics.impressions,
            metrics.clicks,
            metrics.conversions,
            metrics.average_cpc,
            metrics.cost_per_conversion,
            metrics.ctr
        FROM campaign
        WHERE
            {date_filter}
            AND campaign.status='ENABLED'
        ORDER BY metrics.cost_micros DESC
        """
    }

    # Video Query — now includes segments.date + metrics.conversions
    video_query = {
        "query": f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.advertising_channel_type,
            ad_group.id,
            ad_group.name,
            segments.date,
            metrics.impressions,
            metrics.clicks,
            metrics.ctr,
            metrics.cost_micros,
            metrics.conversions,
            metrics.video_trueview_views,
            metrics.video_trueview_view_rate,
            metrics.engagements,
            metrics.engagement_rate,
            metrics.video_watch_time_duration_millis
        FROM video
        WHERE
            {date_filter}
            AND campaign.advertising_channel_type = 'VIDEO'
        """
    }

    # Execute Summary
    response = requests.post(url, headers=headers, json=summary_query)
    if response.status_code != 200:
        _raise_for_google_status(response, "GoogleAds:summary")
    summary_data = response.json()

    # Execute Campaigns
    response = requests.post(url, headers=headers, json=campaign_query)
    if response.status_code != 200:
        _raise_for_google_status(response, "GoogleAds:summary")
    campaign_data = response.json()

    # Execute Video
    response = requests.post(url, headers=headers, json=video_query)
    if response.status_code != 200:
        _raise_for_google_status(response, "GoogleAds:summary")
    video_data = response.json()

    # Process Summary
    total_spend = 0
    total_clicks = 0
    total_impressions = 0
    total_conversions = 0

    for row in summary_data.get("results", []):
        metrics = row["metrics"]
        total_spend += int(metrics.get("costMicros", 0))
        total_clicks += int(metrics.get("clicks", 0))
        total_impressions += int(metrics.get("impressions", 0))
        total_conversions += float(metrics.get("conversions", 0))

    total_spend /= 1_000_000
    avg_cpc = total_spend / total_clicks if total_clicks else 0
    cpa = total_spend / total_conversions if total_conversions else 0
    ctr = total_clicks * 100 / total_impressions if total_impressions else 0

    # Process Campaigns
    campaigns = []
    for row in campaign_data.get("results", []):
        metrics = row["metrics"]
        spend = int(metrics.get("costMicros", 0)) / 1_000_000
        avg_campaign_cpc = (
            int(metrics.get("averageCpc", 0)) / 1_000_000 if metrics.get("averageCpc") else 0
        )
        cost_per_conversion = (
            int(metrics.get("costPerConversion", 0)) / 1_000_000 if metrics.get("costPerConversion") else 0
        )

        campaigns.append({
            "campaign_id": row["campaign"]["id"],
            "campaign_name": row["campaign"]["name"],
            "status": row["campaign"]["status"],
            "channel_type": row["campaign"].get("advertisingChannelType"),
            "spend": round(spend, 2),
            "impressions": int(metrics.get("impressions", 0)),
            "clicks": int(metrics.get("clicks", 0)),
            "conversions": float(metrics.get("conversions", 0)),
            "ctr": metrics.get("ctr"),
            "average_cpc": round(avg_campaign_cpc, 2),
            "cost_per_conversion": round(cost_per_conversion, 2),
        })

    # Process Video — per-campaign summary
    video_summary: dict = {}

    for row in video_data.get("results", []):
        metrics = row["metrics"]
        campaign = row["campaign"]

        bucket = video_summary.setdefault(campaign["id"], {
            "campaign_id": campaign["id"],
            "campaign_name": campaign["name"],
            "impressions": 0,
            "clicks": 0,
            "video_views": 0,
            "engagements": 0,
            "spend": 0.0,
            "watch_time_ms": 0,
        })

        bucket["impressions"]    += int(metrics.get("impressions", 0))
        bucket["clicks"]         += int(metrics.get("clicks", 0))
        bucket["video_views"]    += int(metrics.get("videoTrueviewViews", 0))
        bucket["engagements"]    += int(metrics.get("engagements", 0))
        bucket["spend"]          += int(metrics.get("costMicros", 0)) / 1_000_000
        bucket["watch_time_ms"]  += int(metrics.get("videoWatchTimeDurationMillis", 0))

    youtube_video_data = [
        {
            **v,
            "spend": round(v["spend"], 2),
            "cpv": round(v["spend"] / v["video_views"], 4) if v["video_views"] else None,
            "view_rate_pct": round(v["video_views"] / v["impressions"] * 100, 2) if v["impressions"] else None,
        }
        for v in video_summary.values()
    ]

    # Process Video — per-day totals (for weekly rollups downstream)
    video_daily: dict = {}

    for row in video_data.get("results", []):
        metrics = row["metrics"]
        segments = row.get("segments", {})
        day = segments.get("date")
        if not day:
            continue

        b = video_daily.setdefault(day, {"impressions": 0, "conversions": 0.0})
        b["impressions"]  += int(metrics.get("impressions", 0))
        b["conversions"]  += float(metrics.get("conversions", 0))

    youtube_daily_data = [
        {"day": d, "impressions": b["impressions"], "conversions": round(b["conversions"], 2)}
        for d, b in sorted(video_daily.items())
    ]

    return {
        "summary": {
            "spend": round(total_spend, 2),
            "clicks": total_clicks,
            "impressions": total_impressions,
            "conversions": total_conversions,
            "ctr": round(ctr, 2),
            "average_cpc": round(avg_cpc, 2),
            "cost_per_conversion": round(cpa, 2),
            "message": "Account-level Google Ads totals for the selected date range.",
        },
        "campaigns": campaigns,
        "youtube_video_metrics": youtube_video_data,
        "youtube_daily": youtube_daily_data,
        "message": {
            "summary": "Shows the overall performance of your Google Ads account.",
            "campaigns": "Shows how each Google Ads campaign performed, including spend, clicks, impressions, and conversions.",
            "youtube_video_metrics": "Shows the performance of your YouTube video campaigns, including views, watch time, and engagement.",
            "youtube_daily": "Shows daily YouTube campaign impressions and conversions over the selected date range."
        }
    }

#----- Meta -----# 

AD_ACCOUNT_ID = "927089761323961"
def get_meta_report(
    # date_preset: str = Query("last_30d"),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
):
    db: Session = SessionLocal()

    try:
        token = db.query(GoogleOAuthToken).first()

        # META_ACCESS_TOKEN in .env wins over the stored row, so a renewed
        # token only needs an .env update and a restart.
        access_token = os.getenv("META_ACCESS_TOKEN") or (token.meta_access_token if token else None)

        if not access_token:
            raise HTTPException(
                status_code=404,
                detail="Meta access token not found."
            )

    finally:
        db.close()

    url = f"https://graph.facebook.com/v22.0/act_{AD_ACCOUNT_ID}/insights"

    date_filter = {}

    if start_date and end_date:
        date_filter["time_range"] = json.dumps({
            "since": start_date,
            "until": end_date
        })
    else:
        date_filter["date_preset"] = "last_30d"

    reports = {
        "account_summary": {
            "fields": "spend,impressions,clicks,reach,cpm,cpc",
            "level": "account",
        },

        "campaigns": {
            "fields": "campaign_name,spend,impressions,clicks,actions,cost_per_action_type",
            "level": "campaign",
        },

        "adsets": {
            "fields": "campaign_name,adset_name,spend,impressions,clicks,cpm,cpc",
            "level": "adset",
        },

        "platform_spend": {
            "fields": "spend",
            "level": "account",
            "breakdowns": "publisher_platform",
        }
    }

    raw_results = {}

    for report_name, params in reports.items():
        params.update(date_filter)
        params["access_token"] = access_token

        response = requests.get(
            url,
            params=params
        )

        if response.status_code != 200:
            try:
                error_data = response.json()
            except Exception:
                error_data = {
                    "message": response.text
                }

            raise HTTPException(
                status_code=response.status_code,
                detail={
                    "report": report_name,
                    "error": error_data
                }
            )

        raw_results[report_name] = response.json()

    # Process Campaigns
    campaigns = []

    total_spend = 0
    total_leads = 0
    total_website_leads = 0
    total_meta_form_leads = 0
    total_click_to_call = 0

    for campaign in raw_results["campaigns"].get("data", []):

        spend = float(campaign.get("spend", 0))
        impressions = int(campaign.get("impressions", 0))
        clicks = int(campaign.get("clicks", 0))

        leads = 0
        website_leads = 0
        meta_form_leads = 0
        click_to_call = 0

        for action in campaign.get("actions", []):

            action_type = action.get("action_type", "")
            value = int(float(action.get("value", 0)))

            # Website Leads
            if action_type == "offsite_conversion.fb_pixel_lead":
                website_leads += value
                leads += value

            # Meta Instant Form Leads
            elif action_type in (
                "lead",
                "onsite_conversion.lead_grouped",
            ):
                meta_form_leads += value
                leads += value

            # Click to Call
            elif "phone_call" in action_type.lower():
                click_to_call += value

        cpl = round(spend / leads, 2) if leads else 0

        campaigns.append({
            "campaign_name": campaign.get("campaign_name"),
            "spend": spend,
            "impressions": impressions,
            "clicks": clicks,
            "leads": leads,
            "website_leads": website_leads,
            "meta_form_leads": meta_form_leads,
            "click_to_call": click_to_call,
            "cpl": cpl,
        })

        total_spend += spend
        total_leads += leads
        total_website_leads += website_leads
        total_meta_form_leads += meta_form_leads
        total_click_to_call += click_to_call

    # Platform Spend

    platform_spend = []

    for row in raw_results["platform_spend"].get("data", []):

        platform_spend.append({
            "platform": row.get("publisher_platform"),
            "spend": float(row.get("spend", 0))
        })

    # Final Response

    return {
        "summary": raw_results["account_summary"].get("data", [{}])[0],

        "campaigns": campaigns,

        "platform_spend": platform_spend,

        "adsets": raw_results["adsets"].get("data", []),

        "totals": {
            "spend": round(total_spend, 2),
            "leads": total_leads,
            "website_leads": total_website_leads,
            "meta_form_leads": total_meta_form_leads,
            "click_to_call": total_click_to_call,
            "average_cpl": round(total_spend / total_leads, 2)
            if total_leads else 0,
            "message": "Overall Meta Ads performance for the selected date range.",
        },

        "message": {
            "summary": "Shows the overall performance of your Meta Ads account.",
            "campaigns": "Shows how each Meta Ads campaign performed, including spend, clicks, and leads.",
            "platform_spend": "Shows how your ad spend was distributed across platforms like Facebook and Instagram.",
            "adsets": "Shows the performance of each ad set within your campaigns.",
            "totals": "Shows the total leads generated, lead sources, and average cost per lead."
        }
    }

# google Ads and Meta Ads 

@router.get("/google-meta-ads")
async def paid_media(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    mom: bool = Query(False, description="Include previous-day data"),
):
    # Resolve defaults
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()

    if not end_date:
        end_date = date.today().isoformat()

    # ---------------------------------------------------------
    # Current period
    # Google Ads and Meta Ads should fail independently
    # ---------------------------------------------------------
    google_ads = None
    meta_ads = None

    google_ads_error = None
    meta_ads_error = None

    try:
        google_ads = await asyncio.to_thread(
            get_google_ads_report,
            start_date=start_date,
            end_date=end_date,
        )
    except HTTPException as e:
        google_ads_error = e.detail
    except Exception as e:
        google_ads_error = {
            "error": "google_ads_error",
            "message": str(e),
        }

    try:
        meta_ads = await asyncio.to_thread(
            get_meta_report,
            start_date=start_date,
            end_date=end_date,
        )
    except HTTPException as e:
        meta_ads_error = e.detail
    except Exception as e:
        meta_ads_error = {
            "error": "meta_ads_error",
            "message": str(e),
        }

    response = {
        "google_ads": google_ads,
        "meta_ads": meta_ads,
    }

    # Add errors only when something failed
    if google_ads_error is not None:
        response["google_ads_error"] = google_ads_error

    if meta_ads_error is not None:
        response["meta_ads_error"] = meta_ads_error

    # ---------------------------------------------------------
    # MoM / previous period
    # ---------------------------------------------------------
    if mom:
        prior_start = (
            date.fromisoformat(start_date) - timedelta(days=1)
        ).isoformat()

        prior_end = (
            date.fromisoformat(end_date) - timedelta(days=1)
        ).isoformat()

        previous_google_ads = None
        previous_meta_ads = None

        previous_google_error = None
        previous_meta_error = None

        try:
            previous_google_ads = await asyncio.to_thread(
                get_google_ads_report,
                start_date=prior_start,
                end_date=prior_end,
            )
        except HTTPException as e:
            previous_google_error = e.detail
        except Exception as e:
            previous_google_error = {
                "error": "google_ads_error",
                "message": str(e),
            }

        try:
            previous_meta_ads = await asyncio.to_thread(
                get_meta_report,
                start_date=prior_start,
                end_date=prior_end,
            )
        except HTTPException as e:
            previous_meta_error = e.detail
        except Exception as e:
            previous_meta_error = {
                "error": "meta_ads_error",
                "message": str(e),
            }

        response["mom"] = {
            "google_ads": previous_google_ads,
            "meta_ads": previous_meta_ads,
        }

        if previous_google_error is not None:
            response["mom"]["google_ads_error"] = previous_google_error

        if previous_meta_error is not None:
            response["mom"]["meta_ads_error"] = previous_meta_error

    return response

API_URL = "https://apirequest.app/query"
AGENCY_ANALYTICS_API_KEY  = os.getenv("AGENCY_ANALYTICS_API_KEY")

def generate_analytics_access_token():
    return base64.b64encode(
        f":{AGENCY_ANALYTICS_API_KEY}".encode("utf-8")
    ).decode("utf-8")


def save_analytics_token(db: Session):
    token = generate_analytics_access_token()

    oauth = db.query(GoogleOAuthToken).first()
    if oauth is None:
        oauth = GoogleOAuthToken()

    oauth.analytic_access_token = token

    db.add(oauth)
    db.commit()
    db.refresh(oauth)

    return token


def get_analytics_token(db: Session):
    oauth = db.query(GoogleOAuthToken).first()
    if oauth is None:
        return None

    return oauth.analytic_access_token


def call_agency_api(payload, db: Session):
    token = get_analytics_token(db)

    if not token:
        token = save_analytics_token(db)

    headers = {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }

    response = requests.post(API_URL, headers=headers, json=payload, timeout=120)

    if response.status_code == 401:
        token = save_analytics_token(db)
        headers["Authorization"] = f"Basic {token}"
        response = requests.post(API_URL, headers=headers, json=payload, timeout=120)

    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail={"stage": "call_agency_api", "body": response.text},
        )

    return response.json()


def campaign_payload():
    return {
        "provider": "agency-analytics-v2",
        "asset": "campaign",
        "operation": "read",
        "fields": [
            "id",
            "date_created",
            "date_modified",
            "url",
            "company",
            "status",
            "scope",
            "group_title",
            "email_addresses",
            "phone_numbers",
            "address",
            "city",
            "state",
            "zip",
            "country",
            "revenue",
            "headcount",
            "google_ignore_places",
            "enforce_google_cid",
            "timezone",
            "type",
            "campaign_group_id",
            "account_id"
        ],
        "sort": [{"id": "desc"}],
        "offset": 0,
        "limit": 100
    }


def keyword_payload(campaign_id: str):
    return {
        "provider": "agency-analytics-v2",
        "asset": "keyword",
        "operation": "read",
        "fields": [
            "keyword_phrase",
            "id"
        ],
        "filters": [
            {
                "campaign_id": {
                    "$equals_comparison": campaign_id
                }
            }
        ],
        "sort": [{"id": "desc"}],
        "offset": 0,
        "limit": 50
    }


def keyword_ranking_payload(campaign_id: str, keyword_id: str, start_date, end_date):
    return {
        "provider": "agency-analytics-v2",
        "asset": "keyword-rankings",
        "operation": "read",
        "group_by": ["date"],
        "fields": [
            "date",
            "google_ranking",
            "google_ranking_url",
            "google_mobile_ranking",
            "google_mobile_ranking_url",
            "google_local_ranking",
            "bing_ranking",
            "bing_ranking_url",
            "results",
            "volume",
            "competition",
            "field_status"
        ],
        "filters": [
            {
                "campaign_id": {
                    "$equals_comparison": campaign_id
                }
            },
            {
                "keyword_id": {
                    "$equals_comparison": keyword_id
                }
            },
            {
                "start_date": {
                    "$greaterthanorequal_comparison": start_date
                }
            },
            {
                "end_date": {
                    "$lessthanorequal_comparison": end_date
                }
            }
        ],
        "sort": [{"date": "asc"}],
        "offset": 0,
        "limit": 9999
    }


def campaign_ranking_payload(campaign_id: str, start_date, end_date):
    return {
        "provider": "agency-analytics-v2",
        "asset": "campaign-rankings",
        "operation": "read",
        "fields": [
            "date",
            "google_ranking_change",
            "google_ranking_count",
            "google_local_ranking_change",
            "google_local_ranking_count",
            "google_mobile_ranking_change",
            "google_mobile_ranking_count",
            "bing_ranking_change",
            "bing_ranking_count",
            "ranking_average",
            "results",
            "volume",
            "competition",
            "field_status"
        ],
        "filters": [
            {
                "campaign_id": {
                    "$equals_comparison": campaign_id
                }
            },
            {
                "start_date": {
                    "$greaterthanorequal_comparison": start_date
                }
            },
            {
                "end_date": {
                    "$lessthanorequal_comparison": end_date
                }
            }
        ],
        "group_by": ["date"],
        "sort": [{"date": "asc"}],
        "offset": 0,
        "limit": 9999
    }


# ---- agency_analytics --------# 
def agency_analytics(
    db:Session,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
):
    
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")

    if not end_date:
        end_date = date.today().strftime("%Y-%m-%d") 

    campaigns = call_agency_api(campaign_payload(), db)
    # 2. Filter Ramos campaigns
    campaign_list = []
    ramos_campaign_ids = []

    for row in campaigns.get("results", {}).get("rows", []):
        url = (row.get("url") or "").lower()

        if "ramoslaw.com" not in url:
            continue

        ramos_campaign_ids.append(str(row.get("id")))

        campaign_list.append({
            "id": row.get("id"),
            "company": row.get("company"),
            "status": row.get("status"),
            "scope": row.get("scope"),
            "group_title": row.get("group_title"),
            "city": row.get("city"),
            "state": row.get("state"),
            "country": row.get("country"),
            "revenue": row.get("revenue"),
            "headcount": row.get("headcount"),
            "timezone": row.get("timezone"),
            "type": row.get("type"),
            "url": row.get("url"),
        })
        
    keyword_list = []
    keyword_history = []
    campaign_history = []

    seen_keywords = set()

    for campaign_id in ramos_campaign_ids:

        keywords = call_agency_api(keyword_payload(campaign_id), db)

        campaign_keywords = []

        for row in keywords.get("results", {}).get("rows", []):

            keyword_id = str(row.get("id"))
            keyword = row.get("keyword_phrase")

            if keyword in seen_keywords:
                continue

            seen_keywords.add(keyword)

            item = {
                "campaign_id": campaign_id,
                "keyword_id": keyword_id,
                "keyword": keyword,
            }

            keyword_list.append(item)
            campaign_keywords.append(item)

        # ---------------- Keyword Rankings ----------------

        for keyword in campaign_keywords:

            keyword_rankings = call_agency_api(
                keyword_ranking_payload(
                    campaign_id,
                    keyword["keyword_id"],
                    start_date,
                    end_date,
                ),
                db,
            )

            for row in keyword_rankings.get("results", {}).get("rows", []):
                keyword_history.append({
                    "keyword_id": keyword["keyword_id"],
                    "keyword": keyword["keyword"],
                    "date": row.get("date"),
                    "google_rank": row.get("google_ranking"),
                    "google_mobile_rank": row.get("google_mobile_ranking"),
                    "google_local_rank": row.get("google_local_ranking"),
                    "bing_rank": row.get("bing_ranking"),
                    "volume": row.get("volume"),
                    "competition": row.get("competition"),
                    "results": row.get("results"),
                    "status": row.get("field_status"),
                })

        # ---------------- Campaign Rankings ----------------

        campaign_rankings = call_agency_api(
            campaign_ranking_payload(
                campaign_id,
                start_date,
                end_date,
            ),
            db,
        )

        for row in campaign_rankings.get("results", {}).get("rows", []):
            campaign_history.append({
                "campaign_id": campaign_id,
                "date": row.get("date"),
                "ranking_average": row.get("ranking_average"),
                "google_ranking_change": row.get("google_ranking_change"),
                "google_ranking_count": row.get("google_ranking_count"),
                "google_mobile_ranking_change": row.get("google_mobile_ranking_change"),
                "google_mobile_ranking_count": row.get("google_mobile_ranking_count"),
                "google_local_ranking_change": row.get("google_local_ranking_change"),
                "google_local_ranking_count": row.get("google_local_ranking_count"),
                "bing_ranking_change": row.get("bing_ranking_change"),
                "bing_ranking_count": row.get("bing_ranking_count"),
                "results": row.get("results"),
                "volume": row.get("volume"),
                "competition": row.get("competition"),
                "status": row.get("field_status"),
            })

    # Summary

    summary = {
        "campaign_count": len(campaign_list),
        "keyword_count": len(keyword_list),
        "keyword_history_records": len(keyword_history),
        "campaign_history_records": len(campaign_history),
    }

    # Response
    return {
        "summary": summary,
        "campaigns": campaign_list,
        "keywords": keyword_list,
        "keyword_rankings": keyword_history,
        "campaign_rankings": campaign_history,
        "message": {
            "summary": "Counts of Ramos Law campaigns, keywords, and ranking records pulled from Agency Analytics.",
            "campaigns": "Ramos Law campaigns configured in Agency Analytics.",
            "keywords": "Tracked keywords per Ramos Law campaign.",
            "keyword_rankings": "Daily search-ranking history per keyword, used to track SEO progress.",
            "campaign_rankings": "Daily aggregate ranking movement per campaign.",
        }
    } 

@router.get("/website-ga4-agency")
async def get_ga4_agency(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    mom: bool = Query(False, description="Include previous-day data"),
    db: Session = Depends(get_db),
):
    # Resolve defaults here so the previous-day range can be calculated reliably.
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()
    if not end_date:
        end_date = date.today().isoformat()

    google_ga4, analytics = await asyncio.gather(
        asyncio.to_thread(
            get_ga4_report,
            start_date=start_date,
            end_date=end_date,
        ),
        asyncio.to_thread(
            agency_analytics,
            db=db,
            start_date=start_date,
            end_date=end_date,
        ),
    )

    response = {
        "google_ga4": google_ga4,
        "agency_analytics": analytics,
    }

    if mom:
        prior_start = (
            date.fromisoformat(start_date) - timedelta(days=1)
        ).isoformat()

        prior_end = (
            date.fromisoformat(end_date) - timedelta(days=1)
        ).isoformat()

        previous_ga4, previous_analytics = await asyncio.gather(
            asyncio.to_thread(
                get_ga4_report,
                start_date=prior_start,
                end_date=prior_end,
            ),
            asyncio.to_thread(
                agency_analytics,
                db=db,
                start_date=prior_start,
                end_date=prior_end,
            ),
        )

        # Same structure as `response`, using previous-day values.
        response["mom"] = {
            "google_ga4": previous_ga4,
            "agency_analytics": previous_analytics,
        }

    return response


PROFOUND_API_KEY = os.getenv("PROFOUND_API_KEY")

PROFOUND_BASE_URL = "https://api.tryprofound.com/v1"

CATEGORY_ID = "d89a58bb-c5ac-4f5a-83d7-4c4697009c7a"

DIRECTORY_HOSTNAMES = ["expertise.com", "threebestrated.com", "avvo.com", "lawyer.com", "forbes.com"]


@router.get("/auth/google/url")
async def google_auth_url():
    """
    Start Google OAuth flow.
    """
    try:
        url = get_auth_url()
        return RedirectResponse(url=url)

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to build auth URL: {e}"
        )

GOOGLE_DASHBOARD_URL = "https://lxintel.net/marketing_dashboard"

@router.api_route("/auth/google/callback", methods=["GET", "POST"])
async def google_auth_callback(request: Request):
    try:
        code = request.query_params.get("code")

        # Support providers configured to submit the callback as a form POST.
        if not code and request.method == "POST":
            form = await request.form()
            code = form.get("code")

        if not code:
            raise HTTPException(status_code=400, detail="Missing OAuth authorization code.")

        exchange_code_for_tokens(code)
        return RedirectResponse(
            url=f"{GOOGLE_DASHBOARD_URL}?google_auth=success",
            status_code=303,
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"Google OAuth error: {e}")
        return RedirectResponse(
            url=f"{GOOGLE_DASHBOARD_URL}?google_auth=failed",
            status_code=303,
        )
    
@router.get("/aeo-ai-visibility")
def get_ai_visibility(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    mom: bool = Query(False, description="Include month-over-month engine comparison"),
    critical_issues: bool = Query(False, description="Include Top 3 Critical Issues from Profound"),
):
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
    if not end_date:
        end_date = date.today().strftime("%Y-%m-%d")

    # ── existing logic (unchanged) ────────────────────────────────────────────
    headers = {"x-api-key": PROFOUND_API_KEY, "Content-Type": "application/json"}

    category_response = requests.get(f"{PROFOUND_BASE_URL}/org/categories", headers=headers)
    if category_response.status_code != 200:
        raise HTTPException(status_code=category_response.status_code, detail=category_response.json())
    categories = category_response.json()

    asset_response = requests.get(f"{PROFOUND_BASE_URL}/org/categories/{CATEGORY_ID}/assets", headers=headers)
    if asset_response.status_code != 200:
        raise HTTPException(status_code=asset_response.status_code, detail=asset_response.json())
    assets = asset_response.json()

    payload = {
        "category_id": CATEGORY_ID, "start_date": start_date, "end_date": end_date,
        "metrics": ["share_of_voice", "visibility_score"], "dimensions": ["asset_name"],
        "pagination": {"limit": 100},
    }
    visibility_response = requests.post(f"{PROFOUND_BASE_URL}/reports/visibility", headers=headers, json=payload)
    if visibility_response.status_code != 200:
        raise HTTPException(status_code=visibility_response.status_code, detail=visibility_response.json())
    visibility = visibility_response.json()

    category_list = [{"id": r["id"], "name": r["name"], "organization": r["organization"]["name"]} for r in categories]

    asset_list = []
    owned_asset = None
    for row in assets:
        if row["is_owned"]:
            owned_asset = row["name"]
        asset_list.append({"id": row["id"], "name": row["name"], "website": row["website"], "logo": row["logo_url"], "owned": row["is_owned"]})

    leaderboard = []
    total_visibility = 0
    total_share = 0
    for row in visibility.get("data", []):
        asset_name     = row["dimensions"][0]
        share_of_voice = round(row["metrics"][0] * 100, 2)
        visibility_score = round(row["metrics"][1] * 100, 2)
        leaderboard.append({"asset_name": asset_name, "share_of_voice": share_of_voice, "visibility_score": visibility_score})
        total_share += share_of_voice
        total_visibility += visibility_score

    summary = {
        "total_categories": len(category_list), "total_assets": len(asset_list),
        "owned_asset": owned_asset,
        "average_share_of_voice": round(total_share / len(leaderboard), 2) if leaderboard else 0,
        "average_visibility_score": round(total_visibility / len(leaderboard), 2) if leaderboard else 0,
    }

    radar_payload = {
        "category_id": CATEGORY_ID, "start_date": start_date, "end_date": end_date,
        "metrics": ["share_of_voice", "visibility_score"], "dimensions": ["asset_name", "model"],
        "pagination": {"limit": 500},
    }
    radar_response = requests.post(f"{PROFOUND_BASE_URL}/reports/visibility", headers=headers, json=radar_payload)
    if radar_response.status_code != 200:
        raise HTTPException(status_code=radar_response.status_code, detail=radar_response.json())

    platform_radar: dict = defaultdict(dict)
    for row in radar_response.json().get("data", []):
        asset_name, model = row["dimensions"]
        platform_radar[asset_name][model] = round(row["metrics"][0] * 100, 2)

    answers_payload = {
        "category_id": CATEGORY_ID, "start_date": start_date, "end_date": end_date,
        "pagination": {"limit": 5000},
        "include": {"response": True, "citations": True, "citation_details": True,
                    "model": True, "asset": True, "mentions": True, "topic": True, "prompt": True},
    }
    answers_response = requests.post(f"{PROFOUND_BASE_URL}/prompts/answers", headers=headers, json=answers_payload)
    if answers_response.status_code != 200:
        raise HTTPException(status_code=answers_response.status_code, detail=answers_response.json())
    answers_rows = answers_response.json().get("data", [])

    placement_counts: dict = defaultdict(lambda: {"total": 0, "first": 0, "top3": 0, "mentioned": 0})
    practice_area_visibility: dict = defaultdict(dict)
    verbatim_citations = []
    directory_presence: dict = defaultdict(lambda: {"owned": False, "competitors": set()})
    owned_first_token = owned_asset.split()[0].lower() if owned_asset else ""

    def _name_appears(name: str, text: str) -> bool:
        normalized_name = " ".join(
            "".join(char if char.isalnum() else " " for char in name.casefold()).split()
        )
        normalized_text = " ".join(
            "".join(char if char.isalnum() else " " for char in text.casefold()).split()
        )
        return bool(normalized_name and normalized_name in normalized_text)

    # Re-use answers_rows for critical issues bucket too (avoid double API call)
    ci_buckets: dict = defaultdict(lambda: defaultdict(lambda: {
        "total": 0, "gap": 0, "weak": 0, "mentioned": 0,
        "example_prompts": [], "competitors_appearing": set(),
    }))

    STATUS_RANK = {"#1": 4, "Top-3": 3, "Cited": 2, "Weak": 1, "Gap": 0}

    for row in answers_rows:
        model    = row.get("model") or "unknown"
        topic    = row.get("topic") or "Uncategorized"
        mentions = row.get("mentions") or []
        response_text    = row.get("response") or ""
        prompt   = row.get("prompt") or ""
        citation_details = row.get("citation_details") or []

        placement_counts[model]["total"] += 1
        ci_buckets[topic][model]["total"] += 1

        status = "Gap"
        owned_mention_index = next(
            (
                index
                for index, mention in enumerate(mentions)
                if str(mention).strip().casefold() == owned_asset.strip().casefold()
            ),
            None,
        ) if owned_asset else None

        if owned_mention_index is not None:
            idx = owned_mention_index
            placement_counts[model]["mentioned"] += 1
            ci_buckets[topic][model]["mentioned"] += 1
            if idx == 0:
                placement_counts[model]["first"] += 1
                status = "#1"
            else:
                status = "Cited"
            if idx < 3:
                placement_counts[model]["top3"] += 1
                if idx > 0:
                    status = "Top-3"
            if response_text:
                verbatim_citations.append({"platform": model, "query": prompt, "quote": response_text})
        elif owned_first_token and any(
            owned_first_token in (c.get("title", "") + c.get("text", "")).lower()
            for c in citation_details
        ):
            status = "Weak"
            ci_buckets[topic][model]["weak"] += 1
            if len(ci_buckets[topic][model]["example_prompts"]) < 2:
                ci_buckets[topic][model]["example_prompts"].append(prompt)
        else:
            ci_buckets[topic][model]["gap"] += 1
            if len(ci_buckets[topic][model]["example_prompts"]) < 2:
                ci_buckets[topic][model]["example_prompts"].append(prompt)

        practice_area_visibility[topic][model] = status

        for c in citation_details:
            citation_url = str(c.get("url") or "")
            hostname = str(c.get("hostname") or "").strip().lower()
            if not hostname and citation_url:
                hostname = citation_url.split("://", 1)[-1].split("/", 1)[0]
            hostname = hostname.split(":", 1)[0].removeprefix("www.")
            matched_host = next(
                (
                    host for host in DIRECTORY_HOSTNAMES
                    if host.lower().removeprefix("www.") == hostname
                ),
                None,
            )
            if matched_host is None:
                continue
            text_blob = " ".join(
                str(c.get(field) or "") for field in ("title", "url", "text")
            ).casefold()
            owned_is_present = bool(
                owned_asset
                and (
                    _name_appears(owned_asset, text_blob)
                    or _name_appears(owned_first_token, text_blob)
                )
            )
            if owned_is_present:
                directory_presence[matched_host]["owned"] = True
                continue
            for m in mentions:
                if owned_asset and str(m).strip().casefold() == owned_asset.strip().casefold():
                    continue
                competitor_name = str(m).strip()
                if competitor_name and _name_appears(competitor_name, text_blob):
                    directory_presence[matched_host]["competitors"].add(competitor_name)

        if owned_mention_index is None:
            for m in mentions:
                if not owned_asset or str(m).strip().casefold() != owned_asset.strip().casefold():
                    ci_buckets[topic][model]["competitors_appearing"].add(m)

    # Include models returned by the visibility report even when the answers
    # endpoint has no rows for that model in this date range.
    for model_values in platform_radar.values():
        for model in model_values:
            placement_counts[model]

    directory_gap_analysis = []
    for host in DIRECTORY_HOSTNAMES:
        info = directory_presence.get(host, {"owned": False, "competitors": set()})
        directory_gap_analysis.append({
            "directory": host, "competitor_listed": "Yes" if info["competitors"] else "No",
            "ramos_listed": "Yes" if info["owned"] else "No",
            "platform": ", ".join(sorted(info["competitors"])) if info["competitors"] else "-",
            "action": "Review",
        })

    response = {
        "summary": summary, "categories": category_list, "assets": asset_list,
        "leaderboard": leaderboard, "platform_radar": platform_radar,
        "placement_counts": dict(placement_counts), "practice_area_visibility": practice_area_visibility,
        "verbatim_citations": verbatim_citations, "directory_gap_analysis": directory_gap_analysis,
        "report_info": visibility.get("info", {}),
        "message": {
            "summary": "Overall AI (AEO) visibility snapshot.",
            "categories": "Profound categories tracked for AI-answer visibility.",
            "assets": "Brands/assets tracked in this category.",
            "leaderboard": "Share of voice and visibility score per asset.",
            "platform_radar": "Share of voice per asset broken down by AI model/platform.",
            "placement_counts": "How often each AI model mentions us first, in the top 3.",
            "practice_area_visibility": "Visibility status per topic and AI model.",
            "verbatim_citations": "Actual AI answer text where we were mentioned.",
            "directory_gap_analysis": "Whether we (vs competitors) appear on key legal directory sites.",
        },
    }

    # ── Previous-day response (same format as the current response) ──────────
    if mom:
        prior_start = (date.fromisoformat(start_date) - timedelta(days=1)).isoformat()
        prior_end = (date.fromisoformat(end_date) - timedelta(days=1)).isoformat()

        # Keep `mom` structurally identical to the current response. Passing
        # mom=False prevents recursive nesting; critical_issues is forwarded so
        # optional top-level sections are present in both datasets.
        previous_response = get_ai_visibility(
            start_date=prior_start,
            end_date=prior_end,
            mom=False,
            critical_issues=critical_issues,
        )

        # Make placement keys consistent across current and previous datasets.
        # Missing model rows are represented by explicit zero values.
        empty_placement = {"total": 0, "first": 0, "top3": 0, "mentioned": 0}
        all_models = sorted(
            set(response.get("placement_counts", {}))
            | set(previous_response.get("placement_counts", {}))
        )
        response["placement_counts"] = {
            model: response["placement_counts"].get(model, empty_placement.copy())
            for model in all_models
        }
        previous_response["placement_counts"] = {
            model: previous_response.get("placement_counts", {}).get(
                model, empty_placement.copy()
            )
            for model in all_models
        }

        # These assignments are explicit so both fields are guaranteed to be
        # available directly under `mom` for frontend comparisons.
        previous_response["directory_gap_analysis"] = list(
            previous_response.get("directory_gap_analysis") or []
        )
        response["mom"] = previous_response

    # ── NEW: Top 3 Critical Issues ────────────────────────────────────────────
    if critical_issues:
        issues = []
        for topic, models in ci_buckets.items():
            for model, b in models.items():
                severity = b["gap"] * 3 + b["weak"]
                if severity == 0:
                    continue
                gap_rate     = round(b["gap"]  / b["total"] * 100, 1) if b["total"] else 0
                weak_rate    = round(b["weak"] / b["total"] * 100, 1) if b["total"] else 0
                mention_rate = round(b["mentioned"] / b["total"] * 100, 1) if b["total"] else 0
                if gap_rate >= 70:
                    priority = "Critical"
                elif gap_rate >= 40 or (gap_rate + weak_rate) >= 60:
                    priority = "High"
                else:
                    priority = "Medium"
                if gap_rate >= 70:
                    rec = (f"We are absent in {round(gap_rate)}% of '{topic}' queries on {model}. "
                           "Publish authoritative content targeting this topic and submit to directories these AI engines cite.")
                elif gap_rate >= 40:
                    rec = (f"We appear in fewer than {round(100 - gap_rate)}% of '{topic}' queries on {model}. "
                           "Strengthen topical authority with long-form content and earned citations.")
                else:
                    rec = (f"Our presence for '{topic}' on {model} is weak. "
                           "Improve structured data and authoritative backlinks to move from 'Cited' to 'Top-3'.")
                issues.append({
                    "topic": topic, "engine": model, "priority": priority,
                    "severity_score": severity, "total_prompts": b["total"],
                    "gap_count": b["gap"],     "gap_rate_pct": gap_rate,
                    "weak_count": b["weak"],   "weak_rate_pct": weak_rate,
                    "mention_rate_pct": mention_rate,
                    "example_prompts": b["example_prompts"],
                    "competitors_appearing": sorted(b["competitors_appearing"])[:5],
                    "recommendation": rec,
                })
        issues.sort(key=lambda x: (-x["severity_score"], -x["total_prompts"]))
        response["critical_issues"] = issues[:3]

    return response

#----------------- quartely tabs---------------# 
def quarter_bounds(year: int, quarter: int, cap_at: date | None = None) -> dict:
    start_month = 3 * (quarter - 1) + 1
    end_month = start_month + 2
    start = date(year, start_month, 1)
    end = date(year, 12, 31) if end_month == 12 else date(year, end_month + 1, 1) - timedelta(days=1)
 
    if cap_at:
        end = min(end, cap_at)
 
    return {
        "year": year,
        "quarter": quarter,
        "label": f"Q{quarter} {year}",
        "tab_label": f"Q{quarter}'{str(year)[2:]}",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
 
 
def current_quarter(today: date | None = None) -> dict:
    today = today or date.today()
    quarter = (today.month - 1) // 3 + 1
    q = quarter_bounds(today.year, quarter, cap_at=today)
    q["is_live"] = True
    return q

def shift_quarter(q: dict, offset: int) -> dict:
    absolute = (q["year"] * 4 + (q["quarter"] - 1)) + offset
    year, quarter = divmod(absolute, 4)
    return quarter_bounds(year, quarter + 1, cap_at=date.today())
 
def last_n_quarters(n: int) -> list[dict]:
    cq = current_quarter()
    return [shift_quarter(cq, -offset) for offset in range(n - 1, -1, -1)]
 
def resolve_quarter(quarter_param: str | None) -> dict:
    if not quarter_param:
        return current_quarter()
 
    cleaned = quarter_param.upper().replace("'", "-").replace(" ", "-")
    q_part, y_part = cleaned.split("-", 1)
    quarter = int(q_part.replace("Q", ""))
    year = int(y_part) if len(y_part) == 4 else 2000 + int(y_part)
 
    cq = current_quarter()
    is_live = (year == cq["year"] and quarter == cq["quarter"])
    q = quarter_bounds(year, quarter, cap_at=date.today() if is_live else None)
    q["is_live"] = is_live
    return q
 
def classify_channel(source: str, medium: str) -> str:
    source = (source or "").lower()
    medium = (medium or "").lower()
 
    if source in ("(direct)", "direct") and medium in ("(none)", "none"):
        return "Direct"
 
    if medium == "organic":
        return "Organic Search"
 
    if medium in ("cpc", "ppc", "paid") and source in ("google", "bing"):
        return "Paid Search"
 
    social_sources = ("facebook", "instagram", "fb", "ig", "linkedin", "tiktok", "meta")
    if source in social_sources or "social" in medium:
        return "Paid Social" if medium in ("cpc", "paid", "paid_social") else "Organic Social"
 
    return "Other"

 
def sessions_by_channel(ga4_report: dict) -> dict:
    totals: dict[str, int] = defaultdict(int)
    for row in ga4_report.get("traffic_sources", []):
        channel = classify_channel(row.get("source"), row.get("medium"))
        totals[channel] += row.get("sessions", 0)
    return dict(totals)
 
 
def bounce_driver(ga4_report: dict) -> str | None:
    traffic = ga4_report.get("traffic_sources", [])
    if not traffic:
        return None
 
    overall_bounce = ga4_report.get("summary", {}).get("bounce_rate", 0)
 
    by_channel: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "weighted_bounce": 0.0})
    for row in traffic:
        channel = classify_channel(row.get("source"), row.get("medium"))
        by_channel[channel]["sessions"] += row.get("sessions", 0)
        by_channel[channel]["weighted_bounce"] += row.get("sessions", 0) * row.get("bounce_rate", 0)
 
    if not by_channel:
        return None
 
    top_channel = max(by_channel.items(), key=lambda kv: kv[1]["sessions"])
    name, stats = top_channel
    channel_bounce = (stats["weighted_bounce"] / stats["sessions"]) if stats["sessions"] else 0
 
    return name if channel_bounce >= overall_bounce else None
 
 
# Main Quarterly Review endpoint
@router.get("/quarterly-review")
async def get_quarterly_review(
    quarter: str | None = Query(
        None,
        description="e.g. 'Q2-2026'. Defaults to the current live quarter."
    ),
    db: Session = Depends(get_db),
):
    ott_spend = None
    ott_vcr = None

    q = resolve_quarter(quarter)
    prior_q = shift_quarter(q, -1)
    tabs = last_n_quarters(4)

    last_12mo_start = (date.today() - timedelta(days=365)).isoformat()
    today_str = date.today().isoformat()

    eight_quarters = last_n_quarters(8)

    # =========================================================
    # Initialize reports and errors
    # =========================================================

    ga4_report = None
    google_ads_report = None
    meta_report_ttm = None
    ai_visibility_current = None
    ai_visibility_prior = None

    ga4_error = None
    google_ads_error = None
    meta_error = None
    ai_visibility_current_error = None
    ai_visibility_prior_error = None

    # =========================================================
    # GA4
    # =========================================================

    try:
        ga4_report = await asyncio.to_thread(
            get_ga4_report,
            start_date=q["start_date"],
            end_date=q["end_date"],
        )

    except HTTPException as e:
        ga4_error = e.detail

    except Exception as e:
        ga4_error = {
            "error": "ga4_error",
            "message": str(e),
        }

    # =========================================================
    # Google Ads
    # =========================================================

    try:
        google_ads_report = await asyncio.to_thread(
            get_google_ads_report,
            start_date=q["start_date"],
            end_date=q["end_date"],
        )

    except HTTPException as e:
        google_ads_error = e.detail

    except Exception as e:
        google_ads_error = {
            "error": "google_ads_error",
            "message": str(e),
        }

    # =========================================================
    # Meta Ads
    #
    # IMPORTANT:
    # If Meta token is expired, only Meta fails.
    # Google / GA4 / AI / OTT data continues.
    # =========================================================

    try:
        meta_report_ttm = await asyncio.to_thread(
            get_meta_report,
            start_date=last_12mo_start,
            end_date=today_str,
        )

    except HTTPException as e:
        meta_error = e.detail

    except Exception as e:
        meta_error = {
            "error": "meta_ads_error",
            "message": str(e),
        }

    # =========================================================
    # AI Visibility - Current Quarter
    # =========================================================

    try:
        ai_visibility_current = await asyncio.to_thread(
            get_ai_visibility,
            start_date=q["start_date"],
            end_date=q["end_date"],
        )

    except HTTPException as e:
        ai_visibility_current_error = e.detail

    except Exception as e:
        ai_visibility_current_error = {
            "error": "ai_visibility_error",
            "message": str(e),
        }

    # =========================================================
    # AI Visibility - Prior Quarter
    # =========================================================

    try:
        ai_visibility_prior = await asyncio.to_thread(
            get_ai_visibility,
            start_date=prior_q["start_date"],
            end_date=prior_q["end_date"],
        )

    except HTTPException as e:
        ai_visibility_prior_error = e.detail

    except Exception as e:
        ai_visibility_prior_error = {
            "error": "ai_visibility_error",
            "message": str(e),
        }

    # =========================================================
    # 8-Quarter GA4 Data
    #
    # One failed quarter should not break the entire API.
    # =========================================================

    eight_quarter_ga4 = []

    for eq in eight_quarters:

        try:
            report = await asyncio.to_thread(
                get_ga4_report,
                start_date=eq["start_date"],
                end_date=eq["end_date"],
            )

            eight_quarter_ga4.append(report)

        except HTTPException:
            eight_quarter_ga4.append({})

        except Exception:
            eight_quarter_ga4.append({})

    # =========================================================
    # Safe summaries
    # =========================================================

    ga4_summary = (
        ga4_report.get("summary", {})
        if ga4_report
        else {}
    )

    ads_summary = (
        google_ads_report.get("summary", {})
        if google_ads_report
        else {}
    )

    meta_totals = (
        meta_report_ttm.get("totals", {})
        if meta_report_ttm
        else {}
    )

    ai_current = (
        ai_visibility_current.get("summary", {})
        if ai_visibility_current
        else {}
    )

    ai_prior = (
        ai_visibility_prior.get("summary", {})
        if ai_visibility_prior
        else {}
    )

    # =========================================================
    # AI Visibility Delta
    # =========================================================

    ai_visibility_delta = round(
        ai_current.get("average_visibility_score", 0)
        - ai_prior.get("average_visibility_score", 0),
        1,
    )

    # =========================================================
    # Bounce Driver
    # =========================================================

    driver_channel = (
        bounce_driver(ga4_report)
        if ga4_report
        else None
    )

    # =========================================================
    # OTT / Consult.TV
    # =========================================================

    q_all = (
        db.query(OttAllCampaignsRecord)
        .join(
            MarketingUpload,
            MarketingUpload.id == OttAllCampaignsRecord.upload_id
        )
        .filter(
            MarketingUpload.file_type.in_(
                _CONSULT_TV_FILE_TYPES
            )
        )
        .filter(
            OttAllCampaignsRecord.day >= q["start_date"]
        )
        .filter(
            OttAllCampaignsRecord.day <= q["end_date"]
        )
    )

    rows = q_all.all()

    total_spend = sum(
        r.spend or 0
        for r in rows
    )

    total_impressions = sum(
        r.impressions or 0
        for r in rows
    )

    total_completions = sum(
        r.completions or 0
        for r in rows
    )

    ott_spend = round(
        total_spend,
        2
    )

    ott_vcr = (
        round(
            total_completions * 100 / total_impressions,
            2,
        )
        if total_impressions
        else None
    )

    # =========================================================
    # Cards
    # =========================================================

    cards = {

        "sessions": {
            "value": ga4_summary.get(
                "sessions",
                0
            ),
            "period_label": (
                f"{q['label']} · "
                f"{'Live' if q.get('is_live') else 'Final'}"
            ),
            "date_range": (
                f"{q['start_date']} "
                f"to {q['end_date']}"
            ),
            "source": "Agency Analytics",
            "message": (
                "Total website sessions "
                "for the selected quarter."
            ),
        },

        "conversions": {
            "value": ga4_summary.get(
                "conversions",
                0
            ),
            "period_label": (
                f"{q['label']} · "
                f"{'Live' if q.get('is_live') else 'Final'}"
            ),
            "date_range": (
                f"{q['start_date']} "
                f"to {q['end_date']}"
            ),
            "source": "Agency Analytics GA4",
            "message": (
                "Total website conversions "
                "for the selected quarter."
            ),
        },

        "ott_spend": {
            "value": ott_spend,
            "period_label": "Consult.TV actual",
            "date_range": (
                f"{q['start_date']} "
                f"to {q['end_date']}"
            ),
            "source": "Consult.TV",
            "message": (
                "Total OTT/streaming TV ad spend "
                "for the selected quarter."
            ),
        },

        "ai_visibility": {
            "value": ai_current.get(
                "average_visibility_score",
                0
            ),
            "delta_pts": ai_visibility_delta,
            "compared_to": prior_q["label"],
            "source": "Profound",
            "message": (
                "Average AI-answer visibility score, "
                "compared to the prior quarter."
            ),
        },

        # =====================================================
        # META
        #
        # If Meta token expired:
        # meta_report_ttm = None
        # meta_totals = {}
        # value = 0
        #
        # Other cards continue working.
        # =====================================================

        "meta_cpl": {
            "value": meta_totals.get(
                "average_cpl",
                0
            ),
            "period_label": "Last 12 months · Live",
            "source": "Meta",
            "message": (
                "Average cost-per-lead on Meta "
                "over the trailing 12 months."
            ),
        },

        "ott_vcr": {
            "value": ott_vcr,
            "period_label": "Consult.TV actual",
            "date_range": (
                f"{q['start_date']} "
                f"to {q['end_date']}"
            ),
            "source": "Consult.TV",
            "message": (
                "Video completion rate for "
                "OTT/streaming TV ads in "
                "the selected quarter."
            ),
        },

        "bounce_rate": {
            "value": ga4_summary.get(
                "bounce_rate",
                0
            ),
            "driven_by": driver_channel,
            "period_label": q["label"],
            "source": "Agency Analytics GA4",
            "message": (
                "Overall site bounce rate, "
                "with the channel most responsible "
                "for it."
            ),
        },

        "google_ads_ctr": {
            "value": ads_summary.get(
                "ctr",
                0
            ),
            "period_label": (
                "Live"
                if q.get("is_live")
                else "Final"
            ),
            "source": (
                "Agency Analytics · googleadwords"
            ),
            "message": (
                "Click-through rate across enabled "
                "Google Ads campaigns."
            ),
        },
    }

    # =========================================================
    # 8-Quarter Channel Performance
    # =========================================================

    per_quarter_channels = [
        sessions_by_channel(r)
        for r in eight_quarter_ga4
    ]

    totals: dict[str, int] = defaultdict(int)

    for channel_sessions in per_quarter_channels:

        for channel, sessions in channel_sessions.items():
            totals[channel] += sessions

    top_3 = [
        c
        for c, _ in sorted(
            totals.items(),
            key=lambda x: x[1],
            reverse=True,
        )[:3]
    ]

    channel_series = []

    for eq, channel_sessions in zip(
        eight_quarters,
        per_quarter_channels,
    ):

        channel_series.append({
            "label": eq["tab_label"],
            "start_date": eq["start_date"],
            "end_date": eq["end_date"],
            **{
                channel: channel_sessions.get(
                    channel,
                    0
                )
                for channel in top_3
            },
        })

    # =========================================================
    # Errors
    #
    # Only failed services appear here.
    # =========================================================

    errors = {}

    if ga4_error is not None:
        errors["ga4"] = ga4_error

    if google_ads_error is not None:
        errors["google_ads"] = google_ads_error

    if meta_error is not None:
        errors["meta_ads"] = meta_error

    if ai_visibility_current_error is not None:
        errors["ai_visibility_current"] = (
            ai_visibility_current_error
        )

    if ai_visibility_prior_error is not None:
        errors["ai_visibility_prior"] = (
            ai_visibility_prior_error
        )

    # =========================================================
    # Final Response
    # =========================================================

    return {
        "quarter": q,

        "available_tabs": [
            t["tab_label"]
            for t in tabs
        ],

        "cards": cards,

        "errors": errors,

        "channel_performance_8q": {
            "top_channels": top_3,

            "series": channel_series,

            "range_label": (
                f"{eight_quarters[0]['label']} - "
                f"{eight_quarters[-1]['label']}"
            ),

            "message": (
                "Session trend across the top 3 "
                "traffic channels over the last "
                "8 quarters."
            ),
        },

        "message": {
            "quarter": (
                "The quarter this review covers, "
                "and whether it's still in progress."
            ),

            "available_tabs": (
                "The last 4 quarters available "
                "for selection in the UI."
            ),

            "cards": (
                "Headline KPI cards summarizing "
                "performance for the selected quarter."
            ),

            "channel_performance_8q": (
                "Session trend across the top 3 "
                "traffic channels over the last "
                "8 quarters."
            ),
        },
    }

def _iso_week_key(day_str: str) -> str:
    d = date.fromisoformat(day_str)
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()

_CONSULT_TV_FILE_TYPES = ("ott_all", "ott_colorado", "ott_phoenix", "ott_wyoming")

@router.get("/media")
async def media_tab(
    start_date: Optional[str] = Query(None, description="Filter start date, format YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="Filter end date, format YYYY-MM-DD"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    start = _safe_date(start_date) if start_date else None
    end = _safe_date(end_date) if end_date else None

    google_ads_report = await asyncio.to_thread(
        get_google_ads_report, start_date=start_date, end_date=end_date
    )

    all_campaigns = google_ads_report.get("campaigns", [])
    youtube_campaigns = [c for c in all_campaigns if c.get("channel_type") == "VIDEO"]

    # Weekly Impressions & CR
    youtube_daily = google_ads_report.get("youtube_daily", [])

    weekly_buckets: dict = {}
    for row in youtube_daily:
        week_start = _iso_week_key(row["day"])
        b = weekly_buckets.setdefault(week_start, {"impressions": 0, "conversions": 0.0})
        b["impressions"]  += row["impressions"]
        b["conversions"]  += row["conversions"]

    youtube_weekly_series = [
        {
            "week_start": week,
            "impressions": b["impressions"],
            "conversions": round(b["conversions"], 2),
            "cr_pct": round(b["conversions"] / b["impressions"] * 100, 2) if b["impressions"] else None,
        }
        for week, b in sorted(weekly_buckets.items())
    ]

    youtube_weekly_block = {
        "title": "YouTube — Weekly Impressions & CR",
        "series": youtube_weekly_series,
    } if youtube_weekly_series else {
        "title": "YouTube — Weekly Impressions & CR",
        "series": [],
        "note": "No Avaliable Data",
    }

    youtube_block = {
        "campaigns": youtube_campaigns,
        "video_metrics": google_ads_report.get("youtube_video_metrics", []),
        "weekly": youtube_weekly_block,
        "summary": {
            "spend": round(sum(c["spend"] for c in youtube_campaigns), 2),
            "impressions": sum(c["impressions"] for c in youtube_campaigns),
            "clicks": sum(c["clicks"] for c in youtube_campaigns),
            "conversions": round(sum(c["conversions"] for c in youtube_campaigns), 2),
            "message": "Totals across all YouTube (video channel) Google Ads campaigns.",
        },
        "message": "YouTube video campaign performance, pulled from Google Ads.",
    }

    q_all = (
        db.query(OttAllCampaignsRecord)
        .join(MarketingUpload, MarketingUpload.id == OttAllCampaignsRecord.upload_id)
        .filter(MarketingUpload.file_type.in_(_CONSULT_TV_FILE_TYPES))
    )
    if start:
        q_all = q_all.filter(OttAllCampaignsRecord.day >= start)
    if end:
        q_all = q_all.filter(OttAllCampaignsRecord.day <= end)
    all_rows = q_all.all()

    ct_total_spend       = sum(r.spend for r in all_rows) if all_rows else 0.0
    ct_total_impressions = sum(r.impressions for r in all_rows) if all_rows else 0
    ct_total_completions = sum(r.completions for r in all_rows) if all_rows else 0
    ct_total_leads       = sum(r.leads for r in all_rows) if all_rows else 0
    ct_total_page_visits = sum(r.page_visits for r in all_rows) if all_rows else 0

    daily_rows = all_rows

    daily_buckets: dict = {}
    for r in daily_rows:
        if r.day is None:
            continue
        b = daily_buckets.setdefault(r.day, {
            "spend": 0.0, "impressions": 0, "completions": 0,
            "leads": 0, "page_visits": 0,
        })
        b["spend"]       += r.spend
        b["impressions"] += r.impressions
        b["completions"] += r.completions
        b["leads"]        += r.leads
        b["page_visits"] += r.page_visits

    consult_tv_daily = [
        {
            "day": day.isoformat(),
            "spend": round(b["spend"], 2),
            "impressions": b["impressions"],
            "completions": b["completions"],
            "vcr_pct": round(b["completions"] / b["impressions"] * 100, 2) if b["impressions"] else None,
            "leads": b["leads"],
            "page_visits": b["page_visits"],
        }
        for day, b in sorted(daily_buckets.items())
    ]

    q_camp = (
        db.query(OttCampaignRecord)
        .join(MarketingUpload, MarketingUpload.id == OttCampaignRecord.upload_id)
        .filter(MarketingUpload.file_type.in_(_CONSULT_TV_FILE_TYPES))
    )
    if start:
        q_camp = q_camp.filter(OttCampaignRecord.day >= start)
    if end:
        q_camp = q_camp.filter(OttCampaignRecord.day <= end)
    camp_rows = q_camp.all()

    campaign_summary: dict = {}
    for r in camp_rows:
        bucket = campaign_summary.setdefault(r.campaign_name, {
            "spend": 0.0, "impressions": 0, "leads": 0, "reach": 0,
        })
        bucket["spend"]       += r.spend
        bucket["impressions"] += r.impressions
        bucket["leads"]       += r.leads
        bucket["reach"]       += r.reach

    consult_tv_campaigns = [
        {
            "campaign": campaign,
            "spend": round(data["spend"], 2),
            "impressions": data["impressions"],
            "leads": data["leads"],
            "reach": data["reach"],
            "cpm": round(data["spend"] / data["impressions"] * 1000, 2) if data["impressions"] else None,
            "cpl": round(data["spend"] / data["leads"], 2) if data["leads"] else None,
        }
        for campaign, data in sorted(campaign_summary.items(), key=lambda x: x[1]["spend"], reverse=True)
    ]

    completion_summary: dict = {}
    for r in camp_rows:
        bucket = completion_summary.setdefault(r.campaign_name, {
            "impressions": 0,
            "completions": 0,
        })
        bucket["impressions"] += r.impressions
        bucket["completions"] += getattr(r, "completions", 0)

    consult_tv_completion_rates = [
        {
            "campaign": campaign,
            "impressions": data["impressions"],
            "vcr_pct": round(data["completions"] / data["impressions"] * 100, 2) if data["impressions"] else None,
        }
        for campaign, data in sorted(completion_summary.items(), key=lambda x: x[1]["impressions"], reverse=True)
    ]

    geo_summary: dict = {}
    for r in camp_rows:
        dma = r.dma or "Unknown"
        bucket = geo_summary.setdefault(dma, {"spend": 0.0, "impressions": 0, "leads": 0})
        bucket["spend"]       += r.spend
        bucket["impressions"] += r.impressions
        bucket["leads"]       += r.leads

    consult_tv_geo = {
        "rows": [
            {
                "dma": dma,
                "spend": round(data["spend"], 2),
                "impressions": data["impressions"],
                "leads": data["leads"],
            }
            for dma, data in sorted(geo_summary.items(), key=lambda x: x[1]["impressions"], reverse=True)
        ]
    }

    ct_days = [r.day for r in camp_rows if r.day] + [r.day for r in all_rows if r.day]
    if ct_days:
        ct_start, ct_end = min(ct_days), max(ct_days)
        consult_tv_period = (
            f"{ct_start.strftime('%b')} {ct_start.day}"
            f"–{ct_end.strftime('%b')} {ct_end.day}, {ct_end.year}"
        )
    else:
        consult_tv_period = None

    if all_rows or camp_rows:
        consult_tv_kpis = {
            "spend": round(ct_total_spend, 2),
            "impressions": ct_total_impressions,
            "completions": ct_total_completions,
            "vcr_pct": round(ct_total_completions / ct_total_impressions * 100, 2) if ct_total_impressions else None,
            "cpm": round(ct_total_spend / ct_total_impressions * 1000, 2) if ct_total_impressions else None,
            "leads": ct_total_leads,
            "page_visits": ct_total_page_visits,
            "lead_cpa": round(ct_total_spend / ct_total_leads, 2) if ct_total_leads else None,
            "period": consult_tv_period,
        }
    else:
        consult_tv_kpis = {
            "spend": None, "impressions": None, "completions": None,
            "vcr_pct": None, "cpm": None, "leads": None,
            "page_visits": None, "lead_cpa": None, "period": None,
            "note": "No Consult.TV data uploaded yet",
        }

    consult_tv_block = {
        "vendor": "Consult.TV",
        "kpis": consult_tv_kpis,
        "campaigns": consult_tv_campaigns,
        "completion_rates": consult_tv_completion_rates,
        "daily": consult_tv_daily,
        "geo": consult_tv_geo,
        "message": {
            "kpis": "Overall Consult.TV OTT campaign totals for the selected date range.",
            "campaigns": "Spend, leads, and reach broken down per Consult.TV campaign.",
            "completion_rates": "Video completion rate (VCR) per Consult.TV campaign.",
            "daily": "Day-by-day Consult.TV spend, impressions, completions, leads, and page visits.",
            "geo": "Consult.TV spend and results broken down by DMA (geographic market).",
        }
    }

    q_jpr = (
        db.query(OttConsultTvMonthlyRecord)
        .join(MarketingUpload, MarketingUpload.id == OttConsultTvMonthlyRecord.upload_id)
    )
    if start:
        q_jpr = q_jpr.filter(OttConsultTvMonthlyRecord.month_date >= start)
    if end:
        q_jpr = q_jpr.filter(OttConsultTvMonthlyRecord.month_date <= end)
    jpr_rows = q_jpr.all()

    if jpr_rows:
        monthly_buckets: dict = {}
        for r in jpr_rows:
            key = r.month_date or r.month
            b = monthly_buckets.setdefault(key, {
                "month": r.month,
                "month_date": r.month_date,
                "impressions": 0,
                "completed_views": 0,
                "spend": 0.0,
                "campaign_count": 0,
            })
            b["impressions"] += r.impressions or 0
            b["completed_views"] += r.completed_views or 0
            b["spend"] += r.spend or 0.0
            b["campaign_count"] += 1

        def _sort_key(item):
            b = item[1]
            return b["month_date"] or date.min

        jpr_monthly_trend = [
            {
                "month": b["month"],
                "impressions": b["impressions"],
                "completed_views": b["completed_views"],
                "vcr_pct": round(b["completed_views"] / b["impressions"] * 100, 2) if b["impressions"] else None,
                "spend": round(b["spend"], 2),
                "cpm": round(b["spend"] / b["impressions"] * 1000, 2) if b["impressions"] else None,
                "campaign_count": b["campaign_count"],
            }
            for _, b in sorted(monthly_buckets.items(), key=_sort_key)
        ]

        jpr_total_impr  = sum(r.impressions or 0 for r in jpr_rows)
        jpr_total_views = sum(r.completed_views or 0 for r in jpr_rows)
        jpr_total_spend = sum(r.spend or 0.0 for r in jpr_rows)

        jpr_kpis = {
            "spend_total": round(jpr_total_spend, 2),
            "spend_latest_month": jpr_monthly_trend[-1]["spend"] if jpr_monthly_trend else None,
            "vcr_weighted_pct": round(jpr_total_views / jpr_total_impr * 100, 2) if jpr_total_impr else None,
            "impressions_total": jpr_total_impr,
            "completed_views_total": jpr_total_views,
            "cpm_blended": round(jpr_total_spend / jpr_total_impr * 1000, 2) if jpr_total_impr else None,
            "campaign_count": len({r.campaign_id or r.campaign_name for r in jpr_rows}),
            "months_covered": len(jpr_monthly_trend),
        }

        market_buckets: dict = {}
        for r in jpr_rows:
            market = r.market or "Unknown"
            b = market_buckets.setdefault(market, {
                "impressions": 0, "completed_views": 0, "spend": 0.0, "campaign_count": 0,
            })
            b["impressions"] += r.impressions or 0
            b["completed_views"] += r.completed_views or 0
            b["spend"] += r.spend or 0.0
            b["campaign_count"] += 1

        jpr_geo_market_breakdown = [
            {
                "market": market,
                "impressions": b["impressions"],
                "completed_views": b["completed_views"],
                "vcr_pct": round(b["completed_views"] / b["impressions"] * 100, 2) if b["impressions"] else None,
                "spend": round(b["spend"], 2),
                "cpm": round(b["spend"] / b["impressions"] * 1000, 2) if b["impressions"] else None,
                "campaign_count": b["campaign_count"],
            }
            for market, b in sorted(market_buckets.items(), key=lambda x: x[1]["impressions"], reverse=True)
        ]

        months_with_dates = sorted(
            (b["month_date"], b["month"]) for b in monthly_buckets.values() if b["month_date"]
        )
        if months_with_dates:
            start_label = months_with_dates[0][0].strftime("%b")
            end_label = months_with_dates[-1][0].strftime("%b %Y")
            jpr_period = f"{start_label}–{end_label}"
        else:
            jpr_period = None

        jpr_kpis["period"] = jpr_period
    else:
        jpr_monthly_trend = []
        jpr_geo_market_breakdown = []
        jpr_period = None
        jpr_kpis = {
            "spend_total": None, "spend_latest_month": None, "vcr_weighted_pct": None,
            "impressions_total": None, "completed_views_total": None, "cpm_blended": None,
            "campaign_count": 0, "months_covered": 0, "period": None,
            "note": "No JPR CTV/OLV monthly data uploaded yet",
        }

    jpr_block = {
        "vendor": "JPR",
        "kpis": jpr_kpis,
        "monthly_trend": jpr_monthly_trend,
        "geo_market": jpr_geo_market_breakdown,
        "message": {
            "kpis": "Overall JPR CTV/OLV campaign totals for the selected date range.",
            "monthly_trend": "Month-by-month JPR CTV/OLV spend, impressions, and completed views.",
            "geo_market": "JPR CTV/OLV spend and results broken down by market.",
        }
    }

    jpr_spend        = jpr_kpis.get("spend_total") or 0.0
    consult_tv_spend = consult_tv_kpis.get("spend") or 0.0
    combined_spend    = round(jpr_spend + consult_tv_spend, 2)

    combined = {
        "total_spend": combined_spend,
        "message": "Combined OTT spend across both vendors (JPR + Consult.TV), with each vendor's share.",
        "vendor_breakdown": [
            {
                "vendor": "JPR",
                "spend": jpr_kpis.get("spend_total"),
                "pct": round(jpr_spend / combined_spend * 100, 1) if combined_spend else None,
                "period": jpr_period,
            },
            {
                "vendor": "Consult.TV",
                "spend": consult_tv_kpis.get("spend"),
                "pct": round(consult_tv_spend / combined_spend * 100, 1) if combined_spend else None,
                "period": consult_tv_period,
            },
        ],
    }

    footer_parts = []
    if jpr_period:
        footer_parts.append(f"JPR CTV/OLV ({jpr_period})")
    if consult_tv_period:
        footer_parts.append(f"Consult.TV OTT ({consult_tv_period})")

    ott_footer = {
        "text": " · ".join(footer_parts) if footer_parts else "No OTT data uploaded yet",
        "jpr_period": jpr_period,
        "consult_tv_period": consult_tv_period,
    }

    return {
        "ott": {
            "jpr": jpr_block,
            "consult_tv": consult_tv_block,
            "combined": combined,
            "footer": ott_footer,
            "message": "OTT/streaming TV performance, combined across the jpr and Consult.TV vendors.",
        },
        "youtube": youtube_block,
        "podcasts": {
            "episodes": [],
            "message": "Podcast advertising performance",
        },
        "message": {
            "ott": "OTT/streaming TV performance, combined across the jpr and Consult.TV vendors.",
            "youtube": "YouTube video campaign performance, pulled from Google Ads.",
            "podcasts": "Podcast advertising performance ",
        }
    } 


#---------------- AI services ------------------# 
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

TAB_FOCUS = {
    "website_performance": """
Focus on:
- Website traffic
- User acquisition
- User engagement
- Bounce rate
- Device distribution
- Traffic sources
- Campaign tracking
- SEO performance
""",

    "aeo_ai_visibility": """
Focus on:
- Share of Voice
- Visibility Score
- AI platform performance
- Competitor comparison
- Directory presence
- Practice area visibility
""",

    "google_ads": """
Focus on:
- Campaign performance
- Spend
- Clicks
- Impressions
- CTR
- CPC
- Conversions
- Cost per Conversion
- ROAS
""",

    "media": """
Focus on:
- Media reach
- Impressions
- Video completion
- Frequency
- CPM
- Channel performance
- Geographic performance
"""
}


async def build_prompt(tab: str, start_date: str, end_date: str, data: dict):

    dashboard_name = tab.replace("_", " ").title()

    focus = TAB_FOCUS.get(tab, "")

    return f"""
You are an expert Digital Marketing Analyst.
Analyze the **{dashboard_name}** dashboard.

Reporting Period:
{start_date} to {end_date}

Dashboard Data:
{json.dumps(data, indent=2)}

Task:
Generate exactly 3 concise insights from the dashboard data.

{focus}

Rules:
- Use only the provided data.
- Do not assume missing metrics.
- Keep insights concise.
- Return ONLY valid JSON.

Return this format:

{{
  "insights": [
    "",
    "",
    ""
  ]
}}
"""

async def generate_ai_summary(prompt):
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.1,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": "You are an expert Digital Marketing Analyst."
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    return json.loads(response.choices[0].message.content)


SUPPORTED_TABS = {
    "website_performance",
    "aeo_ai_visibility",
    "google_ads",
    "media"
}


@router.post("/ai-summary")
async def ai_summary(payload: dict = Body(...)):

    tab = payload["tab"]

    if tab not in SUPPORTED_TABS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported dashboard tab."
        )

    prompt = await build_prompt(
        tab=tab,
        start_date=payload["start_date"],
        end_date=payload["end_date"],
        data=payload["page_data"]
    )

    result = await generate_ai_summary(prompt)

    return {
        "success": True,
        "data": result
    }

from marketing_dashboard.whiteboard import router as whiteboard_router
router.include_router(whiteboard_router, prefix="/whiteboard")

from marketing_dashboard.referral_partners import router as referral_partners_router
router.include_router(referral_partners_router, prefix="/referral-partners")