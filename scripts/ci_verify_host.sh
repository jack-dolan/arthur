#!/usr/bin/env bash
set -euo pipefail

# Post-deploy verification, run on the host by the deploy pipeline
# (CI/CD Step 9, 2026-07-29).
#
#   usage: ci_verify.sh <expected-image-ref> [previous-container-id]
#
# It answers one question honestly: did a NEW container, running the image we
# just deployed, actually come up and pass its own schema guard?
#
# WHY THE PREVIOUS CONTAINER ID IS AN ARGUMENT. The first version of this check
# compared only the running image against the expected image, and it passed
# instantly on a re-deploy of the SAME tag -- because the container already
# running was, trivially, running that image. It reported success before the
# deploy had even restarted anything. Comparing the container ID is what makes
# "it restarted" distinguishable from "it was already like that". Caught on
# 2026-07-30 by a manual re-run of the pipeline.
#
# WHY THE GUARD CHECK RETRIES. The same first version grepped the logs once,
# immediately, and failed the whole pipeline when a healthy container simply had
# not written the line yet. A false red is worse than no check: it teaches the
# operator to ignore the pipeline. Everything here is polled, and the only
# failure is a genuine timeout.

EXPECT="${1:?expected image reference required}"
PREV="${2:-}"
ATTEMPTS="${VERIFY_ATTEMPTS:-40}"
INTERVAL="${VERIFY_INTERVAL:-10}"

# WHY NOTHING HERE PIPES A COMMAND INTO grep. `grep -q` and `grep -m1` both
# stop reading at the first match. Pipe a still-running command into either and
# the writer is killed by SIGPIPE, which under `set -o pipefail` makes the
# pipeline exit 141. That produced a FALSE RED on a perfectly healthy deploy on
# 2026-08-03 (run 30815410291), and it has two faces, both seen:
#
#   * outside a condition, `set -e` aborts the script on the 141 -- the deploy
#     is reported as failed after the check has already succeeded;
#   * inside `if ...; then`, `set -e` does not apply, so the 141 simply reads
#     as "no match" and the loop times out blaming a missing schema-guard line
#     that was in fact present.
#
# It fired only when the container had written more than a pipe buffer (~64 KiB)
# after its schema-guard line, which is why it passed by luck for weeks: the
# guard line is one of the FIRST lines the app writes, so catching a container
# early is exactly when there is most still to come.
#
# The fix is to read each stream ONCE into a variable and match against that.
# A here-string cannot break this way -- grep consumes all of its input and no
# writer is left holding a closed pipe. Regression cover:
# tests/unit/test_ci_verify_host.py.
for i in $(seq 1 "$ATTEMPTS"); do
  ps_out="$(docker ps --format '{{.ID}} {{.Image}}' || true)"
  matches="$(grep 'home-rental-automation' <<<"$ps_out" || true)"
  line="${matches%%$'\n'*}"
  cid="${line%% *}"
  img="${line#* }"

  if [ -n "$cid" ] && [ "$img" = "$EXPECT" ] && [ "$cid" != "$PREV" ]; then
    logs="$(docker logs "$cid" 2>&1 || true)"
    guard="$(grep 'SCHEMA GUARD' <<<"$logs" || true)"

    if [ -n "$guard" ]; then
      echo "verify: new container $cid running $img"
      printf '%s\n' "${guard%%$'\n'*}"
      # Not a gate -- the sandbox/production banner is printed so a wrong-target
      # deploy is visible in the run log rather than discovered later.
      target="$(grep 'DocuSign target' <<<"$logs" || true)"
      if [ -n "$target" ]; then
        printf '%s\n' "${target%%$'\n'*}"
      fi
      echo "verify: ok"
      exit 0
    fi
  fi

  echo "verify: attempt $i/$ATTEMPTS container=${cid:-none} image=${img:-none}"
  sleep "$INTERVAL"
done

echo "verify: did not converge — wanted a NEW container on $EXPECT with a schema-guard line" >&2
exit 1
