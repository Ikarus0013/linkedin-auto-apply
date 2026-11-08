import json
import logging
import re

from playwright.async_api import Page, BrowserContext

from config import APPLICANT
from linkedin_bot import random_delay, human_click
import gemini_helper

logger = logging.getLogger(__name__)

DIRECT_APPLY_BTN = 'button.jobs-apply-button[aria-label*="Apply on"]'
MAX_STEPS = 20

SUCCESS_URL_PATTERNS = [
    "confirmation", "success", "thank", "submitted", "complete",
    "applied", "received", "application-sent",
]

DIRECT_APPLY_PROMPT = """\
You are helping automate filling out a job application on an EXTERNAL company website (not LinkedIn).

{profile}

Below is a screenshot of the current page.

Your job:
1. Classify the page_type as one of:
   - "form": a job application form with fillable fields or navigation buttons
   - "captcha": a CAPTCHA verification is blocking progress
   - "account_required": requires creating an account or signing in to continue
   - "success": application was submitted (thank you / confirmation page)
   - "unknown": cannot determine what to do

2. If page_type is "form", return all field actions to fill visible, empty fields:
   - Use applicant profile data for personal info
   - Salary: __SALARY__
   - Notice period / availability / start date: "__NOTICE__"
   - Work authorization EU: __EU_AUTH__
   - US visa sponsorship: __US_SPONSORSHIP__
   - Source / how did you hear: "LinkedIn"
   - Cover letter: write a 2-3 sentence professional pitch using the summary
   - Skip already-filled fields
   - Do NOT include actions for file upload fields (handled separately)

3. Identify the button to click AFTER filling fields:
   - Final submit/apply button → type "submit"
   - Next / Continue / Proceed → type "next"
   - No visible navigation button → type "wait"
   - Page is success → type "done"
   - Cannot proceed (captcha/account required/unknown after attempts) → type "give_up"

Return ONLY valid JSON (no markdown fences):
{
  "page_type": "form" | "captcha" | "account_required" | "success" | "unknown",
  "fields": [
    {"action": "fill", "label": "<label>", "value": "<value>"},
    {"action": "select", "label": "<label>", "option": "<option text>"},
    {"action": "radio", "label": "<group label>", "option": "<option label>"},
    {"action": "checkbox", "label": "<label>", "check": true}
  ],
  "next_action": {
    "type": "submit" | "next" | "wait" | "done" | "give_up",
    "button_text": "<approximate visible text on the button>",
    "reason": "<only for give_up>"
  }
}
"""


async def has_direct_apply(page: Page) -> bool:
    btn = page.locator(DIRECT_APPLY_BTN)
    return await btn.count() > 0


async def run(linkedin_page: Page, context: BrowserContext, job: dict) -> tuple[bool, str]:
    """
    Click 'Apply on company website', handle the new tab,
    and attempt to fill the external form using Gemini.
    Returns (success, notes).
    """
    profile_text = gemini_helper._build_profile_text(APPLICANT)
    resume_path = APPLICANT["resume_path"]

    btn = linkedin_page.locator(DIRECT_APPLY_BTN)
    if await btn.count() == 0:
        return False, "Direct apply button not found"

    # Click and wait for the new tab to open
    async with context.expect_page() as new_page_info:
        try:
            await human_click(linkedin_page, btn.first)
        except Exception as e:
            return False, f"Could not click direct apply button: {e}"

    try:
        ext_page = await new_page_info.value
        await ext_page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception as e:
        return False, f"New tab did not open or load: {e}"

    logger.info("Direct apply: opened %s for %s at %s",
                ext_page.url, job.get("title", ""), job.get("company", ""))

    try:
        success, notes = await _run_apply_loop(ext_page, profile_text, resume_path)
    finally:
        try:
            await ext_page.close()
        except Exception:
            pass

    return success, notes


