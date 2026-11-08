import argparse
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

from config import (
    JOB_ROLES,
    LOCATIONS,
    MAX_APPLICATIONS_PER_RUN,
    APP_DELAY_MIN,
    APP_DELAY_MAX,
    DATA_DIR,
    LOG_PATH,
)
from linkedin_bot import launch_browser, login, search_jobs, collect_job_listings, next_page, random_delay
from easy_apply import has_easy_apply, click_easy_apply, fill_form, dismiss_modal
from direct_apply import has_direct_apply, run as direct_apply_run
from tracker import load_applied_jobs, is_already_applied, log_application
import gemini_helper


LOCK_FILE = DATA_DIR / "bot.lock"

_JUNIOR_KEYWORDS = [
    "junior", "jr.", "jr ", "entry level", "entry-level", "associate ",
    "intern", "trainee", "apprentice", "graduate ", "grad ",
    "working student", "werkstudent", "praktikant", "praktikantin",
    "berufseinsteiger", "absolvent",
]


def is_junior_role(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _JUNIOR_KEYWORDS)


def acquire_lock():
    if LOCK_FILE.exists():
        existing_pid = LOCK_FILE.read_text().strip()
        try:
            os.kill(int(existing_pid), 0)  # check if process is alive
            print(f"ERROR: bot is already running (PID {existing_pid}). Exiting.")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass  # stale lock — process is gone, safe to continue
    LOCK_FILE.write_text(str(os.getpid()))


def release_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def setup_logging():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
        ],
    )


def parse_args():
    parser = argparse.ArgumentParser(description="LinkedIn Easy Apply Bot")
    parser.add_argument("--dry-run", action="store_true", help="Collect jobs but don't apply")
    parser.add_argument("--max-apps", type=int, default=MAX_APPLICATIONS_PER_RUN, help="Max applications per run")
    parser.add_argument("--roles", type=str, default="", help="Comma-separated roles to search (overrides config)")
    parser.add_argument("--locations", type=str, default="", help="Comma-separated locations to search (overrides config)")
    return parser.parse_args()


