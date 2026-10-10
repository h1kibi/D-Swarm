"""Direct authentication routes that bypass AccountService issues"""
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
import sqlite3
import bcrypt
import jwt
import os
from datetime import datetime, timedelta

router = APIRouter(prefix="/api/auth", tags=["auth"])

class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    account_id: str
    username: str
    display_name: str
    role: str

JWT_SECRET = os.getenv("DSWARM_JWT_SECRET", "dev-secret-key")
JWT_ALGORITHM = "HS256"
JWT_EXP_HOURS = 12

@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    """Direct login - bypasses AccountService"""
    conn = sqlite3.connect("dswarm.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            "SELECT id, username, display_name, password_hash, role FROM accounts WHERE username = ? AND is_active = 1",
            (req.username,)
        )
        account = cursor.fetchone()
        
        if not account:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password"
            )
        
        # Verify password
        if not bcrypt.checkpw(req.password.encode(), account['password_hash'].encode()):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password"
            )
        
        # Create JWT token
        exp = datetime.utcnow() + timedelta(hours=JWT_EXP_HOURS)
        payload = {
            'sub': account['id'],
            'username': account['username'],
            'role': account['role'],
            'exp': exp
        }
        token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
        ttl = int((exp - datetime.utcnow()).total_seconds())
        
        return LoginResponse(
            access_token=token,
            token_type="bearer",
            expires_in=ttl,
            account_id=account['id'],
            username=account['username'],
            display_name=account['display_name'],
            role=account['role']
        )
    finally:
        conn.close()

@router.post("/logout")
async def logout():
    """Logout endpoint"""
    return {"status": "ok"}

@router.get("/me")
async def get_me():
    """Get current user info"""
    return {"authenticated": True}
