"""daily_scraper.py

Simple configurable web scraper that runs once and emails results.
Configure via environment variables or a .env file (see .env.example).

Usage:
  python daily_scraper.py           # runs once (use Task Scheduler to run daily)
  python daily_scraper.py --test    # runs and prints results, won't send email if --dry-run used
"""
import os
import sys
import argparse
import logging
import smtplib
from email.message import EmailMessage
import re
from html import escape as escape_html
from typing import List, Dict
import time
import random
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load .env if present
load_dotenv()

# Helper function to safely get environment variables
def get_env(key: str, default: str) -> str:
    value = os.getenv(key, "").strip()
    return value if value else default

# Config (from env)
SCRAPE_URL = os.getenv("SCRAPE_URL")
CSS_SELECTOR = get_env("CSS_SELECTOR", "body")
# Support multiple selectors (comma-separated)
CSS_SELECTORS = [s.strip() for s in CSS_SELECTOR.split(",")] if CSS_SELECTOR else ["body"]
MAX_ITEMS = int(get_env("MAX_ITEMS", "20"))
USER_AGENT = get_env("USER_AGENT", "daily-scraper/1.0 (+https://example.com)")
TIMEOUT = float(get_env("REQUEST_TIMEOUT", "15"))

# Email config with proper defaults for type checking
SMTP_HOST = os.getenv("SMTP_HOST", "")  # Empty string as default
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")  # Empty string as default
SMTP_PASS = os.getenv("SMTP_PASS", "")  # Empty string as default
EMAIL_FROM = os.getenv("EMAIL_FROM", "")  # Empty string as default
EMAIL_TO = os.getenv("EMAIL_TO", "")  # Empty string as default
EMAIL_SUBJECT_PREFIX = os.getenv("EMAIL_SUBJECT_PREFIX", "Daily Scrape")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")

# Retry/backoff configuration
RETRIES = int(os.getenv("RETRIES", "3"))
BACKOFF_FACTOR = float(os.getenv("BACKOFF_FACTOR", "1"))
MAX_BACKOFF = float(os.getenv("MAX_BACKOFF", "60"))


LOG_FILE = os.getenv("LOG_FILE", "daily_scraper.log")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


class ScrapeError(Exception):
    pass


def fetch(url: str) -> str:
    headers = {"User-Agent": USER_AGENT}
    logger.info(f"Fetching {url}")
    last_exc = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
            # Retry on server errors or rate limits
            if r.status_code >= 500 or r.status_code == 429:
                msg = f"Server returned {r.status_code}"
                logger.warning(f"Attempt {attempt}/{RETRIES} failed: {msg}")
                last_exc = ScrapeError(msg)
            else:
                r.raise_for_status()
                return r.text
        except requests.RequestException as e:
            logger.warning(f"Attempt {attempt}/{RETRIES} failed with exception: {e}")
            last_exc = e

        if attempt == RETRIES:
            break

        # exponential backoff with jitter
        backoff = min(MAX_BACKOFF, BACKOFF_FACTOR * (2 ** (attempt - 1)))
        jitter = random.uniform(0, backoff * 0.1)
        sleep_for = backoff + jitter
        logger.info(f"Sleeping {sleep_for:.1f}s before retrying...")
        time.sleep(sleep_for)

    logger.exception("Failed to fetch URL after retries")
    raise ScrapeError(last_exc)


def parse(html: str, selectors: List[str], max_items: int) -> Dict[str, List[str]]:
    """Return a mapping of selector -> list of extracted item strings."""
    soup = BeautifulSoup(html, "lxml")
    results: Dict[str, List[str]] = {}
    items_per_selector = max_items // len(selectors) if selectors else max_items

    for selector in selectors:
        nodes = soup.select(selector)
        logger.info(f"Found {len(nodes)} nodes for selector '{selector}'")
        selector_results: List[str] = []
        count = 0

        for n in nodes:
            if count >= items_per_selector:
                break

            # If it's a table row, handle cells separately
            if n.name == "tr":
                cells = n.find_all(["td", "th"])
                if cells:
                    row_text = " | ".join(cell.get_text(strip=True) for cell in cells)
                    selector_results.append(row_text)
                    count += 1
                continue

            # If the selected node is a table or contains rows, iterate its <tr> children
            if n.name in ("table", "tbody", "thead") or n.find("tr"):
                rows = n.select("tr")
                for r in rows:
                    if count >= items_per_selector:
                        break
                    cells = r.find_all(["td", "th"])
                    if cells:
                        row_text = " | ".join(cell.get_text(strip=True) for cell in cells)
                        selector_results.append(row_text)
                        count += 1
                continue

            # Otherwise treat as a normal node
            text = n.get_text(separator=" ", strip=True)
            href = None
            if n.name == "a" and n.has_attr("href"):
                href = n["href"]
            if not href:
                a = n.find("a")
                if a and a.has_attr("href"):
                    href = a["href"]

            # If this selector is for replay-center-assignment, do NOT include the link URL — only capture text
            if "replay-center-assignment" in selector:
                if text:
                    selector_results.append(text)
            else:
                if href:
                    selector_results.append(f"{text} — {href}")
                else:
                    if text:
                        selector_results.append(text)
            count += 1

        results[selector] = selector_results

    return results


