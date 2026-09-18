import sqlite3
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_bcrypt_safe_password(password: str) -> str:
    password_bytes = password.encode('utf-8')
    if len(password_bytes) > 72:
        password_bytes = password_bytes[:72]
    return password_bytes.decode('utf-8', errors='ignore')

conn = sqlite3.connect("times.db")
cursor = conn.cursor()

user_id = "1"
name = "Test Worker"
raw_password = "123"  # Change this to whatever password you want
role = "worker"

try:
    hashed_pw = pwd_context.hash(get_bcrypt_safe_password(raw_password))
    cursor.execute(
        "INSERT INTO users (id, name, password_hash, role) VALUES (?, ?, ?, ?)",
        (user_id, name, hashed_pw, role)
    )
    conn.commit()
    print(f"✅ Worker {user_id} successfully created with password: {raw_password}")
except sqlite3.IntegrityError:
    print(f"⚠️ User ID {user_id} already exists in the database.")
finally:
    conn.close()