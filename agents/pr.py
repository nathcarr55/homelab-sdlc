#!/usr/bin/env python3
"""
PR agent — opens a pull request from the ai branch to main,
with a summary of what was done and links back to the issue.
"""

import os
import json
import sys
from openai import OpenAI
from github import Github, GithubException

# ── Config from environment ──────────────────────────────────────────────────
LITELLM_URL     = os.environ["LITELLM_URL"]
LITELLM_KEY     = os.environ["LITELLM_MASTER_KEY"]
GITHUB_TOKEN    = os.environ["GITHUB_TOKEN"]
REPO_FULL_NAME  = os.environ["REPO_FULL_NAME"]
ISSUE_NUMBER    = int(os.environ["ISSUE_NUMBER"])
TASK_PLAN_RAW   = os.environ.get("TASK_PLAN", "")
TESTS_PASSED    = os.environ.get("TESTS_PASSED", "false").lower() == "true"
REVIEW_APPROVED = os.environ.get("REVIEW_APPROVED", "true").lower() == "true"

# ── Clients ───────────────────────────────────────────────────────────────────
llm   = OpenAI(base_url=f"{LITELLM_URL}/v1", api_key=LITELLM_KEY)
gh    = Github(GITHUB_TOKEN)
repo  = gh.get_repo(REPO_FULL_NAME)
issue = repo.get_issue(ISSUE_NUMBER)

# ── Parse task plan ───────────────────────────────────────────────────────────
try:
    plan = json.loads(TASK_PLAN_RAW)
except (json.JSONDecodeError, ValueError) as e:
    print(f"ERROR: could not parse TASK_PLAN: {e}")
    sys.exit(1)

branch_name    = plan["branch_name"]
default_branch = repo.default_branch

# ── Check branch has commits ahead of main ────────────────────────────────────
try:
    comparison = repo.compare(default_branch, branch_name)
    if comparison.ahead_by == 0:
        print("Branch has no commits ahead of main — nothing to PR.")
        issue.create_comment(
            "🤖 **AI Agent** — No changes were committed so no PR was opened."
        )
        sys.exit(0)
    print(f"Branch is {comparison.ahead_by} commit(s) ahead of {default_branch}.")
except GithubException as e:
    print(f"ERROR comparing branches: {e}")
    sys.exit(1)

# ── Generate PR description with LLM ─────────────────────────────────────────
tasks_text = "\n".join(
    f"- [{t['type']}] {t['description']}" for t in plan.get("tasks", [])
)

test_status  = "✅ Tests passed" if TESTS_PASSED else "⚠️ Tests failed or were not run"
review_status = "✅ Review approved" if REVIEW_APPROVED else "⚠️ Review flagged issues — please audit before merging"

prompt = f"""Write a concise GitHub pull request description for these changes:

Summary: {plan.get('summary', '')}
Tasks completed:
{tasks_text}

Notes: {plan.get('notes', '')}
Test status: {test_status}
Review status: {review_status}
Closes issue: #{ISSUE_NUMBER}

Format as markdown with:
- A short ## What changed section (2-3 sentences)
- A ## Tasks section listing what was done
- A ## Status section with test and review outcome
- Closes #{ISSUE_NUMBER} at the end

Keep it professional and concise."""

try:
    response = llm.chat.completions.create(
        model="fast",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=500,
    )
    pr_body = response.choices[0].message.content.strip()
except Exception as e:
    print(f"WARNING: LLM failed for PR description ({e}), using fallback.")
    pr_body = f"""## What changed

{plan.get('summary', 'AI-generated changes.')}

## Tasks

{tasks_text}

## Status

- {test_status}
- {review_status}

Closes #{ISSUE_NUMBER}"""

# ── Determine if PR should be draft ──────────────────────────────────────────
open_as_draft = not TESTS_PASSED or not REVIEW_APPROVED

# ── Open the PR ───────────────────────────────────────────────────────────────
pr_title = f"ai: {plan.get('summary', f'Fixes #{ISSUE_NUMBER}')[:72]}"

try:
    existing_prs = repo.get_pulls(state="open", head=f"{repo.owner.login}:{branch_name}")
    if existing_prs.totalCount > 0:
        pr = existing_prs[0]
        pr.edit(body=pr_body)
        print(f"Updated existing PR #{pr.number}: {pr.html_url}")
    else:
        pr = repo.create_pull(
            title=pr_title,
            body=pr_body,
            head=branch_name,
            base=default_branch,
            draft=open_as_draft,
        )
        print(f"Opened PR #{pr.number}: {pr.html_url}")

    status_line = f"{test_status} | {review_status}"
    issue.create_comment(
        f"🤖 **AI Agent — Complete**\n\n"
        f"{status_line}\n\n"
        f"Pull request: {pr.html_url}"
        + ("\n\n⚠️ Opened as **draft** — review before merging." if open_as_draft else "")
    )

except GithubException as e:
    print(f"ERROR creating PR: {e}")
    sys.exit(1)

print("PR agent complete.")
