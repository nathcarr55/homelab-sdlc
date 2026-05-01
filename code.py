#!/usr/bin/env python3
"""
Coding agent — takes the task plan from the planning agent, fetches relevant
file contents, generates code changes, and commits them to a new branch.
"""

import os
import json
import sys
import base64
from openai import OpenAI
from github import Github, GithubException

# ── Config from environment ──────────────────────────────────────────────────
LITELLM_URL    = os.environ["LITELLM_URL"]
LITELLM_KEY    = os.environ["LITELLM_MASTER_KEY"]
GITHUB_TOKEN   = os.environ["GITHUB_TOKEN"]
REPO_FULL_NAME = os.environ["REPO_FULL_NAME"]
ISSUE_NUMBER   = int(os.environ["ISSUE_NUMBER"])
STACK          = os.environ.get("STACK", "unknown")
TASK_PLAN_RAW  = os.environ.get("TASK_PLAN", "")
TEST_FAILURE   = os.environ.get("TEST_FAILURE", "false").lower() == "true"

# ── Clients ───────────────────────────────────────────────────────────────────
llm  = OpenAI(base_url=f"{LITELLM_URL}/v1", api_key=LITELLM_KEY)
gh   = Github(GITHUB_TOKEN)
repo = gh.get_repo(REPO_FULL_NAME)
issue = repo.get_issue(ISSUE_NUMBER)

# ── Parse task plan ───────────────────────────────────────────────────────────
try:
    plan = json.loads(TASK_PLAN_RAW)
except (json.JSONDecodeError, ValueError) as e:
    print(f"ERROR: could not parse TASK_PLAN: {e}")
    sys.exit(1)

branch_name = plan["branch_name"]
tasks       = plan["tasks"]

# ── Create or reset branch ────────────────────────────────────────────────────
default_branch = repo.default_branch
base_sha = repo.get_branch(default_branch).commit.sha

try:
    repo.get_branch(branch_name)
    print(f"Branch {branch_name} already exists, reusing.")
except GithubException:
    repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_sha)
    print(f"Created branch: {branch_name}")

# ── Helper: fetch file content from repo ─────────────────────────────────────
def fetch_file(path: str) -> tuple[str, str]:
    """Returns (content, sha) or raises."""
    try:
        f = repo.get_contents(path, ref=branch_name)
        content = base64.b64decode(f.content).decode("utf-8", errors="replace")
        return content, f.sha
    except GithubException:
        return "", ""  # new file

# ── Helper: commit file to branch ────────────────────────────────────────────
def commit_file(path: str, content: str, sha: str, message: str):
    if sha:
        repo.update_file(path, message, content, sha, branch=branch_name)
    else:
        repo.create_file(path, message, content, branch=branch_name)

# ── System prompt for coding ─────────────────────────────────────────────────
SYSTEM = f"""You are an expert {STACK} developer making precise, minimal code changes.

Rules:
- Return ONLY the complete new file content — no explanations, no markdown fences, no preamble.
- Preserve all existing code not related to the task.
- Follow the existing code style exactly.
- If creating a new file, write clean, idiomatic code for the stack.
- Do not add unnecessary comments.
{"- IMPORTANT: Tests just failed. Fix the issue carefully." if TEST_FAILURE else ""}
"""

# ── Process each task ─────────────────────────────────────────────────────────
committed_files = []

for task in tasks:
    desc  = task["description"]
    files = task.get("files_to_change", [])
    ttype = task.get("type", "change")

    print(f"\nTask [{ttype}]: {desc}")

    for file_path in files:
        print(f"  Processing: {file_path}")
        existing_content, file_sha = fetch_file(file_path)

        user_prompt = f"""Task: {desc}

File: {file_path}
{"Existing content:" if existing_content else "This is a new file."}
{existing_content if existing_content else ""}

Issue context: {plan.get('summary', '')}
Notes: {plan.get('notes', '')}

Return the complete new file content:"""

        try:
            response = llm.chat.completions.create(
                model="coding",
                messages=[
                    {"role": "system", "content": SYSTEM},
                    {"role": "user",   "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=4000,
            )
            new_content = response.choices[0].message.content.strip()

            # Strip accidental markdown fences if model adds them
            if new_content.startswith("```"):
                lines = new_content.split("\n")
                new_content = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

        except Exception as e:
            print(f"  ERROR calling LiteLLM for {file_path}: {e}")
            continue

        # Skip if nothing changed
        if new_content == existing_content:
            print(f"  No changes needed for {file_path}")
            continue

        commit_msg = f"ai: {desc[:60]} (#{ISSUE_NUMBER})"
        try:
            commit_file(file_path, new_content, file_sha, commit_msg)
            committed_files.append(file_path)
            print(f"  Committed: {file_path}")
        except Exception as e:
            print(f"  ERROR committing {file_path}: {e}")

# ── Summary comment on issue ──────────────────────────────────────────────────
if committed_files:
    files_list = "\n".join(f"- `{f}`" for f in committed_files)
    issue.create_comment(
        f"🤖 **AI Agent — Code Changes**\n\n"
        f"{'⚠️ Retrying after test failure.' if TEST_FAILURE else ''}\n\n"
        f"Committed changes to `{branch_name}`:\n{files_list}"
    )
    print(f"\nCoding complete. {len(committed_files)} file(s) committed to {branch_name}.")
else:
    print("\nNo files were changed.")
    issue.create_comment("🤖 **AI Agent** — No file changes were necessary for this task.")
