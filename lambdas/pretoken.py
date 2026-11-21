"""Cognito の pre token generation トリガー（V3_0）。

アクセストークンに 2 つのクレームを足す。
- email      : Knowledge Base の ACL（userContext.userId）に使う
- department : メタデータの filter に使う。Cognito のカスタム属性 custom:department から取る
"""


def handler(event, context):
    attrs = (event.get("request") or {}).get("userAttributes") or {}
    add = {"email": attrs.get("email", "")}
    if attrs.get("custom:department"):
        add["department"] = attrs["custom:department"]
    event["response"] = {"claimsAndScopeOverrideDetails": {
        "accessTokenGeneration": {"claimsToAddOrOverride": add},
    }}
    return event
