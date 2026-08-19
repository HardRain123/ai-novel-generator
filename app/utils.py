from datetime import datetime, timezone
import json


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def json_loads(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default

