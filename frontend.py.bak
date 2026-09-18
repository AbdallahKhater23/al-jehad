import streamlit as st
import requests
import time
from streamlit_geolocation import streamlit_geolocation

# Configure the page layout for a cleaner, modern look
st.set_page_config(page_title="Site Attendance", page_icon="🏗️", layout="centered")

st.title("🏗️ Site Attendance Scanner")
st.markdown("Please confirm your location and capture your face to log your hours.")
st.divider()

# 1. Inputs (Organized into columns for a compact, professional UI)
col1, col2 = st.columns(2)
with col1:
    action = st.radio("Select Action:", ["Clock In", "Clock Out"], horizontal=True)
    worker_id = st.text_input("Worker ID", value="1")
with col2:
    st.markdown("**GPS Location**")
    # This renders a button asking the phone browser for GPS permissions
    location = streamlit_geolocation()
    
    # Extract coordinates if the user clicked "Allow"
    if location and location.get('latitude') and location.get('longitude'):
        lat = location['latitude']
        lon = location['longitude']
        st.success("📍 Location Verified")
    else:
        lat = None
        lon = None
        st.warning("⚠️ Please click the button to allow location access")

st.divider()

# 2. The Camera Fragment (Only this section refreshes)
@st.fragment
def camera_scanner():
    if "camera_key" not in st.session_state:
        st.session_state.camera_key = 1
        
    picture = st.camera_input("Take a clear selfie", key=f"cam_{st.session_state.camera_key}")

    if picture is not None:
        st.info("📷 Photo captured. Review and confirm below.")
        
        # Primary, full-width button makes the UI feel tactile and alive
        if st.button(f"Confirm {action}", use_container_width=True, type="primary"):
            
            # --- GPS BLOCKER ---
            # Stop the user from clocking in if they denied location access
            if lat is None or lon is None:
                st.error("❌ We need your GPS coordinates first! Click the location button above.")
                return
                
            with st.spinner("Processing biometric data..."):
                data_payload = {
                    "worker_id": worker_id,
                    "action": action,
                    "latitude": lat,
                    "longitude": lon
                }
                
                # --- THE FIX: Extracting the raw bytes before sending ---
                file_payload = {"selfie": ("selfie.jpg", picture.getvalue(), "image/jpeg")}
                
                try:
                    response = requests.post(
                        "http://127.0.0.1:8000/api/v1/attendance/verify",
                        data=data_payload,
                        files=file_payload,
                        timeout=20
                    )
                    
                    if response.status_code == 200:
                        raw_data = response.json()
                        backend_status = raw_data.get("status")
                        backend_msg = raw_data.get("message")
                        
                        if backend_status == "success":
                            # Slide-in toast notification for a dynamic feel
                            st.toast(f"✅ {backend_msg}") 
                            st.success(f"Match Approved (Score: {raw_data.get('score')})")
                            time.sleep(1.5)
                            
                            # Reset camera and rerun ONLY this fragment
                            st.session_state.camera_key += 1 
                            st.rerun(scope="fragment") 
                            
                        elif backend_status == "flagged":
                            st.warning(f"⚠️ {backend_msg} (Score: {raw_data.get('score')})")
                    else:
                        st.error(f"Failed! Backend says: {response.text}")
                        
                except Exception as e:
                    st.error(f"Network error: Could not reach backend. Is the server running? {e}")

# 3. Call the fragment
camera_scanner()