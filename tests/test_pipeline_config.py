"""
Static validation suite for the SDLC pipeline configuration.

Catches misconfigurations without API calls or network access in < 10 seconds.

What this tests:
  - All agent scripts are syntactically valid Python
  - No imports of packages not installed by the workflow
  - Every env var an agent hard-requires is actually provided by its workflow step
  - Step output key names match what downstream steps reference
  - Workflow YAML structure and permissions are correct
  - ai-task.yml forwards all secrets that agents need (including the ANTHROPIC_API_KEY
    trap: lifeline.py hard-requires it but ai-task.yml doesn't forward it by default)
  - Subprocess paths in test_loop.py match the checkout path in the workflow
  - Sandbox baseline tests are green

Run with: pytest tests/ -v
Requires:  pip install pytest pyyaml
"""

import ast
import os
import re
import subprocess
import sys
import pytest

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS_DIR = os.path.join(REPO_ROOT, "agents")
WORKFLOWS_DIR = os.path.join(REPO_ROOT, ".github", "workflows")
SANDBOX_DIR = os.path.join(REPO_ROOT, "sandbox")

AGENT_FILES = ["plan.py", "code.py", "test_loop.py", "lifeline.py", "review.py", "pr.py"]

# Packages installed by the workflow step "Install agent dependencies"
# 'github' is the importable module name for PyGithub
INSTALLED_PACKAGES = {"openai", "github", "anthropic", "requests"}

# Env vars that GitHub Actions injects automatically — not required in step env sections
GH_AUTO_VARS = {
    "GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_WORKSPACE", "GITHUB_SHA",
    "GITHUB_REF", "GITHUB_RUN_ID", "GITHUB_ACTOR", "GITHUB_REPOSITORY",
    "GITHUB_EVENT_NAME", "RUNNER_OS",
}

SKIP_NO_YAML = pytest.mark.skipif(not HAS_YAML, reason="pyyaml not installed: pip install pyyaml")


# ── Helpers ───────────────────────────────────────────────────────────────────

def agent_path(name: str) -> str:
    return os.path.join(AGENTS_DIR, name)


def read_agent(name: str) -> str:
    with open(agent_path(name)) as f:
        return f.read()


def parse_agent(name: str) -> ast.Module:
    return ast.parse(read_agent(name), filename=name)


def extract_env_reads(tree: ast.AST) -> tuple[set[str], set[str]]:
    """
    Returns (required, optional) env var names read by the agent.
      required: os.environ["KEY"]
      optional: os.environ.get("KEY", ...)
    """
    required, optional = set(), set()
    for node in ast.walk(tree):
        # os.environ["KEY"]
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "os"
        ):
            key = getattr(node.slice, "value", None)
            if isinstance(key, str):
                required.add(key)
        # os.environ.get("KEY", ...)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "environ"
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "os"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            optional.add(node.args[0].value)
    return required, optional


def extract_github_output_keys(source: str) -> set[str]:
    """
    Extract keys written to GITHUB_OUTPUT. Handles:
      f.write("key<<EOF\n")           # multiline heredoc (plan.py)
      f.write(f"key={value}\n")       # f-string (test_loop, review)
    """
    keys = set()
    keys.update(re.findall(r'"(\w+)<<EOF', source))
    keys.update(re.findall(r'f"(\w+)=', source))
    return keys


def load_workflow(filename: str) -> dict:
    path = os.path.join(WORKFLOWS_DIR, filename)
    with open(path) as f:
        return yaml.safe_load(f)


def workflow_triggers(wf: dict) -> dict:
    """
    Return the 'on:' (triggers) section of a workflow.

    PyYAML YAML 1.1 parses the bare word 'on' as the boolean True, so the
    key in the parsed dict may be True (bool) rather than "on" (str).
    """
    return wf.get("on") or wf.get(True) or {}


def get_step_by_name(workflow: dict, name: str) -> dict:
    for step in workflow["jobs"]["ai-task"]["steps"]:
        if step.get("name") == name:
            return step
    pytest.fail(f"Step '{name}' not found in sdlc.yml — step was renamed or removed")


def get_step_env_keys(step: dict) -> set[str]:
    return set(step.get("env", {}).keys())


# ── Agent Files ───────────────────────────────────────────────────────────────

