#!/usr/bin/env python3
"""The bundled Python runtimes' third-party licences: inventory, texts, checks.

The pinned python-build-standalone ``install_only_stripped`` archives carry
only CPython's own licence. The same release's ``pgo+lto-full`` archives
(one per target) describe each runtime in ``python/PYTHON.json`` (every
extension module, the libraries it links and their licences) and carry the
licence texts in ``python/licenses/``. They are read once, by hash, and the
result is checked in under ``packaging/licenses/python/``::

  inventory.json      per target: the full archive (file, sha256, size) and
                      the install-only archive it describes, every library
                      linked into the shipped runtime (name, version, SPDX
                      licences, the texts that cover it, the extension
                      modules that link it) and every extension pruned from
                      it with its libraries
  LICENSE.<name>.txt  the texts the shipped libraries need

so a release build stays offline: ``tools/build.py`` copies the target's
texts into ``share/licenses/python/`` of each bundle, renders their notices
and SBOM components, and refuses a runtime this inventory does not describe
or a shipped library without its text.

Copyleft rule: an extension module whose linked library is under a strong
copyleft licence (:data:`STRONG_COPYLEFT`) is pruned (the runtime's
delete-only list) and may never be shipped; weak copyleft ships with its
text. Libraries the operating system provides (``system``/``framework``
links) are not part of the runtime and need no text here.

Regenerating (after a runtime pin moves; standard library only)::

  python3 tools/_build/runtime_licenses.py derive --archives DIR \\
      --versions downloads.json [--text NAME=FILE ...] --out packaging/licenses/python

``--archives`` holds the four ``pgo+lto-full`` archives (``.tar.zst``; the
sha256 and size are recorded), ``--versions`` the release's
``pythonbuild/downloads.json`` (library versions and licence files), and
``--text`` supplies a text the archives name but do not carry (its source is
recorded with its sha256).
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

FORMAT = 1
INVENTORY = "inventory.json"
DEFAULT_DIR = Path(__file__).resolve().parents[2] / "packaging" / "licenses" / "python"
FLAVOR = "pgo+lto-full"
BUNDLE_SUBDIR = "python"
NOTICES = "THIRD_PARTY_NOTICES.txt"
TARGET_INVENTORY = "INVENTORY.json"
# Strong copyleft (SPDX ids, or prefixes ending in "-"): such a library is
# never shipped; weak copyleft (LGPL, MPL, EPL) ships with its text.
STRONG_COPYLEFT = ("AGPL-", "GPL-", "Sleepycat", "SSPL-", "OSL-", "EUPL-", "RPL-", "CPAL-")
# The library behind each link name of PYTHON.json, as the release's
# downloads.json names it.
LINK_COMPONENTS = {
    "bz2": "bzip2", "crypto": "openssl-3.5", "ssl": "openssl-3.5", "db": "bdb", "edit": "libedit",
    "expat": "expat", "ffi": "libffi", "lzma": "xz", "mpdec": "mpdecimal", "ncursesw": "ncurses",
    "panelw": "ncurses", "sqlite3": "sqlite", "uuid": "uuid", "z": "zlib", "zstd": "zstd",
    "X11": "libX11", "Xau": "libXau", "xcb": "libxcb", "tclstub": "tcl", "tkstub": "tk",
}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TEXT = re.compile(r"LICENSE\.[A-Za-z0-9.+_-]+\.txt")


class LicenseError(Exception):
    """The runtime licence inventory is inconsistent, or a runtime does not match it."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def strong_copyleft(spdx: str) -> bool:
    return any(spdx.startswith(item) if item.endswith("-") else spdx == item for item in STRONG_COPYLEFT)


# --------------------------------------------------------------- the checked-in inventory


@dataclass(frozen=True)
class Inventory:
    directory: Path
    document: Mapping[str, Any]

    def target(self, name: str) -> Mapping[str, Any]:
        try:
            return self.document["targets"][name]
        except KeyError:
            raise LicenseError(f"the runtime licence inventory has no target {name!r}") from None

    def text_path(self, name: str) -> Path:
        return self.directory / name


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LicenseError(message)