async def _run_apply_loop(
    page: Page,
    profile_text: str,
    resume_path: str,
) -> tuple[bool, str]:
    """Gemini-driven loop on the external application page."""
    prev_url = ""
    no_progress_count = 0

    for step in range(MAX_STEPS):
        await random_delay(2, 3)
        current_url = page.url
        logger.info("Direct apply step %d — %s", step + 1, current_url)

        # Passive success check via URL
        if _is_success_url(current_url):
            logger.info("Direct apply: success detected via URL")
            return True, "Applied via company website"

        # Screenshot the viewport
        try:
            png = await page.screenshot(full_page=False)
        except Exception as e:
            return False, f"Screenshot failed: {e}"

        # Ask Gemini
        result = await _ask_gemini(png, profile_text, step)
        if result is None:
            return False, "Gemini failed to respond"

        page_type = result.get("page_type", "unknown")
        fields = result.get("fields", [])
        next_action = result.get("next_action", {})
        next_type = next_action.get("type", "wait")
        button_text = next_action.get("button_text", "")

        logger.info("Gemini: page_type=%s, fields=%d, next=%s (%s)",
                    page_type, len(fields), next_type, button_text)

        # Terminal states
        if page_type == "success" or next_type == "done":
            return True, "Applied via company website"

        if next_type == "give_up":
            reason = next_action.get("reason", page_type)
            return False, f"Give up: {reason}"

        if page_type in ("captcha", "account_required"):
            return False, f"Blocked: {page_type}"

        # Fill fields
        if fields:
            await _execute_fields(page, fields)

        # Upload resume if a file input is present
        await _try_upload_resume(page, resume_path)

        # Navigate
        if next_type in ("submit", "next") and button_text:
            clicked = await _click_button_by_text(page, button_text)
            if not clicked:
                clicked = await _click_nav_fallback(page, next_type)

            if clicked:
                await random_delay(2, 4)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                except Exception:
                    pass

                if _is_success_url(page.url) or await _check_success_text(page):
                    return True, "Applied via company website"

                if page.url == prev_url:
                    no_progress_count += 1
                    if no_progress_count >= 3:
                        return False, "Stuck: no page progress"
                else:
                    no_progress_count = 0
                    prev_url = page.url
            else:
                no_progress_count += 1
                if no_progress_count >= 3:
                    return False, "Could not find navigation button"

        elif next_type == "wait":
            no_progress_count += 1
            if no_progress_count >= 2:
                return False, "No navigation button found"

    return False, "Exceeded max steps"


async def _ask_gemini(png: bytes, profile_text: str, step: int) -> dict | None:
    from google.genai import types

    prompt = (
        DIRECT_APPLY_PROMPT
        .replace("__SALARY__", str(APPLICANT.get("salary_expectation", "")))
        .replace("__NOTICE__", str(APPLICANT.get("notice_period", "")))
        .replace("__EU_AUTH__", "Yes" if APPLICANT.get("work_authorization_eu") else "No")
        .replace("__US_SPONSORSHIP__",
                 "Yes (needs sponsorship)" if APPLICANT.get("needs_sponsorship_us")
                 else "No (no sponsorship needed)")
        .format(profile=profile_text)
    )
    try:
        client = gemini_helper._client_instance()
        img_part = types.Part.from_bytes(data=png, mime_type="image/png")
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[prompt, img_part],
        )
        raw = response.text.strip()
        logger.debug("Gemini direct apply raw (step %d): %s", step + 1, raw[:500])
    except Exception as e:
        logger.warning("Gemini API error on step %d: %s", step + 1, e)
        return None

    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Gemini JSON parse error: %s — raw: %s", e, raw[:300])
        return None


