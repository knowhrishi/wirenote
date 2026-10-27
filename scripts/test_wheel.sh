#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Run the test suite against a built wheel instead of the source tree.
#
# Usage: scripts/test_wheel.sh DIST_DIR [PYTHON_VERSION] [pytest args...]
#
# The wheel goes into a fresh virtual environment with dependencies resolved
# today, as `uv tool install wirenote` would; the source tree is never on the
# import path. This catches what tests of the checkout cannot: a file missing from
# the wheel, a dependency not declared, a version bound that no longer resolves.
set -eu

dist=$1
shift
python=3.13
if [ $# -gt 0 ]; then python=$1; shift; fi

env_dir=$(mktemp -d)/env
uv venv --quiet --python "$python" "$env_dir"
uv pip install --quiet --python "$env_dir/bin/python" "$dist"/wirenote-*.whl pytest quickjs
"$env_dir/bin/python" -c '
import wirenote, sys
assert "site-packages" in wirenote.__file__, f"testing {wirenote.__file__}, not the wheel"
print(f"testing wirenote {wirenote.__version__} from {wirenote.__file__} on Python {sys.version.split()[0]}")
'
exec "$env_dir/bin/python" -m pytest -q -p no:cacheprovider "$@" tests
