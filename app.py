"""
Competitor Finder - Streamlit UI

A wizard:
  1) Enter industry, location, and how many competitors to find
  2) Pick one of the competitors returned by find_competitors()
  3) Resolve that competitor's Facebook Page via scrape_competitor_profile.py
  4) Choose how many posts to retrieve and kick off the post scrape
     (results are cached to CSV per-page so revisiting this page later
     doesn't trigger another scrape)
  5) Pick a post and choose how many comments to retrieve
  6) Scrape (or load cached) comments for that post via fb_scraper_w_reels.py

Run with:
    streamlit run app.py
"""

import os
import re

import pandas as pd
import streamlit as st

from get_competitors import find_competitors
from scrape_competitor_profile import setup_browser, try_login, find_facebook_page_url, _load_fb_credentials
from post_scraper_test import FacebookScraper
from fb_scraper_w_reels import FacebookScraper as CommentScraper

st.set_page_config(page_title="Competitor Finder", page_icon="🔎", layout="centered")

# ---------------------------------------------------------------------------
# Post cache (CSV on disk, keyed by the Facebook page's handle)
# ---------------------------------------------------------------------------
CACHE_DIR = "scraped_posts"


def _cache_path_for(page_url: str) -> str:
    """Build a stable CSV path for a given Facebook page URL."""
    handle = page_url.rstrip("/").split("/")[-1]
    handle = re.sub(r"[^A-Za-z0-9_.-]", "_", handle)
    return os.path.join(CACHE_DIR, f"{handle}.csv")


def load_cached_posts(page_url: str, num_posts: int):
    """Return up to num_posts cached posts for this page, or None if no usable cache exists."""
    path = _cache_path_for(page_url)
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
    except Exception:
        return None
    if df.empty or len(df) < num_posts:
        # Not enough cached posts to satisfy the request
        return None
    df = df.where(pd.notnull(df), None)
    return df.head(num_posts).to_dict(orient="records")


def save_posts_to_cache(page_url: str, posts_data) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path_for(page_url)
    df = pd.DataFrame(posts_data, columns=[
        "post_text", "likes", "comments", "shares", "post_time", "post_link"
    ])

    df.to_csv(path, index=False)


# ---------------------------------------------------------------------------
# Comment cache (CSV on disk, keyed by the post's permalink)
# ---------------------------------------------------------------------------
COMMENTS_CACHE_DIR = "scraped_comments"


def _comment_cache_path_for(post_link: str) -> str:
    """Build a stable CSV path for a given post permalink."""
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", post_link.rstrip("/"))
    key = key[-120:]  # keep filenames sane for long permalinks
    return os.path.join(COMMENTS_CACHE_DIR, f"{key}.csv")


def load_cached_comments(post_link: str, num_comments: int):
    """Return up to num_comments cached comments for this post, or None if no usable cache exists."""
    path = _comment_cache_path_for(post_link)
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
    except Exception:
        return None
    if df.empty or len(df) < num_comments:
        return None
    df = df.where(pd.notnull(df), None)
    return df.head(num_comments).to_dict(orient="records")


def save_comments_to_cache(post_link: str, comments_data) -> None:
    os.makedirs(COMMENTS_CACHE_DIR, exist_ok=True)
    path = _comment_cache_path_for(post_link)
    df = pd.DataFrame(comments_data, columns=[
        "commenter_name", "profile_url", "comment_text", "comment_time"
    ])
    df.to_csv(path, index=False, encoding="utf-8-sig")


# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
        .stApp { background-color: #f5f6fb; }
        .block-container { max-width: 720px; padding-top: 2.5rem; }

        .step-pill {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            font-weight: 600;
            font-size: 1.05rem;
            color: #1f2233;
            margin-bottom: 1.5rem;
        }
        .step-pill span.num {
            background: #6C5CE7;
            color: white;
            width: 26px;
            height: 26px;
            border-radius: 50%;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-size: 0.85rem;
        }

        div[data-testid="stForm"] {
            background: white;
            padding: 2rem;
            border-radius: 16px;
            border: 1px solid #eceefb;
            box-shadow: 0 2px 10px rgba(30, 30, 60, 0.04);
        }

        .stButton>button, .stFormSubmitButton>button {
            background-color: #6C5CE7;
            color: white;
            border-radius: 10px;
            border: none;
            padding: 0.6rem 1.2rem;
            font-weight: 600;
        }
        .stButton>button:hover, .stFormSubmitButton>button:hover {
            background-color: #5a4bd6;
            color: white;
        }

        .profile-card {
            background: white;
            border: 1px solid #eceefb;
            border-radius: 14px;
            padding: 1rem 1.2rem;
            margin-bottom: 1.2rem;
            display: flex;
            align-items: center;
            gap: 14px;
        }
        .profile-avatar {
            width: 46px;
            height: 46px;
            border-radius: 50%;
            background: #1f2233;
            color: white;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 1.1rem;
            flex-shrink: 0;
        }
        .profile-name { font-weight: 600; color: #1f2233; font-size: 1rem; }
        .profile-meta { color: #6C5CE7; font-size: 0.85rem; }
        .profile-sub { color: #7a7d92; font-size: 0.8rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
defaults = {
    "step": 1,
    "industry": "",
    "location": "",
    "competitors": [],
    "selected_idx": None,
    "fb_page_url": None,
    "fb_lookup_done": False,
    "fb_page_url_corrected": False,
    "num_posts": 20,
    "posts": None,
    "posts_done": False,
    "posts_from_cache": False,
    "force_rescrape": False,
    # Comment-scraping state
    "selected_post": None,
    "num_comments": 20,
    "comments": None,
    "comments_done": False,
    "comments_from_cache": False,
    "force_recomment": False,
}
for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value


def go_to_step(step: int) -> None:
    st.session_state.step = step


st.title("Competitor Finder")

# ---------------------------------------------------------------------------
# Step 1 - Enter industry, location & number of competitors
# ---------------------------------------------------------------------------
if st.session_state.step == 1:
    st.markdown('<div class="step-pill"><span class="num">1</span> Enter Industry, Location &amp; Competitors</div>', unsafe_allow_html=True)

    with st.form("search_form"):
        st.subheader("Tell us about your search")
        st.caption("We'll find relevant competitors for you.")

        industry = st.text_input("Industry (singular)", placeholder="e.g. Coffee shop")
        location = st.text_input("Location", placeholder="e.g. Alexandria, Egypt")
        max_competitors = st.slider("Number of competitors to find", min_value=1, max_value=20, value=5)

        submitted = st.form_submit_button("Find Competitors", use_container_width=True)

    if submitted:
        if not industry.strip() or not location.strip():
            st.error("Please fill in both the industry and the location.")
        else:
            with st.spinner(f"Searching for {industry} in {location}..."):
                try:
                    results = find_competitors(industry.strip(), location.strip(), max_competitors)
                except Exception as e:
                    results = None
                    st.error(f"Something went wrong while searching: {e}")

            if results is not None:
                if not results:
                    st.warning("No competitors found. Try a different industry or location.")
                else:
                    st.session_state.competitors = results
                    st.session_state.industry = industry.strip()
                    st.session_state.location = location.strip()
                    st.session_state.selected_idx = None
                    st.session_state.fb_page_url = None
                    st.session_state.fb_lookup_done = False
                    st.session_state.fb_page_url_corrected = False
                    go_to_step(2)
                    st.rerun()

# ---------------------------------------------------------------------------
# Step 2 - Select a competitor
# ---------------------------------------------------------------------------
elif st.session_state.step == 2:
    st.markdown('<div class="step-pill"><span class="num">2</span> Select a Competitor</div>', unsafe_allow_html=True)

    competitors = st.session_state.competitors
    st.write(f"We found {len(competitors)} competitors for you.")

    labels = []
    for c in competitors:
        label = f"**{c['name']}**"
        labels.append(label)

    choice = st.radio(
        "Choose a competitor",
        options=range(len(competitors)),
        format_func=lambda i: labels[i],
        index=st.session_state.selected_idx if st.session_state.selected_idx is not None else 0,
        label_visibility="collapsed",
    )
    st.session_state.selected_idx = choice

    col_back, col_continue = st.columns([1, 1])
    with col_back:
        if st.button("Back", use_container_width=True):
            go_to_step(1)
            st.rerun()
    with col_continue:
        if st.button("Continue", use_container_width=True, type="primary"):
            # New competitor selected -> force a fresh Facebook lookup
            st.session_state.fb_page_url = None
            st.session_state.fb_lookup_done = False
            st.session_state.fb_page_url_corrected = False
            go_to_step(3)
            st.rerun()

# ---------------------------------------------------------------------------
# Step 3 - Resolve Facebook Page, let the user correct it, then ask how
#          many posts to retrieve
# ---------------------------------------------------------------------------
elif st.session_state.step == 3:
    selected = st.session_state.competitors[st.session_state.selected_idx]
    name = selected.get("name", "Unknown")
    address = selected.get("address", "")

    st.markdown('<div class="step-pill"><span class="num">3</span> Facebook Profile</div>', unsafe_allow_html=True)
    st.subheader("How many posts would you like to get?")
    st.caption(f"From: {name}")

    # Resolve the Facebook Page URL once per competitor selection
    if not st.session_state.fb_lookup_done:
        with st.spinner(f"Looking up '{name}' on Facebook..."):
            try:
                driver = setup_browser(headless=True)
                try:
                    try_login(driver)
                    extra = " ".join(x for x in [st.session_state.get("industry"), st.session_state.get("location")] if x)
                    page_url = find_facebook_page_url(driver, name, industry=extra or None)
                finally:
                    driver.quit()
            except Exception as e:
                page_url = None
                st.error(f"Facebook lookup failed: {e}")
        st.session_state.fb_page_url = page_url
        st.session_state.fb_lookup_done = True

    detected_url = st.session_state.fb_page_url

    if not detected_url and not st.session_state.fb_page_url_corrected:
        st.warning(f"Couldn't find a Facebook page for **{name}**.")

    st.markdown(
        f"""
        <div class="profile-card">
            <div class="profile-avatar">{name[0].upper() if name else "?"}</div>
            <div>
                <div class="profile-name">{name}</div>
                <div class="profile-sub">{address}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Let the user fix the detected URL if it's wrong (or supply one if
    # nothing was found at all).
    edited_url = st.text_input(
        "Facebook page URL",
        value=detected_url or "",
        placeholder="https://www.facebook.com/PageName",
        help="This is what we found automatically. Edit it here if it's wrong, then continue.",
    ).strip()

    page_url = edited_url or None

    if page_url and page_url != detected_url:
        st.session_state.fb_page_url_corrected = True

    if page_url:
        handle = page_url.rstrip("/").split("/")[-1]
        st.caption(f"@{handle}")
        st.markdown(f"[View Facebook page ↗]({page_url})")

    if not page_url:
        if st.button("← Back", use_container_width=True):
            go_to_step(2)
            st.rerun()
    else:
        num_posts = st.slider("Number of posts to retrieve", min_value=1, max_value=100, value=st.session_state.num_posts)
        st.session_state.num_posts = num_posts

        has_cache = os.path.exists(_cache_path_for(page_url))
        force_rescrape = False
        if has_cache:
            force_rescrape = st.checkbox(
                "Ignore saved posts and scrape again",
                value=False,
                help="We already have saved posts for this page. Leave unchecked to reuse them instead of scraping again.",
            )

        col_back, col_get = st.columns([1, 1])
        with col_back:
            if st.button("← Back", use_container_width=True):
                go_to_step(2)
                st.rerun()
        with col_get:
            if st.button("Get Posts", use_container_width=True, type="primary"):
                # Lock in the (possibly corrected) URL for the scrape step
                st.session_state.fb_page_url = page_url
                st.session_state.posts = None
                st.session_state.posts_done = False
                st.session_state.posts_from_cache = False
                st.session_state.force_rescrape = force_rescrape
                go_to_step(4)
                st.rerun()

# ---------------------------------------------------------------------------
# Step 4 - Load cached posts, or run the scrape if none exist yet
# ---------------------------------------------------------------------------
elif st.session_state.step == 4:
    selected = st.session_state.competitors[st.session_state.selected_idx]
    name = selected.get("name", "Unknown")
    page_url = st.session_state.fb_page_url
    num_posts = st.session_state.num_posts

    st.markdown('<div class="step-pill"><span class="num">4</span> Scraped Posts</div>', unsafe_allow_html=True)
    st.subheader(f"Posts from {name}")

    if not st.session_state.posts_done:
        cached = None
        if not st.session_state.force_rescrape:
            cached = load_cached_posts(page_url, num_posts)

        if cached is not None:
            st.session_state.posts = cached
            st.session_state.posts_done = True
            st.session_state.posts_from_cache = True
        else:
            creds = _load_fb_credentials()
            if not creds:
                st.error(
                    "No Facebook credentials found. Add `FB_EMAIL` and `FB_PASSWORD` to "
                    "`.streamlit/secrets.toml` so `post_scraper_test.py` can log in and scrape posts."
                )
                st.session_state.posts_done = True  # stop re-running this check every rerun
            else:
                with st.spinner(f"Fetching {num_posts} posts from {name}..."):
                    scraper = FacebookScraper(creds["email"], creds["password"])
                    try:
                        scraper.initialize_driver()
                        scraper.login()
                        # The root Page URL can show a "Featured"/algorithmic mix of
                        # posts (older high-engagement posts can rank above recent
                        # ones). The /posts sub-page forces strict newest-first
                        # ordering instead.
                        posts_url = page_url.rstrip("/") + "/posts"
                        scraper.navigate_to_profile(posts_url)
                        posts = scraper.scrape_posts(max_posts=num_posts)
                    except Exception as e:
                        posts = None
                        st.error(f"Something went wrong while fetching posts: {e}")
                    finally:
                        scraper.close()

                if posts:
                    save_posts_to_cache(page_url, posts)

                st.session_state.posts = posts
                st.session_state.posts_done = True
                st.session_state.posts_from_cache = False

    posts = st.session_state.posts
    print(posts)
    # Drop any record without a resolved post_link, and drop duplicate links
    if posts:
        seen_links = set()
        deduped = []
        for p in posts:
            link = p.get("post_link")
            if not link:
                continue
            norm = link.split("?")[0].rstrip("/")   # normalize before comparing
            if norm in seen_links:
                continue
            seen_links.add(norm)
            deduped.append(p)
        posts = deduped
    if st.session_state.posts_from_cache:
        st.caption("Loaded from previously saved posts — no new scrape needed.")

    if posts:
        st.caption(f"Showing {len(posts)} posts")

        # Download button for the scraped posts
        df_posts = pd.DataFrame(posts, columns=[
            "post_text", "likes", "comments", "shares", "post_time", "post_link"
        ])
        st.download_button(
            "Download posts (CSV)",
            data=df_posts.to_csv(index=False, encoding="utf-8-sig"),
            file_name="posts.csv",
            mime="text/csv",
            use_container_width=True,
        )

        for i, p in enumerate(posts):
            text = p.get("post_text", "")
            text_html = f'<div class="profile-name" style="margin:4px 0;">{text}</div>' if text else ""
            st.markdown(
                f"""
                <div class="profile-card">
                    <div>
                        {text_html}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            link_col, btn_col = st.columns([1, 1])
            with link_col:
                st.markdown(f"[View on Facebook ↗]({p['post_link']})")
            with btn_col:
                if st.button("Get comments →", key=f"getcomments_{i}", use_container_width=True):
                    st.session_state.selected_post = p
                    st.session_state.num_comments = 20
                    st.session_state.comments = None
                    st.session_state.comments_done = False
                    st.session_state.comments_from_cache = False
                    st.session_state.force_recomment = False
                    go_to_step(5)
                    st.rerun()
    elif st.session_state.posts is not None:
        st.warning("No posts were found for this page.")

    col_back, col_refresh = st.columns([1, 1])
    with col_back:
        if st.button("← Back"):
            go_to_step(3)
            st.rerun()
    with col_refresh:
        if st.button("Scrape again", help="Ignore the saved posts and fetch fresh ones"):
            st.session_state.posts = None
            st.session_state.posts_done = False
            st.session_state.force_rescrape = True
            st.rerun()

# ---------------------------------------------------------------------------
# Step 5 - Choose how many comments to retrieve for the selected post
# ---------------------------------------------------------------------------
elif st.session_state.step == 5:
    post = st.session_state.selected_post
    post_link = post.get("post_link") if post else None

    st.markdown('<div class="step-pill"><span class="num">5</span> Choose Number of Comments</div>', unsafe_allow_html=True)
    st.subheader("How many comments would you like to get?")

    preview = (post.get("post_text") or "").strip() if post else ""
    if preview:
        st.markdown(
            f"""
            <div class="profile-card">
                <div>
                    <div class="profile-sub" style="margin-bottom:4px;">From selected post</div>
                    <div class="profile-name">{preview}</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    if post_link:
        st.markdown(f"[View on Facebook ↗]({post_link})")

    num_comments = st.slider("Number of comments to retrieve", min_value=1, max_value=200, value=st.session_state.num_comments)
    st.session_state.num_comments = num_comments

    has_cache = bool(post_link) and os.path.exists(_comment_cache_path_for(post_link))
    force_recomment = False
    if has_cache:
        force_recomment = st.checkbox(
            "Ignore saved comments and scrape again",
            value=False,
            help="We already have saved comments for this post. Leave unchecked to reuse them.",
        )

    col_back, col_get = st.columns([1, 1])
    with col_back:
        if st.button("← Back", use_container_width=True):
            go_to_step(4)
            st.rerun()
    with col_get:
        if st.button("Get Comments", use_container_width=True, type="primary"):
            st.session_state.comments = None
            st.session_state.comments_done = False
            st.session_state.comments_from_cache = False
            st.session_state.force_recomment = force_recomment
            go_to_step(6)
            st.rerun()

# ---------------------------------------------------------------------------
# Step 6 - Load cached comments, or scrape them from the post link
# ---------------------------------------------------------------------------
elif st.session_state.step == 6:
    post = st.session_state.selected_post
    post_link = post.get("post_link") if post else None
    num_comments = st.session_state.num_comments

    st.markdown('<div class="step-pill"><span class="num">6</span> Comments </div>', unsafe_allow_html=True)

    # Header with dummy relevancy toggle (no effect yet)
    head_l, head_r = st.columns([2, 1])
    with head_l:
        st.subheader("Comments")
    with head_r:
        st.toggle("Filter (Relevancy)", value=False, help="Not wired up yet — placeholder.")

    if not post_link:
        st.warning("No post selected.")
    else:
        if not st.session_state.comments_done:
            cached = None
            if not st.session_state.force_recomment:
                cached = load_cached_comments(post_link, num_comments)

            if cached is not None:
                st.session_state.comments = cached
                st.session_state.comments_done = True
                st.session_state.comments_from_cache = True
            else:
                creds = _load_fb_credentials()
                if not creds:
                    st.error(
                        "No Facebook credentials found. Add `FB_EMAIL` and `FB_PASSWORD` to "
                        "`.streamlit/secrets.toml` so comments can be scraped."
                    )
                    st.session_state.comments_done = True
                else:
                    with st.spinner(f"Fetching up to {num_comments} comments..."):
                        scraper = CommentScraper(creds["email"], creds["password"])
                        try:
                            scraper.initialize_driver()
                            scraper.login()
                            scraper.navigate_to_post(post_link)

                            max_c = num_comments
                            actual = scraper.get_actual_comment_count()
                            if actual is not None and actual < max_c:
                                max_c = actual

                            scraper.load_comments(max_comments=max_c)
                            comments = scraper.extract_comments(max_comments=max_c)
                        except Exception as e:
                            comments = None
                            st.error(f"Something went wrong while fetching comments: {e}")
                        finally:
                            scraper.close()

                    if comments:
                        save_comments_to_cache(post_link, comments)

                    st.session_state.comments = comments
                    st.session_state.comments_done = True
                    st.session_state.comments_from_cache = False

    comments = st.session_state.comments

    if st.session_state.comments_from_cache:
        st.caption("Loaded from previously saved comments — no new scrape needed.")

    if comments:
        st.caption(f"Showing {len(comments)} comments")
        for c in comments:
            cname = c.get("commenter_name", "") or "?"
            ctext = c.get("comment_text", "")
            ctime = c.get("comment_time", "")
            profile = c.get("profile_url", "")
            name_html = f'<a href="{profile}" target="_blank">{cname}</a>' if profile else cname
            st.markdown(
                f"""
                <div class="profile-card">
                    <div class="profile-avatar">{cname[0].upper() if cname else "?"}</div>
                    <div>
                        <div class="profile-name">{name_html}</div>
                        <div class="profile-name" style="font-weight:400;margin:2px 0;">{ctext}</div>
                        <div class="profile-sub">{ctime}</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        # Download button for the scraped comments
        df_comments = pd.DataFrame(comments, columns=[
            "commenter_name", "profile_url", "comment_text", "comment_time"
        ])
        st.download_button(
            "Download comments (CSV)",
            data=df_comments.to_csv(index=False, encoding="utf-8-sig"),
            file_name="comments.csv",
            mime="text/csv",
            use_container_width=True,
        )
    elif st.session_state.comments is not None:
        st.warning("No comments were found for this post.")

    col_back, col_refresh = st.columns([1, 1])
    with col_back:
        if st.button("← Back"):
            go_to_step(5)
            st.rerun()
    with col_refresh:
        if st.button("Scrape again", help="Ignore saved comments and fetch fresh ones"):
            st.session_state.comments = None
            st.session_state.comments_done = False
            st.session_state.force_recomment = True
            st.rerun()