async def _execute_fields(page: Page, fields: list[dict]):
    for action in fields:
        act = action.get("action", "")
        label = action.get("label", "").strip()
        value = str(action.get("value", "")).strip()

        try:
            if act == "fill" and value:
                inp = await _find_input_by_label(page, label)
                if inp and await inp.is_visible():
                    current = (await inp.input_value()).strip()
                    if not current:
                        await inp.click()
                        await inp.fill(value)
                        logger.info("Direct fill '%s' → '%s'", label, value[:60])
                        await random_delay(0.2, 0.5)

            elif act == "select":
                option_text = action.get("option", "").strip()
                sel = await _find_select_by_label(page, label)
                if sel and await sel.is_visible() and option_text:
                    try:
                        await sel.select_option(label=option_text)
                        logger.info("Direct select '%s' → '%s'", label, option_text)
                    except Exception:
                        opts = await sel.locator("option").all_text_contents()
                        match = next((o for o in opts if option_text.lower() in o.lower()), None)
                        if match:
                            await sel.select_option(label=match)
                            logger.info("Direct select '%s' → '%s' (partial)", label, match)

            elif act == "radio":
                option_text = action.get("option", "").strip()
                if option_text:
                    radio = await _find_radio_by_labels(page, label, option_text)
                    if radio and await radio.is_visible() and not await radio.is_checked():
                        await radio.check()
                        logger.info("Direct radio '%s' → '%s'", label, option_text)

            elif act == "checkbox":
                should_check = action.get("check", True)
                cb = await _find_checkbox_by_label(page, label)
                if cb and await cb.is_visible():
                    checked = await cb.is_checked()
                    if should_check and not checked:
                        await cb.check()
                        logger.info("Direct checkbox '%s' → checked", label)
                    elif not should_check and checked:
                        await cb.uncheck()

        except Exception as e:
            logger.debug("Direct apply field action failed (%s '%s'): %s", act, label, e)


async def _try_upload_resume(page: Page, resume_path: str):
    file_input = page.locator('input[type="file"]')
    if await file_input.count() == 0:
        return
    try:
        await file_input.first.set_input_files(resume_path)
        logger.info("Direct apply: uploaded resume")
        await random_delay(1, 2)
    except Exception as e:
        logger.debug("Direct apply: resume upload skipped: %s", e)


async def _click_button_by_text(page: Page, button_text: str) -> bool:
    buttons = page.locator("button, input[type='submit'], a[role='button']")
    count = await buttons.count()
    needle = button_text.lower().strip()

    # First pass: substring match
    for i in range(count):
        btn = buttons.nth(i)
        if not await btn.is_visible():
            continue
        text = (await btn.text_content() or await btn.get_attribute("value") or "").strip().lower()
        if needle in text or text in needle:
            await human_click(page, btn)
            return True

    # Second pass: any word in common
    needle_words = set(needle.split()) - {"the", "a", "an", "your"}
    if needle_words:
        for i in range(count):
            btn = buttons.nth(i)
            if not await btn.is_visible():
                continue
            text = (await btn.text_content() or "").strip().lower()
            if needle_words & set(text.split()):
                await human_click(page, btn)
                return True

    return False


async def _click_nav_fallback(page: Page, action_type: str) -> bool:
    if action_type == "submit":
        selectors = [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Submit")',
            'button:has-text("Apply")',
            'button:has-text("Send application")',
        ]
    else:
        selectors = [
            'button:has-text("Next")',
            'button:has-text("Continue")',
            'button:has-text("Proceed")',
        ]
    for sel in selectors:
        btns = page.locator(sel)
        if await btns.count() > 0 and await btns.first.is_visible():
            await human_click(page, btns.first)
            return True
    return False


def _is_success_url(url: str) -> bool:
    url_lower = url.lower()
    return any(p in url_lower for p in SUCCESS_URL_PATTERNS)


async def _check_success_text(page: Page) -> bool:
    try:
        loc = page.locator(
            'h1:has-text("Thank"), h2:has-text("Thank"), h3:has-text("Thank"), '
            ':has-text("application has been submitted"), '
            ':has-text("application received"), '
            ':has-text("successfully applied"), '
            ':has-text("successfully submitted")'
        )
        return await loc.count() > 0
    except Exception:
        return False


