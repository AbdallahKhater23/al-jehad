import streamlit as st
import requests
import time

st.title("Site Attendance Scanner")

# Initialize session state variables
if "camera_key" not in st.session_state:
    st.session_state.camera_key = 1
if "success_msg" not in st.session_state:
    st.session_state.success_msg = None

# If we have a success message from the last run, show it!
if st.session_state.success_msg:
    st.success(st.session_state.success_msg)
    # Clear it so it doesn't stay forever
    st.session_state.success_msg = None

# 1. Action Selector (In vs Out)
action = st.radio("Select Action:", ["Clock In", "Clock Out"])

# 2. Worker ID & Mock GPS
worker_id = st.text_input("Worker ID", value="1")
mock_lat = st.number_input("Mock Latitude", value=30.050000, format="%.6f", step=0.0001) 
mock_lon = st.number_input("Mock Longitude", value=31.230000, format="%.6f", step=0.0001)

# 3. The Camera Widget

picture = st.camera_input("Take a clear photo", key=f"cam_{st.session_state.camera_key}")

# 4. The Review & Submit Phase
if picture is not None:
    st.info("Photo captured. Please review before submitting.")
    
    # This button creates the "Pause" you wanted!
    if st.button(f"Confirm {action}"):
        st.write("Sending to server...")
        
        # Package the data (Notice we are now sending the 'action' too!)
        data_payload = {
            "worker_id": worker_id,
            "action": action,
            "latitude": mock_lat,
            "longitude": mock_lon
        }
        
        file_payload = {
            "selfie": ("selfie.jpg", picture, "image/jpeg")
        }
        
        try:
            response = requests.post(
                "https://check-in-check-out-s43i.onrender.com",
                data=data_payload,
                files=file_payload,
                timeout=60
            )
            
            # 1. Did the backend approve the request?
            if response.status_code == 200:
                raw_data = response.json()
                backend_status = raw_data.get("status")
                backend_msg = raw_data.get("message")
                backend_score = raw_data.get("score")
                
                # --- THIS MUST BE INDENTED HERE ---
                if backend_status == "success":
                    st.success(f"✅ {backend_msg} (Score: {backend_score})")
                    time.sleep(1.5)
                    st.session_state.camera_key += 1 
                    st.rerun() 
                    
                elif backend_status == "flagged":
                    st.warning(f"⚠️ {backend_msg} (Score: {backend_score})")
            
            # 2. Did the backend reject the request (like a GPS failure)?
            else:
                st.error(f"Failed! Backend says: {response.text}")
                
        except Exception as e:
            st.error(f"Network error: Could not reach backend. {e}")