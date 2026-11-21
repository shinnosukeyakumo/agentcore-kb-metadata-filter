"""AgentCore Gateway（kbmf-gw）を用意する。

- 認証: CUSTOM_JWT。aud が kbmf-gateway のトークンだけを受け付ける（allowedAudience）
  サインインしただけのトークンには aud が無いので通らない。エージェントがトークン交換で受け取ったものだけが通る
- REQUEST interceptor: lambdas/interceptor.py。Retrieve の userContext と filter をトークンのクレームで入れ直す
- Policy engine（kbmf_pe）を ENFORCE で付ける。Cedar は interceptor の後で評価される
  ポリシーは 04_policy.py で入れる。入れるまでは、Cedar の既定どおりすべての呼び出しが拒否される
- ターゲットは 3 つ。どれも同じ KB の Retrieve で、LLM に見せる引数だけを変える
    locked : filter は見えるが、説明は「無視される」。interceptor が department の条件に置き換える
    open   : filter を LLM が書ける。interceptor が department の条件を andAll で重ねる
    static : filter を parameterValues に固定（year = 2026）。interceptor は filter を触らない

visible: false にした引数は interceptor が書いても "cannot set parameter(s)" で弾かれる。
そのため interceptor が書く引数は visible: true のまま、値を必ず上書きする。
"""

import json
import time

from common import (ACCOUNT, GATEWAY_AUDIENCE, GATEWAY_DONE, PREFIX, REGION, ROOT, ctl, ensure_function, ensure_role,
                    load_state, save_state, wait_status)

GW_NAME = f"{PREFIX}-gw"
FN = f"{PREFIX}-interceptor"
ROLE = f"{PREFIX}-gateway-role"
PE_NAME = f"{PREFIX}_pe"  # Policy engine とポリシーの名前にハイフンは使えない

QUERY = {"path": "$.retrievalQuery.text", "description": "The search query.", "visible": True}
USERCTX = {"path": "$.userContext", "description": "Ignored. The gateway sets this from your token.", "visible": True}
FILTER = "$.retrievalConfiguration.managedSearchConfiguration.filter"


def targets(kid: str) -> dict:
    return {
        "locked": {"parameterValues": {"knowledgeBaseId": kid},
                   "parameterOverrides": [QUERY, USERCTX, {
                       "path": FILTER, "visible": True,
                       "description": "Ignored. The gateway sets this from your token."}]},
        "open": {"parameterValues": {"knowledgeBaseId": kid},
                 "parameterOverrides": [QUERY, USERCTX, {
                     "path": FILTER, "visible": True,
                     "description": "Metadata filter. Keys: department (hr, sales), year (number)."}]},
        "static": {"parameterValues": {"knowledgeBaseId": kid, "retrievalConfiguration": {"managedSearchConfiguration": {
                       "filter": {"equals": {"key": "year", "value": 2026}}}}},
                   "parameterOverrides": [QUERY, USERCTX]},
    }


def target_config(spec: dict) -> dict:
    return {"mcp": {"connector": {"source": {"connectorId": "bedrock-knowledge-bases", "version": "1.0.0"},
                                  "configurations": [{"name": "Retrieve", **spec}]}}}


def gateway_role(kid: str, fn_arn: str) -> str:
    arn = ensure_role(ROLE, "bedrock-agentcore.amazonaws.com", {
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["bedrock:GetKnowledgeBase", "bedrock:Retrieve"],
             "Resource": f"arn:aws:bedrock:{REGION}:{ACCOUNT}:knowledge-base/{kid}"},
            {"Effect": "Allow", "Action": ["bedrock:AgenticRetrieveStream"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": fn_arn},
            {"Effect": "Allow", "Action": [
                "bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                "bedrock-agentcore:GetWorkloadAccessTokenForUserId"], "Resource": "*"},
            # Policy engine を付けた Gateway に必要。ドキュメントに書かれていないが無いと動かない
            {"Effect": "Allow", "Action": [
                "bedrock-agentcore:GetPolicyEngine", "bedrock-agentcore:AuthorizeAction",
                "bedrock-agentcore:PartiallyAuthorizeActions"], "Resource": "*"}]},
        source_account=False)
    time.sleep(15)  # inline ポリシーの伝播待ち
    return arn


def authorizer(state: dict) -> dict:
    return {"customJWTAuthorizer": {
        "discoveryUrl": f"{state['cognito']['issuer']}/.well-known/openid-configuration",
        "allowedAudience": [GATEWAY_AUDIENCE]}}


def ensure_engine() -> dict:
    pe = next((p for p in ctl.list_policy_engines().get("policyEngines", []) if p["name"] == PE_NAME), None)
    if not pe:
        pe = ctl.create_policy_engine(name=PE_NAME, description="kbmf: check userContext against the token")
        print(f"created policy engine {PE_NAME}")
    for _ in range(60):
        pe = ctl.get_policy_engine(policyEngineId=pe["policyEngineId"])
        if pe["status"] in ("ACTIVE", "CREATE_FAILED"):
            break
        time.sleep(3)
    print("policy engine", pe["status"])
    return pe


def ensure_target(gid: str, name: str, config: dict) -> dict:
    t = next((t for t in ctl.list_gateway_targets(gatewayIdentifier=gid)["items"] if t["name"] == name), None)
    tid = t["targetId"] if t else None
    if not tid:
        for _ in range(8):
            try:
                tid = ctl.create_gateway_target(
                    gatewayIdentifier=gid, name=name, targetConfiguration=config,
                    credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}])["targetId"]
                break
            except ctl.exceptions.ValidationException as e:
                print(f"retry create_gateway_target {name}:", str(e)[:200])  # ロールの伝播待ち
                time.sleep(15)
    tt = wait_status(lambda: ctl.get_gateway_target(gatewayIdentifier=gid, targetId=tid), GATEWAY_DONE)
    print(f"target {name}", tt["status"], tt.get("statusReasons") or "")
    return {"id": tid, "status": tt["status"]}


def main() -> None:
    state = load_state()
    kid = state["kb"]["kb_id"]
    fn_arn = ensure_function(FN, ROOT / "lambdas" / "interceptor.py")
    role_arn = gateway_role(kid, fn_arn)
    pe = ensure_engine()
    gw = next((g for g in ctl.list_gateways()["items"] if g["name"] == GW_NAME), None)
    if not gw:
        gw = ctl.create_gateway(
            name=GW_NAME, roleArn=role_arn, protocolType="MCP",
            protocolConfiguration={"mcp": {"supportedVersions": ["2025-11-25"]}},
            authorizerType="CUSTOM_JWT", authorizerConfiguration=authorizer(state),
            interceptorConfigurations=[{
                "interceptor": {"lambda": {"arn": fn_arn}}, "interceptionPoints": ["REQUEST"],
                "inputConfiguration": {"passRequestHeaders": True}}],
            policyEngineConfiguration={"arn": pe["policyEngineArn"], "mode": "ENFORCE"},
            exceptionLevel="DEBUG")
        print(f"created gateway {GW_NAME}")
    gid = gw["gatewayId"]
    g = wait_status(lambda: ctl.get_gateway(gatewayIdentifier=gid), GATEWAY_DONE)
    print("gateway", g["status"])
    out = {name: ensure_target(gid, name, target_config(spec)) for name, spec in targets(kid).items()}
    state["gateway"] = {"id": gid, "arn": g["gatewayArn"], "url": g["gatewayUrl"], "role_arn": role_arn,
                        "engine_id": pe["policyEngineId"], "targets": out}
    save_state(state)


if __name__ == "__main__":
    main()
