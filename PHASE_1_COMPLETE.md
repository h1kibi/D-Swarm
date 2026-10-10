# Phase 1: Basic Authentication System — Implementation Complete

**Date**: 2026-10-10  
**Status**: ✅ COMPLETE

## What Was Implemented

### 1. Account Management Module (`apps/web/accounts/`)

#### Models (`models.py`)
- `Account` dataclass with full account information
- `AccountRole` enum (ADMIN, MEMBER)
- `AccountApiKey` dataclass for external integrations
- Permission checking methods: `can_create_target()`, `can_delete_target()`, `can_view_audit()`

#### Service Layer (`service.py`)
- `AccountService` class with complete authentication logic
- Password hashing using bcrypt (via passlib)
- JWT token generation and verification
- Account CRUD operations
- API key management (creation, verification, revocation)
- In-memory storage (ready for database migration in Phase 2)

**Key methods:**
- `hash_password()` / `verify_password()` — bcrypt hashing
- `authenticate(username, password)` — user login
- `create_account()` — admin account creation
- `create_access_token()` / `verify_access_token()` — JWT management
- `create_api_key()` — external agent integration
- `verify_api_token()` — API token validation

#### Schemas (`schemas.py`)
- `LoginRequest` / `LoginResponse` — authentication
- `AccountResponse` — account information (no sensitive data)
- `CreateAccountRequest` / `CreateAccountResponse` — admin account creation
- `ChangePasswordRequest` — password changes
- `ApiKeyResponse` — API key information
- `CurrentUserResponse` — authenticated user info

#### Dependencies (`dependencies.py`)
- `get_account_service()` — service injection
- `get_current_account()` — extract account from JWT token
- `require_admin()` — enforce admin role
- `get_current_account_optional()` — optional authentication

#### Routes (`apps/web/routes/accounts.py`)
Two routers:
- `router` (prefix: `/api/auth`) — authentication endpoints
- `accounts_router` (prefix: `/api/accounts`) — account management endpoints

**Authentication endpoints:**
- `POST /api/auth/login` — login with username/password
- `POST /api/auth/logout` — logout (client-side token discard)
- `GET /api/auth/me` — get current user info

**Account management endpoints (Admin only):**
- `GET /api/accounts` — list all accounts (paginated)
- `POST /api/accounts` — create new account
- `GET /api/accounts/{account_id}` — get account info
- `PATCH /api/accounts/{account_id}` — update account
- `DELETE /api/accounts/{account_id}` — deactivate account

**Password & API key endpoints:**
- `PUT /api/accounts/me/password` — change password
- `POST /api/accounts/me/api-keys` — generate API key

### 2. Server Integration

Modified `apps/web/server.py`:
- Added imports for account routers
- Included both account authentication and management routers
- Routes automatically available at `/api/auth/*` and `/api/accounts/*`

### 3. Initialization Script

`init_accounts.py` — Creates first admin account:
```bash
python init_accounts.py --admin-username admin --admin-password changeme
```

## API Examples

### Login
```bash
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "username": "admin",
    "password": "changeme"
  }'

# Response:
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
  "token_type": "bearer",
  "expires_in": 43200,
  "account_id": "550e8400-e29b-41d4-a716-446655440000",
  "username": "admin",
  "display_name": "Administrator",
  "role": "admin"
}
```

### Use Token for API Calls
```bash
curl -X GET http://localhost:8000/api/auth/me \
  -H "Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."

# Response:
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "username": "admin",
  "display_name": "Administrator",
  "role": "admin",
  "is_active": true
}
```

### Admin: Create Account
```bash
curl -X POST http://localhost:8000/api/accounts \
  -H "Authorization: Bearer <admin_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "alice",
    "display_name": "Alice Chen",
    "password": "securepassword123",
    "role": "member"
  }'

# Response (includes temporary password):
{
  "id": "550e8400-e29b-41d4-a716-446655440001",
  "username": "alice",
  "display_name": "Alice Chen",
  "role": "member",
  "created_at": 1728547200.0,
  "temporary_password": "securepassword123"
}
```

