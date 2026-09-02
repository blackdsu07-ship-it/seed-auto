"""
Seed Inbox Placement Checker (Yahoo IMAP) — Streamlit version
---------------------------------------------------------------
Upload accounts (email,password per line), give a sender address/domain to
track. The app logs into each Yahoo IMAP account, finds matching emails from
the last 24h in both Inbox and Spam/Bulk, opens them, clicks the links inside
(requests first, headless Chromium via Playwright as fallback), and reports
Inbox vs Spam placement + percentages.

Deploy on Streamlit Community Cloud:
  - requirements.txt -> streamlit, requests, playwright
  - packages.txt     -> apt deps Chromium needs (included alongside this file)
  - Yahoo accounts need an APP PASSWORD (Account Security > Generate app
    password), not the normal login password.
"""

import email
import email.utils
import imaplib
import re
import subprocess
from datetime import datetime, timedelta, timezone

import requests
import streamlit as st

IMAP_HOST = "imap.mail.yahoo.com"
IMAP_PORT = 993
SPAM_FOLDER_CANDIDATES = ["Bulk", "Spam", "Junk"]
LOOKBACK_SECONDS = 24 * 60 * 60
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

st.set_page_config(page_title="Seed Inbox Placement Checker", layout="wide")


# ---------------------------------------------------------------------------
# Playwright setup (installs the headless browser once per running instance)
# ---------------------------------------------------------------------------

@st.cache_resource
def ensure_playwright_browser():
    try:
        subprocess.run(["playwright", "install", "chromium"], check=False, timeout=180)
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_accounts_file(uploaded_file):
    accounts = []
    text = uploaded_file.getvalue().decode("utf-8", errors="ignore")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "email" in line.lower() and "pass" in line.lower():
            continue  # skip header row
        parts = re.split(r"[,:|\t]", line, maxsplit=1)
        if len(parts) < 2:
            continue
        acc_email, acc_pass = parts[0].strip(), parts[1].strip()
        if acc_email and acc_pass:
            accounts.append((acc_email, acc_pass))
    return accounts


def open_mailbox(email_addr, password, folder="INBOX"):
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(email_addr, password)
        typ, _ = imap.select(folder, readonly=True)
        if typ != "OK":
            imap.logout()
            return None
        return imap
    except Exception:
        return None


def find_spam_folder(email_addr, password):
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(email_addr, password)
        typ, folders = imap.list()
        imap.logout()
        if typ != "OK" or not folders:
            return None
        for raw in folders:
            decoded = raw.decode(errors="ignore") if isinstance(raw, bytes) else str(raw)
            m = re.search(r'"([^"]+)"\s*$', decoded)
            name = m.group(1) if m else decoded.split()[-1]
            for cand in SPAM_FOLDER_CANDIDATES:
                if name.strip('"').lower() == cand.lower():
                    return name.strip('"')
        return None
    except Exception:
        return None


def search_recent_from(imap, sender, lookback_seconds):
    since_date = (datetime.now() - timedelta(days=2)).strftime("%d-%b-%Y")
    try:
        typ, data = imap.uid("search", None, f'(SINCE "{since_date}" FROM "{sender}")')
    except Exception:
        return []
    if typ != "OK" or not data or not data[0]:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=lookback_seconds)
    matches = []
    for uid in data[0].split():
        try:
            typ, hdr_data = imap.uid(
                "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (DATE SUBJECT FROM)])"
            )
            if typ != "OK" or not hdr_data or not hdr_data[0]:
                continue
            raw_header = hdr_data[0][1]
            msg = email.message_from_bytes(raw_header)
            date_hdr = msg.get("Date")
            if not date_hdr:
                continue
            dt = email.utils.parsedate_to_datetime(date_hdr)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt < cutoff:
                continue
            subject = msg.get("Subject", "(no subject)")
            from_ = msg.get("From", "")
            matches.append({"uid": uid, "subject": subject, "from": from_, "date": dt})
        except Exception:
            continue
    return matches


def get_message_body(imap, uid):
    try:
        typ, msg_data = imap.uid("fetch", uid, "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            return ""
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        parts = []
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype in ("text/plain", "text/html"):
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="ignore"))
                    except Exception:
                        continue
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                parts.append(payload.decode(msg.get_content_charset() or "utf-8", errors="ignore"))
        return "\n".join(parts)
    except Exception:
        return ""


def extract_links(body):
    links = re.findall(r'https?://[^\s"\'<>\)]+', body, flags=re.IGNORECASE)
    return list(dict.fromkeys(links))  # dedupe, preserve order


def click_via_requests(url):
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20, allow_redirects=True)
        return r.status_code < 400
    except Exception:
        return False


def click_via_headless(url):
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="networkidle", timeout=25000)
            page.wait_for_timeout(2000)
            page.close()
            browser.close()
        return True
    except Exception:
        return False


def click_link(url):
    if click_via_requests(url):
        return "http"
    if click_via_headless(url):
        return "headless"
    return "failed"


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("Seed Inbox Placement Checker")
st.caption("Yahoo IMAP · checks Inbox vs Spam placement in the last 24 hours · clicks links in matched emails")

ensure_playwright_browser()

accounts_file = st.file_uploader("Accounts file (CSV/TXT — one \"email,password\" per line)", type=["csv", "txt"])
sender = st.text_input("Sender address or domain to track", placeholder="yourdomain.com")
run = st.button("Run Check", type="primary")

