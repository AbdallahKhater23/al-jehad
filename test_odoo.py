import xmlrpc.client
from datetime import datetime

# --- 1. YOUR ODOO CREDENTIALS ---
ODOO_URL = "https://xabdallah.odoo.com"  # Just the base website address
ODOO_DB = "xabdallah"                    # Just the name before .odoo.com
ODOO_USER = "abdallahkhater102@gmail.com"     # The email you log in with
ODOO_PASSWORD = "8c59c34b9eec293aedf6c79821d28b0f699e8397"          # Your login password or API key

# --- 2. YOUR MANUAL INPUTS ---
EMPLOYEE_ID = 1  
MANUAL_PROJECT_NAME = "Internal" # The name you want to manually enter

try:
    print("🔌 Attempting to connect to Odoo...")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    
    if not uid:
        print("❌ Authentication Failed: Check your database name, username, or password.")
    else:
        print(f"✅ Authenticated Successfully! (User ID: {uid})")
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
        
        # --- 3. TRANSLATE PROJECT NAME TO ID ---
        print(f"🔍 Searching for project named '{MANUAL_PROJECT_NAME}'...")
        # We tell Odoo to look in 'project.project' where the 'name' exactly matches our variable
        project_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'project.project', 'search',
            [[['name', '=', MANUAL_PROJECT_NAME]]]
        )
        
        if not project_ids:
            print(f"❌ Error: No project found in Odoo with the name '{MANUAL_PROJECT_NAME}'.")
        else:
            project_id = project_ids[0] # Take the first match
            print(f"✅ Found Project! ID is: {project_id}")
            
            # --- 4. CREATE THE TIMESHEET ENTRY ---
            print("📝 Writing 8 hours to the timesheet...")
            timesheet_payload = {
                "name": f"Site check-out via Camera", 
                "employee_id": EMPLOYEE_ID,
                "project_id": project_id, # We use the dynamically found ID here!
                "unit_amount": 9.0, 
                "date": datetime.today().strftime('%Y-%m-%d') 
            }
            
            record_id = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 
                'account.analytic.line', 'create', 
                [timesheet_payload]
            )
            
            print(f"🎉 Success! Timesheet entry created with Record ID: {record_id}")

except Exception as e:
    print(f"⚠️ Odoo Connection Error: {e}")