import sys
import re
import time
import random
import platform
import shutil
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import NoSuchElementException, ElementClickInterceptedException
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.core.os_manager import ChromeType
from bs4 import BeautifulSoup
import pandas as pd

POST_URL   = sys.argv[1] if len(sys.argv) > 1 else ""
EMAIL      = sys.argv[2] if len(sys.argv) > 2 else ""
PASSWORD   = sys.argv[3] if len(sys.argv) > 3 else ""
MAX        = int(sys.argv[4]) if len(sys.argv) > 4 else 10
OUTPUT_CSV = sys.argv[5] if len(sys.argv) > 5 else "fb_comments.csv"

_TIME_RE = re.compile(
    r'\s*(\d+\s+(?:second|minute|hour|day|week|month|year)s?\s+ago'
    r'|a\s+(?:second|minute|hour|day|week|month|year)\s+ago'
    r'|just\s+now'
    r'|\d+[smhd])\s*$',
    re.IGNORECASE
)
_NON_PROFILE_PATHS = ("/posts/", "/groups/", "/events/", "/pages/",
                      "/photo", "/video", "/reel", "/watch")

_COUNT_RE = re.compile(r'^([\d,.]+)\s*([KkMm]?)\s*comments?$', re.IGNORECASE)


class FacebookScraper:
    def __init__(self, email, password):
        self.email    = email
        self.password = password
        self.driver   = None
        self.is_reel  = False

    def initialize_driver(self):
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument("--window-size=1280,900")

        if platform.system() == "Linux":
            chromium_path = (
                shutil.which("chromium")
                or shutil.which("chromium-browser")
                or "/usr/bin/chromium"
            )
            options.binary_location = chromium_path
            driver_path = ChromeDriverManager(chrome_type=ChromeType.CHROMIUM).install()
        else:
            driver_path = ChromeDriverManager().install()

        service = Service(driver_path)
        self.driver = webdriver.Chrome(service=service, options=options)
        self.driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

    def simulate_human_typing(self, element, text):
        for char in text:
            element.send_keys(char)
            time.sleep(random.uniform(0.05, 0.2))

    def login(self):
        self.driver.get("https://www.facebook.com/login")
        email_input = WebDriverWait(self.driver, 15).until(
            EC.presence_of_element_located((By.NAME, "email"))
        )
        self.simulate_human_typing(email_input, self.email)
        password_input = self.driver.find_element(By.NAME, "pass")
        self.simulate_human_typing(password_input, self.password)
        password_input.send_keys(Keys.RETURN)
        time.sleep(15)  # Increased wait for login

    def navigate_to_post(self, post_url):
        self.is_reel = "/reel/" in post_url.lower()
        self.driver.get(post_url)
        time.sleep(8)
        if self.is_reel:
            self._open_reel_comments_panel()

    # ------------------------------------------------------------------
    # Click helpers
    # ------------------------------------------------------------------

    def _is_click_intercepted(self, element):
        """
        A JS `.click()` fires directly on the element regardless of what's
        stacked on top of it, so it can silently do nothing on FB's layered
        reel UI (video overlay, scrubber, mute button, etc). This checks
        whether the element at the button's actual screen coordinates is
        the button itself (or a descendant) before we bother clicking.
        """
        try:
            return not self.driver.execute_script(
                """
                const el = arguments[0];
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return false;
                const x = r.left + r.width / 2;
                const y = r.top + r.height / 2;
                const top = document.elementFromPoint(x, y);
                return !!(top && (top === el || el.contains(top) || top.contains(el)));
                """,
                element,
            )
        except Exception:
            return False  # fail open, let the click attempt happen anyway

    def _real_click(self, element):
        """
        Uses a genuine pointer-event sequence (move -> pause -> click)
        instead of a synthetic JS click. Facebook's Comet UI frequently
        binds handlers to pointerdown/pointerup rather than plain click,
        so a JS-dispatched click can be a no-op even when it doesn't
        raise an exception.
        """
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", element
        )
        time.sleep(0.3)
        try:
            ActionChains(self.driver).move_to_element(element).pause(0.15).click().perform()
            return True
        except ElementClickInterceptedException:
            try:
                self.driver.execute_script("arguments[0].click();", element)
                return True
            except Exception:
                return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Reel comment panel
    # ------------------------------------------------------------------

    def _open_reel_comments_panel(self):
        """
        On a reel, the comments panel is frequently not rendered in the
        DOM at all until the comment icon below the video is clicked to
        open it. Reels also render as a vertical carousel that preloads
        the next reel below the current one, so more than one "Comment"
        button can exist in the DOM at once -- only the active reel's
        button has tabindex="0"; the preloaded one has tabindex="-1".
        We only click the active one, and we verify the click actually
        registered (via aria-expanded or newly-present comment articles)
        rather than assuming success just because .click() didn't throw.
        """
        selectors = [
            "div[aria-label='Comment'][tabindex='0']",
            "div[aria-label='Comment']",
            "div[aria-label='Leave a comment']",
            "div[aria-label='Comments']",
            "[aria-label='Comment' i][tabindex='0']",
            "[aria-label*='comment' i]",
        ]
        clicked_ok = False
        for sel in selectors:
            try:
                btns = self.driver.find_elements(By.CSS_SELECTOR, sel)
            except Exception:
                continue

            for btn in btns:
                if self._is_click_intercepted(btn):
                    # something is stacked on top of this button at its
                    # actual coordinates -- skip it, clicking would miss
                    continue

                if not self._real_click(btn):
                    continue

                time.sleep(2)
                opened = (
                    btn.get_attribute("aria-expanded") == "true"
                    or self._count_comment_articles() > 0
                    or self._find_comment_dialog() is not None
                )
                if opened:
                    clicked_ok = True
                    time.sleep(2)
                    return True

            if clicked_ok:
                break

        self._dump_reel_debug("after_comment_click", clicked=clicked_ok)
        return False

    def _find_comment_dialog(self):
        """
        Reels frequently open comments inside a portal-rendered
        role="dialog" that sits outside the article's DOM subtree
        entirely -- so walking up from the comment article to find a
        scrollable ancestor (which works fine on regular posts) can fail
        to find anything. Look for a dialog that actually contains
        comment articles.
        """
        try:
            dialogs = self.driver.find_elements(By.CSS_SELECTOR, "div[role='dialog']")
        except Exception:
            return None
        for d in dialogs:
            try:
                if d.find_elements(
                    By.XPATH, ".//div[@role='article' and starts-with(@aria-label, 'Comment by')]"
                ):
                    return d
            except Exception:
                continue
        return None

    def _dump_reel_debug(self, label, clicked=None):
        try:
            import os
            os.makedirs("debug", exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            png_path = f"debug/{label}_{stamp}.png"
            html_path = f"debug/{label}_{stamp}.html"
            self.driver.save_screenshot(png_path)
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            print(f"[debug] clicked comment button: {clicked}; dumped {png_path}, {html_path}")
        except Exception as e:
            print(f"[debug] could not write debug dump: {e}")

    def _count_comment_articles(self):
        soup = BeautifulSoup(self.driver.page_source, "html.parser")
        return len([
            d for d in soup.find_all("div", {"role": "article"})
            if d.get("aria-label", "").startswith("Comment by")
        ])

    def _get_scrollable_comments_container(self):
        """
        On reels, comments live in a scrollable side panel (or a
        portal-rendered dialog) next to the video rather than in the
        main page body, so scrolling `window` doesn't page in more of
        them. First tries to find a dialog that actually holds comment
        articles (see _find_comment_dialog); if that comes up empty,
        falls back to walking up from an actual *comment* element (not
        just any role="article" -- the reel/video itself is also
        role="article" and is usually the first such element on the
        page) to find its nearest scrollable ancestor.
        Returns None if no comment element is found yet (e.g. panel
        hasn't loaded/opened).
        """
        dialog = self._find_comment_dialog()
        if dialog is not None:
            try:
                is_scrollable = self.driver.execute_script(
                    """
                    const el = arguments[0];
                    const style = window.getComputedStyle(el);
                    return el.scrollHeight > el.clientHeight + 20 &&
                           (style.overflowY === 'auto' || style.overflowY === 'scroll' || style.overflowY === 'hidden');
                    """,
                    dialog,
                )
                if is_scrollable:
                    return dialog
            except Exception:
                pass

        try:
            article = self.driver.find_element(
                By.XPATH,
                "//div[@role='article' and starts-with(@aria-label, 'Comment by')]",
            )
        except NoSuchElementException:
            return dialog  # may still be usable even if not detected as scrollable
        try:
            container = self.driver.execute_script(
                """
                let el = arguments[0];
                while (el && el !== document.body) {
                    const style = window.getComputedStyle(el);
                    if (el.scrollHeight > el.clientHeight + 20 &&
                        (style.overflowY === 'auto' || style.overflowY === 'scroll')) {
                        return el;
                    }
                    el = el.parentElement;
                }
                return null;
                """,
                article,
            )
            return container or dialog
        except Exception:
            return dialog

    def load_comments(self, max_comments=10):
        """Scroll (page or, on reels, the comments panel/dialog) and click 'View more comments'"""
        if self.is_reel:
            self._open_reel_comments_panel()
            time.sleep(1.5)

        container = self._get_scrollable_comments_container() if self.is_reel else None

        for i in range(30):
            if container is not None:
                self.driver.execute_script("arguments[0].scrollTop += 800;", container)
            else:
                self.driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(2)

            if self.is_reel and container is None:
                container = self._get_scrollable_comments_container()

            try:
                btns = self.driver.find_elements(
                    By.XPATH,
                    "//span[contains(text(), 'View more comments') or contains(text(), 'view more comments') or contains(text(), 'Voir plus de commentaires') or contains(text(), 'Ver más comentarios')]"
                )
                for btn in btns:
                    if self._is_click_intercepted(btn):
                        continue
                    if self._real_click(btn):
                        time.sleep(3)
            except Exception:
                pass

            found = self._count_comment_articles()
            if found >= max_comments:
                break
        time.sleep(4)

    def get_actual_comment_count(self):
        try:
            soup = BeautifulSoup(self.driver.page_source, "html.parser")

            for span in soup.find_all("span"):
                text = span.get_text(strip=True)
                parsed = self._parse_count_text(text)
                if parsed is not None:
                    return parsed

            for el in soup.find_all(attrs={"aria-label": True}):
                parsed = self._parse_count_text(el["aria-label"].strip())
                if parsed is not None:
                    return parsed
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_count_text(text):
        m = _COUNT_RE.match(text)
        if not m:
            return None
        num_str, suffix = m.group(1).replace(",", ""), m.group(2).upper()
        try:
            value = float(num_str)
        except ValueError:
            return None
        if suffix == "K":
            value *= 1_000
        elif suffix == "M":
            value *= 1_000_000
        return int(value)

    @staticmethod
    def _parse_aria(aria_label):
        raw = aria_label.replace("Comment by ", "", 1).strip()
        m   = _TIME_RE.search(raw)
        if m:
            timestamp = m.group(0).strip()
            name      = raw[: m.start()].strip()
        else:
            name      = raw
            timestamp = ""
        return name, timestamp

    @staticmethod
    def _profile_url(href):
        if not href:
            return ""
        if href.startswith("https://web.facebook.com"):
            href = href.replace("https://web.facebook.com", "https://www.facebook.com", 1)
        elif href.startswith("/"):
            href = "https://www.facebook.com" + href
        if not href.startswith("https://www.facebook.com"):
            return ""
        base = href.split("?")[0].rstrip("/")
        path = base.replace("https://www.facebook.com", "")
        if any(p in path for p in _NON_PROFILE_PATHS):
            return ""
        if not path or path == "/":
            return ""
        return base

    def extract_comments(self, max_comments=10):
        soup           = BeautifulSoup(self.driver.page_source, "html.parser")
        comments_data  = []
        all_articles   = soup.find_all("div", {"role": "article"})
        comment_blocks = [
            d for d in all_articles
            if d.get("aria-label", "").startswith("Comment by")
        ]
        for block in comment_blocks:
            if len(comments_data) >= max_comments:
                break
            aria      = block.get("aria-label", "")
            name, ts  = self._parse_aria(aria)
            profile_url = ""
            for a in block.find_all("a", href=True):
                url = self._profile_url(a["href"])
                if url:
                    profile_url = url
                    break
            text = ""
            try:
                parts = []
                for el in block.find_all("div", {"dir": "auto"}):
                    t = el.get_text(strip=True)
                    if t and t != name and len(t) > 1:
                        parts.append(t)
                seen, unique = set(), []
                for p in parts:
                    if p not in seen:
                        seen.add(p); unique.append(p)
                text = " ".join(unique)
            except Exception:
                pass
            if text:
                comments_data.append({
                    "commenter_name": name,
                    "profile_url":    profile_url,
                    "comment_text":   text,
                    "comment_time":   ts,
                })
        return comments_data

    def close(self):
        if self.driver:
            self.driver.quit()

    def dump_debug(self, label="fb_scraper"):
        try:
            import os
            os.makedirs("debug", exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            png_path = f"debug/{label}_{stamp}.png"
            html_path = f"debug/{label}_{stamp}.html"
            self.driver.save_screenshot(png_path)
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            print(f"Dumped debug info: {png_path}, {html_path}")
        except Exception as e:
            print(f"Could not write debug dump: {e}")


if __name__ == "__main__":
    if not all([POST_URL, EMAIL, PASSWORD]):
        print("Missing required arguments", file=sys.stderr)
        sys.exit(1)

    scraper = FacebookScraper(EMAIL, PASSWORD)
    try:
        scraper.initialize_driver()
        scraper.login()
        scraper.navigate_to_post(POST_URL)

        actual = scraper.get_actual_comment_count()
        print(actual)

        if actual is not None and actual < MAX:
            print(f"Post only has {actual} comments, adjusting max from {MAX} to {actual}")
            MAX = actual

        scraper.load_comments(max_comments=MAX)
        comments = scraper.extract_comments(max_comments=MAX)

        if comments:
            df = pd.DataFrame(comments)
            df.drop_duplicates(inplace=True)
            df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
            print(f"Successfully scraped {len(comments)} comments to {OUTPUT_CSV}")
        else:
            print("No comments found", file=sys.stderr)
            scraper.dump_debug("no_comments")
    except Exception as e:
        print(f"Error: {str(e)}", file=sys.stderr)
        sys.exit(1)
    finally:
        scraper.close()