def load(directory: Path | str = DEFAULT_DIR) -> Inventory:
    """Read and validate ``inventory.json`` and every text it names (sha256)."""

    root = Path(directory)
    path = root / INVENTORY
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LicenseError(f"{path}: {exc}") from None
    _require(isinstance(document, dict) and document.get("format") == FORMAT, f"{path}: not format {FORMAT}")
    texts = document.get("texts")
    _require(isinstance(texts, dict) and texts, f"{path}: no texts")
    for name, record in texts.items():
        _require(bool(_TEXT.fullmatch(name)), f"{path}: text name {name!r}")
        _require(isinstance(record, dict) and _SHA256.fullmatch(str(record.get("sha256"))) is not None
                 and isinstance(record.get("source"), str) and record["source"], f"{path}: text {name} record")
        try:
            data = (root / name).read_bytes()
        except OSError as exc:
            raise LicenseError(f"{root / name}: {exc.strerror or exc}") from None
        _require(sha256_bytes(data) == record["sha256"], f"{root / name}: sha256 differs from {INVENTORY}")
    on_disk = sorted(p.name for p in root.iterdir() if p.name != INVENTORY)
    _require(on_disk == sorted(texts), f"{root}: files {on_disk} are not exactly the inventory's texts")
    targets = document.get("targets")
    _require(isinstance(targets, dict) and targets, f"{path}: no targets")
    for target, entry in targets.items():
        _check_target(path, target, entry, texts)
    return Inventory(root, document)


def _check_target(path: Path, target: str, entry: Any, texts: Mapping[str, Any]) -> None:
    where = f"{path}: {target}"
    _require(isinstance(entry, dict), f"{where}: not an object")
    for key in ("full_archive", "install_only"):
        record = entry.get(key)
        _require(isinstance(record, dict) and isinstance(record.get("filename"), str)
                 and _SHA256.fullmatch(str(record.get("sha256"))) is not None, f"{where}: {key}")
    runtime = entry.get("runtime")
    _require(isinstance(runtime, dict) and isinstance(runtime.get("version"), str)
             and isinstance(runtime.get("spdx"), list) and runtime["spdx"], f"{where}: runtime")
    libraries = entry.get("libraries")
    _require(isinstance(libraries, list) and libraries, f"{where}: no libraries")
    names = []
    for library in libraries:
        _require(isinstance(library, dict) and isinstance(library.get("name"), str)
                 and isinstance(library.get("version"), str) and library["version"], f"{where}: library {library!r}")
        names.append(library["name"])
        spdx = library.get("spdx")
        _require(isinstance(spdx, list) and (spdx or library.get("public_domain") is True),
                 f"{where}: {library['name']} names no licence")
        for item in spdx:
            _require(not strong_copyleft(item), f"{where}: a shipped extension links {library['name']} ({item}), "
                     "a strong-copyleft library; prune the extension")
        covered = library.get("texts")
        _require(isinstance(covered, list) and covered, f"{where}: {library['name']} has no licence text")
        for name in covered:
            _require(name in texts, f"{where}: {library['name']}'s text {name} is not checked in")
        _require(isinstance(library.get("extensions"), list) and library["extensions"],
                 f"{where}: {library['name']} is linked by no extension")
    _require(names == sorted(set(names)), f"{where}: libraries not sorted and unique")
    pruned = entry.get("pruned")
    _require(isinstance(pruned, list), f"{where}: pruned")
    for item in pruned:
        _require(isinstance(item, dict) and isinstance(item.get("extension"), str)
                 and isinstance(item.get("libraries"), list) and isinstance(item.get("pattern"), str),
                 f"{where}: pruned entry {item!r}")
    shipped = {extension for library in libraries for extension in library["extensions"]}
    for item in pruned:
        _require(item["extension"] not in shipped, f"{where}: {item['extension']} is both shipped and pruned")


def check_runtimes(inventory: Inventory, runtimes: Any, targets: Iterable[str]) -> None:
    """The inventory describes exactly the pinned runtimes (release, CPython
    version, each target's install-only archive) and its pruned extensions
    are on the runtime's delete-only list."""

    document = inventory.document
    _require(document.get("release") == runtimes.release and document.get("python") == runtimes.python,
             f"the runtime licence inventory describes python-build-standalone {document.get('release')} CPython "
             f"{document.get('python')}, the pin is {runtimes.release} CPython {runtimes.python}; regenerate it "
             "(tools/_build/runtime_licenses.py derive)")
    for target in targets:
        entry = inventory.target(target)
        runtime = runtimes.runtime(target)
        recorded = entry["install_only"]
        _require((recorded["filename"], recorded["sha256"]) == (runtime.filename, runtime.sha256),
                 f"{target}: the runtime licence inventory describes {recorded['filename']}, the pin is "
                 f"{runtime.filename}; regenerate it")
        for item in entry["pruned"]:
            _require(item["pattern"] in runtimes.prune,
                     f"{target}: {item['extension']} must be pruned ({item['pattern']} is not on the prune list)")


