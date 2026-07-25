"""Agent CI/CD Admin dashboard — single-stack CDK definition.

Resources: Lambda (dashboard + API), HTTP API Gateway, DynamoDB table,
Cognito user pool + admin user, IAM role with the field-verified permission set.
"""
import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as integrations,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parent.parent


class AdminDashboardStack(cdk.Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)

        # ── configuration (context -> env var -> empty) ──────────────────────
        def cfg(key: str, default: str = "") -> str:
            return self.node.try_get_context(key) or default

        ui_harness = cfg("uiHarness")
        if not ui_harness:
            raise ValueError("Set context uiHarness (cdk deploy -c uiHarness=MyHarness-AbC123)")

        # ── DynamoDB: fan-out batches + drafted recommendations ──────────────
        table = dynamodb.Table(
            self, "Runs",
            table_name="AgentAdminRuns",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ── Cognito: admin-only login ─────────────────────────────────────────
        pool = cognito.UserPool(
            self, "AdminPool",
            user_pool_name="agent-admin-pool",
            self_sign_up_enabled=False,
            password_policy=cognito.PasswordPolicy(
                min_length=12, require_uppercase=True,
                require_lowercase=True, require_digits=True, require_symbols=False,
            ),
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        client = pool.add_client(
            "Dashboard",
            user_pool_client_name="admin-dashboard",
            auth_flows=cognito.AuthFlow(user_password=True),
            access_token_validity=cdk.Duration.hours(8),
            id_token_validity=cdk.Duration.hours(8),
            refresh_token_validity=cdk.Duration.days(30),
            prevent_user_existence_errors=True,
        )
        # admin user + generated password stored in Secrets Manager
        login_secret = secretsmanager.Secret(
            self, "AdminLogin",
            secret_name="agent-admin/dashboard-login",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({"username": "admin"}),
                generate_string_key="password",
                password_length=20,
                exclude_punctuation=True,
            ),
        )
        cognito.CfnUserPoolUser(
            self, "AdminUser",
            user_pool_id=pool.user_pool_id,
            username="admin",
            message_action="SUPPRESS",
        )
        # NOTE: CFN cannot set a permanent password; run once after deploy:
        #   aws cognito-idp admin-set-user-password --user-pool-id <pool> --username admin \
        #     --password "$(aws secretsmanager get-secret-value --secret-id agent-admin/dashboard-login \
        #        --query SecretString --output text | python3 -c 'import sys,json;print(json.load(sys.stdin)["password"])')" --permanent

        # ── Lambda: single function serves HTML + JSON API ────────────────────
        fn = lambda_.Function(
            self, "Dashboard",
            function_name="agent-cicd-admin",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="lambda_function.handler",
            timeout=cdk.Duration.minutes(5),
            memory_size=512,
            # bundle vendored boto3 (Lambda's builtin may predate AgentCore harness APIs)
            code=lambda_.Code.from_asset(
                str(REPO_ROOT / "src"),
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash", "-c",
                        "pip install boto3 -t /asset-output && "
                        f"sed 's|__UI_HARNESS__|{ui_harness}|g; s|__SKILL_REPO_URL__|{cfg('skillRepoUrl')}|g' "
                        "lambda_function.py > /asset-output/lambda_function.py",
                    ],
                ),
            ),
            environment={
                "UI_HARNESS": ui_harness,
                "BUGFIX_HARNESS": cfg("bugfixHarness"),
                "TARGET_REPO": cfg("targetRepo"),
                "TARGET_URL": cfg("targetUrl"),
                "QA_BUCKET": cfg("qaBucket"),
                "LOGIN_SECRET_ID": cfg("loginSecretId"),
                "RUNS_TABLE": table.table_name,
                "COGNITO_POOL_ID": pool.user_pool_id,
                "COGNITO_CLIENT_ID": client.user_pool_client_id,
                "SPANS_SINCE": cfg("spansSince"),
            },
        )

        # ── IAM: the field-verified permission set (see deploy/iam-policy.json) ─
        policy = json.loads((REPO_ROOT / "deploy" / "iam-policy.json").read_text())
        for stmt in policy["Statement"]:
            resources = stmt["Resource"] if isinstance(stmt["Resource"], list) else [stmt["Resource"]]
            fn.add_to_role_policy(iam.PolicyStatement(
                sid=stmt.get("Sid"),
                actions=stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]],
                resources=[r.replace("ACCOUNT_ID", self.account).replace("REGION", self.region)
                           for r in resources],
            ))
        table.grant_read_write_data(fn)
        login_secret.grant_read(fn)

        # ── HTTP API Gateway (not a Function URL: org guardrails may block those) ─
        api = apigwv2.HttpApi(
            self, "Api",
            api_name="agent-cicd-admin-api",
            default_integration=integrations.HttpLambdaIntegration("Fn", fn),
        )

        cdk.CfnOutput(self, "DashboardUrl", value=api.api_endpoint)
        cdk.CfnOutput(self, "CognitoPoolId", value=pool.user_pool_id)
        cdk.CfnOutput(self, "LoginSecret", value=login_secret.secret_name)
