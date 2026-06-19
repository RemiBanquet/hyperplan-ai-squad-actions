#!/usr/bin/env python3
"""
Agent 5 Engagement Monitor, GitHub Actions runtime (triage mode).

What it does, per run (cron every 4h + manual dispatch):
  1. Lemlist REST: pull reply activities for the squad campaigns in
     config.json since LOOKBACK_HOURS.
  2. Dedup by reply id against the Notion Replies DB.
  3. Classify each new reply: deterministic safety net + optional
     Anthropic LLM pass + override hierarchy (classify.py).
  4. Notion REST: write the classified row to the Replies DB with
     Gate 5 Decision = Pending, linked to the Lead row when found.
  5. Telegram: immediate alert for Critical, run summary when there
     is anything new.

What it deliberately does NOT do (policy, Rémi rulings 2026-06-09 +
architecture spec):
  - Never sends or drafts outbound. Drafting stays in the Cowork
    Agent 5 sweep at Gate 5. TRIAGE ONLY.
  - Never pauses campaigns, leads, or sequences. Opt-out and hostile
    replies are flagged + alerted; execution stays with Rémi/Cowork.
  - Never touches campaigns outside config.json (squad scope only).

Env (GitHub Actions secrets):
  LEMLIST_API_KEY     required
  NOTION_TOKEN        required
  ANTHROPIC_API_KEY   optional (without it: rule-only, non-Critical
                      replies land as Ambiguous for Gate 5)
  BOT_TOKEN, CHAT_ID  optional (Telegram; stub-logs if absent)
  LOOKBACK_HOURS      optional, default 6 (cron 4h + 2h overlap;
                      dedup makes the overlap safe)

Modes:
  python3 main.py             live run
  python3 main.py --dry-run   pull + classify + print, NO Notion writes,
                              NO Telegram. Use this first to verify the
                              Lemlist activity payload shape (see README).
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

from classify import classify, Classification

HERE = os.path.dirname(os.path.abspath(__file__))
NOTION_API = "https://api.notion.com/v1"
LEMLIST_API = "https://api.lemlist.com/api"
NOTION_VERSION = "2022-06-28"


def load_config() -> Dict[str, Any]:
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


# --------- HTTP helpers (stdlib only) ---------

USER_AGENT = "Mozilla/5.0 (compatible; hyperplan-ai-squad-agent5/1.0; +https://hyperplan.ag)"


def _request(url: str, headers: Dict[str, str], method: str = "GET",
             payload: Optional[Dict[str, Any]] = None, retries: int = 2) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    # Cloudflare blocks urllib's default "Python-urllib/x.y" UA with error
    # 1010 (seen on every Lemlist call from GitHub runners, 2026-06-09).
    headers = dict(headers)
    headers.setdefault("User-Agent", USER_AGENT)
    headers.setdefault("Accept", "application/json")
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace").strip()
            # A 2xx with an empty body (e.g. 204, or an empty inbox response) is
            # not an error: return None and let callers degrade. fetch_body_from_inbox
            # already treats a non-dict/None result as "no body".
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                # 2xx but the body is not JSON (empty-ish, HTML, Cloudflare splash).
                # Retry once or twice, then raise a CATCHABLE RuntimeError instead of
                # a bare JSONDecodeError, so callers that already guard RuntimeError
                # (fetch_body_from_inbox) degrade gracefully instead of crashing the run.
                last = f"non-JSON body (len {len(raw)}): {raw[:200]}"
                if attempt < retries:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise RuntimeError(f"{method} {url} returned non-JSON after retries: {last}")
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}"
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2.0 * (attempt + 1))
                continue
            raise RuntimeError(f"{method} {url} failed: {last}")
        except urllib.error.URLError as e:
            last = f"network: {e.reason}"
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"{method} {url} failed after retries: {last}")


def lemlist_get(path: str, api_key: str, params: Optional[Dict[str, Any]] = None) -> Any:
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    auth = base64.b64encode(f":{api_key}".encode()).decode()
    # 600ms self-throttle per the shared rate-limiter convention (§17).
    time.sleep(0.6)
    return _request(f"{LEMLIST_API}{path}{qs}", {"Authorization": f"Basic {auth}"})


def notion_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


# --------- Reply body extraction (IL-36) ---------
# The first live reply (act_RTXdot9Twnih7jzYz, 2026-06-11) arrived as
# Outlook-mobile HTML with no usable plain-text field; the runner stored an
# empty body and classify() bucketed blank content as Ambiguous. Fix, 3
# layers: (1) try every known body field on the activity payload, (2)
# convert HTML to text (stdlib HTMLParser) and strip quoted history, (3) if
# still empty, fetch the conversation from the inbox API. A blank result
# after all 3 is an EXTRACTION FAILURE, not a classification: the row is
# written without a bucket and marked for manual fetch.
# Golden test: python3 main.py --selftest (fixtures/outlook-mobile-reply.html
# is the exact live payload).

BODY_FIELD_CANDIDATES = ["text", "body", "message", "bodyHtml", "html", "content", "snippet"]

QUOTE_MARKERS = [
    re.compile(r"^\s*From:\s.+$", re.IGNORECASE),
    re.compile(r"^\s*-{3,}\s*Original Message\s*-{3,}", re.IGNORECASE),
    re.compile(r"^\s*On .{4,140} wrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*Le .{4,140} a écrit\s*:\s*$", re.IGNORECASE),
]


class _HTMLText(HTMLParser):
    SKIP = {"style", "script", "head", "title"}
    BREAKS = {"br", "p", "div", "tr", "li", "blockquote", "hr", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    """HTML to plain text. Non-HTML strings pass through untouched."""
    if "<" not in raw or ">" not in raw:
        return raw.strip()
    parser = _HTMLText()
    try:
        parser.feed(raw)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # malformed HTML: a crude tag strip beats a crash
        text = re.sub(r"<[^>]+>", "\n", raw)
    text = text.replace("\xa0", " ")
    lines = [ln.strip() for ln in text.splitlines()]
    out: List[str] = []
    for ln in lines:
        if ln:
            out.append(ln)
        elif out and out[-1]:
            out.append("")
    return "\n".join(out).strip()


def strip_quoted_history(text: str) -> str:
    """Cut at the first quoted-history marker (Outlook 'From:' block,
    'On ... wrote:', '-----Original Message-----'). Keeps the lead's new
    content + signature, drops our own quoted thread so classification
    runs on what the lead actually wrote. Never cuts to nothing."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if any(m.match(ln) for m in QUOTE_MARKERS):
            head = "\n".join(lines[:i]).strip()
            if head:
                return head
    return text


