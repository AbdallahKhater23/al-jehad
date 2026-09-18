from fastapi import APIRouter, File, Form, UploadFile, HTTPException, FastAPI
from fastapi.concurrency import run_in_threadpool
from deepface import DeepFace 
import os
import sqlite3
from datetime import datetime
import math
import io
from PIL import Image, ImageOps
from scipy.spatial.distance import cosine
import json
import traceback
import numpy as np

from passlib.context import CryptContext

# 1. OPTIMIZED: Reduced bcrypt rounds to 10 for much faster verification speed
pwd_context = CryptContext(schemes=["bcrypt"], bcrypt__rounds=10, deprecated="auto")

# 2. OPTIMIZED: RAM Cache to entirely eliminate SQLite overhead on user lookups
USER_CACHE = {}

def get_bcrypt_safe_password(password: str) -> str:
    password_bytes = password.encode('utf-8')
    if len(password_bytes) > 72:
        password_bytes = password_bytes[:72]
    return password_bytes.decode('utf-8', errors='ignore')

def init_db():
    global USER_CACHE
    conn = sqlite3.connect("times.db")
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS active_sessions (
            worker_id TEXT PRIMARY KEY,
            site_name TEXT,
            clock_in_time DATETIME
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'worker'
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS attendance_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worker_id TEXT,
            site_name TEXT,
            action TEXT,
            timestamp DATETIME,
            hours FLOAT,
            score FLOAT
        )
    """)
    
    cursor.execute("SELECT id FROM users WHERE id = '1000'")
    if not cursor.fetchone():
        hashed_pw = pwd_context.hash(get_bcrypt_safe_password("admin"))
        cursor.execute("INSERT INTO users (id, name, password_hash, role) VALUES (?, ?, ?, ?)",
                       ("1000", "System Admin", hashed_pw, "admin"))
        
    conn.commit()
    
    # Load all users into RAM cache
    cursor.execute("SELECT id, password_hash FROM users")
    for row in cursor.fetchall():
        USER_CACHE[row[0]] = row[1]
        
    conn.close()
    
init_db()

CONSTRUCTION_SITES = {
    "Downtown Tower A": {"lat": 30.050000, "lon": 31.230000, "radius": 65},
    "New Capital Zone B": {"lat": 29.980000, "lon": 31.750000, "radius": 100},
    "Nozha 2 HQ": {"lat": 30.125000, "lon": 31.365000, "radius": 50}, 
    "Eid Abou Meatek": {"lat": 30.12846731, "lon": 31.352024, "radius": 100},
}

app = FastAPI()
router = APIRouter()

DeepFace.build_model("VGG-Face")

def get_distance_meters(lat1, lon1, lat2, lon2):
    R = 6371000 
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlon/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def compare_faces_sync(reference_json_path: str, live_image_data) -> dict:
    try:
        with open(reference_json_path, 'r') as f:
            reference_embedding = json.load(f)

        live_embedding_objs = DeepFace.represent(
            img_path=live_image_data, 
            model_name="VGG-Face",
            enforce_detection=True,
            detector_backend="opencv" 
        )
        
        if len(live_embedding_objs) > 1:
            return {"verified": False, "distance": 99.9, "error": "Multiple faces detected."}
            
        live_embedding = live_embedding_objs[0]["embedding"]
        distance = cosine(reference_embedding, live_embedding)
        
        return {
            "verified": bool(distance <= 0.40), 
            "distance": round(distance, 4),
            "error": None
        }
        
    except ValueError:
        return {"verified": False, "distance": 99.9, "error": "No face detected."}
    except FileNotFoundError:
        return {"verified": False, "distance": 99.9, "error": "Reference embedding not found."}
    except Exception as e:
        return {"verified": False, "distance": 99.9, "error": "Internal processing error."}

from pydantic import BaseModel

class LoginRequest(BaseModel):
    user_id: str
    password: str

class UserAddRequest(BaseModel):
    user_id: str
    name: str
    password: str
    role: str = "worker"

@router.post("/api/v1/auth/login")
async def login(req: LoginRequest):
    password_hash = USER_CACHE.get(req.user_id)
    safe_pw = get_bcrypt_safe_password(req.password)
    
    if not password_hash or not pwd_context.verify(safe_pw, password_hash):
        raise HTTPException(status_code=401, detail="Invalid ID or Password")

    conn = sqlite3.connect("times.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, role FROM users WHERE id = ?", (req.user_id,))
    user = cursor.fetchone()
    conn.close()

    return {"status": "success", "user": {"id": user[0], "name": user[1], "role": user[2]}}

@router.get("/api/v1/admin/users")
async def list_users():
    conn = sqlite3.connect("times.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, role FROM users")
    users = [{"id": r[0], "name": r[1], "role": r[2]} for r in cursor.fetchall()]
    conn.close()
    return users

@router.post("/api/v1/admin/users/add")
async def add_user(req: UserAddRequest):
    conn = sqlite3.connect("times.db")
    cursor = conn.cursor()
    try:
        safe_pw = get_bcrypt_safe_password(req.password)
        hashed_pw = pwd_context.hash(safe_pw)
        cursor.execute("INSERT INTO users (id, name, password_hash, role) VALUES (?, ?, ?, ?)",
                       (req.user_id, req.name, hashed_pw, req.role))
        conn.commit()
        USER_CACHE[req.user_id] = hashed_pw
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="User ID already exists")
    finally:
        conn.close()
    return {"status": "success", "message": f"User {req.user_id} added"}

@router.get("/api/v1/admin/logs")
async def get_logs():
    conn = sqlite3.connect("times.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM attendance_logs ORDER BY timestamp DESC LIMIT 100")
    logs = [{"id": r[0], "worker_id": r[1], "site": r[2], "action": r[3], "timestamp": r[4], "hours": r[5], "score": r[6]} for r in cursor.fetchall()]
    conn.close()
    return logs

@router.post("/api/v1/attendance/verify")
async def verify_worker(
    worker_id: str = Form(...),
    password: str = Form(...),
    action: str = Form(...),
    latitude: str = Form(None),
    longitude: str = Form(None),
    selfie: UploadFile = File(...)
):
    # 3. OPTIMIZED: RAM Cache Authentication Check (Zero connection lag)
    password_hash = USER_CACHE.get(worker_id)
    safe_pw = get_bcrypt_safe_password(password)
    
    if not password_hash or not pwd_context.verify(safe_pw, password_hash):
        raise HTTPException(status_code=401, detail="Invalid ID or Password")

    if latitude is None or longitude is None:
        raise HTTPException(status_code=400, detail="GPS missing.")
        
    try:
        lat_float, lon_float = float(latitude), float(longitude)
    except ValueError:
        raise HTTPException(status_code=400, detail="GPS must be numbers.")

    detected_site = next((site for site, coords in CONSTRUCTION_SITES.items() 
                          if get_distance_meters(coords["lat"], coords["lon"], lat_float, lon_float) <= coords["radius"]), None)
            
    if not detected_site:
        raise HTTPException(status_code=403, detail="Location Rejected.")
    
    # 4. OPTIMIZED: Direct-to-RAM Image Transformation (No temporary disk file writes)
    file_bytes = await selfie.read()
    try:
        image = Image.open(io.BytesIO(file_bytes))
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((640, 640)) # Lowered resolution footprint for faster CV processing
        img_array = np.array(image)[:, :, ::-1] 
    except Exception as e:
        raise HTTPException(status_code=400, detail="Image processing failed.")

    reference_filepath = f"./local_references/{worker_id}.json"
    if not os.path.exists(reference_filepath):
        raise HTTPException(status_code=404, detail="Worker reference not found.")
        
    face_data = await run_in_threadpool(compare_faces_sync, reference_filepath, img_array)

    if face_data.get("error"):
        raise HTTPException(status_code=400, detail=face_data["error"])
        
    similarity_score = face_data["distance"]

    if similarity_score <= 0.40:
        status_val, status_msg = "success", "Auto-Approved"
    elif 0.40 < similarity_score <= 0.60:
        status_val, status_msg = "flagged", "Flagged for HR Manual Review"
    else:
        raise HTTPException(status_code=401, detail=f"Face verification failed. Score: {similarity_score}")
            
    # 5. OPTIMIZED: Short-lived database session exclusively for writing transaction data
    conn = sqlite3.connect("times.db")
    cursor = conn.cursor()
    now = datetime.now()
    hours_worked = 0.0

    try:
        if action == "Clock In":
            cursor.execute("SELECT clock_in_time FROM active_sessions WHERE worker_id = ?", (worker_id,))
            if cursor.fetchone():
                raise HTTPException(status_code=400, detail="Already clocked in!")
                
            cursor.execute("INSERT INTO active_sessions (worker_id, site_name, clock_in_time) VALUES (?, ?, ?)", 
                           (worker_id, detected_site, str(now)))
            status_msg += f" (Clocked In at {detected_site})"
            
        elif action == "Clock Out":
            cursor.execute("SELECT clock_in_time, site_name FROM active_sessions WHERE worker_id = ?", (worker_id,))
            result = cursor.fetchone()
            
            if result:
                try:
                    clock_in_time = datetime.strptime(result[0], "%Y-%m-%d %H:%M:%S.%f")
                except ValueError:
                    clock_in_time = datetime.strptime(result[0], "%Y-%m-%d %H:%M:%S")
                    
                hours_worked = round((now - clock_in_time).total_seconds() / 3600, 4)
                cursor.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker_id,))
                status_msg += f" (Clocked Out of {result[1]}. Total Hours: {hours_worked})"
            else:
                raise HTTPException(status_code=400, detail="Cannot clock out without clocking in first.")

        cursor.execute("""
            INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, score)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (worker_id, detected_site, action, str(now), hours_worked, similarity_score))
        
        conn.commit()
    finally:
        conn.close()

    return {
        "status": status_val,
        "message": status_msg,
        "score": similarity_score,
        "hours": hours_worked,
        "site": detected_site 
    }

app.include_router(router)