#!/usr/bin/env bash
# Builds Autobench's Cloud Run image with Cloud Build and deploys it with Terraform (infra/autobench.tf),
# the same way as the Vral Altos cloud runner (agents/va_agents_deployment/deploy.sh).
#   deploy/deploy.sh
# Needs the operator's gcloud login, Terraform, Dart (the web UI) and Flutter (Financier's web build).
# Nothing personal goes into the image: state is in Firestore/Cloud Storage, credentials in Secret Manager.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
VA_ROOT="$(cd "$REPO/../.." && pwd)"
PROJECT=vral-altos
REGION=us-central1
TAG="$(date -u +%Y%m%d-%H%M%S)-$(git -C "$REPO" rev-parse --short HEAD)"
IMAGE="$REGION-docker.pkg.dev/$PROJECT/autobench/autobench:$TAG"

# the web UI and Financier's web build (Autobench serves both)
(cd "$REPO/app" && dart run tool/build.dart)
(cd "$VA_ROOT/tools/Financier/app" && flutter build web --release --no-web-resources-cdn --dart-define-from-file=.env)

CTX="$(mktemp -d)"
trap 'rm -rf "$CTX"' EXIT
mkdir -p "$CTX/autobench" "$CTX/financier"
rsync -a --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.dart_tool' \
  --exclude 'server/data/' --exclude '.env' --exclude '.DS_Store' --exclude 'deploy/' --exclude '.mcp.json' \
  "$REPO/" "$CTX/autobench/"
mkdir -p "$CTX/autobench/server/data"
rsync -a "$VA_ROOT/tools/Financier/app/build/web/" "$CTX/financier/"
cp "$HERE/Dockerfile" "$HERE/cloudbuild.yaml" "$CTX/"

gcloud builds submit "$CTX" \
  --project="$PROJECT" --region="$REGION" \
  --config="$CTX/cloudbuild.yaml" \
  --substitutions="_IMAGE=$IMAGE" \
  --service-account="projects/$PROJECT/serviceAccounts/va-builder@$PROJECT.iam.gserviceaccount.com" \
  --gcs-source-staging-dir="gs://$PROJECT-builds/source"

# The deployed image is recorded in infra/ so a later plain `terraform apply` keeps it.
printf '# Written by tools/Autobench/deploy/deploy.sh\nautobench_image = "%s"\n' "$IMAGE" \
  > "$VA_ROOT/infra/autobench.auto.tfvars"
terraform -chdir="$VA_ROOT/infra" apply -input=false -auto-approve
echo "Autobench deployed: $IMAGE"
gcloud run services describe autobench --project="$PROJECT" --region="$REGION" --format='value(status.url)'
