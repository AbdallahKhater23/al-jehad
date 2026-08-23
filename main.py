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

def init_db():
    conn = sqlite3.connect("times.db")
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS active_sessions (
            worker_id TEXT PRIMARY KEY,
            clock_in_time DATETIME
        )
    """)
    conn.commit()
    conn.close()

# Run this once when the server starts
init_db()

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
def compare_faces_sync(reference_path: str, selfie_path: str) -> dict:
    """Runs DeepFace synchronously and checks for faces first."""
    try:
        # 1. Count faces first
        faces = DeepFace.extract_faces(img_path=selfie_path, enforce_detection=True)
        
        if len(faces) > 1:
            return {"verified": False, "distance": 99.9, "error": "Multiple faces detected. Please step forward alone."}
            
        # 2. If exactly 1 face, run verification
        result = DeepFace.verify(
            img1_path=reference_path,
            img2_path=selfie_path,
            enforce_detection=True 
        )
        return {
            "verified": result.get("verified", False),
            "distance": result.get("distance", 99.9),
            "error": None
        }
    except ValueError:
        return {"verified": False, "distance": 99.9, "error": "No face detected. Make sure the lighting is good!"}
    except Exception as e:
        print(f"⚠️ DEEPFACE ERROR: {e}")
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

    if not (-90 <= lat_float <= 90) or not (-180 <= lon_float <= 180):
        raise HTTPException(status_code=400, detail="Invalid GPS Coordinates")

    SITE_LAT = 30.050000 
    SITE_LON = 31.230000

    distance_from_site = get_distance_meters(SITE_LAT, SITE_LON, lat_float, lon_float)

    if distance_from_site > 65:
        raise HTTPException(status_code=403, detail=f"Location Rejected. You are {int(distance_from_site)}m away. You must be at the site.")
    elif distance_from_site > 40:
        raise HTTPException(status_code=403, detail=f"Almost there! You are {int(distance_from_site)}m away. Please step inside the site (under 40m) to clock in.")

    # STEP 2: Save, Rotate, and Shrink the UploadFile securely
    unique_filename = f"{uuid.uuid4()}.jpg"
    temp_filepath = f"./temp/{unique_filename}"

    print("🚨 DEBUG: Saving image, fixing rotation, and resizing...") 
    file_bytes = await selfie.read()
    
    try:
        image = Image.open(io.BytesIO(file_bytes))
        # Physically rotate upright based on phone orientation tag
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        # Shrink massive phone resolutions down so the AI can read it
        image.thumbnail((800, 800))
        image.save(temp_filepath, format="JPEG")
    except Exception as e:
        print(f"🚨 DEBUG: Image processing failed: {e}")
        with open(temp_filepath, "wb") as buffer:
            buffer.write(file_bytes)

    # STEP 3: DeepFace Verification (Threaded)
    reference_filepath = f"./local_references/{worker_id}.jpg"
    
    if not os.path.exists(reference_filepath):
        if os.path.exists(temp_filepath):
            os.remove(temp_filepath)
        raise HTTPException(status_code=404, detail=f"Reference photo for worker {worker_id} not found.")
    
    print("🚨 DEBUG: Sending to DeepFace (This might take a few seconds)...") 
    face_data = await run_in_threadpool(compare_faces_sync, reference_filepath, temp_filepath)

    if face_data.get("error"):
        if os.path.exists(temp_filepath):
            os.remove(temp_filepath)
        raise HTTPException(status_code=400, detail=face_data["error"])
    
    print(f"🚨 DEBUG: DeepFace Finished! Result: {face_data}") 

    # Delete the temp file immediately on success
    if os.path.exists(temp_filepath):
        os.remove(temp_filepath)

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
            
        cursor.execute("INSERT INTO active_sessions (worker_id, clock_in_time) VALUES (?, ?)", (worker_id, str(now)))
        conn.commit()
        status_msg += " (Clocked In successfully)"
        
    elif action == "Clock Out":
        cursor.execute("SELECT clock_in_time FROM active_sessions WHERE worker_id = ?", (worker_id,))
        result = cursor.fetchone()
        
        if result:
            clock_in_str = result[0]
            try:
                clock_in_time = datetime.strptime(clock_in_str, "%Y-%m-%d %H:%M:%S.%f")
            except ValueError:
                clock_in_time = datetime.strptime(clock_in_str, "%Y-%m-%d %H:%M:%S")
                
            duration = now - clock_in_time
            hours_worked = round(duration.total_seconds() / 3600, 4)
            
            cursor.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker_id,))
            conn.commit()
            status_msg += f" (Clocked Out. Total Hours: {hours_worked})"
        else:
            conn.close()
            raise HTTPException(status_code=400, detail="You cannot clock out without clocking in first.")

    conn.close()

    return {
        "status": status_val,
        "message": status_msg,
        "score": similarity_score,
        "hours": hours_worked
    }

app.include_router(router)