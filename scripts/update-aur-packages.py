#!/usr/bin/env python3
"""Tiny config-driven updater for this AUR packaging repo.

Most packages only need a `latest` block and a simple `apply` block in
packages.json. Odd packages can get one small apply function below.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "packages.json"
UPDATED_FILE = ROOT / ".aur-updated-packages"
FOUND_FILE = ROOT / ".aur-updates-found"


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "aur-pkgbuild-updater"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def fetch_json(url: str) -> Any:
    return json.loads(fetch_bytes(url).decode())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def version_key(version: str) -> tuple:
    parts: list[object] = []
    for token in re.split(r"([0-9]+)", version.lstrip("v")):
        if token:
            parts.append(int(token) if token.isdigit() else token)
    return tuple(parts)


def pkgbuild_path(pkg: dict[str, Any]) -> Path:
    return ROOT / pkg["dir"] / "PKGBUILD"


def pkgver(pkg: dict[str, Any]) -> str:
    match = re.search(r"^pkgver=(.+)$", pkgbuild_path(pkg).read_text(), re.MULTILINE)
    if not match:
        raise RuntimeError(f"pkgver not found for {pkg['dir']}")
    return match.group(1).strip().strip("'\"")


def replace_once(text: str, pattern: str, repl: str, label: str) -> str:
    updated, count = re.subn(pattern, repl, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError(f"Could not update {label}")
    return updated


def set_array_value(text: str, array_name: str, index: int, value: str) -> str:
    match = re.search(rf"({array_name}=\(\n)(.*?)(\n\))", text, re.DOTALL)
    if not match:
        raise RuntimeError(f"{array_name} array not found")

    lines = match.group(2).splitlines()
    value_lines = [i for i, line in enumerate(lines) if line.strip().startswith("'")]
    if index >= len(value_lines):
        raise RuntimeError(f"{array_name}[{index}] not found")

    lines[value_lines[index]] = re.sub(r"'[^']*'", f"'{value}'", lines[value_lines[index]], count=1)
    return text[: match.start(2)] + "\n".join(lines) + text[match.end(2) :]


def write_basic_pkgbuild_update(pkg: dict[str, Any], latest: str, checksum: str, checksum_index: int) -> None:
    path = pkgbuild_path(pkg)
    text = path.read_text()
    text = replace_once(text, r"^pkgver=.*$", f"pkgver={latest}", "pkgver")
    text = replace_once(text, r"^pkgrel=.*$", "pkgrel=1", "pkgrel")
    text = set_array_value(text, "sha256sums", checksum_index, checksum)
    path.write_text(text)


def replace_printf_json(text: str, output_path: str, json_value: str) -> str:
    pattern = rf"^\s*printf\b[^>\n]*(?:\n['\"]?)?\s*>\s*{re.escape(output_path)}$"
    replacement = f"  printf '%s\\n' '{json_value}' > {output_path}"
    updated, count = re.subn(pattern, lambda _match: replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError(f"Could not update {output_path}")
    return updated


def latest_github_tags(spec: dict[str, Any]) -> tuple[str, dict[str, str]]:
    repo = spec["repo"]
    tags = fetch_json(f"https://api.github.com/repos/{repo}/tags?per_page=100")
    allow_prereleases = bool(spec.get("allow_prereleases", False))
    tag_pattern = r"^v?[0-9]" if allow_prereleases else r"^v?[0-9]+(?:\.[0-9]+)*$"
    versions = [(tag["name"].lstrip("v"), tag["name"]) for tag in tags if re.match(tag_pattern, tag["name"])]
    if not versions:
        raise RuntimeError(f"No version tags found for {repo}")
    version, tag = max(versions, key=lambda item: version_key(item[0]))
    return version, {"repo": repo, "tag": tag}


def latest_pypi(spec: dict[str, Any]) -> tuple[str, dict[str, str]]:
    project = spec["project"]
    metadata = fetch_json(f"https://pypi.org/pypi/{project}/json")
    return metadata["info"]["version"], {"project": project}


LATEST_PROVIDERS = {
    "github-tags": latest_github_tags,
    "pypi": latest_pypi,
}


def latest_for(pkg: dict[str, Any]) -> tuple[str, dict[str, str]]:
    spec = pkg["latest"]
    try:
        provider = LATEST_PROVIDERS[spec["type"]]
    except KeyError as exc:
        raise RuntimeError(f"Unknown latest provider {spec['type']!r} for {pkg['dir']}") from exc
    return provider(spec)


def format_url(template: str, pkg: dict[str, Any], result: dict[str, Any]) -> str:
    return template.format(
        package=pkg["dir"],
        version=result["latest"],
        tag=result.get("tag", result["latest"]),
        repo=result.get("repo", ""),
        project=result.get("project", ""),
    )


def apply_simple_tarball(pkg: dict[str, Any], result: dict[str, Any]) -> None:
    spec = pkg["apply"]
    archive_url = format_url(spec["url"], pkg, result)
    checksum = sha256_bytes(fetch_bytes(archive_url))
    write_basic_pkgbuild_update(pkg, result["latest"], checksum, int(spec.get("source_sha256_index", 0)))


def sdist_release(releases: dict, version: str, project: str) -> dict:
    for item in releases[version]:
        if item.get("packagetype") == "sdist":
            return item
    raise RuntimeError(f"No sdist found for {project} {version}")


def tar_member_bytes(archive: Path, member: str) -> bytes:
    with tarfile.open(archive) as tar:
        extracted = tar.extractfile(member)
        if extracted is None:
            raise RuntimeError(f"{member} not found in {archive}")
        return extracted.read()


def pdfium_version_info(archive: Path) -> tuple[int, int, int, int]:
    version = tar_member_bytes(archive, "VERSION").decode()
    values = dict(line.split("=", 1) for line in version.strip().splitlines())
    return (int(values["MAJOR"]), int(values["MINOR"]), int(values["BUILD"]), int(values["PATCH"]))


def apply_pypdfium2(pkg: dict[str, Any], result: dict[str, Any]) -> None:
    latest = result["latest"]
    metadata = fetch_json("https://pypi.org/pypi/pypdfium2/json")
    sdist = sdist_release(metadata["releases"], latest, "pypdfium2")
    sdist_data = fetch_bytes(sdist["url"])
    sdist_sha = sha256_bytes(sdist_data)

    with tempfile.TemporaryDirectory() as tmpdir:
        sdist_path = Path(tmpdir) / f"pypdfium2-{latest}.tar.gz"
        sdist_path.write_bytes(sdist_data)
        pyproject = tar_member_bytes(sdist_path, f"pypdfium2-{latest}/pyproject.toml").decode()
        record = json.loads(tar_member_bytes(sdist_path, f"pypdfium2-{latest}/autorelease/record.json").decode())

    commit_match = re.search(r"ctypesgen @ git\+https://github.com/pypdfium2-team/ctypesgen@([0-9a-f]+)", pyproject)
    if not commit_match:
        raise RuntimeError("Could not find pinned ctypesgen commit in pypdfium2 pyproject.toml")

    pdfium_ver = int(record["post_pdfium"] or record["pdfium"])
    pdfium_url = (
        "https://github.com/bblanchon/pdfium-binaries/releases/download/"
        f"chromium%2F{pdfium_ver}/pdfium-linux-x64.tgz"
    )
    pdfium_data = fetch_bytes(pdfium_url)
    pdfium_sha = sha256_bytes(pdfium_data)

    with tempfile.TemporaryDirectory() as tmpdir:
        pdfium_archive = Path(tmpdir) / f"pdfium-linux-x64-{pdfium_ver}.tgz"
        pdfium_archive.write_bytes(pdfium_data)
        major, minor, build, patch = pdfium_version_info(pdfium_archive)

    path = pkgbuild_path(pkg)
    text = path.read_text()
    text = replace_once(text, r"^pkgver=.*$", f"pkgver={latest}", "pkgver")
    text = replace_once(text, r"^pkgrel=.*$", "pkgrel=1", "pkgrel")
    text = replace_once(text, r"^_ctypesgencommit=.*$", f"_ctypesgencommit='{commit_match.group(1)}'", "_ctypesgencommit")
    text = replace_once(text, r"^_pdfiumver=.*$", f"_pdfiumver={pdfium_ver}", "_pdfiumver")
    text = set_array_value(text, "sha256sums", 0, sdist_sha)
    text = set_array_value(text, "sha256sums_x86_64", 0, pdfium_sha)
    text = replace_printf_json(
        text,
        "data/linux_x64/version.json",
        f'{{"major":{major},"minor":{minor},"build":{build},"patch":{patch},"n_commits":0,"hash":null,"origin":"pdfium-binaries","flags":[]}}',
    )
    text = replace_printf_json(
        text,
        "data/bindings/version.json",
        f'{{"version":{build},"flags":[],"windows_cross":false}}',
    )
    path.write_text(text)


APPLY_STRATEGIES = {
    "simple-tarball": apply_simple_tarball,
    "pypi-pypdfium2": apply_pypdfium2,
}


def load_packages() -> list[dict[str, Any]]:
    return json.loads(CONFIG.read_text())


def package_by_dir(package_dir: str) -> dict[str, Any]:
    for pkg in load_packages():
        if pkg["dir"] == package_dir:
            return pkg
    raise RuntimeError(f"Unknown package directory: {package_dir}")


def check_package(pkg: dict[str, Any]) -> dict[str, Any]:
    current = pkgver(pkg)
    latest, meta = latest_for(pkg)
    return {
        "package": pkg["dir"],
        "current": current,
        "latest": latest,
        "update": version_key(latest) > version_key(current),
        **meta,
    }


def apply_package(pkg: dict[str, Any], result: dict[str, Any]) -> bool:
    if not result["update"]:
        print(f"{pkg['dir']} is current ({result['current']})")
        return False

    try:
        strategy = APPLY_STRATEGIES[pkg["apply"]["type"]]
    except KeyError as exc:
        raise RuntimeError(f"Unknown apply strategy {pkg['apply']['type']!r} for {pkg['dir']}") from exc
    strategy(pkg, result)
    print(f"{pkg['dir']} updated {result['current']} -> {result['latest']}")
    return True


def write_update_markers(updated: list[str]) -> None:
    UPDATED_FILE.write_text("".join(f"{pkg}\n" for pkg in updated))
    FOUND_FILE.write_text("1\n" if updated else "0\n")


def run_check(package_dir: str, output: Path) -> int:
    result = check_package(package_by_dir(package_dir))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    status = "new version found" if result["update"] else "current"
    print(f"{package_dir}: {status} ({result['current']} -> {result['latest']})")
    return 0


def apply_check_results(results_dir: Path) -> int:
    updated: list[str] = []
    for path in sorted(results_dir.rglob("*.json")):
        result = json.loads(path.read_text())
        pkg = package_by_dir(result["package"])
        if apply_package(pkg, result):
            updated.append(pkg["dir"])
    write_update_markers(updated)
    return 0


def run_updates() -> int:
    updated: list[str] = []
    for pkg in load_packages():
        result = check_package(pkg)
        if apply_package(pkg, result):
            updated.append(pkg["dir"])
    write_update_markers(updated)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-dirs", action="store_true", help="print configured package directories")
    parser.add_argument("--list-dirs-json", action="store_true", help="print configured package directories as a JSON array")
    parser.add_argument("--aur-extras", metavar="PKGDIR", help="print AUR extra files for a package directory")
    parser.add_argument("--check", metavar="PKGDIR", help="check one package and write a JSON result")
    parser.add_argument("--output", type=Path, default=Path("update-check.json"), help="path for --check JSON output")
    parser.add_argument("--apply-checks", type=Path, metavar="DIR", help="apply all update-check JSON files below DIR")
    args = parser.parse_args()

    if args.list_dirs:
        print("\n".join(pkg["dir"] for pkg in load_packages()))
        return 0
    if args.list_dirs_json:
        print(json.dumps([pkg["dir"] for pkg in load_packages()]))
        return 0
    if args.aur_extras:
        print(" ".join(package_by_dir(args.aur_extras).get("aur_extra_files", [])))
        return 0
    if args.check:
        return run_check(args.check, args.output)
    if args.apply_checks:
        return apply_check_results(args.apply_checks)
    return run_updates()


if __name__ == "__main__":
    raise SystemExit(main())
