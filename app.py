"""
Seed Unread Count Checker (Yahoo IMAP) — Streamlit version
---------------------------------------------------------------
Upload accounts (email,password per line), give a sender address/domain to
track. For each account, reports how many UNREAD messages from that sender
are sitting in Inbox and in Spam/Bulk right now.

No link-clicking, no full-header archival - just counts, and fast:
IMAP SEARCH UNSEEN is a single lightweight server-side operation, and we
only fetch the From header for the (usually small) set of unread messages
to match the sender - never the whole mailbox.

Deploy on Streamlit Community Cloud:
  - requirements.txt -> streamlit
  - Yahoo accounts need an APP PASSWORD (Account Security > Generate app
    password), not the normal login password.
"""

import email
import imaplib
import re
import time
from datetime import datetime

import streamlit as st

IMAP_HOST = "imap.mail.yahoo.com"
IMAP_PORT = 993
IMAP_TIMEOUT = 20  # seconds - socket-level timeout so a stuck connection can't hang forever
MAX_CANDIDATES_PER_FOLDER = 400  # cap header fetches if a folder has huge unread volume
FOLDER_TIME_BUDGET = 60  # seconds - abort a folder's fetch loop past this, move on
SPAM_FOLDER_CANDIDATES = ["Bulk", "Bulk Mail", "Spam", "Junk"]

st.set_page_config(page_title="Seed Unread Count Checker", layout="wide")


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


def count_unseen_from(imap, sender, debug=None):
    """Returns (matched_count, total_unseen_count).

    total_unseen_count is the raw UNSEEN count in the folder (free, from the
    same SEARCH). matched_count is how many of those unseen messages have
    `sender` as a substring of their From header - fetched only for the
    unseen set, never the whole mailbox.
    """
    try:
        typ, data = imap.uid("search", None, "UNSEEN")
    except Exception as e:
        if debug is not None:
            debug["search_error"] = f"{type(e).__name__}: {e}"
        return 0, 0

    if debug is not None:
        debug["search_typ"] = typ

    if typ != "OK" or not data or not data[0]:
        return 0, 0

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
    matched = 0
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
            typ, hdr_data = imap.uid("fetch", uid_set, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
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
            raw_header = item[1]
            try:
                msg = email.message_from_bytes(raw_header)
                from_ = msg.get("From", "")
                if debug is not None and len(debug["sample_from_headers"]) < SAMPLE_CAP:
                    if from_ not in debug["sample_from_headers"]:
                        debug["sample_from_headers"].append(from_)
                if sender_needle in from_.lower():
                    matched += 1
            except Exception as e:
                if debug is not None:
                    debug["fetch_errors"].append(f"{type(e).__name__}: {e}")
                continue

    return matched, total_unseen


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("Seed Unread Count Checker")
st.caption("Yahoo IMAP · unread-from-sender count in Inbox + Spam/Bulk per account")

accounts_file = st.file_uploader("Accounts file (CSV/TXT — one \"email,password\" per line)", type=["csv", "txt"])
sender = st.text_input("Sender address or domain to track", placeholder="yourdomain.com")
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
                acct = {"email": acc_email, "status": "ok", "debug": {}}

                inbox_debug = {}
                inbox_conn = open_mailbox(acc_email, acc_pass, "INBOX", debug=inbox_debug)
                acct["debug"]["inbox"] = inbox_debug
                if not inbox_conn:
                    acct["status"] = "auth_failed"
                    results.append(acct)
                    continue

                inbox_matched, inbox_total_unseen = count_unseen_from(inbox_conn, sender, debug=inbox_debug)
                acct["inbox_matched"] = inbox_matched
                acct["inbox_total_unseen"] = inbox_total_unseen
                try:
                    inbox_conn.logout()
                except Exception:
                    pass

                spam_folder = find_spam_folder(acc_email, acc_pass)
                acct["spam_folder"] = spam_folder
                acct["spam_matched"] = 0
                acct["spam_total_unseen"] = 0
                if spam_folder:
                    progress.progress(i / len(accounts), text=f"Checking {acc_email}: {spam_folder}...")
                    spam_debug = {}
                    spam_conn = open_mailbox(acc_email, acc_pass, spam_folder, debug=spam_debug)
                    acct["debug"]["spam"] = spam_debug
                    if spam_conn:
                        spam_matched, spam_total_unseen = count_unseen_from(spam_conn, sender, debug=spam_debug)
                        acct["spam_matched"] = spam_matched
                        acct["spam_total_unseen"] = spam_total_unseen
                        try:
                            spam_conn.logout()
                        except Exception:
                            pass

                grand_inbox_matched += acct["inbox_matched"]
                grand_spam_matched += acct["spam_matched"]
                results.append(acct)

            progress.progress(1.0, text="Done")
            progress.empty()

            total_matched = grand_inbox_matched + grand_spam_matched
            c1, c2, c3 = st.columns(3)
            c1.metric("Unread in Inbox", grand_inbox_matched)
            c2.metric("Unread in Spam/Bulk", grand_spam_matched)
            c3.metric("Total Unread (from sender)", total_matched)

            st.divider()

            rows = []
            for acct in results:
                if acct["status"] != "ok":
                    st.error(f"{acct['email']}: {acct['status']}")
                    if debug_mode:
                        st.json(acct["debug"])
                    continue
                rows.append({
                    "Account": acct["email"],
                    "Unread (Inbox)": acct["inbox_matched"],
                    "Unread (Spam/Bulk)": acct["spam_matched"],
                    "Total unread (all senders, Inbox)": acct["inbox_total_unseen"],
                    "Total unread (all senders, Spam)": acct["spam_total_unseen"],
                })
                if debug_mode:
                    with st.expander(f"{acct['email']} — debug"):
                        st.json(acct["debug"])

            st.dataframe(rows, use_container_width=True, hide_index=True)

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
