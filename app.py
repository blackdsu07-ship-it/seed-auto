"""
Seed Unread Count Checker (Yahoo IMAP) — Streamlit version
---------------------------------------------------------------
Upload accounts (email,password per line), give a sender address/domain to
track. For each account, reports how many UNREAD messages from that sender
are sitting in Inbox and in Spam/Bulk right now.

The count pass is intentionally light: IMAP SEARCH UNSEEN is a single
server-side operation, and we only fetch the From header for the (usually
small) set of unread messages to match the sender - never the whole mailbox,
never the message bodies, no link-clicking.

Per account, once you see a nonzero count, an "Open email & click links"
button fetches the actual matched message(s) for that account, extracts
links from the body, and clicks them with a real, VISIBLE Chromium window
(Playwright, headless=False) - only for that one account, only on demand.

Optional: give a "Link to match" value. When set, only the first link in
each message that CONTAINS that value (substring match, not exact) is
clicked - every other link in the message is skipped. After a message's
matched link is clicked, that message is marked as read (\\Seen) on the
server. If no target is given, the old behavior applies: every link gets
clicked and messages aren't marked read.

Run this on your own PC (not Streamlit Community Cloud or any other
headless server) if you want to actually see the browser: a visible
browser window needs a real display, which a cloud server doesn't have.
Locally:
  pip install streamlit requests playwright
  playwright install chromium
  streamlit run seed_unread_checker.py
  - Yahoo accounts need an APP PASSWORD (Account Security > Generate app
    password), not the normal login password.
"""

import email
import imaplib
import re
import subprocess
import sys
import time
from datetime import datetime

import requests
import streamlit as st

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

IMAP_HOST = "imap.mail.yahoo.com"
IMAP_PORT = 993
IMAP_TIMEOUT = 20  # seconds - socket-level timeout so a stuck connection can't hang forever
MAX_CANDIDATES_PER_FOLDER = 400  # cap header fetches if a folder has huge unread volume
FOLDER_TIME_BUDGET = 60  # seconds - abort a folder's fetch loop past this, move on
SPAM_FOLDER_CANDIDATES = ["Bulk", "Bulk Mail", "Spam", "Junk"]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

st.set_page_config(page_title="Seed Unread Count Checker", layout="wide")


# ---------------------------------------------------------------------------
# Playwright setup (installed lazily - only when a click action first needs it)
# ---------------------------------------------------------------------------

