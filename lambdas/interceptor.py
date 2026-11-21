"""AgentCore Gateway の REQUEST interceptor。

Retrieve の引数を、LLM が書いた値ではなくトークンのクレームから入れ直す。
- userContext.userId : どのターゲットでも、トークンの email で上書きする
- filter             : ターゲットごとに扱いを変える
    locked : LLM が何を書いても捨てて、department の条件に置き換える
    open   : LLM が書いた条件があれば、department の条件と andAll で重ねる（LLM の条件は絞り込みにしか使えない）
    static : 触らない（ターゲットの parameterValues に固定した条件だけで動く）
  department が無いトークンでは、どの文書にも一致しない値にする

確認用に、受け取った引数と書き換えた後の引数をログに出す。トークン文字列は出さない。
"""

import base64
import copy
import json


def _claims(headers):
    for k, v in (headers or {}).items():
        if k.lower() == "authorization" and v.startswith("Bearer "):
            p = v[7:].split(".")[1]
            p += "=" * (-len(p) % 4)
            return json.loads(base64.urlsafe_b64decode(p))
    return {}


def handler(event, context):
    request = (event or {}).get("mcp", {}).get("gatewayRequest", {})
    body = request.get("body", {})
    name = body.get("params", {}).get("name", "") if body.get("method") == "tools/call" else ""
    if name.endswith("___Retrieve"):
        args = body["params"].setdefault("arguments", {})
        received = copy.deepcopy(args)
        c = _claims(request.get("headers"))
        args["userContext"] = {"userId": c.get("email") or "__no_identity__@invalid"}
        target = name.split("___")[0]
        if target in ("locked", "open"):
            dept = {"equals": {"key": "department", "value": c.get("department") or "__no_department__"}}
            cfg = args.setdefault("retrievalConfiguration", {}).setdefault("managedSearchConfiguration", {})
            if target == "open" and cfg.get("filter"):
                cfg["filter"] = {"andAll": [cfg["filter"], dept]}
            else:
                cfg["filter"] = dept
        print(json.dumps({"marker": "KBMF_INTERCEPTOR", "tool": name, "received": received, "sent": args},
                         ensure_ascii=False))
    return {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayRequest": {"body": body}}}
