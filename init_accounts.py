"""Initialize D-Swarm CTF team platform with first admin account.

Usage:
    python init_accounts.py --admin-username admin --admin-password changeme

This script creates the first admin account for the system.
It should only be run once during initial setup.
"""

import argparse
import sys
from apps.web.accounts.service import AccountService
from apps.web.accounts.models import AccountRole


def main():
    parser = argparse.ArgumentParser(description="Initialize D-Swarm accounts")
    parser.add_argument(
        "--admin-username",
        default="admin",
        help="Username for admin account"
    )
    parser.add_argument(
        "--admin-password",
        required=True,
        help="Password for admin account (must be at least 8 characters)"
    )
    parser.add_argument(
        "--admin-display-name",
        default="Administrator",
        help="Display name for admin account"
    )
    
    args = parser.parse_args()
    
    # Validate password length
    if len(args.admin_password) < 8:
        print("Error: Admin password must be at least 8 characters", file=sys.stderr)
        sys.exit(1)
    
    # Create service instance
    service = AccountService()
    
    # Check if admin already exists
    if service.get_account_by_username(args.admin_username):
        print(f"Error: Admin account '{args.admin_username}' already exists", file=sys.stderr)
        sys.exit(1)
    
    # Create admin account
    try:
        admin = service.create_account(
            username=args.admin_username,
            display_name=args.admin_display_name,
            password=args.admin_password,
            role=AccountRole.ADMIN,
            created_by=None  # First admin, no creator
        )
        
        print(f"✓ Admin account created successfully")
        print(f"  Account ID: {admin.id}")
        print(f"  Username: {admin.username}")
        print(f"  Display Name: {admin.display_name}")
        print(f"  Role: {admin.role.value}")
        print()
        print("Next steps:")
        print("1. Start the server: ./run.sh web")
        print("2. Login with username/password at http://localhost:3001")
        print("3. Create team member accounts from the admin dashboard")
        
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