if run:
    if not accounts_file:
        st.error("Please upload an accounts file.")
    elif not sender.strip():
        st.error("Sender address/domain is required.")
    else:
        accounts = parse_accounts_file(accounts_file)
        if not accounts:
            st.error('No valid accounts found in file. Expected one "email,password" per line.')
        else:
            results = []
            total_inbox = 0
            total_spam = 0

            progress = st.progress(0.0, text="Starting...")
            for i, (acc_email, acc_pass) in enumerate(accounts):
                progress.progress((i) / len(accounts), text=f"Checking {acc_email}...")
                account_result = {"email": acc_email, "status": "ok", "messages": []}

                inbox_conn = open_mailbox(acc_email, acc_pass, "INBOX")
                if not inbox_conn:
                    account_result["status"] = "auth_failed"
                    results.append(account_result)
                    continue

                for m in search_recent_from(inbox_conn, sender, LOOKBACK_SECONDS):
                    body = get_message_body(inbox_conn, m["uid"])
                    links = extract_links(body)
                    clicks = [{"url": link, "method": click_link(link)} for link in links]
                    account_result["messages"].append(
                        {
                            "folder": "Inbox",
                            "subject": m["subject"],
                            "from": m["from"],
                            "date": m["date"].strftime("%Y-%m-%d %H:%M"),
                            "links_clicked": clicks,
                        }
                    )
                    total_inbox += 1
                try:
                    inbox_conn.logout()
                except Exception:
                    pass

                spam_folder = find_spam_folder(acc_email, acc_pass)
                if spam_folder:
                    spam_conn = open_mailbox(acc_email, acc_pass, spam_folder)
                    if spam_conn:
                        for m in search_recent_from(spam_conn, sender, LOOKBACK_SECONDS):
                            body = get_message_body(spam_conn, m["uid"])
                            links = extract_links(body)
                            clicks = [{"url": link, "method": click_link(link)} for link in links]
                            account_result["messages"].append(
                                {
                                    "folder": spam_folder,
                                    "subject": m["subject"],
                                    "from": m["from"],
                                    "date": m["date"].strftime("%Y-%m-%d %H:%M"),
                                    "links_clicked": clicks,
                                }
                            )
                            total_spam += 1
                        try:
                            spam_conn.logout()
                        except Exception:
                            pass

                results.append(account_result)

            progress.progress(1.0, text="Done")
            progress.empty()

            total_found = total_inbox + total_spam
            inbox_pct = round((total_inbox / total_found) * 100, 1) if total_found else 0
            spam_pct = round((total_spam / total_found) * 100, 1) if total_found else 0

            c1, c2, c3 = st.columns(3)
            c1.metric("Total Found (24h)", total_found)
            c2.metric("Landed in Inbox", f"{total_inbox} ({inbox_pct}%)")
            c3.metric("Landed in Spam", f"{total_spam} ({spam_pct}%)")

            st.divider()

            for acct in results:
                match_count = len(acct["messages"])
                header_status = acct["status"] if acct["status"] != "ok" else f"{match_count} match(es)"
                with st.expander(f"{acct['email']} — {header_status}"):
                    if acct["status"] != "ok":
                        st.error(acct["status"])
                        continue
                    if not acct["messages"]:
                        st.write("No matching email in last 24h.")
                        continue
                    for msg in acct["messages"]:
                        badge = "🟢 Inbox" if msg["folder"] == "Inbox" else f"🟠 {msg['folder']}"
                        st.markdown(f"**{badge}** — {msg['subject']}  \n"
                                    f"From: {msg['from']} · Received: {msg['date']}")
                        if msg["links_clicked"]:
                            for lc in msg["links_clicked"]:
                                icon = {"http": "✅", "headless": "🌐", "failed": "❌"}[lc["method"]]
                                st.caption(f"{icon} {lc['method']}: {lc['url'][:80]}")
                        else:
                            st.caption("no links found")
                        st.markdown("---")

                    # per-account mini breakdown
                    acct_inbox = sum(1 for m in acct["messages"] if m["folder"] == "Inbox")
                    acct_spam = len(acct["messages"]) - acct_inbox
                    st.caption(f"This account: {acct_inbox} inbox / {acct_spam} spam")

            st.divider()
            csv_lines = ["email,folder,subject,from,date,links_clicked,links_failed"]
            for acct in results:
                if acct["status"] != "ok":
                    csv_lines.append(f'{acct["email"]},AUTH_FAILED,,,,,')
                    continue
                for msg in acct["messages"]:
                    ok_links = sum(1 for lc in msg["links_clicked"] if lc["method"] != "failed")
                    failed_links = sum(1 for lc in msg["links_clicked"] if lc["method"] == "failed")
                    subject_clean = msg["subject"].replace(",", ";").replace("\n", " ")
                    from_clean = msg["from"].replace(",", ";")
                    csv_lines.append(
                        f'{acct["email"]},{msg["folder"]},{subject_clean},{from_clean},'
                        f'{msg["date"]},{ok_links},{failed_links}'
                    )
            csv_data = "\n".join(csv_lines)
            st.download_button(
                "Download results (CSV)",
                data=csv_data,
                file_name=f"seed_placement_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
            )
