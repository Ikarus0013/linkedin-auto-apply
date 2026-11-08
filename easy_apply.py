import asyncio
import logging
import random

from playwright.async_api import Page

from pathlib import Path

from config import (
    APPLICANT, ACTION_DELAY_MIN, ACTION_DELAY_MAX, DATA_DIR,
    DEFAULT_ANSWERS, PHONE_COUNTRY_CODE_MATCH, EDUCATION_DROPDOWN_MATCH,
)
from linkedin_bot import random_delay, human_click
import gemini_helper

logger = logging.getLogger(__name__)

ANSWERS_LOG = DATA_DIR / "invented_answers.csv"

def _log_answer(job_title: str, company: str, field_label: str, value: str, source: str):
    """Append a filled answer to the answers log for transparency."""
    ANSWERS_LOG.parent.mkdir(parents=True, exist_ok=True)
    write_header = not ANSWERS_LOG.exists()
    with open(ANSWERS_LOG, "a", encoding="utf-8") as f:
        if write_header:
            f.write("timestamp,job_title,company,field_label,value,source\n")
        import datetime
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Escape commas in values
        def esc(s): return f'"{s}"' if "," in str(s) else str(s)
        f.write(f"{ts},{esc(job_title)},{esc(company)},{esc(field_label)},{esc(value)},{source}\n")

# Current job context for logging (set by fill_form)
_current_job: dict = {"title": "", "company": ""}

# LinkedIn's Easy Apply form modal selector
M = ".jobs-easy-apply-modal"

# Mapping: if a label contains any of these keywords, fill with the corresponding value
FIELD_MAP = {
    # Personal info
    "first name": APPLICANT["first_name"],
    "last name": APPLICANT["last_name"],
    "full name": APPLICANT["full_name"],
    "email": APPLICANT["email"],
    "phone": APPLICANT["phone"],
    "mobile": APPLICANT["phone"],
    "city": APPLICANT["city"],
    "country": APPLICANT["country"],
    "location": APPLICANT["city"],
    # Experience
    "years of experience": APPLICANT["years_of_experience"],
    "years experience": APPLICANT["years_of_experience"],
    "how many years": APPLICANT["years_of_experience"],
    # Profile
    "headline": APPLICANT["headline"],
    "summary": APPLICANT["headline"],
    "cover letter": APPLICANT["headline"],
    "current title": APPLICANT["current_title"],
    "current role": APPLICANT["current_title"],
    "job title": APPLICANT["current_title"],
    "linkedin": APPLICANT["linkedin_url"],
    # Salary
    "salary": APPLICANT["salary_expectation"],
    "compensation": APPLICANT["salary_expectation"],
    "expected salary": APPLICANT["salary_expectation"],
    "desired salary": APPLICANT["salary_expectation"],
    "salary expectation": APPLICANT["salary_expectation"],
    "salary requirement": APPLICANT["salary_expectation"],
    "minimum salary": APPLICANT["salary_expectation"],
    # Defaults that are not direct profile facts — every filled value is logged
    # to data/invented_answers.csv so you can review it
    "notice period": APPLICANT.get("notice_period", ""),
    "notice": APPLICANT.get("notice_period", ""),
    "availability": APPLICANT.get("notice_period", ""),
    "available to start": APPLICANT.get("notice_period", ""),
    "start date": APPLICANT.get("notice_period", ""),
    "website": APPLICANT.get("website", ""),
    "portfolio": APPLICANT.get("website", ""),
    "github": APPLICANT.get("github_url", ""),
    # Answers from profile.json "default_answers" (EEO questions, source, ...)
    **DEFAULT_ANSWERS,
}

# Keywords in labels for yes/no radio or select questions
YES_KEYWORDS = [
    "authorized",
    "authorised",
    "legally authorized",
    "right to work",
    "work authorization",
    "eligible to work",
    "legally eligible",
    "do you have",
    "are you willing",
    "willing to relocate",
    "commute",
    "consent",
    "agree",
    "acknowledge",
    "background check",
    "drug test",
]

NO_KEYWORDS = [
    "sponsorship",
    "require sponsorship",
    "need sponsorship",
    "visa sponsorship",
]


