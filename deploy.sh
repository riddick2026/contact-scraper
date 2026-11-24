#!/usr/bin/env bash
# Deploy the committed code to Azure.
#
#   ./deploy.sh test         -> contact-scraper-4b54ea-test (any branch; normally `test`)
#   ./deploy.sh production   -> contact-scraper-4b54ea / vendors.crosbyllc.com (only from `main`)
#
# Deploys what is committed at HEAD (not unsaved edits). Requires `az login`.
set -euo pipefail
cd "$(dirname "$0")"

case "${1:-}" in
  test)       APP=contact-scraper-4b54ea-test; URL=https://contact-scraper-4b54ea-test.azurewebsites.net ;;
  production) APP=contact-scraper-4b54ea;      URL=https://vendors.crosbyllc.com ;;
  *) echo "Usage: $0 test|production" >&2; exit 2 ;;
esac

BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ -n "$(git status --porcelain)" ]; then
  echo "Note: you have uncommitted changes; they will NOT be deployed (only commit $(git rev-parse --short HEAD))."
fi
if [ "$1" = production ]; then
  if [ "$BRANCH" != main ]; then
    echo "Refusing: production deploys only from 'main' (you're on '$BRANCH'). Merge your tested changes into main first." >&2
    exit 1
  fi
  git fetch -q origin main
  if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
    echo "Refusing: local main differs from GitHub. Push (or pull) first so production matches GitHub." >&2
    exit 1
  fi
fi

ZIP=$(mktemp -t deploy).zip
git archive --format=zip -o "$ZIP" HEAD
echo "Deploying $BRANCH @ $(git rev-parse --short HEAD) to $APP ..."
az webapp deploy -g rg-contact-scraper -n "$APP" --src-path "$ZIP" --type zip -o none
rm -f "$ZIP"
echo "Done. Open $URL (the first load after a deploy can take a minute)."
