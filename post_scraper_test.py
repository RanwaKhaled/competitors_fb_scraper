from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
import re
import time
import random
import pandas as pd

# Patterns that indicate the href is a real post permalink rather than a
# tracking-only query fragment (Facebook stopped putting the real post id in
# the timestamp anchor's static href, so this is only ever a positive match,
# never something to rely on for extraction).
_REAL_POST_PATTERNS = [
    re.compile(r'/posts/[\w-]+'),
    re.compile(r'/videos/[\w-]+'),
    re.compile(r'/reel/[\w-]+'),
    re.compile(r'story_fbid=[\w%.-]+'),
    re.compile(r'permalink\.php\?story_fbid=[\w%.-]+'),
]

TIME_ANCHOR_SELECTOR = "div.xu06os2.x1ok221b > span > div > span > span > a"


class FacebookScraper:
    def __init__(self, email, password):
        self.email = email
        self.password = password
        self.driver = None
        # Cache resolved permalinks by the post's raw (unresolved) href so a
        # post that's still on screen across multiple scroll iterations isn't
        # clicked through more than once.
        self._resolved_link_cache = {}

    def initialize_driver(self):
        """Initialize the Chrome webdriver with custom options"""
        options = webdriver.ChromeOptions()
        # make it headless
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        self.driver = webdriver.Chrome(options=options)
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    def simulate_human_typing(self, element, text):
        """Simulate human-like typing patterns"""
        for char in text:
            element.send_keys(char)
            time.sleep(random.uniform(0.1, 0.3))
            if random.random() < 0.1:
                time.sleep(random.uniform(0.3, 0.7))

    def login(self):
        """Login to Facebook"""
        self.driver.get("https://www.facebook.com/login")

        # Enter email
        email_input = WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.NAME, "email"))
        )
        self.simulate_human_typing(email_input, self.email)

        # Enter password
        password_input = WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.NAME, "pass"))
        )
        self.simulate_human_typing(password_input, self.password)

        # Submit by pressing Enter instead of locating/clicking the login button
        password_input.send_keys(Keys.RETURN)

        time.sleep(15)

    def navigate_to_profile(self, profile_url):
        """Navigate to a specific Facebook profile"""
        self.driver.get(profile_url)
        time.sleep(4)

    def slow_scroll(self, step=500):
        """Scroll the page slowly"""
        self.driver.execute_script(f"window.scrollBy(0, {step});")
        time.sleep(2)

    @staticmethod
    def _normalize_href(href):
        """Turn a relative or web.facebook.com href into an absolute www.facebook.com URL"""
        if not href:
            return ""
        if href.startswith("https://web.facebook.com"):
            href = href.replace("https://web.facebook.com", "https://www.facebook.com", 1)
        elif href.startswith("/") or href.startswith("?"):
            href = "https://www.facebook.com" + href
        if not href.startswith("https://www.facebook.com"):
            return ""
        return href

    @staticmethod
    def _is_real_post_link(href):
        return bool(href) and any(p.search(href) for p in _REAL_POST_PATTERNS)

    def _resolve_permalink_by_click(self, timestamp_element):
        """
        Click the post's timestamp link and read back the URL the browser
        actually navigates to. Facebook no longer reliably puts the real
        post id in the timestamp anchor's static href (it's usually just a
        tracking query fragment like "?__cft__[0]=..."), so clicking through
        and reading the resulting URL is the only reliable way to recover it.
        """
        original_window = self.driver.current_window_handle
        before_handles = set(self.driver.window_handles)
        resolved = None
        try:
            try:
                timestamp_element.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", timestamp_element)

            new_handle = None
            end = time.time() + 5
            while time.time() < end:
                opened = set(self.driver.window_handles) - before_handles
                if opened:
                    new_handle = opened.pop()
                    break
                time.sleep(0.2)

            if new_handle:
                self.driver.switch_to.window(new_handle)
                time.sleep(2)
                resolved = self.driver.current_url
                self.driver.close()
                self.driver.switch_to.window(original_window)
            else:
                # No new tab — it may have routed the current tab client-side.
                time.sleep(1.5)
                if self.driver.current_window_handle == original_window:
                    resolved = self.driver.current_url
                    self.driver.back()
                    time.sleep(2)
        except Exception:
            resolved = None
        finally:
            if self.driver.current_window_handle != original_window and original_window in self.driver.window_handles:
                self.driver.switch_to.window(original_window)
        return resolved

    def resolve_post_link_and_time(self, live_post, post_soup):
        """Get the post's timestamp text and its real permalink, clicking through when needed"""
        time_anchor = post_soup.select_one(TIME_ANCHOR_SELECTOR)
        post_time = time_anchor.get_text(strip=True) if time_anchor else None
        raw_href = time_anchor.get("href") if time_anchor else None

        href = self._normalize_href(raw_href)
        if self._is_real_post_link(href):
            return post_time, href.split("?")[0]

        if raw_href in self._resolved_link_cache:
            return post_time, self._resolved_link_cache[raw_href]

        try:
            live_anchor = live_post.find_element(By.CSS_SELECTOR, TIME_ANCHOR_SELECTOR)
        except Exception:
            live_anchor = None

        post_link = href or None
        if live_anchor is not None:
            clicked_url = self._resolve_permalink_by_click(live_anchor)
            if clicked_url:
                resolved = self._normalize_href(clicked_url) or clicked_url
                if self._is_real_post_link(resolved):
                    resolved = resolved.split("?")[0]
                post_link = resolved

        if raw_href is not None:
            self._resolved_link_cache[raw_href] = post_link

        return post_time, post_link

    def extract_posts_with_bs(self):
        """Extract posts data, resolving each post's real permalink via BeautifulSoup + live click-through"""
        posts_data = []
        live_posts = self.driver.find_elements(By.CSS_SELECTOR, "div.x1n2onr6.x1ja2u2z")

        for live_post in live_posts:
            try:
                outer_html = live_post.get_attribute("outerHTML")
                post = BeautifulSoup(outer_html, "html.parser")

                message_elements = post.find_all("div", {"data-ad-preview": "message"})
                post_text = " ".join([msg.get_text(strip=True) for msg in message_elements])

                likes_element = post.select_one("span.xt0b8zv.x1jx94hy.xrbpyxo.xl423tq > span > span")
                likes = likes_element.get_text(strip=True) if likes_element else None

                comments_element = post.select("div > div > span > div > div > div > span > span.html-span ")
                comments = comments_element[0].text if comments_element else None

                shares_element = post.select("div > div > span > div > div > div > span > span.html-span ")
                shares = shares_element[1].text if shares_element else None

                post_time, post_link = self.resolve_post_link_and_time(live_post, post)

                if not post_text or not post_link:
                    continue   # drop nested fragments / blank containers

                posts_data.append({
                    "post_text": post_text,
                    "likes": likes,
                    "comments": comments,
                    "shares": shares,
                    "post_time": post_time,
                    "post_link": post_link
                })
            except Exception as e:
                print("Error extracting post data:", e)

        return posts_data

    def remove_duplicates(self, data_list):
        seen = set()
        unique_data = []
        for data in data_list:
            link = data.get("post_link")
            if link:
                key = link.split("?")[0].rstrip("/")   # normalize slash + query
            else:
                key = tuple(data.items())
            if key not in seen:
                seen.add(key)
                unique_data.append(data)
        return unique_data

    def scrape_posts(self, max_posts):
        """Scrape a specified number of posts"""
        all_posts = []
        stale_scrolls = 0
        previous_count = 0

        while len(all_posts) < max_posts:
            posts = self.extract_posts_with_bs()
            all_posts.extend(posts)
            all_posts = self.remove_duplicates(all_posts)
            print(f"Extracted {len(all_posts)} unique posts so far.")

            if len(all_posts) >= max_posts:
                break

            # Stop if scrolling isn't producing new posts (e.g. reached end of feed)
            if len(all_posts) == previous_count:
                stale_scrolls += 1
                if stale_scrolls >= 5:
                    print("No new posts found after several scrolls, stopping.")
                    break
            else:
                stale_scrolls = 0
            previous_count = len(all_posts)

            self.slow_scroll()

        return all_posts[:max_posts]

    def to_dataframe(self, posts_data):
        """Convert scraped posts data into a pandas DataFrame"""
        df = pd.DataFrame(posts_data, columns=[
            "post_text", "likes", "comments", "shares", "post_time", "post_link"
        ])
        return df

    def print_posts(self, posts_data):
        """Print the scraped posts data"""
        for idx, post in enumerate(posts_data, start=1):
            print(f"Post {idx}:")
            print(f"Text: {post['post_text']}")
            print(f"Likes: {post['likes']}")
            print(f"Comments: {post['comments']}")
            print(f"Shares: {post['shares']}")
            print(f"Time Posted: {post['post_time']}")
            print(f"Link: {post['post_link']}")
            print("-" * 50)

    def close(self):
        """Close the browser"""
        if self.driver:
            self.driver.quit()


# Example usage
if __name__ == "__main__":
    # Initialize the scraper
    scraper = FacebookScraper("hathawayrose50@gmail.com", "saintjoseph1282004")

    try:
        # Setup and login
        scraper.initialize_driver()
        scraper.login()

        # Navigate to a profile
        scraper.navigate_to_profile("https://web.facebook.com/Worldfitstanley")

        # Scrape 10 posts
        posts_data = scraper.scrape_posts(max_posts=4)

        # Print the results
        scraper.print_posts(posts_data)

        # Build and inspect the DataFrame
        df = scraper.to_dataframe(posts_data)
        # df.dropna(inplace=True)
        df.dropna(subset=['post_link'], inplace=True)
        print(df)

        # Optionally save to CSV
        df.to_csv("facebook_posts.csv", index=False)

    finally:
        # Clean up
        scraper.close()