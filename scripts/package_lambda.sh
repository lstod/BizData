#!/usr/bin/env bash
# Build the Lambda deployment zip.
#
#   scripts/package_lambda.sh
#
# Writes build/bizdata-server.zip, which infra/main/lambda.tf uploads. Terraform hashes
# the file, so the build is deterministic on purpose: every file is stamped to a fixed
# mtime and added in sorted order, and rebuilding without changing a source file produces
# a byte-identical zip and therefore no redeploy. The alternative is an apply that always
# claims the function changed, which trains you to stop reading plans.
#
# Dependencies are installed for the target platform rather than this laptop's. That
# matters for the three packages here with compiled extensions — pydantic-core, rpds-py
# and cryptography — which would otherwise be built as macOS arm64 wheels and fail on
# Lambda at import.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${REPO_ROOT}/build"
STAGE_DIR="${BUILD_DIR}/package"
ZIP_PATH="${BUILD_DIR}/bizdata-server.zip"

# Any fixed timestamp works; this one is step 5's date. It must not be before 1980, which
# is the earliest a zip entry can express.
STAMP="202608200000.00"

PYTHON_VERSION="3.12"

# The repository's virtualenv if there is one, so the build does not depend on whatever
# `pip` happens to mean on the PATH. --platform makes the installing interpreter almost
# irrelevant, but "almost" is doing work there: pip's own version decides which wheel tags
# it understands.
if [[ -x "${REPO_ROOT}/.venv/bin/pip" ]]; then
    PIP=("${REPO_ROOT}/.venv/bin/pip")
else
    PIP=(python3 -m pip)
fi

rm -rf "${STAGE_DIR}" "${ZIP_PATH}"
mkdir -p "${STAGE_DIR}"

echo "installing dependencies for linux/arm64, python ${PYTHON_VERSION}"
# Repeated --platform flags are load-bearing. Lambda's Amazon Linux 2023 runs glibc 2.34,
# and these projects publish wheels under whichever manylinux tag was current when they
# released, so asking for manylinux2014 alone finds no candidate for cryptography and the
# install fails with "none of the wheels are compatible".
"${PIP[@]}" install \
    --requirement "${REPO_ROOT}/requirements-lambda.txt" \
    --target "${STAGE_DIR}" \
    --platform manylinux2014_aarch64 \
    --platform manylinux_2_28_aarch64 \
    --platform manylinux_2_34_aarch64 \
    --implementation cp \
    --python-version "${PYTHON_VERSION}" \
    --only-binary=:all: \
    --upgrade \
    --quiet

echo "copying application code"
cp -R "${REPO_ROOT}/server" "${STAGE_DIR}/server"
# db/sql/ ships because server/db.py reads every query out of it at call time. db/views/
# and db/checks/ do not: they are applied by the seed script from the laptop, never read
# by the running server.
mkdir -p "${STAGE_DIR}/db"
cp -R "${REPO_ROOT}/db/sql" "${STAGE_DIR}/db/sql"
cp "${REPO_ROOT}/run.sh" "${STAGE_DIR}/run.sh"
chmod 755 "${STAGE_DIR}/run.sh"

# The commit this package was built from, baked in so the deployed server can say what code
# it is running. server/build_info.py reads it and puts it on MCPServer(version=...), which
# comes back in serverInfo on every response; scripts/check_auth.py compares it against the
# working tree.
#
# This is step 6's third finding closed. A Skill went out against a Lambda four steps behind
# it, every harness passed because every harness ran against the checkout, and two Cowork
# runs produced wrong deliverables before anybody looked at the function's modified date.
#
# It costs the zip some of its determinism: the same source tree at two different commits now
# produces two different archives, so a docs-only commit forces a redeploy. Accepted, because
# a stamp that only moves when shipped files move cannot detect the thing it is for. The zip
# is still byte-identical for a given commit, which is what makes a rebuild-and-apply a no-op.
COMMIT="$(git -C "${REPO_ROOT}" describe --always --dirty --abbrev=12 2>/dev/null || echo unknown)"
printf '{"commit": "%s"}\n' "${COMMIT}" > "${STAGE_DIR}/server/_build_stamp.json"
echo "stamped ${COMMIT}"
case "${COMMIT}" in
    *-dirty)
        echo "  warning: built from a tree with uncommitted changes" >&2
        ;;
esac

# Bytecode only. It is machine- and timestamp-specific, unnecessary at runtime, and would
# defeat the deterministic zip below.
#
# The .dist-info directories stay, and that is not an oversight — an earlier version of
# this script deleted them to save space and the deployed function died on import:
#
#   File "/var/task/httpx2/__version__.py", line 5, in <module>
#     __version__ = version("httpx2")
#   importlib.metadata.PackageNotFoundError: No package metadata was found for httpx2
#
# Packages that report their own version through importlib.metadata read it out of
# .dist-info at import time, so deleting it turns a dependency into a crash that only
# happens once deployed. Their contents are fixed for a pinned version, so keeping them
# costs nothing in reproducibility.
find "${STAGE_DIR}" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "${STAGE_DIR}" -type f -name '*.pyc' -delete

echo "building ${ZIP_PATH##"${REPO_ROOT}/"}"
find "${STAGE_DIR}" -exec touch -t "${STAMP}" {} +
cd "${STAGE_DIR}"
# -X drops platform extra fields, -D drops directory entries, and feeding sorted names on
# stdin fixes the order. Together those are what make the archive reproducible.
find . -type f -o -type l | sed 's|^\./||' | LC_ALL=C sort | zip -X -D -q "${ZIP_PATH}" -@

SIZE="$(du -h "${ZIP_PATH}" | cut -f1)"
COUNT="$(unzip -l "${ZIP_PATH}" | tail -1 | awk '{print $2}')"
echo "packaged ${COUNT} files, ${SIZE}"
