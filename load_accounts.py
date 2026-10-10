"""Load accounts from database into AccountService on startup"""
import sqlite3
import sys

DB_PATH = "dswarm.db"

def load_accounts_into_service():
    """Load accounts from SQLite into AccountService memory"""
    try:
        from apps.web.accounts.service import AccountService
        from apps.web.accounts.models import Account, AccountRole
        
        # Get all accounts from SQLite
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT id, username, display_name, password_hash, role, is_active FROM accounts")
        db_accounts = cursor.fetchall()
        conn.close()
        
        if not db_accounts:
            print("WARNING: No accounts found in database!")
            return False
        
        # Load into AccountService
        for db_acc in db_accounts:
            try:
                account = Account(
                    id=db_acc['id'],
                    username=db_acc['username'],
                    display_name=db_acc['display_name'],
                    password_hash=db_acc['password_hash'],
                    role=AccountRole(db_acc['role']),
                    is_active=bool(db_acc['is_active'])
                )
                AccountService._accounts[account.id] = account
            except Exception as e:
                print(f"ERROR loading account {db_acc['username']}: {e}")
                return False
        
        print(f"[STARTUP] Loaded {len(AccountService._accounts)} accounts from database")
        return True
        
    except Exception as e:
        print(f"ERROR: Could not load accounts: {e}")
        return False

if __name__ == "__main__":
    if load_accounts_into_service():
        sys.exit(0)
    else:
        sys.exit(1)
