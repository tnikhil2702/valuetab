from fastapi import APIRouter, Depends, HTTPException
from admin.auth import get_current_admin
from pydantic import BaseModel, EmailStr
from db import get_db, Whitelist
from sqlalchemy.orm import Session

admin_router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    dependencies=[Depends(get_current_admin)] 
)

class WhitelistEntry(BaseModel):
    name: str
    email: EmailStr

# APIs Endpoints for managing the whitelist
@admin_router.get("/whitelist")
async def view_whitelist(db: Session = Depends(get_db)):

    records = db.query(Whitelist).filter(
        Whitelist.role == "user"
    ).all()

    members = [
        {
            "name": r.name,
            "email": r.email
        }
        for r in records
    ]
    return {"members": members}

@admin_router.post("/whitelist")
async def add_to_whitelist(entry: WhitelistEntry, db: Session = Depends(get_db)):
    existing = db.query(Whitelist).filter(
        Whitelist.email == entry.email.lower(),
        Whitelist.role == "user"
    ).first()

    if existing:
        return {
            "message": "Email already exists",
            "member": entry
        }
    
    db.add(Whitelist(
        name=entry.name,
        email=entry.email.lower(),
        role="user"
    ))

    db.commit()
    return {
        "message": "Member added successfully",
        "member": entry
    }

@admin_router.delete("/whitelist")
async def remove_from_whitelist(email: str, db: Session = Depends(get_db)):

    record = db.query(Whitelist).filter(
        Whitelist.email == email.lower(),
        Whitelist.role == "user"
    ).first()

    if not record:
        raise HTTPException(status_code=404, detail="Email not found in whitelist")

    db.delete(record)
    db.commit()

    return {
        "message": "Member removed successfully",
        "email": email
    }