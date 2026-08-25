#!/usr/bin/env python3
"""Prove the plugin is installable, current, and carries no credential.

    python scripts/check_plugin.py                 everything, including the live checks
    python scripts/check_plugin.py --offline       skip anything needing AWS or the network
    python scripts/check_plugin.py -v              show the assertions that passed too

Takes no seed. The other five harnesses drive the tools against a database seventeen times;
this one asserts things about files, and about whether those files still describe the thing
that is deployed. Closer to check_auth.py than to check_tools.py.

Three groups of assertion, and the middle one is the reason this file exists.

**Structure.** The manifest parses, the marketplace entry points at a real plugin directory,
the three Skills each have their SKILL.md and their bundled script, and the built package
carries no macOS cruft, no bytecode and no nested archive. Cheap, and it catches the class of
mistake step 7 caught by hand: an upload that is missing the script the Skill depends on.

**Currency.** The endpoint and the OAuth client id written into plugin/.mcp.json are compared
against what Terraform actually deployed, and the callback port is compared against the redirect
URIs Cognito will actually accept. This is step 7's freshness check pointed at the manifest
rather than at the Lambda, and for the same reason: steps 6 and 7 both shipped an artifact that
described a deployment four commits behind it, and every harness passed because every harness
read the working tree. A manifest in a public repository is worse than a stale Lambda, because
the stale Lambda is at least fixable without anyone re-installing anything.

**No credential ships.** The manifest format has a field for an OAuth client id and none for a
client secret, which is a good design and an awkward one — see docs/notes/plugin-packaging.md.
The awkwardness creates an obvious temptation, so the absence is asserted three ways: no key
named like a secret, no string shaped like one, and specifically not the real value, fetched
from Terraform and searched for. The third is the one that would actually catch it.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PLUGIN_DIR = REPO_ROOT / "plugin"
MANIFEST = PLUGIN_DIR / ".claude-plugin" / "plugin.json"
MCP_CONFIG = PLUGIN_DIR / ".mcp.json"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"

PLUGIN_NAME = "bizdata-delivery-review"

# Each Skill and the files it cannot ship without. Step 7's finding is that a Skill missing its
# bundled script does not fail — the model writes a substitute and the run looks fine — so the
# only place this can be caught is before upload.
SKILLS: dict[str, tuple[str, ...]] = {
    "assemble-delivery-pack": ("scripts/build_workbook.py",),
    "house-format": ("scripts/build_deck.py", "assets/self-check.md"),
    "scope-escalation": ("scripts/classify.py",),
}

# Reserved for Anthropic, per the marketplace documentation. A marketplace using one of these
# names is rejected at add time, which is a confusing failure to debug from the other end.
RESERVED_MARKETPLACE_NAMES = {
    "claude-code-marketplace", "claude-code-plugins", "claude-plugins-official",
    "claude-plugins-community", "claude-community", "anthropic-marketplace",
    "anthropic-plugins", "agent-skills", "anthropic-agent-skills",
    "knowledge-work-plugins", "life-sciences", "claude-for-legal",
    "claude-for-financial-services", "financial-services-plugins",
    "first-party-plugins", "healthcare",
}

# Cowork's published limits for an uploaded package.
MAX_PACKAGE_BYTES = 200 * 1024 * 1024
MAX_PACKAGE_FILES = 5000

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+].*)?$")
KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class Checks:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [r for r in self.results if not r[1]]


def load_json(path: Path) -> tuple[dict | None, str]:
    try:
        return json.loads(path.read_text()), "parsed"
    except FileNotFoundError:
        return None, "missing"
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"


def terraform_output(name: str) -> str | None:
    """One raw output, or None when Terraform or AWS is not reachable."""
    try:
        result = subprocess.run(
            ["terraform", f"-chdir={REPO_ROOT / 'infra' / 'main'}", "output", "-raw", name],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def http_status(url: str) -> int:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return 0


def frontmatter(text: str) -> dict[str, str]:
    """The keys of a SKILL.md's YAML frontmatter, without a YAML dependency.

    Only top-level ``key:`` lines are read, and folded values are not reassembled — every
    assertion here is about whether a key is present and what it starts with, so the body of
    a folded block is not needed and parsing it properly would mean taking the dependency.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    keys: dict[str, str] = {}
    for line in text[3:end].splitlines():
        match = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line)
        if match:
            keys[match.group(1)] = match.group(2).strip()
    return keys


