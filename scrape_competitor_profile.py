"""
Facebook Competitor Page Finder (CLI)

Flow:
    1. Load competitors from competitors.json
    2. Let the user pick one via CLI (or run for all of them)
    3. Search Facebook for the competitor name -> take the first Page result
    4. Save the resolved Page URL(s) to competitor_page_urls.csv

This is the "just find the profile URL" version — it stops right after
resolving each competitor's Facebook Page URL. No post scraping.

Requirements:
    pip install selenium beautifulsoup4 toml

Notes:
    - Facebook often requires being logged in to reliably see Page search
      results. If credentials are present in .streamlit/secrets.toml
      (FB_EMAIL / FB_PASSWORD), the script will attempt to log in.
      Otherwise it proceeds logged-out, which may limit result quality.
    - Facebook's DOM is obfuscated and changes often; if the search stops
      finding pages, that selector logic is the first place to check.
"""

import csv
import json
import logging
import os
import random
import re
import sys
import time
from typing import Any, Dict, List, Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

COMPETITORS_FILE = "competitors.json"
INDUSTRY = "coffee shop alexandria"
OUTPUT_CSV = "competitor_page_urls.csv"


# ---------------------------------------------------------------------- #
#  Browser setup                                                         #
# ---------------------------------------------------------------------- #
def setup_browser(headless: bool = False):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--lang=en-US")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    )
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option("prefs", {
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_setting_values.geolocation": 2,
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    })

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(30)
    driver.implicitly_wait(2)
    return driver


def _load_fb_credentials(path: str = ".streamlit/secrets.toml") -> Optional[Dict[str, str]]:
    if not os.path.exists(path):
        return None
    try:
        import toml
        secrets = toml.load(path)
        email = secrets.get("FB_EMAIL") or secrets.get("fb_email")
        password = secrets.get("FB_PASSWORD") or secrets.get("fb_password")
        if email and password:
            return {"email": email, "password": password}
    except Exception as e:
        logging.warning(f"Could not read {path}: {e}")
    return None


def _human_type(element, text: str):
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(0.05, 0.2))


def try_login(driver) -> bool:
    creds = _load_fb_credentials()
    if not creds:
        logging.info("No FB credentials found — continuing logged-out.")
        return False
    try:
        driver.get("https://www.facebook.com/login")
        email_input = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.NAME, "email"))
        )
        _human_type(email_input, creds["email"])
        password_input = driver.find_element(By.NAME, "pass")
        _human_type(password_input, creds["password"])
        password_input.send_keys(Keys.RETURN)
        time.sleep(15)
        logging.info("Submitted Facebook login.")
        return True
    except Exception as e:
        logging.warning(f"Facebook login failed: {e}")
        return False


# ---------------------------------------------------------------------- #
#  Find the competitor's Facebook Page                                   #
# ---------------------------------------------------------------------- #
def _looks_like_invalid_page(driver) -> bool:
    """Heuristic check for Facebook's 'this content isn't available' /
    removed-page states, so we don't save a dead link as if it were the
    competitor's real page."""
    text = driver.page_source.lower()
    error_markers = [
        "content isn't available",
        "this content isn't available right now",
        "page isn't available",
        "sorry, this content isn't available",
        "the link you followed may be broken",
        "this page isn't available",
    ]
    return any(m in text for m in error_markers)


def _resolve_and_verify_page_url(driver, candidate_url: str) -> Optional[str]:
    """
    Opens a candidate page URL, confirms it isn't a broken/removed-content
    page, and returns the URL Selenium actually ends up on afterward —
    which also resolves it past any redirect Facebook applies (e.g. a
    search result URL that redirects to the page's canonical vanity URL).
    Returns None if the page doesn't look valid.
    """
    try:
        driver.get(candidate_url)
        time.sleep(4)
    except Exception as e:
        logging.warning(f"Could not open candidate page {candidate_url}: {e}")
        return None

    if _looks_like_invalid_page(driver):
        logging.warning(f"Candidate page looks invalid/unavailable: {candidate_url}")
        return None

    final_url = (driver.current_url or candidate_url).split("?")[0].rstrip("/")
    return final_url


def _normalize_href(href: str) -> str:
    """Facebook sometimes serves the stripped-down web.facebook.com
    interface instead of www.facebook.com (commonly when the session is
    logged-out / looks automated). Same links, different domain — treat
    them the same by normalizing to www."""
    if href.startswith("https://web.facebook.com"):
        return href.replace("https://web.facebook.com", "https://www.facebook.com", 1)
    return href


