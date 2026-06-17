# Agent 5 always-on triage (GitHub Actions)

The squad's first 24/7 capability: reply triage that runs when the laptop is closed. Built 2026-06-09 per `agents/agent5-engagement-monitor/references/github-actions-extraction-spec.md`, scoped down to **triage mode** on purpose.

## What it does, every 4 hours

Discover the squad campaigns by naming convention (`A3-*` / `A4-*`, merged with the static fallback list in config.json) → pull their reply activities → dedup against the Notion Replies DB → classify (deterministic safety net + Anthropic Haiku, override hierarchy from classification-rules.md) → write rows to the Replies DB at Gate 5 Pending → Telegram alert for Critical, summary for the rest.

Scaling: new waves are in scope automatically the moment Agent 4 creates their campaigns, because Agent 4's naming convention IS the scope filter. No config edit per wave. The run log prints the resolved scope ("N squad campaigns") every run; if a launched campaign is missing from it, the campaign name broke convention, fix the name, not the config. The convention is canonical in the architecture reference §17 ("Campaign naming convention", load-bearing); Agent 4 Step 5.5 asserts the prefix at creation time.

## What it deliberately does not do

- No drafting, no sending. Drafting stays in the Cowork `agent5-reply-sweep` (brand voice files + validate-outreach Layer 2 live there). The Action gets replies INTO Gate 5 fast; the sweep drafts on its 10/14/16 weekday cadence.
- No pausing of anything, ever (Rémi ruling 2026-06-09). Critical and opt-out replies are flagged and alerted; execution is Rémi's.
- No campaigns outside `config.json`. Squad scope only.
- Synthetic fixture rows (`R-2026-05-19*`) excluded.

Rule-only degradation: without `ANTHROPIC_API_KEY`, keyword-Critical still fires and everything else lands as Ambiguous at Gate 5. The system fails toward MORE human review, never less.

## Why this split is safe to run alongside the Cowork sweep

The spec's cutover warning ("never two systems doing the same triage") is satisfied by dedup: both writers query the Replies DB by Reply ID before creating. Whichever runs first wins; the other skips. Still, once the Action is trusted, narrow the Cowork sweep to drafting-only (step 6 below).

## Deployment (one-time, ~20 minutes)

1. **Create a private repo** (e.g. `hyperplan-ai-squad-actions`) under the account that runs the agri-news digest Action.
2. **Copy this folder** into the repo as `github-actions/engagement-monitor/`, and copy `workflow-agent5-monitor.yml` to `.github/workflows/agent5-monitor.yml`.
3. **Copy the fixtures** for the in-repo selftest: `agents/agent5-engagement-monitor/fixtures/synthetic-replies.json` → `github-actions/engagement-monitor/fixtures/synthetic-replies.json` (already done if you copied the whole folder).
4. **Set Actions secrets** (repo Settings → Secrets → Actions): `LEMLIST_API_KEY`, `NOTION_TOKEN` (integration with access to Replies + Leads DBs), `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Secrets only, never in code (and rotate the bot token that sat in the local .env).
5. **First run = manual dry run.** Actions tab → agent5-engagement-monitor → Run workflow → dry_run: true. Read the log: it prints each pulled activity with `_raw_keys`. If the Lemlist payload field names differ from the defaults, fix `activity_field_map` in `config.json` (not the code) and re-run. Expect 0-2 replies at current volume; an empty pull on a quiet day is a pass, not a failure.
6. **Validation window before trusting it** (the spec's own rule): let it run 1-2 weeks against real Wave 1/2 replies in parallel with the Cowork sweep. Compare classifications at Gate 5. When the edit rate on the Action's classifications is acceptable, edit the `agent5-reply-sweep` scheduled task prompt to skip ingestion and only draft on rows where `Draft reply` is empty and Classification is Positive/Neutral.

## Maintenance

- New wave launches: nothing to do, auto-discovered by the `A3-`/`A4-` prefix. Only off-convention campaign names need a manual entry in `config.json`. Check the "scope: N squad campaigns" line in the run log after each wave launch.
- Classification keyword changes: edit `classify.py` lists AND `agents/agent5-engagement-monitor/references/classification-rules.md` together; the reference file stays canonical. Run `python3 classify.py --selftest` (20 checks) before pushing.
- Cost: ~1 Haiku call per new reply. At current volume this is cents per month.

## Body extraction (IL-36, added 2026-06-12)

The first live reply arrived as Outlook-mobile HTML with no usable plain-text field; the runner stored a blank body and misclassified it Ambiguous. `main.py` now extracts in 3 layers: every known body field on the activity payload (`text`, `body`, `message`, `bodyHtml`, ...) → HTML-to-text + quoted-history strip → inbox-conversation fetch as last resort. A blank after all 3 writes the row WITHOUT a Classification bucket, reasoning = "extraction-failed, needs manual fetch". The `Telegram alert sent` checkbox is now set in the same pass that sends an alert (Critical immediately, run-summary rows after the summary send succeeds).

- Golden test (no network): `python3 main.py --selftest`. Runs against `fixtures/outlook-mobile-reply.html`, the EXACT live payload of act_RTXdot9Twnih7jzYz fetched from the Lemlist conversation on 2026-06-12. Run it together with `classify.py --selftest` before any push.
- VERIFY ON FIRST LIVE USE: the inbox-fallback REST paths (`/inbox/conversations/{ctc_id}`) mirror the MCP `get_inbox_conversation` shape but are unverified against public docs. Failure is safe (degrades to extraction-failed), but if a row lands extraction-failed while the conversation clearly has a body, fix the path here.

## Not built yet (deliberate)

- Drafting in the Action (spec step 3): needs brand voice + response playbook + Layer 2 in-repo. Add only if the Cowork drafting cadence becomes the bottleneck.
- Agent 6 booking webhook: needs hosted webhook infra; the positive-reply → booking flow stays in Cowork at Gate 5/6 for now.
- HubSpot lifecycle sync on opt-outs: flagged in Notion instead; execution manual pending the opt-out autopause decision.
