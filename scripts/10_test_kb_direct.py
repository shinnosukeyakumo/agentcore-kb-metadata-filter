"""Gateway を通さず、自分の IAM で Knowledge Base を直接 Retrieve する。

M1 ACL       : userContext なし / user-a / user-b で、出てくる文書が変わるか
M2 書式       : 平らな書式と型付きの書式のどちらでも filter が効くか。結果のメタデータの見え方
M3 filter×ACL : filter と ACL は重なって効くか（user-a で department=sales → discount-rules は出ない）
                startsWith のような未対応の演算子はどうなるか
M4 書き込み    : 文書担当（kbmf-doc-writer）と管理者（kbmf-meta-admin）のロールで docs/ meta/ acl/ に書けるか
                バケットポリシーで、meta/ と acl/ は管理者しか書けないようにしてある
"""

import json
import re
import time

import boto3
from botocore.exceptions import ClientError

from common import REGION, USER_A, USER_B, load_state, save_result, sts

rt = boto3.client("bedrock-agent-runtime", region_name=REGION)
DOC = re.compile(r"docs/([a-z]+/[a-z0-9-]+)\.txt")
QUERY = "plan, rules, salary, pipeline for 2026"
STATE = load_state()
KID = STATE["kb"]["kb_id"]
PROBE_KEYS = ("docs/zz-probe.txt", "meta/docs/zz-probe.txt.metadata.json", "acl/zz-probe.json")


def retrieve(user=None, flt=None, detail=False):
    cfg = {"numberOfResults": 10}
    if flt:
        cfg["filter"] = flt
    kw = dict(knowledgeBaseId=KID, retrievalQuery={"text": QUERY},
              retrievalConfiguration={"managedSearchConfiguration": cfg})
    if user:
        kw["userContext"] = {"userId": user}
    try:
        rr = rt.retrieve(**kw)["retrievalResults"]
    except ClientError as e:
        return {"error": str(e)[:300]}
    found = [(DOC.search(json.dumps(r["location"])), r) for r in rr]
    docs = sorted({m.group(1) for m, _ in found if m})
    if detail:
        return {"docs": docs, "metadata": {m.group(1): r.get("metadata") for m, r in found if m}}
    return docs


def dept(v):
    return {"equals": {"key": "department", "value": v}}


def assume(role_arn: str):
    for _ in range(10):  # 作ったばかりのロールは、引き受けられるまで少しかかる
        try:
            c = sts.assume_role(RoleArn=role_arn, RoleSessionName="kbmf-probe")["Credentials"]
            return boto3.client("s3", region_name=REGION, aws_access_key_id=c["AccessKeyId"],
                                aws_secret_access_key=c["SecretAccessKey"], aws_session_token=c["SessionToken"])
        except ClientError:
            time.sleep(5)
    raise RuntimeError(f"cannot assume {role_arn}")


def try_put(s3, key: str) -> str:
    bucket = STATE["kb"]["bucket"]
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=b"probe")
    except ClientError as e:
        return e.response["Error"]["Code"]
    s3.delete_object(Bucket=bucket, Key=key)
    return "allowed"


def write_probe() -> dict:
    out = {}
    for label in ("writer_role", "admin_role"):
        s3 = assume(STATE["kb"][label])
        out[label.removesuffix("_role")] = {k: try_put(s3, k) for k in PROBE_KEYS}
    return out


def main() -> None:
    out = {
        "M1_acl": {"no_userContext": retrieve(), "user-a": retrieve(USER_A), "user-b": retrieve(USER_B)},
        "M2_format": {
            "user-a_detail": retrieve(USER_A, detail=True),
            "user-a_department_hr": retrieve(USER_A, dept("hr")),
            "user-a_year_2026": retrieve(USER_A, {"equals": {"key": "year", "value": 2026}}),
        },
        "M3_filter_and_acl": {
            "user-a_department_sales": retrieve(USER_A, dept("sales")),
            "user-b_department_sales": retrieve(USER_B, dept("sales")),
            "user-b_department_hr": retrieve(USER_B, dept("hr")),
            "user-a_startsWith_s": retrieve(USER_A, {"startsWith": {"key": "department", "value": "s"}}),
        },
        "M4_write": write_probe(),
    }
    path = save_result("10_kb_direct", out)
    print(json.dumps({k: v for k, v in out.items() if k != "M2_format"}, indent=1, ensure_ascii=False))
    print("M2_format", {k: v for k, v in out["M2_format"].items() if k != "user-a_detail"})
    print(f"saved {path}")


if __name__ == "__main__":
    main()
