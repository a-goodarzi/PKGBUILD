#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <pkgdir> [extra-file ...]" >&2
  exit 2
fi

pkgdir="$1"
shift || true
pkgbase="$(basename "${pkgdir}")"
workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT
extras=("$@")

if [[ ${#extras[@]} -eq 0 ]]; then
  mapfile -t extras < <(python "$(dirname "$0")/update-aur-packages.py" --aur-extras "${pkgdir}" | xargs -r -n1 printf '%s\n')
fi

repo_url="ssh://aur@aur.archlinux.org/${pkgbase}.git"
if ! git clone "${repo_url}" "${workdir}/${pkgbase}"; then
  mkdir -p "${workdir}/${pkgbase}"
  git -C "${workdir}/${pkgbase}" init
  git -C "${workdir}/${pkgbase}" remote add origin "${repo_url}"
fi

cp "${pkgdir}/PKGBUILD" "${pkgdir}/.SRCINFO" "${workdir}/${pkgbase}/"
for extra in "${extras[@]}"; do
  cp "${pkgdir}/${extra}" "${workdir}/${pkgbase}/"
done

git -C "${workdir}/${pkgbase}" add PKGBUILD .SRCINFO "${extras[@]}"
if git -C "${workdir}/${pkgbase}" diff --cached --quiet; then
  echo "No AUR changes for ${pkgbase}"
  exit 0
fi

git -C "${workdir}/${pkgbase}" commit -m "Update to $(. "${pkgdir}/PKGBUILD"; printf '%s' "${pkgver}")"
git -C "${workdir}/${pkgbase}" push origin master