class TestAgentFiles:

    @pytest.mark.parametrize("agent", AGENT_FILES)
    def test_file_exists(self, agent):
        assert os.path.exists(agent_path(agent)), (
            f"agents/{agent} is missing — workflow will fail at the step that runs it"
        )

    @pytest.mark.parametrize("agent", AGENT_FILES)
    def test_syntax_valid(self, agent):
        try:
            parse_agent(agent)
        except SyntaxError as e:
            pytest.fail(f"agents/{agent} has a syntax error: {e}")

    @pytest.mark.parametrize("agent", AGENT_FILES)
    def test_no_unlisted_imports(self, agent):
        """Only packages installed by the workflow may be imported."""
        stdlib = {
            "os", "sys", "json", "subprocess", "base64", "re", "ast",
            "pathlib", "typing", "collections", "functools", "itertools",
            "contextlib", "io", "time", "datetime", "math", "random",
            "hashlib", "hmac", "struct", "threading", "concurrent",
            "urllib", "http", "socket", "shutil", "tempfile",
        }
        tree = parse_agent(agent)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                tops = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                tops = [node.module.split(".")[0]] if node.module else []
            else:
                continue
            for top in tops:
                if top not in stdlib and top not in INSTALLED_PACKAGES:
                    pytest.fail(
                        f"agents/{agent} imports '{top}' which is not installed by the workflow.\n"
                        f"Installed: openai, PyGithub (→ github), requests, anthropic.\n"
                        f"Add it to the 'Install agent dependencies' step in sdlc.yml."
                    )


# ── Env Var Contracts ─────────────────────────────────────────────────────────

@SKIP_NO_YAML
class TestEnvVarContracts:
    """
    Each workflow step must provide every env var that its agent hard-requires
    (os.environ["KEY"], not os.environ.get("KEY")).

    A mismatch here causes a KeyError at runtime — the agent crashes immediately
    before doing any useful work.
    """

    def _assert_step_covers_agent(self, agent: str, step_name: str):
        tree = parse_agent(agent)
        required, _ = extract_env_reads(tree)
        required -= GH_AUTO_VARS

        workflow = load_workflow("sdlc.yml")
        step = get_step_by_name(workflow, step_name)
        provided = get_step_env_keys(step)

        missing = required - provided
        assert not missing, (
            f"agents/{agent} hard-requires env vars not provided by step '{step_name}':\n"
            f"  Missing: {sorted(missing)}\n"
            f"  Add them to the step's env: section in sdlc.yml."
        )

    def test_plan_agent(self):
        self._assert_step_covers_agent("plan.py", "Run planning agent")

    def test_code_agent(self):
        self._assert_step_covers_agent("code.py", "Run coding agent")

    def test_test_loop(self):
        self._assert_step_covers_agent("test_loop.py", "Test and fix loop")

    def test_lifeline(self):
        self._assert_step_covers_agent(
            "lifeline.py", "Claude lifeline — diagnose and fix persistent failures"
        )

    def test_review_agent(self):
        self._assert_step_covers_agent("review.py", "Run review agent")

    def test_pr_agent(self):
        self._assert_step_covers_agent("pr.py", "Open pull request")

    def test_code_optional_vars_are_not_required(self):
        """
        code.py receives TEST_FAILURE/TEST_OUTPUT/REVIEW_FEEDBACK only when called
        as a retry (subprocess from test_loop or direct step after review rejection).
        They must be os.environ.get() so the initial coding step doesn't crash.
        """
        tree = parse_agent("code.py")
        required, _ = extract_env_reads(tree)
        for var in ("TEST_FAILURE", "TEST_OUTPUT", "REVIEW_FEEDBACK"):
            assert var not in required, (
                f"code.py uses os.environ['{var}'] but this var is only set during retries. "
                f"Change it to os.environ.get('{var}', ...) so the initial coding step doesn't crash."
            )


# ── Step Output Key Alignment ─────────────────────────────────────────────────

