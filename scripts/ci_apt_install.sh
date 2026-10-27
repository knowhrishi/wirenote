#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Install Debian packages on a CI runner without letting a stalled mirror hang the job.
#
# Usage: [CI_APT_CACHE=DIR] scripts/ci_apt_install.sh PACKAGE...
#
# With CI_APT_CACHE, the packages come from that folder when it holds them, which takes
# seconds and needs no mirror; .github/actions/apt-install keeps it in the Actions cache.
# A stalled mirror then delays only the run that fills the cache. When the folder is
# empty, apt downloads into it.
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
cache=${CI_APT_CACHE:-}
fix=

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

installed() {
  for p in "$@"; do
    dpkg-query -W -f '${Status}' "$p" 2>/dev/null | grep -q 'ok installed' || return 1
  done
}

if [ -n "$cache" ] && ls "$cache"/*.deb > /dev/null 2>&1; then
  # -G keeps a newer version the runner image already has.
  if sudo timeout 300 dpkg -i -G "$cache"/*.deb && installed "$@"; then
    exit 0
  fi
  # Cached packages that no longer fit the image are left half installed; apt finishes
  # them. The folder is not refilled: a cache that was found is never saved again.
  echo "the cached packages did not install; downloading them instead" >&2
  fix=--fix-broken
elif [ -n "$cache" ]; then
  mkdir -p "$cache/partial"
  # _apt cannot write to the runner's folders, so root downloads.
  opts="$opts -o Dir::Cache::archives=$cache/ -o APT::Sandbox::User=root"
fi

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
  # shellcheck disable=SC2086  # $fix is empty or one option
  if apt_get update && apt_get install -y --no-install-recommends $fix "$@"; then
    if [ -n "$cache" ] && [ -z "$fix" ]; then
      # apt's lock and partial folder stay root's; the cache action could not read them.
      sudo rm -rf "$cache/partial" "$cache/lock"
      sudo chown -R "$(id -u):$(id -g)" "$cache"
    fi
    exit 0
  fi
  echo "apt attempt $attempt failed" >&2
  sleep 15
done
exit 1