async def _find_input_by_label(page: Page, label_text: str):
    label_lower = label_text.lower()
    inputs = page.locator(
        'input[type="text"], input[type="email"], input[type="tel"], '
        'input[type="number"], input[type="url"], textarea'
    )
    count = await inputs.count()
    for i in range(count):
        inp = inputs.nth(i)
        aria = (await inp.get_attribute("aria-label") or "").lower()
        if label_lower in aria:
            return inp
        ph = (await inp.get_attribute("placeholder") or "").lower()
        if label_lower in ph:
            return inp
        inp_id = await inp.get_attribute("id") or ""
        if inp_id:
            lbl = page.locator(f'label[for="{inp_id}"]')
            if await lbl.count() > 0:
                lbl_text = (await lbl.first.text_content() or "").lower()
                if label_lower in lbl_text:
                    return inp
        name = (await inp.get_attribute("name") or "").lower()
        if label_lower in name:
            return inp
    return None


async def _find_select_by_label(page: Page, label_text: str):
    needle = re.sub(r"[^a-z0-9]", "", label_text.lower())
    selects = page.locator("select")
    count = await selects.count()
    for i in range(count):
        sel = selects.nth(i)
        aria = re.sub(r"[^a-z0-9]", "", (await sel.get_attribute("aria-label") or "").lower())
        if needle in aria or aria in needle:
            return sel
        sel_id = await sel.get_attribute("id") or ""
        if sel_id:
            lbl = page.locator(f'label[for="{sel_id}"]')
            if await lbl.count() > 0:
                lbl_text = re.sub(r"[^a-z0-9]", "", (await lbl.first.text_content() or "").lower())
                if needle in lbl_text or lbl_text in needle:
                    return sel
    return None


async def _find_radio_by_labels(page: Page, group_label: str, option_label: str):
    group_lower = group_label.lower()
    option_lower = option_label.lower()

    # Try fieldset/legend grouping first
    fieldsets = page.locator("fieldset")
    count = await fieldsets.count()
    for i in range(count):
        fs = fieldsets.nth(i)
        legend = fs.locator("legend")
        if await legend.count() > 0:
            legend_text = (await legend.first.text_content() or "").lower()
            if group_lower in legend_text or legend_text in group_lower:
                radios = fs.locator('input[type="radio"]')
                rc = await radios.count()
                for j in range(rc):
                    r = radios.nth(j)
                    rid = await r.get_attribute("id") or ""
                    if rid:
                        lbl = page.locator(f'label[for="{rid}"]')
                        if await lbl.count() > 0:
                            lbl_text = (await lbl.first.text_content() or "").lower()
                            if option_lower in lbl_text:
                                return r

    # Fallback: scan all radio buttons
    radios = page.locator('input[type="radio"]')
    rc = await radios.count()
    for j in range(rc):
        r = radios.nth(j)
        rid = await r.get_attribute("id") or ""
        if rid:
            lbl = page.locator(f'label[for="{rid}"]')
            if await lbl.count() > 0:
                lbl_text = (await lbl.first.text_content() or "").lower()
                if option_lower in lbl_text:
                    return r
    return None


async def _find_checkbox_by_label(page: Page, label_text: str):
    label_lower = label_text.lower()
    cbs = page.locator('input[type="checkbox"]')
    count = await cbs.count()
    for i in range(count):
        cb = cbs.nth(i)
        aria = (await cb.get_attribute("aria-label") or "").lower()
        if label_lower in aria:
            return cb
        cid = await cb.get_attribute("id") or ""
        if cid:
            lbl = page.locator(f'label[for="{cid}"]')
            if await lbl.count() > 0:
                lbl_text = (await lbl.first.text_content() or "").lower()
                if label_lower in lbl_text:
                    return cb
    return None
