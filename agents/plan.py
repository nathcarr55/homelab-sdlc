#!/usr/bin/env python3
"""
Planning agent — reads a GitHub issue and produces a structured task plan.
Outputs task_plan as a GitHub Actions step output for downstream agents.
"""

import os
import json
import sys
from openai import OpenAI
from github import Github

# ── Config from environment ──────────────────────────────────────────────────
LITELLM_URL      = os.environ["LITELLM_URL"]
LITELLM_KEY      = os.environ["LITELLM_MASTER_KEY"]
GITHUB_TOKEN     = os.environ["GITHUB_TOKEN"]
REPO_FULL_NAME   = os.environ["REPO_FULL_NAME"]
ISSUE_NUMBER     = int(os.environ["ISSUE_NUMBER"])
ISSUE_TITLE      = os.environ.get("ISSUE_TITLE", "")
ISSUE_BODY       = os.environ.get("ISSUE_BODY", "")
STACK            = os.environ.get("STACK", "unknown")

# ── Clients ───────────────────────────────────────────────────────────────────
llm = OpenAI(base_url=f"{LITELLM_URL}/v1", api_key=LITELLM_KEY)
gh  = Github(GITHUB_TOKEN)
repo = gh.get_repo(REPO_FULL_NAME)
issue = repo.get_issue(ISSUE_NUMBER)

# ── Get repo file tree for context ────────────────────────────────────────────
def get_file_tree(max_files: int = 60) -> str:
    try:
        contents = repo.get_git_tree("HEAD", recursive=True)
        files = [f.path for f in contents.tree if f.type == "blob"][:max_files]
        return "\n".join(files)
    except Exception as e:
        return f"(could not fetch file tree: {e})"

file_tree = get_file_tree()

# ── Build the planning prompt ─────────────────────────────────────────────────
system_prompt = f"""You are a senior software engineer planning work on a {STACK} codebase.
Given a GitHub issue, produce a precise, actionable task plan.

Respond ONLY with a JSON object — no preamble, no markdown fences. Schema:
{{
  "summary": "one sentence describing what needs to be done",
  "branch_name": "kebab-case branch name starting with ai/",
  "tasks": [
    {{
      "id": 1,
      "description": "what to do",
      "files_to_change": ["path/to/file.py"],
      "type": "feature|bugfix|refactor|test|docs"
    }}
  ],
  "notes": "any caveats or things the coding agent should watch out for"
}}

Keep tasks small and discrete. Max 5 tasks. files_to_change should be real paths from the file tree.
"""

user_prompt = f"""Issue #{ISSUE_NUMBER}: {ISSUE_TITLE}

{ISSUE_BODY}

Repository file tree:
{file_tree}
"""

# ── Call LiteLLM ──────────────────────────────────────────────────────────────
print(f"Planning issue #{ISSUE_NUMBER}: {ISSUE_TITLE}")

try:
    response = llm.chat.completions.create(
        model="smart",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=1000,
    )
    raw = response.choices[0].message.content.strip()
    plan = json.loads(raw)
except json.JSONDecodeError:
    print(f"ERROR: LLM returned non-JSON:\n{raw}")
    sys.exit(1)
except Exception as e:
    print(f"ERROR calling LiteLLM: {e}")
    sys.exit(1)

# ── Validate ──────────────────────────────────────────────────────────────────
required_keys = {"summary", "branch_name", "tasks"}
if not required_keys.issubset(plan.keys()):
    print(f"ERROR: plan missing keys. Got: {list(plan.keys())}")
    sys.exit(1)

print(f"Plan: {plan['summary']}")
print(f"Branch: {plan['branch_name']}")
print(f"Tasks: {len(plan['tasks'])}")
for t in plan["tasks"]:
    print(f"  [{t['type']}] {t['description']}")

# ── Post plan as issue comment ────────────────────────────────────────────────
comment_lines = [
    "🤖 **AI Agent — Task Plan**",
    "",
    f"**Summary:** {plan['summary']}",
    f"**Branch:** `{plan['branch_name']}`",
    "",
    "**Tasks:**",
]
for t in plan["tasks"]:
    files = ", ".join(f"`{f}`" for f in t.get("files_to_change", []))
    comment_lines.append(f"- [{t['type']}] {t['description']} → {files}")

if plan.get("notes"):
    comment_lines += ["", f"**Notes:** {plan['notes']}"]

issue.create_comment("\n".join(comment_lines))

# ── Write output for next step ────────────────────────────────────────────────
plan_json = json.dumps(plan)
with open(os.environ["GITHUB_OUTPUT"], "a") as f:
    # GitHub Actions multiline output encoding
    f.write("task_plan<<EOF\n")
    f.write(plan_json + "\n")
    f.write("EOF\n")

print("Planning complete.")