def extract_body(activity: Dict[str, Any], preferred_field: str) -> str:
    candidates = [preferred_field] + [f for f in BODY_FIELD_CANDIDATES if f != preferred_field]
    for key in candidates:
        val = activity.get(key)
        if isinstance(val, str) and val.strip():
            text = strip_quoted_history(html_to_text(val))
            if text:
                return text
    return ""


def fetch_body_from_inbox(activity: Dict[str, Any], api_key: str) -> str:
    """Last-resort fallback: pull the conversation and find this activity's
    message. Mirrors the team-inbox API the Cowork sweep uses
    (get_inbox_conversation, keyed by ctc_ contact id). The REST path is
    unverified against public docs (VERIFY on first live use, same
    convention as activity_field_map), so every failure degrades to ''
    (extraction-failed row) instead of raising."""
    contact_id = ""
    for key in ("contactId", "leadId", "_contact", "contact"):
        v = activity.get(key)
        if isinstance(v, str) and v.startswith("ctc_"):
            contact_id = v
            break
    if not contact_id:
        return ""
    act_id = str(activity.get("_id") or activity.get("id") or "")
    for path in (f"/inbox/conversations/{contact_id}", f"/contacts/{contact_id}/conversation"):
        try:
            data = lemlist_get(path, api_key, {"limit": 50})
        except RuntimeError:
            continue
        msgs = data.get("activities") if isinstance(data, dict) else data
        if not isinstance(msgs, list):
            continue
        fallback = ""
        for m in msgs:
            if not isinstance(m, dict):
                continue
            body = next((m[k] for k in BODY_FIELD_CANDIDATES
                         if isinstance(m.get(k), str) and m[k].strip()), "")
            if not body:
                continue
            text = strip_quoted_history(html_to_text(body))
            if not text:
                continue
            if act_id and str(m.get("id") or m.get("_id") or "") == act_id:
                return text
            if m.get("type") in ("emailsReplied", "linkedinReplied") and not fallback:
                fallback = text
        if fallback:
            return fallback
    return ""