async def has_easy_apply(page: Page) -> bool:
    # Require BOTH: jobs-apply-button class (job panel button) AND "Easy Apply" in aria-label.
    # External apply buttons have the same class but say "Apply on company website".
    # The search filter pill has "Easy Apply" in aria-label but not jobs-apply-button class.
    btn = page.locator('button.jobs-apply-button[aria-label*="Easy Apply"]')
    return await btn.count() > 0


async def click_easy_apply(page: Page) -> bool:
    btn = page.locator('button.jobs-apply-button[aria-label*="Easy Apply"]')
    if await btn.count() == 0:
        return False
    try:
        await human_click(page, btn.first)
        # Wait for the modal to actually appear (up to 8s)
        try:
            await page.locator(M).wait_for(state="visible", timeout=8_000)
        except Exception:
            logger.warning("Easy Apply clicked but modal did not appear")
            return False
        return True
    except Exception as e:
        logger.warning("Failed to click Easy Apply: %s", e)
        return False


def _clean_label(s: str) -> str:
    """Strip trailing asterisks, colons, and whitespace from field labels."""
    return s.strip().rstrip("*:").strip().lower()


async def _get_label_for_input(page: Page, input_el) -> str:
    # Try aria-label
    aria = await input_el.get_attribute("aria-label") or ""
    if aria.strip():
        return _clean_label(aria)

    # Try associated <label>
    input_id = await input_el.get_attribute("id") or ""
    if input_id:
        label_el = page.locator(f'label[for="{input_id}"]')
        if await label_el.count() > 0:
            return _clean_label((await label_el.text_content()) or "")

    # Try placeholder
    placeholder = await input_el.get_attribute("placeholder") or ""
    if placeholder.strip():
        return _clean_label(placeholder)

    # Try closest fieldset legend or parent label text
    parent = input_el.locator("xpath=ancestor::div[contains(@class,'fb-dash-form-element')]")
    if await parent.count() > 0:
        label_span = parent.locator("label, legend, span.fb-dash-form-element__label")
        if await label_span.count() > 0:
            return _clean_label((await label_span.first.text_content()) or "")

    return ""


def _match_field_value(label: str) -> str | None:
    for keyword, value in FIELD_MAP.items():
        if keyword in label:
            return value
    return None


def _match_yes_no(label: str) -> str | None:
    for kw in YES_KEYWORDS:
        if kw in label:
            return "yes"
    for kw in NO_KEYWORDS:
        if kw in label:
            return "no"
    return None


async def _fill_text_inputs(page: Page) -> int:
    filled = 0
    inputs = page.locator(
        f'{M} input[type="text"], '
        f'{M} input[type="email"], '
        f'{M} input[type="tel"], '
        f'{M} input[type="number"], '
        f'{M} textarea'
    )
    count = await inputs.count()

    for i in range(count):
        inp = inputs.nth(i)
        if not await inp.is_visible():
            continue

        label = await _get_label_for_input(page, inp)
        value = _match_field_value(label)

        # Skip already-filled fields, EXCEPT email — always enforce our email
        current_val = (await inp.input_value()).strip()
        if current_val and "email" not in label:
            continue

        if value:
            await inp.click()
            await random_delay(0.2, 0.5)
            await inp.fill(value)
            filled += 1
            # Determine if this is a known field or an invented default
            known_keys = {"first name", "last name", "full name", "email", "phone",
                          "mobile", "city", "country", "location", "years of experience",
                          "years experience", "how many years", "headline", "summary",
                          "current title", "current role", "job title", "linkedin",
                          "salary", "compensation", "expected salary", "desired salary",
                          "salary expectation", "salary requirement", "minimum salary"}
            source = "config" if any(k in label for k in known_keys) else "invented"
            logger.debug("Filled '%s' → %s [%s]", label, value, source)
            _log_answer(_current_job["title"], _current_job["company"], label, value, source)
            await random_delay(0.3, 0.8)

    return filled


