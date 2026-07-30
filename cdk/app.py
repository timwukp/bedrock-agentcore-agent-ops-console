#!/usr/bin/env python3
"""CDK app for the Agent CI/CD Admin dashboard.

Configure via cdk.json context or environment variables (see README), then:
    cd cdk && pip install -r requirements.txt && cdk deploy
"""
import os

import aws_cdk as cdk

from stack import AdminDashboardStack

app = cdk.App()

AdminDashboardStack(
    app,
    "AgentCicdAdmin",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
)

# ABAC isolation tag (Security pillar SEC05): every resource in this app belongs
# to the token-monitor system. The matching Permission Boundary on the Lambda
# execution role denies lambda:UpdateFunction* / iam:*RolePolicy on resources
# tagged with a different system value — blocking cross-system contamination.
cdk.Tags.of(app).add("system", "token-monitor")

app.synth()
