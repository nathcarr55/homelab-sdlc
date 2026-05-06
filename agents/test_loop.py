#!/usr/bin/env python3
"""
Test loop agent — runs the test command, feeds failure output to the coding
agent, and repeats until tests pass or MAX_ATTEMPTS is exhausted.
"""

import os
import sys
import subprocess
from github import Github

GITHUB_TOKEN   = os.environ["GITHUB_TOKEN"]
REPO_FULL_NAME = os.environ["REPO_FULL_NAME"]
ISSUE_NUMBER   = int(os.environ["ISSUE_NUMBER"])
TEST_COMMAND   = os.environ.get("TEST_COMMAND", "echo no tests configured")
MAX_ATTEMPTS   = int(os.environ.get("MAX_ATTEMPTS", "4"))
LOOP_LABEL     = os.environ.get("LOOP_LABEL", "")  # e.g. "post-review" for second cycle

gh    = Github(GITHUB_TOKEN)
repo  = gh.get_repo(REPO_FULL_NAME)
issue = repo.get_issue(ISSUE_NUMBER)

label = f" ({LOOP_LABEL})" if LOOP_LABEL else ""


def run_tests() -> tuple[bool, str]:
    result = subprocess.run(
        TEST_COMMAND,
        shell=True,
        executable="/bin/bash",
        capture_output=True,
        text=True,
        timeout=600,
    )
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


def run_coding_agent(test_output: str):
    env = {
        **os.environ,
        "TEST_FAILURE": "true",
        "TEST_OUTPUT": test_output[-6000:],
        "REVIEW_FEEDBACK": os.environ.get("REVIEW_FEEDBACK", ""),
    }
    subprocess.run([sys.executable, ".sdlc/agents/code.py"], env=env)


tests_passed = False

for attempt in range(1, MAX_ATTEMPTS + 1):
    print(f"\n{'='*55}")
    print(f"Test loop{label} — attempt {attempt}/{MAX_ATTEMPTS}")
    print("="*55)

    passed, output = run_tests()

    if passed:
        print(f"✅ Tests passed on attempt {attempt}!")
        tests_passed = True
        issue.create_comment(
            f"✅ **Test Loop{label}** — Tests passed on attempt {attempt}/{MAX_ATTEMPTS}."
        )
        break

    print(f"❌ Tests failed on attempt {attempt}.")
    tail = output[-3000:] if len(output) > 3000 else output

    if attempt < MAX_ATTEMPTS:
        print("Running coding agent with failure output...")
        issue.create_comment(
            f"🔄 **Test Loop{label}** — Attempt {attempt}/{MAX_ATTEMPTS} failed. "
            f"Retrying with coding agent.\n\n"
            f"<details><summary>Test output</summary>\n\n```\n{tail}\n```\n</details>"
        )
        run_coding_agent(output)
    else:
        print(f"Max attempts ({MAX_ATTEMPTS}) reached — tests still failing.")
        issue.create_comment(
            f"⚠️ **Test Loop{label}** — Tests still failing after {MAX_ATTEMPTS} attempts.\n\n"
            f"<details><summary>Last test output</summary>\n\n```\n{tail}\n```\n</details>"
        )

github_output = os.environ.get("GITHUB_OUTPUT", "")
if github_output:
    with open(github_output, "a") as f:
        f.write(f"tests_passed={'true' if tests_passed else 'false'}\n")

print(f"\nTest loop complete. Passed: {tests_passed}")
sys.exit(0 if tests_passed else 1)
