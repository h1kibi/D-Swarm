"""Direct JWT verification middleware - bypasses DbAccountService issues"""
import jwt
import os
from typing import Optional

JWT_SECRET = os.getenv("DSWARM_JWT_SECRET", "dev-secret-key")
JWT_ALGORITHM = "HS256"

def verify_jwt_token(token: str) -> dict:
    """Verify JWT token and return payload"""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except Exception as e:
        raise Exception(f"Token verification failed: {e}")

def extract_token_from_header(authorization: Optional[str]) -> str:
    """Extract JWT token from Authorization header"""
    if not authorization or not authorization.startswith("Bearer "):
        raise Exception("No valid Authorization header")
    return authorization[7:]