async def _fill_dropdowns(page: Page) -> int:
    filled = 0
    selects = page.locator(f"{M} select")
    count = await selects.count()

    for i in range(count):
        sel = selects.nth(i)
        if not await sel.is_visible():
            continue

        label = await _get_label_for_input(page, sel)

        # Try to pick the best option
        options = sel.locator("option")
        opt_count = await options.count()
        option_texts = []
        for j in range(opt_count):
            text = ((await options.nth(j).text_content()) or "").strip()
            option_texts.append(text)

        # Check FIELD_MAP first (email, phone, name, etc. can appear as selects)
        field_value = _match_field_value(label)
        if field_value:
            for j, text in enumerate(option_texts):
                if field_value.lower() in text.lower():
                    await sel.select_option(index=j)
                    filled += 1
                    logger.debug("Select FIELD_MAP '%s' → %s", label, text)
                    break
            continue

        # Check yes/no type
        yn = _match_yes_no(label)
        if yn:
            for j, text in enumerate(option_texts):
                if yn.lower() in text.lower():
                    await sel.select_option(index=j)
                    filled += 1
                    logger.debug("Selected '%s' → %s", label, text)
                    break
            continue

        # Phone country code
        if any(k in label for k in ("country code", "landesvorwahl", "phone code", "dialing code")):
            for j, text in enumerate(option_texts):
                if any(m in text.lower() for m in PHONE_COUNTRY_CODE_MATCH):
                    await sel.select_option(index=j)
                    filled += 1
                    logger.debug("Selected country code → %s", text)
                    break
            continue

        # Experience-related: pick matching number or highest
        if "experience" in label or "years" in label:
            target = int(APPLICANT["years_of_experience"])
            for j, text in enumerate(option_texts):
                if str(target) in text:
                    await sel.select_option(index=j)
                    filled += 1
                    break
            continue

        # Education
        if "education" in label or "degree" in label:
            for j, text in enumerate(option_texts):
                if EDUCATION_DROPDOWN_MATCH in text.lower():
                    await sel.select_option(index=j)
                    filled += 1
                    break
            continue

        # Default: select first non-empty option
        if opt_count > 1:
            for j, text in enumerate(option_texts):
                if text and text.lower() not in ("select an option", "-- select --", ""):
                    await sel.select_option(index=j)
                    filled += 1
                    logger.debug("Default-selected '%s' → %s", label, text)
                    break

    return filled


async def _fill_radio_buttons(page: Page) -> int:
    filled = 0
    fieldsets = page.locator(f"{M} fieldset")
    count = await fieldsets.count()

    for i in range(count):
        fieldset = fieldsets.nth(i)
        legend = fieldset.locator("legend, span.fb-dash-form-element__label")
        label = ""
        if await legend.count() > 0:
            label = ((await legend.first.text_content()) or "").strip().lower()

        yn = _match_yes_no(label)
        if not yn:
            continue

        radios = fieldset.locator('input[type="radio"]')
        radio_count = await radios.count()

        for j in range(radio_count):
            radio = radios.nth(j)
            radio_id = await radio.get_attribute("id") or ""
            # Prefer explicit label[for=id] — most reliable
            if radio_id:
                radio_label_el = page.locator(f'label[for="{radio_id}"]')
            else:
                radio_label_el = radio.locator("xpath=following-sibling::label | ancestor::label")
            if await radio_label_el.count() == 0:
                radio_label_el = radio.locator("xpath=ancestor::label")

            if await radio_label_el.count() > 0:
                radio_text = ((await radio_label_el.first.text_content()) or "").strip().lower()
                if yn in radio_text:
                    # Click the label — it overlays the input and intercepts direct .check()
                    try:
                        await radio_label_el.first.click()
                    except Exception:
                        await radio.evaluate("el => el.click()")
                    filled += 1
                    logger.debug("Radio '%s' → %s", label, radio_text)
                    break

    return filled


async def _upload_resume(page: Page, already_uploaded: bool) -> bool:
    """Upload resume only if not already uploaded this application."""
    if already_uploaded:
        return False
    # Prefer the specific resume input; fall back to first file input
    file_input = page.locator(f'{M} input[type="file"][id*="resume"]')
    if await file_input.count() == 0:
        file_input = page.locator(f'{M} input[type="file"]').first
    if await file_input.count() == 0:
        return False
    resume_path = APPLICANT["resume_path"]
    try:
        await file_input.set_input_files(resume_path)
        logger.info("Uploaded resume: %s", resume_path)
        await random_delay(1, 2)
        return True
    except Exception as e:
        logger.warning("Resume upload failed: %s", e)
        return False


