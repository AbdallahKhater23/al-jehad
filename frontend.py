import streamlit as st
import requests
import time

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
    mock_lat = st.number_input("Latitude", value=30.050000, format="%.6f") 
    mock_lon = st.number_input("Longitude", value=31.230000, format="%.6f")

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
            
            with st.spinner("Processing biometric data..."):
                data_payload = {
                    "worker_id": worker_id,
                    "action": action,
                    "latitude": mock_lat,
                    "longitude": mock_lon
                }
                
                file_payload = {"selfie": ("selfie.jpg", picture, "image/jpeg")}
                
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
                    st.error(f"Network error: Could not reach backend. {e}")

# 3. Call the fragment
camera_scanner()