# ------------------------------------------------------------------- structure


def check_manifest(checks: Checks) -> dict:
    manifest, detail = load_json(MANIFEST)
    checks.add("the plugin manifest parses", manifest is not None, detail)
    if manifest is None:
        return {}

    checks.add(
        "its name is the plugin's namespace, in kebab-case",
        manifest.get("name") == PLUGIN_NAME and bool(KEBAB.match(manifest.get("name", ""))),
        str(manifest.get("name")),
    )
    checks.add(
        "it carries a version, and the version is semver",
        bool(SEMVER.match(str(manifest.get("version", "")))),
        str(manifest.get("version")),
    )
    # Not cosmetic: the description is what the plugin manager shows, and it is the only thing
    # somebody deciding whether to install this will read.
    description = str(manifest.get("description", ""))
    checks.add(
        "it describes itself in more than a name",
        len(description) > 60,
        f"{len(description)} chars",
    )
    checks.add(
        "it names an author and a repository",
        bool(manifest.get("author")) and bool(manifest.get("repository")),
        str(manifest.get("repository")),
    )
    # Only plugin.json belongs in .claude-plugin/. Everything else at the plugin root. This is
    # the single most common plugin mistake and it fails silently — the components just do not
    # load, with no error saying why.
    stray = [p.name for p in (PLUGIN_DIR / ".claude-plugin").iterdir() if p.name != "plugin.json"]
    checks.add(
        "nothing but plugin.json lives in .claude-plugin/",
        not stray,
        ", ".join(stray) if stray else "clean",
    )
    return manifest


def check_marketplace(checks: Checks, manifest: dict) -> None:
    marketplace, detail = load_json(MARKETPLACE)
    checks.add("the marketplace catalogue parses", marketplace is not None, detail)
    if marketplace is None:
        return

    name = str(marketplace.get("name", ""))
    checks.add(
        "the marketplace has a name, an owner and a plugin list",
        bool(name) and bool(marketplace.get("owner")) and isinstance(marketplace.get("plugins"), list),
        name,
    )
    checks.add(
        "its name is not one Anthropic reserves",
        name.lower() not in RESERVED_MARKETPLACE_NAMES,
        name,
    )

    plugins = marketplace.get("plugins") or []
    checks.add("it lists exactly one plugin", len(plugins) == 1, f"{len(plugins)} entries")
    if len(plugins) != 1:
        return

    entry = plugins[0]
    checks.add(
        "the catalogue entry's name matches the manifest's",
        entry.get("name") == manifest.get("name"),
        f"{entry.get('name')} vs {manifest.get('name')}",
    )

    # Relative sources resolve against the marketplace root — the directory containing
    # .claude-plugin/ — and not against .claude-plugin/ itself. Getting this wrong produces a
    # marketplace that adds cleanly and then cannot install anything.
    source = entry.get("source")
    checks.add("the source is a relative path inside the repository", isinstance(source, str) and source.startswith("./"), str(source))
    if isinstance(source, str) and source.startswith("./"):
        resolved = (REPO_ROOT / source[2:]).resolve()
        checks.add(
            "and it resolves to a directory holding a plugin manifest",
            (resolved / ".claude-plugin" / "plugin.json").is_file(),
            str(resolved.relative_to(REPO_ROOT)),
        )