async def _log_error_fields(page: Page):
    """Log the labels of fields currently showing validation errors."""
    errors = page.locator(f'{M} .artdeco-inline-feedback--error, {M} [data-test-form-element-error]')
    count = await errors.count()
    if count == 0:
        return
    msgs = []
    for i in range(count):
        text = ((await errors.nth(i).text_content()) or "").strip()
        if text:
            msgs.append(text)
    if msgs:
        logger.warning("Error fields: %s", " | ".join(msgs[:10]))


async def _check_for_errors(page: Page) -> bool:
    error_msgs = page.locator(
        f'{M} .artdeco-inline-feedback--error, '
        f'{M} [data-test-form-element-error]'
    )
    return await error_msgs.count() > 0


async def fill_form(page: Page, job_title: str = "", company: str = "") -> bool:
    _current_job["title"] = job_title
    _current_job["company"] = company
    max_steps = 25  # safety limit for multi-step forms
    resume_uploaded = False

    for step in range(max_steps):
        logger.debug("Form step %d", step + 1)
        await random_delay(1, 2)

        # Fill all field types with hardcoded logic first
        await _fill_text_inputs(page)
        await _fill_dropdowns(page)
        await _fill_radio_buttons(page)
        if await _upload_resume(page, resume_uploaded):
            resume_uploaded = True

        # Gemini fallback: fill any fields the hardcoded logic missed
        try:
            gemini_filled = await gemini_helper.fill_unknown_fields(
                page, M, gemini_helper._build_profile_text(APPLICANT), step=step + 1
            )
            if gemini_filled:
                logger.info("Gemini filled %d additional field(s) on step %d", gemini_filled, step + 1)
        except Exception as e:
            logger.debug("Gemini fill skipped: %s", e)

        # Check for unfilled required fields (errors)
        if await _check_for_errors(page):
            logger.warning("Form has validation errors on step %d — attempting to continue", step + 1)

        # Look for Next, Review, or Submit button — scoped to modal first, then page-wide
        submit_btn = page.locator(
            f'{M} button[aria-label="Submit application"], '
            f'{M} button[aria-label="Review your application"], '
            f'{M} button:has-text("Submit application"), '
            f'{M} button:has-text("Review your application"), '
            'button[aria-label="Submit application"], '
            'button[aria-label="Review your application"]'
        )
        next_btn = page.locator(
            f'{M} button[aria-label="Continue to next step"], '
            f'{M} button:has-text("Next"), '
            'button[aria-label="Continue to next step"]'
        )
        # Fallback: any visible primary button in the modal footer or page footer area
        footer_primary_btn = page.locator(
            f'{M} footer button.artdeco-button--primary, '
            f'{M} .ph5 button.artdeco-button--primary, '
            'footer button.artdeco-button--primary'
        )

        if await submit_btn.count() > 0:
            # Final step — submit
            return await submit_application(page)

        if await next_btn.count() > 0:
            await human_click(page, next_btn.first)
            await random_delay(1, 2)

            # If errors remain, retry Gemini up to 2 more times in error mode
            if await _check_for_errors(page):
                advanced = False
                for _retry in range(2):
                    logger.info("Validation errors on step %d — Gemini error-retry %d/2", step + 1, _retry + 1)
                    await _log_error_fields(page)
                    await gemini_helper.fill_unknown_fields(
                        page, M, gemini_helper._build_profile_text(APPLICANT),
                        step=step + 1, error_mode=True
                    )
                    retry_next = page.locator(
                        f'{M} button[aria-label="Continue to next step"], '
                        f'{M} button:has-text("Next"), '
                        'button[aria-label="Continue to next step"]'
                    )
                    if await retry_next.count() > 0:
                        await human_click(page, retry_next.first)
                        await random_delay(1, 2)
                        if not await _check_for_errors(page):
                            advanced = True
                            break
                if not advanced:
                    logger.warning("Cannot advance past step %d — required fields unfilled", step + 1)
                    return False
            continue

        # Fallback: click the primary button in the modal footer
        if await footer_primary_btn.count() > 0:
            btn = footer_primary_btn.first
            btn_text = (await btn.text_content() or "").strip()
            logger.info("Fallback footer button on step %d: '%s'", step + 1, btn_text)
            if any(kw in btn_text.lower() for kw in ("submit", "review")):
                return await submit_application(page)
            await human_click(page, btn)
            await random_delay(1, 2)
            # Check if the application was submitted (works for any language)
            success_check = page.locator(
                'h3:has-text("application was sent"), '
                'h2:has-text("application was sent"), '
                'h3:has-text("Application sent")'
            )
            if await success_check.count() > 0:
                logger.info("Application submitted successfully (non-English form)")
                dismiss_btn = page.locator('button[aria-label="Dismiss"]')
                for i in range(await dismiss_btn.count()):
                    d = dismiss_btn.nth(i)
                    if await d.is_visible():
                        await d.click()
                        break
                return True
            if await _check_for_errors(page):
                logger.warning("Cannot advance past step %d — required fields unfilled", step + 1)
                return False
            continue

        # Log all buttons visible on page for debugging
        all_btns = page.locator('button')
        btn_count = await all_btns.count()
        btn_info = []
        for i in range(btn_count):
            b = all_btns.nth(i)
            if not await b.is_visible():
                continue
            label = await b.get_attribute("aria-label") or ""
            text = (await b.text_content() or "").strip()[:40]
            cls = (await b.get_attribute("class") or "")[:50]
            btn_info.append(f"label='{label}' text='{text}' class='{cls}'")
        logger.warning("No Next/Submit on step %d. Modal count=%d. Visible buttons: %s",
                       step + 1, await page.locator(M).count(), btn_info)
        return False

    logger.warning("Exceeded max form steps (%d)", max_steps)
    return False


