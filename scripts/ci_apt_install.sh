#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Install Debian packages on a CI runner without letting a stalled mirror hang the job.
#
# Usage: scripts/ci_apt_install.sh PACKAGE...
#
# A plain apt-get install has sat silent for 20 minutes on GitHub's runners until the
# job was cancelled. Each attempt here is capped at 5 minutes, with network retries
# inside, and the whole install is tried 3 times. `update` is not quieted, so the log
# shows which source stalled.
set -eu

opts="-o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30"
for attempt in 1 2 3; do
  # shellcheck disable=SC2086  # $opts is a list of options
  if sudo timeout 300 apt-get $opts update &&
     sudo timeout 300 apt-get $opts install -y -qq --no-install-recommends "$@"; then
    exit 0
  fi
  echo "apt attempt $attempt failed" >&2
  sleep 15
done
exit 1