# --------- Step 0: discover in-scope campaigns ---------

def discover_campaigns(cfg: Dict[str, Any], api_key: str) -> List[Dict[str, Any]]:
    """Auto-discover squad campaigns by naming convention, so the scope
    scales with every new wave without a config edit.

    Squad convention (enforced by Agent 3/4): campaign names start with
    "A3-" or "A4-". Discovery pulls the full campaign list from Lemlist
    and keeps matches. The static config.json "campaigns" list is merged
    in as a fallback (and as the only source if discovery fails), so the
    8 Wave 1 campaigns stay covered even if the listing endpoint
    misbehaves. Non-squad campaigns (Clémence's, legacy NA, internal)
    never match the prefixes, which keeps the Rémi 2026-06-09 scope
    ruling intact.
    """
    prefixes = tuple(cfg.get("campaign_name_prefixes", ["A3-", "A4-"]))
    discovered: List[Dict[str, Any]] = []
    try:
        offset = 0
        while True:
            page = lemlist_get("/campaigns", api_key, {"limit": 100, "offset": offset})
            if isinstance(page, dict):
                page = page.get("campaigns") or page.get("data") or []
            if not page:
                break
            for c in page:
                name = c.get("name") or ""
                cid = c.get("_id") or c.get("id") or ""
                # Skip archived campaigns: Lemlist's listing returns them
                # (found 2026-06-09: an archived Wave 0 test campaign with an
                # A4- prefix landed in scope). Field name varies, check all.
                if (c.get("archived") or c.get("isArchived")
                        or c.get("status") == "archived"):
                    continue
                if cid and name.startswith(prefixes):
                    discovered.append({"id": cid, "name": name, "account": ""})
            if len(page) < 100:
                break
            offset += 100
    except RuntimeError as e:
        print(f"[warn] campaign discovery failed, using static config list only: {e}",
              file=sys.stderr)

    merged: Dict[str, Dict[str, Any]] = {c["id"]: c for c in cfg.get("campaigns", [])}
    for c in discovered:
        merged.setdefault(c["id"], c)
    scope = list(merged.values())
    print(f"[info] scope: {len(scope)} squad campaigns "
          f"({len(discovered)} discovered by prefix, {len(cfg.get('campaigns', []))} static)",
          file=sys.stderr)
    return scope


# --------- Step 1: pull reply activities ---------