async def run(args):
    logger = logging.getLogger("main")
    load_dotenv()

    roles = [r.strip() for r in args.roles.split(",") if r.strip()] if args.roles else JOB_ROLES
    locations = [l.strip() for l in args.locations.split(",") if l.strip()] if args.locations else LOCATIONS
    max_apps = args.max_apps
    dry_run = args.dry_run

    if dry_run:
        logger.info("=== DRY RUN MODE — will not submit applications ===")

    gemini_helper.init_gemini()
    applied_set = load_applied_jobs()
    stats = {"applied": 0, "skipped": 0, "errors": 0, "already_applied": 0}

    context, page = await launch_browser()

    try:
        await login(page)

        for role in roles:
            if stats["applied"] >= max_apps:
                logger.info("Reached max applications (%d), stopping", max_apps)
                break

            for location in locations:
                if stats["applied"] >= max_apps:
                    break

                await search_jobs(page, role, location)

                page_num = 1
                max_pages = 40
                while stats["applied"] < max_apps and page_num <= max_pages:
                    logger.info("--- %s | %s | Page %d ---", role, location, page_num)
                    listings = await collect_job_listings(page)

                    if not listings:
                        logger.info("No listings found, moving on")
                        break

                    for job in listings:
                        if stats["applied"] >= max_apps:
                            break

                        job_id = job["job_id"]

                        if is_already_applied(job_id, applied_set):
                            stats["already_applied"] += 1
                            logger.debug("Already applied: %s", job["title"])
                            continue

                        if is_junior_role(job["title"]):
                            log_application(
                                job["title"], job["company"], job["location"],
                                job["url"], job_id, "skipped", "Junior role"
                            )
                            applied_set.add(job_id)
                            stats["skipped"] += 1
                            logger.info("Skipped (junior): %s at %s", job["title"], job["company"])
                            continue

                        logger.info("Attempting: %s at %s", job["title"], job["company"])

                        try:
                            # Click on the job card to load details in side panel
                            job_card = page.locator(f'a[href*="/jobs/view/{job_id}"]')
                            if await job_card.count() > 0:
                                from linkedin_bot import human_click
                                await human_click(page, job_card.first)
                                await random_delay(2, 3)
                            else:
                                # Navigate directly
                                from linkedin_bot import open_job
                                await open_job(page, job["url"])

                            easy_apply = await has_easy_apply(page)
                            direct_apply = not easy_apply and await has_direct_apply(page)

                            if not easy_apply and not direct_apply:
                                log_application(
                                    job["title"], job["company"], job["location"],
                                    job["url"], job_id, "skipped", "No apply button"
                                )
                                stats["skipped"] += 1
                                continue

                            if dry_run:
                                apply_type = "Easy Apply" if easy_apply else "Direct Apply"
                                log_application(
                                    job["title"], job["company"], job["location"],
                                    job["url"], job_id, "skipped", "Dry run"
                                )
                                stats["skipped"] += 1
                                logger.info("[DRY RUN] Would apply (%s): %s at %s",
                                            apply_type, job["title"], job["company"])
                                continue

                            if easy_apply:
                                clicked = await click_easy_apply(page)
                                if not clicked:
                                    log_application(
                                        job["title"], job["company"], job["location"],
                                        job["url"], job_id, "error", "Could not click Easy Apply"
                                    )
                                    stats["errors"] += 1
                                    continue

                                success = await fill_form(page, job["title"], job["company"])
                                if success:
                                    log_application(
                                        job["title"], job["company"], job["location"],
                                        job["url"], job_id, "applied", "easy_apply"
                                    )
                                    applied_set.add(job_id)
                                    stats["applied"] += 1
                                    logger.info("Applied (Easy Apply): %s at %s (%d/%d)",
                                                job["title"], job["company"],
                                                stats["applied"], max_apps)
                                else:
                                    await dismiss_modal(page)
                                    log_application(
                                        job["title"], job["company"], job["location"],
                                        job["url"], job_id, "error", "Form fill/submit failed"
                                    )
                                    stats["errors"] += 1

                            else:  # direct_apply
                                success, notes = await direct_apply_run(page, context, job)
                                if success:
                                    log_application(
                                        job["title"], job["company"], job["location"],
                                        job["url"], job_id, "applied", "direct_apply"
                                    )
                                    applied_set.add(job_id)
                                    stats["applied"] += 1
                                    logger.info("Applied (Direct Apply): %s at %s (%d/%d)",
                                                job["title"], job["company"],
                                                stats["applied"], max_apps)
                                else:
                                    log_application(
                                        job["title"], job["company"], job["location"],
                                        job["url"], job_id, "error", f"Direct apply failed: {notes}"
                                    )
                                    stats["errors"] += 1

                        except Exception as e:
                            logger.error("Error processing %s: %s", job["title"], e)
                            await dismiss_modal(page)
                            log_application(
                                job["title"], job["company"], job["location"],
                                job["url"], job_id, "error", str(e)
                            )
                            stats["errors"] += 1

                        await random_delay(APP_DELAY_MIN, APP_DELAY_MAX)

                    # Try next page
                    if not await next_page(page):
                        logger.info("No more pages for %s | %s", role, location)
                        break
                    page_num += 1

    finally:
        logger.info("=" * 50)
        logger.info("RUN COMPLETE")
        logger.info("  Applied:         %d", stats["applied"])
        logger.info("  Skipped:         %d", stats["skipped"])
        logger.info("  Errors:          %d", stats["errors"])
        logger.info("  Already applied: %d", stats["already_applied"])
        logger.info("=" * 50)
        await context.close()


def main():
    setup_logging()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    acquire_lock()
    args = parse_args()
    try:
        asyncio.run(run(args))
    finally:
        release_lock()


if __name__ == "__main__":
    main()
