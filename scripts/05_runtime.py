"""AgentCore Runtime（kbmf_agent）に agent/main.py を CodeZip で置く。

- S3: kbmf-code-<アカウント ID>（arm64 の依存を入れた zip）
- AgentCore Identity: OAuth2 credential provider kbmf-obo（Cognito の agent クライアント、On-Behalf-Of の設定つき）
  エージェントはこれを使って、ユーザーのトークンを Gateway 用のトークン（aud = kbmf-gateway）に交換する
- IAM: kbmf-runtime-role（ログ、ワークロードトークン、トークン交換、jp. の推論プロファイルで Sonnet 4.6）
- inbound: CUSTOM_JWT（Cognito、allowedClients=[web]）。サインインしたユーザーのトークンで呼ぶ
"""

import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import boto3

from common import (ACCOUNT, GATEWAY_AUDIENCE, GATEWAY_SCOPE, PREFIX, REGION, ROOT, ctl, ensure_role, load_state,
                    save_state)

s3 = boto3.client("s3", region_name=REGION)

SRC = ROOT / "agent"
BUILD = SRC / ".build"
BUCKET = f"{PREFIX}-code-{ACCOUNT}"
NAME = f"{PREFIX}_agent"  # Runtime の名前にハイフンは使えない
KEY = f"{NAME}/code.zip"
MODEL = "anthropic.claude-sonnet-4-6"
PROVIDER = f"{PREFIX}-obo"


def ensure_provider(state: dict) -> None:
    try:
        ctl.get_oauth2_credential_provider(name=PROVIDER)
        return
    except ctl.exceptions.ResourceNotFoundException:
        pass
    agent = state["cognito"]["agent"]
    ctl.create_oauth2_credential_provider(
        name=PROVIDER, credentialProviderVendor="CustomOauth2",
        oauth2ProviderConfigInput={"customOauth2ProviderConfig": {
            "oauthDiscovery": {"discoveryUrl": f"{state['cognito']['issuer']}/.well-known/openid-configuration"},
            "clientId": agent["id"], "clientSecret": agent["secret"],
            "clientAuthenticationMethod": "CLIENT_SECRET_BASIC",
            # actor_token に、agent クライアントの client_credentials で取ったトークンを付ける
            "onBehalfOfTokenExchangeConfig": {"grantType": "TOKEN_EXCHANGE", "tokenExchangeGrantTypeConfig": {
                "actorTokenContent": "M2M", "actorTokenScopes": [GATEWAY_SCOPE]}}}})
    print(f"created credential provider {PROVIDER}")


def build_zip() -> Path:
    shutil.rmtree(BUILD, ignore_errors=True)
    pkg = BUILD / "pkg"
    subprocess.run(["uv", "pip", "install", "--quiet", "--target", str(pkg), "--python-platform", "aarch64-manylinux2014",
                    "--python-version", "3.12", "--only-binary=:all:", "-r", str(SRC / "requirements.txt")], check=True)
    shutil.copy(SRC / "main.py", pkg / "main.py")
    out = BUILD / "code.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(pkg.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                z.write(p, p.relative_to(pkg))
    print("zip", out.stat().st_size // 1024, "KiB")
    return out


def upload(zip_path: Path) -> None:
    try:
        s3.head_bucket(Bucket=BUCKET)
    except s3.exceptions.ClientError:
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.put_public_access_block(Bucket=BUCKET, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        print(f"created bucket {BUCKET}")
    s3.upload_file(str(zip_path), BUCKET, KEY)


def runtime_role() -> str:
    wid = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:workload-identity-directory/default"
    vault = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:token-vault/default"
    return ensure_role(f"{PREFIX}-runtime-role", "bedrock-agentcore.amazonaws.com", {
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Resource": "*", "Action": [
                "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams",
                "logs:DescribeLogGroups", "xray:PutTraceSegments", "xray:PutTelemetryRecords",
                "cloudwatch:PutMetricData"]},
            {"Effect": "Allow", "Resource": [wid, f"{wid}/workload-identity/*", vault,
                                             f"{vault}/oauth2credentialprovider/*"], "Action": [
                "bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                "bedrock-agentcore:GetWorkloadAccessTokenForUserId", "bedrock-agentcore:GetResourceOauth2Token"]},
            # credential provider の client secret は Identity が Secrets Manager に置く
            {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
             "Resource": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:bedrock-agentcore-identity!*"},
            # jp. の推論プロファイルは東京と大阪に振り分けるので、両方の基盤モデルを許可する
            {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
             "Resource": [f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/jp.{MODEL}",
                          f"arn:aws:bedrock:ap-northeast-1::foundation-model/{MODEL}*",
                          f"arn:aws:bedrock:ap-northeast-3::foundation-model/{MODEL}*"]}]})


def ensure_runtime(state: dict, role_arn: str) -> dict:
    kw = dict(
        agentRuntimeArtifact={"codeConfiguration": {
            "code": {"s3": {"bucket": BUCKET, "prefix": KEY}}, "runtime": "PYTHON_3_12", "entryPoint": ["main.py"]}},
        roleArn=role_arn, networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "HTTP"},
        authorizerConfiguration={"customJWTAuthorizer": {
            "discoveryUrl": f"{state['cognito']['issuer']}/.well-known/openid-configuration",
            "allowedClients": [state["cognito"]["client_id"]]}},
        environmentVariables={"AWS_REGION": REGION, "OBO_PROVIDER": PROVIDER, "GATEWAY_AUDIENCE": GATEWAY_AUDIENCE})
    rt = next((r for r in ctl.list_agent_runtimes().get("agentRuntimes", []) if r["agentRuntimeName"] == NAME), None)
    if rt:
        rid = rt["agentRuntimeId"]
        ctl.update_agent_runtime(agentRuntimeId=rid, **kw)
        print(f"updated runtime {NAME}")
    else:
        for _ in range(6):
            try:
                rid = ctl.create_agent_runtime(agentRuntimeName=NAME, **kw)["agentRuntimeId"]
                break
            except ctl.exceptions.ValidationException as e:
                print("retry create_agent_runtime:", str(e)[:200])  # ロールの伝播待ち
                time.sleep(10)
        print(f"created runtime {NAME}")
    for _ in range(120):
        r = ctl.get_agent_runtime(agentRuntimeId=rid)
        if r["status"] in ("READY", "CREATE_FAILED", "UPDATE_FAILED"):
            break
        time.sleep(5)
    print("runtime", r["status"], r.get("failureReason", ""))
    return {"id": rid, "arn": r["agentRuntimeArn"], "status": r["status"]}


def main() -> None:
    state = load_state()
    ensure_provider(state)
    upload(build_zip())
    state["runtime"] = {**ensure_runtime(state, runtime_role()), "bucket": BUCKET}
    save_state(state)


if __name__ == "__main__":
    main()
