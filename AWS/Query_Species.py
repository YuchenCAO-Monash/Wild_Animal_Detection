import json
import os
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr


dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

TABLE_NAME = os.environ.get("TABLE_NAME", "FIT5225_A2_test")
URL_EXPIRES_IN = int(os.environ.get("URL_EXPIRES_IN", "300"))
table = dynamodb.Table(TABLE_NAME)


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
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "OPTIONS,POST,GET"
        },
        "body": json.dumps(to_json_safe(body))
    }


def get_claims(event):
    authorizer = event.get("requestContext", {}).get("authorizer", {})

    claims = authorizer.get("claims")
    if claims:
        return claims

    jwt_claims = authorizer.get("jwt", {}).get("claims")
    if jwt_claims:
        return jwt_claims

    return {}


def normalize_text(value):
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def add_presigned_urls(item):
    """
    给 DynamoDB item 添加前端可直接使用的 file_url 和 thumbnail_url
    """

    bucket = item.get("bucket")
    s3_key = item.get("s3_key")
    thumbnail_bucket = item.get("thumbnail_bucket") or bucket
    thumbnail_key = item.get("thumbnail_s3_key")

    if bucket and s3_key:
        try:
            item["file_url"] = s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": bucket,
                    "Key": s3_key
                },
                ExpiresIn=URL_EXPIRES_IN
            )
        except Exception as e:
            print("Failed to generate file_url:", str(e))
            item["file_url"] = ""

    if thumbnail_bucket and thumbnail_key:
        try:
            item["thumbnail_url"] = s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": thumbnail_bucket,
                    "Key": thumbnail_key
                },
                ExpiresIn=URL_EXPIRES_IN
            )
        except Exception as e:
            print("Failed to generate thumbnail_url:", str(e))
            item["thumbnail_url"] = ""

    return item


def species_matches(item_species, query_species):
    """
    兼容：
    1. species = ["Eastern gray kangaroo"]
    2. species = "Eastern gray kangaroo"
    3. species = None
    """

    query_species = normalize_text(query_species)

    if not item_species:
        return False

    if isinstance(item_species, list):
        for species in item_species:
            species_name = normalize_text(species)

            if species_name == query_species or query_species in species_name:
                return True
        return False

    if isinstance(item_species, str):
        species_name = normalize_text(item_species)
        return species_name == query_species or query_species in species_name

    return False


def tags_contain_species(tags, query_species):
    if isinstance(tags, list):
        tag_items = [(tag, 1) for tag in tags]
    elif isinstance(tags, dict):
        tag_items = tags.items()
    else:
        return False

    for tag, count in tag_items:
        tag_name = normalize_text(tag)

        try:
            count_value = int(count)
        except Exception:
            count_value = 0

        if count_value > 0 and (tag_name == query_species or query_species in tag_name):
            return True

    return False


def lambda_handler(event, context):
    try:
        print("Received event:")
        print(json.dumps(event))

        method = (
            event.get("requestContext", {}).get("http", {}).get("method")
            or event.get("httpMethod")
        )

        if method == "OPTIONS":
            return response(200, {"message": "ok"})

        body = json.loads(event.get("body") or "{}")
        query_species = normalize_text(body.get("species"))

        if not query_species:
            return response(400, {
                "message": "species is required"
            })

        claims = get_claims(event)

        owner_sub = claims.get("sub", "")
        owner_email = claims.get("email", "")

        print("owner_sub:", owner_sub)
        print("owner_email:", owner_email)
        print("query_species:", query_species)

        if not owner_sub and not owner_email:
            return response(401, {
                "message": "Unauthorized",
                "detail": "Missing Cognito user claims"
            })

        filter_expression = None

        if owner_sub and owner_email:
            filter_expression = (
                Attr("owner_sub").eq(owner_sub) |
                Attr("owner_email").eq(owner_email)
            )
        elif owner_sub:
            filter_expression = Attr("owner_sub").eq(owner_sub)
        else:
            filter_expression = Attr("owner_email").eq(owner_email)

        scan_kwargs = {
            "FilterExpression": filter_expression
        }

        items = []

        while True:
            result = table.scan(**scan_kwargs)
            items.extend(result.get("Items", []))

            if "LastEvaluatedKey" not in result:
                break

            scan_kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]

        print("Current user item count:", len(items))

        matched_files = []

        for item in items:
            item_species = item.get("species", [])

            if (
                species_matches(item_species, query_species)
                or tags_contain_species(item.get("tags", {}), query_species)
            ):
                matched_files.append(add_presigned_urls(item))

        print("Matched count:", len(matched_files))

        return response(200, {
            "count": len(matched_files),
            "owner_sub": owner_sub,
            "owner_email": owner_email,
            "query": {
                "species": query_species
            },
            "files": matched_files
        })

    except Exception as e:
        print("Query species failed:", str(e))

        return response(500, {
            "message": "Query species failed",
            "error": str(e)
        })