class TestStepOutputs:
    """
    Keys that agents write to GITHUB_OUTPUT must match exactly what downstream
    workflow steps reference. A name mismatch silently produces an empty string.
    """

    def test_plan_writes_task_plan(self):
        keys = extract_github_output_keys(read_agent("plan.py"))
        assert "task_plan" in keys, (
            "plan.py must write 'task_plan' to GITHUB_OUTPUT.\n"
            "The workflow uses steps.plan.outputs.task_plan — a name mismatch silently "
            "passes an empty TASK_PLAN to the coding agent."
        )

    def test_test_loop_writes_tests_passed(self):
        keys = extract_github_output_keys(read_agent("test_loop.py"))
        assert "tests_passed" in keys, (
            "test_loop.py must write 'tests_passed' to GITHUB_OUTPUT.\n"
            "The workflow conditions lifeline/review/PR on steps.test_loop.outputs.tests_passed."
        )

    def test_review_writes_approved(self):
        keys = extract_github_output_keys(read_agent("review.py"))
        assert "approved" in keys, (
            "review.py must write 'approved' to GITHUB_OUTPUT.\n"
            "The workflow conditions the retry coding step on steps.review.outputs.approved == 'false'."
        )

    def test_review_writes_feedback(self):
        keys = extract_github_output_keys(read_agent("review.py"))
        assert "feedback" in keys, (
            "review.py must write 'feedback' to GITHUB_OUTPUT.\n"
            "The workflow passes steps.review.outputs.feedback as REVIEW_FEEDBACK to the retry coding step."
        )

    def test_boolean_outputs_are_lowercase(self):
        """
        Workflow conditions use == 'true' / == 'false' (lowercase).
        Python's str(True) == 'True' (capitalized) would never match.
        """
        for agent, keys in [("test_loop.py", ["tests_passed"]), ("review.py", ["approved"])]:
            source = read_agent(agent)
            for key in keys:
                assert f"{key}=True" not in source, (
                    f"agents/{agent} writes '{key}=True' (Python bool) to GITHUB_OUTPUT. "
                    f"The workflow checks == 'true' (lowercase). Use 'true'/'false' strings."
                )
                assert f"{key}=False" not in source, (
                    f"agents/{agent} writes '{key}=False' (Python bool) to GITHUB_OUTPUT. "
                    f"Use 'true'/'false' strings."
                )


# ── Workflow YAML Structure ───────────────────────────────────────────────────

