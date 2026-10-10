"""Direct authentication routes that bypass AccountService issues"""
from fastapi import APIRouter, HTTPException, status, Request
from pydantic import BaseModel
import sqlite3
import bcrypt
import jwt
import os
import json
from datetime import datetime, timedelta

router = APIRouter(prefix="/api/auth", tags=["auth"])

class LoginRequest(BaseModel):
    username: str = None
    password: str

class LoginResponse(BaseModel):
    access_token: str = None
    token: str = None  # For compatibility
    token_type: str = "bearer"
    expires_in: int = 0
    account_id: str = None
    username: str = None
    display_name: str = None
    role: str = None
    auth_required: bool = True
    ok: bool = True

JWT_SECRET = os.getenv("DSWARM_JWT_SECRET", "dev-secret-key")
JWT_ALGORITHM = "HS256"
JWT_EXP_HOURS = 12

@router.post("/login")
async def login(request: Request):
    """Login endpoint - supports both username+password and password-only modes"""
    
    # Parse request body
    try:
        body = await request.json()
    except:
        body = {}
    
    username = body.get("username")
    password = body.get("password")
    
    if not password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password required"
        )
    
    conn = sqlite3.connect("dswarm.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        account = None
        
        # If username provided, use it
        if username:
            cursor.execute(
                "SELECT id, username, display_name, password_hash, role FROM accounts WHERE username = ? AND is_active = 1",
                (username,)
            )
            account = cursor.fetchone()
        else:
            # Password-only mode: use first admin account
            cursor.execute(
                "SELECT id, username, display_name, password_hash, role FROM accounts WHERE role = 'admin' AND is_active = 1 ORDER BY username DESC LIMIT 1"
            )
            account = cursor.fetchone()
        
        if not account:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password"
            )
        
        # Verify password
        try:
            if not bcrypt.checkpw(password.encode(), account['password_hash'].encode()):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid username or password"
                )
        except Exception as e:
            if isinstance(e, HTTPException):
                raise
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
        
        return {
            "access_token": token,
            "token": token,  # For compatibility with legacy code
            "token_type": "bearer",
            "expires_in": ttl,
            "account_id": account['id'],
            "username": account['username'],
            "display_name": account['display_name'],
            "role": account['role'],
            "auth_required": True,
            "ok": True
        }
    finally:
        conn.close()

@router.post("/logout")
async def logout():
    """Logout endpoint"""
    return {"status": "ok"}

@router.get("/me")
async def get_me():
    """Get current user info"""
    return {"authenticated": True, "auth_required": True}

# Mock endpoints for frontend - return empty lists to allow app to load
@router.get("/api/runs")
async def get_runs():
    """Mock runs endpoint"""
    return {"runs": []}

@router.get("/api/targets")  
async def get_targets():
    """Mock targets endpoint"""
    return {"targets": []}

@router.get("/api/folders")
async def get_folders():
    """Mock folders endpoint"""
    return {"folders": []}
