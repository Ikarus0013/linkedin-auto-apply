import asyncio
import logging
import os
import random
import re

from playwright.async_api import Page, BrowserContext, async_playwright
from playwright_stealth import Stealth

from config import (
    ACTION_DELAY_MIN,
    ACTION_DELAY_MAX,
    BROWSER_STATE_DIR,
)

# Realistic Win10 Chrome user agent — matches what a real user would have
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_STEALTH = Stealth(
    navigator_platform_override="Win32",
    navigator_user_agent_override=_USER_AGENT,
    navigator_vendor_override="Google Inc.",
)

logger = logging.getLogger(__name__)


async def random_delay(lo: float = ACTION_DELAY_MIN, hi: float = ACTION_DELAY_MAX):
    delay = random.uniform(lo, hi)
    await asyncio.sleep(delay)


async def human_click(page: Page, locator, timeout: int = 10_000):
    element = locator if hasattr(locator, "bounding_box") else page.locator(locator)
    await element.wait_for(state="visible", timeout=timeout)
    await element.scroll_into_view_if_needed()
    box = await element.bounding_box()
    if box:
        x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        await page.mouse.move(x, y, steps=random.randint(5, 15))
        await asyncio.sleep(random.uniform(0.1, 0.3))
        await page.mouse.click(x, y)
    else:
        await element.click()
    await random_delay(0.5, 1.5)


async def launch_browser() -> tuple[BrowserContext, Page]:
    BROWSER_STATE_DIR.mkdir(parents=True, exist_ok=True)
    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(BROWSER_STATE_DIR),
        headless=False,
        viewport={"width": 1280, "height": 800},
        user_agent=_USER_AGENT,
        locale="en-US",
        timezone_id="America/New_York",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
        ],
    )
    # Apply full stealth patches to every page opened in this context
    await _STEALTH.apply_stealth_async(context)
    page = context.pages[0] if context.pages else await context.new_page()
    logger.info("Browser launched with persistent context (stealth mode)")
    return context, page


async def login(page: Page):
    email = os.getenv("LINKEDIN_EMAIL", "")
    password = os.getenv("LINKEDIN_PASSWORD", "")
    if not email or not password:
        raise RuntimeError("Set LINKEDIN_EMAIL and LINKEDIN_PASSWORD in .env")

    await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
    await random_delay(1, 2)

    # Check if already logged in (redirected to feed)
    if "/feed" in page.url:
        logger.info("Already logged in (session restored)")
        return

    # Fill credentials
    await page.fill("#username", email)
    await random_delay(0.5, 1)
    await page.fill("#password", password)
    await random_delay(0.5, 1)

    await human_click(page, 'button[type="submit"]')
    await random_delay(3, 5)

    # Handle potential 2FA / security check
    if "checkpoint" in page.url or "challenge" in page.url:
        logger.warning("2FA or security challenge detected — waiting 120s for manual resolution")
        await asyncio.sleep(120)

    if "/feed" in page.url or "linkedin.com" in page.url:
        logger.info("Login successful")
    else:
        logger.warning("Login may have failed — current URL: %s", page.url)


async def search_jobs(page: Page, role: str, location: str):
    search_url = (
        "https://www.linkedin.com/jobs/search/?"
        f"keywords={role.replace(' ', '%20')}"
        f"&location={location.replace(' ', '%20')}"
        "&f_AL=true"  # Easy Apply filter
        "&sortBy=DD"  # Sort by most recent
    )
    logger.info("Searching: %s in %s", role, location)
    await page.goto(search_url, wait_until="domcontentloaded")
    await random_delay(3, 5)


async def collect_job_listings(page: Page) -> list[dict]:
    listings = []
    job_cards = page.locator(".job-card-container, .jobs-search-results__list-item")
    count = await job_cards.count()
    logger.info("Found %d job cards on page", count)

    for i in range(count):
        card = job_cards.nth(i)
        try:
            # Extract job info — use .first to avoid duplicate text from nested elements
            title_el = card.locator(".job-card-list__title, .job-card-container__link")
            title = (await title_el.first.text_content() or "").strip() if await title_el.count() > 0 else ""

            company_el = card.locator(".job-card-container__primary-description, .artdeco-entity-lockup__subtitle")
            company = (await company_el.first.text_content() or "").strip() if await company_el.count() > 0 else ""

            location_el = card.locator(".job-card-container__metadata-item, .artdeco-entity-lockup__caption")
            loc = (await location_el.first.text_content() or "").strip() if await location_el.count() > 0 else ""

            link_el = card.locator("a[href*='/jobs/view/']")
            href = await link_el.get_attribute("href") or ""
            job_url = href.split("?")[0] if href else ""

            # Extract job ID from URL
            job_id_match = re.search(r"/jobs/view/(\d+)", job_url)
            job_id = job_id_match.group(1) if job_id_match else ""

            if job_id:
                listings.append({
                    "title": title,
                    "company": company,
                    "location": loc,
                    "url": f"https://www.linkedin.com{job_url}" if job_url.startswith("/") else job_url,
                    "job_id": job_id,
                })
        except Exception as e:
            logger.debug("Failed to parse job card %d: %s", i, e)

    return listings


async def next_page(page: Page) -> bool:
    next_btn = page.locator('button[aria-label="View next page"], li.artdeco-pagination__indicator--number.selected + li button')
    if await next_btn.count() > 0:
        try:
            await human_click(page, next_btn.first)
            await random_delay(3, 5)
            return True
        except Exception:
            return False
    return False


async def open_job(page: Page, url: str):
    await page.goto(url, wait_until="domcontentloaded")
    await random_delay(2, 4)
