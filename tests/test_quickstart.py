"""Runs the real scripts/quickstart.sh against stub aws / terraform / openssl binaries."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

AWS_STUB = r'''#!/usr/bin/env bash
S="$QS_STATE"; echo "aws $*" >> "$S/calls.log"; args="$*"
case "$args" in
  *"sts get-caller-identity"*) [ -z "${QS_NO_CREDS:-}" ] || exit 255; echo 123456789012 ;;
  *"configure get region"*) [ -n "${QS_CLI_REGION-us-east-1}" ] && echo "${QS_CLI_REGION-us-east-1}" || exit 1 ;;
  *"ssm get-parameter"*"--with-decryption"*) cat "$S/secret" ;;
  *"ssm get-parameter"*) [ -f "$S/secret" ] ;;
  *"ssm put-parameter"*) v="${args##*--value }"; echo "${v%% *}" > "$S/secret" ;;
  *"lambda invoke"*)
    out="${@: -1}"
    case "$args" in *job-radar-digest*) echo '{"sent": 7}' > "$out" ;; *) echo '{"fetched": 97, "stored": 58, "errors": []}' > "$out" ;; esac ;;
  *"sns list-subscriptions-by-topic"*)
    n=$(( $(cat "$S/polls" 2>/dev/null || echo 0) + 1 )); echo $n > "$S/polls"
    if [ "$n" -gt "${QS_CONFIRM_AFTER:-1}" ]; then echo arn:aws:sns:us-east-1:123456789012:job-radar:abc; else echo PendingConfirmation; fi ;;
esac
'''
TERRAFORM_STUB = r'''#!/usr/bin/env bash
S="$QS_STATE"; echo "terraform $*" >> "$S/calls.log"; args="$*"
case "$args" in
  version) echo "Terraform v${QS_TF_VERSION:-1.9.5}"; echo "on linux_amd64" ;;
  *"output -raw ingest_url"*) printf 'https://abc123.lambda-url.us-east-1.on.aws/' ;;
  *"output -raw sns_topic_arn"*) printf 'arn:aws:sns:us-east-1:123456789012:job-radar' ;;
  *) echo "terraform ok" ;;
esac
'''
OPENSSL_STUB = '#!/usr/bin/env bash\necho 00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff\n'


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "quickstart.sh", tmp_path / "scripts")
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / "backend.tf").write_text('bucket = "amine-hassoun-aws-terraform-infra-state"\n')
    (tmp_path / "extension").mkdir()
    shutil.copy(ROOT / "extension" / "manifest.json", tmp_path / "extension")
    stubs = tmp_path / "stubs"; stubs.mkdir()
    for name, body in (("aws", AWS_STUB), ("terraform", TERRAFORM_STUB), ("openssl", OPENSSL_STUB)):
        f = stubs / name; f.write_text(body); f.chmod(f.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "state").mkdir()
    return tmp_path


def run(repo, *args, stdin="", **env):
    e = {**os.environ, "PATH": f"{repo / 'stubs'}:{os.environ['PATH']}", "QS_STATE": str(repo / "state"),
         "QS_POLL_SECONDS": "1", "QS_WAIT_SECONDS": "3"}
    for k in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        e.pop(k, None)
    e.update(env)
    return subprocess.run(["bash", str(repo / "scripts" / "quickstart.sh"), *args], input=stdin,
                          capture_output=True, text=True, env=e, timeout=60)


def calls(repo):
    return (repo / "state" / "calls.log").read_text().splitlines()


def test_fresh_install_end_to_end(repo):
    r = run(repo, "me@example.com", "--yes", QS_CONFIRM_AFTER="2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'notification_email = "me@example.com"' in (repo / "infra/terraform.tfvars").read_text()
    assert 'aws_region         = "us-east-1"' in (repo / "infra/terraform.tfvars").read_text()
    assert not (repo / "infra/backend.tf").exists() and (repo / "infra/backend.tf.disabled").exists()
    cfg = (repo / "extension/config.js").read_text()
    assert 'endpoint: "https://abc123.lambda-url.us-east-1.on.aws/"' in cfg and "00112233445566778899aabbccddeeff" in cfg
    manifest = (repo / "extension/manifest.json").read_text()
    assert "lambda-url.us-east-1.on.aws" in manifest and "eu-west-3" not in manifest
    log = "\n".join(calls(repo))
    assert log.count("ssm put-parameter") == 1 and "terraform -chdir=infra apply -auto-approve" in log
    assert log.index("job-radar-fetch") < log.index("sns list-subscriptions") < log.index("job-radar-digest")
    assert "Load unpacked" in r.stdout and '"sent": 7' in r.stdout


def test_rerun_keeps_the_secret(repo):
    run(repo, "me@example.com", "--yes")
    first = (repo / "extension/config.js").read_text()
    r = run(repo, "me@example.com", "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "\n".join(calls(repo)).count("ssm put-parameter") == 1 and "Already exists" in r.stdout
    assert (repo / "extension/config.js").read_text() == first


def test_unconfirmed_email_prints_the_follow_up_command(repo):
    r = run(repo, "me@example.com", "--yes", QS_CONFIRM_AFTER="999")
    assert r.returncode == 0 and "Not confirmed yet" in r.stdout
    assert not any("lambda invoke" in c and "job-radar-digest" in c for c in calls(repo))


def test_no_credentials(repo):
    r = run(repo, "me@example.com", "--yes", QS_NO_CREDS="1")
    assert r.returncode == 1 and "no working credentials" in r.stderr
    assert not (repo / "infra/terraform.tfvars").exists()


def test_no_region(repo):
    r = run(repo, "me@example.com", "--yes", QS_CLI_REGION="")
    assert r.returncode == 1 and "No AWS region is set" in r.stderr


def test_region_from_environment_wins(repo):
    r = run(repo, "me@example.com", "--yes", AWS_REGION="eu-west-1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'aws_region         = "eu-west-1"' in (repo / "infra/terraform.tfvars").read_text()
    assert "lambda-url.eu-west-1.on.aws" in (repo / "extension/manifest.json").read_text()


def test_old_terraform_is_rejected(repo):
    r = run(repo, "me@example.com", "--yes", QS_TF_VERSION="1.5.7")
    assert r.returncode == 1 and "too old" in r.stderr


def test_declining_the_prompt_creates_nothing(repo):
    r = run(repo, "me@example.com", stdin="n\n")
    assert r.returncode == 1 and "Aborted" in r.stdout
    assert not any("put-parameter" in c or "apply" in c for c in calls(repo))


def test_usage_errors(repo):
    assert run(repo).returncode == 2
    assert run(repo, "not-an-email").returncode == 2


@pytest.mark.parametrize("version", ["1.6.0", "1.10.3", "2.0.1"])
def test_accepted_terraform_versions(repo, version):
    r = run(repo, "me@example.com", "--yes", QS_TF_VERSION=version)
    assert r.returncode == 0, r.stdout + r.stderr
