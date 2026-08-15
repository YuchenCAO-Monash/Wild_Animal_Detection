import json
import os
import boto3
from decimal import Decimal
from urllib.parse import urlparse, unquote

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

TABLE_NAME = os.environ.get("TABLE_NAME", "FIT5225_A2_test")
URL_EXPIRES_IN = int(os.environ.get("URL_EXPIRES_IN", "300"))

table = dynamodb.Table(TABLE_NAME)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "http://127.0.0.1:3000",
    "Access-Control-Allow-Headers": "content-type,authorization",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
}


def to_json_safe(obj):
    if isinstance(obj, list):
        return [to_json_safe(x) for x in obj]

    if isinstance(obj, dict):
        return {k: to_json_safe(v) for k, v in obj.items()}

    if isinstance(obj, Decimal):
        if obj % 1 == 0:
            return int(obj)
        return float(obj)

    return obj


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            **CORS_HEADERS,
            "Content-Type": "application/json",
        },
        "body": json.dumps(to_json_safe(body)),
    }


def get_claims(event):
    return (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )


def extract_thumbnail_key(value):
    """
    Supports:
    1. thumbnail/xxx_thumb.jpg
    2. normal S3 URL
    3. presigned S3 URL from thumbnail_url
    """

    if not value:
        return ""

    text = str(value).strip()

    # Direct S3 key
    if text.startswith("thumbnail/"):
        return text

    parsed = urlparse(text)
    path = unquote(parsed.path or "")

    if path.startswith("/"):
        path = path[1:]

    # Example:
    # https://fit5225-a2-test.s3.amazonaws.com/thumbnail/abc_thumb.jpg
    if path.startswith("thumbnail/"):
        return path

    # Example:
    # https://s3.amazonaws.com/fit5225-a2-test/thumbnail/abc_thumb.jpg
    marker = "thumbnail/"
    if marker in path:
        return path[path.index(marker):]

    return ""


def add_presigned_urls(item):
    bucket = item.get("bucket")
    s3_key = item.get("s3_key")
    thumbnail_bucket = item.get("thumbnail_bucket") or bucket
    thumbnail_s3_key = item.get("thumbnail_s3_key")

    if bucket and s3_key:
        try:
            item["file_url"] = s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": bucket,
                    "Key": s3_key,
                },
                ExpiresIn=URL_EXPIRES_IN,
            )
        except Exception:
            item["file_url"] = None

    if thumbnail_bucket and thumbnail_s3_key:
        try:
            item["thumbnail_url"] = s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": thumbnail_bucket,
                    "Key": thumbnail_s3_key,
                },
                ExpiresIn=URL_EXPIRES_IN,
            )
        except Exception:
            item["thumbnail_url"] = None

    return item


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method")

    if method == "OPTIONS":
        return response(200, {"message": "ok"})

    claims = get_claims(event)

    owner_sub = claims.get("sub")
    owner_email = claims.get("email", "")

    if not owner_sub:
        return response(401, {"message": "Missing Cognito user identity."})

    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return response(400, {"message": "Invalid JSON body"})

    thumbnail_value = (
        body.get("thumbnail_url")
        or body.get("thumbnail_s3_key")
        or body.get("url")
        or ""
    )

    thumbnail_key = extract_thumbnail_key(thumbnail_value)

    if not thumbnail_key:
        return response(
            400,
            {
                "message": "Please provide a valid thumbnail_url or thumbnail_s3_key containing thumbnail/...",
            },
        )

    matched_item = None
    scan_kwargs = {}

    while True:
        scan_result = table.scan(**scan_kwargs)

        for item in scan_result.get("Items", []):
            if item.get("owner_sub") != owner_sub:
                continue

            if item.get("thumbnail_s3_key") == thumbnail_key:
                matched_item = item
                break

        if matched_item:
            break

        last_key = scan_result.get("LastEvaluatedKey")

        if not last_key:
            break

        scan_kwargs["ExclusiveStartKey"] = last_key

    if not matched_item:
        return response(
            404,
            {
                "message": "No file found for this thumbnail under the current user.",
                "thumbnail_s3_key": thumbnail_key,
            },
        )

    return response(
        200,
        {
            "owner_sub": owner_sub,
            "owner_email": owner_email,
            "thumbnail_s3_key": thumbnail_key,
            "file": add_presigned_urls(matched_item),
        },
    )