def inspect_selector(html: str, selector: str, limit: int = 10) -> List[dict]:
    """Return a short summary of the first `limit` elements matching selector to help pick the right CSS selector.

    Each dict contains: index, tag, attrs, text_snippet, html_snippet
    """
    soup = BeautifulSoup(html, "lxml")
    nodes = soup.select(selector)
    out = []
    for i, n in enumerate(nodes[:limit]):
        text = n.get_text(separator=" ", strip=True)
        text_snippet = (text[:200] + "...") if len(text) > 200 else text
        html_snippet = str(n)
        if len(html_snippet) > 400:
            html_snippet = html_snippet[:400] + "..."
        out.append({
            "index": i,
            "tag": n.name,
            "attrs": dict(n.attrs),
            "text_snippet": text_snippet,
            "html_snippet": html_snippet,
        })
    return out


def build_email(subject: str, body: str, sender: str, recipients: List[str]) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    # Plain-text body
    msg.set_content(body)
    return msg


def build_email_with_html(subject: str, body: str, html_body: str, sender: str, recipients: List[str]) -> EmailMessage:
    """Build a multipart email with both plain-text and HTML alternatives."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    msg.add_alternative(html_body, subtype="html")
    return msg


def format_html_body(items: List[str] | Dict[str, List[str]], source_url: str) -> str:
    """Return a responsive HTML body string.

    `items` may be either a flat list of strings or a mapping of selector -> list of strings.
    When a mapping is provided, a separate table is generated for each selector with a small
    heading indicating the selector name.
    """
    # Basic responsive CSS for email clients (inline-friendly)
    css = (
        "body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial; "
        "color: #111; margin:0; padding:16px; }"
        "table { width:100%; border-collapse: collapse; margin-bottom:18px; }"
        "th, td { text-align:left; padding:8px; border-bottom:1px solid #eee; font-size:14px; }"
        "th { background:#f6f6f6; font-weight:600; }"
        "@media only screen and (max-width:600px) { td, th { display:block; width:100%; box-sizing:border-box; }"
        "table { display:block; } }"
    )

    def render_table_for_list(lst: List[str], title: str | None = None) -> str:
        rows_html = []
        header_cells = None
        # Special-casing by selector name:
        force_no_header = False
        drop_last_column = False
        if title:
            if "nba-refs-content" in title:
                # For nba-refs-content, treat the first row as regular data
                force_no_header = True
            if "gl-refs-content" in title:
                # For gl-refs-content, remove the last column (Alternate)
                drop_last_column = True

        if lst and not force_no_header:
            first = lst[0]
            if " | " in first:
                parts = [p.strip() for p in first.split("|")]
                if any(re.search(r"[A-Za-z]", p) for p in parts):
                    header_cells = parts
                    if drop_last_column and len(header_cells) > 0:
                        header_cells = header_cells[:-1]

        for idx, it in enumerate(lst):
            # If we detected header_cells coming from the first row, skip that row
            if header_cells and idx == 0:
                continue
            if " | " in it:
                parts = [p.strip() for p in it.split("|")]
                if drop_last_column and len(parts) > 0:
                    parts = parts[:-1]
                cell_parts = []
                for cidx, p in enumerate(parts):
                    escaped = escape_html(p)
                    if cidx == 0:
                        cell_parts.append(f"<td><strong>{escaped}</strong></td>")
                    else:
                        cell_parts.append(f"<td>{escaped}</td>")
                cell_html = "".join(cell_parts)
                rows_html.append(f"<tr>{cell_html}</tr>")
            else:
                # single-cell row -> first (and only) column should be bold
                rows_html.append(f"<tr><td><strong>{escape_html(it)}</strong></td></tr>")

        header_html = ""
        if header_cells:
            header_html = "<tr>" + "".join(f"<th>{escape_html(h)}</th>" for h in header_cells) + "</tr>"

        # Map selector strings to friendlier display titles
        display_title = None
        if title:
            if "nba-refs-content" in title:
                display_title = "NBA Assignments"
            elif "replay-center-assignment" in title:
                display_title = "Replay Assignments"
            elif "gl-refs-content" in title:
                display_title = "G League Assignments"
            else:
                display_title = title

        title_html = f"<h3 style='margin:10px 0 6px 0'>{escape_html(display_title)}</h3>" if display_title else ""
        return f"{title_html}<table role='presentation'>{header_html}{''.join(rows_html)}</table>"

    # Normalize items into sections: list of (title, list)
    sections: List[tuple[str | None, List[str]]] = []
    if isinstance(items, dict):
        for sel, lst in items.items():
            sections.append((sel, lst))
    else:
        sections.append((None, items))

    tables_html = "".join(render_table_for_list(lst, title=sel) for sel, lst in sections)

    html = f"""
