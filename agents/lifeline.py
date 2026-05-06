#!/usr/bin/env python3
"""
Lifeline agent — called when the test loop exhausts all attempts.
Uses Claude Sonnet directly to diagnose test failures and produce targeted fixes.
"""

import os
import sys
import subprocess
import json
import base64
import anthropic
from github import Github, GithubException

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_TOKEN      = os.environ["GITHUB_TOKEN"]
REPO_FULL_NAME    = os.environ["REPO_FULL_NAME"]
ISSUE_NUMBER      = int(os.environ["ISSUE_NUMBER"])
TASK_PLAN_RAW     = os.environ.get("TASK_PLAN", "")
STACK             = os.environ.get("STACK", "unknown")
TEST_COMMAND      = os.environ.get("TEST_COMMAND", "echo no tests configured")

gh    = Github(GITHUB_TOKEN)
repo  = gh.get_repo(REPO_FULL_NAME)
issue = repo.get_issue(ISSUE_NUMBER)

try:
    plan = json.loads(TASK_PLAN_RAW)
except (json.JSONDecodeError, ValueError) as e:
    print(f"ERROR: could not parse TASK_PLAN: {e}")
    sys.exit(1)

branch_name    = plan["branch_name"]
default_branch = repo.default_branch

# Run tests to get current failure output
print("Running tests to capture fresh failure output...")
proc = subprocess.run(
    TEST_COMMAND, shell=True, executable="/bin/bash", capture_output=True, text=True, timeout=600
)
test_output = (proc.stdout + proc.stderr).strip()

if proc.returncode == 0:
    print("Tests are already passing — lifeline not needed.")
    sys.exit(0)

print(f"Tests failed. Sending to Claude for diagnosis...")

# Get the diff between branch and main
diff_text = "Diff unavailable."
changed_files = []
try:
    comparison = repo.compare(default_branch, branch_name)
    diff_sections = []
    for f in comparison.files:
        patch = getattr(f, "patch", None)
        if patch:
            diff_sections.append(f"### {f.filename}\n```diff\n{patch}\n```")
        # Also fetch current file content for context
        try:
            obj = repo.get_contents(f.filename, ref=branch_name)
            content = base64.b64decode(obj.content).decode("utf-8", errors="replace")
            changed_files.append({"path": f.filename, "content": content, "sha": obj.sha})
        except GithubException:
            pass
    diff_text = "\n\n".join(diff_sections) or "No patches available."
    if len(diff_text) > 25000:
        diff_text = diff_text[:25000] + "\n\n... (diff truncated)"
except GithubException as e:
    print(f"WARNING: Could not fetch diff: {e}")

# Build current file contents section
files_section = ""
for f in changed_files:
    snippet = f["content"][:4000]
    files_section += f"\n### {f['path']}\n```\n{snippet}\n```\n"

sha_lookup = {f["path"]: f["sha"] for f in changed_files}

# Call Claude for diagnosis + fixes
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

prompt = f"""You are a senior {STACK} developer. A coding agent has been trying to implement a feature but the test suite keeps failing after {4} attempts. You are the lifeline.

## Test Failure Output (most recent run)
```
{test_output[-6000:]}
```

## Code Changes (git diff vs main)
{diff_text}

## Current File Contents (files changed on this branch)
{files_section}

## Your Task
1. Diagnose the exact root cause of the test failures.
2. Produce complete, corrected file contents for every file that needs to change.

Be precise — look at the actual error messages and tracebacks, not just the diff.

Respond with a JSON object only — no markdown fences, no explanation outside the JSON:
{{
  "diagnosis": "clear explanation of root cause",
  "fixes": [
    {{
      "path": "relative/path/to/file.ext",
      "content": "complete corrected file content"
    }}
  ]
}}

Only include files that genuinely need to change to fix the failures."""

try:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    result_data = json.loads(raw)
except json.JSONDecodeError as e:
    print(f"ERROR: Could not parse Claude response as JSON: {e}")
    issue.create_comment(
        "🆘 **Lifeline Agent** — Could not parse Claude's response. Manual intervention needed.\n\n"
        f"Parse error: {e}"
    )
    sys.exit(1)
except Exception as e:
    print(f"ERROR: Claude API call failed: {e}")
    sys.exit(1)

diagnosis = result_data.get("diagnosis", "")
fixes     = result_data.get("fixes", [])

print(f"\nDiagnosis: {diagnosis}")
print(f"Fixes to apply: {len(fixes)} file(s)")

# Commit the fixes
committed = []
for fix in fixes:
    path    = fix.get("path", "")
    content = fix.get("content", "")
    if not path or not content:
        continue
    sha = sha_lookup.get(path, "")
    try:
        if sha:
            repo.update_file(
                path,
                f"ai(lifeline): fix test failures in {path} (#{ISSUE_NUMBER})",
                content,
                sha,
                branch=branch_name,
            )
        else:
            repo.create_file(
                path,
                f"ai(lifeline): create {path} (#{ISSUE_NUMBER})",
                content,
                branch=branch_name,
            )
        committed.append(path)
        print(f"  Fixed: {path}")
    except Exception as e:
        print(f"  ERROR committing {path}: {e}")

if committed:
    files_md = "\n".join(f"- `{f}`" for f in committed)
    issue.create_comment(
        f"🆘 **Lifeline Agent (Claude Sonnet)**\n\n"
        f"**Diagnosis:** {diagnosis}\n\n"
        f"**Fixed files:**\n{files_md}"
    )
else:
    issue.create_comment(
        f"🆘 **Lifeline Agent (Claude Sonnet)**\n\n"
        f"**Diagnosis:** {diagnosis}\n\n"
        f"No file changes committed — the fix may require manual review."
    )

print(f"\nLifeline complete. {len(committed)} file(s) committed.")
sys.exit(0)
