#!/usr/bin/env python3
"""
Agent 5 classification, GitHub Actions port.

Single-sources the logic from
agents/agent5-engagement-monitor/references/classification-rules.md:
  1. Rule-based safety net (deterministic substring matching, ported
     verbatim including the 2026-05-21 don't-reply patch and the
     multilingual lists).
  2. Optional LLM classification via the Anthropic API (Haiku by
     default). Without ANTHROPIC_API_KEY the scanner is rule-only.
  3. Override hierarchy: Critical-override flags beat the LLM; LLM
     confidence < 0.7 falls back to Ambiguous; rule-only mode maps
     non-Critical replies to Ambiguous (never guesses Positive).

Safety posture: a misclassification toward MORE human review (Ambiguous,
Critical) is acceptable; one toward LESS review (silent Positive) is not.

Selftest (no network): python3 classify.py --selftest
Runs the 16 synthetic fixtures in rule-only mode (asserts no fixture
lands in a less-severe bucket than expected) plus override-hierarchy
unit checks with mocked LLM verdicts.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

CONFIDENCE_FLOOR = 0.7
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# --------- Rule-based safety net (ported from classification-rules.md) ---------

OPT_OUT = [
    "unsubscribe", "remove me from", "take me off", "opt out", "opt-out",
    "stop emailing", "stop sending", "no more emails", "do not contact",
    "don't contact me", "do not email", "don't reply", "do not reply",
    "please don't reply", "no reply needed", "no response",
    "gdpr request", "right to be forgotten", "delete my data",
    # multilingual
    "désinscription", "arrêtez de m'écrire", "ne plus me contacter",
    "no me contacten", "no más correos", "não me contate", "descadastrar",
    "cancellate", "non scrivetemi più",
]

HOSTILITY = [
    "fuck off", "piss off", "go away", "leave me alone", "stop bothering",
    "you people", "spammer", "spam", "harassment", "harassing",
    "i'm going to report", "i'm reporting", "reporting this", "report you",
    "report this to", "shame on you", "how dare you", "who gave you my",
    "where did you get my email", "get my email", "got my email",
    # multilingual
    "foutez moi la paix", "arrêtez de me harceler", "laissez-moi tranquille",
    "no me molesten", "déjenme en paz", "dejen de molestar",
    "não me incomodem", "parem de me incomodar", "basta", "smettetela",
]

LEGAL = [
    "my lawyer", "our lawyer", "legal action", "sue you", "sue your",
    "court", "cease and desist", "gdpr violation", "gdpr complaint",
    "data protection authority", "cnil", "ico complaint", "report to dpa",
    "the ico", "to the ico",
]

CLIENT_SELF_ID = [
    "we already use hyperplan", "we are a hyperplan customer",
    "hyperplan is already deployed", "we have hyperplan",
    "we're already running hyperplan", "i'm on the hyperplan account",
]

AI_DETECTION = [
    "are you a bot", "are you an ai", "are you human", "this is ai",
    "sounds like ai", "chatgpt", "are you a real person",
    "prove you're human", "ignore previous instructions",
    "ignore your instructions", "your system prompt", "what model are you",
]

PRICING = [
    "how much does it cost", "what's the price", "pricing", "quote",
    "budget", "how much is", "what do you charge", "your rates",
    "your fees", "cost per", "per country", "per seat", "per user",
    "license fee", "subscription fee",
]

# Flags in this group force Critical regardless of the LLM verdict.
CRITICAL_OVERRIDE_FLAGS = {
    "hostility-marker", "legal-threat", "client-self-id", "ai-detection-test",
}

_FLAG_LISTS = [
    ("opt-out-keyword", OPT_OUT),
    ("hostility-marker", HOSTILITY),
    ("legal-threat", LEGAL),
    ("client-self-id", CLIENT_SELF_ID),
    ("ai-detection-test", AI_DETECTION),
    ("pricing-question", PRICING),
]


def _normalize(text: str) -> str:
    t = text.lower()
    # Normalize apostrophe variants so "don't" matches "don’t".
    t = t.replace("’", "'").replace("ʼ", "'")
    # Collapse whitespace; keep letters/apostrophes/hyphens for phrase matching.
    t = re.sub(r"[^\w'\-%€$£ ]", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def safety_net(reply_text: str, competitor_names: Optional[List[str]] = None) -> List[str]:
    """Return the list of safety flags that fire on this reply."""
    norm = _normalize(reply_text)
    flags: List[str] = []
    for flag, phrases in _FLAG_LISTS:
        for phrase in phrases:
            if _normalize(phrase) in norm:
                flags.append(flag)
                break
    for name in competitor_names or []:
        if re.search(r"\b" + re.escape(name.lower()) + r"\b", norm):
            flags.append("competitor-mention")
            break
    return flags


# --------- LLM classification (optional) ---------

LLM_PROMPT = """You are classifying an inbound reply to a Hyperplan cold outreach message.

