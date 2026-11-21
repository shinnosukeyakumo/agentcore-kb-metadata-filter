"""AgentCore Runtime 上のエージェント。

1. Runtime が、入口で検証したユーザーのトークンからワークロードトークンを作って渡してくる
2. AgentCore Identity の On-Behalf-Of で、Gateway 用のトークン（aud = kbmf-gateway）に交換する
   中では Cognito の token exchange が呼ばれる。ユーザーの email と department は交換後のトークンにも入る
3. 交換したトークンで Gateway に接続し、LLM（Claude Sonnet 4.6）に Retrieve ツールを 1 本だけ渡す
   filter の書き方は指示しない

LLM がツール引数に何を書いたか、検索で返ってきた文書名、交換後のトークンのクレームを返す。トークン本体は返さない。

payload: {"gateway_url": "...", "target": "open" | "locked", "prompt": "..."}
"""

import base64
import json
import os
import re

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
MODEL_ID = "jp.anthropic.claude-sonnet-4-6"
SYSTEM_PROMPT = "You are an internal assistant. Use the Retrieve tool to search company documents, then answer the user."
DOC = re.compile(r"docs/([a-z]+/[a-z0-9-]+)\.txt")
PROVIDER = os.environ["OBO_PROVIDER"]
AUDIENCE = os.environ["GATEWAY_AUDIENCE"]
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

app = BedrockAgentCoreApp()
dp = boto3.client("bedrock-agentcore", region_name=REGION)


def claims(token: str) -> dict:
    p = token.split(".")[1]
    p += "=" * (-len(p) % 4)
    c = json.loads(base64.urlsafe_b64decode(p))
    return {k: c.get(k) for k in ("client_id", "aud", "email", "department", "act")}


def exchange(wat: str) -> str:
    # Identity は subject_token_type に jwt を付けて送るが、Cognito は access_token しか受け付けないので上書きする
    return dp.get_resource_oauth2_token(
        workloadIdentityToken=wat, resourceCredentialProviderName=PROVIDER,
        oauth2Flow="ON_BEHALF_OF_TOKEN_EXCHANGE", scopes=[], audiences=[AUDIENCE],
        customParameters={"subject_token_type": ACCESS_TOKEN_TYPE})["accessToken"]


def run_llm(url: str, token: str, prompt: str, target: str) -> dict:
    client = MCPClient(url=url, headers={"Authorization": f"Bearer {token}"})
    with client:
        tools = [t for t in client.list_tools_sync() if t.tool_name == f"{target}___Retrieve"]
        agent = Agent(model=BedrockModel(model_id=MODEL_ID, region_name=REGION), tools=tools,
                      system_prompt=SYSTEM_PROMPT, callback_handler=None)
        answer = str(agent(prompt))
    calls = []
    for m in agent.messages:
        for c in m.get("content", []):
            if "toolUse" in c:
                calls.append({"input": c["toolUse"].get("input")})
            if "toolResult" in c and calls:
                text = " ".join(x.get("text", "") for x in c["toolResult"].get("content", []) if "text" in x)
                calls[-1]["docs"] = sorted(set(DOC.findall(text)))
                calls[-1]["status"] = c["toolResult"].get("status")
    return {"tools_given": [t.tool_name for t in tools], "tool_calls": calls, "answer": answer[:600]}


@app.entrypoint
def invoke(payload, context):
    wat = BedrockAgentCoreContext.get_workload_access_token()
    if not wat:
        return {"error": "no workload access token. Call the runtime with a user's JWT."}
    token = exchange(wat)
    return {"exchanged": claims(token), **run_llm(payload["gateway_url"], token, payload["prompt"], payload["target"])}


if __name__ == "__main__":
    app.run()
