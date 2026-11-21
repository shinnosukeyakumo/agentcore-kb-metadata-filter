"""AgentCore Runtime 上のエージェント。

Runtime が受け取ったユーザーのアクセストークン（Authorization ヘッダー）を、そのまま Gateway に渡す。
LLM（Claude Sonnet 4.6）には Gateway の Retrieve ツールを 1 本だけ渡し、filter の書き方は指示しない。
LLM がツール引数に何を書いたかと、検索で返ってきた文書名を返す。トークン本体は返さない。

payload: {"gateway_url": "...", "target": "open" | "locked", "prompt": "..."}
"""

import os
import re

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
MODEL_ID = "jp.anthropic.claude-sonnet-4-6"
SYSTEM_PROMPT = "You are an internal assistant. Use the Retrieve tool to search company documents, then answer the user."
DOC = re.compile(r"docs/([a-z]+/[a-z0-9-]+)\.txt")

app = BedrockAgentCoreApp()


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
    headers = {k.lower(): v for k, v in (getattr(context, "request_headers", None) or {}).items()}
    auth = headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return {"error": "no Authorization header. Set requestHeaderAllowlist on the runtime."}
    return run_llm(payload["gateway_url"], auth[7:], payload["prompt"], payload["target"])


if __name__ == "__main__":
    app.run()
