#!/usr/bin/env python3
"""
Beeline brand mention monitor
Processes Google Alerts RSS feeds → Google Sheets + enriched Slack posts
"""

import os
import json
import re
import html
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
import anthropic
import gspread
from google.oauth2.service_account import Credentials
import requests

# ─── Feed config ──────────────────────────────────────────────────────────────

FEEDS = {
    "Beeline Moto": "https://www.google.com/alerts/feeds/01542989359509077897/9035693777240751167",
    "Beeline Velo": "https://www.google.com/alerts/feeds/01542989359509077897/898648235394539070",
}

# ─── Env vars (set as GitHub Actions secrets or local .env) ───────────────────

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SLACK_WEBHOOK     = os.environ.get("SLACK_WEBHOOK_URL", "")
SHEETS_ID         = os.environ["GOOGLE_SHEETS_ID"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]   # full service account JSON as a string

# ─── Sheet structure ──────────────────────────────────────────────────────────

HEADERS = [
    "ID", "Timestamp", "Product", "Headline",
    "URL", "Source Type", "Country/Region", "Sentiment", "Summary"
]

# ─── Lookup tables ────────────────────────────────────────────────────────────

SOURCE_PATTERNS = {
    "Reddit":     ["reddit.com"],
    "YouTube":    ["youtube.com", "youtu.be"],
    "Twitter/X":  ["twitter.com", "x.com"],
    "Facebook":   ["facebook.com"],
    "Instagram":  ["instagram.com"],
    "Amazon":     ["amazon."],
    "Trustpilot": ["trustpilot.com"],
}

TLD_TO_COUNTRY = {
    "co.uk": "UK",   "uk": "UK",
    "de": "Germany", "fr": "France", "es": "Spain",    "it": "Italy",
    "nl": "Netherlands", "be": "Belgium", "ch": "Switzerland", "at": "Austria",
    "se": "Sweden",  "no": "Norway",  "dk": "Denmark", "fi": "Finland",
    "pl": "Poland",  "pt": "Portugal", "ie": "Ireland",
    "au": "Australia", "nz": "New Zealand", "ca": "Canada",
    "za": "South Africa", "in": "India",
    "jp": "Japan",   "sg": "Singapore", "hk": "Hong Kong",
    "br": "Brazil",  "mx": "Mexico",
    "eu": "Europe",  "com": "Global",   "io": "Global",   "co": "Global",
}

SENTIMENT_ICON = {"Positive": ":white_check_mark:", "Negative": ":x:", "Neutral": ":white_circle:"}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def strip_html(text: str) -> str:
    text = html.unescape(text)
    return re.sub(r"<[^>]+>", " ", text).strip()


def detect_source_type(url: str) -> str:
    domain = urlparse(url).netloc.lower()
    for source, patterns in SOURCE_PATTERNS.items():
        if any(p in domain for p in patterns):
            return source
    if any(x in domain for x in ["forum", "discuss", "community", "board", "talk"]):
        return "Forum"
    return "Article"


def detect_country(url: str) -> str:
    domain = re.sub(r"^www\.", "", urlparse(url).netloc.lower())
    # Sort by TLD length descending so co.uk matches before uk
    for tld, country in sorted(TLD_TO_COUNTRY.items(), key=lambda x: -len(x[0])):
        if domain.endswith(f".{tld}"):
            return country
    return "Unknown"


def analyse_with_claude(title: str, snippet: str, client: anthropic.Anthropic) -> dict:
    prompt = f"""Analyse this brand mention and return valid JSON only — no markdown, no explanation.

Title: {title}
Snippet: {snippet}

Return this exact structure:
{{
  "sentiment": "Positive" or "Negative" or "Neutral",
  "summary": "One sentence, max 20 words"
}}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {"sentiment": "Neutral", "summary": title[:120]}


# ─── Google Sheets ────────────────────────────────────────────────────────────

def get_sheet():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS_JSON),
        scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEETS_ID).sheet1


def ensure_headers(sheet):
    if not sheet.row_values(1):
        sheet.insert_row(HEADERS, 1)


def get_existing_ids(sheet) -> set:
    values = sheet.col_values(1)   # Column A
    return set(values[1:])          # Skip header row


# ─── Slack ────────────────────────────────────────────────────────────────────

def post_to_slack(product: str, title: str, url: str, source: str, country: str, sentiment: str, summary: str):
    if not SLACK_WEBHOOK:
        return
    icon = SENTIMENT_ICON.get(sentiment, ":white_circle:")
    text = (
        f"*{product}* | {icon} {sentiment} | {source} | {country}\n"
        f"*<{url}|{title}>*\n"
        f"_{summary}_"
    )
    try:
        requests.post(SLACK_WEBHOOK, json={"text": text}, timeout=10)
    except requests.RequestException as e:
        print(f"Slack post failed: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    claude  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    sheet   = get_sheet()
    ensure_headers(sheet)
    existing_ids = get_existing_ids(sheet)

    new_rows = []
    processed = 0

    for product, feed_url in FEEDS.items():
        feed = feedparser.parse(feed_url)

        if feed.bozo:
            print(f"Warning: feed parse issue for {product}: {feed.bozo_exception}")

        for entry in feed.entries:
            entry_id = entry.get("id") or entry.get("link", "")
            if not entry_id or entry_id in existing_ids:
                continue

            title   = strip_html(entry.get("title", ""))
            url     = entry.get("link", "")
            snippet = strip_html(entry.get("summary", ""))
            ts      = entry.get("published", datetime.now(timezone.utc).isoformat())

            source    = detect_source_type(url)
            country   = detect_country(url)
            analysis  = analyse_with_claude(title, snippet, claude)
            sentiment = analysis.get("sentiment", "Neutral")
            summary   = analysis.get("summary", title[:120])

            new_rows.append([entry_id, ts, product, title, url, source, country, sentiment, summary])
            post_to_slack(product, title, url, source, country, sentiment, summary)
            existing_ids.add(entry_id)
            processed += 1

    if new_rows:
        sheet.append_rows(new_rows, value_input_option="USER_ENTERED")
        print(f"Logged {processed} new mention(s)")
    else:
        print("No new mentions found")


if __name__ == "__main__":
    main()
