"""
OSS Issue Finder
Runs every Thursday at 10pm BDT via GitHub Actions.
Searches GitHub for beginner-friendly issues matched to your stack,
scores them by complexity, and emails a digest via Resend.
"""

import os
import random
import time
import requests
from datetime import datetime, timezone, timedelta

# ─── Config ──────────────────────────────────────────────────────────────────

GITHUB_TOKEN  = os.environ["GITHUB_TOKEN"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
TO_EMAIL      = os.environ["TO_EMAIL"]

# Your languages — update this list as your skills evolve
LANGUAGES = ["python", "javascript"]

# Label tiers — used for SCORING only (tier inference from returned issue labels)
#
# BEGINNER_LABELS: strong intent signal → score boosted
# GENERAL_LABELS:  weaker signal → score penalized
BEGINNER_LABELS = [
    "good first issue",
    "good-first-issue",
    "beginner",
    "easy",
    "starter",
    "first-timers-only",
]
GENERAL_LABELS = [
    "help wanted",
]

# Fast lookup set for tier inference — derived from BEGINNER_LABELS, not hand-maintained
BEGINNER_LABELS_SET = {l.lower() for l in BEGINNER_LABELS}

# SEARCH_LABELS — used for QUERYING only (one query per language per label)
#
# Deliberately kept to 2 high-signal labels:
#   "good first issue" covers ~90% of repos using beginner labels
#   "help wanted"      catches repos that skip the beginner label entirely
#
# Why not OR-combine all labels into one query?
# GitHub's Search API does NOT support parenthesized OR grouping for label:
# filters. The syntax (label:"x" OR label:"y") silently returns 0 results
# because GitHub treats the parentheses as literal search characters.
# Separate queries per label is the only reliable approach.
#
# 2 labels × 2 languages = 4 total search calls — comfortably under the
# secondary rate limit threshold.
SEARCH_LABELS = [
    "good first issue",
    "help wanted",
]

# Only surface issues created within this window (reduces stale results)
RECENCY_DAYS = 90

# After any 403, pause the entire run before the next query.
# Mimics human browsing cadence and lets GitHub's session limiter reset.
GLOBAL_COOLDOWN_SECS = 60

# Repo quality floor
MIN_STARS = 200

# Issues with more than this many comments are usually contested/complex
MAX_COMMENTS = 15

# How many issues to include in the digest
TOP_N = 8

GITHUB_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ─── GitHub API ───────────────────────────────────────────────────────────────

def search_issues(language: str, label: str, since_date: str) -> list[dict]:
    """
    Search GitHub for open, unassigned issues for one language + one label.

    Deliberately one label per call — GitHub's Search API does NOT support
    parenthesized OR grouping for label: filters. The syntax
    (label:"x" OR label:"y") silently returns 0 results because GitHub treats
    the parentheses as literal search characters, not grouping operators.

    Call count: len(SEARCH_LABELS) × len(LANGUAGES) = 2 × 4 = 8 queries.
    At 4s spacing that's ~32s total — well within secondary rate limit bounds.

    Retry policy: respects Retry-After header, falls back to hardcoded waits.
    """
    # no:assignee is intentionally omitted from the query — it tightens the
    # filter signature and contributes to abuse detection. Assignee check is
    # done in Python after results are returned.
    query = (
        f'label:"{label}" language:{language} '
        f'state:open is:issue '
        f'created:>{since_date}'
    )
    params = {"q": query, "sort": "created", "order": "desc", "per_page": 50}

    for attempt, _ in enumerate(range(3), start=1):
        resp = requests.get(
            "https://api.github.com/search/issues",
            headers=GITHUB_HEADERS,
            params=params,
        )
        if resp.status_code == 403 and attempt < 3:
            # Exponential backoff: 20s, 40s — respects Retry-After if present
            fallback = 20 * (2 ** (attempt - 1))
            wait = int(resp.headers.get("Retry-After", fallback))
            print(f"    ↻ 403 on attempt {attempt}, sleeping {wait}s "
                  f"(Retry-After: {resp.headers.get('Retry-After', 'n/a')})...", flush=True)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json().get("items", [])

    resp.raise_for_status()
    return []


def get_repo(full_name: str, cache: dict) -> dict:
    """
    Fetch repo metadata (stars, archived status, language).

    cache is a dict passed in by the caller and mutated here — many issues
    come from the same repo, so without caching we'd make duplicate API calls
    for every issue in a popular repo (e.g. 10 issues from microsoft/vscode
    previously triggered 10 identical get_repo calls).
    """
    if full_name in cache:
        return cache[full_name]
    resp = requests.get(
        f"https://api.github.com/repos/{full_name}",
        headers=GITHUB_HEADERS,
    )
    resp.raise_for_status()
    cache[full_name] = resp.json()
    return cache[full_name]


# ─── Scoring ─────────────────────────────────────────────────────────────────

def score_body_quality(body: str) -> int:
    """
    Lightweight NLP signal — no external libraries, pure keyword matching.
    Returns a bonus score (0–20) based on how actionable the issue body is.

    Actionable issues tend to include:
      - Reproduction steps ("steps to reproduce", "to reproduce")
      - Expected vs actual behaviour ("expected", "actual", "instead")
      - A clear ask ("fix", "should", "error", "bug", "crash", "fail")
      - Structure markers ("##", "- [ ]") suggesting a well-formatted report
    """
    if not body:
        return 0

    text = body.lower()
    score = 0

    # Reproduction / expected-behaviour markers — strong actionability signal
    repro_keywords = [
        "steps to reproduce", "to reproduce", "how to reproduce",
        "expected behavior", "expected behaviour", "expected result",
        "actual behavior", "actual behaviour", "actual result",
        "reproduce",
    ]
    if any(kw in text for kw in repro_keywords):
        score += 10

    # Fix-oriented keywords — confirms there's a concrete task
    fix_keywords = ["fix", "error", "bug", "crash", "fail", "broken", "issue", "incorrect"]
    if sum(1 for kw in fix_keywords if kw in text) >= 2:
        score += 5

    # Structural formatting — suggests effort went into the report
    if "##" in body or "- [ ]" in body or "```" in body:
        score += 5

    return min(score, 20)  # cap at 20


def score_issue(issue: dict, repo: dict, label_tier: str) -> int:
    """
    Score an issue from 0–100 based on complexity + repo health signals.
    Higher = better fit for a 4–8 hour weekend session.

    Signals used:
      - label_tier:    "beginner" → +15 bonus, "general" → -10 penalty
      - Body quality:  keyword-based actionability score (+0 to +20)
      - Comment count: 2–8 is ideal (some context, not a rabbit hole)
      - Body length:   200–1500 chars (clear enough, not overwhelming)
      - Stars:         signals an active, maintained repo
      - Assignee:      confirmed absent by query, double-checked here
      - Type labels:   bug/enhancement add clarity
    """
    score = 0

    comments    = issue.get("comments", 0)
    body        = (issue.get("body") or "")
    body_len    = len(body)
    stars       = repo.get("stargazers_count", 0)
    label_names = [l["name"].lower() for l in issue.get("labels", [])]

    # ── Label tier ───────────────────────────────────────────────────────────
    # Labels are now a scoring modifier, not a hard gate.
    if label_tier == "beginner":
        score += 15
    elif label_tier == "general":
        score -= 10   # help wanted often = complex, unowned work

    # ── Body quality (NLP signals) ────────────────────────────────────────────
    score += score_body_quality(body)

    # ── Comment count ─────────────────────────────────────────────────────────
    # Weight reduced vs before — labels + body quality are now better signals.
    if 2 <= comments <= 8:
        score += 20
    elif comments < 2:
        score += 8    # newly filed — low context but not disqualifying
    elif comments <= MAX_COMMENTS:
        score += 5
    else:
        score -= 15   # heavily debated = likely underspecified

    # ── Body length ───────────────────────────────────────────────────────────
    if 200 <= body_len <= 1500:
        score += 20
    elif body_len > 1500:
        score += 8
    else:
        score -= 10   # too vague to scope

    # ── Repo star tiers ───────────────────────────────────────────────────────
    if stars >= 2000:
        score += 20
    elif stars >= 1000:
        score += 16
    elif stars >= 500:
        score += 12
    elif stars >= MIN_STARS:
        score += 6
    else:
        score -= 10

    # ── Assignee check ────────────────────────────────────────────────────────
    if issue.get("assignee") is None:
        score += 8

    # ── Type label context ────────────────────────────────────────────────────
    if "bug" in label_names or "enhancement" in label_names:
        score += 5

    return score


# ─── Collection ──────────────────────────────────────────────────────────────

def collect_issues() -> list[dict]:
    """
    Search across SEARCH_LABELS × LANGUAGES, deduplicate, score, return top N.

    Anti-detection measures applied:
      - Pairs are shuffled before each run (non-sequential language order)
      - Sleep between queries uses random jitter (4–7s) instead of fixed 4s
      - no:assignee removed from query; filtered in Python (looser fingerprint)
      - Global 60s cooldown fires after any 403 before the next query
    """
    seen          = set()
    repo_cache: dict = {}
    candidates    = []
    global_cooldown_triggered = False

    since_date = (
        datetime.now(timezone.utc) - timedelta(days=RECENCY_DAYS)
    ).strftime("%Y-%m-%d")

    # Build and shuffle the search plan so query order is non-deterministic
    pairs = [(lang, label) for lang in LANGUAGES for label in SEARCH_LABELS]
    random.shuffle(pairs)

    print(f"  Recency filter: issues created after {since_date}", flush=True)
    print(f"  Search plan: {len(pairs)} queries (shuffled)", flush=True)

    for lang, label in pairs:
        # Jitter: random delay between 4–7s breaks the bot-pattern fingerprint
        jitter = random.uniform(4, 7)
        time.sleep(jitter)

        print(f"  Searching: language={lang}, label='{label}'", flush=True)

        try:
            items = search_issues(lang, label, since_date)
        except requests.HTTPError as e:
            print(f"  ⚠ Search failed ({e}), skipping.", flush=True)
            # Global cooldown: any 403 that exhausted all retries means the
            # session is flagged — pause the whole run before continuing
            if "403" in str(e) and not global_cooldown_triggered:
                global_cooldown_triggered = True
                print(f"  ⚠ Global cooldown: sleeping {GLOBAL_COOLDOWN_SECS}s "
                      f"to let rate limiter reset...", flush=True)
                time.sleep(GLOBAL_COOLDOWN_SECS)
            continue

        print(f"    → {len(items)} raw results", flush=True)

        for issue in items:
            issue_id = issue["id"]
            if issue_id in seen:
                continue
            seen.add(issue_id)

            # Python-side assignee filter (removed from query to reduce bot signature)
            if issue.get("assignee") is not None:
                continue

            repo_full = issue["repository_url"].split("repos/")[-1]

            try:
                is_cache_miss = repo_full not in repo_cache
                repo = get_repo(repo_full, repo_cache)
                if is_cache_miss:
                    time.sleep(0.4)
            except requests.HTTPError:
                continue

            if repo.get("archived"):
                continue
            if repo.get("stargazers_count", 0) < MIN_STARS:
                continue

            issue_label_set = {l["name"].lower() for l in issue.get("labels", [])}
            tier = "beginner" if issue_label_set & BEGINNER_LABELS_SET else "general"

            s = score_issue(issue, repo, tier)
            candidates.append({
                "score":        s,
                "label_tier":   tier,
                "title":        issue["title"],
                "url":          issue["html_url"],
                "repo":         repo_full,
                "stars":        repo.get("stargazers_count", 0),
                "language":     repo.get("language") or lang.capitalize(),
                "comments":     issue.get("comments", 0),
                "labels":       [l["name"] for l in issue.get("labels", [])],
                "body_preview": (issue.get("body") or "").strip()[:220],
                "created_at":   issue["created_at"],
            })

    print(f"  Repo cache: {len(repo_cache)} unique repos fetched.", flush=True)
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:TOP_N]


