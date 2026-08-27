import requests
import time
import os

url = "http://127.0.0.1:8000/api/v1/attendance/verify"

# CRITICAL FIX: You must upload a REAL image (the simulated "live" selfie).
# The server will automatically find the matching .json using the worker_id="1".
test_image_path = "./worker_photos/1.jpg" 

if not os.path.exists(test_image_path):
    print(f"❌ Error: Cannot find test image at {test_image_path}")
    print("Please provide a real .jpg file to simulate the live selfie.")
    exit()

print("🚀 Starting 50-request stress test...")
success_count, fail_count = 0, 0
total_time = 0.0

for i in range(1, 51):
    # Alternate Clock In/Out so SQLite doesn't block us for being "already clocked in"
    action = "Clock In" if i % 2 != 0 else "Clock Out"
    
    data_payload = {
        "worker_id": "1",
        "action": action,
        "latitude": "30.050010", # Valid location for the site
        "longitude": "31.230010"
    }
    
    with open(test_image_path, "rb") as img_file:
        file_payload = {"selfie": ("live_selfie.jpg", img_file, "image/jpeg")}
        start_time = time.time()
        
        try:
            # Send the request to your FastAPI server
            response = requests.post(url, data=data_payload, files=file_payload, timeout=20)
            
            # Calculate how long the server took to respond
            latency = round(time.time() - start_time, 2)
            total_time += latency
            
            if response.status_code == 200:
                print(f"✅ Request {i:02d} [{action}] - Success ({latency}s)")
                success_count += 1
            else:
                print(f"❌ Request {i:02d} [{action}] - Failed ({latency}s): {response.text}")
                fail_count += 1
                
        except Exception as e:
            print(f"⚠️ Request {i:02d} - Error: {e}")
            fail_count += 1
            
    time.sleep(0.5) # Slight delay to mimic real-world spacing

# Calculate the new average speed
avg_latency = round(total_time / success_count, 2) if success_count > 0 else 0

print("\n" + "="*40)
print(f"🏁 Test Complete! Success: {success_count} | Fails: {fail_count}")
print(f"⚡ Average Speed:  {avg_latency} seconds per request")
print("="*40)