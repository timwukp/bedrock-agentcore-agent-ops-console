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

app.synth()
