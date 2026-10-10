"""Initialize database and create initial admin account."""

import os
import sys
import argparse
from datetime import datetime

from apps.web.db.session import init_db, get_db_session
from apps.web.db.models import AccountModel, AccountRole
from apps.web.accounts.service import AccountService


def init_database():
    """Initialize database tables."""
    print("Initializing database...")
    init_db()
    print("✓ Database tables created")


def create_admin_from_env():
    """Create admin account from environment variables."""
    admin_user = os.getenv("DSWARM_ADMIN_USERNAME", "admin")
    admin_pass = os.getenv("DSWARM_ADMIN_PASSWORD")
    
    if not admin_pass:
        print("Warning: DSWARM_ADMIN_PASSWORD not set, skipping admin creation")
        return
    
    db = get_db_session()
    try:
        service = AccountService(db)
        
        # Check if admin already exists
        existing = db.query(AccountModel).filter_by(username=admin_user).first()
        if existing:
            print(f"✓ Admin account '{admin_user}' already exists")
            return
        
        # Create admin
        account = service.create_account(
            username=admin_user,
            display_name="Administrator",
            password=admin_pass,
            role=AccountRole.ADMIN
        )
        print(f"✓ Admin account created: {admin_user} ({account.id})")
        
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Initialize D-Swarm database")
    parser.add_argument("--init-db", action="store_true", help="Initialize database tables")
    parser.add_argument("--create-admin", action="store_true", help="Create admin from env vars")
    parser.add_argument("--admin-user", default="admin", help="Admin username")
    parser.add_argument("--admin-pass", help="Admin password")
    
    args = parser.parse_args()
    
    # If no args, do both
    if not any([args.init_db, args.create_admin]):
        args.init_db = True
        args.create_admin = True
    
    if args.init_db:
        init_database()
    
    if args.create_admin:
        if args.admin_pass:
            os.environ["DSWARM_ADMIN_PASSWORD"] = args.admin_pass
            if args.admin_user != "admin":
                os.environ["DSWARM_ADMIN_USERNAME"] = args.admin_user
        
        create_admin_from_env()
    
    print("\n✓ Database initialization complete")


if __name__ == "__main__":
    main()