@SKIP_NO_YAML
class TestWorkflowStructure:

    def test_sdlc_parses(self):
        wf = load_workflow("sdlc.yml")
        assert "jobs" in wf and "ai-task" in wf["jobs"]

    def test_ai_task_parses(self):
        wf = load_workflow("ai-task.yml")
        assert "jobs" in wf

    def test_sdlc_permissions(self):
        """Pipeline needs write access to commit code, comment on issues, and open PRs."""
        perms = load_workflow("sdlc.yml").get("permissions", {})
        for perm, level in [("contents", "write"), ("issues", "write"), ("pull-requests", "write")]:
            assert perms.get(perm) == level, (
                f"sdlc.yml missing 'permissions.{perm}: {level}'. "
                f"Without it the step that needs it will get a 403."
            )

    def test_sdlc_declares_required_secrets(self):
        secrets = workflow_triggers(load_workflow("sdlc.yml"))["workflow_call"]["secrets"]
        for s in ("TAILSCALE_AUTHKEY", "LITELLM_URL", "LITELLM_MASTER_KEY"):
            assert s in secrets, f"sdlc.yml does not declare secret {s}"
            # PyYAML parses 'required: true' as {'required': True} (Python bool)
            assert secrets[s].get("required") == True, f"sdlc.yml secret {s} should be required: true"  # noqa: E712

    def test_ai_task_forwards_required_secrets(self):
        """All required secrets in sdlc.yml must be explicitly forwarded by ai-task.yml."""
        sdlc = load_workflow("sdlc.yml")
        ai_task = load_workflow("ai-task.yml")

        required = {
            name for name, cfg in workflow_triggers(sdlc)["workflow_call"]["secrets"].items()
            if cfg and cfg.get("required")
        }
        forwarded = set()
        for job in ai_task["jobs"].values():
            forwarded.update(job.get("secrets", {}).keys())

        missing = required - forwarded
        assert not missing, (
            f"ai-task.yml does not forward these required secrets to sdlc.yml: {missing}\n"
            f"Add them to the 'secrets:' block of the calling job."
        )

    def test_ai_task_forwards_anthropic_key(self):
        """
        CRITICAL: lifeline.py uses os.environ['ANTHROPIC_API_KEY'] (hard required).
        If the lifeline step triggers (all test loop attempts exhausted) and
        ANTHROPIC_API_KEY is not forwarded, the agent crashes with a KeyError/auth error.

        review.py handles the missing key gracefully (defaults to approved),
        but lifeline.py does NOT — it requires the key unconditionally.
        """
        ai_task = load_workflow("ai-task.yml")
        forwarded = set()
        for job in ai_task["jobs"].values():
            forwarded.update(job.get("secrets", {}).keys())

        assert "ANTHROPIC_API_KEY" in forwarded, (
            "ai-task.yml does not forward ANTHROPIC_API_KEY.\n"
            "lifeline.py uses os.environ['ANTHROPIC_API_KEY'] and will crash if\n"
            "the test loop exhausts all attempts and the lifeline step is triggered.\n"
            "Add 'ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}' to the secrets block."
        )

    def test_plan_step_before_code_step(self):
        """Coding agent depends on task_plan output from planning agent."""
        steps = load_workflow("sdlc.yml")["jobs"]["ai-task"]["steps"]
        names = [s.get("name", "") for s in steps]
        plan_idx = next(i for i, n in enumerate(names) if "planning agent" in n.lower())
        code_idx = next(i for i, n in enumerate(names) if n == "Run coding agent")
        assert plan_idx < code_idx, "Planning agent step must appear before coding agent step"

    def test_test_loop_steps_have_continue_on_error(self):
        """
        All test loop steps need continue-on-error: true so the pipeline continues
        to lifeline/review/PR even when tests fail. Without it, the entire job stops
        on first test failure and no PR is opened.
        """
        steps = load_workflow("sdlc.yml")["jobs"]["ai-task"]["steps"]
        loop_ids = {"test_loop", "test_loop_lifeline", "test_loop_review"}
        for step in steps:
            if step.get("id") in loop_ids:
                assert step.get("continue-on-error") is True, (
                    f"Step id='{step['id']}' must have 'continue-on-error: true'.\n"
                    f"Without it, the job stops on test failure and skips lifeline/review/PR."
                )

    def test_lifeline_triggers_on_test_loop_failure(self):
        """Lifeline condition must check test_loop output, not a hardcoded value."""
        steps = load_workflow("sdlc.yml")["jobs"]["ai-task"]["steps"]
        lifeline = next(s for s in steps if s.get("id") == "lifeline")
        cond = lifeline.get("if", "")
        assert "test_loop" in cond and "tests_passed" in cond, (
            f"Lifeline 'if' condition looks wrong: '{cond}'\n"
            f"Expected something like: steps.test_loop.outputs.tests_passed == 'false'"
        )

    def test_review_runs_after_lifeline_success(self):
        """
        Review step must check BOTH test_loop and test_loop_lifeline outputs.
        If only test_loop is checked, a successful lifeline fix is ignored and
        review never runs — the PR is created without a code review.
        """
        steps = load_workflow("sdlc.yml")["jobs"]["ai-task"]["steps"]
        review = next(s for s in steps if s.get("id") == "review")
        cond = review.get("if", "")
        assert "test_loop_lifeline" in cond, (
            f"Review 'if' condition does not include test_loop_lifeline: '{cond}'\n"
            f"A lifeline fix that makes tests pass should also trigger the review."
        )

    def test_pr_aggregates_all_test_loop_results(self):
        """
        The PR step's TESTS_PASSED env var must reflect the result of all three
        test loop phases, not just the first one.
        """
        steps = load_workflow("sdlc.yml")["jobs"]["ai-task"]["steps"]
        pr_step = next(s for s in steps if s.get("name") == "Open pull request")
        tests_passed_expr = str(pr_step.get("env", {}).get("TESTS_PASSED", ""))
        for loop_id in ("test_loop_review", "test_loop_lifeline", "test_loop"):
            assert loop_id in tests_passed_expr, (
                f"PR step TESTS_PASSED expression is missing '{loop_id}'.\n"
                f"Current: {tests_passed_expr}"
            )

    def test_agent_scripts_checked_out_to_sdlc(self):
        """
        test_loop.py calls subprocess('.sdlc/agents/code.py'). The checkout path
        must match. If it's changed to e.g. '.agents', the subprocess call breaks.
        """
        steps = load_workflow("sdlc.yml")["jobs"]["ai-task"]["steps"]
        checkout = next(s for s in steps if s.get("name") == "Checkout agent scripts")
        assert checkout["with"]["path"] == ".sdlc", (
            f"Agent scripts checkout path is '{checkout['with']['path']}' but "
            f"test_loop.py calls '.sdlc/agents/code.py'. These must match."
        )


# ── Subprocess Paths ──────────────────────────────────────────────────────────

@SKIP_NO_YAML
class TestSubprocessPaths:

    def test_test_loop_calls_sdlc_code_path(self):
        """
        test_loop.py calls code.py via subprocess. The path '.sdlc/agents/code.py'
        must match the checkout path in the workflow ('path: .sdlc').
        """
        source = read_agent("test_loop.py")
        assert ".sdlc/agents/code.py" in source, (
            "test_loop.py must reference '.sdlc/agents/code.py' in its subprocess call.\n"
            "The workflow checks out homelab-sdlc to '.sdlc' (Checkout agent scripts step).\n"
            f"Current source has: {[line.strip() for line in source.splitlines() if 'code.py' in line]}"
        )


# ── Agent Logic ───────────────────────────────────────────────────────────────

