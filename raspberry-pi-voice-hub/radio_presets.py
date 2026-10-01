"""Read a bounded, local radio configuration without contacting any stream.

URLs must point to a browser-playable audio stream, not a station's web page.
Validation here checks URL syntax only; Chromium reports playback failures.
"""

import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


MAX_CONFIG_BYTES = 64 * 1024
MAX_PRESETS = 24
MAX_NAME_CHARS = 80
MAX_URL_CHARS = 2048
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "display" / "radio_presets.json"


def _result(status, presets=None, rejected_count=0):
    # Do not return file paths, exception text, or rejected configuration data.
    return {"presets": presets or [], "status": status,
            "rejected_count": rejected_count}


def _valid_stream_url(value):
    if (not isinstance(value, str) or not value or len(value) > MAX_URL_CHARS
            or "\\" in value
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127
                   for char in value)):
        return False
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or "#" in value or parsed.netloc.endswith(":")):
            return False
        # Accessing port also rejects malformed and out-of-range ports.
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
        hostname = parsed.hostname
        if ":" in hostname:
            ipaddress.IPv6Address(hostname)
        else:
            ascii_host = hostname.encode("idna").decode("ascii").rstrip(".")
            if len(ascii_host) > 253 or not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in ascii_host.split(".")
            ):
                return False
    except (ValueError, UnicodeError):
        return False
    return True


def load_radio_presets(path=None):
    """Return sanitized presets and a stable status for the display API.

    Only the first MAX_PRESETS entries are inspected. Oversized files and
    malformed configurations fail closed to an empty list. Individual invalid
    entries are skipped, allowing the remaining stations to stay usable.
    """
    config_path = DEFAULT_CONFIG_PATH if path is None else Path(path)
    try:
        with config_path.open("rb") as config_file:
            raw = config_file.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return _result("missing")
    except OSError:
        return _result("invalid")
    if len(raw) > MAX_CONFIG_BYTES:
        return _result("invalid")
    try:
        config = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError, RecursionError):
        return _result("invalid")
    if not isinstance(config, dict) or not isinstance(config.get("presets"), list):
        return _result("invalid")
    entries = config["presets"]
    if not entries:
        return _result("empty")
    presets = []
    rejected = max(0, len(entries) - MAX_PRESETS)
    for entry in entries[:MAX_PRESETS]:
        if not isinstance(entry, dict):
            rejected += 1
            continue
        name = entry.get("name")
        url = entry.get("url")
        if isinstance(name, str):
            name = name.strip()
        if (not isinstance(name, str) or not name or len(name) > MAX_NAME_CHARS
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or not _valid_stream_url(url)):
            rejected += 1
            continue
        presets.append({"name": name, "url": url})
    return _result("ready" if presets else "invalid", presets, rejected)
