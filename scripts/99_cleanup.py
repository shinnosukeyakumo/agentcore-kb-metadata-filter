"""作ったリソースをすべて消す。

.state.json が無くても消せるように、名前の頭（kbmf）で探す。
消す順番: Runtime → credential provider → Gateway（ターゲット → 本体）→ Policy（ポリシー → engine）
          → KB（データソース → 本体）→ S3（バケットポリシー → 中身 → 本体）→ Lambda
          → Cognito（ドメイン → ユーザープール）→ IAM ロール → CloudWatch Logs
"""

import time

import boto3

from common import ACCOUNT, PREFIX, REGION, STATE_PATH, ctl, iam, idp, lam

agent = boto3.client("bedrock-agent", region_name=REGION)
s3 = boto3.resource("s3", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)


def gone(check, tries=60, sleep=5) -> bool:
    for _ in range(tries):
        if not check():
            return True
        time.sleep(sleep)
    return False


def runtimes() -> None:
    for r in ctl.list_agent_runtimes(maxResults=100).get("agentRuntimes", []):
        if r["agentRuntimeName"].startswith(f"{PREFIX}_"):
            ctl.delete_agent_runtime(agentRuntimeId=r["agentRuntimeId"])
            rid = r["agentRuntimeId"]
            ok = gone(lambda: any(x["agentRuntimeId"] == rid
                                  for x in ctl.list_agent_runtimes(maxResults=100).get("agentRuntimes", [])))
            print("deleted runtime", r["agentRuntimeName"], ok)


def credential_providers() -> None:
    for p in ctl.list_oauth2_credential_providers(maxResults=20).get("credentialProviders", []):
        if p["name"].startswith(f"{PREFIX}-"):
            ctl.delete_oauth2_credential_provider(name=p["name"])
            print("deleted credential provider", p["name"])


def gateways() -> None:
    for g in ctl.list_gateways(maxResults=100)["items"]:
        if not g["name"].startswith(f"{PREFIX}-"):
            continue
        gid = g["gatewayId"]
        for t in ctl.list_gateway_targets(gatewayIdentifier=gid)["items"]:
            ctl.delete_gateway_target(gatewayIdentifier=gid, targetId=t["targetId"])
        gone(lambda: ctl.list_gateway_targets(gatewayIdentifier=gid)["items"])
        ctl.delete_gateway(gatewayIdentifier=gid)
        ok = gone(lambda: any(x["gatewayId"] == gid for x in ctl.list_gateways(maxResults=100)["items"]))
        print("deleted gateway", g["name"], ok)


def policies() -> None:
    for pe in ctl.list_policy_engines(maxResults=100).get("policyEngines", []):
        if not pe["name"].startswith(f"{PREFIX}_"):
            continue
        pid = pe["policyEngineId"]
        for p in ctl.list_policies(policyEngineId=pid).get("policies", []):
            ctl.delete_policy(policyEngineId=pid, policyId=p["policyId"])
        gone(lambda: ctl.list_policies(policyEngineId=pid).get("policies", []))
        ctl.delete_policy_engine(policyEngineId=pid)
        ok = gone(lambda: any(x["policyEngineId"] == pid
                              for x in ctl.list_policy_engines(maxResults=100).get("policyEngines", [])))
        print("deleted policy engine", pe["name"], ok)


def knowledge_bases() -> None:
    for kb in agent.list_knowledge_bases(maxResults=100)["knowledgeBaseSummaries"]:
        if not kb["name"].startswith(f"{PREFIX}-"):
            continue
        kid = kb["knowledgeBaseId"]
        for ds in agent.list_data_sources(knowledgeBaseId=kid)["dataSourceSummaries"]:
            agent.delete_data_source(knowledgeBaseId=kid, dataSourceId=ds["dataSourceId"])
        gone(lambda: agent.list_data_sources(knowledgeBaseId=kid)["dataSourceSummaries"], tries=120)
        agent.delete_knowledge_base(knowledgeBaseId=kid)
        ok = gone(lambda: any(x["knowledgeBaseId"] == kid for x in
                              agent.list_knowledge_bases(maxResults=100)["knowledgeBaseSummaries"]), tries=120)
        print("deleted knowledge base", kb["name"], ok)


def buckets() -> None:
    for name in (f"{PREFIX}-docs-{ACCOUNT}", f"{PREFIX}-code-{ACCOUNT}"):
        b = s3.Bucket(name)
        try:
            b.Policy().delete()  # meta/ と acl/ の Deny を外してから中身を消す
            b.object_versions.delete()
            b.objects.all().delete()
            b.delete()
            print("deleted bucket", name)
        except s3.meta.client.exceptions.NoSuchBucket:
            pass


def functions() -> None:
    for fn in (f"{PREFIX}-pretoken", f"{PREFIX}-interceptor"):
        try:
            lam.delete_function(FunctionName=fn)
            print("deleted function", fn)
        except lam.exceptions.ResourceNotFoundException:
            pass


def user_pools() -> None:
    for page in idp.get_paginator("list_user_pools").paginate(MaxResults=60):
        for p in page["UserPools"]:
            if p["Name"] == f"{PREFIX}-pool":
                domain = idp.describe_user_pool(UserPoolId=p["Id"])["UserPool"].get("Domain")
                if domain:  # ドメインが残っているとユーザープールを消せない
                    idp.delete_user_pool_domain(Domain=domain, UserPoolId=p["Id"])
                    print("deleted user pool domain", domain)
                idp.delete_user_pool(UserPoolId=p["Id"])
                print("deleted user pool", p["Name"])


def roles() -> None:
    for name in (f"{PREFIX}-kb-role", f"{PREFIX}-gateway-role", f"{PREFIX}-runtime-role", f"{PREFIX}-lambda-role",
                 f"{PREFIX}-doc-writer", f"{PREFIX}-meta-admin"):
        try:
            for p in iam.list_role_policies(RoleName=name)["PolicyNames"]:
                iam.delete_role_policy(RoleName=name, PolicyName=p)
            for p in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
                iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
            iam.delete_role(RoleName=name)
            print("deleted role", name)
        except iam.exceptions.NoSuchEntityException:
            pass


def log_groups() -> None:
    for prefix in (f"/aws/lambda/{PREFIX}-", f"/aws/bedrock-agentcore/runtimes/{PREFIX}_"):
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
            for g in page["logGroups"]:
                logs.delete_log_group(logGroupName=g["logGroupName"])
                print("deleted log group", g["logGroupName"])


def main() -> None:
    # Gateway や Knowledge Base は、削除のときにも実行ロールを使う。
    # 先にロールや S3 を消すと削除が FAILED で止まるので、前半が失敗したら後半には進まない
    failed = False
    for step in (runtimes, credential_providers, gateways, policies, knowledge_bases):
        try:
            step()
        except Exception as e:
            failed = True
            print(f"{step.__name__} failed: {type(e).__name__}: {e}")
    if failed:
        print("AgentCore / Knowledge Base の削除に失敗したので、ここで止めます。もう一度実行してください")
        return
    for step in (buckets, functions, user_pools, roles, log_groups):
        try:
            step()
        except Exception as e:  # ここから先は互いに依存しないので、1 つ失敗しても残りは消す
            print(f"{step.__name__} failed: {type(e).__name__}: {e}")
    if STATE_PATH.exists():
        STATE_PATH.unlink()
        print("deleted .state.json")


if __name__ == "__main__":
    main()
