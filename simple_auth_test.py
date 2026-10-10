"""Test authentication directly"""
import sqlite3
import bcrypt

DB_PATH = "dswarm.db"

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Get testadmin account
cursor.execute("SELECT username, password_hash FROM accounts WHERE username = 'testadmin'")
row = cursor.fetchone()

if not row:
    print("No testadmin account found!")
else:
    username, password_hash = row
    print(f"Username: {username}")
    print(f"Hash: {password_hash[:50]}...")
    
    # Try to verify password
    try:
        result = bcrypt.checkpw("TestPass123!".encode(), password_hash.encode())
        print(f"Password verification: {result}")
    except Exception as e:
        print(f"Error during verification: {e}")
        print("Hash format might be incorrect")

conn.close()