def ensure_playwright_browser():
    """Installs the Chromium build Playwright drives. Uses `sys.executable -m
    playwright` instead of a bare `playwright` command, since on Windows the
    `playwright` console script often isn't on PATH even when the package is
    installed in the active venv - a bare `subprocess.run(["playwright", ...])`
    just raises FileNotFoundError, which looks identical to "nothing went
    wrong" if you swallow it. Returns (ok, output) so the caller can surface
    a real reason instead of guessing.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=180,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output.strip()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Account / IMAP helpers
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


def open_mailbox(email_addr, password, folder="INBOX", debug=None, readonly=True):
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT)
        imap.login(email_addr, password)
        typ, sel_data = imap.select(folder, readonly=readonly)
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


def count_unseen_from(imap, sender, debug=None):
    """Returns (matched_count, total_unseen_count, matched_uids).

    total_unseen_count is the raw UNSEEN count in the folder (free, from the
    same SEARCH). matched_count is how many of those unseen messages have
    `sender` as a substring of their From header. matched_uids is the list
    of UID strings that matched, so a later on-demand action can fetch just
    those specific messages without searching again.
    """
    try:
        typ, data = imap.uid("search", None, "UNSEEN")
    except Exception as e:
        if debug is not None:
            debug["search_error"] = f"{type(e).__name__}: {e}"
        return 0, 0, []

    if debug is not None:
        debug["search_typ"] = typ

    if typ != "OK" or not data or not data[0]:
        return 0, 0, []

    uids = data[0].split()
    total_unseen = len(uids)
    uid_strs = [u.decode() if isinstance(u, bytes) else str(u) for u in uids]

    truncated = False
    if len(uid_strs) > MAX_CANDIDATES_PER_FOLDER:
        uid_strs = uid_strs[-MAX_CANDIDATES_PER_FOLDER:]
        truncated = True

    if debug is not None:
        debug["total_unseen"] = total_unseen
        debug["candidates_fetched"] = len(uid_strs)
        debug["candidates_truncated"] = truncated
        debug["fetch_errors"] = []
        debug["fetch_batches"] = 0
        debug["aborted_time_budget"] = False
        debug["sample_from_headers"] = []

    sender_needle = sender.strip().lower()
    matched_uids = []
    SAMPLE_CAP = 15
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
            typ, hdr_data = imap.uid("fetch", uid_set, "(UID BODY.PEEK[HEADER.FIELDS (FROM)])")
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
                continue  # skip closing b')' entries IMAP includes per message
            meta, raw_header = item[0], item[1]
            uid_match = re.search(rb"UID (\d+)", meta)
            uid = uid_match.group(1).decode() if uid_match else None
            try:
                msg = email.message_from_bytes(raw_header)
                from_ = msg.get("From", "")
                if debug is not None and len(debug["sample_from_headers"]) < SAMPLE_CAP:
                    if from_ not in debug["sample_from_headers"]:
                        debug["sample_from_headers"].append(from_)
                if sender_needle in from_.lower() and uid:
                    matched_uids.append(uid)
            except Exception as e:
                if debug is not None:
                    debug["fetch_errors"].append(f"{type(e).__name__}: {e}")
                continue

    return len(matched_uids), total_unseen, matched_uids


# ---------------------------------------------------------------------------
# Open-and-click helpers (only run on demand, per account)
# ---------------------------------------------------------------------------

def get_message_full(imap, uid):
    """Fetch subject/from/body for one message without marking it seen."""
    try:
        typ, msg_data = imap.uid("fetch", uid, "(BODY.PEEK[])")
        if typ != "OK" or not msg_data or not msg_data[0]:
            return None
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        subject = msg.get("Subject", "(no subject)")
        from_ = msg.get("From", "")
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
        return {"subject": subject, "from": from_, "body": "\n".join(parts)}
    except Exception:
        return None


def extract_links(body):
    links = re.findall(r'https?://[^\s"\'<>\)]+', body, flags=re.IGNORECASE)
    return list(dict.fromkeys(links))  # dedupe, preserve order


def select_links_to_click(links, target_link):
    """Given all links found in a message body (in document order) and an
    optional target substring, return the list of links that should actually
    be clicked.

    - No target -> click every link (old behavior).
    - Target given -> substring match (case-insensitive, not exact) against
      each link; return only the FIRST match, as a single-item list. Empty
      list if nothing matches.
    """
    if not target_link:
        return links
    needle = target_link.strip().lower()
    if not needle:
        return links
    for link in links:
        if needle in link.lower():
            return [link]
    return []


def mark_seen(conn, uid):
    try:
        conn.uid("store", uid, "+FLAGS", "(\\Seen)")
        return True
    except Exception:
        return False


def click_via_requests(url):
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20, allow_redirects=True)
        return r.status_code < 400
    except Exception:
        return False


def open_and_click_for_account(acc_email, acc_pass, inbox_uids, spam_folder, spam_uids,
                                target_link=None, on_progress=None, on_browser_status=None):
    """Fetches only the specific matched messages for this account (Inbox +
    Spam/Bulk) and clicks the links inside each using one real, VISIBLE
    Chromium window (headless=False) for the whole run - not headless, so
    this needs a display and only works when run on your own PC, not on a
    headless server like Streamlit Community Cloud. Falls back to a plain
    request only if the browser navigation itself fails, or if Playwright
    isn't installed at all. Runs only when the user presses the button for
    this account.

    If `target_link` is given, only the first link in each message that
    contains that value (substring match) is clicked, and the message is
    then marked as read on the server. Every other link in the message is
    left alone. If `target_link` is not given, every link is clicked (old
    behavior) and nothing is marked read.

    on_progress(opened, total_messages, clicked), if given, is called after
    every message is opened and after every link click so the caller can
    show live counts.

    on_browser_status(stage, ok, detail), if given, is called twice, BEFORE
    any message processing starts, so the caller can show the launch result
    immediately instead of waiting for the whole run to finish:
      - ("install", True/False, output text)
      - ("launch", True/False, error text or "")
    """
    messages = []
    total_msgs = len(inbox_uids) + len(spam_uids)
    counts = {"opened": 0, "clicked": 0}
    status = {"used_headed_browser": False, "install_ok": None, "install_output": "", "launch_error": None}

    def run(page):
        def click_one(url):
            if page is not None:
                try:
                    page.goto(url, wait_until="networkidle", timeout=25000)
                    page.wait_for_timeout(1500)
                    method = "browser"
                except Exception as e:
                    status.setdefault("nav_errors", []).append(f"{url[:80]}: {type(e).__name__}: {e}")
                    method = "http" if click_via_requests(url) else "failed"
            else:
                method = "http" if click_via_requests(url) else "failed"
            counts["clicked"] += 1
            if on_progress:
                on_progress(counts["opened"], total_msgs, counts["clicked"])
            return method

        def process_folder(folder_label, conn, uids):
            for uid in uids:
                m = get_message_full(conn, uid)
                if not m:
                    continue
                counts["opened"] += 1
                if on_progress:
                    on_progress(counts["opened"], total_msgs, counts["clicked"])

                all_links = extract_links(m["body"])
                links_to_click = select_links_to_click(all_links, target_link)
                clicks = [{"url": link, "method": click_one(link)} for link in links_to_click]

                no_match = bool(target_link) and not clicks
                marked_read = False
                if clicks:
                    marked_read = mark_seen(conn, uid)

                messages.append({
                    "folder": folder_label,
                    "subject": m["subject"],
                    "from": m["from"],
                    "links": clicks,
                    "no_match": no_match,
                    "marked_read": marked_read,
                })

        # Open with readonly=False here (unlike the count pass) so a
        # successful click can flip \Seen on the message.
        if inbox_uids:
            conn = open_mailbox(acc_email, acc_pass, "INBOX", readonly=False)
            if conn:
                process_folder("Inbox", conn, inbox_uids)
                try:
                    conn.logout()
                except Exception:
                    pass

        if spam_folder and spam_uids:
            conn = open_mailbox(acc_email, acc_pass, spam_folder, readonly=False)
            if conn:
                process_folder(spam_folder, conn, spam_uids)
                try:
                    conn.logout()
                except Exception:
                    pass

    if PLAYWRIGHT_AVAILABLE:
        install_ok, install_output = ensure_playwright_browser()
        status["install_ok"] = install_ok
        status["install_output"] = install_output
        if on_browser_status:
            on_browser_status("install", install_ok, install_output)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False, args=["--start-maximized"])
                page = browser.new_page(user_agent=USER_AGENT, no_viewport=True)
                page.bring_to_front()
                status["used_headed_browser"] = True
                if on_browser_status:
                    on_browser_status("launch", True, "")
                run(page)
                # Keep the window up for a moment after the last click so it
                # doesn't just flash and vanish before you can see it.
                page.wait_for_timeout(2000)
                page.close()
                browser.close()
        except Exception as e:
            # Browser binary missing/failed to launch even though the package
            # imported fine - fall back to requests-only for this run rather
            # than losing all progress made so far, but keep the real reason
            # instead of failing silently.
            status["launch_error"] = f"{type(e).__name__}: {e}"
            if on_browser_status:
                on_browser_status("launch", False, status["launch_error"])
            run(None)
    else:
        status["launch_error"] = "playwright package not importable in this environment"
        if on_browser_status:
            on_browser_status("launch", False, status["launch_error"])
        run(None)

    return messages, status


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("Seed Unread Count Checker")
st.caption("Yahoo IMAP · unread-from-sender count in Inbox + Spam/Bulk per account")

if not PLAYWRIGHT_AVAILABLE:
    st.warning(
        "⚠️ Playwright isn't installed in this deployment - \"Open email & click links\" "
        "will fall back to plain HTTP requests instead of a real browser. To fix: make sure "
        "requirements.txt in your repo has `streamlit`, `requests`, and `playwright` on separate "
        "lines, then use Manage app → Reboot app (a plain redeploy can reuse a cached environment "
        "and skip picking up the new package)."
    )

accounts_file = st.file_uploader("Accounts file (CSV/TXT — one \"email,password\" per line)", type=["csv", "txt"])
sender = st.text_input("Sender address or domain to track", placeholder="yourdomain.com")
target_link = st.text_input(
    "Link to match (optional)",
    placeholder="leave blank to click every link",
    help="If set, only the first link in each message that CONTAINS this value is clicked "
         "(substring match, not exact). That message is then marked as read. All other links "
         "are skipped. Leave blank to click every link (old behavior, nothing marked read).",
)
debug_mode = st.checkbox("Show debug info", value=False)
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
            grand_inbox_matched = 0
            grand_spam_matched = 0

            progress = st.progress(0.0, text="Starting...")
            for i, (acc_email, acc_pass) in enumerate(accounts):
                progress.progress(i / len(accounts), text=f"Checking {acc_email}: inbox...")
                acct = {"email": acc_email, "password": acc_pass, "status": "ok", "debug": {}}

                inbox_debug = {}
                inbox_conn = open_mailbox(acc_email, acc_pass, "INBOX", debug=inbox_debug)
                acct["debug"]["inbox"] = inbox_debug
                if not inbox_conn:
                    acct["status"] = "auth_failed"
                    results.append(acct)
                    continue

                inbox_matched, inbox_total_unseen, inbox_uids = count_unseen_from(inbox_conn, sender, debug=inbox_debug)
                acct["inbox_matched"] = inbox_matched
                acct["inbox_total_unseen"] = inbox_total_unseen
                acct["inbox_uids"] = inbox_uids
                try:
                    inbox_conn.logout()
                except Exception:
                    pass

                spam_folder = find_spam_folder(acc_email, acc_pass)
                acct["spam_folder"] = spam_folder
                acct["spam_matched"] = 0
                acct["spam_total_unseen"] = 0
                acct["spam_uids"] = []
                if spam_folder:
                    progress.progress(i / len(accounts), text=f"Checking {acc_email}: {spam_folder}...")
                    spam_debug = {}
                    spam_conn = open_mailbox(acc_email, acc_pass, spam_folder, debug=spam_debug)
                    acct["debug"]["spam"] = spam_debug
                    if spam_conn:
                        spam_matched, spam_total_unseen, spam_uids = count_unseen_from(spam_conn, sender, debug=spam_debug)
                        acct["spam_matched"] = spam_matched
                        acct["spam_total_unseen"] = spam_total_unseen
                        acct["spam_uids"] = spam_uids
                        try:
                            spam_conn.logout()
                        except Exception:
                            pass

                grand_inbox_matched += acct["inbox_matched"]
                grand_spam_matched += acct["spam_matched"]
                results.append(acct)

            progress.progress(1.0, text="Done")
            progress.empty()

            # Persist across reruns triggered by per-account "open & click" buttons
            st.session_state["results"] = results
            st.session_state["grand_inbox_matched"] = grand_inbox_matched
            st.session_state["grand_spam_matched"] = grand_spam_matched

# Render from session_state (not just right after Run Check) so per-account
# buttons below don't wipe the results table on their own rerun.
if "results" in st.session_state:
    results = st.session_state["results"]
    grand_inbox_matched = st.session_state["grand_inbox_matched"]
    grand_spam_matched = st.session_state["grand_spam_matched"]
    total_matched = grand_inbox_matched + grand_spam_matched

    c1, c2, c3 = st.columns(3)
    c1.metric("Unread in Inbox", grand_inbox_matched)
    c2.metric("Unread in Spam/Bulk", grand_spam_matched)
    c3.metric("Total Unread (from sender)", total_matched)

    st.divider()

    for acct in results:
        if acct["status"] != "ok":
            st.error(f"{acct['email']}: {acct['status']}")
            if debug_mode:
                st.json(acct["debug"])
            continue

        col_label, col_button = st.columns([4, 1])
        with col_label:
            st.markdown(
                f"**{acct['email']}** — Inbox: {acct['inbox_matched']} unread · "
                f"{acct['spam_folder'] or 'Spam'}: {acct['spam_matched']} unread"
            )
        has_matches = bool(acct["inbox_uids"] or acct["spam_uids"])
        with col_button:
            click_pressed = st.button(
                "Open email & click links",
                key=f"open_click_{acct['email']}",
                disabled=not has_matches,
            )

        if click_pressed:
            total_msgs = len(acct["inbox_uids"]) + len(acct["spam_uids"])
            browser_status_placeholder = st.empty()
            status_placeholder = st.empty()
            progress_bar = st.progress(0.0)
            browser_status_placeholder.markdown("⏳ Checking Chromium install...")
            status_placeholder.markdown(f"📨 Opened **0/{total_msgs}** emails · 🔗 Clicked **0** links")

            def progress_cb(opened, total, clicked):
                frac = (opened / total) if total else 1.0
                progress_bar.progress(min(frac, 1.0))
                status_placeholder.markdown(
                    f"📨 Opened **{opened}/{total}** emails · 🔗 Clicked **{clicked}** links"
                )

            def browser_status_cb(stage, ok, detail):
                if stage == "install":
                    browser_status_placeholder.markdown(
                        "✅ Chromium install OK" if ok else f"❌ Chromium install failed: `{detail[:300]}`"
                    )
                elif stage == "launch":
                    browser_status_placeholder.markdown(
                        "🖥️ Browser window launched — look for it now (check your taskbar too)"
                        if ok else f"❌ Browser launch failed: `{detail}`"
                    )

            messages, browser_status = open_and_click_for_account(
                acct["email"], acct["password"],
                acct["inbox_uids"], acct["spam_folder"], acct["spam_uids"],
                target_link=target_link.strip() if target_link else None,
                on_progress=progress_cb,
                on_browser_status=browser_status_cb,
            )
            progress_bar.empty()
            total_clicked = sum(len(m["links"]) for m in messages)
            status_placeholder.markdown(
                f"✅ Done — opened **{len(messages)}/{total_msgs}** emails, clicked **{total_clicked}** links"
            )
            if not browser_status["used_headed_browser"]:
                st.error(
                    "A visible browser window never opened for this run — links were clicked via "
                    "plain HTTP requests instead. Reason: "
                    f"`{browser_status['launch_error']}`"
                )
                if browser_status["install_ok"] is False:
                    st.caption(f"Chromium install also failed: {browser_status['install_output'][:500]}")
            if browser_status.get("nav_errors"):
                with st.expander(f"{acct['email']} — browser navigation errors"):
                    for err in browser_status["nav_errors"]:
                        st.caption(err)
            st.session_state[f"click_result_{acct['email']}"] = messages

        result_key = f"click_result_{acct['email']}"
        if result_key in st.session_state:
            messages = st.session_state[result_key]
            if not messages:
                st.caption("No matched messages could be opened.")
            for msg in messages:
                st.markdown(f"🟢 **{msg['folder']}** — {msg['subject']}  \nFrom: {msg['from']}")
                if msg["links"]:
                    for lc in msg["links"]:
                        icon = {"browser": "🌐", "http": "✅", "failed": "❌"}[lc["method"]]
                        st.caption(f"{icon} {lc['method']}: {lc['url'][:80]}")
                    if msg.get("marked_read"):
                        st.caption("✔️ marked as read")
                elif msg.get("no_match"):
                    st.caption("no link matching target found in this message")
                else:
                    st.caption("no links found in this message")

        if debug_mode:
            with st.expander(f"{acct['email']} — debug"):
                st.json(acct["debug"])

        st.markdown("---")

    csv_lines = ["email,inbox_unread_matched,spam_unread_matched,inbox_total_unseen,spam_total_unseen"]
    for acct in results:
        if acct["status"] != "ok":
            csv_lines.append(f'{acct["email"]},AUTH_FAILED,,,')
            continue
        csv_lines.append(
            f'{acct["email"]},{acct["inbox_matched"]},{acct["spam_matched"]},'
            f'{acct["inbox_total_unseen"]},{acct["spam_total_unseen"]}'
        )
    st.download_button(
        "Download results (CSV)",
        data="\n".join(csv_lines),
        file_name=f"unread_counts_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
    )
