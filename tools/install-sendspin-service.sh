#!/usr/bin/env bash
# Install a released Sendspin service package on Debian-based systems.
set -euo pipefail

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: bash install-sendspin-service.sh [--latest | RELEASE_TAG] [--yes]

Without a version, choose from recent stable releases in an interactive menu.
  --latest   Install or update to the latest stable release.
  RELEASE_TAG  Install an exact release, for example v1.2.3.
  --yes      Confirm installation, updates, or downgrades without prompting.
  --help     Show this help.

Run the installer again to update an existing installation.
EOF
}

release_tag=
assume_yes=false
for argument in "$@"; do
    case "$argument" in
        --help|-h) usage; exit 0 ;;
        --yes|-y) assume_yes=true ;;
        --latest)
            [[ -z "$release_tag" ]] || fail 'Choose only one release.'
            release_tag=latest
            ;;
        -*) fail "Unknown option: $argument. Use --help for usage." ;;
        *)
            [[ -z "$release_tag" ]] || fail 'Choose only one release.'
            release_tag=$argument
            ;;
    esac
done

for command in curl dpkg dpkg-query apt sha256sum systemctl; do
    command -v "$command" >/dev/null || fail "Required command not found: $command"
done
[[ -d /run/systemd/system ]] || fail 'This installer requires a system running systemd.'

architecture=$(dpkg --print-architecture)
case "$architecture" in
    arm64|amd64) ;;
    *) fail "Unsupported architecture: $architecture. Use a 64-bit arm64 or amd64 OS." ;;
esac

release_base=https://github.com/dutchdronesquad/rh-race-voice/releases
installed_version=
installed_package=$(dpkg-query -W -f='${Status} ${Version}' sendspin-service 2>/dev/null || true)
if [[ "$installed_package" == 'install ok installed '* ]]; then
    installed_version=${installed_package#install ok installed }
    printf 'Installed Sendspin service: %s\n' "$installed_version"
else
    printf 'Sendspin service is not installed.\n'
fi

if [[ -z "$release_tag" ]]; then
    [[ -t 0 ]] || fail 'Use --latest or an exact release tag with --yes for unattended installation.'
    command -v python3 >/dev/null || fail 'The release menu requires python3. Alternatively, use --latest or an exact release tag.'
    printf 'Fetching recent stable releases...\n'
    release_list=$(curl --fail --silent --show-error --location \
        'https://api.github.com/repos/dutchdronesquad/rh-race-voice/releases?per_page=30' | \
        python3 -c '
import json, sys
architecture = sys.argv[1]
for release in json.load(sys.stdin):
    if release["draft"] or release["prerelease"]:
        continue
    tag = release["tag_name"]
    package = "sendspin-service_" + tag.removeprefix("v") + "_" + architecture + ".deb"
    assets = {asset["name"] for asset in release["assets"]}
    if package in assets and package + ".sha256" in assets:
        print(tag)
' "$architecture")
    [[ -n "$release_list" ]] || fail 'No stable releases with a matching package were found. Use an exact release tag if needed.'
    mapfile -t release_choices <<< "$release_list"
    printf 'Choose the same release as your Race Voice plugin (newest listed first).\n'
    PS3='Select a release number: '
    select choice in "${release_choices[@]}" 'Cancel'; do
        [[ "$choice" != Cancel ]] || { printf 'Installation cancelled.\n'; exit 0; }
        if [[ -n "$choice" ]]; then
            release_tag=$choice
            break
        fi
        printf 'Enter one of the listed numbers.\n'
    done
    [[ -n "$release_tag" ]] || fail 'No release selected.'
fi
if [[ "$release_tag" == latest ]]; then
    release_url=$(curl --fail --silent --show-error --location --output /dev/null \
        --write-out '%{url_effective}' "$release_base/latest")
    [[ "$release_url" == "$release_base/tag/"* ]] || fail 'Could not determine the latest release.'
    release_tag=${release_url##*/}
fi
[[ "$release_tag" =~ ^v?[0-9][A-Za-z0-9.+-]*$ ]] || fail "Invalid release tag: $release_tag"
version=${release_tag#v}
action=Install
apt_options=(-y -o Dpkg::Options::=--force-confold)
if [[ -n "$installed_version" ]]; then
    if dpkg --compare-versions "$installed_version" eq "$version"; then
        printf 'Sendspin service %s is already installed; no changes made.\n' "$version"
        exit 0
    elif dpkg --compare-versions "$installed_version" lt "$version"; then
        action=Update
    else
        action=Downgrade
        apt_options+=(--allow-downgrades)
    fi
fi
printf '%s Sendspin service: %s -> %s\n' "$action" "${installed_version:-not installed}" "$version"
printf 'Use Race Voice plugin release %s. Existing service configuration will be kept.\n' "$release_tag"
if [[ "$assume_yes" != true ]]; then
    [[ -t 0 ]] || fail 'Use --yes to confirm an unattended installation.'
    read -r -p "$action and restart Sendspin service? [y/N]: " answer || answer=
    case "$answer" in
        y|Y|yes|YES) ;;
        *) printf 'Installation cancelled.\n'; exit 0 ;;
    esac
fi
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
"${privilege[@]}" apt install "${apt_options[@]}" "$install_dir/$package"
"${privilege[@]}" systemctl enable sendspin-service
"${privilege[@]}" systemctl restart sendspin-service
systemctl is-active --quiet sendspin-service \
    || fail 'Service did not start. Check: journalctl -u sendspin-service -n 80 --no-pager'

printf '\nSendspin service %s is installed and running.\n' "$version"
printf 'Use Race Voice plugin release %s.\n' "$release_tag"
printf 'On the same RotorHazard host, keep Sendspin service URL: http://127.0.0.1:8766\n'
printf 'Open the RotorHazard /player page, connect, then click Play audio check.\n'
