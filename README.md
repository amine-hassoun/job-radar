# job-radar

A personal, zero-cost job-search pipeline: capture LinkedIn postings while browsing, pull fresh remote listings from RemoteOK, score every one against my profile, and get one ranked email per day.

**Cost: $0/month.** Lambda, DynamoDB (provisioned), EventBridge rules, CloudWatch, SNS and SSM standard parameters are all Always Free. No NAT Gateway, no VPC, no LLM calls.

## Architecture

```
Chrome extension ──POST (shared-secret header)──▶ Lambda Function URL ─▶ ingest ─┐
(record mode: postings I open)                                                    │
                                                                                  ▼
EventBridge 04:00 UTC ─▶ fetch (RemoteOK) ─▶ score ─▶ DynamoDB job-radar-jobs
                                                                  │  sparse index: pending-index
EventBridge 05:30 UTC ─▶ digest ◀── Query (unsent, score ≥ 45) ◀──┘
                           └─▶ SNS ─▶ email  (+ CloudWatch error alarms)
```

| Piece | Where | Notes |
|---|---|---|
| Capture | `extension/` | Manifest V3. Record mode auto-captures only postings I open; it never navigates or scrolls. A service worker does the network call. |
| Ingest | `infra/lambda/src/ingest.py` | Constant-time secret check, validation, scoring, conditional write (no duplicates). |
| Fetch | `infra/lambda/src/fetch.py` | RemoteOK API (remote jobs worldwide). Failure raises, which trips the Errors alarm and emails me. |
| Score | `infra/lambda/src/scoring.py` | Title rules + weighted skills + TF-IDF cosine + remote/location bonus, minus eligibility and seniority wording. Config in `profile.json`. |
| Digest | `infra/lambda/src/digest.py` | Reads a sparse index (`digest_pk = PENDING`), publishes, then marks jobs notified. |

## Scoring (0-100)

| Component | Points |
|---|---|
| Target title (DevOps, SRE, Platform, Cloud...) | +30 (excluded titles: -30) |
| Skills matched, weighted, capped | up to +40 |
| TF-IDF cosine vs my profile text | up to +10 |
| Remote / EU location | +10 / +5 |
| Visa, residency or language wording | -30, flagged `check-eligibility` |
| Principal/staff/10+ years wording | -10, flagged `senior-wording` |

Deterministic and explainable: every score lists the skills that matched. Tune `profile.json`, not code.

## Setup

```bash
# 1. Shared secret (SecureString, never in Terraform state)
aws ssm put-parameter --name /job-radar/ingest-shared-secret --type SecureString --value "$(openssl rand -hex 32)" --overwrite

# 2. Infrastructure
cd infra && echo 'notification_email = "you@example.com"' > terraform.tfvars
terraform init && terraform apply     # then click the SNS confirmation link in your inbox

# 3. Extension: copy extension/config.example.js to config.js, fill endpoint + secret,
#    then chrome://extensions → Developer mode → Load unpacked → extension/
```

First run without waiting for the schedule:

```bash
aws lambda invoke --function-name job-radar-fetch --cli-read-timeout 130 /dev/stdout
aws lambda invoke --function-name job-radar-digest /dev/stdout
```

## Security decisions

- Function URL uses `AuthType NONE` (needs both resource-policy statements) and is protected by a shared secret compared in constant time.
- One IAM role per function, each limited to the exact actions and ARNs it uses.
- The fetcher is intentionally not VPC-attached: public API calls with free egress.
- DynamoDB is PROVISIONED with explicit small capacity; CI fails on `PAY_PER_REQUEST`, NAT Gateways, load balancers or `vpc_config`.
- Capture is passive: only postings I already have open, at human pace.

## Development

```bash
pip install boto3 "moto[dynamodb,ssm,sns]" pytest && pytest tests -q
```

CI (`.github/workflows/checks.yml`) runs the cost guard, `terraform fmt/validate`, the tests and checkov. Listings from RemoteOK link back to their original pages, as their terms require.

## Lessons learned

- A sparse GSI beats a Scan on tiny provisioned throughput: the digest reads only unsent matches.
- Scheduled jobs must fail loudly: a failed fetch raises, the Errors alarm emails.
- Dropped Adzuna: it needed an API key and only added EU on-site listings; LinkedIn capture covers those.
- Content scripts can't call arbitrary origins; the extension's service worker owns the network.
