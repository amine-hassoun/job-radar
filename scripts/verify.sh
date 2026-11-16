#!/usr/bin/env bash
# End-to-end verification of the deployed job-radar stack.
# Read-only except two synthetic test items (deleted at the end) and ONE real digest email.
# Usage: AWS_PROFILE=<terraform user> ./scripts/verify.sh
set -uo pipefail
export AWS_PAGER=""
cd "$(dirname "$0")/../infra" || exit 2

PASS=0; FAIL=0
ok()      { echo "  PASS  $1"; PASS=$((PASS + 1)); }
bad()     { echo "  FAIL  $1${2:+  -> $2}"; FAIL=$((FAIL + 1)); }
info()    { echo "  info  $1"; }
check()   { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected '$2', got '$3'"; fi; }
section() { echo; echo "== $1"; }
code()    { curl -s -o /dev/null -w "%{http_code}" -H 'Content-Type: application/json' "$@"; }
jget()    { python3 -c "import sys,json;print(json.load(sys.stdin)$1)" 2>/dev/null; }

TABLE=job-radar-jobs
URL=$(terraform output -raw ingest_url 2>/dev/null)
TOPIC=$(terraform output -raw sns_topic_arn 2>/dev/null)
KEY=$(aws ssm get-parameter --name /job-radar/ingest-shared-secret --with-decryption \
      --query Parameter.Value --output text 2>/dev/null)
if [ -z "$URL" ] || [ -z "$TOPIC" ] || [ -z "$KEY" ]; then
  echo "Could not read terraform outputs or the secret. Check AWS_PROFILE / region."; exit 2
fi

section "1. Terraform state"
terraform plan -detailed-exitcode -input=false >/dev/null 2>&1; rc=$?
check "no drift (plan exit code 0)" 0 "$rc"

section "2. DynamoDB"
check "table ACTIVE" ACTIVE "$(aws dynamodb describe-table --table-name $TABLE --query Table.TableStatus --output text)"
check "pending-index ACTIVE" ACTIVE "$(aws dynamodb describe-table --table-name $TABLE \
  --query 'Table.GlobalSecondaryIndexes[?IndexName==`pending-index`]|[0].IndexStatus' --output text)"
check "captured-index ACTIVE" ACTIVE "$(aws dynamodb describe-table --table-name $TABLE \
  --query 'Table.GlobalSecondaryIndexes[?IndexName==`captured-index`]|[0].IndexStatus' --output text)"
check "TTL enabled" ENABLED "$(aws dynamodb describe-time-to-live --table-name $TABLE \
  --query TimeToLiveDescription.TimeToLiveStatus --output text)"
POOL=$(for t in $(aws dynamodb list-tables --query 'TableNames[]' --output text); do
         aws dynamodb describe-table --table-name "$t" --output json; done | python3 -c '
import sys, json
dec = json.JSONDecoder(); s = sys.stdin.read(); i = 0; r = w = 0
while True:
    while i < len(s) and s[i].isspace(): i += 1
    if i >= len(s): break
    o, i = dec.raw_decode(s, i); t = o["Table"]
    for x in [t] + t.get("GlobalSecondaryIndexes", []):
        p = x.get("ProvisionedThroughput", {})
        r += p.get("ReadCapacityUnits", 0); w += p.get("WriteCapacityUnits", 0)
print(r, w)')
read -r R W <<<"$POOL"
if [ "${R:-99}" -le 25 ] && [ "${W:-99}" -le 25 ]; then ok "Always Free pool: $R RCU / $W WCU (limit 25 / 25)"
else bad "DynamoDB pool over the Always Free limit" "$R RCU / $W WCU"; fi

section "3. Lambda functions"
for f in ingest fetch digest; do
  got=$(aws lambda get-function-configuration --function-name "job-radar-$f" \
        --query '[Runtime,Handler,VpcConfig.VpcId||`none`]' --output text 2>&1)
  check "$f: python3.13, $f.handler, not in a VPC" "$(printf 'python3.13\t%s.handler\tnone' "$f")" "$got"
done

section "4. SNS"
SUB=$(aws sns list-subscriptions-by-topic --topic-arn "$TOPIC" --query 'Subscriptions[0].SubscriptionArn' --output text)
if [[ "$SUB" == arn:aws:sns:* ]]; then ok "email subscription confirmed"
else bad "email subscription" "$SUB (click the confirmation link in your inbox)"; fi

section "5. Schedules and alarms"
check "fetch rule ENABLED" ENABLED "$(aws events describe-rule --name job-radar-fetch-daily --query State --output text)"
check "digest rule ENABLED" ENABLED "$(aws events describe-rule --name job-radar-digest-daily --query State --output text)"
for a in ingest fetch digest; do
  st=$(aws cloudwatch describe-alarms --alarm-names "job-radar-$a-errors" --query 'MetricAlarms[0].StateValue' --output text)
  if [ "$st" = OK ] || [ "$st" = INSUFFICIENT_DATA ]; then ok "alarm $a-errors is $st"; else bad "alarm $a-errors" "$st"; fi
done

section "6. Ingest endpoint (auth, validation, scoring, dedupe)"
check "no key -> 401"        401 "$(code -X POST "$URL" -d '{}')"
check "wrong key -> 401"     401 "$(code -X POST "$URL" -H 'x-job-radar-key: wrong' -d '{}')"
check "bad JSON -> 400"      400 "$(code -X POST "$URL" -H "x-job-radar-key: $KEY" -d 'not json')"
check "empty batch -> 400"   400 "$(code -X POST "$URL" -H "x-job-radar-key: $KEY" -d '{"jobs":[]}')"
check "short description is rejected" "[0]" "$(curl -s -X POST "$URL" -H 'Content-Type: application/json' \
  -H "x-job-radar-key: $KEY" -d '{"jobs":[{"jobId":"9999999990","title":"x","company":"y","description":"short"}]}' | jget '["rejected"]')"

LOW='{"jobs":[{"jobId":"9999999991","title":"Verification Test Posting","company":"TestCo","location":"Nowhere","description":"Synthetic posting used only to verify the ingest endpoint end to end. It mentions nothing relevant, so it must score very low and never reach the digest email. Lorem ipsum dolor sit amet."}]}'
HIGH='{"jobs":[{"jobId":"9999999992","title":"DevOps Engineer","company":"TestCo","location":"Remote, Europe","description":"Synthetic posting. Own our AWS platform: Terraform modules, Kubernetes with Helm, GitHub Actions CI/CD pipelines, Docker, Prometheus and Grafana observability, Linux and Python automation. Fully remote across Europe."}]}'
post() { curl -s -X POST "$URL" -H 'Content-Type: application/json' -H "x-job-radar-key: $KEY" -d "$1"; }

R=$(post "$LOW");  check "irrelevant job stored" 1 "$(echo "$R" | jget '["stored"]')"
S=$(echo "$R" | jget '["scores"][0]'); [ "${S:-99}" -lt 45 ] && ok "irrelevant job scores low ($S < 45)" || bad "irrelevant job score" "$S"
R=$(post "$LOW");  check "same job again is a duplicate" 1 "$(echo "$R" | jget '["duplicates"]')"
R=$(post "$HIGH"); check "relevant job stored" 1 "$(echo "$R" | jget '["stored"]')"
S=$(echo "$R" | jget '["scores"][0]'); [ "${S:-0}" -ge 45 ] && ok "relevant job scores high ($S >= 45)" || bad "relevant job score" "$S"

section "6b. Extension API (list / export / delete)"
api() { curl -s -X POST "$URL" -H 'Content-Type: application/json' -H "x-job-radar-key: $KEY" -d "$1"; }
sleep 5   # GSI propagation is eventually consistent
check "list with a wrong key -> 401" 401 "$(code -X POST "$URL" -H 'x-job-radar-key: wrong' -d '{"action":"list"}')"
check "unknown action -> 400" 400 "$(code -X POST "$URL" -H "x-job-radar-key: $KEY" -d '{"action":"nope"}')"
LIST=$(api '{"action":"list","only_fits":true}')
echo "$LIST" | grep -q '"jobId": "9999999992"' && ok "list(only_fits) includes the relevant test job" || bad "list(only_fits) missing the relevant test job"
echo "$LIST" | grep -q '"jobId": "9999999991"' && bad "list(only_fits) wrongly includes the irrelevant test job" || ok "list(only_fits) excludes the irrelevant test job"
api '{"action":"list"}' | grep -q '"jobId": "9999999991"' && ok "list(all) includes the irrelevant test job" || bad "list(all) missing the irrelevant test job"
api '{"action":"export","job_ids":["9999999992"]}' | grep -q 'Synthetic posting' && ok "export returns the full description" || bad "export did not return the description"
check "export of a RemoteOK-style or unknown id returns nothing" "[]" "$(api '{"action":"export","job_ids":["1234567890"]}' | jget '["jobs"]')"

section "7. Fetch Lambda (live RemoteOK call)"
FE=$(aws lambda invoke --function-name job-radar-fetch --cli-read-timeout 130 /tmp/jr_fetch.json --query FunctionError --output text)
check "fetch ran without FunctionError" None "$FE"
check "fetch reported no source errors" "[]" "$(jget '["errors"]' </tmp/jr_fetch.json)"
N=$(jget '["fetched"]' </tmp/jr_fetch.json); [ "${N:-0}" -gt 0 ] && ok "fetched $N jobs from RemoteOK" || bad "fetched 0 jobs"

section "7b. RemoteOK tab (list / export, separate from LinkedIn)"
sleep 5   # GSI propagation is eventually consistent
RO=$(api '{"action":"list","source":"remoteok"}')
RN=$(echo "$RO" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["jobs"]))' 2>/dev/null)
[ "${RN:-0}" -gt 0 ] && ok "RemoteOK list returns $RN job(s)" || bad "RemoteOK list is empty" "run the fetch first, or no match scored above the store threshold"
RID=$(echo "$RO" | python3 -c 'import sys,json;print(json.load(sys.stdin)["jobs"][0]["jobId"])' 2>/dev/null)
if [ -n "$RID" ]; then
  api "{\"action\":\"export\",\"source\":\"remoteok\",\"job_ids\":[\"$RID\"]}" | grep -q '"description": "' \
    && ok "RemoteOK export returns the stored description" || bad "RemoteOK export has no description"
  api '{"action":"list"}' | grep -q "\"jobId\": \"$RID\"" \
    && bad "LinkedIn list wrongly contains a RemoteOK job" || ok "LinkedIn list does not contain RemoteOK jobs"
fi
check "unknown source -> 400" 400 "$(code -X POST "$URL" -H "x-job-radar-key: $KEY" -d '{"action":"list","source":"nope"}')"

section "8. Digest end to end (sends ONE real email containing the synthetic job)"
sleep 5   # GSI propagation is eventually consistent
FE=$(aws lambda invoke --function-name job-radar-digest /tmp/jr_digest.json --query FunctionError --output text)
check "digest ran without FunctionError" None "$FE"
SENT=$(jget '["sent"]' </tmp/jr_digest.json)
[ "${SENT:-0}" -ge 1 ] && ok "digest published $SENT item(s): check your inbox for 'TestCo'" || bad "digest sent nothing" "$SENT"
sleep 5
PEND=$(aws dynamodb query --table-name $TABLE --index-name pending-index --key-condition-expression 'digest_pk = :p' \
       --expression-attribute-values '{":p":{"S":"PENDING"}}' --select COUNT --query Count --output text)
check "pending index empty after digest (re-run is a no-op)" 0 "$PEND"

section "9. Data"
for s in remoteok linkedin-extension; do
  n=$(aws dynamodb scan --table-name $TABLE --filter-expression '#s = :s' --expression-attribute-names '{"#s":"source"}' \
      --expression-attribute-values "{\":s\":{\"S\":\"$s\"}}" --select COUNT --query Count --output text)
  info "$s items stored: $n"
done

section "10. Cleanup (through the delete action, which tests it too)"
check "delete removes both synthetic jobs" 2 "$(api '{"action":"delete","job_ids":["9999999991","9999999992"]}' | jget '["deleted"]')"
sleep 5
api '{"action":"list"}' | grep -qE '"jobId": "99999999(91|92)"' && bad "deleted jobs still listed" || ok "deleted jobs no longer listed"
[ -z "$(aws dynamodb get-item --table-name $TABLE --key '{"job_id":{"S":"linkedin-extension#9999999992"}}' --query Item --output text | grep -v '^None$')" ] \
  && ok "synthetic item gone from the table" || bad "synthetic item still in the table"

echo; echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