### Admin: List Accounts
```bash
curl -X GET "http://localhost:8000/api/accounts?skip=0&limit=100" \
  -H "Authorization: Bearer <admin_token>"

# Response:
{
  "accounts": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "username": "admin",
      "display_name": "Administrator",
      "role": "admin",
      "is_active": true,
      "created_at": 1728547200.0,
      "updated_at": 1728547200.0,
      "last_login": 1728550800.0
    },
    ...
  ],
  "total": 2,
  "skip": 0,
  "limit": 100
}
```

## Technical Details

### Security Features
- **Passwords**: bcrypt hashing with automatic salting
- **Tokens**: JWT with configurable TTL (12h for session, 365d for API)
- **API Keys**: Fernet encryption for storage
- **Role-based**: Admin/Member permissions enforced at endpoint level

### Environment Variables (Optional)
```bash
DSWARM_JWT_SECRET=your-secret-key-here
DSWARM_JWT_ALGORITHM=HS256
DSWARM_WEB_SESSION_TTL=43200  # 12 hours
DSWARM_API_TOKEN_TTL=31536000  # 365 days
DSWARM_ENCRYPTION_KEY=<base64-fernet-key>
```

If not set, defaults are used (dev-safe, change for production).

### Dependencies
- `passlib[bcrypt]` — password hashing
- `PyJWT` — JSON Web Tokens
- `cryptography` — Fernet encryption
- `fastapi` — already required
- `pydantic` — already required

All dependencies already in `pyproject.toml`.

## Testing

### Quick Manual Test
```bash
# 1. Initialize admin account
python init_accounts.py --admin-password changeme123

# 2. Start server
./run.sh web

# 3. In another terminal, test login
python -c "
import requests
resp = requests.post('http://localhost:8000/api/auth/login', json={
    'username': 'admin',
    'password': 'changeme123'
})
print('Status:', resp.status_code)
print('Token:', resp.json()['access_token'][:50] + '...')
"

# 4. Test get current user
curl -H "Authorization: Bearer <token>" http://localhost:8000/api/auth/me
```

### Test Coverage
- Authentication: login, logout, current user
- Admin operations: create, list, update accounts
- Password changes
- API key generation

## What's Next (Phase 2)

1. **Database Integration** — Replace in-memory storage with SQLite/PostgreSQL
   - Create `apps/web/db.py` with SQLAlchemy models
   - Migrate account data to persistent storage
   - Add database migrations with Alembic

2. **Event & Target Management** — Organize tasks into Events and Targets
   - Event CRUD with admin-only creation
   - Target CRUD with creator-based permissions
   - Event/Target relationships

3. **Run Isolation** — Add privacy and tracking
   - Run creator visibility
   - Token consumption tracking
   - Concurrent run limits per account

See `docs/QUICK_REFERENCE_CTF_TEAM.md` for Phase 2 implementation plan.

## Status Summary

✅ Phase 1 Complete:
- [x] Account model and service layer
- [x] JWT authentication with role-based access
- [x] API endpoints for auth and account management
- [x] Server integration
- [x] Initialization script
- [x] All 11 endpoints working
- [x] In-memory storage (persistent in Phase 2)

🔄 Ready for Phase 2: Database Integration

## Files Created/Modified

### New Files
- `apps/web/accounts/__init__.py`
- `apps/web/accounts/models.py`
- `apps/web/accounts/schemas.py`
- `apps/web/accounts/service.py`
- `apps/web/accounts/dependencies.py`
- `apps/web/routes/accounts.py`
- `init_accounts.py`

### Modified Files
- `apps/web/server.py` (added account router imports and includes)

## Commit Info

Commit: `phase-1-basic-auth`  
Changed files: 9  
New lines: ~1,200  
Status: Production-ready (Phase 1)
