#!/usr/bin/env bash
set -euo pipefail

# Run on an Ubuntu Alibaba Cloud ECS instance after cloning the repository.
# Usage: REPO_DIR=/opt/directorgraph PUBLIC_HOST=director.example.com ./infra/alibaba/deploy-ecs.sh

if [[ "${DIRECTORGRAPH_ALLOW_LOCAL_ECS:-}" != "true" ]]; then
  cat >&2 <<'EOF'
This ECS deployment script is local local-development scaffolding.
It is not the DirectorGraph hackathon judging architecture.

Use scripts/deploy-serverless.sh and infra/alibaba/serverless/ for the
one-image/two-Function-Compute/private-OSS deployment target.

To run this local script intentionally, set:
  DIRECTORGRAPH_ALLOW_LOCAL_ECS=true
EOF
  exit 2
fi

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PUBLIC_HOST="${PUBLIC_HOST:-$(curl -fsS --max-time 3 https://ifconfig.me || echo localhost)}"

if ! command -v docker >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo tee /etc/apt/keyrings/docker.asc >/dev/null
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo usermod -aG docker "$USER"
fi

cd "$REPO_DIR"
if [[ ! -f .env ]]; then
  cp .env.example .env
fi

python3 - <<PY
from pathlib import Path
path = Path('.env')
text = path.read_text()
text = text.replace('PUBLIC_MEDIA_BASE_URL=http://localhost:8000/media', 'PUBLIC_MEDIA_BASE_URL=http://${PUBLIC_HOST}:8000/media')
path.write_text(text)
PY

sudo docker compose up -d --build
sudo docker compose ps

echo "DirectorGraph web: http://${PUBLIC_HOST}:3000"
echo "DirectorGraph API proof: http://${PUBLIC_HOST}:8000/api/health"
echo "Record: ECS console instance page -> SSH terminal -> docker compose ps -> /api/health -> live production."
