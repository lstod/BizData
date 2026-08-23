"""What code this is, so a caller can tell whether it is the code they think it is.

Step 6 shipped a Skill against a Lambda four steps behind it. `person_concentration_pct` was
added to `list_engagements`, verified across seventeen seeds on local Postgres and again over
the Data API, and never packaged. The function serving the connector was last modified at
2026-08-21T03:54Z; the column landed in the working tree at 01:48Z the following day. Two
Cowork runs produced wrong deliverables from that gap, and every harness in the repository
passed the whole time, because every harness ran against the checkout.

The gap is structural rather than careless: nothing anywhere asserted that the code answering
the public endpoint was the code on the laptop, so there was no check to forget to run. This
module closes it. `scripts/package_lambda.sh` writes `_build_stamp.json` into the deployment
package, `server/app.py` puts the result on `MCPServer(version=...)`, and it comes back in
`serverInfo` on every MCP response — no new route, nothing published unauthenticated, and no
tool schema changed. `scripts/check_auth.py` compares it to the working tree and says so.

Locally there is no stamp file and the version is read from git instead, which means the
comparison works before anything is deployed and a developer running uvicorn sees the same
string the Lambda would report for that commit.

On the deliberate cost: baking the commit into the package means a commit that changes only
documentation still changes the zip and forces a redeploy, which softens the reproducible-zip
property `package_lambda.sh` is built around. That is the right trade here. The failure being
guarded against is precisely "the tree moved and the deployment did not", and a stamp that
only changes when shipped files change cannot detect it.
"""

from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from pathlib import Path

RELEASE = "0.3.0"

STAMP_PATH = Path(__file__).resolve().parent / "_build_stamp.json"
UNKNOWN = "unknown"


def _from_stamp() -> str | None:
    """The commit baked in at packaging time, if this is a packaged copy."""
    try:
        return json.loads(STAMP_PATH.read_text()).get("commit") or None
    except (OSError, json.JSONDecodeError):
        return None


def _from_git() -> str | None:
    """The working tree's commit, for a server run from a checkout.

    `--dirty` is included rather than dropped. A deployment built from a tree with
    uncommitted changes is a deployment whose commit hash is a half-truth, and the whole
    point of this string is that it does not tell half-truths.
    """
    try:
        result = subprocess.run(
            ["git", "describe", "--always", "--dirty", "--abbrev=12"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


@lru_cache(maxsize=1)
def commit() -> str:
    """The commit this code came from: the stamp if packaged, git if not, else 'unknown'."""
    return _from_stamp() or _from_git() or UNKNOWN


@lru_cache(maxsize=1)
def version() -> str:
    """The string that goes on serverInfo, as ``0.3.0+<commit>``.

    Semver's build-metadata separator, so a client that parses the version at all still
    reads a release of 0.3.0 rather than choking on the suffix.
    """
    return f"{RELEASE}+{commit()}"
