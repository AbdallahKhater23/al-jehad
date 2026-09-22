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

STATE_ROOT="${STATE_ROOT:-/data}"
RUN_AS_USER="${RUN_AS_USER:-appuser}"

mkdir -p "$STATE_ROOT"
chown -R "$RUN_AS_USER:$RUN_AS_USER" "$STATE_ROOT"

# re-exec as the unprivileged user; exec so PID 1 stays the server and receives
# the platform's signals (TERM -> graceful shutdown) instead of a wrapper that
# would swallow them.
exec setpriv --reuid="$RUN_AS_USER" --regid="$RUN_AS_USER" --clear-groups \
    python backend/serve.py --tunnel "$@"
