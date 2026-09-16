import requests

API_URL = "http://127.0.0.1:8000/api/v1/auth/login"

def test_login(user_id, email_or_phone, password):
    print(f"Testing login for: ID={user_id}, Identifier={email_or_phone}")
    try:
        response = requests.post(API_URL, json={
            "user_id": user_id, 
            "email_or_phone": email_or_phone, 
            "password": password
        })
        if response.status_code == 200:
            print(f"✅ Success: {response.json()}")
        else:
            print(f"❌ Failed: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"⚠️ Error: {e}")

# Test Worker (User 1)
test_login("1", "test@example.com", "testpassword")

# Test Admin (User 5000)
test_login("5000", "admin@siteops.com", "admin")

# Test Failure (Wrong Password)
test_login("1", "test@example.com", "wrongpassword")