def find_facebook_page_url(driver, name: str, industry: Optional[str] = None) -> Optional[str]:
    query = f"{name} {industry}".strip() if industry else name
    search_url = f"https://www.facebook.com/search/pages/?q={query.replace(' ', '%20')}"
    logging.info(f"Searching Facebook pages: {search_url}")
    driver.get(search_url)
    time.sleep(5)

    for sel in ["[aria-label='Allow all cookies']", "[aria-label*='Allow']", "[aria-label*='Accept']"]:
        try:
            btn = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(1.5)
            break
        except (TimeoutException, NoSuchElementException):
            continue

    excluded_fragments = [
        "/search/", "/ads/", "/help/", "/policies/", "/login", "/groups/",
        "/profile.php", "/me/", "/me?", "/bookmarks", "/settings",
        "/notifications", "/messages", "/friends/", "/marketplace",
        "/gaming", "/watch/", "/games/", "/pages/?", "/pages/create",
    ]

    # Don't scope to div[role="main"] and don't assume the www domain —
    # if this session got redirected to the stripped-down web.facebook.com
    # interface, that wrapper may not exist and links will point at
    # web.facebook.com instead. Search the whole page for links to either
    # domain and let the exclusion list + normalization sort it out.
    try:
        candidates = WebDriverWait(driver, 10).until(
            EC.presence_of_all_elements_located(
                (
                    By.CSS_SELECTOR,
                    'a[href^="https://www.facebook.com/"], a[href^="https://web.facebook.com/"]',
                )
            )
        )
    except TimeoutException:
        candidates = []

    # Pull hrefs out up front — once we start navigating to verify each
    # one, the original search-results elements go stale.
    candidate_urls = []
    for el in candidates:
        href = el.get_attribute("href") or ""
        if not href:
            continue
        href = _normalize_href(href)
        if any(frag in href for frag in excluded_fragments):
            continue
        clean_url = href.split("?")[0]
        if clean_url.rstrip("/") == "https://www.facebook.com":
            continue
        if clean_url not in candidate_urls:
            candidate_urls.append(clean_url)

    for clean_url in candidate_urls:
        resolved_url = _resolve_and_verify_page_url(driver, clean_url)
        if resolved_url:
            logging.info(f"Selected Facebook page: {resolved_url}")
            return resolved_url

    logging.warning(f"No valid Facebook page found for '{query}'")
    _dump_debug(driver, name)
    return None


def _dump_debug(driver, label: str):
    """Save a screenshot + page source so we can see what Selenium was
    actually looking at when a search came back empty (checkpoint page,
    changed DOM, empty results, etc.)."""
    try:
        os.makedirs("debug", exist_ok=True)
        safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", label).strip("_") or "unknown"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        png_path = f"debug/no_result_{safe_label}_{stamp}.png"
        html_path = f"debug/no_result_{safe_label}_{stamp}.html"
        driver.save_screenshot(png_path)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        logging.info(f"Dumped debug info: {png_path}, {html_path}")
        logging.info(f"Current URL at time of failure: {driver.current_url}")
    except Exception as e:
        logging.warning(f"Could not write debug dump: {e}")


# ---------------------------------------------------------------------- #
#  CSV output                                                            #
# ---------------------------------------------------------------------- #
def save_urls_to_csv(rows: List[Dict[str, str]], path: str = OUTPUT_CSV) -> str:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "facebook_page_url"])
        writer.writeheader()
        writer.writerows(rows)
    logging.info(f"Saved {path}")
    return path


# ---------------------------------------------------------------------- #
#  CLI                                                                   #
# ---------------------------------------------------------------------- #
def load_competitors(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "competitors" in data:
        data = data["competitors"]
    return data


def prompt_choice(competitors: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    print("\nSelect a competitor (or 0 to look up ALL of them):")
    for i, c in enumerate(competitors, start=1):
        print(f"  {i}. {c.get('name', 'Unknown')}")
    while True:
        choice = input(f"Enter a number (0-{len(competitors)}): ").strip()
        if choice.isdigit() and 0 <= int(choice) <= len(competitors):
            idx = int(choice)
            return None if idx == 0 else competitors[idx - 1]
        print("Invalid choice, try again.")


def main():
    if not os.path.exists(COMPETITORS_FILE):
        print(f"Could not find {COMPETITORS_FILE} in the current directory.")
        sys.exit(1)

    competitors = load_competitors(COMPETITORS_FILE)
    if not competitors:
        print("No competitors found in the file.")
        sys.exit(1)

    selected = prompt_choice(competitors)
    targets = competitors if selected is None else [selected]

    driver = setup_browser(headless=False)
    results: List[Dict[str, str]] = []
    try:
        try_login(driver)

        for c in targets:
            name = c.get("name", "")
            print(f"\nLooking up '{name}'...")
            page_url = find_facebook_page_url(driver, name, industry=INDUSTRY)
            if page_url:
                print(f"  -> {page_url}")
            else:
                print(f"  -> Not found")
            results.append({"name": name, "facebook_page_url": page_url or ""})

        if results:
            out_path = save_urls_to_csv(results)
            print(f"\nDone. Saved:\n  {out_path}")
        else:
            print("Nothing to save.")

    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()