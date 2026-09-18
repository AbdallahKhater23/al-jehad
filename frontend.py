import streamlit as st
import requests
import time
import pandas as pd
from streamlit_geolocation import streamlit_geolocation
import io
from PIL import Image, ImageOps

# --- CONFIGURATION ---
API_BASE_URL = "http://127.0.0.1:8000"
st.set_page_config(page_title="Site Attendance Pro", page_icon="🏗️", layout="wide")

# --- CUSTOM CSS ---
st.markdown("""
    <style>
    .main {
        background-color: #f5f7f9;
    }
    .stButton>button {
        border-radius: 8px;
        font-weight: 600;
    }
    .status-card {
        padding: 20px;
        border-radius: 10px;
        background-color: white;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05);
        margin-bottom: 20px;
    }
    </style>
    """, unsafe_allow_html=True)

# --- SESSION STATE ---
if "user" not in st.session_state:
    st.session_state.user = None
if "page" not in st.session_state:
    st.session_state.page = "Login"

# --- HELPER FUNCTIONS ---
def login(user_id, password):
    try:
        response = requests.post(f"{API_BASE_URL}/api/v1/auth/login", json={"user_id": user_id, "password": password})
        if response.status_code == 200:
            st.session_state.user = response.json()["user"]
            st.success(f"Welcome, {st.session_state.user['name']}!")
            time.sleep(1)
            st.rerun()
        else:
            st.error("Invalid ID or Password")
    except Exception as e:
        st.error(f"Connection error: {e}")

def logout():
    st.session_state.user = None
    st.session_state.page = "Login"
    st.rerun()

# --- NAVIGATION ---
if st.session_state.user:
    st.sidebar.title(f"👤 {st.session_state.user['name']}")
    st.sidebar.info(f"Role: {st.session_state.user['role'].capitalize()}")
    
    nav_options = ["Attendance"]
    if st.session_state.user["role"] == "admin":
        nav_options.append("Admin Dashboard")
    
    st.session_state.page = st.sidebar.radio("Navigation", nav_options)
    
    if st.sidebar.button("Logout", use_container_width=True):
        logout()
else:
    st.session_state.page = "Login"

# --- PAGES ---

# 1. LOGIN PAGE
if st.session_state.page == "Login":
    st.title("🏗️ Site Attendance Pro")
    st.markdown("### Secure Login")
    
    with st.container():
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            st.markdown('<div class="status-card">', unsafe_allow_html=True)
            u_id = st.text_input("User ID")
            u_pw = st.text_input("Password", type="password")
            if st.button("Login", use_container_width=True, type="primary"):
                login(u_id, u_pw)
            st.markdown('</div>', unsafe_allow_html=True)

# 2. ATTENDANCE PAGE
elif st.session_state.page == "Attendance":
    st.title("🕒 Clock In/Out")
    
    col1, col2 = st.columns([1, 1])
    
    with col1:
        st.markdown("### 1. Verification")
        action = st.radio("Action", ["Clock In", "Clock Out"], horizontal=True)
        worker_id = st.text_input("Confirm ID", value=st.session_state.user["id"], disabled=True)
        password = st.text_input("Verify Password", type="password")
        
        st.markdown("### 2. Location")
        location = streamlit_geolocation()
        lat, lon = None, None
        if location and location.get('latitude'):
            lat, lon = location['latitude'], location['longitude']
            st.success(f"📍 Location Verified: {lat:.4f}, {lon:.4f}")
        else:
            st.warning("⚠️ Please allow GPS access")

    with col2:
        st.markdown("### 3. Face Scanner")
        picture = st.camera_input("Take a selfie")
        
        if picture and st.button(f"Confirm {action}", use_container_width=True, type="primary"):
            if not lat or not lon:
                st.error("❌ GPS coordinates required!")
            elif not password:
                st.error("❌ Please enter your password for verification.")
            else:
                with st.spinner("Processing..."):
                    try:
                        payload = {
                            "worker_id": st.session_state.user["id"],
                            "password": password,
                            "action": action,
                            "latitude": lat,
                            "longitude": lon
                        }
                        files = {"selfie": ("selfie.jpg", picture.getvalue(), "image/jpeg")}
                        
                        resp = requests.post(f"{API_BASE_URL}/api/v1/attendance/verify", data=payload, files=files, timeout=30)
                        
                        if resp.status_code == 200:
                            res = resp.json()
                            st.balloons()
                            st.success(f"✅ {res['message']}")
                            if res.get('hours'):
                                st.info(f"Total Hours: {res['hours']}")
                        else:
                            st.error(f"Failed: {resp.json().get('detail', 'Unknown error')}")
                    except Exception as e:
                        st.error(f"Error: {e}")

