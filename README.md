# Daily Scraper

This repository contains a small Python script to scrape a website and email the results. It is designed to be run once per day by the Windows Task Scheduler or GitHub Actions.

Files
- `daily_scraper.py` — main script. Reads configuration from environment variables or a `.env` file.
- `.env.example` — example environment variables. Copy to `.env` and edit.
- `requirements.txt` — Python dependencies.

Setup
1. Create a virtual environment and install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

2. Copy `.env.example` to `.env` and edit values (especially `SCRAPE_URL`, `CSS_SELECTOR`, and SMTP settings).

Running manually

```powershell
# Dry run (prints results, doesn't send email)
python .\daily_scraper.py --dry-run

# Run and send email
python .\daily_scraper.py
```

Testing CSS selectors locally

If you're not sure what CSS selector to use, run the script in `--inspect` mode. It will fetch the page and print a short summary of the first N matching elements so you can refine the selector.

```powershell
# Show up to 10 matches (default)
python .\daily_scraper.py --inspect

# Show up to 50 matches
python .\daily_scraper.py --inspect --inspect-limit 50
```

Set `SCRAPE_URL` and `CSS_SELECTOR` in a local `.env` (or export env vars) before running.

Scheduling daily on Windows (Task Scheduler)

1. Open Task Scheduler -> Create Basic Task.
2. Trigger: Daily, set time.
3. Action: Start a program.
   - Program/script: powershell.exe
   - Add arguments (replace paths):

```powershell
-NoProfile -WindowStyle Hidden -Command "cd 'C:\Users\cshelor\OneDrive - Conga\Documents\Scripts and Development\NBA-referee-scraper'; & 'C:\Path\To\python.exe' .\daily_scraper.py"
```

Notes:
- Use the full path to your Python executable if it's not on the system PATH.
- If you want the task to run whether you're logged in or not, configure the task to run with stored credentials.
- Alternatively, you can call the script with the Python executable directly in the 'Program/script' box and put the script path in 'Add arguments'.

Security
- Keep SMTP credentials private. If using Gmail, consider creating an app password.
- For more advanced use, send via an API (SendGrid, Mailgun, SES) instead of SMTP.

Troubleshooting
- Check `daily_scraper.log` (or the file set in `LOG_FILE`) for errors.
- If parsing fails, tweak `CSS_SELECTOR` or open the target page in a browser and inspect the HTML.

Enhancements (ideas)
- Attach a CSV of results to the email.
- Use an API-based mail service for more reliable delivery and metrics.
- Add a retention or history file to detect changes since last run.

If you'd like, I can also:
- Add an example Task Scheduler XML you can import.
- Add optional support for sending via an API instead of SMTP.

GitHub Actions
----------------

You can run this scraper in GitHub Actions on a schedule instead of using Task Scheduler. I added a workflow at `.github/workflows/daily-scrape.yml` that runs daily by default.

Secrets to add to your repository (Settings -> Secrets & variables -> Actions):

- SCRAPE_URL — The page to scrape (e.g. https://example.com)
- CSS_SELECTOR — CSS selector for items you want to extract (e.g. 'a' or '.news-list a')
- MAX_ITEMS — (optional) max items to include in email
- SMTP_HOST — SMTP server hostname
- SMTP_PORT — SMTP port (e.g. 587)
- SMTP_USER — SMTP username
- SMTP_PASS — SMTP password (keep secret)
- EMAIL_FROM — From address (e.g. "Me <me@example.com>")
- EMAIL_TO — Comma-separated list of recipients
- EMAIL_SUBJECT_PREFIX — (optional) subject prefix

Optional secrets you can set to tune behavior:

- USER_AGENT — HTTP user agent the scraper uses
- REQUEST_TIMEOUT — Request timeout in seconds

SendGrid (recommended)
-----------------------

If you prefer not to store SMTP credentials, you can use SendGrid's Web API. Set the following secret:

- SENDGRID_API_KEY — Your SendGrid API key (with Mail Send permission)

When `SENDGRID_API_KEY` is present the workflow/script will prefer using SendGrid to send the email.

Scheduling and timezone notes
-----------------------------

The Actions workflow is scheduled to run daily at approximately 9:30 AM Eastern Time. Because GitHub Actions' cron uses UTC and Daylight Saving Time changes the offset, the workflow includes two cron entries that together cover both standard and daylight offsets. The job will run once per day at 9:30 AM local ET.

To run the workflow manually, go to the Actions tab, select "Daily Scrape" and click "Run workflow".
