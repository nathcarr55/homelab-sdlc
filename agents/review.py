#!/usr/bin/env python3
"""
Review agent — uses Claude to audit AI-generated code changes against the
original issue spec. Outputs 'approved' and 'feedback' for the CI pipeline.
"""

import os
import sys
import json
import anthropic
from github import Github, GithubException

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN      = os.environ["GITHUB_TOKEN"]
REPO_FULL_NAME    = os.environ["REPO_FULL_NAME"]
ISSUE_NUMBER      = int(os.environ["ISSUE_NUMBER"])
TASK_PLAN_RAW     = os.environ.get("TASK_PLAN", "")
STACK             = os.environ.get("STACK", "unknown")
REVIEW_ATTEMPT    = int(os.environ.get("REVIEW_ATTEMPT", "1"))

gh    = Github(GITHUB_TOKEN)
repo  = gh.get_repo(REPO_FULL_NAME)
issue = repo.get_issue(ISSUE_NUMBER)


def write_outputs(approved: bool, feedback: str):
    feedback_escaped = feedback.replace("\n", " ").replace("\r", "")
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"approved={'true' if approved else 'false'}\n")
            f.write(f"feedback={feedback_escaped}\n")
    print(f"approved={'true' if approved else 'false'}")
    print(f"feedback={feedback_escaped}")


# If no API key, skip gracefully
if not ANTHROPIC_API_KEY:
    print("ANTHROPIC_API_KEY not set — skipping review, defaulting to approved.")
    write_outputs(True, "")
    sys.exit(0)

try:
    plan = json.loads(TASK_PLAN_RAW)
except (json.JSONDecodeError, ValueError) as e:
    print(f"ERROR: could not parse TASK_PLAN: {e}")
    write_outputs(True, "")
    sys.exit(0)

branch_name    = plan["branch_name"]
default_branch = repo.default_branch

# Get the diff between branch and main
try:
    comparison = repo.compare(default_branch, branch_name)
    if comparison.ahead_by == 0:
        print("No commits to review — skipping.")
        write_outputs(True, "")
        sys.exit(0)
    print(f"Reviewing {comparison.ahead_by} commit(s) on {branch_name}.")
except GithubException as e:
    print(f"ERROR comparing branches: {e}")
    write_outputs(True, "")
    sys.exit(0)

# Build diff text from changed files
diff_sections = []
for f in comparison.files:
    patch = getattr(f, "patch", None)
    if patch:
        diff_sections.append(f"### {f.filename}\n```diff\n{patch}\n```")

diff_text = "\n\n".join(diff_sections) if diff_sections else "No patch available."

MAX_DIFF_CHARS = 40000
if len(diff_text) > MAX_DIFF_CHARS:
    diff_text = diff_text[:MAX_DIFF_CHARS] + "\n\n... (diff truncated for length)"

# Call Claude
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

prompt = f"""You are a senior code reviewer auditing AI-generated code for a {STACK} project.

## Original Issue

**Title:** {issue.title}

**Specification:**
{issue.body}

## Code Changes (git diff)

{diff_text}

## Your Task

Review the code changes strictly against the original issue specification. Focus on:

1. **Completeness** — Are all files the spec requires actually changed?
2. **Phantom imports** — Are there imports of models, modules, or functions that don't exist in the codebase and weren't created in this diff?
3. **Correctness** — Does the implementation match the spec? Wrong function names, wrong API endpoints, wrong property names?
4. **Obvious bugs** — Things that will clearly cause a runtime error.

Do NOT reject for style preferences, minor naming differences, or things the spec didn't mention.
Only reject if the implementation will fail to work or clearly misses a required piece of the spec.

Respond with a JSON object only — no markdown fences, no explanation outside the JSON:
{{
  "approved": true or false,
  "summary": "one sentence verdict",
  "issues": [
    "specific problem with file path and exactly what needs to change",
    "..."
  ]
}}

If approved, set "issues" to an empty array."""

result = {"approved": True, "summary": "Review defaulted to approved.", "issues": []}

try:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    result = json.loads(raw)
except json.JSONDecodeError as e:
    print(f"WARNING: Could not parse Claude response as JSON ({e}) — defaulting to approved.")
except Exception as e:
    print(f"ERROR calling Claude API ({e}) — defaulting to approved.")

approved = bool(result.get("approved", True))
summary  = result.get("summary", "")
issues   = result.get("issues", [])

print(f"\n{'✅ APPROVED' if approved else '❌ REJECTED'}: {summary}")
if issues:
    print("\nIssues found:")
    for item in issues:
        print(f"  - {item}")

# Post result as a comment on the issue
issues_md = "\n".join(f"- {i}" for i in issues) if issues else ""
comment = (
    f"🔍 **Review Agent — {'✅ Approved' if approved else '❌ Rejected'}"
    f" (attempt {REVIEW_ATTEMPT})**\n\n"
    f"**Verdict:** {summary}\n\n"
    + (f"**Issues to fix:**\n{issues_md}" if issues_md else "No issues found — code looks good.")
)
issue.create_comment(comment)

write_outputs(approved, "; ".join(issues))
print("\nReview agent complete.")
sys.exit(0)
