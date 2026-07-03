#!/usr/bin/env bash
# One-time GCP setup for the Cloud Run deploy workflow (Tier 2).
#
# Keyless auth: GitHub Actions authenticates via Workload Identity
# Federation, so this script never creates or downloads a service-account
# key. Idempotent - safe to re-run on a project where it already ran;
# existing resources are kept and missing bindings are added.
#
# Run it in Cloud Shell on the target project:
#
#   PROJECT=<your-project-id> \
#   REPO=<your-github-username>/<your-repo> \
#   REGION=<your-region> \
#   bash setup-gcp-wif.sh
#
# At the end it prints the two values to paste into
# .github/workflows/deploy.yml (they are identifiers, not secrets).
set -euo pipefail

: "${PROJECT:?set PROJECT=<gcp-project-id>}"
: "${REPO:?set REPO=<github-owner>/<repo>}"
REGION="${REGION:-europe-north1}"

OWNER="${REPO%%/*}"
POOL="github"
PROVIDER="github-oidc"
DEPLOYER="github-deployer"
RUNNER="scraper-runner"
AR_REPO="scrapers"

gcloud config set project "$PROJECT" >/dev/null

echo "==> Enabling APIs"
gcloud services enable \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  logging.googleapis.com

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')

echo "==> Artifact Registry repo '$AR_REPO' in $REGION"
gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" >/dev/null 2>&1 ||
  gcloud artifacts repositories create "$AR_REPO" \
    --location="$REGION" --repository-format=docker \
    --description="Scraper images pushed from GitHub Actions"

echo "==> Workload Identity pool '$POOL' + provider '$PROVIDER'"
gcloud iam workload-identity-pools describe "$POOL" --location=global >/dev/null 2>&1 ||
  gcloud iam workload-identity-pools create "$POOL" \
    --location=global --display-name="GitHub Actions"

# The attribute condition restricts token exchange to repos owned by
# $OWNER; the per-repo binding below narrows it to exactly $REPO.
gcloud iam workload-identity-pools providers describe "$PROVIDER" \
    --location=global --workload-identity-pool="$POOL" >/dev/null 2>&1 ||
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
    --location=global --workload-identity-pool="$POOL" \
    --display-name="GitHub OIDC" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
    --attribute-condition="assertion.repository_owner=='$OWNER'"

echo "==> Service accounts '$DEPLOYER' and '$RUNNER'"
for SA in "$DEPLOYER" "$RUNNER"; do
  gcloud iam service-accounts describe "$SA@$PROJECT.iam.gserviceaccount.com" >/dev/null 2>&1 ||
    gcloud iam service-accounts create "$SA" --display-name="$SA"
done

DEPLOYER_EMAIL="$DEPLOYER@$PROJECT.iam.gserviceaccount.com"
RUNNER_EMAIL="$RUNNER@$PROJECT.iam.gserviceaccount.com"

echo "==> Allow $REPO to impersonate $DEPLOYER_EMAIL"
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_EMAIL" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.repository/$REPO" \
  >/dev/null

echo "==> Deployer roles (push images, deploy+run the job, read logs)"
for ROLE in roles/artifactregistry.writer roles/run.developer roles/logging.viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$DEPLOYER_EMAIL" --role="$ROLE" \
    --condition=None >/dev/null
done

echo "==> Allow deployer to attach $RUNNER_EMAIL to the job"
gcloud iam service-accounts add-iam-policy-binding "$RUNNER_EMAIL" \
  --role="roles/iam.serviceAccountUser" \
  --member="serviceAccount:$DEPLOYER_EMAIL" \
  >/dev/null

cat <<EOF

============================================================
Done. Paste these into .github/workflows/deploy.yml
(plain identifiers - hardcode them, no secrets needed):

  env block:
    PROJECT_ID: $PROJECT
    REGION: $REGION

  auth step:
    workload_identity_provider: projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER
    service_account: $DEPLOYER_EMAIL
============================================================
EOF
