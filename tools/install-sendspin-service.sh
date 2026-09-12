#!/usr/bin/env bash
# Install a released Sendspin service package on Debian-based systems.
set -euo pipefail

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

for command in curl dpkg apt sha256sum systemctl; do
    command -v "$command" >/dev/null || fail "Required command not found: $command"
done
[[ -d /run/systemd/system ]] || fail 'This installer requires a system running systemd.'
[[ $# -le 1 ]] || fail 'Usage: install-sendspin-service.sh [release-tag]'

architecture=$(dpkg --print-architecture)
case "$architecture" in
    arm64|amd64) ;;
    *) fail "Unsupported architecture: $architecture. Use a 64-bit arm64 or amd64 OS." ;;
esac

release_base=https://github.com/dutchdronesquad/rh-race-voice/releases
release_tag=${1:-}
if [[ -z "$release_tag" ]]; then
    release_url=$(curl --fail --silent --show-error --location --output /dev/null \
        --write-out '%{url_effective}' "$release_base/latest")
    [[ "$release_url" == "$release_base/tag/"* ]] || fail 'Could not determine the latest release.'
    release_tag=${release_url##*/}
fi
[[ "$release_tag" =~ ^v?[0-9][A-Za-z0-9.+-]*$ ]] || fail "Invalid release tag: $release_tag"
version=${release_tag#v}
package="sendspin-service_${version}_${architecture}.deb"
download_url="$release_base/download/$release_tag/$package"

install_dir=$(mktemp -d /tmp/sendspin-install.XXXXXXXX)
trap 'rm -rf "$install_dir"' EXIT
# Allow apt's _apt user to read the downloaded package.
chmod 755 "$install_dir"
printf 'Downloading Sendspin service %s for %s...\n' "$version" "$architecture"
curl --fail --silent --show-error --location "$download_url" -o "$install_dir/$package"
curl --fail --silent --show-error --location "$download_url.sha256" -o "$install_dir/checksum"
read -r expected_checksum _ < "$install_dir/checksum"
[[ "$expected_checksum" =~ ^[[:xdigit:]]{64}$ ]] || fail 'Invalid package checksum.'
printf '%s  %s\n' "$expected_checksum" "$install_dir/$package" | sha256sum --check --status \
    || fail 'Package checksum verification failed.'
chmod 644 "$install_dir/$package"

privilege=()
if [[ $EUID -ne 0 ]]; then
    command -v sudo >/dev/null || fail 'Run this installer as root or install sudo.'
    privilege=(sudo)
fi
"${privilege[@]}" apt install -y "$install_dir/$package"
"${privilege[@]}" systemctl enable --now sendspin-service
systemctl is-active --quiet sendspin-service \
    || fail 'Service did not start. Check: journalctl -u sendspin-service -n 80 --no-pager'

printf '\nSendspin service %s is installed and running.\n' "$version"
printf 'Use Race Voice plugin release %s.\n' "$release_tag"
printf 'On the same RotorHazard host, keep Sendspin service URL: http://127.0.0.1:8766\n'
printf 'Open the RotorHazard /player page, connect, then click Play audio check.\n'
