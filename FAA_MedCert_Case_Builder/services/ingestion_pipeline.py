import base64
import json
from logging import root
import zipfile
import tempfile
import os
from fastapi import UploadFile
from sqlalchemy.orm import Session
from FAA_MedCert_Case_Builder.services.blob_search import upload_blob
from FAA_MedCert_Case_Builder.services.index_service import delete_by_session
from FAA_MedCert_Case_Builder.services.llm_service import generate_answer
from session_redis import set_session, get_session, delete_session
from db import MedCertRuns

async def extract_zip_files(zip_bytes: bytes, zip_filename: str):
    extracted_files = []
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = os.path.join(temp_dir, zip_filename)
        with open(zip_path, "wb") as f:
            f.write(zip_bytes)

        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            print("ZIP CONTENTS:", zip_ref.namelist())
            zip_ref.extractall(temp_dir)


        for root, dirs, files in os.walk(temp_dir):
            for file in files:
                if file == zip_filename:
                    continue

                # Track folder path
                relative_folder = os.path.relpath(root, temp_dir)

                if relative_folder == ".":
                    relative_folder = "root"

                if not file.lower().endswith((".pdf", ".docx")):
                    print(f"Skipping unsupported file: {file}")
                    continue

                file_path = os.path.join(root, file)

                with open(file_path, "rb") as f:
                    file_bytes = f.read()

                document_path = f"{relative_folder}/{file}" 
                extracted_files.append({
                    "filename": file,
                    "bytes": base64.b64encode(file_bytes).decode("utf-8"),
                    "document_path": document_path
                })
    return extracted_files

async def store_session(session_id, user_email, zip_filename, extracted_files=None,  processed_data=None, status="uploaded"):
    await set_session(session_id, {
        "user_email": user_email,
        "zip": zip_filename,
        "extracted_files": extracted_files,
        "processed_data": processed_data,
        "status": status
    })
    
async def upload_and_extract(file: UploadFile):

    zip_filename = file.filename
    await upload_blob(file.file, zip_filename)   
    file.file.seek(0) 
    file_bytes = await file.read()
    extracted_files = await extract_zip_files(file_bytes, zip_filename) 
    return zip_filename, extracted_files
 
async def case_generate(session_id: str, user_email: str, db: Session):
    session = await get_session(session_id)
    if not session:
        raise Exception("Session expired or not found")
    try:
        answer, token_info = await generate_answer(session_id)
        structured_data = json.loads(answer)
        db.add(MedCertRuns(
            session_id=session_id,
            user_email=user_email,
            case_header=structured_data.get("case_header"),
            addressee_logic=structured_data.get("addressee_logic"),
            procedural_stage=structured_data.get("procedural_stage"),
            documents=structured_data.get("documents"),
            taxonomy=structured_data.get("taxonomy"),
            issue_stack=structured_data.get("issue_stack"),
            fact_model=structured_data.get("fact_model"),
            timeline=structured_data.get("timeline"),
            missing_items=structured_data.get("missing_items"),
            guideline_checklists=structured_data.get("guideline_checklists"),
            draft_outline=structured_data.get("draft_outline")
        ))
        db.commit()
        return {
            "session_id": session_id,
            "llm_output": answer,
            "token_usage": token_info
        }
    except Exception as e:
        print("LLM error:", e)
        raise e
    finally:
        await delete_by_session(session_id)  # Azure search
        await delete_session(session_id)  # Redis