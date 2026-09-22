#!/bin/sh
# Container entrypoint: fix the volume's ownership, then drop privileges.
#
# WHY THIS EXISTS
# ---------------
# The image runs the application as the non-root user ``appuser``, which is the right
# default - but a platform volume (Railway's bind mounts included) is mounted
# **root-owned**, and a filesystem mounted over /data erases whatever ownership the
# image gave it. The first boot therefore reported
#
#     biometric_dirs_writable: local_references: [Errno 13] Permission denied
#     biometric file adoption failed: /data/quick_link_photos
#
# and every punch would have failed to store its evidence. Taking the ownership back
# needs root, so this script runs AS root, chowns the state tree, and then re-execs the
# server as ``appuser`` - the application itself never holds privileges.
#
# The chown runs on EVERY boot (not only the first) for the same reason the mount
# erases the ownership every boot. It is bounded to /data, so it costs nothing on the
# layers the image owns, and it is idempotent: on an already-owned volume chown is a
# metadata no-op.

set -e

# The listen port is the image's own, not the platform's.
#
# WHY: Railway injects a generated PORT (this deploy got 8080) while its edge routes
# the public domain to the port the Dockerfile EXPOSEs (8000). The app faithfully
# listened on 8080, the edge knocked on 8000, and the result was a 502 with a perfectly
# healthy self-test - the exact failure mode the port can never settle from inside the
# app, because the app cannot know which door the edge chose. The container must be
# self-consistent, so the listen port is pinned to the EXPOSEd port. APP_PORT moves
# both together (and overrides any injected PORT) when a deployment needs another.
APP_PORT="${APP_PORT:-8000}"
export PORT="$APP_PORT"

STATE_ROOT="${STATE_ROOT:-/data}"
RUN_AS_USER="${RUN_AS_USER:-appuser}"

mkdir -p "$STATE_ROOT"
chown -R "$RUN_AS_USER:$RUN_AS_USER" "$STATE_ROOT"

# re-exec as the unprivileged user; exec so PID 1 stays the server and receives
# the platform's signals (TERM -> graceful shutdown) instead of a wrapper that
# would swallow them.
#
# With arguments, exec THOSE (they are the command the platform asked for - Railway
# appends its startCommand to the image's ENTRYPOINT); with none, run the server.
# Appending the default AFTER "$@" is what produced a 502 on one deploy: serve.py
# received the platform's whole command line again as positional arguments and
# exited on the unrecognized arguments, and the container crash-looped.
if [ "$#" -gt 0 ]; then
    exec setpriv --reuid="$RUN_AS_USER" --regid="$RUN_AS_USER" --clear-groups "$@"
fi
exec setpriv --reuid="$RUN_AS_USER" --regid="$RUN_AS_USER" --clear-groups \
    python backend/serve.py --tunnel --port "$APP_PORT"
