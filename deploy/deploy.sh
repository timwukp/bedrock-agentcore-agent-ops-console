#!/usr/bin/env bash
# Deploy the Agent CI/CD Admin dashboard: Lambda + HTTP API Gateway + DynamoDB + Cognito.
# Idempotent-ish: safe to re-run for code updates (skips resources that already exist).
#
# Prereqs: aws cli v2 configured, python3, zip. Fill in the variables below (or export them).
set -euo pipefail

# ── required configuration ────────────────────────────────────────────────────
REGION="${REGION:-us-east-1}"
UI_HARNESS="${UI_HARNESS:?set UI_HARNESS to your UI-test harness id, e.g. MyUiTest-AbC123}"
BUGFIX_HARNESS="${BUGFIX_HARNESS:-}"          # optional second harness to monitor
TARGET_REPO="${TARGET_REPO:-}"                 # org/repo of the CI workflow (GitHub public API)
TARGET_URL="${TARGET_URL:-}"                   # site under test
QA_BUCKET="${QA_BUCKET:-}"                     # S3 bucket with qa reports (pr-<n>/test-report-latest.json)
LOGIN_SECRET_ID="${LOGIN_SECRET_ID:-}"         # Secrets Manager secret with test-site creds
SKILL_REPO_URL="${SKILL_REPO_URL:-}"           # git repo the harness skill lives in
SPANS_SINCE="${SPANS_SINCE:-}"                 # ISO ts when OTEL_TRACES_SAMPLER=always_on was enabled

FN=agent-cicd-admin
ROLE=AgentAdminLambdaRole
TABLE=AgentAdminRuns
POOL_NAME=agent-admin-pool
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "Account $ACCOUNT_ID / $REGION"

# ── DynamoDB ──────────────────────────────────────────────────────────────────
aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" >/dev/null 2>&1 || \
  aws dynamodb create-table --table-name "$TABLE" --region "$REGION" \
    --attribute-definitions AttributeName=id,AttributeType=S \
    --key-schema AttributeName=id,KeyType=HASH --billing-mode PAY_PER_REQUEST

# ── IAM role (see deploy/iam-policy.json for the full permission set) ─────────
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document '{
    "Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
fi
sed "s/ACCOUNT_ID/$ACCOUNT_ID/g; s/REGION/$REGION/g" "$(dirname "$0")/iam-policy.json" > /tmp/admin-policy.json
aws iam put-role-policy --role-name "$ROLE" --policy-name AgentAdminPerms --policy-document file:///tmp/admin-policy.json

# ── Cognito (admin login) ─────────────────────────────────────────────────────
POOL_ID=$(aws cognito-idp list-user-pools --max-results 60 --region "$REGION" \
  --query "UserPools[?Name=='$POOL_NAME'].Id" --output text)
if [ -z "$POOL_ID" ] || [ "$POOL_ID" = "None" ]; then
  POOL_ID=$(aws cognito-idp create-user-pool --pool-name "$POOL_NAME" --region "$REGION" \
    --policies '{"PasswordPolicy":{"MinimumLength":12,"RequireUppercase":true,"RequireLowercase":true,"RequireNumbers":true,"RequireSymbols":false}}' \
    --admin-create-user-config '{"AllowAdminCreateUserOnly":true}' \
    --deletion-protection ACTIVE --query 'UserPool.Id' --output text)
  CLIENT_ID=$(aws cognito-idp create-user-pool-client --user-pool-id "$POOL_ID" --region "$REGION" \
    --client-name admin-dashboard --no-generate-secret \
    --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
    --access-token-validity 8 --id-token-validity 8 --refresh-token-validity 30 \
    --token-validity-units '{"AccessToken":"hours","IdToken":"hours","RefreshToken":"days"}' \
    --prevent-user-existence-errors ENABLED --query 'UserPoolClient.ClientId' --output text)
  PASSWORD="Adm-$(openssl rand -base64 18 | tr -dc 'a-zA-Z0-9' | head -c 20)"
  aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" --username admin --message-action SUPPRESS --region "$REGION"
  aws cognito-idp admin-set-user-password --user-pool-id "$POOL_ID" --username admin --password "$PASSWORD" --permanent --region "$REGION"
  aws secretsmanager create-secret --region "$REGION" --name agent-admin/dashboard-login \
    --secret-string "{\"username\":\"admin\",\"password\":\"$PASSWORD\",\"poolId\":\"$POOL_ID\",\"clientId\":\"$CLIENT_ID\"}" >/dev/null
  echo "Cognito admin user created; password stored in Secrets Manager: agent-admin/dashboard-login"
else
  CLIENT_ID=$(aws cognito-idp list-user-pool-clients --user-pool-id "$POOL_ID" --region "$REGION" \
    --query 'UserPoolClients[0].ClientId' --output text)
fi

# ── package: vendor boto3 (Lambda's builtin may predate AgentCore harness APIs) ─
BUILD=$(mktemp -d)
pip3 install -q boto3 -t "$BUILD"
sed "s|__UI_HARNESS__|$UI_HARNESS|g; s|__SKILL_REPO_URL__|$SKILL_REPO_URL|g" \
  "$(dirname "$0")/../src/lambda_function.py" > "$BUILD/lambda_function.py"
(cd "$BUILD" && zip -rq /tmp/admin-dashboard.zip .)

ENV_VARS="Variables={REGION=$REGION,UI_HARNESS=$UI_HARNESS,BUGFIX_HARNESS=$BUGFIX_HARNESS,TARGET_REPO=$TARGET_REPO,TARGET_URL=$TARGET_URL,QA_BUCKET=$QA_BUCKET,LOGIN_SECRET_ID=$LOGIN_SECRET_ID,RUNS_TABLE=$TABLE,COGNITO_POOL_ID=$POOL_ID,COGNITO_CLIENT_ID=$CLIENT_ID,SPANS_SINCE=$SPANS_SINCE}"

if aws lambda get-function --function-name "$FN" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FN" --zip-file fileb:///tmp/admin-dashboard.zip --region "$REGION" >/dev/null
  aws lambda wait function-updated --function-name "$FN" --region "$REGION"
  aws lambda update-function-configuration --function-name "$FN" --environment "$ENV_VARS" --region "$REGION" >/dev/null
else
  aws lambda create-function --function-name "$FN" --runtime python3.12 --timeout 300 --memory-size 512 \
    --role "arn:aws:iam::$ACCOUNT_ID:role/$ROLE" --handler lambda_function.handler \
    --zip-file fileb:///tmp/admin-dashboard.zip --environment "$ENV_VARS" --region "$REGION" >/dev/null
fi
aws lambda wait function-active --function-name "$FN" --region "$REGION"

# ── HTTP API Gateway (avoids org guardrails that block public Lambda Function URLs) ─
API_ID=$(aws apigatewayv2 get-apis --region "$REGION" --query "Items[?Name=='$FN-api'].ApiId" --output text)
if [ -z "$API_ID" ] || [ "$API_ID" = "None" ]; then
  API_ID=$(aws apigatewayv2 create-api --name "$FN-api" --protocol-type HTTP --region "$REGION" \
    --target "arn:aws:lambda:$REGION:$ACCOUNT_ID:function:$FN" --query ApiId --output text)
  aws lambda add-permission --function-name "$FN" --statement-id apigw --region "$REGION" \
    --action lambda:InvokeFunction --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:$REGION:$ACCOUNT_ID:$API_ID/*"
fi

echo ""
echo "Dashboard: https://$API_ID.execute-api.$REGION.amazonaws.com/"
echo "Login: username 'admin'; password in Secrets Manager 'agent-admin/dashboard-login'"
