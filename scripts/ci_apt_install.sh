#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Install Debian packages on a CI runner without letting a stalled mirror hang the job.
#
# Usage: scripts/ci_apt_install.sh PACKAGE...
#
# A plain apt-get install has sat silent for 20 minutes on GitHub's runners until the
# job was cancelled. Each attempt here is capped at 5 minutes, with network retries
# inside, and the whole install is tried 3 times. Neither step is quieted, so the log
# shows which source or package stalled, and an attempt that times out prints who
# holds the apt and dpkg locks.
#
# A mirror that trickles bytes never trips apt's network timeout: Azure's mirror once
# served ffmpeg's packages at about 40 kB/s, too slow for three attempts to finish.
# apt only moves to the next mirror in the runner's list on an error, so after a
# timeout the later attempts drop the first one. Packages already downloaded are kept.
# Only one is dropped: if the next mirror is slow too, the runner's network is.
set -eu

mirrors=${CI_APT_MIRRORS:-/etc/apt/apt-mirrors.txt}
mirror_skipped=

# Rebuilding the man page index after an install can take minutes on the runners, and
# CI never reads a man page.
sudo rm -f /var/lib/man-db/auto-update

opts="-o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30"

# A timed-out apt-get prints nothing about why; the lock holders and any leftover
# apt or dpkg run usually say what it was waiting for.
show_lock_holders() {
  echo "apt-get $1 timed out after 300 s; apt and dpkg lock holders:" >&2
  sudo fuser -v /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock \
    /var/lib/apt/lists/lock /var/cache/apt/archives/lock >&2 || true
  # shellcheck disable=SC2009  # pgrep cannot show how long each one has been running
  ps -eo pid,etime,args | grep -wE '[a]pt|[d]pkg|[u]nattended-upgrades?' >&2 || true
}

skip_first_mirror() {
  if [ -z "$mirror_skipped" ] && [ -f "$mirrors" ] && [ "$(grep -c . "$mirrors")" -gt 1 ]; then
    echo "next attempts skip the mirror $(head -n 1 "$mirrors" | cut -f 1)" >&2
    rest=$(tail -n +2 "$mirrors")
    printf '%s\n' "$rest" | sudo tee "$mirrors" > /dev/null
    mirror_skipped=1
  fi
}

apt_get() {
  # shellcheck disable=SC2086  # $opts is a list of options
  sudo timeout 300 apt-get $opts "$@" && return 0
  rc=$?
  if [ "$rc" -eq 124 ]; then
    show_lock_holders "$1"
    skip_first_mirror
  fi
  return "$rc"
}

for attempt in 1 2 3; do
  if apt_get update && apt_get install -y --no-install-recommends "$@"; then
    exit 0
  fi
  echo "apt attempt $attempt failed" >&2
  sleep 15
done
exit 1
