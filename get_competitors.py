"""
Competitor Finder (standalone)

Searches Google Maps for businesses in a given industry/region and
returns basic info (name, rating, review count, address, Maps URL)
for up to `max_competitors` results.

Requirements:
    pip install selenium
    A matching Chrome + chromedriver installed and on PATH.

Usage:
    python get_competitors.py
    (edit the INDUSTRY / REGION / MAX_COMPETITORS constants below,
     or import find_competitors() into your own code)
"""

import json
import logging
import re
import time
from typing import Any, Dict, List
import os
import subprocess

from selenium.webdriver.chrome.service import Service
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    StaleElementReferenceException,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def _setup_browser():
    """Create a headless Chrome driver tuned for scraping."""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--window-size=1280,720")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--lang=en-US")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    )
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_experimental_option("useAutomationExtension", False)
    prefs = {
        "profile.default_content_setting_values": {"notifications": 2, "geolocation": 2},
        "profile.managed_default_content_settings": {"images": 2},
    }
    options.add_experimental_option("prefs", prefs)

    # try:
    #     driver = webdriver.Chrome(options=options)
    #     driver.set_page_load_timeout(30)
    #     driver.implicitly_wait(2)
    #     return driver
    # except Exception as e:
    #     logging.error(f"Chrome setup failed: {e}")
    #     raise
    chromium_path = "/usr/bin/chromium"
    driver_path = "/usr/bin/chromedriver"
    if os.path.exists(chromium_path):
        options.binary_location = chromium_path

    try:
        logging.info(subprocess.run(["chromium", "--version"], capture_output=True, text=True).stdout)
        logging.info(subprocess.run(["chromedriver", "--version"], capture_output=True, text=True).stdout)

        if os.path.exists(driver_path):
            service = Service(executable_path=driver_path)
            driver = webdriver.Chrome(service=service, options=options)
        else:
            driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(30)
        driver.implicitly_wait(2)
        return driver
    except Exception as e:
        logging.error(f"Chrome setup failed: {e}")
        raise

def find_competitors(industry: str, region: str, max_competitors: int = 5) -> List[Dict[str, Any]]:
    """
    Search Google Maps for `industry` businesses in `region` and return
    up to `max_competitors` results.

    Returns a list of dicts:
        {
            "name": str,
            "rating": float,
            "review_count": int,
            "address": str,
            "google_maps_url": str,
        }
    """
    driver = None
    try:
        driver = _setup_browser()

        query = f"{industry} in {region}"
        search_url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}/?hl=en"
        logging.info(f"Searching Google Maps: {search_url}")

        driver.get(search_url)
        time.sleep(5)

        # Dismiss cookie consent if present
        for selector in ["[aria-label='Accept all']", "[aria-label*='Accept']"]:
            try:
                btn = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                )
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(2)
                break
            except (TimeoutException, NoSuchElementException):
                continue

        # Wait for results to load (try a few selector variants, Google changes these often)
        results_selectors = [
            'div[role="feed"] a[href*="/maps/place/"]',
            'div.m6QErb.XiKgde a[href*="/maps/place/"]',
            'a[href*="/maps/place/"]',
        ]

        result_links = []
        for selector in results_selectors:
            try:
                result_links = WebDriverWait(driver, 15).until(
                    EC.presence_of_all_elements_located((By.CSS_SELECTOR, selector))
                )
                if result_links:
                    break
            except TimeoutException:
                continue

        if not result_links:
            logging.warning("No Google Maps results found")
            return []

        competitors = []
        seen_names = set()

        # grab extra links in case some fail to parse
        for link_el in result_links[: max_competitors * 2]:
            if len(competitors) >= max_competitors:
                break
            try:
                href = link_el.get_attribute("href") or ""
                if "/maps/place/" not in href:
                    continue

                name = link_el.get_attribute("aria-label") or ""
                if not name:
                    name_el = link_el.find_elements(
                        By.CSS_SELECTOR,
                        'div.fontHeadlineSmall, span.fontHeadlineSmall, div[class*="fontHeadline"]',
                    )
                    name = name_el[0].text.strip() if name_el else ""

                if not name or name in seen_names:
                    continue
                seen_names.add(name)

                rating = 0.0
                review_count = 0
                try:
                    rating_el = link_el.find_elements(By.CSS_SELECTOR, 'span[role="img"]')
                    if rating_el:
                        aria = rating_el[0].get_attribute("aria-label") or ""
                        rating_match = re.search(r"(\d+\.?\d*)", aria)
                        if rating_match:
                            rating = float(rating_match.group(1))
                        review_match = re.search(r"(\d[\d,]*)\s*review", aria, re.IGNORECASE)
                        if review_match:
                            review_count = int(review_match.group(1).replace(",", ""))
                except Exception:
                    pass

                address = ""
                try:
                    addr_parts = link_el.find_elements(
                        By.CSS_SELECTOR, 'div[class*="fontBodyMedium"] span:not([role="img"])'
                    )
                    for part in addr_parts:
                        txt = part.text.strip()
                        if txt and txt != name and "star" not in txt.lower():
                            address = txt
                            break
                except Exception:
                    pass

                competitors.append(
                    {
                        "name": name,
                        "rating": rating,
                        "review_count": review_count,
                        "address": address,
                        "google_maps_url": href,
                    }
                )
            except StaleElementReferenceException as e:
                logging.warning(f"Stale element while parsing result: {e}")
                continue
            except Exception as e:
                logging.warning(f"Error parsing result: {e}")
                continue

        logging.info(f"Found {len(competitors)} competitors")

        # save the results in a json file 
        with open("competitors.json", "w", encoding="utf-8") as file:
            json.dump(competitors, file, indent=4, ensure_ascii=False)
        
        return competitors

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    INDUSTRY = "coffee shops"
    REGION = "Alexandria, Egypt"
    MAX_COMPETITORS = 3

    results = find_competitors(INDUSTRY, REGION, MAX_COMPETITORS)
    print(json.dumps(results, indent=2, ensure_ascii=False))
