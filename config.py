import json
import sys
from pathlib import Path

# --- Paths ---

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CSV_PATH = DATA_DIR / "applications.csv"
LOG_PATH = DATA_DIR / "run.log"
BROWSER_STATE_DIR = DATA_DIR / "browser_state"
PROFILE_PATH = BASE_DIR / "profile.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

# --- Candidate profile (personal data lives in the gitignored profile.json) ---


def _load_profile() -> dict:
    if not PROFILE_PATH.exists():
        sys.exit(
            f"ERROR: {PROFILE_PATH.name} not found. "
            "Copy profile.example.json to profile.json and fill in your data."
        )
    try:
        with open(PROFILE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: {PROFILE_PATH.name} is not valid JSON: {e}")


PROFILE = _load_profile()

# --- Job Search Configuration ---

JOB_ROLES = PROFILE.get("job_roles", [])
LOCATIONS = PROFILE.get("locations", ["Remote"])

# --- Personal / Form Defaults ---

APPLICANT = dict(PROFILE["applicant"])
_resume = Path(APPLICANT.get("resume_path", "")).expanduser()
if APPLICANT.get("resume_path") and not _resume.is_absolute():
    _resume = BASE_DIR / _resume
APPLICANT["resume_path"] = str(_resume) if APPLICANT.get("resume_path") else ""

# Answers for common form questions that are not in the applicant block
# (EEO questions, "how did you hear about us", ...). Order matters: first match wins.
DEFAULT_ANSWERS = PROFILE.get("default_answers", {})

# Dropdown option text to look for in phone country code fields
PHONE_COUNTRY_CODE_MATCH = [s.lower() for s in PROFILE.get("phone_country_code_match", [])]

# Dropdown option text to look for in education / degree fields
EDUCATION_DROPDOWN_MATCH = PROFILE.get("education_dropdown_match", "bachelor").lower()

# Jobs that permanently fail regardless of retries — skipped every run
BLOCKLISTED_JOB_IDS = set(str(j) for j in PROFILE.get("blocklisted_job_ids", []))

# --- Timing ---

ACTION_DELAY_MIN = 2       # seconds between actions (clicks, typing)
ACTION_DELAY_MAX = 5
APP_DELAY_MIN = 20          # seconds between applications
APP_DELAY_MAX = 40

# --- Limits ---

MAX_APPLICATIONS_PER_RUN = 50
