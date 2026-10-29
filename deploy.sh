#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

VM_HOST=""
VM_USER=""
REMOTE_DIR="~/daybrief"

usage() {
    echo "Usage:"
    echo "  $0 -i <VM_IP_OR_HOST> -u <VM_USER> [-d <REMOTE_DIR>]"
    echo
    echo "Example:"
    echo "  $0 -i 1.2.3.4 -u username"
    echo
    echo "Options:"
    echo "  -i, --ip       Nebius VM public IP or SSH hostname"
    echo "  -u, --user     SSH username"
    echo "  -d, --dir      Remote directory (default: ~/daybrief)"
    echo "  -h, --help     Show this help"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -i|--ip)
            [[ $# -ge 2 ]] || {
                echo "Missing value for $1" >&2
                exit 1
            }
            VM_HOST="$2"
            shift 2
            ;;

        -u|--user|--name)
            [[ $# -ge 2 ]] || {
                echo "Missing value for $1" >&2
                exit 1
            }
            VM_USER="$2"
            shift 2
            ;;

        -d|--dir)
            [[ $# -ge 2 ]] || {
                echo "Missing value for $1" >&2
                exit 1
            }
            REMOTE_DIR="$2"
            shift 2
            ;;

        -h|--help)
            usage
            exit 0
            ;;

        *)
            echo "Unknown argument: $1" >&2
            usage
            exit 1
            ;;
    esac
done

if [[ -z "$VM_HOST" ]]; then
    echo "Error: VM IP/hostname is required." >&2
    usage
    exit 1
fi

if [[ -z "$VM_USER" ]]; then
    echo "Error: VM SSH username is required." >&2
    usage
    exit 1
fi

[[ "$VM_HOST" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:-]*$ ]] || {
    echo "Invalid VM_HOST" >&2
    exit 1
}

[[ "$VM_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || {
    echo "Invalid VM_USER" >&2
    exit 1
}

[[ "$REMOTE_DIR" =~ ^[a-zA-Z0-9_./~-]+$ ]] || {
    echo "REMOTE_DIR must not contain spaces or shell metacharacters" >&2
    exit 1
}

for tool in docker ssh scp; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "Missing command: $tool" >&2
        exit 1
    }
done

docker info >/dev/null
docker buildx version >/dev/null

[[ -f "docker-compose.prod.yml" ]] || {
    echo "docker-compose.prod.yml not found" >&2
    exit 1
}

[[ -f "deploy/nginx.conf" ]] || {
    echo "deploy/nginx.conf not found" >&2
    exit 1
}

host="$VM_HOST"
[[ "$host" != *:* ]] || host="[$host]"

target="$VM_USER@$host"

archive_dir=$(mktemp -d "${TMPDIR:-/tmp}/daybrief-deploy.XXXXXX")
trap 'rm -rf "$archive_dir"' EXIT

echo
echo "Checking remote VM..."

printf -v quoted_dir '%q' "$REMOTE_DIR"

ssh "$target" "bash -s -- $quoted_dir" > "$archive_dir/remote-path" <<'REMOTE'
set -euo pipefail

remote_dir=$1

case "$remote_dir" in
    '~/'*)
        remote_dir="$HOME/${remote_dir:2}"
        ;;
    /*)
        ;;
    *)
        echo "REMOTE_DIR must be absolute or start with ~/" >&2
        exit 1
        ;;
esac

command -v docker >/dev/null || {
    echo "Docker is not installed on VM" >&2
    exit 1
}

docker info >/dev/null || {
    echo "Docker is not running or current user cannot access Docker" >&2
    exit 1
}

docker compose version >/dev/null || {
    echo "Docker Compose is not available on VM" >&2
    exit 1
}

[[ "$(uname -m)" == "x86_64" ]] || {
    echo "Expected x86_64 Nebius VM because images are built for linux/amd64" >&2
    exit 1
}

mkdir -p "$remote_dir/deploy"

if [[ ! -f "$remote_dir/backend/.env" ]]; then
    echo "Warning: $remote_dir/backend/.env does not exist yet." >&2
    echo "Create it on the VM before starting services if your backend requires it." >&2
fi

printf '%s\n' "$remote_dir"
REMOTE

remote_path=$(cat "$archive_dir/remote-path")

[[ "$remote_path" =~ ^/[a-zA-Z0-9_./-]+$ ]] || {
    echo "Unsupported remote path: $remote_path" >&2
    exit 1
}

echo
echo "Building Linux amd64 images locally..."

docker buildx build \
    --platform linux/amd64 \
    --load \
    -t daybrief-backend:deploy \
    ./backend

docker buildx build \
    --platform linux/amd64 \
    --load \
    -t daybrief-frontend:deploy \
    ./frontend

printf 'FROM nginx:stable-alpine\n' | \
docker buildx build \
    --platform linux/amd64 \
    --load \
    -t daybrief-proxy:deploy \
    -

echo
echo "Saving Docker images..."

docker image save \
    -o "$archive_dir/images.tar" \
    daybrief-backend:deploy \
    daybrief-frontend:deploy \
    daybrief-proxy:deploy

echo
echo "Transferring images and deployment files..."

scp \
    "$archive_dir/images.tar" \
    "$target:$remote_path/images.tar"

scp \
    docker-compose.prod.yml \
    "$target:$remote_path/docker-compose.prod.yml"

scp \
    deploy/nginx.conf \
    "$target:$remote_path/deploy/nginx.conf"

echo
echo "Deploying on Nebius VM..."

printf -v quoted_path '%q' "$remote_path"

ssh "$target" "bash -s -- $quoted_path" <<'REMOTE'
set -euo pipefail

cd "$1"

compose=(
    docker compose
    -p daybrief
    -f docker-compose.prod.yml
)

echo
echo "Validating Docker Compose config..."
"${compose[@]}" config --quiet

echo
echo "Loading Docker images..."
docker load -i images.tar
rm -f images.tar

echo
echo "Starting/updating containers..."
"${compose[@]}" up \
    -d \
    --pull never \
    --wait \
    --wait-timeout 180

echo
echo "Container status:"
"${compose[@]}" ps

echo
echo "Checking local health endpoint..."

curl \
    --fail \
    --silent \
    --show-error \
    --retry 8 \
    --retry-connrefused \
    --retry-delay 2 \
    --max-time 10 \
    http://127.0.0.1:8080/health

printf '\n'
REMOTE

echo
echo "========================================"
echo "Deployment completed successfully."
echo "========================================"
echo
echo "URL:"
echo "  http://${VM_HOST}:8080/"
echo
echo "Health:"
echo "  http://${VM_HOST}:8080/health"
echo
echo "External access requires TCP 8080 to be allowed"
echo "in the Nebius firewall/security group."