<html>
    <head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <style>{css}</style>
    </head>
    <body>
    <h2 style="margin-top:0">Results from {escape_html(source_url)}</h2>
    {tables_html}
    <p style="color:#666;font-size:12px">Sent by NBA-referee-scraper</p>
    </body>
</html>
"""
    return html


def send_email(msg: EmailMessage, host: str, port: int, user: str, password: str) -> None:
    logger.info(f"Sending email to {msg['To']} via {host}:{port}")
    last_exc = None
    for attempt in range(1, RETRIES + 1):
        server = None
        try:
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, timeout=30)
            else:
                server = smtplib.SMTP(host, port, timeout=30)
                server.starttls()
            if user and password:
                server.login(user, password)
            server.send_message(msg)
            try:
                server.quit()
            except Exception:
                pass
            logger.info("Email sent successfully")
            return
        except Exception as e:
            logger.warning(f"SMTP attempt {attempt}/{RETRIES} failed: {e}")
            last_exc = e
            try:
                if server:
                    server.quit()
            except Exception:
                pass

        if attempt == RETRIES:
            break

        backoff = min(MAX_BACKOFF, BACKOFF_FACTOR * (2 ** (attempt - 1)))
        jitter = random.uniform(0, backoff * 0.1)
        sleep_for = backoff + jitter
        logger.info(f"Sleeping {sleep_for:.1f}s before retrying SMTP...")
        time.sleep(sleep_for)

    logger.exception("Failed to send email via SMTP after retries")
    if last_exc is None:
        last_exc = Exception("Max retries exceeded")
    raise last_exc


def send_via_sendgrid(subject: str, body: str, sender: str, recipients: List[str], api_key: str, html_body: str | None = None) -> None:
    """Send plain-text email via SendGrid Web API v3.

    This uses the /mail/send endpoint and requires a full-access API key stored in SENDGRID_API_KEY.
    """
    logger.info(f"Sending email via SendGrid to {', '.join(recipients)}")
    # Extract email address from "Name <email@example.com>" if necessary
    m = re.search(r"<([^>]+)>", sender or "")
    from_email = m.group(1) if m else (sender or "no-reply@example.com")

    payload = {
        "personalizations": [
            {
                "to": [{"email": r} for r in recipients],
                "subject": subject,
            }
        ],
        "from": {"email": from_email},
        "content": [
            {"type": "text/plain", "value": body},
        ],
    }

    if html_body:
        payload["content"].append({"type": "text/html", "value": html_body})

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_exc = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.post("https://api.sendgrid.com/v3/mail/send", json=payload, headers=headers, timeout=30)
            if resp.status_code >= 500 or resp.status_code == 429:
                logger.warning(f"SendGrid attempt {attempt}/{RETRIES} returned {resp.status_code}")
                last_exc = Exception(f"SendGrid {resp.status_code}: {resp.text}")
            else:
                resp.raise_for_status()
                logger.info("SendGrid accepted the message")
                return
        except requests.RequestException as e:
            logger.warning(f"SendGrid attempt {attempt}/{RETRIES} failed: {e}")
            last_exc = e

        if attempt == RETRIES:
            break

        backoff = min(MAX_BACKOFF, BACKOFF_FACTOR * (2 ** (attempt - 1)))
        jitter = random.uniform(0, backoff * 0.1)
        sleep_for = backoff + jitter
        logger.info(f"Sleeping {sleep_for:.1f}s before retrying SendGrid...")
        time.sleep(sleep_for)

    logger.exception("Failed to send email via SendGrid after retries")
    if last_exc is None:
        last_exc = Exception("Max retries exceeded")
    raise last_exc


def main(args):
    if not SCRAPE_URL:
        logger.error("SCRAPE_URL is not configured. See .env.example")
        sys.exit(2)

    # If SendGrid is not configured, require SMTP settings. If SendGrid is set we allow SendGrid-only runs.
    if not SENDGRID_API_KEY and (not EMAIL_TO or not EMAIL_FROM or not SMTP_HOST):
        logger.error("Email configuration incomplete. Provide SENDGRID_API_KEY or SMTP_HOST + EMAIL_FROM + EMAIL_TO. See .env.example")
        sys.exit(2)

    try:
        html = fetch(SCRAPE_URL)
        items = parse(html, CSS_SELECTORS, MAX_ITEMS)

        if args.inspect:
            limit = getattr(args, "inspect_limit", 10)
            for selector in CSS_SELECTORS:
                print(f"\n--- Selector: {selector} ---")
                insp = inspect_selector(html, selector, limit=limit)
                for e in insp:
                    print(f"[{e['index']}] <{e['tag']}> attrs={e['attrs']}")
                    print(f"text: {e['text_snippet']}")
                    print(f"html: {e['html_snippet']}\n")
            return 0

        # Normalize items for plain-text body (items may be a dict of selector->list)
        if not items:
            body = f"No results found when scraping {SCRAPE_URL} with selector '{CSS_SELECTOR}'."
        else:
            flat_lines: List[str] = [f"Results from {SCRAPE_URL}", "-" * 40]
            if isinstance(items, dict):
                first_section = True
                for sel, lst in items.items():
                    if not lst:
                        continue
                    if not first_section:
                        flat_lines.append("-" * 20 + f" {sel} " + "-" * 20)
                    else:
                        first_section = False
                    flat_lines.extend(lst)
            else:
                flat_lines.extend(items)
            flat_lines.append("-" * 40)
            body = "\n".join(flat_lines)

        today = datetime.now().strftime("%Y-%m-%d")
        subject = f"{EMAIL_SUBJECT_PREFIX} ({today}): {SCRAPE_URL}"

        # Build HTML body for nicer formatting on mobile/desktop
        html_body = format_html_body(items, SCRAPE_URL)

        if args.dry_run:
            print("--- DRY RUN: Email body below ---")
            print(subject)
            print(body)
            print("--- HTML preview ---")
            print(html_body)
            print("--- END ---")
            return 0

        recipients = [e.strip() for e in EMAIL_TO.split(",") if e.strip()]

        # Send using SendGrid if configured, otherwise SMTP
        if SENDGRID_API_KEY:
            send_via_sendgrid(subject, body, EMAIL_FROM, recipients, SENDGRID_API_KEY, html_body=html_body)
        else:
            # Ensure SMTP settings exist
            if not SMTP_HOST:
                logger.error("SMTP_HOST is not configured and SENDGRID_API_KEY not provided")
                sys.exit(2)
            if not EMAIL_FROM:
                logger.error("EMAIL_FROM is not configured")
                sys.exit(2)
            msg = build_email_with_html(subject, body, html_body, EMAIL_FROM, recipients)
            send_email(msg, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS)

    except ScrapeError:
        logger.exception("Scrape failed")
        sys.exit(1)
    except Exception:
        logger.exception("Unexpected error")
        sys.exit(1)

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily scraper that emails results")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="Don't send email; print results")
    parser.add_argument("--test", dest="test", action="store_true", help="Alias for --dry-run")
    parser.add_argument("--inspect", dest="inspect", action="store_true", help="Print a short summary of elements matching CSS_SELECTOR and exit")
    parser.add_argument("--inspect-limit", dest="inspect_limit", type=int, default=10, help="How many matched elements to show with --inspect")
    ns = parser.parse_args()
    if ns.test:
        ns.dry_run = True
    sys.exit(main(ns))
