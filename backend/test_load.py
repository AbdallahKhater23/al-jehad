import glob
import os
import time

import requests

url = "https://sixth-subpanel-resample.ngrok-free.dev/"

#: The reference selfies in ``worker_photos/`` are named after each account's immutable
#: biometric id, not after the account id, so this takes whichever one is there instead of
#: assuming ``1.jpg`` (which no longer exists - see ``biometrics``).
_photos = sorted(glob.glob("../worker_photos/*.jpg"))
test_image_path = _photos[0] if _photos else None

if test_image_path is None:
    print("❌ Error: no .jpg in ../worker_photos to use as a live selfie.")
    print("Please provide a real .jpg file to simulate the live selfie.")
    exit()

print("🚀 Starting 50-request stress test...")
success_count, fail_count = 0, 0
total_time = 0.0

for i in range(1, 51):
    action = "Clock In" if i % 2 != 0 else "Clock Out"
    
    data_payload = {
        "worker_id": "1",
        'email_or_phone' : 'test@example.com',
        "password": "testpassword", 
        "action": action,
        "latitude": "30.050010", 
        "longitude": "31.230010"
    }
    
    with open(test_image_path, "rb") as img_file:
        file_payload = {"selfie": ("live_selfie.jpg", img_file, "image/jpeg")}
        start_time = time.time()
        
        try:
            response = requests.post(url, data=data_payload, files=file_payload, timeout=20)
            latency = round(time.time() - start_time, 2)
            
            if response.status_code == 200:
                total_time += latency
                print(f"✅ Request {i:02d} [{action}] - Success ({latency}s)")
                success_count += 1
            else:
                print(f"❌ Request {i:02d} [{action}] - Failed ({latency}s): {response.text}")
                fail_count += 1
                
        except Exception as e:
            print(f"⚠️ Request {i:02d} - Error: {e}")
            fail_count += 1
            
    time.sleep(0.5)

avg_latency = round(total_time / success_count, 2) if success_count > 0 else 0

print("\n" + "="*40)
print(f"🏁 Test Complete! Success: {success_count} | Fails: {fail_count}")
print(f"⚡ Average Speed:  {avg_latency} seconds per request")
print("="*40)