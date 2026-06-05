import base64
import json
import mimetypes
import os
import tempfile
import traceback
import urllib.request
import urllib.error
import uuid
from decimal import Decimal

import boto3

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

TABLE_NAME = os.environ.get("TABLE_NAME", "FIT5225_A2_test")
URL_EXPIRES_IN = int(os.environ.get("URL_EXPIRES_IN", "300"))
CLOUD_RUN_BASE_URL = os.environ.get(
    "CLOUD_RUN_BASE_URL",
    "https://test1-316937710174.australia-southeast2.run.app",
).rstrip("/")

table = dynamodb.Table(TABLE_NAME)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "http://127.0.0.1:3000",
    "Access-Control-Allow-Headers": "content-type,authorization",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
}

ALLOWED_IMAGE_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/bmp",
}

ALLOWED_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
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


def normalize_text(value):
    return str(value).strip().lower().replace("_", " ")


def safe_filename(filename):
    filename = str(filename or "query.jpg")
    filename = filename.replace("\\", "_").replace("/", "_").strip()
    return filename or "query.jpg"


def decode_base64_file(file_base64):
    """
    Supports:
    1. raw base64
    2. data URL:
       data:image/jpeg;base64,/9j/...
    """
    if not file_base64:
        raise ValueError("file_base64 is required.")

    text = str(file_base64)

    if "," in text and text.strip().lower().startswith("data:"):
        text = text.split(",", 1)[1]

    return base64.b64decode(text)


def build_multipart_form_data(file_path, field_name="file"):
    filename = os.path.basename(file_path)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    boundary = "----AussieEcoLenseQueryBoundary" + uuid.uuid4().hex

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    body = b""
    body += f"--{boundary}\r\n".encode("utf-8")
    body += (
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
    ).encode("utf-8")
    body += f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
    body += file_bytes
    body += f"\r\n--{boundary}--\r\n".encode("utf-8")

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }

    return body, headers


def call_cloud_run_analyse_image(file_path):
    endpoint = f"{CLOUD_RUN_BASE_URL}/analyse-image"

    body, headers = build_multipart_form_data(file_path)

    request = urllib.request.Request(
        endpoint,
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            resp_body = resp.read().decode("utf-8")
            return json.loads(resp_body)

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise Exception(f"Cloud Run HTTP {e.code}: {error_body}")

    except urllib.error.URLError as e:
        raise Exception(f"Cloud Run connection error: {str(e)}")


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


def tag_count_for_query(stored_tags, query_tag):
    if not isinstance(stored_tags, dict):
        return 0

    query_norm = normalize_text(query_tag)
    total = 0

    for stored_tag, stored_count in stored_tags.items():
        stored_norm = normalize_text(stored_tag)

        try:
            count_value = int(stored_count)
        except Exception:
            count_value = 0

        # Allows:
        # query "kangaroo" match "eastern gray kangaroo"
        # query "southern cassowary" match exact tag
        if (
            query_norm == stored_norm
            or query_norm in stored_norm
            or stored_norm in query_norm
        ):
            total += count_value

    return total


def find_matching_files(owner_sub, query_tags, match_mode="any"):
    """
    match_mode:
    - any: return files matching at least one query tag
    - all: return files matching all query tags
    """

    normalized_query_tags = []

    for tag, count in query_tags.items():
        tag_name = normalize_text(tag)

        if not tag_name:
            continue

        try:
            count_value = int(count)
        except Exception:
            count_value = 1

        if count_value < 1:
            count_value = 1

        normalized_query_tags.append(
            {
                "tag": tag_name,
                "count": count_value,
            }
        )

    if not normalized_query_tags:
        return []

    matched_files = []
    scan_kwargs = {}

    while True:
        scan_result = table.scan(**scan_kwargs)

        for item in scan_result.get("Items", []):
            if item.get("owner_sub") != owner_sub:
                continue

            if item.get("status") != "processed":
                continue

            stored_tags = item.get("tags", {})

            matched_tag_details = []
            matched_tag_count = 0

            for query in normalized_query_tags:
                actual_count = tag_count_for_query(stored_tags, query["tag"])

                if actual_count >= 1:
                    matched_tag_count += 1
                    matched_tag_details.append(
                        {
                            "query_tag": query["tag"],
                            "required_count": 1,
                            "actual_count": actual_count,
                        }
                    )

            if match_mode == "all":
                is_match = matched_tag_count == len(normalized_query_tags)
            else:
                is_match = matched_tag_count >= 1

            if not is_match:
                continue

            item["match"] = {
                "matched_tag_count": matched_tag_count,
                "total_query_tag_count": len(normalized_query_tags),
                "matched_tags": matched_tag_details,
            }

            matched_files.append(add_presigned_urls(item))

        last_key = scan_result.get("LastEvaluatedKey")

        if not last_key:
            break

        scan_kwargs["ExclusiveStartKey"] = last_key

    matched_files.sort(
        key=lambda x: (
            x.get("match", {}).get("matched_tag_count", 0),
            x.get("created_at", ""),
        ),
        reverse=True,
    )

    return matched_files


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

    filename = safe_filename(body.get("filename", "query.jpg"))
    content_type = body.get("content_type") or mimetypes.guess_type(filename)[0] or "image/jpeg"
    file_base64 = body.get("file_base64")
    match_mode = str(body.get("match_mode") or "any").lower().strip()

    if match_mode not in ["any", "all"]:
        match_mode = "any"

    ext = os.path.splitext(filename)[1].lower()

    if content_type not in ALLOWED_IMAGE_TYPES and ext not in ALLOWED_IMAGE_EXTENSIONS:
        return response(
            400,
            {
                "message": "Only image files are supported for /query/by-file.",
                "content_type": content_type,
                "filename": filename,
            },
        )

    local_path = None

    try:
        file_bytes = decode_base64_file(file_base64)

        if len(file_bytes) == 0:
            return response(400, {"message": "Uploaded query file is empty."})

        suffix = ext if ext in ALLOWED_IMAGE_EXTENSIONS else ".jpg"

        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            local_path = tmp.name
            tmp.write(file_bytes)

        print(f"Temporary query image saved to {local_path}")

        cloud_result = call_cloud_run_analyse_image(local_path)

        query_tags = cloud_result.get("tags", {}) or {}

        if not isinstance(query_tags, dict) or not query_tags:
            return response(
                200,
                {
                    "message": "Query image was analysed, but no tags were detected.",
                    "owner_sub": owner_sub,
                    "owner_email": owner_email,
                    "query_tags": {},
                    "count": 0,
                    "files": [],
                },
            )

        matched_files = find_matching_files(
            owner_sub=owner_sub,
            query_tags=query_tags,
            match_mode=match_mode,
        )

        return response(
            200,
            {
                "message": "Query by file completed.",
                "owner_sub": owner_sub,
                "owner_email": owner_email,
                "filename": filename,
                "content_type": content_type,
                "match_mode": match_mode,
                "query_tags": query_tags,
                "count": len(matched_files),
                "files": matched_files,
            },
        )

    except Exception as e:
        print("query/by-file failed:")
        print(str(e))
        print(traceback.format_exc())

        return response(
            500,
            {
                "message": "Query by file failed.",
                "error": str(e),
            },
        )

    finally:
        try:
            if local_path and os.path.exists(local_path):
                os.remove(local_path)
        except Exception:
            pass