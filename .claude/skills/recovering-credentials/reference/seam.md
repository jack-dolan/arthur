# Seam credential recovery

## Contents
- The credential model
- Diagnosing a 401
- Rotating the key
- Old patterns (sandbox quirks)

## The credential model

**The API key IS the environment.** `SEAM_API_KEY` in `.env` selects the
workspace (production workspace = real lock; sandbox workspace = virtual
devices). There is no separate environment flag, and only one key slot —
sandbox and production cannot coexist. The paired device id lives in
`config.yaml` → `properties[n].seam_device_id` and must belong to the same
workspace as the key, or every call 404s on the device.

## Diagnosing a 401

1. Confirm which workspace the key belongs to (Seam console → workspace →
   API keys). A revoked/rotated key 401s on **every** call.
2. Verify read-only from the container:

```bash
docker compose exec app python -c "
from app.integrations.seam.client import get_seam_client
devs = get_seam_client().devices.list()
print([d.device_id for d in devs])"
```

The configured `seam_device_id` must appear in that list.

## Rotating the key

Create a new key in the **production** workspace (Seam console), then on the
VPS: `scripts/manual/set_env_secret.sh` for `SEAM_API_KEY`, force-recreate
the app container, re-run the read check above, and confirm the daily
`verify_access_codes` job passes on its next run (or run its logic manually
for the next check-in via the snippet in `triaging-stuck-tasks`).

If existing bookings have live codes, rotation does not affect them — codes
belong to the workspace, not the key.

## Old patterns (sandbox quirks)

<details>
<summary>Sandbox auto-suspension (bit the project twice pre-go-live)</summary>

Sandbox workspaces auto-suspend after ~14 idle days, and a suspended
workspace's own key is 401'd on every call — so it cannot unsuspend itself.
Reactivate via the console banner, or
`scripts/manual/unsuspend_seam_sandbox.py` (personal-access-token path).
Production workspaces do not auto-suspend. Also: Seam deletes access codes
asynchronously (~2–4s lag before they leave `access_codes.list`).
</details>
