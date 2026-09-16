import sqlite3

conn = sqlite3.connect("times.db")
cursor = conn.cursor()
cursor.execute("SELECT id, name, email, phone, role, password_hash FROM users")
for row in cursor.fetchall():
    print(row)
conn.close()
