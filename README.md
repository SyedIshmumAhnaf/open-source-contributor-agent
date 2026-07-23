# open-source-contributor-agent
This repository aims to find projects which will be an adequate fit for a user's skills and time constraints so that they can increase their contributions.

# OSS Issue Finder

Automatically finds beginner-friendly open source issues matched to your stack,
every Thursday at 10pm BDT — lands in your inbox before the weekend.

## How it works

1. **Scheduler** — GitHub Actions cron fires every Thursday at 10pm BDT
2. **Discovery** — Searches GitHub for `good first issue` / `help wanted` issues in your languages
3. **Scoring** — Ranks issues by complexity signals (comment count, description clarity, repo health)
4. **Delivery** — Sends a digest email via Resend

## Setup (one time, ~10 minutes)

### 1. Fork / create the repo

Push this code to a **public** GitHub repository.
Public repos get unlimited free GitHub Actions minutes.

### 2. Get a Resend API key

1. Sign up at [resend.com](https://resend.com) — free tier allows 100 emails/day
2. Go to **API Keys** → **Create API Key**
3. Copy the key

### 3. Add repository secrets

Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add these two secrets:

| Secret name     | Value                              |
|-----------------|------------------------------------|
| `RESEND_API_KEY` | Your Resend API key               |
| `TO_EMAIL`      | The email address to send digests to |

> `GITHUB_TOKEN` is provided automatically by GitHub Actions — no action needed.

### 4. Test it manually

Go to **Actions** → **Weekly OSS Issue Finder** → **Run workflow**

You should receive an email within ~2 minutes.

### 5. Let it run

The cron fires every Thursday at 4pm UTC (10pm BDT). That's it.

---

## Customising your search

### Change languages

Edit the `languages` section in `skills.yaml`. Set `search: true`/`false` to
include or exclude a language, `level` to record your comfort, and an
optional `weight` (relative to `1.0` = neutral) to nudge scoring:

```yaml
languages:
  python:
    level: advanced
    search: true
    weight: 1.2
  typescript:
    level: beginner
    search: false
```

If `skills.yaml` is missing, malformed, or every language is disabled,
`finder.py` falls back to `["python", "javascript"]` and prints a warning —
the scheduled run never crashes because of a config problem.

### Keyword and preference scoring

`skills.yaml` also supports soft scoring modifiers — matches never exclude
an issue, they only nudge its rank:

```yaml
keywords:
  positive:   # small score bonus if title/body mentions these
    - cli
    - api
  negative:   # moderate score penalty if title/body mentions these
    - blockchain

preferences:
  avoid_docs_only: true   # deprioritize quick docs/readme/typo-only issues
                           # (conservative — skips the penalty if the issue
                           # also has a bug/enhancement label, or mentions
                           # technical work like CLI/API/tests/migration)
```

### Adjust quality thresholds

```python
MIN_STARS    = 200   # Minimum repo stars
MAX_COMMENTS = 15    # Issues with more comments are deprioritized
TOP_N        = 8     # Number of issues in each digest
```

### Dependencies

Dependencies are listed in `requirements.txt` (`requests` + `PyYAML`) and
installed via `pip install -r requirements.txt` in the GitHub Actions
workflow.

---

## Project structure

```
.
├── finder.py                     # Main script
├── skills.yaml                   # Your skill profile (languages, keywords, preferences)
├── requirements.txt              # Python dependencies
└── .github/
    └── workflows/
        └── schedule.yml          # Cron trigger + Actions config
```

## Roadmap

- **Phase 1** — Language-filtered search + complexity scoring + email digest
- **Phase 2** (current) — Skill matcher reads `skills.yaml`: language selection/weighting,
  positive/negative keyword scoring, comment-count rebalancing, docs-only deprioritization
- **Phase 3** — Track which issues you actually worked on, learn from outcomes
