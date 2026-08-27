from fastapi import APIRouter, File, Form, UploadFile, HTTPException, FastAPI
from fastapi.concurrency import run_in_threadpool
from deepface import DeepFace 
import os
import uuid
import sqlite3
from datetime import datetime
import math
import io
from PIL import Image, ImageOps
from scipy.spatial.distance import cosine
import json
import traceback

def init_db():
    conn = sqlite3.connect("times.db")
    cursor = conn.cursor()
    
    # UPGRADE: Added 'site_name' to track where they clocked in
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS active_sessions (
            worker_id TEXT PRIMARY KEY,
            site_name TEXT,
            clock_in_time DATETIME
        )
    """)
    conn.commit()
    conn.close()

# Run this once when the server starts
init_db()

# --- DEFINE YOUR CONSTRUCTION SITES HERE ---
# Odoo will use these exact names for the attendance records.
CONSTRUCTION_SITES = {
    "Downtown Tower A": {"lat": 30.050000, "lon": 31.230000, "radius": 65},
    "New Capital Zone B": {"lat": 29.980000, "lon": 31.750000, "radius": 100},
    "Nozha 2 HQ": {"lat": 30.125000, "lon": 31.365000, "radius": 50}, 
    "Eid Abou Meatek": {"lat": 30.000000, "lon": 31.000000, "radius": 100}, # <-- UPDATE THESE COORDINATES
}

app = FastAPI()
router = APIRouter()

# Solves the "Cold Start" bottleneck by loading the model into the server's RAM
DeepFace.build_model("VGG-Face")

def get_distance_meters(lat1, lon1, lat2, lon2):
    """Calculates the distance in meters between two GPS coordinates."""
    R = 6371000 # Radius of Earth in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlon/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# ---------------------------------------------------------
# HELPER FUNCTION 
# ---------------------------------------------------------
def compare_faces_sync(reference_json_path: str, selfie_path: str) -> dict:
    """Uses pre-computed embeddings and vector math for sub-second verification."""
    try:
        # 1. Load the pre-calculated numbers for the worker
        with open(reference_json_path, 'r') as f:
            reference_embedding = json.load(f)

        # 2. Extract features from the LIVE photo using the MTCNN scanner
        live_embedding_objs = DeepFace.represent(
            img_path=selfie_path, 
            model_name="VGG-Face",
            enforce_detection=True,
            detector_backend="mtcnn" 
        )
        
        # 3. Check for multiple faces
        if len(live_embedding_objs) > 1:
            return {"verified": False, "distance": 99.9, "error": "Multiple faces detected. Please step forward alone."}
            
        live_embedding = live_embedding_objs[0]["embedding"]
        
        # 4. Do the vector math (Takes 0.01 seconds!)
        distance = cosine(reference_embedding, live_embedding)
        
        return {
            "verified": bool(distance <= 0.40), # 0.40 is the threshold for VGG-Face
            "distance": round(distance, 4),
            "error": None
        }
        
    except ValueError:
        return {"verified": False, "distance": 99.9, "error": "No face detected. Make sure the lighting is good!"}
    except FileNotFoundError:
        return {"verified": False, "distance": 99.9, "error": "Reference embedding not found for this worker."}
    except Exception as e:
        print(f"⚠️ DEEPFACE ERROR: {e}")
        traceback.print_exc()
        return {"verified": False, "distance": 99.9, "error": "Internal processing error."}

# ---------------------------------------------------------
# MAIN ROUTE
# ---------------------------------------------------------
@router.post("/api/v1/attendance/verify")
async def verify_worker(
    worker_id: str = Form(...),
    action: str = Form(...),
    latitude: str = Form(None),
    longitude: str = Form(None),
    selfie: UploadFile = File(...)
):

    # STEP 1: Initial Validation (The Bouncer & GPS Check)
    if latitude is None or longitude is None:
        raise HTTPException(status_code=400, detail="GPS coordinates are missing.")
        
    try:
        lat_float = float(latitude)
        lon_float = float(longitude)
    except ValueError:
        raise HTTPException(status_code=400, detail="GPS coordinates must be numbers.")

    # --- NEW GEO-FENCING LOGIC ---
    detected_site = None
    
    for site_name, coords in CONSTRUCTION_SITES.items():
        dist = get_distance_meters(coords["lat"], coords["lon"], lat_float, lon_float)
        
        # If they are within the allowed radius of this specific site
        if dist <= coords["radius"]:
            detected_site = site_name
            break
            
    # If the loop finishes and detected_site is still None, they are nowhere near a site
    if not detected_site:
        raise HTTPException(status_code=403, detail="Location Rejected. You are not within the radius of any authorized construction site.")
    
    # STEP 2: Save, Rotate, and Shrink the UploadFile securely
    os.makedirs("./temp", exist_ok=True)
    unique_filename = f"{uuid.uuid4()}.jpg"
    temp_filepath = f"./temp/{unique_filename}"

    print(f"🚨 DEBUG: Saving image, fixing rotation, and resizing... Location: {detected_site}") 
    file_bytes = await selfie.read()
    
    try:
        image = Image.open(io.BytesIO(file_bytes))
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        image.thumbnail((800, 800))
        image.save(temp_filepath, format="JPEG")
    except Exception as e:
        print(f"🚨 DEBUG: Image processing failed: {e}")
        with open(temp_filepath, "wb") as buffer:
            buffer.write(file_bytes)

    # Wrap the rest of the logic in try/finally to guarantee file cleanup
    try:
        # STEP 3: DeepFace Verification (Threaded)
        reference_filepath = f"./local_references/{worker_id}.json"
        
        if not os.path.exists(reference_filepath):
            raise HTTPException(status_code=404, detail=f"Reference data for worker {worker_id} not found. HR needs to enroll them.")
        
        print("🚨 DEBUG: Sending to DeepFace (Vector comparison)...") 
        face_data = await run_in_threadpool(compare_faces_sync, reference_filepath, temp_filepath)

        if face_data.get("error"):
            raise HTTPException(status_code=400, detail=face_data["error"])
        
        print(f"🚨 DEBUG: DeepFace Finished! Result: {face_data}") 

        similarity_score = face_data["distance"]

        # --- TRAFFIC LIGHT LOGIC ---
        if similarity_score <= 0.40:
            status_val = "success"
            status_msg = "Auto-Approved"
        elif 0.40 < similarity_score <= 0.60:
            status_val = "flagged"
            status_msg = "Flagged for HR Manual Review"
        else:
            raise HTTPException(status_code=401, detail=f"Face verification failed. Score: {similarity_score}")
            
        # --- SQLITE TIME TRACKING LOGIC ---
        conn = sqlite3.connect("times.db")
        cursor = conn.cursor()
        now = datetime.now()
        
        hours_worked = 0.0

        if action == "Clock In":
            cursor.execute("SELECT clock_in_time FROM active_sessions WHERE worker_id = ?", (worker_id,))
            existing_session = cursor.fetchone()
            
            if existing_session:
                conn.close()
                raise HTTPException(status_code=400, detail="You are already clocked in! Please Clock Out first.")
                
            # UPGRADE: Save the detected_site into the database
            cursor.execute("INSERT INTO active_sessions (worker_id, site_name, clock_in_time) VALUES (?, ?, ?)", 
                           (worker_id, detected_site, str(now)))
            conn.commit()
            status_msg += f" (Clocked In at {detected_site})"
            
        elif action == "Clock Out":
            # UPGRADE: Fetch the site_name they clocked into
            cursor.execute("SELECT clock_in_time, site_name FROM active_sessions WHERE worker_id = ?", (worker_id,))
            result = cursor.fetchone()
            
            if result:
                clock_in_str = result[0]
                clocked_in_site = result[1] # We now know where they worked
                
                try:
                    clock_in_time = datetime.strptime(clock_in_str, "%Y-%m-%d %H:%M:%S.%f")
                except ValueError:
                    clock_in_time = datetime.strptime(clock_in_str, "%Y-%m-%d %H:%M:%S")
                    
                duration = now - clock_in_time
                hours_worked = round(duration.total_seconds() / 3600, 4)
                
                cursor.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker_id,))
                conn.commit()
                status_msg += f" (Clocked Out of {clocked_in_site}. Total Hours: {hours_worked})"
            else:
                conn.close()
                raise HTTPException(status_code=400, detail="You cannot clock out without clocking in first.")

        conn.close()

        # UPGRADE: Return the site name to the frontend
        return {
            "status": status_val,
            "message": status_msg,
            "score": similarity_score,
            "hours": hours_worked,
            "site": detected_site 
        }

    finally:
        # STEP 4: GUARANTEED CLEANUP
        if os.path.exists(temp_filepath):
            os.remove(temp_filepath)
            print(f"🚨 DEBUG: Temporary file {unique_filename} securely deleted.")

app.include_router(router)