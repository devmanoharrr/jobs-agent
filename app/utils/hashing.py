import hashlib
from typing import Any

import orjson


def hash_payload(payload: Any) -> str:
    encoded = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(encoded).hexdigest()
