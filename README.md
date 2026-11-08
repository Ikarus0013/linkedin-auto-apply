# linkedin-auto-apply

This Python tool searches LinkedIn for jobs and fills in Easy Apply forms with your profile data and Google Gemini.

> **CAUTION:** Automated use of LinkedIn can break the LinkedIn User Agreement. LinkedIn can restrict or close your account. You are responsible for all use of this tool. Use it for learning and at your own risk.

## What it does

- Opens a visible Chromium browser and logs in to LinkedIn.
- Searches each job role in each location from your profile. It uses the "Easy Apply" filter and sorts by most recent.
- Skips jobs that you applied to before. It also skips junior roles and job IDs on your blocklist.
- Fills in Easy Apply forms with values from `profile.json`. Gemini gets a screenshot of the form and fills in the fields that the fixed rules do not know.
- Uploads your resume and submits the application.
- Writes each result to `data/applications.csv`.
- Writes each value that it types into a form to `data/invented_answers.csv`.

**WARNING:** The tool submits real applications in your name. Do a dry run first.

## Requirements

- Python 3.10 or later (tested with Python 3.13 on macOS)
- A LinkedIn account
- A Google Gemini API key
- Your resume as a PDF file

## Install

1. Clone the repository:

   ```bash
   git clone https://github.com/Ikarus0013/linkedin-auto-apply.git
   cd linkedin-auto-apply
   ```

2. Make a virtual environment and start it:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. Install the Python packages:

   ```bash
   pip install -r requirements.txt
   ```

4. Install the Chromium browser for Playwright:

   ```bash
   playwright install chromium
   ```

## Configure

### Environment variables

1. Copy the example file:

   ```bash
   cp .env.example .env
   ```

2. Open `.env` and set the values in the table.

| Variable | Use |
|---|---|
| `LINKEDIN_EMAIL` | The email address of your LinkedIn account. |
| `LINKEDIN_PASSWORD` | The password of your LinkedIn account. |
| `GEMINI_API_KEY` | Your Google Gemini API key. |

The tool keeps the browser session in `data/browser_state/`. Thus, you usually log in only one time.

### Profile file

1. Copy the example profile:

   ```bash
   cp profile.example.json profile.json
   ```

2. Open `profile.json` and replace all sample values with your data.

If `profile.json` is missing, the tool stops and shows this message:

```text
ERROR: profile.json not found. Copy profile.example.json to profile.json and fill in your data.
```

| Key | Use |
|---|---|
| `applicant` | Your name, contact data, location, experience, salary, notice period, and links. |
| `applicant.resume_path` | The path to your resume PDF. You can use `~` for your home folder. |
| `applicant.headline` | A short summary. The tool uses it for summary and cover letter fields. |
| `default_answers` | Answers for other common questions, for example EEO questions and "How did you hear about us". |
| `phone_country_code_match` | Text to find in the phone country code list, for example `"+49"`. |
| `education_dropdown_match` | Text to find in the education list, for example `"bachelor"`. |
| `job_roles` | The job titles to search. |
| `locations` | The locations to search. |
| `blocklisted_job_ids` | LinkedIn job IDs that the tool must always skip. |

The files `.env` and `profile.json` are in `.gitignore`. Do not commit them.

## Use

1. Start the virtual environment:

   ```bash
   source venv/bin/activate
   ```

2. Do a dry run. The tool finds jobs but does not apply:

   ```bash
   python main.py --dry-run --max-apps 5
   ```

3. Read the log in the terminal. Make sure that the tool finds the correct jobs.

4. Start a real run with a small limit:

   ```bash
   python main.py --max-apps 5
   ```

5. Read `data/applications.csv` and `data/invented_answers.csv`.

Example with your own roles and locations:

```bash
python main.py --roles "Product Manager,Product Owner" --locations "Remote" --max-apps 10
```

Expected output at the end of a run:

```text
==================================================
RUN COMPLETE
  Applied:         3
  Skipped:         12
  Errors:          2
  Already applied: 40
==================================================
```

| Option | Use |
|---|---|
| `--dry-run` | Find jobs but do not apply. |
| `--max-apps N` | Stop after N applications. The default is 50. |
| `--roles "A,B"` | Search these roles. This option replaces `job_roles` from the profile. |
| `--locations "A,B"` | Search these locations. This option replaces `locations` from the profile. |

## How it works

1. `main.py` loads `.env` and `profile.json`, and makes the `data/` folder if it is missing.
2. `linkedin_bot.py` starts Chromium with a persistent session and logs in.
3. `linkedin_bot.py` opens the job search for each role and location, and reads the job cards.
4. `easy_apply.py` opens the Easy Apply form. It fills in fields from fixed keyword rules first.
5. `gemini_helper.py` sends a screenshot of each form step to Gemini. Gemini returns the actions for the fields that are still empty.
6. `easy_apply.py` goes to the next step until it finds the submit button.
7. `tracker.py` writes the result to `data/applications.csv`.

### Answers that are not in your profile

Some forms ask questions that your profile does not answer. In these cases the tool can give answers that are not true for you:

- The Gemini prompt tells Gemini to answer "Yes" or to select the most positive option when it is not sure.
- For years of experience with a specific skill, Gemini selects a number from your total experience.
- Some dropdown lists that the rules do not know get the first option.

The tool writes all values from the keyword rules to `data/invented_answers.csv`. The column `source` shows `config` (from your profile) or `invented` (a default value). Gemini answers go to `data/run.log`.

**CAUTION:** Read all answers before you trust the tool. An application must not contain false facts. Change the rules in `gemini_helper.py` and `easy_apply.py` if they do not agree with your facts.

## Files

| File | Use |
|---|---|
| `main.py` | Command line entry point and main loop. |
| `config.py` | Loads `profile.json`. Sets paths, delays, and limits. |
| `linkedin_bot.py` | Browser start, login, job search, and pagination. |
| `easy_apply.py` | Fills in and submits LinkedIn Easy Apply forms. |
| `direct_apply.py` | Experimental: fills in forms on external company websites. |
| `gemini_helper.py` | Gemini client and prompt for unknown form fields. |
| `tracker.py` | Reads and writes `data/applications.csv`. |
| `profile.example.json` | Sample profile. Copy it to `profile.json`. |
| `.env.example` | Sample environment file. Copy it to `.env`. |
| `requirements.txt` | Python packages. |

The tool makes these files in `data/`: `applications.csv`, `invented_answers.csv`, `run.log`, `bot.lock`, and `browser_state/`.

## Limits

- LinkedIn changes its web pages often. Then the CSS selectors can stop to work.
- The tool supports only one run at a time. A lock file in `data/bot.lock` prevents a second run.
- If LinkedIn shows a security check, the tool waits 120 seconds. Complete the check manually in the browser during this time.
- The tool cannot fill in all custom questions. It skips an application if required fields stay empty.
- The external website flow in `direct_apply.py` is experimental and does not work at this time.
- Each form step sends a screenshot to the Gemini API. This screenshot can contain your personal data. Gemini API use can cost money.
- The success check looks for English text on the confirmation page.

## License

MIT. Refer to [LICENSE](LICENSE).
