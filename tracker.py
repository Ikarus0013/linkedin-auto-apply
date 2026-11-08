import csv
import logging
from datetime import datetime

from config import CSV_PATH, DATA_DIR, BLOCKLISTED_JOB_IDS

logger = logging.getLogger(__name__)

CSV_COLUMNS = [
    "date",
    "job_title",
    "company",
    "location",
    "url",
    "job_id",
    "status",
    "notes",
]


def _ensure_csv():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CSV_PATH.exists():
        with open(CSV_PATH, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_COLUMNS)


def load_applied_jobs() -> set[str]:
    """Load applied job IDs plus the permanent blocklist — all will be skipped."""
    _ensure_csv()
    applied = set(BLOCKLISTED_JOB_IDS)
    with open(CSV_PATH, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("status", "").strip() == "applied":
                job_id = row.get("job_id", "").strip()
                if job_id:
                    applied.add(job_id)
    logger.info("Loaded %d previously applied job IDs (%d blocklisted)",
                len(applied), len(BLOCKLISTED_JOB_IDS))
    return applied


def is_already_applied(job_id: str, applied_set: set[str]) -> bool:
    return job_id in applied_set


def log_application(
    job_title: str,
    company: str,
    location: str,
    url: str,
    job_id: str,
    status: str,
    notes: str = "",
):
    _ensure_csv()
    with open(CSV_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            job_title,
            company,
            location,
            url,
            job_id,
            status,
            notes,
        ])
    logger.info("Logged [%s] %s at %s — %s", status, job_title, company, notes or "ok")
