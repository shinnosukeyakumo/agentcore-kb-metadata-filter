"""Runtime 上のエージェント（LLM）に Gateway のツールを渡して、user-b として頼みごとをする。

Runtime はサインインしたユーザーのトークンで呼ぶ。エージェントが中で On-Behalf-Of の交換をして Gateway に渡す。
交換後のトークンのクレーム（aud、email、department、act）を最初の結果で表示する。

L1 open   : LLM が filter をどう書くか。user-b（sales）なのに department=hr を書くことがある
L2 locked : 同じ頼みでも、interceptor が department=sales に置き換える
LLM の出力は毎回変わるので、filter を書くかどうかは実行ごとに違ってよい。
interceptor のログで、LLM が書いた引数（received）と Gateway が送った引数（sent）を見比べる。
"""

import json
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import quote

import boto3

from common import REGION, USER_B, load_state, save_result, sign_in

logs = boto3.client("logs", region_name=REGION)
PROMPTS = ["人事部の採用計画を教えてください。", "2026年の営業パイプラインを見せてください。",
           "Q3の営業パイプラインはどうなっていますか？"]


def invoke(arn: str, token: str, payload: dict) -> dict:
    url = (f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{quote(arn, safe='')}"
           f"/invocations?qualifier=DEFAULT")
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token}",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": f"kbmf-{uuid.uuid4()}"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"http": e.code, "body": e.read().decode()[:500]}


def main() -> None:
    state = load_state()
    arn, url = state["runtime"]["arn"], state["gateway"]["url"]
    start = int(time.time() * 1000)
    tb = sign_in(state, USER_B)
    out = {}
    for target in ("open", "locked"):
        for i, prompt in enumerate(PROMPTS, 1):
            r = invoke(arn, tb, {"gateway_url": url, "target": target, "prompt": prompt})
            out[f"{target}_{i}"] = {"prompt": prompt, **r}
            calls = r.get("tool_calls", [])
            if target == "open" and i == 1:
                print("exchanged:", json.dumps(r.get("exchanged"), ensure_ascii=False))
            print(target, i, prompt)
            for c in calls:
                f = (c.get("input") or {}).get("retrievalConfiguration", {}).get("managedSearchConfiguration", {})
                print("   LLM filter:", json.dumps(f.get("filter"), ensure_ascii=False), "-> docs:", c.get("docs"))
            if not calls:
                print("   no tool call", json.dumps(r, ensure_ascii=False)[:200])
    time.sleep(15)
    ev = logs.filter_log_events(logGroupName="/aws/lambda/kbmf-interceptor", startTime=start,
                                filterPattern='"KBMF_INTERCEPTOR"').get("events", [])
    out["interceptor_log"] = [json.loads(e["message"][e["message"].index("{"):]) for e in ev]
    print("interceptor log entries:", len(out["interceptor_log"]))
    print(f"saved {save_result('12_llm', out)}")


if __name__ == "__main__":
    main()