def check_skills(checks: Checks) -> None:
    found = sorted(p.name for p in (PLUGIN_DIR / "skills").iterdir() if p.is_dir())
    checks.add(
        "all three Skills are present and nothing else is",
        found == sorted(SKILLS),
        ", ".join(found),
    )

    for skill, bundled in SKILLS.items():
        root = PLUGIN_DIR / "skills" / skill
        skill_md = root / "SKILL.md"
        if not skill_md.is_file():
            checks.add(f"{skill}: SKILL.md exists", False, "missing")
            continue

        keys = frontmatter(skill_md.read_text())
        checks.add(
            f"{skill}: frontmatter names the skill and describes when to use it",
            keys.get("name") == skill and "description" in keys,
            f"name={keys.get('name')}",
        )

        for relative in bundled:
            path = root / relative
            checks.add(f"{skill}: {relative} is bundled", path.is_file(), "present" if path.is_file() else "missing")
            # A script that does not compile reaches the sandbox looking fine and fails at the
            # moment it is needed, which by step 7's finding is the moment the model quietly
            # writes its own replacement.
            if path.is_file() and path.suffix == ".py":
                try:
                    compile(path.read_text(), str(path), "exec")
                    ok, note = True, "compiles"
                except SyntaxError as exc:
                    ok, note = False, f"line {exc.lineno}: {exc.msg}"[:70]
                checks.add(f"{skill}: {relative} compiles", ok, note)


# ------------------------------------------------------------------- currency


def check_mcp_config(checks: Checks, offline: bool) -> dict:
    config, detail = load_json(MCP_CONFIG)
    checks.add("the connector configuration parses", config is not None, detail)
    if config is None:
        return {}

    servers = config.get("mcpServers") or {}
    checks.add("it declares exactly one server", len(servers) == 1, f"{len(servers)} declared")
    if len(servers) != 1:
        return {}

    server = next(iter(servers.values()))

    # A url entry without an explicit type is read as a stdio server and rejected. This is the
    # documented trap and the reason the plan's premise — that the format cannot express a
    # remote server — was worth checking rather than believing.
    checks.add(
        "the server is declared remote, with an explicit http type",
        server.get("type") == "http" and str(server.get("url", "")).startswith("https://"),
        f"type={server.get('type')}",
    )

    oauth = server.get("oauth") or {}
    checks.add(
        "it pins an OAuth client id, a callback port and its scopes",
        bool(oauth.get("clientId")) and bool(oauth.get("callbackPort")) and bool(oauth.get("scopes")),
        f"port={oauth.get('callbackPort')}, scopes={oauth.get('scopes')}",
    )

    if offline:
        return server

    # Cognito serves OIDC discovery and not RFC 8414 — measured at step 14, and the reason
    # authServerMetadataUrl is set at all. Asserted as a pair, because the 200 alone would also
    # be consistent with a discovery chain that never needed overriding.
    metadata_url = str(oauth.get("authServerMetadataUrl", ""))
    checks.add(
        "the authorization server metadata it points at answers 200",
        http_status(metadata_url) == 200 if metadata_url else False,
        f"{metadata_url[:60]}... -> {http_status(metadata_url) if metadata_url else 'unset'}",
    )
    if metadata_url.endswith("/.well-known/openid-configuration"):
        rfc8414 = metadata_url.replace("/.well-known/openid-configuration", "/.well-known/oauth-authorization-server")
        status = http_status(rfc8414)
        checks.add(
            "and the RFC 8414 path does not, which is why it is pinned",
            status != 200,
            f"HTTP {status} at the RFC 8414 path",
        )

    return server


