#!/usr/bin/env python3
"""Write the next native contract from a verified Claude Code release manifest.

Maintainer tooling (Python standard library plus GnuPG). It reads Anthropic's
signed release manifest pair for VERSION from DIR (``DIR/VERSION/manifest.json``
and ``.sig``, or ``DIR/manifest-VERSION.json`` and ``.sig``), verifies the
signature with the vendored Anthropic release key in a throwaway keyring, and
writes the next contract into the checkout: every platform build the manifest
lists (size and sha256), the manifest and signature digests, the key
fingerprint, the date, and the evidence classes (``battery`` for the platform
the essential battery ran on, ``identity+smoke`` for the others) with the
sha256 of the battery receipt, and the settings keys the build knows: from
``--settings-keys-from FILE`` (that version's build for the battery platform,
checked against the manifest, read without running it) or a reviewed
``--settings-keys FILE`` (a JSON array of key names). A new version needs
one of them; a re-recording of the pinned version keeps the recorded keys.
The behaviour sections of the current contract carry over unchanged.

It never fetches anything and never runs the battery; ``claude-multi-dev
repin`` runs the whole flow (candidate inspection, the battery, the contract
write). To fetch the pair yourself::

    https://downloads.claude.ai/claude-code-releases/VERSION/manifest.json
    https://downloads.claude.ai/claude-code-releases/VERSION/manifest.json.sig
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from claude_multi import catalog, layout, pin, release_manifest, strict_json, upgrade  # noqa: E402
from claude_multi import validate as schema_validate  # noqa: E402


def build_contract(
    current: dict, *, version: str, manifest: bytes, signature: bytes, fingerprint: str,
    battery_platform: str, receipt_sha256: str, today: str, settings_keys: list[str] | None = None,
) -> dict:
    """The next contract document (pure; the signature is already verified)."""

    platforms = release_manifest.manifest_platforms(manifest, version=version)
    if battery_platform not in platforms:
        raise release_manifest.ReleaseManifestMismatch(
            f"the manifest lists no {battery_platform} build (the battery platform)")
    record = platforms[battery_platform]
    proof = release_manifest.ReleaseProof(
        version, battery_platform, str(record["sha256"]), int(record["size"]),
        hashlib.sha256(manifest).hexdigest(), hashlib.sha256(signature).hexdigest(),
        fingerprint, platforms)
    inspection = upgrade.CandidateInspection(path=Path(version), version=version,
                                             sha256=proof.sha256, release=proof)
    return upgrade.render_contract(current, inspection, today=today, receipt_sha256=receipt_sha256,
                                   settings_keys=settings_keys)


def settings_keys_from(path: Path, manifest: bytes, *, version: str, platform: str) -> list[str]:
    """The settings keys of ``path`` after checking it is ``version``'s
    ``platform`` build from the signed manifest (size and sha256)."""

    record = release_manifest.manifest_platforms(manifest, version=version).get(platform)
    if record is None:
        raise release_manifest.ReleaseManifestMismatch(f"the manifest lists no {platform} build")
    if path.stat().st_size != int(record["size"]) or pin.file_sha256(path) != record["sha256"]:
        raise release_manifest.ReleaseManifestMismatch(
            f"{path} is not the signed {version} {platform} build (size or sha256 differ)")
    return upgrade.settings_keys_from_binary(path)


def check_contract(document: dict, schema: dict) -> list[str]:
    problems = schema_validate.validate(document, schema, "$")
    if not problems:
        problems = catalog._check_effort_contract(document, "$") + catalog._check_verified_pins(document, "$")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools/pin_claude.py",
        description="Verify a Claude Code release manifest and write the next native contract.")
    parser.add_argument("version", help="the Claude Code version to pin (X.Y.Z)")
    parser.add_argument("--manifest-dir", type=Path, required=True, metavar="DIR",
                        help="directory with VERSION/manifest.json and manifest.json.sig")
    parser.add_argument("--receipt", type=Path, required=True, metavar="FILE",
                        help="the essential battery receipt of this version (its sha256 is recorded)")
    parser.add_argument("--battery-platform", default="linux-x64", metavar="PLATFORM",
                        help="the platform the battery ran on (default: linux-x64)")
    parser.add_argument("--repo", type=Path, default=REPO, metavar="PATH",
                        help="the source checkout to write (default: this checkout)")
    parser.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                        help="verification date (default: today, UTC)")
    keys = parser.add_mutually_exclusive_group()
    keys.add_argument("--settings-keys-from", type=Path, default=None, metavar="FILE",
                      help="this version's build for the battery platform: record the settings "
                           "keys it knows (read from the file, never run)")
    keys.add_argument("--settings-keys", type=Path, default=None, metavar="FILE",
                      help="a reviewed JSON array of the settings key names this version knows")
    parser.add_argument("--dry-run", action="store_true", help="print the contract, write nothing")
    args = parser.parse_args(argv)

    today = args.date or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    try:
        # Writes go only into a verified source checkout, through real
        # directories inside it.
        checkout = layout.verify_checkout(args.repo)
        resources = layout.checkout_resources(checkout)
        contract_path = layout.checkout_destination(checkout, resources / "catalog" / "native-contract.json")
    except layout.LayoutError as exc:
        print(f"pin_claude: {exc}", file=sys.stderr)
        return 2
    schema = strict_json.load(resources / "schemas" / "native-contract.schema.json")
    try:
        current = strict_json.load(contract_path)
        manifest, signature = release_manifest.load_local(args.manifest_dir, args.version)
        gpg = release_manifest.gpg_executable(os.environ)
        fingerprint = release_manifest.verify_signature(manifest, signature, gpg=gpg)
        receipt = hashlib.sha256(args.receipt.read_bytes()).hexdigest()
        keys = None
        if args.settings_keys_from is not None:
            keys = settings_keys_from(args.settings_keys_from, manifest, version=args.version,
                                      platform=args.battery_platform)
        elif args.settings_keys is not None:
            keys = upgrade.read_settings_keys_file(args.settings_keys)
        document = build_contract(
            current, version=args.version, manifest=manifest, signature=signature,
            fingerprint=fingerprint, battery_platform=args.battery_platform,
            receipt_sha256=receipt, today=today, settings_keys=keys)
    except (OSError, release_manifest.ReleaseManifestError, upgrade.UpgradeError) as exc:
        print(f"pin_claude: {exc}", file=sys.stderr)
        return 2
    problems = check_contract(document, schema)
    if problems:
        print("pin_claude: the next contract is invalid: " + "; ".join(problems), file=sys.stderr)
        return 2
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.dry_run:
        sys.stdout.write(text)
        return 0
    try:
        upgrade._write_checkout_file(checkout, contract_path, text.encode("utf-8"))
    except upgrade.UpgradeError as exc:
        print(f"pin_claude: {exc}", file=sys.stderr)
        return 2
    entry = document["verified"][0]
    keys = f", {len(entry['settings_keys'])} settings keys" if "settings_keys" in entry else ""
    print(f"wrote {contract_path}: Claude Code {args.version}, "
          f"{len(entry['platforms'])} platform builds{keys}, signed by …{fingerprint[-16:]}")
    print(f"next: bump catalog_version in {resources / 'version.json'} and the version-pinned test "
          "literals (claude-multi-dev repin does both), then run the full suite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
