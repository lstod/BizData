#!/usr/bin/env bash
# Zip one Skill directory for upload to Cowork, without the macOS cruft.
#
# Committed rather than retyped because step 6 shipped a Skill as a bare SKILL.md and its
# bundled script never reached the sandbox. The model wrote its own builder and did not fail,
# so the omission only showed up on inspection of the workbook. See
# docs/notes/step-7-packaging.md.
#
#     scripts/package_skill.sh assemble-delivery-pack
#     scripts/package_skill.sh house-format
#
# The archive contains the skill directory itself, so it unpacks to
# <name>/SKILL.md alongside <name>/scripts/ and <name>/assets/.

set -euo pipefail

name="${1:-}"
if [[ -z "$name" ]]; then
    echo "usage: $(basename "$0") <skill-directory-name>" >&2
    echo "available:" >&2
    ls -1 "$(dirname "$0")/../plugin/skills" 2>/dev/null | grep -v '\.zip$' >&2 || true
    exit 2
fi

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
skills="$repo_root/plugin/skills"

if [[ ! -d "$skills/$name" ]]; then
    echo "error: no skill directory at plugin/skills/$name" >&2
    exit 1
fi

if [[ ! -f "$skills/$name/SKILL.md" ]]; then
    echo "error: plugin/skills/$name has no SKILL.md" >&2
    exit 1
fi

archive="$skills/$name.zip"
rm -f "$archive"

# -x excludes rather than a copy-and-prune, so the archive is built from the working tree and
# cannot drift from it. COPYFILE_DISABLE stops macOS writing ._ resource forks into the zip.
cd "$skills"
COPYFILE_DISABLE=1 zip -r -q "$archive" "$name" \
    -x "*/.DS_Store" \
    -x "__MACOSX/*" \
    -x "*/__pycache__/*" \
    -x "*.pyc"

echo "$archive"
unzip -l "$archive" | sed -n '4,$p' | grep -v '^-' | grep -v 'files$' || true