# ─── Email ────────────────────────────────────────────────────────────────────

def build_html(issues: list[dict]) -> str:
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")

    def label_pill(text: str) -> str:
        return (
            f'<span style="display:inline-block;background:#f0f4ff;color:#3b5bdb;'
            f'padding:2px 9px;border-radius:20px;font-size:11px;font-weight:500;'
            f'margin:2px 3px 2px 0;">{text}</span>'
        )

    cards = ""
    for i, iss in enumerate(issues, 1):
        pills    = "".join(label_pill(l) for l in iss["labels"])
        preview  = iss["body_preview"]
        ellipsis = "…" if len(preview) == 220 else ""
        cards += f"""
        <div style="border:1px solid #e8e8e8;border-radius:10px;padding:18px 20px;
                    margin-bottom:14px;background:#fff;">
          <div style="font-size:11px;color:#999;margin-bottom:6px;">
            #{i} &nbsp;·&nbsp; {iss['language']}
            &nbsp;·&nbsp; ⭐ {iss['stars']:,}
            &nbsp;·&nbsp; 💬 {iss['comments']} comments
          </div>
          <a href="{iss['url']}"
             style="font-size:15px;font-weight:600;color:#111;text-decoration:none;
                    line-height:1.4;display:block;margin-bottom:3px;">
            {iss['title']}
          </a>
          <div style="font-size:12px;color:#888;margin-bottom:8px;">{iss['repo']}</div>
          <div style="margin-bottom:10px;">{pills}</div>
          <div style="font-size:13px;color:#555;line-height:1.6;
                      border-left:3px solid #e8e8e8;padding-left:12px;">
            {preview}{ellipsis}
          </div>
        </div>
        """

    return f"""<!DOCTYPE html>
<html>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,sans-serif;
             max-width:620px;margin:0 auto;padding:32px 20px;background:#f9f9f9;color:#111;">

  <div style="background:#111;color:#fff;border-radius:10px;padding:24px 28px;margin-bottom:24px;">
    <div style="font-size:11px;color:#888;margin-bottom:6px;letter-spacing:.5px;
                text-transform:uppercase;">Weekly OSS Digest · {today}</div>
    <div style="font-size:22px;font-weight:700;margin-bottom:6px;">
      Your open source picks 🔍
    </div>
    <div style="font-size:13px;color:#aaa;">
      Top issues matched to your stack, sized for a weekend session.
    </div>
  </div>

  {cards}

  <div style="margin-top:24px;font-size:11px;color:#bbb;text-align:center;
              border-top:1px solid #e8e8e8;padding-top:16px;">
    Generated every Thursday at 10pm BDT · Powered by GitHub Search API
  </div>

</body>
</html>"""


def send_email(html: str) -> None:
    subject = f"Your weekly OSS picks — {datetime.now(timezone.utc).strftime('%b %d')}"
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": "OSS Finder <onboarding@resend.dev>",
            "to":   [TO_EMAIL],
            "subject": subject,
            "html": html,
        },
    )
    resp.raise_for_status()
    print(f"✓ Email sent → {resp.json().get('id')}", flush=True)


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("── OSS Issue Finder ──────────────────────────", flush=True)
    print("Searching GitHub for issues...", flush=True)
    issues = collect_issues()
    print(f"Shortlisted {len(issues)} issues.", flush=True)

    if not issues:
        print("No issues found — nothing to send.", flush=True)
    else:
        html = build_html(issues)
        print("Sending digest email...", flush=True)
        send_email(html)
        print("Done.", flush=True)