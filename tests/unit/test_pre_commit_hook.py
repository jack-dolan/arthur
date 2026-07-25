"""Self-tests for the pre-commit privacy guard (``scripts/hooks/pre-commit``).

The hook blocks a commit whose **staged added lines** carry guest PII: a
literal from the local tier-1 name denylist, or a real-looking phone number.
It also refuses to commit private-by-design paths outright.

These tests build a throwaway git repo in ``tmp_path`` and run the real hook
against it.

**Every value planted here is SYNTHETIC**, and the real-looking ones are
assembled from fragments at runtime, so that no real-looking phone number
appears as a literal in this file. This file ships with the test suite; a
pasted literal here would be a privacy violation in its own right.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "scripts" / "hooks" / "pre-commit"
INSTALLER = REPO_ROOT / "scripts" / "install_hooks.sh"

DENYLIST_REL = ".secrets/denylist-guest.txt"

# --- synthetic tier-1 terms (never real values) ----------------------------
FAKE_GUEST_NAME = "Quorbin Fakeguest"
FAKE_GUEST_CODE = "HMZZTESTZZ"

# Real-LOOKING phone fragments, joined at runtime (see the module docstring).
_A, _B, _C = "610", "224", "8899"
REAL_LOOKING_PHONE = f"({_A}) {_B}-{_C}"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed:\n{proc.stderr}"
    return proc


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo with the hook installed and one initial commit."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    git(root, "config", "user.name", "Test Author")
    git(root, "config", "user.email", "author@example.com")
    git(root, "config", "commit.gpgsign", "false")

    hooks_dir = root / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HOOK, hooks_dir / "pre-commit")
    (hooks_dir / "pre-commit").chmod(0o755)

    (root / "README.md").write_text("initial\n")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "initial")
    return root


def write_denylist(repo: Path, terms: list[str]) -> None:
    target = repo / DENYLIST_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    body = "# synthetic denylist for tests\n\n" + "\n".join(terms) + "\n"
    target.write_text(body)


def stage(repo: Path, rel: str, content: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(repo, "add", "--force", rel)


def run_hook(repo: Path) -> subprocess.CompletedProcess:
    """Invoke the installed hook exactly as git would."""
    env = dict(os.environ)
    env.pop("GIT_DIR", None)
    env.pop("GIT_INDEX_FILE", None)
    return subprocess.run(
        [str(repo / ".git" / "hooks" / "pre-commit")],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


def output(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# the clean path
# ---------------------------------------------------------------------------


def test_clean_staged_change_passes(repo: Path):
    write_denylist(repo, [FAKE_GUEST_NAME, FAKE_GUEST_CODE])
    stage(repo, "docs/notes.md", "The guest checks in at 4pm. Phone +1 (555) 010-1234.\n")
    proc = run_hook(repo)
    assert proc.returncode == 0, output(proc)


def test_nothing_staged_passes(repo: Path):
    write_denylist(repo, [FAKE_GUEST_NAME])
    proc = run_hook(repo)
    assert proc.returncode == 0, output(proc)


# ---------------------------------------------------------------------------
# (a) tier-1 denylist literals
# ---------------------------------------------------------------------------


def test_denylist_term_in_added_line_blocks(repo: Path):
    write_denylist(repo, [FAKE_GUEST_NAME, FAKE_GUEST_CODE])
    stage(repo, "docs/notes.md", f"Booking for {FAKE_GUEST_NAME} arrives Friday.\n")
    proc = run_hook(repo)
    assert proc.returncode == 1, output(proc)
    assert "docs/notes.md:1" in output(proc)
    assert FAKE_GUEST_NAME.lower() in output(proc).lower()


def test_denylist_match_is_case_insensitive(repo: Path):
    write_denylist(repo, [FAKE_GUEST_NAME])
    stage(repo, "docs/notes.md", f"guest: {FAKE_GUEST_NAME.upper()}\n")
    assert run_hook(repo).returncode == 1


def test_denylist_comments_and_blanks_are_not_terms(repo: Path):
    """A blank line as a term would match every line and block every commit."""
    (repo / ".secrets").mkdir(parents=True, exist_ok=True)
    (repo / DENYLIST_REL).write_text("# a comment\n\n   \n" + FAKE_GUEST_NAME + "\n")
    stage(repo, "docs/notes.md", "an entirely innocuous line\n")
    proc = run_hook(repo)
    assert proc.returncode == 0, output(proc)


def test_unchanged_lines_are_not_scanned(repo: Path):
    """The historical Session Log must not be re-flagged on every commit.

    A file already in history may legitimately contain tier-1 terms. Only
    lines this commit ADDS are in scope.
    """
    write_denylist(repo, [FAKE_GUEST_NAME])
    stage(repo, "log.md", f"line one about {FAKE_GUEST_NAME}\n")
    git(repo, "commit", "-q", "--no-verify", "-m", "historical content")

    (repo / "log.md").write_text(
        f"line one about {FAKE_GUEST_NAME}\nline two, entirely innocuous\n"
    )
    git(repo, "add", "log.md")
    proc = run_hook(repo)
    assert proc.returncode == 0, output(proc)


def test_removed_lines_are_not_scanned(repo: Path):
    """Deleting a line that contains a term is a cleanup, not a violation."""
    write_denylist(repo, [FAKE_GUEST_NAME])
    stage(repo, "log.md", f"keep me\nremove {FAKE_GUEST_NAME}\n")
    git(repo, "commit", "-q", "--no-verify", "-m", "historical content")

    (repo / "log.md").write_text("keep me\n")
    git(repo, "add", "log.md")
    proc = run_hook(repo)
    assert proc.returncode == 0, output(proc)


def test_unstaged_working_tree_content_is_not_scanned(repo: Path):
    write_denylist(repo, [FAKE_GUEST_NAME])
    stage(repo, "docs/notes.md", "clean staged line\n")
    (repo / "scratch.md").write_text(f"unstaged mention of {FAKE_GUEST_NAME}\n")
    proc = run_hook(repo)
    assert proc.returncode == 0, output(proc)


# ---------------------------------------------------------------------------
# (b) phone sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "planted",
    [
        f"Call {_A}-{_B}-{_C} before check-in.",
        f"phone=+1 ({_A}) {_B}-{_C}",
        f"phone: +1{_A}{_B}{_C}",
        f"traveler phone {_A}{_B}{_C} recorded",
    ],
)
def test_real_looking_phone_blocks(repo: Path, planted: str):
    write_denylist(repo, [FAKE_GUEST_NAME])
    stage(repo, "docs/notes.md", planted + "\n")
    proc = run_hook(repo)
    assert proc.returncode == 1, output(proc)
    assert "PHONE" in output(proc)


@pytest.mark.parametrize(
    "benign",
    [
        "Placeholder +1 (555) 010-1234 stays.",
        "sha 9f8e7d6c5b4a39281706 is a hash",
        "listing id 401992336118420773 style long run",
        "amount 1234567890123 in cents",
    ],
)
def test_benign_digit_runs_pass(repo: Path, benign: str):
    write_denylist(repo, [FAKE_GUEST_NAME])
    stage(repo, "docs/notes.md", benign + "\n")
    proc = run_hook(repo)
    assert proc.returncode == 0, output(proc)


# ---------------------------------------------------------------------------
# (c) private-by-design paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        ".secrets/denylist-guest.txt",
        "example-emails/booking.eml",
        "tests/fixtures/emails/booking.eml",
    ],
)
def test_private_paths_are_refused(repo: Path, rel: str):
    write_denylist(repo, [FAKE_GUEST_NAME])
    stage(repo, rel, "innocuous content\n")
    proc = run_hook(repo)
    assert proc.returncode == 1, output(proc)
    assert "PATH" in output(proc)
    assert rel in output(proc)


# ---------------------------------------------------------------------------
# missing denylist — warn, do not block
# ---------------------------------------------------------------------------


def test_missing_denylist_warns_but_allows(repo: Path):
    stage(repo, "docs/notes.md", "an entirely innocuous line\n")
    proc = run_hook(repo)
    assert proc.returncode == 0, output(proc)
    assert DENYLIST_REL in output(proc)


def test_missing_denylist_notice_says_how_to_restore_it(repo: Path):
    stage(repo, "docs/notes.md", "an entirely innocuous line\n")
    text = output(run_hook(repo))
    assert "one literal term per line" in text
    assert "Privacy rules" in text


def test_pattern_checks_still_run_without_a_denylist(repo: Path):
    stage(repo, "docs/notes.md", f"call {_A}-{_B}-{_C}\n")
    proc = run_hook(repo)
    assert proc.returncode == 1, output(proc)
    assert "PHONE" in output(proc)


# ---------------------------------------------------------------------------
# the block message
# ---------------------------------------------------------------------------


def test_block_message_prints_the_override_instruction(repo: Path):
    write_denylist(repo, [FAKE_GUEST_NAME])
    stage(repo, "docs/notes.md", f"guest {FAKE_GUEST_NAME}\n")
    text = output(run_hook(repo))
    assert "--no-verify" in text


def test_block_message_prints_the_offending_line(repo: Path):
    write_denylist(repo, [FAKE_GUEST_NAME])
    stage(repo, "docs/notes.md", f"booking held for {FAKE_GUEST_NAME} in July\n")
    text = output(run_hook(repo))
    assert "booking held for" in text


# ---------------------------------------------------------------------------
# robustness
# ---------------------------------------------------------------------------


def test_binary_staged_file_does_not_crash(repo: Path):
    target = repo / "assets" / "blob.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bytes(range(256)) * 4)
    git(repo, "add", "assets/blob.bin")
    write_denylist(repo, [FAKE_GUEST_NAME])
    proc = run_hook(repo)
    assert proc.returncode == 0, output(proc)


def test_path_with_spaces_is_scanned(repo: Path):
    write_denylist(repo, [FAKE_GUEST_NAME])
    stage(repo, "docs/a note with spaces.md", f"guest {FAKE_GUEST_NAME}\n")
    proc = run_hook(repo)
    assert proc.returncode == 1, output(proc)


# ---------------------------------------------------------------------------
# end to end, through git itself
# ---------------------------------------------------------------------------


def test_git_commit_is_blocked_and_no_verify_passes(repo: Path):
    write_denylist(repo, [FAKE_GUEST_NAME, FAKE_GUEST_CODE])
    stage(repo, "docs/notes.md", f"{FAKE_GUEST_NAME} booked under {FAKE_GUEST_CODE}\n")

    blocked = subprocess.run(
        ["git", "commit", "-m", "should be blocked"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert blocked.returncode != 0, output(blocked)
    assert "--no-verify" in output(blocked)

    allowed = subprocess.run(
        ["git", "commit", "--no-verify", "-m", "deliberate exception"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert allowed.returncode == 0, output(allowed)


# ---------------------------------------------------------------------------
# the installer
# ---------------------------------------------------------------------------


def fresh_checkout(tmp_path: Path) -> Path:
    """A git repo carrying this repo's hook sources but none of its history.

    Stands in for a fresh clone: the point of the installer is that
    ``.git/hooks`` is not part of a repository and does not travel with one.
    """
    root = tmp_path / "checkout"
    (root / "scripts" / "hooks").mkdir(parents=True)
    shutil.copy2(INSTALLER, root / "scripts" / "install_hooks.sh")
    for src in (REPO_ROOT / "scripts" / "hooks").iterdir():
        shutil.copy2(src, root / "scripts" / "hooks" / src.name)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    return root


def test_installer_installs_a_working_hook(tmp_path: Path):
    """The installer is the portable part: git hooks do not clone."""
    checkout = fresh_checkout(tmp_path)
    assert not (checkout / ".git" / "hooks" / "pre-commit").exists()

    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "install_hooks.sh")],
        cwd=checkout,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, output(proc)
    installed = checkout / ".git" / "hooks" / "pre-commit"
    assert installed.exists()
    assert os.access(installed, os.X_OK)


def test_installer_is_idempotent(tmp_path: Path):
    checkout = fresh_checkout(tmp_path)
    for _ in range(2):
        proc = subprocess.run(
            ["bash", str(checkout / "scripts" / "install_hooks.sh")],
            cwd=checkout,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, output(proc)
    assert (checkout / ".git" / "hooks" / "pre-commit").exists()


def test_installer_honors_core_hookspath(tmp_path: Path):
    """core.hooksPath overrides .git/hooks — install where git actually looks.

    Installing into .git/hooks while git reads elsewhere is a silent no-op,
    and a silently absent privacy guard is the failure that matters most.
    """
    checkout = fresh_checkout(tmp_path)
    subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"], cwd=checkout, check=True
    )
    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "install_hooks.sh")],
        cwd=checkout,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, output(proc)
    assert (checkout / ".githooks" / "pre-commit").exists()
    assert not (checkout / ".git" / "hooks" / "pre-commit").exists()


def test_installer_accepts_hookspath_pointing_at_the_default(tmp_path: Path):
    """A repo-local core.hooksPath set to .git/hooks is not a conflict."""
    checkout = fresh_checkout(tmp_path)
    default_dir = checkout / ".git" / "hooks"
    subprocess.run(
        ["git", "config", "core.hooksPath", str(default_dir)],
        cwd=checkout,
        check=True,
    )
    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "install_hooks.sh")],
        cwd=checkout,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, output(proc)
    assert (default_dir / "pre-commit").exists()