def check_shipped(inventory: Inventory, target: str, dynload: Path) -> None:
    """After pruning (``dynload``: the runtime's ``lib-dynload``): no pruned
    extension is left and every extension module file it ships is one the
    inventory describes."""

    entry = inventory.target(target)
    shipped = {extension for library in entry["libraries"] for extension in library["extensions"]}
    shipped |= set(entry.get("extensions_without_libraries", []))
    pruned = {item["extension"] for item in entry["pruned"]}
    found = sorted(p.name for p in dynload.iterdir()) if dynload.is_dir() else []
    for name in found:
        if name.startswith("."):
            continue
        module = name.split(".", 1)[0]
        _require(module not in pruned, f"{target}: the pruned extension {module} is still in the runtime ({name})")
        _require(module in shipped, f"{target}: the runtime ships {name}, which the licence inventory does not "
                 "describe; regenerate it")


def install(inventory: Inventory, target: str, destination: Path) -> list[str]:
    """Copy ``target``'s texts into ``destination`` (``share/licenses/python``)
    with its inventory and notices; refuses a library without its text."""

    entry = inventory.target(target)
    destination.mkdir(parents=True, exist_ok=True)
    names: set[str] = set()
    for library in entry["libraries"]:
        if not library.get("texts"):
            raise LicenseError(f"{target}: {library['name']} {library['version']} has no licence text")
        names.update(library["texts"])
    written = []
    for name in sorted(names):
        data = inventory.text_path(name).read_bytes()
        if sha256_bytes(data) != inventory.document["texts"][name]["sha256"]:
            raise LicenseError(f"{inventory.text_path(name)}: sha256 differs from the inventory")
        (destination / name).write_bytes(data)
        written.append(name)
    (destination / TARGET_INVENTORY).write_bytes(target_inventory_bytes(inventory, target))
    (destination / NOTICES).write_bytes(notices_bytes(inventory, target))
    return written + [TARGET_INVENTORY, NOTICES]


def target_inventory_bytes(inventory: Inventory, target: str) -> bytes:
    document = inventory.document
    entry = inventory.target(target)
    body = {"format": FORMAT, "source": document["source"], "release": document["release"],
            "python": document["python"], "target": target, **entry,
            "texts": {name: document["texts"][name] for name in sorted(
                {text for library in entry["libraries"] for text in library["texts"]})}}
    return (json.dumps(body, indent=2, sort_keys=True) + "\n").encode()


def notices_bytes(inventory: Inventory, target: str) -> bytes:
    """The runtime's third-party notices: one section per library, its text inline."""

    document = inventory.document
    entry = inventory.target(target)
    lines = [f"Third-party software in the bundled Python runtime ({target})", "",
             f"CPython {document['python']} from python-build-standalone {document['release']} "
             f"({entry['install_only']['filename']}), licensed under {' AND '.join(entry['runtime']['spdx'])}; "
             "its licence is LICENSE.txt in this directory.",
             "It links the libraries below. The extension modules listed under \"Removed\" are not part of this "
             "runtime.", ""]
    for library in entry["libraries"]:
        licence = " AND ".join(library["spdx"]) or "public domain"
        lines += ["=" * 72, f"{library['name']} {library['version']} — {licence}",
                  f"linked by: {', '.join(library['extensions'])}; texts: {', '.join(library['texts'])}", "=" * 72, ""]
        for name in library["texts"]:
            lines.append(inventory.text_path(name).read_text(encoding="utf-8").rstrip("\n"))
            lines.append("")
    if entry["pruned"]:
        lines += ["=" * 72, "Removed", "=" * 72]
        for item in entry["pruned"]:
            libraries = ", ".join(f"{lib['name']} {lib['version']} ({' AND '.join(lib['spdx'])})"
                                  for lib in item["libraries"]) or "no bundled library"
            lines.append(f"{item['extension']}: {libraries} — {item['reason']}")
        lines.append("")
    return ("\n".join(lines).rstrip("\n") + "\n").encode()


