"""Unsuspend a Seam sandbox workspace via a Personal Access Token (PAT).

Seam auto-suspends sandbox workspaces after 14 days of inactivity. Once
suspended, the workspace's own API key is rejected with 401 on EVERY call
("Sandbox workspace suspended due to inactivity."), so the key cannot
unsuspend itself. The only way back in is a user-scoped Personal Access Token,
which authenticates as you (the console user) rather than as the workspace.

This is expected to recur (every 14 idle days), hence a reusable helper.

Usage
-----
1. Create a PAT: Seam Console (app.seam.co) -> Developer (top nav) ->
   Personal Access Tokens (left nav) -> Add Personal Access Token. Copy it.
2. Run WITHOUT pasting the PAT into any durable transcript:

       SEAM_PAT=<your-pat> .venv/bin/python scripts/manual/unsuspend_seam_sandbox.py

   (In a Claude Code session you can type it as a `! ...` command so the PAT
   stays in your shell, not the chat.)

The script lists your workspaces, finds the suspended sandbox(es), flips
is_suspended -> false via POST /workspaces/update (PAT auth + seam-workspace
header), and verifies. It never prints the PAT.

Docs: https://docs.seam.co/api/workspaces/update
"""
from __future__ import annotations

import os
import sys

import httpx

BASE = "https://connect.getseam.com"


def main() -> int:
    pat = os.environ.get("SEAM_PAT", "").strip()
    if not pat:
        print("ERROR: set SEAM_PAT to a Seam Personal Access Token.")
        print("  Seam Console -> Developer -> Personal Access Tokens -> Add.")
        print("  Then: SEAM_PAT=<pat> .venv/bin/python scripts/manual/unsuspend_seam_sandbox.py")
        return 2

    auth = {"Authorization": f"Bearer {pat}"}

    # 1. List all workspaces the PAT can see (PAT is multi-workspace, so this
    #    works even while the sandbox itself is suspended).
    r = httpx.post(f"{BASE}/workspaces/list", headers=auth, json={}, timeout=20)
    if r.status_code != 200:
        print(f"FAIL: /workspaces/list -> {r.status_code}: {r.text[:300]}")
        print("Is the SEAM_PAT valid? (It should authenticate as your console user.)")
        return 1

    workspaces = r.json().get("workspaces", [])
    print(f"Found {len(workspaces)} workspace(s):")
    suspended_sandboxes = []
    for w in workspaces:
        wid = w.get("workspace_id")
        name = w.get("name")
        is_sandbox = w.get("is_sandbox")
        is_suspended = w.get("is_suspended")
        print(f"  - {name!r}  id={wid}  sandbox={is_sandbox}  suspended={is_suspended}")
        if is_suspended:
            suspended_sandboxes.append((wid, name))

    if not suspended_sandboxes:
        print("\nNothing to do — no suspended workspaces.")
        return 0

    # 2. Unsuspend each suspended workspace. PAT auth requires the target
    #    workspace be named via the seam-workspace header (pat_with_workspace).
    exit_code = 0
    for wid, name in suspended_sandboxes:
        headers = {**auth, "seam-workspace": wid}
        upd = httpx.post(
            f"{BASE}/workspaces/update",
            headers=headers,
            json={"is_suspended": False},
            timeout=20,
        )
        if upd.status_code != 200:
            print(f"\nFAIL: unsuspend {name!r} ({wid}) -> {upd.status_code}: {upd.text[:300]}")
            exit_code = 1
            continue

        # 3. Verify.
        chk = httpx.post(f"{BASE}/workspaces/get", headers=headers, json={}, timeout=20)
        still = (
            chk.json().get("workspace", {}).get("is_suspended")
            if chk.status_code == 200
            else "?"
        )
        print(
            f"\nUnsuspended {name!r} ({wid}). is_suspended now = {still} "
            f"(get status {chk.status_code})."
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
