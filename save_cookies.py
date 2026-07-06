import json
import time
from selenium import webdriver
from selenium.webdriver.common.by import By

driver = webdriver.Chrome()
driver.get("https://www.facebook.com")

# Log in manually or programmatically this ONE time
# Wait for you to finish logging in
input("Log in manually in the browser, then press Enter here...")

# Save cookies to a JSON file
cookies = driver.get_cookies()
with open("fb_cookies.json", "w") as f:
    json.dump(cookies, f)

print(f"Saved {len(cookies)} cookies.")
driver.quit()