def check_currency(checks: Checks, server: dict) -> None:
    """The manifest still describes what is deployed."""
    endpoint = terraform_output("mcp_endpoint")
    client_id = terraform_output("cognito_client_id")

    if endpoint is None or client_id is None:
        checks.add(
            "the manifest was compared against the deployment",
            False,
            "terraform output unavailable — run with --offline to skip, or refresh AWS credentials",
        )
        return

    checks.add(
        "the endpoint in the manifest is the deployed endpoint",
        server.get("url") == endpoint,
        server.get("url", "") if server.get("url") == endpoint else f"{server.get('url')} != {endpoint}",
    )

    oauth = server.get("oauth") or {}
    checks.add(
        "the OAuth client id in the manifest is the deployed client",
        oauth.get("clientId") == client_id,
        str(oauth.get("clientId")) if oauth.get("clientId") == client_id else f"{oauth.get('clientId')} != {client_id}",
    )

    # The redirect URI is exact-matched by Cognito, so a callback port the app client does not
    # know about fails at the very end of the flow, after a browser round trip, with an error
    # page rather than anything in a log. Worth asserting from this side.
    issuer = terraform_output("cognito_issuer") or ""
    pool_id = issuer.rsplit("/", 1)[-1] if issuer else ""
    port = oauth.get("callbackPort")
    if pool_id and port:
        expected = f"http://localhost:{port}/callback"
        try:
            import boto3

            described = boto3.client("cognito-idp", region_name=pool_id.split("_")[0]).describe_user_pool_client(
                UserPoolId=pool_id, ClientId=client_id
            )
            registered = described["UserPoolClient"].get("CallbackURLs", [])
            checks.add(
                "the callback port is a redirect URI Cognito will accept",
                expected in registered,
                expected if expected in registered else f"{expected} not in {registered}",
            )
        except Exception as exc:  # noqa: BLE001 — any failure here is the assertion failing
            checks.add("the callback port is a redirect URI Cognito will accept", False, str(exc)[:80])


# ------------------------------------------------------------------- no credential ships


