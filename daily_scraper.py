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
from typing import List
import time
import random

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load .env if present
load_dotenv()

# Config (from env)
SCRAPE_URL = os.getenv("SCRAPE_URL")
CSS_SELECTOR = os.getenv("CSS_SELECTOR", "body")
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "20"))
USER_AGENT = os.getenv("USER_AGENT", "daily-scraper/1.0 (+https://example.com)")
TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "15"))

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_TO = os.getenv("EMAIL_TO")
EMAIL_SUBJECT_PREFIX = os.getenv("EMAIL_SUBJECT_PREFIX", "Daily Scrape")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")

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


def parse(html: str, selector: str, max_items: int) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    nodes = soup.select(selector)
    logger.info(f"Found {len(nodes)} nodes for selector '{selector}'")
    results = []
    for n in nodes[:max_items]:
        text = n.get_text(separator=" ", strip=True)
        # If it's a link, also include href
        href = None
        if n.name == "a" and n.has_attr("href"):
            href = n["href"]
        # attempt to find anchors inside
        if not href:
            a = n.find("a")
            if a and a.has_attr("href"):
                href = a["href"]
        if href:
            results.append(f"{text} — {href}")
        else:
            results.append(text)
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
    msg.set_content(body)
    return msg


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
    raise last_exc


def send_via_sendgrid(subject: str, body: str, sender: str, recipients: List[str], api_key: str) -> None:
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
        "content": [{"type": "text/plain", "value": body}],
    }

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
        items = parse(html, CSS_SELECTOR, MAX_ITEMS)

        if args.inspect:
            limit = getattr(args, "inspect_limit", 10)
            insp = inspect_selector(html, CSS_SELECTOR, limit=limit)
            print(f"--- Inspection: first {len(insp)} nodes for selector '{CSS_SELECTOR}' ---")
            for e in insp:
                print(f"[{e['index']}] <{e['tag']}> attrs={e['attrs']}")
                print(f"text: {e['text_snippet']}")
                print(f"html: {e['html_snippet']}\n")
            return 0

        if not items:
            body = f"No results found when scraping {SCRAPE_URL} with selector '{CSS_SELECTOR}'."
        else:
            lines = [f"Results from {SCRAPE_URL}", "", *[f"- {i}" for i in items]]
            body = "\n".join(lines)

        subject = f"{EMAIL_SUBJECT_PREFIX}: {SCRAPE_URL}"

        if args.dry_run:
            print("--- DRY RUN: Email body below ---")
            print(subject)
            print(body)
            print("--- END ---")
            return 0

        recipients = [e.strip() for e in EMAIL_TO.split(",") if e.strip()]
        msg = build_email(subject, body, EMAIL_FROM, recipients)
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
