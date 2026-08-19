#!/usr/bin/env bash
# Deploy the SceneScout demo site to a single EC2 instance and print its URL.
#
#   ./deploy/deploy-ec2.sh              # create or replace the instance
#   ./deploy/deploy-ec2.sh --terminate  # tear it down
#
# Needs only ec2:* on a default VPC — no IAM roles, no container registry, no
# Docker. The instance clones this repository from GitHub on boot, so push
# before deploying.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
NAME="${SCENESCOUT_STACK:-scenescout-demo}"
TYPE="${SCENESCOUT_INSTANCE_TYPE:-t3.small}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

aws() { command aws --region "$REGION" "$@"; }
say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

running_ids() {
  aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=$NAME" \
              "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text
}

if [[ "${1:-}" == "--terminate" ]]; then
  ids=$(running_ids)
  if [[ -z "$ids" ]]; then say "nothing named $NAME is running"; exit 0; fi
  say "terminating: $ids"
  aws ec2 terminate-instances --instance-ids $ids >/dev/null
  aws ec2 wait instance-terminated --instance-ids $ids
  say "terminated. The security group $NAME-sg is left in place for reuse."
  exit 0
fi

command -v aws >/dev/null || {
  echo "aws CLI not found on PATH." >&2; exit 1; }
who=$(aws sts get-caller-identity --query Arn --output text 2>&1) || {
  cat >&2 <<MSG
No usable AWS credentials for $REGION.

  - already on an AWS machine?  check its instance role
  - laptop, no credentials?     ./deploy/aws-sso-login.sh
  - have a profile?             AWS_PROFILE=<name> $0

$who
MSG
  exit 1; }

say "region $REGION · instance $TYPE · name $NAME"
say "as $who"

VPC=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true \
      --query 'Vpcs[0].VpcId' --output text)
[[ "$VPC" == "None" || -z "$VPC" ]] && { echo "No default VPC in $REGION." >&2; exit 1; }

SUBNET=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" \
         --query 'Subnets[?MapPublicIpOnLaunch==`true`]|[0].SubnetId' --output text)
[[ "$SUBNET" == "None" || -z "$SUBNET" ]] && { echo "No public subnet in $VPC." >&2; exit 1; }
say "vpc $VPC · subnet $SUBNET"

SG=$(aws ec2 describe-security-groups \
     --filters "Name=group-name,Values=$NAME-sg" "Name=vpc-id,Values=$VPC" \
     --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo None)
if [[ "$SG" == "None" || -z "$SG" ]]; then
  say "creating security group $NAME-sg"
  SG=$(aws ec2 create-security-group --group-name "$NAME-sg" --vpc-id "$VPC" \
       --description "SceneScout demo site (HTTP)" --query GroupId --output text)
  aws ec2 authorize-security-group-ingress --group-id "$SG" \
      --protocol tcp --port 80 --cidr 0.0.0.0/0 >/dev/null
fi
say "security group $SG"

AMI=$(aws ssm get-parameter \
      --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
      --query 'Parameter.Value' --output text)
say "ami $AMI"

# Replace rather than accumulate: re-running should give one current instance.
old=$(running_ids)
if [[ -n "$old" ]]; then
  say "replacing existing instance(s): $old"
  aws ec2 terminate-instances --instance-ids $old >/dev/null
fi

say "launching"
ID=$(aws ec2 run-instances \
     --image-id "$AMI" --instance-type "$TYPE" --subnet-id "$SUBNET" \
     --security-group-ids "$SG" --associate-public-ip-address \
     --user-data "file://$HERE/user-data.sh" \
     --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
     --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME},{Key=Project,Value=SceneScout}]" \
     --query 'Instances[0].InstanceId' --output text)
say "instance $ID — waiting for it to run"
aws ec2 wait instance-running --instance-ids "$ID"

IP=$(aws ec2 describe-instances --instance-ids "$ID" \
     --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
URL="http://$IP"
say "public IP $IP — waiting for the site (first boot installs Python deps, ~2-4 min)"

for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$URL/" || true)
  if [[ "$code" == "200" ]]; then
    echo
    say "LIVE:  $URL"
    say "       $URL/calendar"
    exit 0
  fi
  printf '    %2d/60  HTTP %s\n' "$i" "${code:-000}"
  sleep 10
done

echo
echo "The instance is up but the site did not answer in 10 minutes." >&2
echo "Boot log (no SSH key needed):" >&2
echo "  aws ec2 get-console-output --region $REGION --instance-id $ID --output text | tail -40" >&2
exit 1