def pull_replies(cfg: Dict[str, Any], api_key: str, lookback_hours: float) -> List[Dict[str, Any]]:
    """Pull reply-type activities for every in-scope campaign.

    NOTE (verify on first --dry-run): the exact activity payload field
    names (reply text, lead email, activity id) come from Lemlist's
    /activities endpoint and may differ from this mapping. The mapper
    below keeps the raw activity under "_raw" so a dry run shows the
    truth; adjust ACTIVITY_FIELD_MAP in config.json, not this code.
    """
    fmap = cfg.get("activity_field_map", {})
    f_id = fmap.get("id", "_id")
    f_email = fmap.get("lead_email", "leadEmail")
    f_name = fmap.get("lead_name", "leadFirstName")
    f_last = fmap.get("lead_last_name", "leadLastName")
    f_text = fmap.get("text", "text")
    f_date = fmap.get("date", "createdAt")

    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    out: List[Dict[str, Any]] = []
    for camp in discover_campaigns(cfg, api_key):
        for activity_type in cfg.get("reply_activity_types", ["emailsReplied", "linkedinReplied"]):
            try:
                acts = lemlist_get("/activities", api_key, {
                    "campaignId": camp["id"], "type": activity_type, "limit": 100,
                })
            except RuntimeError as e:
                print(f"[warn] activities pull failed for {camp['id']}/{activity_type}: {e}",
                      file=sys.stderr)
                continue
            if not isinstance(acts, list):
                acts = acts.get("activities") or acts.get("data") or []
            for a in acts:
                created = a.get(f_date) or ""
                try:
                    ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
                except ValueError:
                    ts = None
                if ts and ts < since:
                    continue
                # IL-36: layered body extraction, never classify a blank.
                body_text = extract_body(a, f_text)
                extraction = "payload"
                if not body_text:
                    body_text = fetch_body_from_inbox(a, api_key)
                    extraction = "inbox-fallback" if body_text else "failed"
                out.append({
                    "reply_id": str(a.get(f_id) or ""),
                    "campaign_id": camp["id"],
                    "campaign_name": camp.get("name", camp["id"]),
                    "channel": "LinkedIn" if "linkedin" in activity_type.lower() else "Email",
                    "lead_email": a.get(f_email) or "",
                    "lead_name": f"{a.get(f_name) or ''} {a.get(f_last) or ''}".strip(),
                    "lead_title": a.get("leadJobTitle") or "",
                    "lead_company": a.get("leadCompanyName") or camp.get("account", ""),
                    "lead_country": "",
                    "original_text": body_text,
                    "extraction": extraction,
                    "received_at": created,
                    "thread_history": "",
                    "_raw": a,
                })
    return [r for r in out if r["reply_id"]]


# --------- Step 2: dedup against Replies DB ---------
# Dedup key is the Lemlist activity id stored in the "Activity ID" rich_text property
# (added 2026-06-11). Titles follow the human convention R-YYYY-MM-DD-NNN and are NOT
# stable dedup keys. Fallback title check kept for rows created before 2026-06-11.

def reply_exists(replies_db: str, reply_id: str, token: str) -> bool:
    data = _request(
        f"{NOTION_API}/databases/{replies_db}/query", notion_headers(token), "POST",
        {"filter": {"property": "Activity ID", "rich_text": {"equals": reply_id}}, "page_size": 1},
    )
    if data.get("results"):
        return True
    data = _request(
        f"{NOTION_API}/databases/{replies_db}/query", notion_headers(token), "POST",
        {"filter": {"property": "Reply ID", "title": {"equals": reply_id}}, "page_size": 1},
    )
    return bool(data.get("results"))


def next_reply_title(replies_db: str, token: str, received_at: str) -> str:
    """R-YYYY-MM-DD-NNN, NNN = count of same-day rows + 1 (date from the reply's received_at)."""
    day = (received_at or "")[:10] or time.strftime("%Y-%m-%d")
    prefix = f"R-{day}-"
    data = _request(
        f"{NOTION_API}/databases/{replies_db}/query", notion_headers(token), "POST",
        {"filter": {"property": "Reply ID", "title": {"starts_with": prefix}}, "page_size": 100},
    )
    n = len(data.get("results") or []) + 1
    return f"{prefix}{n:03d}"


def find_lead(leads_db: str, email: str, token: str) -> Optional[str]:
    if not email:
        return None
    data = _request(
        f"{NOTION_API}/databases/{leads_db}/query", notion_headers(token), "POST",
        {"filter": {"property": "Email", "email": {"equals": email}}, "page_size": 1},
    )
    results = data.get("results") or []
    return results[0]["id"] if results else None


# --------- Step 4: write Replies DB row ---------

EXTRACTION_FAILED_NOTE = (
    "extraction-failed, needs manual fetch (IL-36): activity body empty after "
    "HTML extraction + inbox fallback. Fetch the conversation in Lemlist, "
    "paste the reply into Original reply, then classify at Gate 5. "
    "NOT classified by design: a blank body is a fetch failure, not a bucket."
)


