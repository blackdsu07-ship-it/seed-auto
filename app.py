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
import time
from datetime import datetime, timedelta, timezone

import requests
import streamlit as st

IMAP_HOST = "imap.mail.yahoo.com"
IMAP_PORT = 993
IMAP_TIMEOUT = 20  # seconds - without this a stuck connection hangs forever
MAX_CANDIDATES_PER_FOLDER = 400  # cap header fetches on high-volume shared seed mailboxes
FOLDER_TIME_BUDGET = 60  # seconds - abort a folder's header-fetch loop past this, don't hang the whole run
SPAM_FOLDER_CANDIDATES = ["Bulk", "Bulk Mail", "Spam", "Junk"]
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


def open_mailbox(email_addr, password, folder="INBOX", debug=None):
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT)
        imap.login(email_addr, password)
        typ, sel_data = imap.select(folder, readonly=True)
        if typ != "OK":
            if debug is not None:
                debug["select_error"] = f"SELECT {folder} -> {typ} {sel_data}"
            imap.logout()
            return None
        return imap
    except Exception as e:
        if debug is not None:
            debug["login_error"] = f"{type(e).__name__}: {e}"
        return None


def find_spam_folder(email_addr, password):
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT)
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


def search_recent_from(imap, sender, lookback_seconds, debug=None):
    """debug, if passed a dict, gets populated with diagnostics so the
    caller can show *why* zero matches came back instead of just seeing 0.

    Yahoo's IMAP SEARCH is unreliable with compound criteria like
    FROM "..." combined with SINCE - it frequently comes back OK with zero
    UIDs even when matching mail exists. So we only let the server filter
    by SINCE (which it handles fine), pull headers for that candidate set,
    and match the sender ourselves in Python.
    """
    since_date = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
    criteria = f'(SINCE "{since_date}")'
    if debug is not None:
        debug["criteria"] = criteria
        debug["sender_filter"] = sender

    try:
        typ, data = imap.uid("search", None, criteria)
    except Exception as e:
        if debug is not None:
            debug["search_error"] = f"{type(e).__name__}: {e}"
        return []

    if debug is not None:
        debug["search_typ"] = typ
        debug["search_raw"] = [d.decode(errors="replace") if isinstance(d, bytes) else str(d) for d in data]

    if typ != "OK" or not data or not data[0]:
        if debug is not None:
            debug["raw_uid_count"] = 0
        return []

    uids = data[0].split()
    uid_strs = [u.decode() if isinstance(u, bytes) else str(u) for u in uids]

    truncated = False
    if len(uid_strs) > MAX_CANDIDATES_PER_FOLDER:
        # UIDs increase over time, so keep the newest ones rather than the
        # oldest - on a shared/high-traffic seed mailbox this can otherwise
        # mean fetching thousands of unrelated headers one folder at a time.
        uid_strs = uid_strs[-MAX_CANDIDATES_PER_FOLDER:]
        truncated = True

    if debug is not None:
        debug["raw_uid_count"] = len(uids)
        debug["candidates_fetched"] = len(uid_strs)
        debug["candidates_truncated"] = truncated
        debug["dropped_no_date_header"] = 0
        debug["dropped_date_parse_error"] = 0
        debug["dropped_older_than_cutoff"] = 0
        debug["dropped_sender_mismatch"] = 0
        debug["fetch_errors"] = []
        debug["fetch_batches"] = 0
        debug["sample_from_headers"] = []  # actual From values seen, for eyeballing the real domain
        debug["aborted_time_budget"] = False

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=lookback_seconds)
    sender_needle = sender.strip().lower()
    matches = []
    SAMPLE_CAP = 15

    # Batch header fetches instead of one IMAP round-trip per message -
    # a mailbox with real volume in the SINCE window made the old
    # one-uid-at-a-time loop slow enough to look like a hang.
    BATCH_SIZE = 200
    start_time = time.monotonic()

    for i in range(0, len(uid_strs), BATCH_SIZE):
        if time.monotonic() - start_time > FOLDER_TIME_BUDGET:
            if debug is not None:
                debug["aborted_time_budget"] = True
            break
        batch = uid_strs[i:i + BATCH_SIZE]
        uid_set = ",".join(batch)
        if debug is not None:
            debug["fetch_batches"] += 1
        try:
            typ, hdr_data = imap.uid(
                "fetch", uid_set, "(UID BODY.PEEK[HEADER.FIELDS (DATE SUBJECT FROM)])"
            )
        except Exception as e:
            if debug is not None:
                debug["fetch_errors"].append(f"batch {uid_set[:40]}...: {type(e).__name__}: {e}")
            continue
        if typ != "OK" or not hdr_data:
            if debug is not None:
                debug["fetch_errors"].append(f"batch {uid_set[:40]}...: fetch typ={typ}")
            continue

        for item in hdr_data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue  # skip the closing b')' entries IMAP includes per message
            meta, raw_header = item[0], item[1]
            uid_match = re.search(rb"UID (\d+)", meta)
            uid = uid_match.group(1).decode() if uid_match else "?"
            try:
                msg = email.message_from_bytes(raw_header)

                from_ = msg.get("From", "")
                if debug is not None and len(debug["sample_from_headers"]) < SAMPLE_CAP:
                    if from_ not in debug["sample_from_headers"]:
                        debug["sample_from_headers"].append(from_)
                if sender_needle not in from_.lower():
                    if debug is not None:
                        debug["dropped_sender_mismatch"] += 1
                    continue

                date_hdr = msg.get("Date")
                if not date_hdr:
                    if debug is not None:
                        debug["dropped_no_date_header"] += 1
                    continue
                try:
                    dt = email.utils.parsedate_to_datetime(date_hdr)
                except Exception as e:
                    if debug is not None:
                        debug["dropped_date_parse_error"] += 1
                        debug["fetch_errors"].append(f"uid {uid}: bad Date header {date_hdr!r} ({e})")
                    continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    if debug is not None:
                        debug["dropped_older_than_cutoff"] += 1
                    continue
                subject = msg.get("Subject", "(no subject)")
                matches.append({"uid": uid.encode(), "subject": subject, "from": from_, "date": dt})
            except Exception as e:
                if debug is not None:
                    debug["fetch_errors"].append(f"uid {uid}: {type(e).__name__}: {e}")
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
debug_mode = st.checkbox("Show debug info (raw IMAP search counts / drop reasons)", value=False)
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
                progress.progress((i) / len(accounts), text=f"Checking {acc_email}: connecting...")
                account_result = {"email": acc_email, "status": "ok", "messages": [], "debug": {}}

                inbox_debug = {}
                inbox_conn = open_mailbox(acc_email, acc_pass, "INBOX", debug=inbox_debug)
                account_result["debug"]["inbox"] = inbox_debug
                if not inbox_conn:
                    account_result["status"] = "auth_failed"
                    results.append(account_result)
                    continue

                progress.progress((i) / len(accounts), text=f"Checking {acc_email}: searching inbox...")
                inbox_search_debug = {}
                for m in search_recent_from(inbox_conn, sender, LOOKBACK_SECONDS, debug=inbox_search_debug):
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
                account_result["debug"]["inbox_search"] = inbox_search_debug
                try:
                    inbox_conn.logout()
                except Exception:
                    pass

                spam_folder = find_spam_folder(acc_email, acc_pass)
                account_result["debug"]["spam_folder_found"] = spam_folder
                if spam_folder:
                    progress.progress((i) / len(accounts), text=f"Checking {acc_email}: searching {spam_folder}...")
                    spam_debug = {}
                    spam_conn = open_mailbox(acc_email, acc_pass, spam_folder, debug=spam_debug)
                    account_result["debug"]["spam"] = spam_debug
                    if spam_conn:
                        spam_search_debug = {}
                        for m in search_recent_from(spam_conn, sender, LOOKBACK_SECONDS, debug=spam_search_debug):
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
                        account_result["debug"]["spam_search"] = spam_search_debug
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
                    if debug_mode:
                        st.json(acct.get("debug", {}))
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