def sbom_components(inventory: Inventory, target: str) -> list[dict[str, Any]]:
    """CycloneDX components for the libraries linked into the runtime."""

    components = []
    for library in inventory.target(target)["libraries"]:
        purl = f"pkg:generic/{library['name']}@{library['version']}"
        component: dict[str, Any] = {"type": "library", "bom-ref": purl, "name": library["name"],
                                     "version": library["version"], "purl": purl}
        if library["spdx"]:
            component["licenses"] = [{"license": {"id": item}} for item in library["spdx"]]
        else:
            component["licenses"] = [{"license": {"name": "public domain"}}]
        component["properties"] = [{"name": "claude-multi:runtime:linked-by", "value": item}
                                   for item in library["extensions"]] + [
            {"name": "claude-multi:runtime:license-text", "value": item} for item in library["texts"]]
        components.append(component)
    return components


# --------------------------------------------------------------- deriving it from the full archives


def _read_members(archive: Path, wanted: Iterable[str]) -> dict[str, bytes]:
    """Members of a ``.tar.zst`` (or ``.tar.gz``) archive by name prefix."""

    prefixes = tuple(wanted)
    if archive.name.endswith(".zst"):
        try:
            from compression import zstd
        except ImportError as exc:  # Python < 3.14
            raise LicenseError(f"reading {archive.name} needs Python 3.14 (compression.zstd)") from exc
        stream: Any = zstd.open(archive, "rb")
    else:
        stream = open(archive, "rb")
    found: dict[str, bytes] = {}
    with stream, tarfile.open(fileobj=stream, mode="r|*") as tar:
        for member in tar:
            if member.isfile() and member.name.startswith(prefixes):
                handle = tar.extractfile(member)
                assert handle is not None
                found[member.name] = handle.read()
    return found


def _sqlite_version(value: str) -> str:
    """downloads.json records SQLite as XYYZZ00 (3530100 = 3.53.1)."""

    if value.isdigit() and len(value) == 7:
        return f"{int(value[0])}.{int(value[1:3])}.{int(value[3:5])}"
    return value


