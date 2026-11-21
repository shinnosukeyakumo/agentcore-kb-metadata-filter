"""Gateway（kbmf-gw）経由で Retrieve を呼ぶ。LLM は使わず、ツール引数を自分で書いて送る。

Gateway に渡すトークンは、エージェントと同じく Cognito の token exchange で取る（aud = kbmf-gateway）。
kbmf-gw には interceptor と Policy の両方が付いている。

G0 tools/list       : 3 つのターゲットで、LLM に見える inputSchema。サインインしただけのトークンは 403 になるか
G1 locked           : 何も書かなくても、トークンの department で絞られるか
G2 locked に偽装     : user-b が department=hr の filter や、user-a の userContext を書いて送る
G3 open             : user-b が filter を書く（department=hr / year=2026 / なし）
G4 static（2026 固定）: クライアントが department=hr を書いて送ったらどうなるか
G5 static を 2025 に : 0 件になれば固定値が効いている（終わったら 2026 に戻す）
G6 interceptor のログ: 受け取った引数と、書き換えて送った引数
"""

import importlib
import json
import re
import time

import boto3

from common import (GATEWAY_DONE, REGION, USER_A, USER_B, claims, ctl, gateway_token, load_state, mcp, save_result,
                    sign_in, wait_status)

gwsetup = importlib.import_module("03_gateway")
logs = boto3.client("logs", region_name=REGION)
DOC = re.compile(r"docs/([a-z]+/[a-z0-9-]+)\.txt")
Q = {"hiring": "hiring plan interviews", "pipeline": "Q3 sales pipeline deals",
     "discount": "discount approval percent", "salary": "salary bands for L3 engineer"}
HR = {"equals": {"key": "department", "value": "hr"}}
Y2026 = {"equals": {"key": "year", "value": 2026}}


def call(url, token, target, q, extra=None) -> dict:
    args = {"retrievalQuery": {"text": Q[q]}, **(extra or {})}
    status, j = mcp(url, token, "tools/call", {"name": f"{target}___Retrieve", "arguments": args})
    if "error" in j:
        return {"http": status, "error": j["error"]}
    res = j.get("result") or {}
    text = " ".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
    out = {"docs": sorted(set(DOC.findall(text)))}
    if res.get("isError"):
        out.update(isError=True, text=text[:300])
    return out


def flt(f):
    return {"retrievalConfiguration": {"managedSearchConfiguration": {"filter": f}}}


def set_static_year(gw, kid, year: int) -> None:
    spec = gwsetup.targets(kid)["static"]
    spec["parameterValues"]["retrievalConfiguration"]["managedSearchConfiguration"]["filter"]["equals"]["value"] = year
    tid = gw["targets"]["static"]["id"]
    ctl.update_gateway_target(gatewayIdentifier=gw["id"], targetId=tid, name="static",
                              targetConfiguration=gwsetup.target_config(spec),
                              credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}])
    wait_status(lambda: ctl.get_gateway_target(gatewayIdentifier=gw["id"], targetId=tid), GATEWAY_DONE)
    time.sleep(10)


def main() -> None:
    state = load_state()
    gw, kid, url = state["gateway"], state["kb"]["kb_id"], state["gateway"]["url"]
    start = int(time.time() * 1000)
    ta, tb = gateway_token(state, USER_A), gateway_token(state, USER_B)
    out = {"claims": {u: {k: claims(t).get(k) for k in ("client_id", "aud", "email", "department", "act")}
                      for u, t in (("user-a", ta), ("user-b", tb))}}
    status, j = mcp(url, sign_in(state, USER_A), "tools/list")
    out["G0_sign_in_token"] = {"http": status, "body": j}
    _, j = mcp(url, ta, "tools/list")
    out["G0_tools_list"] = {t["name"]: t.get("inputSchema") for t in (j.get("result") or {}).get("tools", [])}
    out["G1_locked"] = {f"{u}_{q}": call(url, t, "locked", q) for u, t in (("user-a", ta), ("user-b", tb)) for q in Q}
    out["G2_locked_spoof"] = {
        "user-b_hiring_department_hr": call(url, tb, "locked", "hiring", flt(HR)),
        "user-b_hiring_userContext_user-a": call(url, tb, "locked", "hiring", {"userContext": {"userId": USER_A}}),
    }
    out["G3_open"] = {
        "user-b_hiring_department_hr": call(url, tb, "open", "hiring", flt(HR)),
        "user-b_pipeline_year_2026": call(url, tb, "open", "pipeline", flt(Y2026)),
        "user-b_pipeline_no_filter": call(url, tb, "open", "pipeline"),
        "user-a_hiring_no_filter": call(url, ta, "open", "hiring"),
    }
    out["G4_static_2026"] = {
        "user-b_hiring": call(url, tb, "static", "hiring"),
        "user-b_pipeline": call(url, tb, "static", "pipeline"),
        "user-b_pipeline_department_hr_sent": call(url, tb, "static", "pipeline", flt(HR)),
    }
    try:
        set_static_year(gw, kid, 2025)
        out["G5_static_2025"] = {"user-b_pipeline": call(url, tb, "static", "pipeline")}
    finally:
        set_static_year(gw, kid, 2026)
    time.sleep(15)  # ログの到着待ち
    ev = logs.filter_log_events(logGroupName="/aws/lambda/kbmf-interceptor", startTime=start,
                                filterPattern='"KBMF_INTERCEPTOR"').get("events", [])
    out["G6_interceptor_log"] = [json.loads(e["message"][e["message"].index("{"):]) for e in ev]
    path = save_result("11_gateway", out)
    print(json.dumps({k: v for k, v in out.items() if k not in ("G0_tools_list", "G6_interceptor_log")},
                     indent=1, ensure_ascii=False))
    print("G0 tools:", sorted(out["G0_tools_list"]), "/ G6 log entries:", len(out["G6_interceptor_log"]))
    print(f"saved {path}")


if __name__ == "__main__":
    main()