async def submit_application(page: Page) -> bool:
    try:
        # Click "Review" if present
        review_btn = page.locator(
            f'{M} button[aria-label="Review your application"], '
            f'{M} button:has-text("Review your application")'
        )
        if await review_btn.count() > 0:
            await human_click(page, review_btn.first)
            await random_delay(2, 3)  # give the review page time to load

        # Find "Submit" button — try aria-label, text, then any primary footer button
        submit_btn = page.locator(
            f'{M} button[aria-label="Submit application"], '
            f'{M} button:has-text("Submit application")'
        )
        if await submit_btn.count() == 0:
            # Fallback: the only remaining primary button in the modal footer is Submit
            submit_btn = page.locator(
                f'{M} footer button.artdeco-button--primary, '
                f'{M} .ph5 button.artdeco-button--primary'
            )

        if await submit_btn.count() > 0:
            await human_click(page, submit_btn.first)

            # Wait for the actual success confirmation text (up to 10s)
            success_locator = page.locator(
                'h3:has-text("application was sent"), '
                'h2:has-text("application was sent"), '
                'h3:has-text("Application sent"), '
                '[data-test-modal-id="post-apply-modal"]'
            )
            try:
                await success_locator.first.wait_for(state="visible", timeout=10_000)
                logger.info("Application submitted successfully")
                dismiss_btn = page.locator('button[aria-label="Dismiss"]')
                for i in range(await dismiss_btn.count()):
                    btn = dismiss_btn.nth(i)
                    if await btn.is_visible():
                        await btn.click()
                        break
                return True
            except Exception:
                logger.warning("Submit clicked but no success confirmation detected")
                return False

        logger.warning("Submit button not found")
        return False

    except Exception as e:
        logger.error("Error during submission: %s", e)
        return False


async def dismiss_modal(page: Page):
    try:
        dismiss = page.locator(
            '.artdeco-modal__dismiss, '
            'button[aria-label="Dismiss"], '
            'button[aria-label="Discard"]'
        )
        count = await dismiss.count()
        for i in range(count):
            btn = dismiss.nth(i)
            if await btn.is_visible():
                await btn.click()
                await random_delay(0.5, 1)
                break

        # Handle "Discard" confirmation if it appears
        discard_confirm = page.locator('button[data-control-name="discard_application_confirm_btn"]')
        if await discard_confirm.count() > 0 and await discard_confirm.first.is_visible():
            await discard_confirm.first.click()
            await random_delay(0.5, 1)
    except Exception as e:
        logger.warning("dismiss_modal failed (non-fatal): %s", e)
