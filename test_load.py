import requests
import time

url = "http://127.0.0.1:8000/api/v1/attendance/verify"
test_image_path = "./local_references/1.jpg"

print("🚀 Starting 50-request stress test...")
success_count, fail_count = 0, 0

for i in range(1, 51):
    # Alternate Clock In/Out so SQLite doesn't block us for being "already clocked in"
    action = "Clock In" if i % 2 != 0 else "Clock Out"
    
    data_payload = {
        "worker_id": "1",
        "action": action,
        "latitude": "30.050010",
        "longitude": "31.230010"
    }
    
    with open(test_image_path, "rb") as img_file:
        file_payload = {"selfie": ("selfie.jpg", img_file, "image/jpeg")}
        start_time = time.time()
        
        try:
            response = requests.post(url, data=data_payload, files=file_payload, timeout=10)
            latency = round(time.time() - start_time, 2)
            
            if response.status_code == 200:
                print(f"✅ Request {i} [{action}] - Success ({latency}s)")
                success_count += 1
            else:
                print(f"❌ Request {i} [{action}] - Failed ({latency}s): {response.text}")
                fail_count += 1
        except Exception as e:
            print(f"⚠️ Request {i} - Error: {e}")
            fail_count += 1
            
    time.sleep(0.5) # Slight delay to mimic real-world spacing

print(f"\n🏁 Test Complete! Success: {success_count}, Fails: {fail_count}")