Context:
- Lead: {lead_name}, {lead_title}, {lead_company}, {lead_country}
- Thread history (most recent first): {thread_history}
- Brand context: Hyperplan is a satellite-based crop intelligence GTM tool. Buyers are marketing + commercial heads at Tier 1 agrochem companies (Corteva, BASF, Syngenta) and regional distributors.

The reply to classify:
\"\"\"
{reply_text}
\"\"\"

Classify into exactly one of:
- Positive: explicit interest, info request, meeting accept, "send more", "let's talk"
- Neutral: OOO, redirect to colleague, "follow up later", "not now but maybe later"
- Negative: refusal, "not interested", "wrong person", "no fit", explicit unsubscribe
- Critical: hostility, legal threat, off-topic (nothing to do with Hyperplan or agribusiness), AI detection probe, client self-identification
- Ambiguous: cannot classify confidently

Return JSON only:
{{"classification": "...", "confidence": 0.0, "reasoning": "1-2 sentences"}}"""


def llm_classify(reply: Dict[str, Any], api_key: str, model: str = DEFAULT_MODEL) -> Optional[Dict[str, Any]]:
    """One Anthropic API call. Returns {classification, confidence, reasoning} or None on failure."""
    prompt = LLM_PROMPT.format(
        lead_name=reply.get("lead_name", "?"),
        lead_title=reply.get("lead_title", "?"),
        lead_company=reply.get("lead_company", "?"),
        lead_country=reply.get("lead_country", "?"),
        thread_history=(reply.get("thread_history") or "")[:1500],
        reply_text=(reply.get("original_text") or "")[:4000],
    )
    payload = {
        "model": model,
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in data.get("content", []))
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        verdict = json.loads(m.group(0))
        if verdict.get("classification") not in (
            "Positive", "Neutral", "Negative", "Critical", "Ambiguous"
        ):
            return None
        return verdict
    except Exception:  # noqa: BLE001  (LLM failure must never crash triage)
        return None


# --------- Override hierarchy ---------

@dataclass
class Classification:
    classification: str
    safety_flags: List[str] = field(default_factory=list)
    reasoning: str = ""
    llm_confidence: Optional[float] = None


def classify(reply: Dict[str, Any],
             api_key: Optional[str] = None,
             model: str = DEFAULT_MODEL,
             competitor_names: Optional[List[str]] = None,
             _llm_override: Optional[Dict[str, Any]] = None) -> Classification:
    """Full pipeline: safety net -> (optional) LLM -> override hierarchy.

    _llm_override is for tests only (injects a fake LLM verdict).
    """
    flags = safety_net(reply.get("original_text") or "", competitor_names)
    rule_part = f"Rule-based flags: {', '.join(flags) if flags else 'none'}."

    # 1. Critical override beats everything.
    fired_critical = [f for f in flags if f in CRITICAL_OVERRIDE_FLAGS]
    if fired_critical:
        return Classification(
            classification="Critical",
            safety_flags=flags,
            reasoning=f"{rule_part} Critical override: {', '.join(fired_critical)}.",
        )

    # 2. LLM pass (or injected test verdict).
    verdict = _llm_override
    if verdict is None and api_key:
        verdict = llm_classify(reply, api_key, model)

    if verdict is None:
        # Rule-only mode: never guess an engaged class. Ambiguous routes to
        # Gate 5 for the human call. Opt-out keyword alone still surfaces.
        return Classification(
            classification="Ambiguous",
            safety_flags=flags,
            reasoning=f"{rule_part} No LLM verdict (rule-only mode), defaulting to Ambiguous for Gate 5 review.",
        )

    conf = float(verdict.get("confidence") or 0.0)
    llm_class = verdict["classification"]
    llm_reason = verdict.get("reasoning", "")

    # 3. Confidence floor.
    if conf < CONFIDENCE_FLOOR:
        return Classification(
            classification="Ambiguous",
            safety_flags=flags,
            reasoning=f"{rule_part} LLM: {llm_class} ({conf:.2f}) below {CONFIDENCE_FLOOR} floor. {llm_reason}",
            llm_confidence=conf,
        )

    return Classification(
        classification=llm_class,
        safety_flags=flags,
        reasoning=f"{rule_part} LLM: {llm_class} ({conf:.2f}). {llm_reason}",
        llm_confidence=conf,
    )


# --------- Selftest ---------

SEVERITY = {"Positive": 0, "Neutral": 0, "Ambiguous": 1, "Negative": 2, "Critical": 3}


def selftest() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    fixture_paths = [
        os.path.join(here, "fixtures", "synthetic-replies.json"),
        os.path.join(here, "..", "..", "agents", "agent5-engagement-monitor",
                     "fixtures", "synthetic-replies.json"),
    ]
    fixtures = None
    for p in fixture_paths:
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                fixtures = json.load(f)["replies"]
            break
    if fixtures is None:
        print("FAIL  fixtures file not found")
        return 1

    failures: List[str] = []
    checks = 0

    # A. Rule-only mode on all 16 fixtures: result must never be LESS severe
    #    than expected (no silent Positive), and keyword-driven Criticals
    #    must be caught without the LLM.
    for fx in fixtures:
        checks += 1
        result = classify(fx, api_key=None)
        expected = fx["expected_classification"]
        if SEVERITY[result.classification] < min(SEVERITY[expected], 1):
            failures.append(
                f"{fx['reply_id']}: rule-only gave {result.classification}, "
                f"less severe than expected {expected}"
            )
        expected_flags = set(fx.get("expected_safety_flags") or [])
        critical_expected = expected_flags & CRITICAL_OVERRIDE_FLAGS
        if critical_expected and result.classification != "Critical":
            failures.append(
                f"{fx['reply_id']}: expected Critical via {critical_expected}, "
                f"got {result.classification} (flags: {result.safety_flags})"
            )

    # B. Override hierarchy with mocked LLM verdicts.
    hostile = {"original_text": "Stop bothering me, spammer.", "lead_name": "x",
               "lead_title": "x", "lead_company": "x", "lead_country": "x"}
    checks += 1
    r = classify(hostile, _llm_override={"classification": "Positive", "confidence": 0.99, "reasoning": "mock"})
    if r.classification != "Critical":
        failures.append(f"override: hostile + mock-Positive LLM gave {r.classification}, want Critical")

    lowconf = {"original_text": "Maybe? Depends what you mean.", "lead_name": "x",
               "lead_title": "x", "lead_company": "x", "lead_country": "x"}
    checks += 1
    r = classify(lowconf, _llm_override={"classification": "Positive", "confidence": 0.5, "reasoning": "mock"})
    if r.classification != "Ambiguous":
        failures.append(f"confidence floor: 0.5 gave {r.classification}, want Ambiguous")

    clean = {"original_text": "Interesting, send me the deck for Benelux.", "lead_name": "x",
             "lead_title": "x", "lead_company": "x", "lead_country": "x"}
    checks += 1
    r = classify(clean, _llm_override={"classification": "Positive", "confidence": 0.95, "reasoning": "mock"})
    if r.classification != "Positive":
        failures.append(f"clean positive: gave {r.classification}, want Positive")

    checks += 1
    r = classify({"original_text": "What's the price per country?", "lead_name": "x",
                  "lead_title": "x", "lead_company": "x", "lead_country": "x"},
                 _llm_override={"classification": "Positive", "confidence": 0.9, "reasoning": "mock"})
    if "pricing-question" not in r.safety_flags or r.classification != "Positive":
        failures.append(f"pricing flag: flags={r.safety_flags}, class={r.classification}")

    for f in failures:
        print(f"FAIL  {f}")
    print(f"\nSummary: {checks - len(failures)} / {checks} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print("Usage: python3 classify.py --selftest (or import classify from main.py)")
    sys.exit(1)
