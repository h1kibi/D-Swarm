"""Direct test - bypass all service layers"""
import sys
sys.path.insert(0, '.')

from apps.web.accounts.service import AccountService
import sqlite3
import bcrypt

# Monkey-patch the verify_password method to use bcrypt directly
original_verify = AccountService.verify_password

def direct_verify_password(self, plain_password: str, hashed_password: str) -> bool:
    """Verify password using bcrypt directly"""
    try:
        return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())
    except Exception as e:
        print(f"Verify password error: {e}")
        return False

AccountService.verify_password = direct_verify_password

# Load accounts
conn = sqlite3.connect("dswarm.db")
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

cursor.execute("SELECT id, username, display_name, password_hash, role, is_active FROM accounts")
db_accounts = cursor.fetchall()
conn.close()

from apps.web.accounts.models import Account, AccountRole

for db_acc in db_accounts:
    account = Account(
        id=db_acc['id'],
        username=db_acc['username'],
        display_name=db_acc['display_name'],
        password_hash=db_acc['password_hash'],
        role=AccountRole(db_acc['role']),
        is_active=bool(db_acc['is_active'])
    )
    AccountService._accounts[account.id] = account

print(f"Loaded {len(AccountService._accounts)} accounts")

# Test authentication
service = AccountService()
result = service.authenticate("testadmin", "TestPass123!")
if result:
    print(f"SUCCESS: Authenticated as {result.username} ({result.role})")
else:
    print(f"FAILED: Authentication failed")