class TestAgentLogic:

    def test_lifeline_hard_requires_anthropic_key(self):
        """
        lifeline.py must hard-require ANTHROPIC_API_KEY so we can detect the
        misconfiguration (ai-task.yml not forwarding it) at test time, not
        when a live test loop fails at 2am.
        """
        tree = parse_agent("lifeline.py")
        required, optional = extract_env_reads(tree)
        assert "ANTHROPIC_API_KEY" in required, (
            "lifeline.py should use os.environ['ANTHROPIC_API_KEY'] (hard required). "
            "This makes the test_ai_task_forwards_anthropic_key test catch the misconfiguration."
        )

    def test_review_soft_requires_anthropic_key(self):
        """
        review.py must gracefully handle a missing key (default to approved).
        It should use os.environ.get() and check for empty string before calling the API.
        """
        tree = parse_agent("review.py")
        required, _ = extract_env_reads(tree)
        assert "ANTHROPIC_API_KEY" not in required, (
            "review.py uses os.environ['ANTHROPIC_API_KEY'] as hard required. "
            "Change to os.environ.get() with graceful fallback so the review step "
            "doesn't crash when the key is absent."
        )

    def test_test_loop_exits_nonzero_on_exhaustion(self):
        """test_loop.py must exit 1 when all attempts fail so continue-on-error captures it."""
        source = read_agent("test_loop.py")
        assert "sys.exit(0 if tests_passed else 1)" in source or (
            "sys.exit(1)" in source
        ), (
            "test_loop.py must call sys.exit(1) on failure. "
            "The workflow reads the exit code via continue-on-error to determine "
            "whether to run lifeline and whether to mark tests as failed."
        )

    def test_code_strips_markdown_fences(self):
        """
        LLMs sometimes wrap code in ``` fences despite instructions.
        code.py must strip them before committing, or the committed file starts with ```.
        """
        source = read_agent("code.py")
        assert 'startswith("```")' in source or "```" in source, (
            "code.py must handle and strip markdown code fences from LLM output "
            "before writing to the repo."
        )

    def test_pr_opens_as_draft_on_failure(self):
        """PR must be a draft when tests failed or review rejected so it isn't merged prematurely."""
        source = read_agent("pr.py")
        assert "draft" in source.lower(), (
            "pr.py must set draft=True when tests fail or review rejects. "
            "Without this, a broken implementation gets a merge-ready PR."
        )

    def test_plan_validates_output_schema(self):
        """plan.py must validate the LLM returned a usable plan before posting to GitHub."""
        source = read_agent("plan.py")
        assert "required_keys" in source or ('"summary"' in source and "sys.exit" in source), (
            "plan.py should validate the plan schema and exit if required keys are missing."
        )

    def test_plan_falls_back_to_claude_on_litellm_failure(self):
        """
        plan.py must have a Claude fallback for when LiteLLM is unreachable.
        Without it, any LiteLLM outage kills the entire pipeline at the first step.
        """
        source = read_agent("plan.py")
        assert "ANTHROPIC_API_KEY" in source and "anthropic" in source.lower(), (
            "plan.py should fall back to Claude (Anthropic) when LiteLLM fails."
        )


# ── Sandbox ───────────────────────────────────────────────────────────────────

class TestSandbox:
    """
    The sandbox is the E2E test target that agents modify during pipeline runs.
    These tests verify the sandbox is in a known-good state before a pipeline run.
    """

    def test_pyproject_toml_exists(self):
        assert os.path.exists(os.path.join(SANDBOX_DIR, "pyproject.toml")), (
            "sandbox/pyproject.toml is missing. pytest won't know where to find tests."
        )

    def test_pyproject_configures_pytest(self):
        pyproject = os.path.join(SANDBOX_DIR, "pyproject.toml")
        with open(pyproject) as f:
            content = f.read()
        assert "pytest" in content, (
            "sandbox/pyproject.toml must configure pytest (tool.pytest.ini_options)."
        )

    def test_main_py_importable(self):
        result = subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, '.'); import main"],
            cwd=SANDBOX_DIR,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"sandbox/main.py is not importable:\n{result.stderr}"
        )

    def test_baseline_tests_pass(self):
        """
        Sandbox baseline tests must be green before a pipeline run.
        A red baseline means the agents will waste all retry attempts on a
        pre-existing failure rather than the new feature they're implementing.
        """
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short", "-q"],
            cwd=SANDBOX_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            pytest.fail(
                f"Sandbox baseline tests are failing:\n"
                f"{result.stdout}\n{result.stderr}\n\n"
                f"Fix these before running the SDLC pipeline — agents will spend all "
                f"retry attempts on this pre-existing failure."
            )