def write_reply_row(cfg: Dict[str, Any], reply: Dict[str, Any], c: Optional[Classification],
                    lead_page_id: Optional[str], token: str) -> str:
    """c=None means extraction failed: the row carries no Classification
    bucket, only the manual-fetch note (IL-36 design rule)."""
    def rt(text: str) -> Dict[str, Any]:
        return {"rich_text": [{"type": "text", "text": {"content": text[:1900]}}]}

    title = next_reply_title(cfg["replies_db"], token, reply.get("received_at") or "")
    props: Dict[str, Any] = {
        "Reply ID": {"title": [{"type": "text", "text": {"content": title}}]},
        "Activity ID": rt(reply["reply_id"]),
        "Channel": {"select": {"name": reply["channel"]}},
        "Classification reasoning": rt(EXTRACTION_FAILED_NOTE if c is None else c.reasoning),
        "Original reply": rt(reply["original_text"]),
        "Gate 5 Decision": {"select": {"name": "Critical-Escalated" if c is not None and c.classification == "Critical" else "Pending"}},
    }
    if c is not None:
        props["Classification"] = {"select": {"name": c.classification}}
        if c.safety_flags:
            props["Safety flags"] = {"multi_select": [{"name": f} for f in sorted(set(c.safety_flags))]}
    if reply.get("received_at"):
        props["Received at"] = {"date": {"start": reply["received_at"]}}
    if lead_page_id:
        props["Lead"] = {"relation": [{"id": lead_page_id}]}

    data = _request(f"{NOTION_API}/pages", notion_headers(token), "POST", {
        "parent": {"database_id": cfg["replies_db"]},
        "properties": props,
    })
    return data.get("id", "")


def set_telegram_flag(page_id: str, token: str) -> None:
    """IL-36 part 2: the 'Telegram alert sent' checkbox is set in the same
    pass that sends the alert, never assumed."""
    try:
        _request(f"{NOTION_API}/pages/{page_id}", notion_headers(token), "PATCH",
                 {"properties": {"Telegram alert sent": {"checkbox": True}}})
    except RuntimeError as e:
        print(f"[warn] could not set Telegram flag on {page_id}: {e}", file=sys.stderr)


# --------- Step 5: Telegram ---------

def telegram_send(text: str) -> bool:
    bot, chat = os.environ.get("BOT_TOKEN"), os.environ.get("CHAT_ID")
    if not bot or not chat:
        print(f"[warn] Telegram creds unset, would have sent:\n{text}", file=sys.stderr)
        return False
    try:
        _request(f"https://api.telegram.org/bot{bot}/sendMessage", {"Content-Type": "application/json"},
                 "POST", {"chat_id": chat, "text": text, "disable_web_page_preview": True})
        return True
    except RuntimeError as e:
        print(f"[warn] Telegram send failed: {e}", file=sys.stderr)
        return False


# --------- Orchestrator ---------

