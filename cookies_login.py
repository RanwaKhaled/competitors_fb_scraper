import json
import time
from selenium import webdriver

driver = webdriver.Chrome()

# 1. Navigate to Facebook first (cookies are domain-bound)
driver.get("https://www.facebook.com")

# 2. Load and add each saved cookie
with open("fb_cookies.json", "r") as f:
    cookies = json.load(f)

for cookie in cookies:
    # Remove problematic fields some browsers add
    cookie.pop("sameSite", None)
    cookie.pop("storeId", None)
    try:
        driver.add_cookie(cookie)
    except Exception as e:
        print(f"Skipped cookie {cookie.get('name')}: {e}")

# 3. Reload the page — now you're logged in
driver.get("https://www.facebook.com")

time.sleep(3)

# 4. Verify login worked
if "login" not in driver.current_url:
    print("Logged in successfully via cookies!")
else:
    print("Cookies expired — need to log in again.")

# ... do your work ...

driver.quit()