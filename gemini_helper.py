import asyncio
import json
import logging
import os
import re

from google import genai
from google.genai import types
from playwright.async_api import Page

from config import APPLICANT

logger = logging.getLogger(__name__)

_client: genai.Client | None = None


def init_gemini():
    global _client
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set in .env")
    _client = genai.Client(api_key=api_key)
    logger.info("Gemini initialised")


def _client_instance() -> genai.Client:
    if _client is None:
        raise RuntimeError("Call init_gemini() first")
    return _client


def _build_profile_text(applicant: dict) -> str:
    return f"""
Applicant profile:
- Name: {applicant['full_name']}
- Email: {applicant['email']}
- Phone: {applicant['phone']}
- Location: {applicant['city']}, {applicant['country']}
- Current title: {applicant['current_title']}
- Years of experience: {applicant['years_of_experience']}
- Years of leadership experience: {applicant.get('years_leadership', '0')}+
- Education: {applicant['education']}
- Languages: {', '.join(applicant['languages'])}
- LinkedIn: {applicant.get('linkedin_url', '')}
- Salary expectation: {applicant.get('salary_expectation', '')} USD/year
- Work authorisation EU: {'Yes' if applicant['work_authorization_eu'] else 'No'}
- Work authorisation US (no sponsorship needed): {'Yes' if not applicant.get('needs_sponsorship_us') else 'No'}
- Needs US visa sponsorship: {'Yes' if applicant.get('needs_sponsorship_us') else 'No'}
- Notice period: {applicant.get('notice_period', '')}
- Professional summary: {applicant['headline']}
""".strip()


def _work_auth_rules(applicant: dict) -> str:
    """Work authorisation rules for the Gemini prompt, built from the profile."""
    eu = "Yes" if applicant.get("work_authorization_eu") else "No"
    lines = [f"- For yes/no questions about EU work authorisation: answer {eu}."]
    if applicant.get("needs_sponsorship_us"):
        lines.append("- For yes/no questions about US work authorisation / visa sponsorship: answer No (needs sponsorship).")
    else:
        lines.append("- For yes/no questions about US work authorisation: answer Yes. For US visa sponsorship questions: answer No (no sponsorship needed).")
    return "\n".join(lines)


async def _screenshot_element(page: Page, selector: str) -> bytes | None:
    """Scroll modal to top, then screenshot the element itself (not just viewport)."""
    try:
        el = page.locator(selector)
        if await el.count() > 0:
            # Scroll the modal content to the top so Gemini sees fields from the beginning
            await el.first.evaluate("el => { el.scrollTop = 0; }")
            return await el.first.screenshot()
    except Exception as e:
        logger.debug("Screenshot failed: %s", e)
    return None


FILL_PROMPT = """\
You are helping automate a job application form on LinkedIn Easy Apply.

{profile}

{step_hint}

Below is a screenshot of the current application form step.
Your job is to return a JSON array of actions to fill in every visible, unfilled field.

Rules:
- Be truthful — use the applicant profile above for all personal data.
{work_auth_rules}
- For salary questions: use {salary}.
- For notice period / availability / start date: use "{notice}".
- For "how did you hear" / source: use "LinkedIn".
- For cover letter / additional info: write a short 2-3 sentence professional pitch using the summary above.
- For any question about experience, skills, qualifications, or requirements you are unsure about: answer "Yes" or select the most positive/affirmative option available.
- For numeric experience questions (e.g. "years of experience with X"): use a reasonable number based on the applicant's {years} years total experience (e.g. 2-4 years for any specific tool/skill).
- For fields you truly cannot answer (e.g. a reference name, portfolio URL): use an empty string "".
- Skip fields that already have a value filled in.
- Do NOT click Submit or Next — only fill fields.

Return ONLY a JSON array, no other text. Each element must be one of:

Fill a text/textarea/number input:
  {{"action": "fill", "label": "<visible label text>", "value": "<value>"}}

Select a dropdown option:
  {{"action": "select", "label": "<visible label text>", "option": "<option text to select>"}}

Click a radio button or checkbox:
  {{"action": "radio", "label": "<fieldset/group label>", "option": "<option label to select>"}}

If there are no fields to fill, return an empty array: []
"""


