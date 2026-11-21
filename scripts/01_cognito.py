"""Cognito を用意する。

- ユーザープール（Essentials。アクセストークンへのクレーム追加に必要）
  - カスタム属性 custom:department
  - pre token generation トリガー（V3_0）で、アクセストークンに email と department を足す
- アプリクライアント web（secret なし、ADMIN_USER_PASSWORD_AUTH）
- ユーザー 2 人：user-a（hr）、user-b（sales）。パスワードはランダムに作って .state.json に保存する
"""

import secrets
import string

from common import (ACCOUNT, DEPARTMENTS, PREFIX, REGION, ROOT, claims, ensure_function, idp, lam, load_state,
                    save_state, sign_in)

POOL_NAME = f"{PREFIX}-pool"
TRIGGER = f"{PREFIX}-pretoken"


def password() -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20)) + "!a1A"


def ensure_pool(trigger_arn: str) -> str:
    for page in idp.get_paginator("list_user_pools").paginate(MaxResults=60):
        for p in page["UserPools"]:
            if p["Name"] == POOL_NAME:
                return p["Id"]
    pool_id = idp.create_user_pool(
        PoolName=POOL_NAME,
        UserPoolTier="ESSENTIALS",
        UsernameAttributes=["email"],
        AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
        DeletionProtection="INACTIVE",
        Schema=[{"Name": "department", "AttributeDataType": "String", "Mutable": True,
                 "StringAttributeConstraints": {"MinLength": "1", "MaxLength": "32"}}],
        LambdaConfig={"PreTokenGenerationConfig": {"LambdaVersion": "V3_0", "LambdaArn": trigger_arn}},
    )["UserPool"]["Id"]
    print(f"created user pool {POOL_NAME}")
    return pool_id


def allow_cognito(pool_id: str) -> None:
    try:
        lam.add_permission(FunctionName=TRIGGER, StatementId="cognito-pretoken", Action="lambda:InvokeFunction",
                           Principal="cognito-idp.amazonaws.com",
                           SourceArn=f"arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/{pool_id}")
    except lam.exceptions.ResourceConflictException:
        pass


def ensure_client(state: dict, pool_id: str) -> str:
    if state.get("cognito", {}).get("client_id"):
        return state["cognito"]["client_id"]
    c = idp.create_user_pool_client(
        UserPoolId=pool_id, ClientName=f"{PREFIX}-web", GenerateSecret=False,
        ExplicitAuthFlows=["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
    )["UserPoolClient"]
    print(f"created app client {PREFIX}-web")
    return c["ClientId"]


def ensure_users(state: dict, pool_id: str) -> dict:
    passwords = state.get("cognito", {}).get("passwords", {})
    for email, dept in DEPARTMENTS.items():
        if email in passwords:
            continue
        try:
            idp.admin_create_user(UserPoolId=pool_id, Username=email, MessageAction="SUPPRESS", UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "custom:department", "Value": dept},
            ])
        except idp.exceptions.UsernameExistsException:
            pass
        pw = password()
        idp.admin_set_user_password(UserPoolId=pool_id, Username=email, Password=pw, Permanent=True)
        passwords[email] = pw
        print(f"created user {email} (department={dept})")
    return passwords


def main() -> None:
    state = load_state()
    trigger_arn = ensure_function(TRIGGER, ROOT / "lambdas" / "pretoken.py")
    pool_id = ensure_pool(trigger_arn)
    allow_cognito(pool_id)
    state["cognito"] = {
        **state.get("cognito", {}),
        "pool_id": pool_id,
        "issuer": f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}",
    }
    save_state(state)
    state["cognito"]["client_id"] = ensure_client(state, pool_id)
    save_state(state)
    state["cognito"]["passwords"] = ensure_users(state, pool_id)
    save_state(state)

    # トリガーが効いているかを確かめる。トークン本体は出さず、クレームだけを見る
    for email in DEPARTMENTS:
        c = claims(sign_in(state, email))
        print(email, {k: c.get(k) for k in ("client_id", "email", "department", "token_use")})


if __name__ == "__main__":
    main()
