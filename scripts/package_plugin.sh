#!/usr/bin/env bash
# Zip the whole plugin for upload to Cowork's Customize -> Plugins -> Add -> upload.
#
#     scripts/package_plugin.sh
#
# The marketplace at .claude-plugin/marketplace.json is the other install path and the
# better one — it updates when the repository does, and this repository is public. This
# archive exists for the case where a marketplace cannot be added: an air-gapped review, or
# somebody who wants the artifact without the repository.
#
# The archive contains the plugin directory itself, so it unpacks to
# bizdata-delivery-review/.claude-plugin/plugin.json alongside skills/ and .mcp.json.
# The directory in the repository is named plugin/, which says what it is in the tree but
# would be a poor name in somebody's downloads folder, so it is renamed on the way in.
#
# What this deliberately does not carry: any credential. plugin/.mcp.json holds the OAuth
# client id, which is a public identifier, and no client secret, because the manifest format
# has no field for one. See docs/notes/plugin-packaging.md. scripts/check_plugin.py asserts
# the absence rather than trusting this comment.

set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
plugin="$repo_root/plugin"
name="bizdata-delivery-review"

if [[ ! -f "$plugin/.claude-plugin/plugin.json" ]]; then
    echo "error: no manifest at plugin/.claude-plugin/plugin.json" >&2
    exit 1
fi

outdir="$repo_root/build"
mkdir -p "$outdir"
archive="$outdir/$name.zip"
rm -f "$archive"

# Staged through a copy rather than zipped in place, because the directory has to be renamed
# from plugin/ to the plugin's own name inside the archive. package_skill.sh can zip from the
# working tree directly since the directory is already named correctly; this one cannot.
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT

# -a preserves the executable bit on the bundled scripts. The excludes are the same set
# package_skill.sh drops, plus any archive left over from a previous build.
rsync -a \
    --exclude '.DS_Store' \
    --exclude '__MACOSX/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '*.zip' \
    "$plugin/" "$staging/$name/"

cd "$staging"
COPYFILE_DISABLE=1 zip -r -q "$archive" "$name"

echo "$archive"
unzip -l "$archive" | sed -n '4,$p' | grep -v '^-' | grep -v 'files$' || true