async def fill_unknown_fields(page: Page, modal_selector: str, profile_text: str, step: int = 1, error_mode: bool = False) -> int:
    """
    Take a screenshot of the modal, ask Gemini what to fill,
    and execute the actions. Returns number of fields filled.
    """
    png = await _screenshot_element(page, modal_selector)
    if not png:
        logger.warning("Gemini: could not screenshot modal")
        return 0

    step_hint = f"You are on step {step} of this multi-step form. Focus only on the fields visible in this screenshot."
    if error_mode:
        step_hint += (
            "\n\nCRITICAL: This form has validation errors — some required fields are empty or invalid. "
            "Fields with red borders or red error text MUST be filled. "
            "Do NOT return []. Find every empty or invalid field and fill it. "
            "For any question you are unsure about, answer Yes or select the most positive/affirmative option."
        )
    prompt = FILL_PROMPT.format(
        profile=profile_text,
        step_hint=step_hint,
        work_auth_rules=_work_auth_rules(APPLICANT),
        salary=APPLICANT.get("salary_expectation", ""),
        notice=APPLICANT.get("notice_period", ""),
        years=APPLICANT.get("years_of_experience", ""),
    )

    raw = None
    for attempt in range(2):
        try:
            client = _client_instance()
            img_part = types.Part.from_bytes(data=png, mime_type="image/png")
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=[prompt, img_part],
            )
            raw = response.text.strip()
            logger.info("Gemini raw response: %s", raw[:800])
            break
        except Exception as e:
            logger.warning("Gemini API error (attempt %d/2): %s", attempt + 1, e)
            if attempt == 0:
                await asyncio.sleep(3)

    if raw is None:
        return 0

    # Extract JSON array — find first '[' to last ']' to handle preamble text
    start = raw.find("[")
    end = raw.rfind("]")
    if start != -1 and end != -1 and end >= start:
        raw = raw[start:end + 1]
    try:
        actions = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Gemini JSON parse error: %s — raw: %s", e, raw[:300])
        return 0

    if not isinstance(actions, list):
        return 0

    filled = 0
    modal = page.locator(modal_selector)

    for action in actions:
        act = action.get("action", "")
        label = action.get("label", "").strip()
        value = str(action.get("value", "")).strip()

        try:
            if act == "fill" and value:
                # Find input by label text
                inp = await _find_input_by_label(page, modal, label)
                if inp and await inp.is_visible():
                    current = (await inp.input_value()).strip()
                    if not current:  # only fill if empty
                        await inp.click()
                        await inp.fill(value)
                        filled += 1
                        logger.info("Gemini filled '%s' → '%s'", label, value[:60])

            elif act == "select":
                option_text = action.get("option", "").strip()
                sel = await _find_select_by_label(page, modal, label)
                if sel and await sel.is_visible() and option_text:
                    try:
                        await sel.select_option(label=option_text)
                        filled += 1
                        logger.info("Gemini selected '%s' → '%s'", label, option_text)
                    except Exception:
                        # Option text didn't match exactly — try partial match
                        opts = await sel.locator("option").all_text_contents()
                        match = next((o for o in opts if option_text.lower() in o.lower()), None)
                        if match:
                            await sel.select_option(label=match)
                            filled += 1
                            logger.info("Gemini selected '%s' → '%s' (partial)", label, match)
                else:
                    # No select found — try as text input fallback
                    fallback_value = value or option_text
                    if fallback_value:
                        inp = await _find_input_by_label(page, modal, label)
                        if inp and await inp.is_visible():
                            current = (await inp.input_value()).strip()
                            if not current:
                                await inp.fill(fallback_value)
                                filled += 1
                                logger.info("Gemini filled (select→text fallback) '%s' → '%s'", label, fallback_value[:60])

            elif act == "radio":
                option_text = action.get("option", "").strip()
                if option_text:
                    radio = await _find_radio_by_labels(page, modal, label, option_text)
                    if radio and await radio.is_visible() and not await radio.is_checked():
                        rid = await radio.get_attribute("id") or ""
                        lbl_el = page.locator(f'label[for="{rid}"]') if rid else None
                        if lbl_el and await lbl_el.count() > 0:
                            await lbl_el.first.click()
                        else:
                            await radio.evaluate("el => el.click()")
                        filled += 1
                        logger.info("Gemini checked radio '%s' → '%s'", label, option_text)

        except Exception as e:
            logger.debug("Gemini action failed (%s '%s'): %s", act, label, e)

    return filled


async def _find_input_by_label(page, modal, label_text: str):
    """Find an input/textarea whose label contains the given text."""
    needle = _clean_label(label_text)
    inputs = modal.locator('input[type="text"], input[type="email"], input[type="tel"], input[type="number"], textarea')
    count = await inputs.count()
    for i in range(count):
        inp = inputs.nth(i)
        aria = _clean_label(await inp.get_attribute("aria-label") or "")
        if needle in aria:
            return inp
        ph = _clean_label(await inp.get_attribute("placeholder") or "")
        if needle in ph:
            return inp
        inp_id = await inp.get_attribute("id") or ""
        if inp_id:
            lbl = page.locator(f'label[for="{inp_id}"]')
            if await lbl.count() > 0:
                lbl_text = _clean_label(await lbl.first.text_content() or "")
                if needle in lbl_text:
                    return inp
    return None


def _normalise(s: str) -> str:
    """Strip punctuation/whitespace for fuzzy label matching."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _clean_label(s: str) -> str:
    """Strip trailing asterisks, colons, and whitespace from field labels."""
    return s.strip().rstrip("*:").strip().lower()


async def _find_select_by_label(page, modal, label_text: str):
    """Find a <select> whose label fuzzy-matches the given text."""
    needle = _normalise(_clean_label(label_text))
    selects = modal.locator("select")
    count = await selects.count()
    for i in range(count):
        sel = selects.nth(i)
        aria = _normalise(await sel.get_attribute("aria-label") or "")
        if needle in aria or aria in needle:
            return sel
        sel_id = await sel.get_attribute("id") or ""
        if sel_id:
            lbl = page.locator(f'label[for="{sel_id}"]')
            if await lbl.count() > 0:
                lbl_text = _normalise(await lbl.first.text_content() or "")
                if needle in lbl_text or lbl_text in needle:
                    return sel
    return None


async def _find_radio_by_labels(page, modal, group_label: str, option_label: str):
    """Find a radio button matching both the fieldset label and option label."""
    group_needle = _clean_label(group_label)
    option_needle = _clean_label(option_label)
    fieldsets = modal.locator("fieldset")
    count = await fieldsets.count()
    for i in range(count):
        fs = fieldsets.nth(i)
        legend = fs.locator("legend, span.fb-dash-form-element__label")
        if await legend.count() > 0:
            legend_text = _clean_label(await legend.first.text_content() or "")
            if group_needle in legend_text or legend_text in group_needle:
                radios = fs.locator('input[type="radio"]')
                rc = await radios.count()
                for j in range(rc):
                    r = radios.nth(j)
                    rid = await r.get_attribute("id") or ""
                    lbl = page.locator(f'label[for="{rid}"]') if rid else None
                    if lbl and await lbl.count() > 0:
                        lbl_text = _clean_label(await lbl.first.text_content() or "")
                        if option_needle in lbl_text:
                            return r
    return None
