#!/usr/bin/env bash
# One-time: jobs captured BEFORE the captured-index existed lack the captured_pk attribute, so they
# would not appear in the extension's list. Adds it. Safe to re-run (only touches items without it).
set -euo pipefail
export AWS_PAGER=""
TABLE=job-radar-jobs

ids=$(aws dynamodb scan --table-name "$TABLE" \
  --filter-expression '#s = :s AND attribute_not_exists(captured_pk)' \
  --expression-attribute-names '{"#s":"source"}' \
  --expression-attribute-values '{":s":{"S":"linkedin-extension"}}' \
  --projection-expression job_id --query 'Items[].job_id.S' --output text)

n=0
for id in $ids; do
  aws dynamodb update-item --table-name "$TABLE" --key "{\"job_id\":{\"S\":\"$id\"}}" \
    --condition-expression 'attribute_exists(job_id)' \
    --update-expression 'SET captured_pk = :c' --expression-attribute-values '{":c":{"S":"CAPTURED"}}'
  n=$((n + 1))
done
echo "backfilled $n job(s)"