def run(dry_run: bool) -> int:
    cfg = load_config()
    lemlist_key = os.environ.get("LEMLIST_API_KEY")
    notion_token = os.environ.get("NOTION_TOKEN")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    lookback = float(os.environ.get("LOOKBACK_HOURS", "6"))

    if not lemlist_key:
        print(json.dumps({"error": "LEMLIST_API_KEY unset"}), file=sys.stderr)
        return 1
    if not notion_token and not dry_run:
        print(json.dumps({"error": "NOTION_TOKEN unset"}), file=sys.stderr)
        return 1

    replies = pull_replies(cfg, lemlist_key, lookback)
    print(f"[info] pulled {len(replies)} reply activities in the last {lookback}h", file=sys.stderr)

    synth_prefix = cfg.get("synthetic_prefix", "R-2026-05-19")
    new_count = critical_count = 0
    summary_lines: List[str] = []
    run_pages: List[str] = []     # rows written this run (for the summary-alert flag)
    flagged: set = set()          # rows already flagged via a Critical alert

    for reply in replies:
        if reply["reply_id"].startswith(synth_prefix):
            continue
        if not dry_run and reply_exists(cfg["replies_db"], reply["reply_id"], notion_token):
            continue

        # IL-36: a blank body after all extraction layers is a fetch
        # failure, never a classification input.
        c: Optional[Classification] = None
        if reply.get("extraction") != "failed":
            c = classify(reply, api_key=anthropic_key,
                         competitor_names=cfg.get("competitor_names", []))
        new_count += 1

        if dry_run:
            print(json.dumps({
                "reply_id": reply["reply_id"], "campaign": reply["campaign_name"],
                "lead": reply["lead_name"] or reply["lead_email"],
                "extraction": reply.get("extraction"),
                "classification": c.classification if c else "EXTRACTION-FAILED",
                "flags": c.safety_flags if c else [],
                "reasoning": c.reasoning if c else EXTRACTION_FAILED_NOTE,
                "_raw_keys": sorted((reply.get("_raw") or {}).keys()),
            }, ensure_ascii=False, indent=1))
            continue

        lead_page = find_lead(cfg["leads_db"], reply["lead_email"], notion_token)
        page_id = write_reply_row(cfg, reply, c, lead_page, notion_token)
        label = c.classification if c else "Extraction-FAILED"
        line = f"{label}: {reply['lead_name'] or reply['lead_email']} ({reply['campaign_name']})"
        summary_lines.append(line)
        if page_id:
            run_pages.append(page_id)
        print(f"[ok] {reply['reply_id']} -> {page_id} [{line}]", file=sys.stderr)

        if c is not None and c.classification == "Critical":
            critical_count += 1
            sent = telegram_send(
                "🚨 CRITICAL reply (Agent 5 always-on)\n"
                f"Lead: {reply['lead_name'] or reply['lead_email']}\n"
                f"Campaign: {reply['campaign_name']}\n"
                f"Why: {c.reasoning[:300]}\n"
                f"Reply: {reply['original_text'][:400]}\n"
                "No action taken (policy: alert only). Review in the Replies DB / Gate 5."
            )
            if sent and page_id:
                set_telegram_flag(page_id, notion_token)
                flagged.add(page_id)

    if not dry_run and new_count:
        sent = telegram_send(
            f"Agent 5 always-on run: {new_count} new repl{'y' if new_count == 1 else 'ies'}, "
            f"{critical_count} critical.\n" + "\n".join(summary_lines[:10])
        )
        if sent:
            for pid in run_pages:
                if pid not in flagged:
                    set_telegram_flag(pid, notion_token)
    print(json.dumps({"new_replies": new_count, "critical": critical_count, "dry_run": dry_run}))
    return 0


# --------- Extraction selftest (golden payload, IL-36) ---------

def selftest_extraction() -> int:
    """Runs the layered extraction against the EXACT live payload that
    caused IL-36 (act_RTXdot9Twnih7jzYz, Outlook-mobile HTML, fetched from
    the Lemlist conversation 2026-06-12) plus the degenerate cases.
    No network. python3 main.py --selftest"""
    fixture = os.path.join(HERE, "fixtures", "outlook-mobile-reply.html")
    with open(fixture, encoding="utf-8") as f:
        act = {"text": "", "message": f.read(), "_id": "act_RTXdot9Twnih7jzYz"}
    text = extract_body(act, "text")
    checks = [
        ("starts with the lead's new content", text.startswith("Yes")),
        ("keeps the ask", "Give me three slots the week after next" in text),
        ("keeps the lead's signature block", "Nicolas Steinberg" in text),
        ("drops the quoted thread", "Quick context on the segmentation question" not in text),
        ("no html left", "<" not in text and ">" not in text),
        ("plain text passes through", extract_body({"text": "Yes, works for me"}, "text") == "Yes, works for me"),
        ("blank everything -> '' (extraction-failed path)", extract_body({"text": "  ", "message": ""}, "text") == ""),
        ("quote-strip never cuts to nothing", strip_quoted_history("From: someone@x.com\nhello") != ""),
    ]
    ok = True
    for name, passed in checks:
        print(f"[{'ok' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print(json.dumps({"selftest": "extraction", "passed": ok}))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest_extraction())
    sys.exit(run("--dry-run" in sys.argv))
