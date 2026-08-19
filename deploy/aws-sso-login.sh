#!/usr/bin/env bash
# Sign in to AWS Identity Center and write a usable CLI profile.
#
#   ./deploy/aws-sso-login.sh                 # profile 'kiro'
#   AWS_PROFILE_NAME=demo ./deploy/aws-sso-login.sh
#
# Uses the OIDC device flow, so it works from a terminal with no browser of its
# own: it prints a URL, you approve it anywhere, and it writes short-lived keys
# to ~/.aws/credentials. Credentials expire (typically 8h) — rerun to refresh.
set -euo pipefail

START_URL="${SSO_START_URL:-https://d-90667a7d42.awsapps.com/start}"
REGION="${SSO_REGION:-us-east-1}"
PROFILE="${AWS_PROFILE_NAME:-kiro}"
export AWS_DEFAULT_REGION="$REGION"

command -v aws >/dev/null || { echo "aws CLI not found on PATH" >&2; exit 1; }
PY=$(command -v python3)

# AWS CLI v1 otherwise tries to GET any parameter that looks like a URL.
aws configure set cli_follow_urlparam false

json() { "$PY" -c "import sys,json;$1"; }

reg=$(aws sso-oidc register-client --client-name scenescout-deploy \
      --client-type public --output json)
cid=$(echo "$reg" | json "print(json.load(sys.stdin)['clientId'])")
csec=$(echo "$reg" | json "print(json.load(sys.stdin)['clientSecret'])")

dev=$(aws sso-oidc start-device-authorization --client-id "$cid" \
      --client-secret "$csec" --start-url "$START_URL" --output json)
code=$(echo "$dev" | json "print(json.load(sys.stdin)['deviceCode'])")
interval=$(echo "$dev" | json "print(json.load(sys.stdin).get('interval',5))")

echo
echo "  Open:  $(echo "$dev" | json "print(json.load(sys.stdin)['verificationUriComplete'])")"
echo "  Code:  $(echo "$dev" | json "print(json.load(sys.stdin)['userCode'])")"
echo
echo "  Waiting for approval..."

token=""
for _ in $(seq 1 110); do
  out=$(aws sso-oidc create-token --client-id "$cid" --client-secret "$csec" \
        --grant-type "urn:ietf:params:oauth:grant-type:device_code" \
        --device-code "$code" --output json 2>&1) || true
  if echo "$out" | grep -q '"accessToken"'; then
    token=$(echo "$out" | json "print(json.load(sys.stdin)['accessToken'])")
    break
  fi
  echo "$out" | grep -qi "expired" && { echo "  Device code expired — rerun." >&2; exit 2; }
  sleep "$interval"
done
[[ -n "$token" ]] || { echo "  Timed out." >&2; exit 3; }

accounts=$(aws sso list-accounts --access-token "$token" --output json)
if [[ "$(echo "$accounts" | json "print(len(json.load(sys.stdin)['accountList']))")" == "0" ]]; then
  cat >&2 <<'MSG'

  Signed in, but this user has no AWS accounts assigned to it.

  A Kiro Pro start URL authenticates the IDE subscription, not an AWS account,
  and returns an empty account list however you sign in. Deploy instead from a
  machine that already carries credentials — a workshop VS Code/desktop
  instance, CloudShell, or an EC2 instance with a role — where you can skip
  this script entirely:

      ./deploy/deploy-ec2.sh

MSG
  exit 4
fi
acct=$(echo "$accounts" | json "print(json.load(sys.stdin)['accountList'][0]['accountId'])")
role=$(aws sso list-account-roles --access-token "$token" --account-id "$acct" \
       --output json | json "
names=[r['roleName'] for r in json.load(sys.stdin)['roleList']]
pref=[n for n in names if 'Admin' in n or 'PowerUser' in n]
print((pref or names)[0])")
echo "  Account $acct, role $role"

creds=$(aws sso get-role-credentials --access-token "$token" \
        --account-id "$acct" --role-name "$role" --output json)
set -- $(echo "$creds" | json "
c=json.load(sys.stdin)['roleCredentials']
print(c['accessKeyId'], c['secretAccessKey'], c['sessionToken'])")

aws configure set aws_access_key_id     "$1" --profile "$PROFILE"
aws configure set aws_secret_access_key "$2" --profile "$PROFILE"
aws configure set aws_session_token     "$3" --profile "$PROFILE"
aws configure set region "$REGION"           --profile "$PROFILE"

echo "  Wrote profile '$PROFILE'. Deploy with:"
echo "    AWS_PROFILE=$PROFILE ./deploy/deploy-ec2.sh"