def derive(archives: Mapping[str, Path], install_only: Mapping[str, Mapping[str, str]], versions: Mapping[str, Any],
           prune: Iterable[str], extra_texts: Mapping[str, tuple[bytes, str]], *, release: str, python: str,
           versions_source: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    """The inventory and the texts from each target's full archive."""

    prune = list(prune)
    texts: dict[str, bytes] = {}
    sources: dict[str, str] = {}
    targets: dict[str, Any] = {}
    for target, archive in sorted(archives.items()):
        data = archive.read_bytes()
        members = _read_members(archive, ("python/PYTHON.json", "python/licenses/"))
        meta = json.loads(members["python/PYTHON.json"])
        available = {PurePosixPath(name).name: raw for name, raw in members.items() if name.startswith("python/licenses/")}
        libraries: dict[str, dict[str, Any]] = {}
        pruned: list[dict[str, Any]] = []
        without: list[str] = []
        for extension, variants in sorted(meta["build_info"]["extensions"].items()):
            variant = variants[0]
            links = [link for link in variant.get("links", []) if not link.get("system") and not link.get("framework")]
            shared = variant.get("shared_lib")
            pattern = None
            if shared:
                relative = PurePosixPath(shared).relative_to("install")
                pattern = next((item for item in prune if fnmatch.fnmatchcase(str(relative), item)), None)
            components = sorted({LINK_COMPONENTS[link["name"]] for link in links})
            if pattern is not None:
                pruned.append({"extension": extension, "pattern": pattern, "libraries": [
                    {"name": name, "version": versions[name]["version"], "spdx": versions[name].get("licenses", [])}
                    for name in components], "reason": _prune_reason(components, versions)})
                continue
            if not components:
                if shared:
                    without.append(extension)
                continue
            for name in components:
                record = versions[name]
                license_file = record.get("license_file")
                if not license_file:
                    raise LicenseError(f"{target}: {name} has no licence file in {versions_source}")
                if license_file in available:
                    texts.setdefault(license_file, available[license_file])
                    sources.setdefault(license_file, f"python/licenses/{license_file} of each pgo+lto-full archive")
                    if texts[license_file] != available[license_file]:
                        raise LicenseError(f"{license_file} differs between the archives")
                elif license_file in extra_texts:
                    texts.setdefault(license_file, extra_texts[license_file][0])
                    sources.setdefault(license_file, extra_texts[license_file][1])
                else:
                    raise LicenseError(f"{target}: {name}'s text {license_file} is not in the archive; supply it "
                                       "with --text")
                entry = libraries.setdefault(name, {
                    "name": name, "version": _sqlite_version(record["version"]) if name == "sqlite" else record["version"],
                    "spdx": list(record.get("licenses", [])), "texts": [license_file], "extensions": []})
                if not entry["spdx"]:
                    entry["public_domain"] = True
                entry["extensions"].append(extension)
        targets[target] = {
            "full_archive": {"filename": archive.name, "sha256": sha256_bytes(data), "size": len(data)},
            "install_only": dict(install_only[target]),
            "runtime": {"name": "cpython", "version": python, "spdx": list(meta["licenses"]), "text": "LICENSE.txt"},
            "libraries": [libraries[name] for name in sorted(libraries)],
            "pruned": pruned,
            **({"extensions_without_libraries": sorted(without)} if without else {}),
        }
    document = {
        "format": FORMAT, "release": release, "python": python,
        "source": {"flavor": FLAVOR, "versions": versions_source},
        "texts": {name: {"sha256": sha256_bytes(texts[name]), "source": sources[name]} for name in sorted(texts)},
        "targets": targets,
    }
    return document, texts


def _prune_reason(components: list[str], versions: Mapping[str, Any]) -> str:
    licences = [item for name in components for item in versions[name].get("licenses", [])]
    if any(strong_copyleft(item) for item in licences):
        return "strong copyleft; claude-multi never imports it"
    return "on the runtime's delete-only list"


def write(out: Path, document: Mapping[str, Any], texts: Mapping[str, bytes]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.iterdir():
        if stale.name not in texts and stale.name != INVENTORY:
            stale.unlink()
    for name, data in texts.items():
        (out / name).write_bytes(data)
    (out / INVENTORY).write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import python_runtime  # noqa: E402  (a sibling module)

    parser = argparse.ArgumentParser(prog="runtime_licenses.py")
    commands = parser.add_subparsers(dest="command", required=True)
    derive_parser = commands.add_parser("derive", help="(re)generate the inventory and texts")
    derive_parser.add_argument("--archives", required=True, help="the directory holding the pgo+lto-full archives")
    derive_parser.add_argument("--versions", required=True, help="the release's pythonbuild/downloads.json")
    derive_parser.add_argument("--text", action="append", default=[], metavar="NAME=FILE",
                               help="a text the archives name but do not carry")
    derive_parser.add_argument("--runtimes", default=str(python_runtime.MANIFEST))
    derive_parser.add_argument("--out", default=str(DEFAULT_DIR))
    check_parser = commands.add_parser("check", help="validate the checked-in inventory against the pin")
    check_parser.add_argument("--runtimes", default=str(python_runtime.MANIFEST))
    check_parser.add_argument("--dir", default=str(DEFAULT_DIR))
    args = parser.parse_args(argv)
    runtimes = python_runtime.load(args.runtimes)
    try:
        if args.command == "check":
            check_runtimes(load(args.dir), runtimes, runtimes.targets)
            print("runtime licences: ok")
            return 0
        archives = {}
        for target in runtimes.targets:
            runtime = runtimes.runtime(target)
            name = runtime.filename.replace(f"-{python_runtime.FLAVOR}.tar.gz", f"-{FLAVOR}.tar.zst")
            archives[target] = Path(args.archives) / name
        install_only = {target: {"filename": runtimes.runtime(target).filename,
                                 "sha256": runtimes.runtime(target).sha256} for target in runtimes.targets}
        versions_path = Path(args.versions)
        raw = versions_path.read_bytes()
        extra = {}
        for item in args.text:
            name, _, file = item.partition("=")
            data = Path(file).read_bytes()
            extra[name] = (data, "supplied separately (the archives' PYTHON.json names it but they do not carry "
                                 "it); describe its source here")
        document, texts = derive(archives, install_only, json.loads(raw), runtimes.prune, extra,
                                 release=runtimes.release, python=runtimes.python,
                                 versions_source=f"pythonbuild/downloads.json of {runtimes.release} "
                                                 f"(sha256 {sha256_bytes(raw)})")
        write(Path(args.out), document, texts)
        print(f"wrote {len(texts)} texts and the inventory of {len(document['targets'])} targets to {args.out}")
        return 0
    except LicenseError as exc:
        print(f"runtime licences: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
