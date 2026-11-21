"""AgentCore Policy（Cedar）で userContext の偽装を止める構成を用意する。

interceptor を使わない別の Gateway（kbmf-gw-policy）を作り、Policy engine を ENFORCE で付ける。
ターゲット kb は userContext も filter も LLM に見せる（書き換える仕組みが無いので、Cedar で検査する）。

ポリシーは 2 本。
- permit_retrieve : Retrieve を許可する（Cedar は既定で拒否なので、permit が 1 本は必要）
- forbid_mismatch : userContext.userId がトークンの email と一致しなければ拒否する
"""

import importlib
import time

from common import GATEWAY_DONE, PREFIX, ctl, load_state, save_state, wait_status

gwsetup = importlib.import_module("03_gateway")

GW_NAME = f"{PREFIX}-gw-policy"
PE_NAME = f"{PREFIX}_pe"  # Policy engine とポリシーの名前にハイフンは使えない
TARGET = "kb"


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


def statements(gw_arn: str) -> dict:
    head = f'action == AgentCore::Action::"{TARGET}___Retrieve", resource == AgentCore::Gateway::"{gw_arn}"'
    return {
        "permit_retrieve": f"permit (principal, {head});",
        "forbid_mismatch": (
            f'forbid (principal, {head}) unless {{ principal.hasTag("email") && '
            f'context.input has userContext && context.input.userContext has userId && '
            f'context.input.userContext.userId == principal.getTag("email") }};'),
    }


def ensure_policies(pe_id: str, gw_arn: str) -> dict:
    existing = {p["name"]: p for p in ctl.list_policies(policyEngineId=pe_id).get("policies", [])}
    out = {}
    for name, stmt in statements(gw_arn).items():
        p = existing.get(name) or ctl.create_policy(
            policyEngineId=pe_id, name=name, definition={"cedar": {"statement": stmt}},
            # 既定の検証だと、スキーマに無い context.input の参照で permit まで弾かれる
            validationMode="IGNORE_ALL_FINDINGS")
        for _ in range(30):
            p = ctl.get_policy(policyEngineId=pe_id, policyId=p["policyId"])
            if p["status"] in ("ACTIVE", "CREATE_FAILED"):
                break
            time.sleep(2)
        print(f"policy {name}", p["status"], p.get("statusReasons") or "")
        out[name] = {"id": p["policyId"], "status": p["status"]}
    return out


def main() -> None:
    state = load_state()
    kid = state["kb"]["kb_id"]
    pe = ensure_engine()
    gw = next((g for g in ctl.list_gateways()["items"] if g["name"] == GW_NAME), None)
    if not gw:
        gw = ctl.create_gateway(
            name=GW_NAME, roleArn=state["gateway"]["role_arn"], protocolType="MCP",
            protocolConfiguration={"mcp": {"supportedVersions": ["2025-11-25"]}},
            authorizerType="CUSTOM_JWT", authorizerConfiguration=gwsetup.authorizer(state),
            policyEngineConfiguration={"arn": pe["policyEngineArn"], "mode": "ENFORCE"},
            exceptionLevel="DEBUG")
        print(f"created gateway {GW_NAME}")
    gid = gw["gatewayId"]
    g = wait_status(lambda: ctl.get_gateway(gatewayIdentifier=gid), GATEWAY_DONE)
    print("gateway", g["status"])
    spec = {"parameterValues": {"knowledgeBaseId": kid},
            "parameterOverrides": [
                gwsetup.QUERY,
                {"path": "$.userContext", "description": "The user to search as.", "visible": True},
                {"path": gwsetup.FILTER, "visible": True,
                 "description": "Metadata filter. Keys: department (hr, sales), year (number)."}]}
    target = gwsetup.ensure_target(gid, TARGET, gwsetup.target_config(spec))
    policies = ensure_policies(pe["policyEngineId"], g["gatewayArn"])
    state["policy"] = {"gateway_id": gid, "url": g["gatewayUrl"], "engine_id": pe["policyEngineId"],
                       "target": target, "policies": policies}
    save_state(state)


if __name__ == "__main__":
    main()
