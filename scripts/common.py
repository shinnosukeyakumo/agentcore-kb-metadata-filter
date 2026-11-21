"""各スクリプトで共有する設定と小さな道具。

作ったリソースの ID やユーザーのパスワードは、リポジトリ直下の .state.json に保存する（.gitignore 済み）。
トークン本体は表示もファイル保存もしない。decode したクレームだけを扱う。
"""

import base64
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import boto3

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
PREFIX = "kbmf"  # 作るリソースの名前の頭。cleanup はこの名前で消す

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / ".state.json"
RESULTS = ROOT / "results"

USER_A = "user-a@example.com"  # 人事部（hr）
USER_B = "user-b@example.com"  # 営業部（sales）
DEPARTMENTS = {USER_A: "hr", USER_B: "sales"}

# Gateway 向けのトークンの aud。Cognito の resource server の識別子でもある
GATEWAY_AUDIENCE = f"{PREFIX}-gateway"
GATEWAY_SCOPE = f"{GATEWAY_AUDIENCE}/kb.read"
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

sts = boto3.client("sts", region_name=REGION)
iam = boto3.client("iam", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
idp = boto3.client("cognito-idp", region_name=REGION)
ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
ACCOUNT = sts.get_caller_identity()["Account"]


def load_state() -> dict:
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    STATE_PATH.chmod(0o600)


def save_result(name: str, data: dict) -> Path:
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    return path


def claims(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def sign_in(state: dict, email: str) -> str:
    """ユーザーとしてサインインし、アクセストークンを返す（ADMIN_USER_PASSWORD_AUTH）。"""
    r = idp.admin_initiate_auth(
        UserPoolId=state["cognito"]["pool_id"],
        ClientId=state["cognito"]["client_id"],
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": state["cognito"]["passwords"][email]},
    )
    return r["AuthenticationResult"]["AccessToken"]


def token_endpoint(state: dict, form: dict) -> dict:
    """Cognito の token endpoint に agent クライアント（client_secret_basic）で POST する。"""
    agent = state["cognito"]["agent"]
    basic = base64.b64encode(f"{agent['id']}:{agent['secret']}".encode()).decode()
    req = urllib.request.Request(state["cognito"]["token_endpoint"], method="POST",
                                 data=urllib.parse.urlencode(form).encode(), headers={
                                     "Content-Type": "application/x-www-form-urlencoded",
                                     "Authorization": f"Basic {basic}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"token endpoint {e.code}: {e.read().decode()[:300]}") from None


def gateway_token(state: dict, email: str) -> str:
    """ユーザーのトークンを、Gateway 向け（aud = kbmf-gateway）に交換して返す。

    Runtime の中では AgentCore Identity が同じ交換をする（agent/main.py）。
    テストは Runtime を通さずに Gateway を呼ぶので、Cognito の token exchange を直接呼ぶ。
    actor token は agent クライアントの client_credentials で取る（委任になり、act クレームが付く）。
    """
    actor = token_endpoint(state, {"grant_type": "client_credentials", "scope": GATEWAY_SCOPE})["access_token"]
    return token_endpoint(state, {
        "grant_type": TOKEN_EXCHANGE, "subject_token": sign_in(state, email),
        "subject_token_type": ACCESS_TOKEN_TYPE, "audience": GATEWAY_AUDIENCE,
        "actor_token": actor, "actor_token_type": ACCESS_TOKEN_TYPE})["access_token"]


def mcp(url: str, token: str, method: str, params: dict | None = None) -> tuple[int, dict]:
    """Gateway の MCP エンドポイントに JSON-RPC を 1 回送る。SSE で返ってきたら data: 行をつなぐ。"""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
        "Authorization": f"Bearer {token}",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode()
    if "data:" in raw and not raw.lstrip().startswith("{"):
        raw = "\n".join(line[5:].strip() for line in raw.splitlines() if line.startswith("data:"))
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, {"raw": raw[:300]}


def ensure_role(name: str, service: str, inline: dict | None = None, managed: tuple = (),
                source_account: bool = True) -> str:
    stmt = {"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}
    if source_account:
        stmt["Condition"] = {"StringEquals": {"aws:SourceAccount": ACCOUNT}}
    trust = {"Version": "2012-10-17", "Statement": [stmt]}
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
        print(f"created role {name}")
        time.sleep(10)  # IAM の伝播待ち
    for m in managed:
        iam.attach_role_policy(RoleName=name, PolicyArn=m)
    if inline:
        iam.put_role_policy(RoleName=name, PolicyName="inline", PolicyDocument=json.dumps(inline))
    return arn


def lambda_role() -> str:
    # Lambda のロールは信頼ポリシーに SourceAccount 条件を付けない（lambda.amazonaws.com からの AssumeRole に付かないため）
    name = f"{PREFIX}-lambda-role"
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    try:
        return iam.get_role(RoleName=name)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
        iam.attach_role_policy(RoleName=name,
                               PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
        print(f"created role {name}")
        time.sleep(10)
        return arn


def ensure_function(name: str, src: Path) -> str:
    """1 ファイルの Lambda（python3.13、index.handler）を作る。既にあればコードだけ更新する。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.write(src, "index.py")
    code = buf.getvalue()
    try:
        arn = lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]
        lam.update_function_code(FunctionName=name, ZipFile=code)
    except lam.exceptions.ResourceNotFoundException:
        role = lambda_role()
        for _ in range(6):
            try:
                arn = lam.create_function(FunctionName=name, Runtime="python3.13", Role=role,
                                          Handler="index.handler", Code={"ZipFile": code}, Timeout=10)["FunctionArn"]
                break
            except lam.exceptions.InvalidParameterValueException:
                time.sleep(10)  # ロールの伝播待ち
        print(f"created function {name}")
    lam.get_waiter("function_active_v2").wait(FunctionName=name)
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    return arn


def wait_status(get, done: tuple, tries: int = 120, sleep: int = 5) -> dict:
    for _ in range(tries):
        r = get()
        if r["status"] in done:
            return r
        time.sleep(sleep)
    raise TimeoutError(f"status stayed {r['status']}")


GATEWAY_DONE = ("READY", "FAILED", "UPDATE_UNSUCCESSFUL", "CREATE_UNSUCCESSFUL")
