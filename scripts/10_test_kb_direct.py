"""Gateway を通さず、自分の IAM で Knowledge Base を直接 Retrieve する。

M1 ACL       : userContext なし / user-a / user-b で、出てくる文書が変わるか
M2 書式       : 平らな書式と型付きの書式のどちらでも filter が効くか。結果のメタデータの見え方
M3 filter×ACL : filter と ACL は重なって効くか（user-a で department=sales → discount-rules は出ない）
                startsWith のような未対応の演算子はどうなるか
"""

import json
import re

import boto3
from botocore.exceptions import ClientError

from common import REGION, USER_A, USER_B, load_state, save_result

rt = boto3.client("bedrock-agent-runtime", region_name=REGION)
DOC = re.compile(r"docs/([a-z]+/[a-z0-9-]+)\.txt")
QUERY = "plan, rules, salary, pipeline for 2026"
KID = load_state()["kb"]["kb_id"]


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
    }
    path = save_result("10_kb_direct", out)
    print(json.dumps({k: v for k, v in out.items() if k != "M2_format"}, indent=1, ensure_ascii=False))
    print("M2_format", {k: v for k, v in out["M2_format"].items() if k != "user-a_detail"})
    print(f"saved {path}")


if __name__ == "__main__":
    main()
