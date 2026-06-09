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
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
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
                return json.loads(resp.read().decode("utf-8"))
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
                    "original_text": a.get(f_text) or a.get("body") or "",
                    "received_at": created,
                    "thread_history": "",
                    "_raw": a,
                })
    return [r for r in out if r["reply_id"]]


# --------- Step 2: dedup against Replies DB ---------

def reply_exists(replies_db: str, reply_id: str, token: str) -> bool:
    data = _request(
        f"{NOTION_API}/databases/{replies_db}/query", notion_headers(token), "POST",
        {"filter": {"property": "Reply ID", "title": {"equals": reply_id}}, "page_size": 1},
    )
    return bool(data.get("results"))


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

def write_reply_row(cfg: Dict[str, Any], reply: Dict[str, Any], c: Classification,
                    lead_page_id: Optional[str], token: str) -> str:
    def rt(text: str) -> Dict[str, Any]:
        return {"rich_text": [{"type": "text", "text": {"content": text[:1900]}}]}

    props: Dict[str, Any] = {
        "Reply ID": {"title": [{"type": "text", "text": {"content": reply["reply_id"]}}]},
        "Channel": {"select": {"name": reply["channel"]}},
        "Classification": {"select": {"name": c.classification}},
        "Classification reasoning": rt(c.reasoning),
        "Original reply": rt(reply["original_text"]),
        "Gate 5 Decision": {"select": {"name": "Critical-Escalated" if c.classification == "Critical" else "Pending"}},
    }
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

    for reply in replies:
        if reply["reply_id"].startswith(synth_prefix):
            continue
        if not dry_run and reply_exists(cfg["replies_db"], reply["reply_id"], notion_token):
            continue

        c = classify(reply, api_key=anthropic_key,
                     competitor_names=cfg.get("competitor_names", []))
        new_count += 1

        if dry_run:
            print(json.dumps({
                "reply_id": reply["reply_id"], "campaign": reply["campaign_name"],
                "lead": reply["lead_name"] or reply["lead_email"],
                "classification": c.classification, "flags": c.safety_flags,
                "reasoning": c.reasoning,
                "_raw_keys": sorted((reply.get("_raw") or {}).keys()),
            }, ensure_ascii=False, indent=1))
            continue

        lead_page = find_lead(cfg["leads_db"], reply["lead_email"], notion_token)
        page_id = write_reply_row(cfg, reply, c, lead_page, notion_token)
        line = f"{c.classification}: {reply['lead_name'] or reply['lead_email']} ({reply['campaign_name']})"
        summary_lines.append(line)
        print(f"[ok] {reply['reply_id']} -> {page_id} [{line}]", file=sys.stderr)

        if c.classification == "Critical":
            critical_count += 1
            telegram_send(
                "🚨 CRITICAL reply (Agent 5 always-on)\n"
                f"Lead: {reply['lead_name'] or reply['lead_email']}\n"
                f"Campaign: {reply['campaign_name']}\n"
                f"Why: {c.reasoning[:300]}\n"
                f"Reply: {reply['original_text'][:400]}\n"
                "No action taken (policy: alert only). Review in the Replies DB / Gate 5."
            )

    if not dry_run and new_count:
        telegram_send(
            f"Agent 5 always-on run: {new_count} new repl{'y' if new_count == 1 else 'ies'}, "
            f"{critical_count} critical.\n" + "\n".join(summary_lines[:10])
        )
    print(json.dumps({"new_replies": new_count, "critical": critical_count, "dry_run": dry_run}))
    return 0


if __name__ == "__main__":
    sys.exit(run("--dry-run" in sys.argv))
