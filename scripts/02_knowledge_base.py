"""Managed Knowledge Base を用意する。

S3 バケット kbmf-docs-<アカウント ID>
    docs/<部署>/<文書>.txt                     文書本体
    meta/docs/<部署>/<文書>.txt.metadata.json  メタデータ（データソースの metadataFilesPrefix = meta/）
    acl/global-acl.json                        ACL を有効にするときに必須。空配列

文書の割り当て
    docs/hr/salary-bands.txt       department=hr     user-a          型付きの書式
    docs/hr/hiring-plan.txt        department=hr     user-a, user-b  平らな書式
    docs/sales/q3-pipeline.txt     department=sales  user-a, user-b  型付きの書式
    docs/sales/discount-rules.txt  department=sales  user-b          型付きの書式
"""

import json
import time

import boto3

from common import ACCOUNT, PREFIX, REGION, USER_A, USER_B, ensure_role, load_state, save_state

s3 = boto3.client("s3", region_name=REGION)
agent = boto3.client("bedrock-agent", region_name=REGION)

BUCKET = f"{PREFIX}-docs-{ACCOUNT}"
KB_NAME, DS_NAME = f"{PREFIX}-kb", f"{PREFIX}-docs"

DOCS = {
    "docs/hr/salary-bands.txt": ("hr", [USER_A], "typed",
        "Engineering salary bands for 2026. L3 engineer: 6.0M-7.5M JPY. L4 engineer: 7.5M-9.5M JPY. "
        "Bands are confidential and reviewed every April."),
    "docs/hr/hiring-plan.txt": ("hr", [USER_A, USER_B], "flat",
        "Hiring plan for 2026. The platform team will hire three engineers in the first half. "
        "Interviews are held on Tuesdays and Thursdays."),
    "docs/sales/q3-pipeline.txt": ("sales", [USER_A, USER_B], "typed",
        "Sales pipeline for Q3. Twelve enterprise deals are in negotiation. "
        "The largest deal is with a logistics company in Kofu."),
    "docs/sales/discount-rules.txt": ("sales", [USER_B], "typed",
        "Discount rules. Sales staff may offer up to 15 percent discount without approval. "
        "Discounts above 15 percent need the sales director's approval."),
}


def metadata(dept: str, users: list[str], style: str) -> dict:
    acl = [{"Name": u, "Type": "USER", "Access": "ALLOW"} for u in users]
    if style == "flat":
        return {"metadataAttributes": {"department": dept, "year": 2026}, "accessControlList": acl}
    return {"metadataAttributes": {
                "department": {"value": {"type": "STRING", "stringValue": dept}, "includeForEmbedding": False},
                "year": {"value": {"type": "NUMBER", "numberValue": 2026}, "includeForEmbedding": False}},
            "accessControlList": acl}


def upload() -> None:
    try:
        s3.head_bucket(Bucket=BUCKET)
    except s3.exceptions.ClientError:
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.put_public_access_block(Bucket=BUCKET, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True})
        print(f"created bucket {BUCKET}")
    for key, (dept, users, style, text) in DOCS.items():
        s3.put_object(Bucket=BUCKET, Key=key, Body=text.encode())
        s3.put_object(Bucket=BUCKET, Key=f"meta/{key}.metadata.json",
                      Body=json.dumps(metadata(dept, users, style)).encode())
    s3.put_object(Bucket=BUCKET, Key="acl/global-acl.json", Body=b"[]")


def kb_role() -> str:
    return ensure_role(f"{PREFIX}-kb-role", "bedrock.amazonaws.com", {
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["bedrock:InvokeModel"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"],
             "Resource": [f"arn:aws:s3:::{BUCKET}", f"arn:aws:s3:::{BUCKET}/*"]}]})


def wait(get, done: tuple, tries: int = 180, sleep: int = 5) -> str:
    for _ in range(tries):
        v = get()
        if v in done:
            return v
        time.sleep(sleep)
    raise TimeoutError(v)


def ensure_kb(role_arn: str) -> tuple[str, str]:
    kb = next((k for k in agent.list_knowledge_bases(maxResults=100)["knowledgeBaseSummaries"]
               if k["name"] == KB_NAME), None)
    if not kb:
        for _ in range(6):
            try:
                kb = agent.create_knowledge_base(
                    name=KB_NAME, roleArn=role_arn,
                    knowledgeBaseConfiguration={"type": "MANAGED", "managedKnowledgeBaseConfiguration": {
                        "embeddingModelType": "MANAGED"}})["knowledgeBase"]
                break
            except agent.exceptions.ValidationException as e:
                print("retry create_knowledge_base:", e)  # ロールの伝播待ち
                time.sleep(10)
        print(f"created knowledge base {KB_NAME}")
    kid = kb["knowledgeBaseId"]
    print("knowledge base", wait(lambda: agent.get_knowledge_base(knowledgeBaseId=kid)["knowledgeBase"]["status"],
                                 ("ACTIVE", "FAILED")))
    ds = next((d for d in agent.list_data_sources(knowledgeBaseId=kid)["dataSourceSummaries"]
               if d["name"] == DS_NAME), None)
    if not ds:
        params = {"type": "S3", "version": "1", "aclEnabled": True,
                  "aclConfiguration": {"globalAccessControlListS3Uri": f"s3://{BUCKET}/acl/global-acl.json"},
                  "filterConfiguration": {"inclusionPrefixes": ["docs/"], "maxFileSizeInMegaBytes": "500"},
                  "metadataFilesPrefix": "meta/",
                  "connectionConfiguration": {"bucketName": BUCKET, "bucketOwnerAccountId": ACCOUNT}}
        ds = agent.create_data_source(
            knowledgeBaseId=kid, name=DS_NAME, dataDeletionPolicy="DELETE",
            dataSourceConfiguration={"type": "MANAGED_KNOWLEDGE_BASE_CONNECTOR",
                                     "managedKnowledgeBaseConnectorConfiguration": {"connectorParameters": params}},
        )["dataSource"]
        print(f"created data source {DS_NAME}")
    dsid = ds["dataSourceId"]
    print("data source", wait(lambda: agent.get_data_source(knowledgeBaseId=kid, dataSourceId=dsid)
                              ["dataSource"]["status"], ("AVAILABLE", "FAILED")))
    return kid, dsid


def ingest(kid: str, dsid: str) -> dict:
    job = agent.start_ingestion_job(knowledgeBaseId=kid, dataSourceId=dsid)["ingestionJob"]["ingestionJobId"]
    for _ in range(360):
        j = agent.get_ingestion_job(knowledgeBaseId=kid, dataSourceId=dsid, ingestionJobId=job)["ingestionJob"]
        if j["status"] in ("COMPLETE", "FAILED", "STOPPED"):
            break
        time.sleep(5)
    return {"status": j["status"], "statistics": j.get("statistics"), "failureReasons": j.get("failureReasons")}


def main() -> None:
    state = load_state()
    upload()
    kid, dsid = ensure_kb(kb_role())
    state["kb"] = {"bucket": BUCKET, "kb_id": kid, "ds_id": dsid}
    save_state(state)
    print("ingestion", json.dumps(ingest(kid, dsid), default=str))


if __name__ == "__main__":
    main()
