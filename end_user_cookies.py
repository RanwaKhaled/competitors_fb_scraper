import json
import time
from datetime import datetime
from selenium import webdriver

driver = webdriver.Chrome()
driver.get("https://www.facebook.com")

with open("manual_cookies.json", "r") as f:
    manual_cookies = json.load(f)

for cookie in manual_cookies:
    if "expires" in cookie:
        date_str = cookie.pop("expires")  
        try:
            # 1. Clean the 'Z' format to make it ISO compliant (+00:00)
            clean_date = date_str.replace("Z", "+00:00")
            
            # 2. Parse the full detailed timestamp string directly
            dt = datetime.fromisoformat(clean_date)
            
            # 3. Correctly assign the exact unix timestamp integer
            cookie["expiry"] = int(dt.timestamp())
            
        except ValueError:
            print(f"Warning: Could not parse date '{date_str}' for cookie {cookie.get('name')}. Using fallback.")
            cookie["expiry"] = int(time.time()) + (365 * 24 * 60 * 60)
    
    try:
        driver.add_cookie(cookie)
    except Exception as e:
        print(f"Skipped cookie {cookie.get('name')}: {e}")

# Reload to apply changes and log in
driver.get("https://www.facebook.com")

# Keep the window open long enough to see the successful login dashboard
time.sleep(10)
driver.quit()