def check_no_secret(checks: Checks, offline: bool) -> None:
    files = [p for p in PLUGIN_DIR.rglob("*") if p.is_file()]
    blob = "\n".join(p.read_text(errors="replace") for p in files)

    named = re.search(r"client[_-]?secret", blob, re.IGNORECASE)
    checks.add(
        "no field in the plugin is named like a client secret",
        not named,
        "absent" if not named else f"found {named.group(0)!r}",
    )
    # Cognito app client secrets are long lowercase-alphanumeric strings. So are plenty of
    # harmless things, so this is scoped to values sitting in JSON rather than to prose.
    json_blob = "\n".join(p.read_text(errors="replace") for p in files if p.suffix == ".json")
    suspicious = re.findall(r'"[a-z0-9]{40,}"', json_blob)
    checks.add(
        "no forty-character secret-shaped value sits in any JSON",
        not suspicious,
        ", ".join(s[:16] + "..." for s in suspicious) if suspicious else "none",
    )
    key = re.search(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b", blob)
    checks.add(
        "no AWS access key id anywhere in the plugin",
        not key,
        "absent" if not key else f"found {key.group(0)[:8]}...",
    )

    if offline:
        return

    # The one that would actually catch it. The three above are shape checks and a shape check
    # cannot tell a secret from a hash; this compares against the real value.
    secret = terraform_output("cognito_client_secret")
    if secret is None:
        checks.add(
            "the real client secret was searched for and not found",
            False,
            "terraform output unavailable — run with --offline to skip",
        )
        return
    checks.add(
        "the real client secret appears nowhere in the plugin",
        secret not in blob,
        "absent" if secret not in blob else "PRESENT — do not commit",
    )


# ------------------------------------------------------------------- the package


def check_package(checks: Checks) -> None:
    script = REPO_ROOT / "scripts" / "package_plugin.sh"
    with tempfile.TemporaryDirectory():
        result = subprocess.run([str(script)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=180)
    checks.add("the package builds", result.returncode == 0, (result.stderr or "built").splitlines()[0][:70])
    if result.returncode != 0:
        return

    archive = REPO_ROOT / "build" / f"{PLUGIN_NAME}.zip"
    checks.add("and lands where the script says it did", archive.is_file(), str(archive.relative_to(REPO_ROOT)))
    if not archive.is_file():
        return

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        payload = sum(info.file_size for info in bundle.infolist())
        manifest_bytes = bundle.read(f"{PLUGIN_NAME}/.claude-plugin/plugin.json")

    cruft = [n for n in names if ".DS_Store" in n or "__MACOSX" in n or "__pycache__" in n or n.endswith(".pyc")]
    checks.add("the package carries no macOS cruft and no bytecode", not cruft, ", ".join(cruft[:3]) if cruft else "clean")

    nested = [n for n in names if n.endswith(".zip")]
    checks.add("and no nested archive", not nested, ", ".join(nested) if nested else "none")

    # The archive has to unpack to a directory whose .claude-plugin/plugin.json is findable.
    # A zip of the contents rather than of the directory is the same class of mistake step 7
    # found with a bare SKILL.md, one level up.
    checks.add(
        "it unpacks to the plugin's own directory, not to loose files",
        f"{PLUGIN_NAME}/.claude-plugin/plugin.json" in names,
        f"{PLUGIN_NAME}/",
    )

    # Built from the working tree, so it cannot describe a different plugin than the one in the
    # repository. Cheap, and it is the assertion that fails if somebody edits a staged copy.
    checks.add(
        "the packaged manifest is the manifest in the checkout",
        manifest_bytes == MANIFEST.read_bytes(),
        "identical",
    )

    file_count = len([n for n in names if not n.endswith("/")])
    checks.add(
        "it is inside Cowork's package limits",
        payload < MAX_PACKAGE_BYTES and file_count < MAX_PACKAGE_FILES,
        f"{payload / 1024:.0f} KiB across {file_count} files",
    )


def check_cli_validation(checks: Checks) -> None:
    """What Anthropic's own validator says, which is the only opinion that binds.

    Everything above is this repository's reading of the manifest schema. This is the schema's
    own reading, and it is the check that will notice when the format moves — a field that
    becomes required, or one that quietly stops being recognised. ``--strict`` makes warnings
    count, because a warning here means an unrecognised field, and an unrecognised field is
    usually a typo in a field that would otherwise have done something.

    Requires Claude Code v2.x. The v1 line has no ``plugin`` subcommand at all and drops into
    an interactive session instead, which is what this build found on first attempt.
    """
    for label, path in (("plugin", PLUGIN_DIR), ("marketplace", REPO_ROOT)):
        try:
            result = subprocess.run(
                ["claude", "plugin", "validate", str(path), "--strict"],
                capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks.add(f"claude plugin validate accepts the {label} manifest", False, f"CLI unavailable: {exc}"[:70])
            continue
        output = (result.stdout + result.stderr).strip().splitlines()
        summary = next((line.strip() for line in reversed(output) if line.strip()), "no output")
        checks.add(
            f"claude plugin validate accepts the {label} manifest",
            result.returncode == 0 and "Validation passed" in result.stdout,
            summary[:70],
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="skip anything needing AWS or the network")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    checks = Checks()

    manifest = check_manifest(checks)
    if manifest:
        check_marketplace(checks, manifest)
    check_skills(checks)

    server = check_mcp_config(checks, args.offline)
    if server and not args.offline:
        check_currency(checks, server)

    check_no_secret(checks, args.offline)
    check_cli_validation(checks)
    check_package(checks)

    failed = checks.failed
    if args.verbose or failed:
        print(f"{'':>3}  {'ok':<3} {'assertion':<62} detail")
        for i, (name, ok, detail) in enumerate(checks.results, 1):
            if args.verbose or not ok:
                print(f"{i:>3}  {'t' if ok else 'F':<3} {name[:62]:<62} {detail}")
        print()

    print(
        f"{len(checks.results) - len(failed)} of {len(checks.results)} assertions passed"
        + ("" if not failed else f" -- {len(failed)} FAILED")
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
