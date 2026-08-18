"""POST one mapped score to Golden Records and report what came back.

Returns the outcome for every HTTP status rather than raising, so the
state machine can read the API's own `errors` list and name the reason a
score was rejected. Transient failures are raised instead, so the
caller's Retry policy handles them.
"""

import json
import os

import boto3
import requests

# The Scores API returns this exact message in the response body's
# `errors` list when a matching score is already present. Mirrors
# DUPLICATE_ERROR in app.py.
DUPLICATE_ERROR = "This score already exists in the database."

SCORES_URL = os.environ["SCORES_URL"]
SECRET_ARN = os.environ["API_KEY_SECRET_ARN"]

# Cached across invocations -- the key does not change within a run.
_auth = None


class RetryableStatus(Exception):
    """Raised on 429/5xx so Step Functions retries by error name."""


def _auth_header():
    """Read the API key from the EventBridge connection's own secret.

    Reusing the connection's secret keeps the key in exactly one place:
    it is typed once as a NoEcho parameter and never copied into an
    environment variable. The shape ({"api_key_name","api_key_value"})
    is EventBridge's, so treat a missing field as fatal rather than
    guessing.
    """
    global _auth
    if _auth is None:
        client = boto3.client("secretsmanager")
        raw = client.get_secret_value(SecretId=SECRET_ARN)["SecretString"]
        secret = json.loads(raw)
        _auth = (secret["api_key_name"], secret["api_key_value"])
    return _auth


def _errors(response):
    """Pull the `errors` list out of a response body.

    Mirrors app.py's _extract_errors. A body that is not JSON, or has no
    `errors`, degrades to a short slice of the raw text so the report
    still says something useful.
    """
    try:
        parsed = response.json()
    except ValueError:
        text = response.text.strip()
        return [text[:200]] if text else []
    errors = parsed.get("errors") if isinstance(parsed, dict) else None
    if isinstance(errors, list):
        return [str(item) for item in errors]
    if isinstance(errors, str):
        return [errors]
    return []


def handler(event, context):
    header_name, header_value = _auth_header()
    try:
        response = requests.post(
            SCORES_URL,
            json=event["body"],
            headers={header_name: header_value},
            timeout=25,
        )
    except requests.exceptions.RequestException as error:
        # DNS failure, connection reset, TLS error, timeout -- all
        # transient, and none of them reached the API.
        raise RetryableStatus(f"connection failed: {error}") from error

    status = response.status_code
    if status == 429 or status >= 500:
        raise RetryableStatus(f"HTTP {status}")

    errors = _errors(response)
    return {
        "statusCode": status,
        # A 2xx carrying errors is treated as a rejection, so a
        # "success" that is not one cannot be reported as submitted.
        "ok": 200 <= status < 300 and not errors,
        "duplicate": DUPLICATE_ERROR in errors,
        "errors": errors,
    }