# 3. ADMIN DASHBOARD
elif st.session_state.page == "Admin Dashboard":
    st.title("🛠️ Admin Dashboard")
    
    tab1, tab2, tab3 = st.tabs(["User Management", "Enrollment Tool", "Attendance Logs"])
    
    # TAB 1: USER MANAGEMENT
    with tab1:
        st.subheader("Manage Workers")
        
        # Add User Form
        with st.expander("➕ Add New User"):
            with st.form("add_user_form"):
                new_id = st.text_input("New ID")
                new_name = st.text_input("Full Name")
                new_pw = st.text_input("Initial Password", type="password")
                new_role = st.selectbox("Role", ["worker", "admin"])
                if st.form_submit_button("Create User"):
                    try:
                        r = requests.post(f"{API_BASE_URL}/api/v1/admin/users/add", 
                                          json={"user_id": new_id, "name": new_name, "password": new_pw, "role": new_role})
                        if r.status_code == 200:
                            st.success(f"User {new_id} created!")
                        else:
                            st.error(r.json().get("detail", "Error"))
                    except Exception as e:
                        st.error(str(e))
        
        # User List
        if st.button("Refresh User List"):
            try:
                users = requests.get(f"{API_BASE_URL}/api/v1/admin/users").json()
                st.table(pd.DataFrame(users))
            except:
                st.error("Could not fetch users")

    # TAB 2: ENROLLMENT TOOL (Face Registration)
    with tab2:
        st.subheader("Face Enrollment")
        st.markdown("Before a worker can clock in, their face must be scanned and registered.")
        
        enroll_id = st.text_input("Worker ID to Enroll")
        enroll_photo = st.camera_input("Enrollment Photo", key="enroll_cam")
        
        if enroll_photo and st.button("Process Enrollment"):
            if not enroll_id:
                st.error("Please enter a Worker ID")
            else:
                with st.spinner("Analyzing face..."):
                    # We can use a dedicated enrollment endpoint or just process it locally and save to the server's path
                    # For simplicity, let's assume the server has an enrollment endpoint or we use the local_references logic.
                    # I'll create a simple enrollment route in main.py if needed, or just handle it here if backend allows.
                    # Let's add an enrollment endpoint to main.py.
                    try:
                        files = {"photo": ("photo.jpg", enroll_photo.getvalue(), "image/jpeg")}
                        r = requests.post(f"{API_BASE_URL}/api/v1/admin/enroll", data={"worker_id": enroll_id}, files=files)
                        if r.status_code == 200:
                            st.success(f"✅ Facial data for {enroll_id} has been registered!")
                        else:
                            st.error(r.json().get("detail", "Enrollment failed"))
                    except Exception as e:
                        st.error(str(e))

    # TAB 3: ATTENDANCE LOGS
    with tab3:
        st.subheader("History & Logs")
        if st.button("Refresh Logs"):
            try:
                logs = requests.get(f"{API_BASE_URL}/api/v1/admin/logs").json()
                if logs:
                    df = pd.DataFrame(logs)
                    st.dataframe(df, use_container_width=True)
                    
                    csv = df.to_csv(index=False).encode('utf-8')
                    st.download_button("Export to CSV", csv, "attendance_logs.csv", "text/csv")
                else:
                    st.info("No logs found.")
            except:
                st.error("Could not fetch logs")
