#!/usr/bin/env python3
"""
Create a standalone Unity WebGL or Eagler package from direct asset URLs or an entry URL.

Usage:
  python unity_standalone.py "https://example.com/game/"
  python unity_standalone.py --loader-url "<...loader.js>" --framework-url "<...framework.js|...framework.js.unityweb>" --data-url "<...data|...data.unityweb>" --wasm-url "<...wasm|...wasm.unityweb>"
  python unity_standalone.py "<entry-url>" --out "My Game" --overwrite
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import html
import http.client
import io
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import brotli  # type: ignore
except ImportError:
    brotli = None


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
EAGLER_MOBILE_USERSCRIPT_URL = (
    "https://raw.githubusercontent.com/FlamedDogo99/EaglerMobile/main/eaglermobile.user.js"
)

@dataclass
class DownloadedAssets:
    loader_name: str
    framework_name: str
    data_name: str
    wasm_name: str
    used_br_assets: bool
    build_kind: str = "modern"
    legacy_config: dict[str, Any] = field(default_factory=dict)
    legacy_asset_names: dict[str, str] = field(default_factory=dict)


@dataclass
class FrameworkAnalysis:
    required_functions: list[str]
    window_roots: list[str]
    window_callable_chains: list[str]
    requires_crazygames_sdk: bool


class FetchError(RuntimeError):
    pass


@dataclass
class DetectedBuild:
    build_kind: str
    index_url: str
    index_html: str
    loader_url: str
    candidates: dict[str, list[str]]
    legacy_config: dict[str, Any] = field(default_factory=dict)
    original_folder_url: str = ""
    streaming_assets_url: str = ""
    page_config: dict[str, Any] = field(default_factory=dict)


@dataclass
class DetectedEntry:
    entry_kind: str
    index_url: str
    index_html: str
    source_page_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DetectedEaglerEntry:
    title: str
    index_url: str
    index_html: str
    classes_url: str
    assets_url: str
    locales_url: str
    bootstrap_script: str
    script_urls: list[str] = field(default_factory=list)


def file_contains_any_bytes(path: Path, patterns: Sequence[bytes]) -> bool:
    if not path.exists() or not patterns:
        return False
    try:
        raw = read_maybe_decompressed_bytes(path)
    except OSError:
        return False
    return any(pattern in raw for pattern in patterns)


def maybe_decompress_bytes(raw: bytes, path: Path | None = None) -> bytes:
    if not raw:
        return raw
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    lower_name = path.name.lower() if path is not None else ""
    if lower_name.endswith((".br", ".unityweb")) and brotli is not None:
        try:
            return brotli.decompress(raw)
        except Exception:
            return raw
    return raw


def read_maybe_decompressed_bytes(path: Path) -> bytes:
    return maybe_decompress_bytes(path.read_bytes(), path)


def encode_bytes_like_source(data: bytes, original_raw: bytes, path: Path) -> bytes:
    lower_name = path.name.lower()
    if original_raw[:2] == b"\x1f\x8b":
        return gzip.compress(data, mtime=0)
    if lower_name.endswith(".br"):
        if brotli is None:
            raise RuntimeError(
                f"Cannot rewrite Brotli-compressed asset without brotli support: {path}"
            )
        return brotli.compress(data)
    return data


def patch_redirect_domain_function(framework_path: Path) -> Path | None:
    if not framework_path.exists():
        return None

    try:
        original_raw = framework_path.read_bytes()
    except OSError:
        return None

    decoded = maybe_decompress_bytes(original_raw, framework_path)
    legacy_original = (
        b"function _RedirectDomain(check_domains_str,redirect_domain){"
        b"var redirect=true;"
        b"var domains_string=Pointer_stringify(check_domains_str);"
        b"var redirect_domain_string=Pointer_stringify(redirect_domain);"
        b'var check_domains=domains_string.split("|");'
        b"for(var i=0;i<check_domains.length;i++){var domain=check_domains[i];if(document.location.host==domain){redirect=false}}"
        b"if(redirect){document.location=redirect_domain_string;return true}return false}"
    )
    legacy_replacement = (
        b"function _RedirectDomain(check_domains_str,redirect_domain){"
        b"var domains_string=Pointer_stringify(check_domains_str);"
        b'var source_host="";'
        b'try{if(typeof window!=="undefined"&&window.__unityStandaloneSourcePageUrl){source_host=(new URL(window.__unityStandaloneSourcePageUrl)).host}}catch(e){}'
        b"var current_host=source_host||document.location.host;"
        b'var check_domains=domains_string.split("|");'
        b"for(var i=0;i<check_domains.length;i++){var domain=check_domains[i];if(current_host==domain){return false}}"
        b"if(source_host){return false}"
        b"var redirect_domain_string=Pointer_stringify(redirect_domain);"
        b"document.location=redirect_domain_string;return true}"
    )
    modern_original = (
        b"function _RedirectDomain(check_domains_str,redirect_domain){"
        b"var redirect=true;"
        b"var domains_string=UTF8ToString(check_domains_str);"
        b"var redirect_domain_string=UTF8ToString(redirect_domain);"
        b'var check_domains=domains_string.split("|");'
        b"for(var i=0;i<check_domains.length;i++){var domain=check_domains[i];if(document.location.host==domain){redirect=false}}"
        b"if(redirect){document.location=redirect_domain_string;return true}return false}"
    )
    modern_replacement = (
        b"function _RedirectDomain(check_domains_str,redirect_domain){"
        b"var domains_string=UTF8ToString(check_domains_str);"
        b'var source_host="";'
        b'try{if(typeof window!=="undefined"&&window.__unityStandaloneSourcePageUrl){source_host=(new URL(window.__unityStandaloneSourcePageUrl)).host}}catch(e){}'
        b"var current_host=source_host||document.location.host;"
        b'var check_domains=domains_string.split("|");'
        b"for(var i=0;i<check_domains.length;i++){var domain=check_domains[i];if(current_host==domain){return false}}"
        b"if(source_host){return false}"
        b"var redirect_domain_string=UTF8ToString(redirect_domain);"
        b"document.location=redirect_domain_string;return true}"
    )

    patched_payload = decoded
    for original, replacement in (
        (legacy_original, legacy_replacement),
        (modern_original, modern_replacement),
    ):
        if original in patched_payload:
            patched_payload = patched_payload.replace(original, replacement, 1)

    if patched_payload == decoded:
        return None

    target_path = framework_path
    lower_name = framework_path.name.lower()
    if original_raw[:2] == b"\x1f\x8b" or lower_name.endswith(".br"):
        base_name = framework_path.name
        for suffix in (".unityweb", ".gz", ".br"):
            if base_name.lower().endswith(suffix):
                base_name = base_name[: -len(suffix)]
                break
        if not base_name.lower().endswith(".js"):
            base_name += ".js"
        target_path = framework_path.with_name(base_name)

    target_path.write_bytes(patched_payload)
    return target_path


def patch_gmsoft_host_bridge(framework_path: Path) -> bool:
    if not framework_path.exists():
        return False

    try:
        original_raw = framework_path.read_bytes()
    except OSError:
        return False

    decoded_bytes = maybe_decompress_bytes(original_raw, framework_path)
    try:
        decoded_text = decoded_bytes.decode("utf-8", errors="ignore")
    except UnicodeDecodeError:
        return False

    raw_hostname_expr = (
        r"""document['\x6c\x6f\x63\x61\x74\x69\x6f\x6e']"""
        r"""['\x68\x6f\x73\x74\x6e\x61\x6d\x65']"""
    )
    plain_hostname_expr = "document['location']['hostname']"
    raw_replacement = (
        r"""(window.__unityStandaloneLocalHostName||"""
        r"""document['\x6c\x6f\x63\x61\x74\x69\x6f\x6e']"""
        r"""['\x68\x6f\x73\x74\x6e\x61\x6d\x65'])"""
    )
    plain_replacement = (
        "(window.__unityStandaloneLocalHostName||document['location']['hostname'])"
    )

    if raw_replacement in decoded_text or plain_replacement in decoded_text:
        return False

    if raw_hostname_expr in decoded_text:
        patched_text = decoded_text.replace(raw_hostname_expr, raw_replacement, 1)
    elif plain_hostname_expr in decoded_text:
        patched_text = decoded_text.replace(plain_hostname_expr, plain_replacement, 1)
    else:
        return False

    patched = patched_text.encode("utf-8")
    try:
        framework_path.write_bytes(encode_bytes_like_source(patched, original_raw, framework_path))
    except (OSError, RuntimeError):
        return False
    return True


def patch_gmsoft_sendmessage_defaults(framework_path: Path) -> bool:
    if not framework_path.exists():
        return False

    try:
        original_raw = framework_path.read_bytes()
    except OSError:
        return False

    decoded_bytes = maybe_decompress_bytes(original_raw, framework_path)
    try:
        decoded_text = decoded_bytes.decode("utf-8", errors="ignore")
    except UnicodeDecodeError:
        return False

    if "__unityStandaloneGmSoftParamErr" in decoded_text:
        return False

    prefix_pattern = re.compile(
        r"""function (?P<fn>[_$A-Za-z0-9]+)\((?P<obj>[_$A-Za-z0-9]+),(?P<method>[_$A-Za-z0-9]+),(?P<arg>[_$A-Za-z0-9]+)\)\{"""
        r"""var (?P<methodptr>[_$A-Za-z0-9]+)=(?P<alloc>[_$A-Za-z0-9]+)\((?P=method)\),(?P<objptr>[_$A-Za-z0-9]+)=(?P=alloc)\((?P=obj)\),(?P<ptr>[_$A-Za-z0-9]+)=0x0;try\{"""
    )
    match = prefix_pattern.search(decoded_text)
    if not match:
        return False

    start = match.start()
    end = match.end()
    window_check = decoded_text[end : end + 1500]
    if "===undefined" not in window_check:
        return False

    obj_name = match.group("obj")
    method_name = match.group("method")
    arg_name = match.group("arg")
    injected = (
        match.group(0)
        + "if("
        + arg_name
        + "===undefined){"
        + "if("
        + obj_name
        + "==='GmSoft'&&"
        + method_name
        + "==='SetParam'){"
        + "try{"
        + arg_name
        + "=JSON.stringify(window.GMSOFT_OPTIONS||window.config||{});"
        + "}catch(__unityStandaloneGmSoftParamErr){"
        + arg_name
        + "='{}';}"
        + "}else if("
        + obj_name
        + "==='GmSoft'&&"
        + method_name
        + "==='SetUnityHostName'){"
        + arg_name
        + "=(window.__unityStandaloneLocalHostName||document['\\x6c\\x6f\\x63\\x61\\x74\\x69\\x6f\\x6e']['\\x68\\x6f\\x73\\x74\\x6e\\x61\\x6d\\x65']||'');"
        + "}"
        + "}"
    )
    patched_text = decoded_text[:start] + injected + decoded_text[end:]

    try:
        framework_path.write_bytes(
            encode_bytes_like_source(patched_text.encode("utf-8"), original_raw, framework_path)
        )
    except (OSError, RuntimeError):
        return False
    return True


def patch_sendmessage_value_compat(framework_path: Path) -> bool:
    if not framework_path.exists():
        return False

    try:
        original_raw = framework_path.read_bytes()
    except OSError:
        return False

    decoded_bytes = maybe_decompress_bytes(original_raw, framework_path)
    try:
        decoded_text = decoded_bytes.decode("utf-8", errors="ignore")
    except UnicodeDecodeError:
        return False

    pattern = re.compile(
        r"""else\{if\(typeof (?P<arg>[_$A-Za-z0-9]+)===(?:'\\x73\\x74\\x72\\x69\\x6e\\x67'|'string'|"string")\)"""
        r"""(?P<ptr>[_$A-Za-z0-9]+)=(?P<alloc>[_$A-Za-z0-9]+)\((?P=arg)\),(?P<sendstr>[_$A-Za-z0-9]+)\((?P<objptr>[_$A-Za-z0-9]+),(?P<methodptr>[_$A-Za-z0-9]+),(?P=ptr)\);"""
        r"""else\{if\(typeof (?P=arg)===(?:'\\x6e\\x75\\x6d\\x62\\x65\\x72'|'number'|"number")\)(?P<sendnum>[_$A-Za-z0-9]+)\((?P=objptr),(?P=methodptr),(?P=arg)\);"""
        r"""else throw''\+(?P=arg)\+(?P<msg>'(?:\\x[0-9a-fA-F]{2}|[^'])*'|"(?:\\x[0-9a-fA-F]{2}|[^"])*");\}\}"""
    )
    match = pattern.search(decoded_text)
    if not match:
        return False

    arg = match.group("arg")
    ptr = match.group("ptr")
    alloc = match.group("alloc")
    sendstr = match.group("sendstr")
    objptr = match.group("objptr")
    methodptr = match.group("methodptr")
    sendnum = match.group("sendnum")
    replacement = (
        "else{if(typeof "
        + arg
        + "==='string')"
        + ptr
        + "="
        + alloc
        + "("
        + arg
        + "),"
        + sendstr
        + "("
        + objptr
        + ","
        + methodptr
        + ","
        + ptr
        + ");else{if(typeof "
        + arg
        + "==='number')"
        + sendnum
        + "("
        + objptr
        + ","
        + methodptr
        + ","
        + arg
        + ");else{try{if("
        + arg
        + "!==null&&typeof "
        + arg
        + "==='object'){"
        + arg
        + "=JSON.stringify("
        + arg
        + ");}else{"
        + arg
        + "=''+"
        + arg
        + ";}}catch(__unityStandaloneSendMessageErr){"
        + arg
        + "=''+"
        + arg
        + ";}"
        + ptr
        + "="
        + alloc
        + "("
        + arg
        + "),"
        + sendstr
        + "("
        + objptr
        + ","
        + methodptr
        + ","
        + ptr
        + ");}}}"
    )
    patched_text = decoded_text[: match.start()] + replacement + decoded_text[match.end() :]

    try:
        framework_path.write_bytes(
            encode_bytes_like_source(patched_text.encode("utf-8"), original_raw, framework_path)
        )
    except (OSError, RuntimeError):
        return False
    return True


def log(message: str) -> None:
    print(f"[unity-standalone] {message}", flush=True)


def load_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_json_file(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp_path.replace(path)


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise FetchError("Empty URL.")
    expanded = os.path.expandvars(os.path.expanduser(url.strip('"')))
    if "://" not in url:
        local_candidate = Path(expanded)
        if local_candidate.exists():
            return local_candidate.resolve().as_uri()
    if "://" not in url:
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        local_path_text = urllib.request.url2pathname(parsed.path or "")
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            local_path_text = f"\\\\{parsed.netloc}{local_path_text}"
        local_candidate = Path(local_path_text)
        if local_candidate.exists():
            return local_candidate.resolve().as_uri()
        raise FetchError(f"Local file does not exist: {local_candidate}")
    if parsed.scheme not in {"http", "https"}:
        raise FetchError(f"Unsupported URL scheme: {parsed.scheme}")
    path = urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/:@%+")
    query = urllib.parse.quote(urllib.parse.unquote(parsed.query), safe="=&:@%+/,;[]-_.~")
    fragment = urllib.parse.quote(urllib.parse.unquote(parsed.fragment), safe="=&:@%+/,;[]-_.~")
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, path, parsed.params, query, fragment)
    )


def derive_game_root_url(input_url: str) -> str:
    parsed = urllib.parse.urlparse(input_url)
    path = parsed.path or "/"

    if "/Build/" in path:
        root_path = path.split("/Build/", 1)[0] + "/"
    else:
        last_segment = path.rsplit("/", 1)[-1]
        if "." in last_segment:
            root_path = path.rsplit("/", 1)[0] + "/"
        else:
            root_path = path if path.endswith("/") else path + "/"

    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, root_path, "", "", ""))


def origin_root_url(url: str) -> str:
    parsed = urllib.parse.urlparse(normalize_url(url))
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))


def fetch_url(
    url: str,
    timeout: int = 30,
    referer_url: str = "",
) -> tuple[str, bytes, str, str]:
    parsed = urllib.parse.urlparse(url)
    fallback_referer = (
        urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))
        if parsed.scheme in {"http", "https"} and parsed.netloc
        else ""
    )
    referer_candidates: list[str] = []
    if referer_url:
        referer_candidates.append(referer_url)
    else:
        referer_candidates.append("")
        if fallback_referer:
            referer_candidates.append(fallback_referer)

    last_error: Exception | None = None
    for referer_index, request_referer in enumerate(referer_candidates):
        headers = dict(REQUEST_HEADERS)
        if request_referer:
            headers["Referer"] = request_referer
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                resolved_url = response.geturl()
                body = response.read()
                content_type = response.headers.get_content_type() or ""
                content_encoding = (response.headers.get("Content-Encoding") or "").lower()
                return resolved_url, body, content_type, content_encoding
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 403 and not referer_url and referer_index == 0 and fallback_referer:
                continue
            raise FetchError(f"{url} -> HTTP {exc.code}") from exc
        except http.client.InvalidURL as exc:
            last_error = exc
            raise FetchError(f"{url} -> {exc}") from exc
        except ValueError as exc:
            last_error = exc
            raise FetchError(f"{url} -> {exc}") from exc
        except urllib.error.URLError as exc:
            last_error = exc
            raise FetchError(f"{url} -> {exc.reason}") from exc

    if isinstance(last_error, urllib.error.HTTPError):
        raise FetchError(f"{url} -> HTTP {last_error.code}") from last_error
    if isinstance(last_error, urllib.error.URLError):
        raise FetchError(f"{url} -> {last_error.reason}") from last_error
    raise FetchError(f"{url} -> request failed")


def looks_like_html(raw: bytes) -> bool:
    sample = raw[:512].lower()
    return sample.startswith(b"<!doctype html") or b"<html" in sample


def extract_single_html_from_zip_payload(raw: bytes) -> tuple[str, str]:
    if not raw.startswith(b"PK\x03\x04"):
        return "", ""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            html_names = [name for name in archive.namelist() if name.lower().endswith((".html", ".htm"))]
            if not html_names:
                return "", ""
            preferred_name = html_names[0]
            preferred_score = -1
            preferred_html = ""
            for name in html_names:
                try:
                    candidate_html = decode_html_body(archive.read(name))
                except KeyError:
                    continue
                score = 0
                lower = candidate_html.lower()
                if "window.eaglercraftxopts" in lower:
                    score += 5
                if "<html" in lower:
                    score += 2
                if score > preferred_score:
                    preferred_name = name
                    preferred_score = score
                    preferred_html = candidate_html
            return preferred_name, preferred_html
    except (zipfile.BadZipFile, OSError):
        return "", ""


CRAZYGAMES_LOCALE_MAP = {
    "ar": "ar_SA",
    "br": "pt_BR",
    "cs": "cs_CZ",
    "cz": "cs_CZ",
    "da": "da_DK",
    "de": "de_DE",
    "dk": "da_DK",
    "el": "el_GR",
    "en": "en_US",
    "es": "es_ES",
    "fi": "fi_FI",
    "fr": "fr_FR",
    "gr": "el_GR",
    "hu": "hu_HU",
    "id": "id_ID",
    "it": "it_IT",
    "ja": "ja_JP",
    "jp": "ja_JP",
    "ko": "ko_KR",
    "kr": "ko_KR",
    "nb": "nb_NO",
    "nl": "nl_NL",
    "no": "nb_NO",
    "pl": "pl_PL",
    "pt": "pt_BR",
    "ro": "ro_RO",
    "ru": "ru_RU",
    "se": "sv_SE",
    "sv": "sv_SE",
    "th": "th_TH",
    "tr": "tr_TR",
    "ua": "uk_UA",
    "uk": "uk_UA",
    "vi": "vi_VN",
    "vn": "vi_VN",
}


def candidate_index_urls(input_url: str, root_url: str) -> list[str]:
    candidates = []

    parsed_input = urllib.parse.urlparse(input_url)
    if parsed_input.path and "." in parsed_input.path.rsplit("/", 1)[-1]:
        candidates.append(input_url)

    candidates.append(root_url)
    candidates.append(urllib.parse.urljoin(root_url, "index.html"))

    # Keep order, remove duplicates.
    deduped: list[str] = []
    seen = set()
    for candidate in candidates:
        if candidate not in seen:
            deduped.append(candidate)
            seen.add(candidate)
    return deduped


def decode_html_body(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def decode_js_string_literal(raw_value: str) -> str:
    cleaned = raw_value.replace("\\/", "/")
    try:
        decoded = bytes(cleaned, encoding="utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        decoded = cleaned
    return (
        decoded.replace('\\"', '"')
        .replace("\\'", "'")
        .replace("\\/", "/")
    )


def decode_embedded_html_payload(raw_value: str) -> str:
    decoded = html.unescape(decode_js_string_literal(raw_value))
    parts = re.split(r"(<script\b[^>]*>[\s\S]*?</script>)", decoded, flags=re.IGNORECASE)
    normalized_parts: list[str] = []
    for part in parts:
        if re.match(r"<script\b", part, re.IGNORECASE):
            tag_end = part.find(">")
            close_start = part.lower().rfind("</script>")
            if tag_end != -1 and close_start != -1 and close_start >= tag_end:
                script_head = part[: tag_end + 1]
                script_body = part[tag_end + 1 : close_start]
                script_tail = part[close_start:]
                normalized_parts.append(
                    script_head
                    + normalize_embedded_script_source(script_body)
                    + script_tail
                )
            else:
                normalized_parts.append(part)
            continue
        normalized_parts.append(
            part.replace("\\r\\n", "\n")
            .replace("\\n", "\n")
            .replace("\\r", "\n")
            .replace("\\t", "\t")
        )
    return "".join(normalized_parts)


def normalize_embedded_script_source(script_source: str) -> str:
    output: list[str] = []
    index = 0
    length = len(script_source)
    state = "default"
    escape_active = False

    while index < length:
        char = script_source[index]
        next_char = script_source[index + 1] if index + 1 < length else ""

        if state == "default":
            if char == "/" and next_char == "/":
                output.append("//")
                index += 2
                state = "line_comment"
                continue
            if char == "/" and next_char == "*":
                output.append("/*")
                index += 2
                state = "block_comment"
                continue
            if char == "'":
                output.append(char)
                index += 1
                state = "single"
                escape_active = False
                continue
            if char == '"':
                output.append(char)
                index += 1
                state = "double"
                escape_active = False
                continue
            if char == "`":
                output.append(char)
                index += 1
                state = "template"
                escape_active = False
                continue
            if char == "\\" and next_char in ("n", "r", "t"):
                output.append("\t" if next_char == "t" else "\n")
                index += 2
                continue
            output.append(char)
            index += 1
            continue

        if state == "line_comment":
            if char == "\\" and next_char in ("n", "r"):
                output.append("\n")
                index += 2
                state = "default"
                continue
            output.append(char)
            index += 1
            if char == "\n":
                state = "default"
            continue

        if state == "block_comment":
            if char == "\\" and next_char in ("n", "r", "t"):
                output.append("\t" if next_char == "t" else "\n")
                index += 2
                continue
            output.append(char)
            index += 1
            if char == "*" and next_char == "/":
                output.append("/")
                index += 1
                state = "default"
            continue

        output.append(char)
        index += 1
        if escape_active:
            escape_active = False
            continue
        if char == "\\":
            escape_active = True
            continue
        if state == "single" and char == "'":
            state = "default"
            continue
        if state == "double" and char == '"':
            state = "default"
            continue
        if state == "template" and char == "`":
            state = "default"
            continue

    return "".join(output)


def looks_like_unity_entry_html(index_html: str) -> bool:
    return (
        ".loader.js" in index_html
        or "createUnityInstance" in index_html
        or "UnityLoader.instantiate" in index_html
    )


def looks_like_custom_unity_html_bootstrap(index_html: str) -> bool:
    lower = index_html.lower()
    return (
        "<html" in lower
        and "<canvas" in lower
        and "startunitybr" in lower
        and ("innerloaderurl" in lower or "dataparturls" in lower or "buildurl" in lower)
        and "unityloader.instantiate" not in lower
    )


def split_js_top_level(source: str, separator: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    escape_active = False
    depth = 0
    for char in source:
        if quote:
            current.append(char)
            if escape_active:
                escape_active = False
            elif char == "\\":
                escape_active = True
            elif char == quote:
                quote = ""
            continue
        if char in ("'", '"', "`"):
            quote = char
            current.append(char)
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}" and depth > 0:
            depth -= 1
        if char == separator and depth == 0:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def evaluate_simple_js_concat_expression(expression: str, variables: Mapping[str, str]) -> str:
    value_parts: list[str] = []
    for token in split_js_top_level(expression.strip().rstrip(";"), "+"):
        token = token.strip()
        if not token:
            continue
        if token in variables:
            value_parts.append(variables[token])
            continue
        if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
            value_parts.append(decode_js_string_literal(token[1:-1]))
            continue
        return ""
    return "".join(value_parts).strip()


def strip_custom_split_suffix(name: str) -> str:
    cleaned = name
    if re.search(r"\.\d{2,4}$", cleaned):
        cleaned = cleaned.rsplit(".", 1)[0]
    for suffix in (".unityweb", ".br", ".gz"):
        if cleaned.lower().endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    return cleaned


def infer_custom_split_data_name(data_part_urls: Sequence[str]) -> str:
    if not data_part_urls:
        return "game.data"
    base = strip_custom_split_suffix(basename_from_url(data_part_urls[0]))
    if not base:
        return "game.data"
    if "." not in base:
        base += ".data"
    return sanitize_filename(base, "game.data")


def infer_custom_split_wasm_name(wasm_url: str) -> str:
    base = strip_custom_split_suffix(basename_from_url(wasm_url))
    if not base:
        base = "game"
    if not base.lower().endswith(".wasm"):
        base += ".wasm"
    return sanitize_filename(base, "game.wasm")


def extract_custom_split_unity_bootstrap(
    index_html: str,
    index_url: str,
) -> dict[str, Any] | None:
    if not looks_like_custom_unity_html_bootstrap(index_html):
        return None

    variables: dict[str, str] = {}
    build_url_matches = list(
        re.finditer(
            r"""\b(?:const|let|var)\s+buildUrl\s*=\s*([^;]+);""",
            index_html,
            re.IGNORECASE,
        )
    )
    build_url_match = build_url_matches[-1] if build_url_matches else None
    if build_url_match:
        build_url_value = evaluate_simple_js_concat_expression(build_url_match.group(1), variables)
        if build_url_value:
            variables["buildUrl"] = normalize_url(urllib.parse.urljoin(index_url, build_url_value))

    wasm_matches = list(
        re.finditer(
            r"""\b(?:const|let|var)\s+wasmUrl\s*=\s*([^;]+);""",
            index_html,
            re.IGNORECASE,
        )
    )
    wasm_match = wasm_matches[-1] if wasm_matches else None
    data_parts_matches = list(
        re.finditer(
            r"""\b(?:const|let|var)\s+dataPartUrls\s*=\s*\[(.*?)\]\s*;""",
            index_html,
            re.IGNORECASE | re.DOTALL,
        )
    )
    data_parts_match = data_parts_matches[-1] if data_parts_matches else None
    loader_expr_matches = list(
        re.finditer(
            r"""\binnerLoaderUrl\s*:\s*([^,\n}]+)""",
            index_html,
            re.IGNORECASE,
        )
    )
    loader_expr_match = loader_expr_matches[-1] if loader_expr_matches else None
    framework_expr_matches = list(
        re.finditer(
            r"""\bframeworkUrl\s*:\s*([^,\n}]+)""",
            index_html,
            re.IGNORECASE,
        )
    )
    framework_expr_match = framework_expr_matches[-1] if framework_expr_matches else None
    streaming_assets_matches = list(
        re.finditer(
            r"""\bstreamingAssetsUrl\s*:\s*([^,\n}]+)""",
            index_html,
            re.IGNORECASE,
        )
    )
    streaming_assets_match = streaming_assets_matches[-1] if streaming_assets_matches else None

    if not (wasm_match and data_parts_match and loader_expr_match and framework_expr_match):
        return None

    wasm_url = evaluate_simple_js_concat_expression(wasm_match.group(1), variables)
    loader_url = evaluate_simple_js_concat_expression(loader_expr_match.group(1), variables)
    framework_url = evaluate_simple_js_concat_expression(framework_expr_match.group(1), variables)
    streaming_assets_url = (
        evaluate_simple_js_concat_expression(streaming_assets_match.group(1), variables)
        if streaming_assets_match
        else ""
    )
    data_part_urls = [
        evaluate_simple_js_concat_expression(item, variables)
        for item in split_js_top_level(data_parts_match.group(1), ",")
    ]
    data_part_urls = [url for url in data_part_urls if url]

    if not (wasm_url and loader_url and framework_url and data_part_urls):
        return None

    return {
        "build_root_url": variables.get("buildUrl", ""),
        "loader_url": normalize_url(urllib.parse.urljoin(index_url, loader_url)),
        "framework_url": normalize_url(urllib.parse.urljoin(index_url, framework_url)),
        "wasm_url": normalize_url(urllib.parse.urljoin(index_url, wasm_url)),
        "data_part_urls": [
            normalize_url(urllib.parse.urljoin(index_url, url))
            for url in data_part_urls
        ],
        "streaming_assets_url": (
            normalize_url(urllib.parse.urljoin(index_url, streaming_assets_url))
            if streaming_assets_url
            else ""
        ),
    }


def looks_like_embedded_game_wrapper_html(index_html: str) -> bool:
    lower = index_html.lower()
    wrapper_markers = (
        "_docs_flag_initialdata",
        "sites-viewer-frontend",
        "goog.script.init(",
        'id="sandboxframe"',
        "id='sandboxframe'",
        "innerframegapiinitialized",
        "updateuserhtmlframe(",
        "googleScriptUrl=".lower(),
        "gameXmlUrl=".lower(),
        '"sandboxhost"',
    )
    return any(marker in lower for marker in wrapper_markers)


def looks_like_split_unity_bootstrap_page(index_html: str) -> bool:
    lower = index_html.lower()
    return (
        looks_like_unity_entry_html(index_html)
        and ("fetchandcombineparts" in lower or "dataparts" in lower or "wasmparts" in lower)
        and ("dataurl: \"\"" in lower or "codeurl: \"\"" in lower)
    )


def looks_like_eagler_entry_html(index_html: str) -> bool:
    lower = index_html.lower()
    return (
        "window.eaglercraftxopts" in lower
        or (
            re.search(r"classes(?:\\.min)?\\.js", lower) is not None
            and "assets.epk" in lower
        )
        or ("eaglercraft" in lower and "main();" in lower and "game_frame" in lower)
    )


def looks_like_inline_eagler_payload_html(index_html: str) -> bool:
    if not looks_like_eagler_entry_html(index_html):
        return False
    lower = index_html.lower()
    has_external_scripts = re.search(r"""<script[^>]+\bsrc=["'][^"']+["']""", index_html, re.IGNORECASE)
    has_inline_runtime = "var main;(function(){" in lower or "$rt_seed" in lower
    has_data_assets = "assetsuri" in lower and "data:application/octet-stream;base64," in lower
    has_signed_inline_bundle = (
        "eaglercraftxclientbundle" in lower
        or "eaglercraftxclientsignature" in lower
        or "eaglercraftxoptshints" in lower
    )
    return not has_external_scripts and (
        has_inline_runtime or has_data_assets or has_signed_inline_bundle
    )


def looks_like_html_game_entry_html(index_html: str) -> bool:
    lower = index_html.lower()
    if ("<html" not in lower and "<body" not in lower) or looks_like_unity_entry_html(index_html):
        return False
    if looks_like_eagler_entry_html(index_html):
        return False
    if any(
        marker in lower
        for marker in (
            "_docs_flag_initialdata",
            "sites-viewer-frontend",
            "goog.script.init(",
            "id=\"sandboxframe\"",
            "id='sandboxframe'",
            "innerframegapiinitialized",
            "updateuserhtmlframe(",
        )
    ):
        return False

    score = 0
    if re.search(r"<script\b[^>]*\bsrc\s*=", index_html, re.IGNORECASE):
        score += 2
    if "<canvas" in lower:
        score += 3
    if "touch-action: none" in lower or "touch-action:none" in lower:
        score += 1
    if "overflow: hidden" in lower or "overflow:hidden" in lower:
        score += 1
    if "position: fixed" in lower or "position: absolute" in lower:
        score += 1
    if any(
        marker in lower
        for marker in (
            "gamesnacks.js",
            "voodoo-h5sdk",
            "mpconfig",
            "miniplay",
            "phaser",
            "pixi",
            "c3runtime",
            "playcanvas",
            "babylon",
        )
    ):
        score += 2
    if "<iframe" in lower and "<canvas" not in lower and "<script" not in lower:
        score -= 3
    return score >= 4


def is_ignored_embedded_url(url: str) -> bool:
    lower = url.lower()
    ignored_fragments = (
        "about:blank",
        "amazon-adsystem.com",
        "bugpilot.now.gg",
        "cloudflareinsights.com",
        "doubleclick.net",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "google.com/recaptcha",
        "googlesyndication.com",
        "googletagmanager.com",
        "google-analytics.com",
        "facebook.com/sharer",
        "gstatic.com/",
        "linkedin.com/share",
        "openx.net",
        "pbs-cs.",
        "pubads.g.doubleclick.net",
        "apis.google.com/js/api.js",
        "lh3.googleusercontent.com",
        "reddit.com/submit",
        "sites.google.com/u/",
        "twitter.com/intent",
        "whatsapp.com/send",
        "x.com/intent",
    )
    ignored_suffixes = (
        ".css",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".webp",
        ".woff",
        ".woff2",
        ".ttf",
        ".map",
    )
    if lower.startswith("data:"):
        return True
    if any(fragment in lower for fragment in ignored_fragments):
        return True
    if lower.endswith(ignored_suffixes):
        return True
    if lower.endswith(".js") and "unityloader.js" not in lower and ".loader.js" not in lower:
        return True
    return False


def extract_embedded_html_snippets(index_html: str) -> list[str]:
    snippets: list[str] = []
    source_variants = [index_html]
    unescaped_index_html = html.unescape(index_html)
    if unescaped_index_html != index_html:
        source_variants.append(unescaped_index_html)

    for source_html in source_variants:
        for raw in re.findall(r"<!\[CDATA\[([\s\S]*?)\]\]>", source_html, re.IGNORECASE):
            decoded = raw.strip()
            if decoded:
                snippets.append(decoded)

    for source_html in source_variants:
        for raw in re.findall(r'data-code="([\s\S]*?)"', source_html, re.IGNORECASE):
            decoded = html.unescape(raw).strip()
            if decoded:
                snippets.append(decoded)

    user_html_patterns = (
        r'userHtml\\x22:\s*\\x22([\s\S]*?)\\x22,\s*\\x22ncc\\x22',
        r'"userHtml"\s*:\s*"([\s\S]*?)"\s*,\s*"ncc"',
    )
    for source_html in source_variants:
        for pattern in user_html_patterns:
            for raw in re.findall(pattern, source_html, re.IGNORECASE):
                decoded = decode_embedded_html_payload(raw).strip()
                if decoded:
                    snippets.append(decoded)

    inline_data_url_patterns = (
        r"""data:(?:@file/xml|text/xml|application/xml|text/html|application/xhtml\+xml)[^"'&<>\s]+""",
    )
    for source_html in source_variants:
        for pattern in inline_data_url_patterns:
            for raw in re.findall(pattern, source_html, re.IGNORECASE):
                try:
                    decoded_bytes = decode_data_url_bytes(raw)
                except FetchError:
                    continue
                decoded = decoded_bytes.decode("utf-8", errors="replace").strip()
                if decoded:
                    snippets.append(decoded)

    deduped: list[str] = []
    seen = set()
    for snippet in snippets:
        if snippet not in seen:
            deduped.append(snippet)
            seen.add(snippet)

    def snippet_priority(snippet: str) -> tuple[int, int]:
        lower = snippet.lower()
        score = 0
        if "script.google.com/macros" in lower:
            score += 100
        if "<module>" in lower or "<content type=\"html\">" in lower or "gamexmlurl" in lower:
            score += 90
        if "unityloader.instantiate" in lower or ".loader.js" in lower:
            score += 80
        if "createunityinstance" in lower:
            score += 60
        if "default_url" in lower:
            score += 25
        if "file_url" in lower or ".xml" in lower:
            score -= 20
        return (-score, len(snippet))

    deduped.sort(key=snippet_priority)
    return deduped


def detect_supported_entry_kind(index_html: str) -> str:
    if looks_like_embedded_game_wrapper_html(index_html):
        return ""
    if looks_like_custom_unity_html_bootstrap(index_html):
        return "html"
    if looks_like_unity_entry_html(index_html):
        return "unity"
    if looks_like_eagler_entry_html(index_html):
        return "eaglercraft"
    if looks_like_html_game_entry_html(index_html):
        return "html"
    return ""


def extract_crazygames_loader_entry_url(index_html: str, index_url: str) -> str:
    patterns = (
        r'''"loaderOptions"\s*:\s*\{\s*"url"\s*:\s*"([^"]+)"''',
        r"""loaderOptions\s*:\s*\{\s*url\s*:\s*["']([^"']+)["']""",
        r'''window\.gameOptions\s*=\s*\{[\s\S]*?"url"\s*:\s*"([^"]+)"''',
    )
    for pattern in patterns:
        match = re.search(pattern, index_html, re.IGNORECASE)
        if not match:
            continue
        candidate = decode_js_string_literal(html.unescape(match.group(1))).strip()
        if candidate:
            return normalize_url(urllib.parse.urljoin(index_url, candidate))
    return ""


def extract_crazygames_slug_from_url(index_url: str) -> str:
    parsed = urllib.parse.urlparse(index_url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        return ""
    try:
        game_index = segments.index("game")
    except ValueError:
        return ""
    if game_index + 1 >= len(segments):
        return ""
    return segments[game_index + 1].strip()


def crazygames_locale_candidates(index_url: str) -> list[str]:
    parsed = urllib.parse.urlparse(index_url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    candidates: list[str] = []
    seen: set[str] = set()

    def add(locale: str) -> None:
        normalized = locale.strip()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        candidates.append(normalized)

    if segments:
        add(CRAZYGAMES_LOCALE_MAP.get(segments[0].lower(), ""))
    add("en_US")
    return candidates


def discover_crazygames_entry_url(index_html: str, index_url: str) -> str:
    parsed = urllib.parse.urlparse(index_url)
    host = parsed.netloc.lower()
    if host.startswith("games.crazygames.com"):
        return extract_crazygames_loader_entry_url(index_html, index_url)

    if not host.endswith("crazygames.com"):
        return ""

    slug = extract_crazygames_slug_from_url(index_url)
    if not slug:
        return ""

    last_error = None
    for locale in crazygames_locale_candidates(index_url):
        candidate_url = f"https://games.crazygames.com/{locale}/{urllib.parse.quote(slug)}/index.html"
        try:
            resolved, raw, _, _ = fetch_url(candidate_url, referer_url=index_url)
        except FetchError as exc:
            last_error = exc
            continue
        if not raw:
            continue
        candidate_html = decode_html_body(raw)
        entry_url = extract_crazygames_loader_entry_url(candidate_html, resolved)
        if entry_url:
            return entry_url
        detected_kind = detect_supported_entry_kind(candidate_html)
        if detected_kind:
            return resolved

    return ""


def discover_gamecomets_entry_url(index_url: str) -> str:
    parsed = urllib.parse.urlparse(index_url)
    host = parsed.netloc.lower()
    if not host.endswith("gamecomets.com"):
        return ""

    path = parsed.path.rstrip("/").lower()
    if path in {"/game/geometry-dash-lite", "/games/geometry-dash-lite"}:
        return "https://geometrydashlite.io/geometry-dash-game/"

    return ""


def discover_eagler_entry_override_url(index_html: str, index_url: str) -> str:
    parsed = urllib.parse.urlparse(index_url)
    host = parsed.netloc.lower()
    normalized_path = parsed.path.lower().rstrip("/")
    lower_html = index_html.lower()

    # This mirror strips singleplayer entirely; prefer a singleplayer-capable build.
    if (
        "singleplayer was removed dumbass" in lower_html
        or (
            host in {"raw.githubusercontent.com", "github.com"}
            and "/vidio-boy/eaglercraft1.8.8/" in normalized_path
            and normalized_path.endswith("/eaglercraft.1.8.8.html")
        )
    ):
        return (
            "https://raw.githubusercontent.com/"
            "srzmnx/eaglerforge-compiled/main/"
            "EaglercraftX_1.8_Offline_International.html"
        )

    return ""


def extract_next_data_payload(index_html: str) -> dict[str, Any]:
    match = re.search(
        r"""<script[^>]+id=["']__NEXT_DATA__["'][^>]*>([\s\S]*?)</script>""",
        index_html,
        re.IGNORECASE,
    )
    if not match:
        return {}
    raw_payload = html.unescape(match.group(1)).strip()
    if not raw_payload:
        return {}
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def extract_nowgg_entry_metadata(index_html: str, index_url: str) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(index_url)
    if not parsed.netloc.lower().endswith("now.gg"):
        return {}

    next_data = extract_next_data_payload(index_html)
    if not next_data:
        return {}

    page_props = next_data.get("props", {}).get("pageProps", {})
    if not isinstance(page_props, dict):
        return {}

    app_info = page_props.get("appInfo", {})
    if not isinstance(app_info, dict):
        app_info = {}
    developer_info = app_info.get("appDeveloperInfo", {})
    if not isinstance(developer_info, dict):
        developer_info = {}

    app_page_specific_data = page_props.get("appPageSpecificData", {})
    if not isinstance(app_page_specific_data, dict):
        app_page_specific_data = {}
    app_package_info = app_page_specific_data.get("appPackageInfo", {})
    if not isinstance(app_package_info, dict):
        app_package_info = {}
    app_package_data = app_package_info.get("data", {})
    if not isinstance(app_package_data, dict):
        app_package_data = {}

    direct_candidates: list[tuple[int, str]] = []
    seen_direct_candidates: set[str] = set()

    def add_candidate(raw_url: Any, score: int) -> None:
        if not isinstance(raw_url, str):
            return
        candidate = raw_url.strip()
        if not candidate:
            return
        try:
            absolute = normalize_url(urllib.parse.urljoin(index_url, candidate))
        except FetchError:
            return
        if absolute == normalize_url(index_url):
            return
        parsed_candidate = urllib.parse.urlparse(absolute)
        if parsed_candidate.netloc.lower() != parsed.netloc.lower():
            return
        if absolute in seen_direct_candidates:
            return
        seen_direct_candidates.add(absolute)
        direct_candidates.append((score, absolute))

    add_candidate(app_package_data.get("html_game_url"), 300)
    add_candidate(app_info.get("embeddedGameUrl"), 260)
    add_candidate(app_package_data.get("play_theme_url"), 220)

    def add_heuristic_candidates(container: Mapping[str, Any], base_score: int) -> None:
        for key, value in container.items():
            if not isinstance(value, str):
                continue
            key_lower = key.lower()
            if "url" not in key_lower:
                continue
            if any(token in key_lower for token in ("apppage", "canonical", "share")):
                continue
            if any(token in key_lower for token in ("html", "game", "embed", "iframe", "entry", "play")):
                add_candidate(value, base_score)

    add_heuristic_candidates(app_package_data, 160)
    add_heuristic_candidates(app_info, 120)
    direct_candidates.sort(key=lambda item: (-item[0], item[1]))

    return {
        "remote_provider": "now.gg",
        "app_id": str(app_info.get("appId") or "").strip(),
        "app_name": str(app_info.get("appName") or "").strip(),
        "app_slug": str(app_info.get("appSlug") or "").strip(),
        "app_type": str(app_info.get("appType") or "").strip(),
        "package_name": str(app_info.get("packageName") or "").strip(),
        "developer_slug": str(developer_info.get("developerSlug") or "").strip(),
        "play_domain": str(app_info.get("playDomain") or "").strip(),
        "enable_play_page": bool(app_info.get("enablePlayPage")),
        "app_page_url": str(app_info.get("appPageUrl") or "").strip(),
        "direct_candidate_urls": [item[1] for item in direct_candidates],
    }


def discover_nowgg_entry(index_html: str, index_url: str) -> DetectedEntry | None:
    metadata = extract_nowgg_entry_metadata(index_html, index_url)
    if not metadata:
        return None

    direct_candidate_urls = metadata.get("direct_candidate_urls", [])
    if not isinstance(direct_candidate_urls, list):
        direct_candidate_urls = []

    for candidate_url in direct_candidate_urls:
        if not isinstance(candidate_url, str) or not candidate_url:
            continue
        try:
            resolved_url, raw, _, _ = fetch_url(candidate_url, referer_url=index_url)
        except FetchError:
            continue
        if not raw:
            continue
        candidate_text = decode_html_body(raw)
        candidate_kind = detect_supported_entry_kind(candidate_text)
        if candidate_kind:
            return DetectedEntry(
                entry_kind=candidate_kind,
                index_url=resolved_url,
                index_html=candidate_text,
                source_page_url=index_url,
                metadata=dict(metadata),
            )
        external_unity_entry = detect_unity_entry_from_external_scripts(candidate_text, resolved_url)
        if external_unity_entry is not None:
            return DetectedEntry(
                entry_kind=external_unity_entry.entry_kind,
                index_url=external_unity_entry.index_url,
                index_html=external_unity_entry.index_html,
                source_page_url=index_url,
                metadata=dict(metadata),
            )

    remote_metadata = dict(metadata)
    remote_metadata["remote_url"] = metadata.get("app_page_url") or index_url
    remote_metadata["remote_kind"] = "webrtc_stream"
    remote_metadata["remote_stream_reason"] = (
        "now.gg app page does not expose a same-origin downloadable HTML payload"
    )
    return DetectedEntry(
        entry_kind="remote_stream",
        index_url=index_url,
        index_html=index_html,
        source_page_url=index_url,
        metadata=remote_metadata,
    )


def detect_unity_entry_from_external_scripts(
    index_html: str,
    index_url: str,
) -> DetectedEntry | None:
    script_urls = sorted(
        extract_external_script_urls(index_html, index_url),
        key=lambda script_url: score_external_script_url(script_url, index_url),
    )
    for script_url in script_urls[:12]:
        if is_ignored_external_script_url(script_url):
            continue
        try:
            resolved_script_url, raw_script, _, _ = fetch_url(script_url, referer_url=index_url)
        except FetchError:
            continue
        if not raw_script or looks_like_html(raw_script):
            continue
        script_text = decode_html_body(raw_script)
        if not looks_like_unity_entry_html(script_text):
            continue
        return DetectedEntry(
            entry_kind="unity",
            index_url=resolved_script_url,
            index_html=script_text,
            source_page_url=index_url,
        )
    return None


def find_supported_entry(input_url: str, root_url: str) -> DetectedEntry:
    errors: list[str] = []
    visited_urls: set[str] = set()
    visited_snippets: set[str] = set()

    def inspect_html(index_url: str, index_html: str, depth: int, source: str) -> DetectedEntry | None:
        snippets = extract_embedded_html_snippets(index_html)
        inline_html_snippets: list[str] = []

        for snippet in snippets:
            detected_kind = detect_supported_entry_kind(snippet)
            if detected_kind in {"unity", "eaglercraft", "remote_stream"}:
                return DetectedEntry(
                    entry_kind=detected_kind,
                    index_url=index_url,
                    index_html=snippet,
                    source_page_url=index_url,
                )
            if detected_kind == "html":
                inline_html_snippets.append(snippet)

        gamecomets_entry_url = discover_gamecomets_entry_url(index_url)
        if gamecomets_entry_url:
            result = inspect_url(gamecomets_entry_url, depth + 1, referer_url=index_url)
            if result:
                return result

        crazygames_entry_url = discover_crazygames_entry_url(index_html, index_url)
        if crazygames_entry_url:
            result = inspect_url(crazygames_entry_url, depth + 1, referer_url=index_url)
            if result:
                return result

        eagler_override_entry_url = discover_eagler_entry_override_url(index_html, index_url)
        if eagler_override_entry_url:
            result = inspect_url(eagler_override_entry_url, depth + 1, referer_url=index_url)
            if result:
                return result

        nowgg_entry = discover_nowgg_entry(index_html, index_url)
        if nowgg_entry is not None:
            return nowgg_entry

        detected_kind = detect_supported_entry_kind(index_html)
        if detected_kind in {"unity", "eaglercraft"}:
            return DetectedEntry(
                entry_kind=detected_kind,
                index_url=index_url,
                index_html=index_html,
                source_page_url=index_url,
            )

        if detected_kind != "html":
            external_unity_entry = detect_unity_entry_from_external_scripts(index_html, index_url)
            if external_unity_entry is not None:
                return external_unity_entry

        if depth >= 6:
            if detected_kind == "html":
                return DetectedEntry(
                    entry_kind=detected_kind,
                    index_url=index_url,
                    index_html=index_html,
                    source_page_url=index_url,
                )
            errors.append(f"{source} -> reached embed recursion limit")
            return None

        for child_url in extract_embedded_candidate_urls(index_html, index_url):
            result = inspect_url(child_url, depth + 1, referer_url=index_url)
            if result:
                return result

        for snippet in inline_html_snippets or snippets:
            snippet_key = snippet[:4096]
            if snippet_key in visited_snippets:
                continue
            visited_snippets.add(snippet_key)
            result = inspect_html(index_url, snippet, depth + 1, f"{source} -> embedded HTML")
            if result:
                return result

        if snippets:
            errors.append(f"{source} -> embedded HTML found but no supported build reference found")
            return None

        if detected_kind == "html":
            return DetectedEntry(
                entry_kind=detected_kind,
                index_url=index_url,
                index_html=index_html,
                source_page_url=index_url,
            )

        errors.append(f"{source} -> fetched but no supported build reference found")
        return None

    def inspect_url(candidate: str, depth: int, referer_url: str = "") -> DetectedEntry | None:
        normalized_candidate = normalize_url(candidate)
        if normalized_candidate in visited_urls:
            return None
        visited_urls.add(normalized_candidate)

        try:
            resolved, raw, content_type, _ = fetch_url(normalized_candidate, referer_url=referer_url)
        except FetchError as exc:
            errors.append(str(exc))
            return None

        if (
            content_type == "application/zip"
            or normalized_candidate.lower().endswith(".zip")
            or raw.startswith(b"PK\x03\x04")
        ):
            zip_member_name, zipped_html = extract_single_html_from_zip_payload(raw)
            if zipped_html:
                return inspect_html(
                    resolved,
                    zipped_html,
                    depth,
                    f"{resolved} -> {zip_member_name}",
                )

        text = decode_html_body(raw)
        return inspect_html(resolved, text, depth, resolved)

    for candidate in candidate_index_urls(input_url, root_url):
        result = inspect_url(candidate, 0)
        if result:
            return result

    joined = "\n  - ".join(errors) if errors else "No candidate URLs were tested."
    raise FetchError(f"Could not find a supported entry page.\n  - {joined}")


def extract_embedded_candidate_urls(index_html: str, index_url: str) -> list[str]:
    raw_candidates: list[str] = []
    source_variants = [index_html]
    unescaped_index_html = html.unescape(index_html)
    if unescaped_index_html != index_html:
        source_variants.append(unescaped_index_html)
    patterns = (
        r"""<iframe[^>]+src=["']([^"']+)["']""",
        r"""data-url=["']([^"']+)["']""",
        r"""data-(?:iframe|embed|embed-url|game|game-url|src)=["']([^"']+)["']""",
        r"""googleScriptUrl\s*=\s*["']([^"']+)["']""",
        r'''googleScriptUrl"\s*:\s*"([^"]+)''',
        r"""(?:const|let|var)\s+[A-Za-z_$][A-Za-z0-9_$]*URL\s*=\s*["'](https?://[^"']+)["']""",
        r"""(?:src|href)\s*:\s*["'](https?://[^"']+)["']""",
        r"""window\.open\(\s*["'](https?://[^"']+)["']""",
        r"""location(?:\.href)?\s*=\s*["'](https?://[^"']+)["']""",
    )

    for source_html in source_variants:
        for pattern in patterns:
            raw_candidates.extend(re.findall(pattern, source_html, re.IGNORECASE))

    urls: list[str] = []
    seen = set()
    for raw in raw_candidates:
        candidate = html.unescape(raw).replace("\\/", "/").strip()
        if not candidate:
            continue
        try:
            absolute = normalize_url(urllib.parse.urljoin(index_url, candidate))
        except FetchError:
            continue
        if is_ignored_embedded_url(absolute):
            continue
        if absolute not in seen:
            urls.append(absolute)
            seen.add(absolute)

    parsed_index = urllib.parse.urlparse(index_url)

    def url_priority(url: str) -> tuple[int, str]:
        lower = url.lower()
        parsed_url = urllib.parse.urlparse(url)
        score = 0
        if (
            parsed_url.scheme == parsed_index.scheme
            and parsed_url.netloc == parsed_index.netloc
            and "/games/" in parsed_url.path.lower()
        ):
            score += 140
        if "/game/" in parsed_url.path.lower():
            score += 120
        if parsed_url.netloc and parsed_url.netloc != parsed_index.netloc:
            score += 40
        if "script.google.com/macros" in lower:
            score += 100
        if "googleusercontent.com/embeds/" in lower:
            score += 60
        if lower.endswith(".loader.js") or lower.endswith("unityloader.js"):
            score += 60
        if lower.endswith(".xml"):
            score -= 20
        return (-score, lower)

    urls.sort(key=url_priority)
    return urls


def find_index_html(input_url: str, root_url: str) -> tuple[str, str]:
    entry = find_supported_entry(input_url, root_url)
    if entry.entry_kind != "unity":
        raise FetchError(f"Resolved entry is not a Unity page: {entry.index_url}")
    return entry.index_url, entry.index_html


def extract_html_title(index_html: str) -> str:
    match = re.search(r"<title[^>]*>([\s\S]*?)</title>", index_html, re.IGNORECASE)
    if not match:
        return ""
    title = re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()
    return title


def extract_inline_script_blocks(index_html: str) -> list[str]:
    blocks: list[str] = []
    for attrs, content in re.findall(
        r"<script\b([^>]*)>([\s\S]*?)</script>",
        index_html,
        re.IGNORECASE,
    ):
        if re.search(r"\bsrc\s*=", attrs, re.IGNORECASE):
            continue
        decoded = html.unescape(content).strip()
        if decoded:
            blocks.append(decoded)
    return blocks


def extract_external_script_urls(index_html: str, index_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for raw_url in re.findall(
        r"""<script[^>]+src=["']([^"']+)["']""",
        index_html,
        re.IGNORECASE,
    ):
        candidate = decode_js_string_literal(html.unescape(raw_url)).strip()
        if not candidate or candidate.startswith("data:"):
            continue
        resolved = normalize_url(urllib.parse.urljoin(index_url, candidate))
        lowered = resolved.lower()
        if not lowered.endswith(".js") and ".js?" not in lowered:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        urls.append(resolved)
    return urls


def is_ignored_external_script_url(script_url: str) -> bool:
    lower = script_url.lower()
    ignored_fragments = (
        "googletagmanager.com",
        "google-analytics.com",
        "doubleclick.net",
        "googleads.g.doubleclick.net",
        "googletagservices.com",
        "connect.facebook.net",
        "mgid.com",
        "taboola.com",
        "outbrain.com",
        "amazon-adsystem.com",
        "platform.twitter.com",
        "pagead2.googlesyndication.com",
        "imasdk.googleapis.com",
        "adservice.google.com",
        "googlesyndication.com",
    )
    ignored_suffixes = (
        "analytics.js",
        "gtag.js",
        "adsbygoogle.js",
        "sdk.js",
        "api.js",
    )
    return any(fragment in lower for fragment in ignored_fragments) or any(
        lower.endswith(suffix) or f"/{suffix}?" in lower for suffix in ignored_suffixes
    )


def score_external_script_url(script_url: str, page_url: str) -> tuple[int, str]:
    parsed_script = urllib.parse.urlparse(script_url)
    parsed_page = urllib.parse.urlparse(page_url)
    path = urllib.parse.unquote(parsed_script.path).lower()
    basename = path.rsplit("/", 1)[-1]
    score = 0
    if parsed_script.scheme == parsed_page.scheme and parsed_script.netloc == parsed_page.netloc:
        score += 40
    if any(token in path for token in ("unity", "loader", "build", "game", "main", "webgl")):
        score += 28
    if basename in {"main.js", "game.js", "index.js", "loader.js"}:
        score += 12
    if path.endswith(".loader.js"):
        score += 80
    if is_ignored_external_script_url(script_url):
        score -= 500
    return (-score, script_url)


def is_unity_loader_script_url(script_url: str) -> bool:
    lower = remove_query_and_fragment(script_url).lower()
    return lower.endswith(".loader.js") or lower.endswith("unityloader.js")


def should_ignore_unity_support_script_url(script_url: str, page_url: str) -> bool:
    parsed_script = urllib.parse.urlparse(script_url)
    parsed_page = urllib.parse.urlparse(page_url)
    same_origin = (
        parsed_script.scheme == parsed_page.scheme
        and parsed_script.netloc == parsed_page.netloc
    )
    lower = remove_query_and_fragment(script_url).lower()
    wrapper_ui_tokens = (
        "jquery",
        "rating",
        "raty",
        "comment",
        "share",
        "theme",
        "bootstrap",
        "swiper",
        "slick",
        "carousel",
    )
    if any(token in lower for token in wrapper_ui_tokens):
        return True
    if same_origin:
        return False
    return is_ignored_external_script_url(script_url)


def score_unity_support_script_url(script_url: str, page_url: str) -> tuple[int, str]:
    parsed_script = urllib.parse.urlparse(script_url)
    parsed_page = urllib.parse.urlparse(page_url)
    path = urllib.parse.unquote(parsed_script.path).lower()
    basename = path.rsplit("/", 1)[-1]
    score = 0
    if (
        parsed_script.scheme == parsed_page.scheme
        and parsed_script.netloc == parsed_page.netloc
    ):
        score += 80
    if share_url_parent_directory(script_url, page_url):
        score += 25
    if any(
        token in path
        for token in (
            "api",
            "sdk",
            "ads",
            "reward",
            "leader",
            "cloud",
            "auth",
            "lang",
            "rhm",
            "yandex",
            "game",
        )
    ):
        score += 20
    if basename in {"main.js", "game.js", "index.js"}:
        score += 8
    if is_unity_loader_script_url(script_url):
        score -= 1000
    if should_ignore_unity_support_script_url(script_url, page_url):
        score -= 500
    return (-score, script_url)


def collect_unity_support_script_urls(
    index_html: str,
    index_url: str,
    loader_url: str,
) -> list[str]:
    loader_without_query = remove_query_and_fragment(loader_url)
    support_urls: list[str] = []
    seen: set[str] = set()
    for script_url in sorted(
        extract_external_script_urls(index_html, index_url),
        key=lambda url: score_unity_support_script_url(url, index_url),
    ):
        if remove_query_and_fragment(script_url) == loader_without_query:
            continue
        if should_ignore_unity_support_script_url(script_url, index_url):
            continue
        if script_url in seen:
            continue
        seen.add(script_url)
        support_urls.append(script_url)
        if len(support_urls) >= 8:
            break
    return support_urls


def extract_eagler_external_script_urls(index_html: str, index_url: str) -> list[str]:
    return extract_external_script_urls(index_html, index_url)


def is_eagler_runtime_script_url(script_url: str) -> bool:
    basename = basename_from_url(script_url).lower()
    return bool(re.fullmatch(r"classes(?:\.min)?\.js", basename))


def share_url_parent_directory(url_a: str, url_b: str) -> bool:
    parsed_a = urllib.parse.urlparse(remove_query_and_fragment(url_a))
    parsed_b = urllib.parse.urlparse(remove_query_and_fragment(url_b))
    parent_a = parsed_a.path.rsplit("/", 1)[0]
    parent_b = parsed_b.path.rsplit("/", 1)[0]
    return (
        parsed_a.scheme == parsed_b.scheme
        and parsed_a.netloc == parsed_b.netloc
        and parent_a == parent_b
    )


def extract_eagler_runtime_assets(index_html: str, index_url: str) -> tuple[str, list[str]]:
    script_urls = extract_eagler_external_script_urls(index_html, index_url)
    runtime_url = next((url for url in script_urls if is_eagler_runtime_script_url(url)), "")
    if not runtime_url:
        raise FetchError(
            "No Eagler runtime file (classes.js or classes.min.js) found in entry HTML."
        )

    support_script_urls = [
        url
        for url in script_urls
        if url != runtime_url and share_url_parent_directory(url, runtime_url)
    ]
    return runtime_url, support_script_urls


def extract_eagler_bootstrap_script(index_html: str) -> str:
    candidates: list[tuple[int, int, str]] = []
    for script in extract_inline_script_blocks(index_html):
        lower = script.lower()
        if "window.eaglercraftxopts" not in lower:
            continue
        score = 0
        if "assetsuri" in lower:
            score += 80
        if "localesuri" in lower:
            score += 20
        if "main();" in lower or "main(" in lower:
            score += 20
        if "addEventListener(\"load\"" in script or "addEventListener('load'" in script:
            score += 15
        candidates.append((score, len(script), script))

    if not candidates:
        raise FetchError("No Eagler bootstrap script found in entry HTML.")

    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2]


def extract_eagler_option_string(script_text: str, key: str) -> str:
    patterns = (
        rf"""{key}\s*:\s*["']([^"']+)["']""",
        rf"""window\.eaglercraftXOpts\.{key}\s*=\s*["']([^"']+)["']""",
    )
    for pattern in patterns:
        match = re.search(pattern, script_text, re.IGNORECASE)
        if match:
            return decode_js_string_literal(match.group(1)).strip()
    return ""


def resolve_optional_url(raw_value: str, base_url: str) -> str:
    if not raw_value:
        return ""
    if raw_value.startswith("data:"):
        return raw_value
    return normalize_url(urllib.parse.urljoin(base_url, raw_value))


def strip_wrapping_parentheses(expression: str) -> str:
    trimmed = expression.strip()
    while trimmed.startswith("(") and trimmed.endswith(")"):
        depth = 0
        balanced = True
        for index, char in enumerate(trimmed):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    balanced = False
                    break
                if depth == 0 and index != len(trimmed) - 1:
                    balanced = False
                    break
        if not balanced or depth != 0:
            break
        trimmed = trimmed[1:-1].strip()
    return trimmed


def split_js_top_level(expression: str, delimiter: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote = ""
    escaped = False

    for char in expression:
        if quote:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue

        if char in {"'", '"', "`"}:
            quote = char
            current.append(char)
            continue
        if char in "([{":
            depth += 1
            current.append(char)
            continue
        if char in ")]}":
            depth = max(0, depth - 1)
            current.append(char)
            continue
        if char == delimiter and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)

    parts.append("".join(current).strip())
    return parts


def split_js_top_level_ternary(expression: str) -> tuple[str, str, str] | None:
    quote = ""
    escaped = False
    depth = 0
    question_index = -1
    colon_index = -1

    for index, char in enumerate(expression):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue

        if char in {"'", '"', "`"}:
            quote = char
            continue
        if char in "([{":
            depth += 1
            continue
        if char in ")]}":
            depth = max(0, depth - 1)
            continue
        if depth != 0:
            continue
        if char == "?" and question_index < 0:
            question_index = index
            continue
        if char == ":" and question_index >= 0:
            colon_index = index
            break

    if question_index < 0 or colon_index < 0:
        return None
    return (
        expression[:question_index].strip(),
        expression[question_index + 1 : colon_index].strip(),
        expression[colon_index + 1 :].strip(),
    )


def decode_js_string_token(token: str) -> str:
    stripped = token.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"', "`"}:
        return decode_js_string_literal(stripped[1:-1])
    return ""


def expand_js_string_expression(
    expression: str,
    env: Mapping[str, Sequence[str]],
    max_candidates: int = 8,
) -> list[str]:
    trimmed = strip_wrapping_parentheses(expression)
    if not trimmed:
        return []

    literal_value = decode_js_string_token(trimmed)
    if literal_value or (len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in {"'", '"', "`"}):
        return [literal_value]

    ternary_parts = split_js_top_level_ternary(trimmed)
    if ternary_parts is not None:
        _, when_true, when_false = ternary_parts
        combined: list[str] = []
        seen: set[str] = set()
        for candidate in expand_js_string_expression(when_true, env, max_candidates) + expand_js_string_expression(
            when_false,
            env,
            max_candidates,
        ):
            if candidate in seen:
                continue
            seen.add(candidate)
            combined.append(candidate)
            if len(combined) >= max_candidates:
                break
        return combined

    concat_parts = split_js_top_level(trimmed, "+")
    if len(concat_parts) > 1:
        combinations = [""]
        for part in concat_parts:
            part_candidates = expand_js_string_expression(part, env, max_candidates)
            if not part_candidates:
                return []
            next_combinations: list[str] = []
            for prefix in combinations:
                for suffix in part_candidates:
                    next_combinations.append(prefix + suffix)
                    if len(next_combinations) >= max_candidates:
                        break
                if len(next_combinations) >= max_candidates:
                    break
            combinations = next_combinations
            if not combinations:
                return []
        return combinations[:max_candidates]

    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", trimmed):
        return list(env.get(trimmed, []))[:max_candidates]

    return []


def extract_js_string_variable_candidates(
    index_html: str,
    variable_names: Sequence[str] | None = None,
) -> dict[str, list[str]]:
    interested_names = set(variable_names or ())
    assignment_pattern = re.compile(
        r"""(?:^|[\n;{}])\s*(?:(?:const|let|var)\s+)?([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*([^;]+);""",
        re.MULTILINE,
    )
    env: dict[str, list[str]] = {}

    for match in assignment_pattern.finditer(index_html):
        variable_name = match.group(1)
        if interested_names and variable_name not in interested_names:
            continue
        expression = match.group(2).strip()
        candidates = expand_js_string_expression(expression, env)
        if not candidates:
            continue
        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized_candidate = candidate.replace("\\/", "/").strip()
            if not normalized_candidate or normalized_candidate in seen:
                continue
            seen.add(normalized_candidate)
            deduped.append(normalized_candidate)
        if deduped:
            env[variable_name] = deduped

    return env


def detect_eagler_entry(index_url: str, index_html: str) -> DetectedEaglerEntry:
    bootstrap_script = extract_eagler_bootstrap_script(index_html)
    assets_raw = extract_eagler_option_string(bootstrap_script, "assetsURI")
    if not assets_raw:
        raise FetchError("Eagler entry is missing assetsURI.")

    classes_url, support_script_urls = extract_eagler_runtime_assets(index_html, index_url)

    return DetectedEaglerEntry(
        title=extract_html_title(index_html) or "Eaglercraft",
        index_url=index_url,
        index_html=index_html,
        classes_url=classes_url,
        assets_url=resolve_optional_url(assets_raw, index_url),
        locales_url=resolve_optional_url(
            extract_eagler_option_string(bootstrap_script, "localesURI"),
            index_url,
        ),
        bootstrap_script=bootstrap_script,
        script_urls=[classes_url, *support_script_urls],
    )


def extract_build_url_prefix_candidates(index_html: str) -> list[str]:
    env = extract_js_string_variable_candidates(
        index_html,
        variable_names=("baseUrl", "versionFolder", "buildUrl", "loaderUrl"),
    )
    build_url_candidates = list(env.get("buildUrl", []))
    if build_url_candidates:
        return build_url_candidates

    match = re.search(
        r"""(?:const|let|var)\s+buildUrl\s*=\s*["']([^"']+)["']""",
        index_html,
        re.IGNORECASE,
    )
    if not match:
        return []
    return [html.unescape(match.group(1)).replace("\\/", "/").strip()]


def extract_build_url_prefix(index_html: str) -> str:
    candidates = extract_build_url_prefix_candidates(index_html)
    return candidates[0] if candidates else ""


def absolutize_with_build_prefix_candidates(
    raw_value: str,
    index_url: str,
    build_prefix_candidates: Sequence[str],
) -> list[str]:
    candidate = html.unescape(raw_value).replace("\\/", "/").strip()
    prefixes = list(build_prefix_candidates) if build_prefix_candidates else [""]
    resolved_urls: list[str] = []
    seen: set[str] = set()

    for prefix in prefixes:
        adjusted = candidate
        normalized_prefix = prefix.strip("/")
        if normalized_prefix and adjusted.startswith("/") and not adjusted.startswith("//"):
            if adjusted.lstrip("/").startswith(normalized_prefix + "/"):
                adjusted = adjusted.lstrip("/")
            else:
                adjusted = normalized_prefix + adjusted
        try:
            absolute = normalize_url(urllib.parse.urljoin(index_url, adjusted))
        except FetchError:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        resolved_urls.append(absolute)

    return resolved_urls


def extract_urls_with_suffix(index_html: str, index_url: str, suffix_regex: str) -> list[str]:
    # Find quoted URLs in script/html content.
    pattern = re.compile(rf"""["']([^"']+?{suffix_regex}(?:\?[^"']*)?)["']""", re.IGNORECASE)
    urls: list[str] = []
    build_prefix_candidates = extract_build_url_prefix_candidates(index_html)

    for match in pattern.findall(index_html):
        urls.extend(absolutize_with_build_prefix_candidates(match, index_url, build_prefix_candidates))

    deduped: list[str] = []
    seen = set()
    for url in urls:
        if url not in seen:
            deduped.append(url)
            seen.add(url)
    return deduped


def extract_config_asset_urls(index_html: str, index_url: str) -> dict[str, list[str]]:
    build_prefix_candidates = extract_build_url_prefix_candidates(index_html)

    def collect_for_key(key: str) -> list[str]:
        found: list[str] = []
        seen = set()

        concat_pattern = re.compile(
            rf"""{key}\s*:\s*buildUrl\s*\+\s*["'`]([^"'`]+)["'`]""",
            re.IGNORECASE,
        )
        direct_pattern = re.compile(
            rf"""{key}\s*:\s*["'`]([^"'`]+)["'`]""",
            re.IGNORECASE,
        )

        for raw in concat_pattern.findall(index_html):
            for absolute in absolutize_with_build_prefix_candidates(raw, index_url, build_prefix_candidates):
                if absolute not in seen:
                    found.append(absolute)
                    seen.add(absolute)

        for raw in direct_pattern.findall(index_html):
            for absolute in absolutize_with_build_prefix_candidates(raw, index_url, build_prefix_candidates):
                if absolute not in seen:
                    found.append(absolute)
                    seen.add(absolute)

        return found

    return {
        "data": collect_for_key("dataUrl"),
        "framework": collect_for_key("frameworkUrl"),
        "wasm": collect_for_key("codeUrl") + collect_for_key("wasmUrl"),
    }


def extract_original_folder_url(index_html: str, index_url: str) -> str:
    patterns = (
        r"""window\.originalFolder\s*=\s*["'`]([^"'`]+)["'`]""",
        r"""(?:const|let|var)\s+originalFolder\s*=\s*["'`]([^"'`]+)["'`]""",
    )
    for pattern in patterns:
        match = re.search(pattern, index_html, re.IGNORECASE)
        if not match:
            continue
        candidate = decode_js_string_literal(match.group(1)).replace("\\/", "/").strip()
        if candidate:
            return normalize_url(urllib.parse.urljoin(index_url, candidate))
    return ""


def extract_streaming_assets_url(
    index_html: str,
    index_url: str,
    original_folder_url: str = "",
) -> str:
    build_prefix_candidates = extract_build_url_prefix_candidates(index_html)

    if original_folder_url:
        original_folder_match = re.search(
            r"""streamingAssetsUrl\s*:\s*window\.originalFolder\s*\+\s*["'`]([^"'`]+)["'`]""",
            index_html,
            re.IGNORECASE,
        )
        if original_folder_match:
            suffix = decode_js_string_literal(original_folder_match.group(1)).replace("\\/", "/")
            return normalize_url(
                urllib.parse.urljoin(original_folder_url.rstrip("/") + "/", suffix.lstrip("/"))
            )

    concat_match = re.search(
        r"""streamingAssetsUrl\s*:\s*buildUrl\s*\+\s*["'`]([^"'`]+)["'`]""",
        index_html,
        re.IGNORECASE,
    )
    if concat_match:
        candidates = absolutize_with_build_prefix_candidates(
            concat_match.group(1),
            index_url,
            build_prefix_candidates,
        )
        return candidates[0] if candidates else ""

    direct_match = re.search(
        r"""streamingAssetsUrl\s*:\s*["'`]([^"'`]+)["'`]""",
        index_html,
        re.IGNORECASE,
    )
    if direct_match:
        candidates = absolutize_with_build_prefix_candidates(
            direct_match.group(1),
            index_url,
            build_prefix_candidates,
        )
        return candidates[0] if candidates else ""

    env = extract_js_string_variable_candidates(
        index_html,
        variable_names=("baseUrl", "versionFolder", "buildUrl", "streamingAssetsUrl"),
    )
    expression_match = re.search(
        r"""streamingAssetsUrl\s*:\s*([^,\r\n}]+)""",
        index_html,
        re.IGNORECASE,
    )
    if expression_match:
        expression = expression_match.group(1).strip()
        for candidate in expand_js_string_expression(expression, env):
            normalized_candidate = candidate.replace("\\/", "/").strip()
            if not normalized_candidate:
                continue
            if re.match(r"^[a-z][a-z0-9+.-]*:", normalized_candidate, re.IGNORECASE):
                return normalize_url(normalized_candidate)
            return normalized_candidate

    return ""


_MISSING_JS_PRIMITIVE = object()


def parse_js_primitive_expression(expression: str) -> Any:
    trimmed = strip_wrapping_parentheses(expression.strip())
    if not trimmed:
        return _MISSING_JS_PRIMITIVE

    literal_value = decode_js_string_token(trimmed)
    if literal_value or (len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in {"'", '"', "`"}):
        return literal_value

    lowered = trimmed.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None

    if re.fullmatch(r"-?\d+", trimmed):
        try:
            return int(trimmed)
        except ValueError:
            return _MISSING_JS_PRIMITIVE

    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", trimmed):
        try:
            return float(trimmed)
        except ValueError:
            return _MISSING_JS_PRIMITIVE

    return _MISSING_JS_PRIMITIVE


def extract_page_config(index_html: str) -> dict[str, Any]:
    page_config: dict[str, Any] = {}

    config_object_match = re.search(
        r"""(?:const|let|var)\s+config\s*=\s*\{([\s\S]*?)\}\s*;""",
        index_html,
        re.IGNORECASE,
    )
    if config_object_match:
        object_body = config_object_match.group(1)
        property_pattern = re.compile(
            r"""([A-Za-z_$][A-Za-z0-9_$]*)\s*:\s*([^,\r\n]+)""",
            re.MULTILINE,
        )
        for match in property_pattern.finditer(object_body):
            key = match.group(1)
            value = parse_js_primitive_expression(match.group(2))
            if value is _MISSING_JS_PRIMITIVE:
                continue
            page_config[key] = value

    assignment_pattern = re.compile(
        r"""config\.([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*([^;]+);""",
        re.IGNORECASE,
    )
    for match in assignment_pattern.finditer(index_html):
        key = match.group(1)
        expression = match.group(2).strip()
        value = parse_js_primitive_expression(expression)
        if value is _MISSING_JS_PRIMITIVE:
            if re.fullmatch(r"""isHostOnGD\s*\(\s*\)""", expression, re.IGNORECASE):
                page_config[key] = "__standalone_isHostOnGD__"
            continue
        page_config[key] = value

    if "eventLog" in page_config:
        page_config["eventLog"] = False
    if "enablePromotion" in page_config:
        page_config["enablePromotion"] = False
    if "enableMoreGame" in page_config:
        page_config["enableMoreGame"] = "no"

    return page_config


def looks_like_gmsoft_page_config(page_config: Mapping[str, Any]) -> bool:
    if not page_config:
        return False
    gmsoft_keys = {
        "buildAPI",
        "gameId",
        "gdHost",
        "hostindex",
        "pubId",
    }
    if any(key in page_config for key in gmsoft_keys):
        return True
    build_api = str(page_config.get("buildAPI") or "").lower()
    return "azgame" in build_api or "1games" in build_api


def canonicalize_source_page_url(source_page_url: str, original_folder_url: str = "") -> str:
    if not source_page_url:
        return ""
    parsed = urllib.parse.urlparse(source_page_url)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    original_path = urllib.parse.urlparse(original_folder_url).path.rstrip("/").lower()
    if (
        host == "geometrydashlite.io"
        and path == "/geometry-dash-lite"
        and original_path.endswith("/geometry-dash-lite")
    ):
        return normalize_url(urllib.parse.urljoin(f"{parsed.scheme}://{parsed.netloc}/", "/geometry-dash-game/"))
    return source_page_url


def should_route_setting_to_parent_root(
    source_page_url: str,
    source_url: str,
    original_folder_url: str = "",
) -> bool:
    canonical_source_page_url = canonicalize_source_page_url(source_page_url, original_folder_url)
    page_parts = urllib.parse.urlparse(canonical_source_page_url)
    source_parts = urllib.parse.urlparse(source_url)
    return (
        page_parts.netloc.lower() == "geometrydashlite.io"
        and page_parts.path.rstrip("/") == "/geometry-dash-game"
        and source_parts.netloc.lower() == "geometrydashlite.io"
        and source_parts.path == "/setting.txt"
    )


def extract_loader_url(index_html: str, index_url: str) -> str:
    env = extract_js_string_variable_candidates(
        index_html,
        variable_names=("baseUrl", "versionFolder", "buildUrl", "loaderUrl"),
    )
    loader_candidates = env.get("loaderUrl", [])
    if loader_candidates:
        return normalize_url(urllib.parse.urljoin(index_url, loader_candidates[0]))

    build_prefix = extract_build_url_prefix(index_html)
    normalized_prefix = build_prefix.strip("/")

    # Prefer explicit JS assignment: loaderUrl = buildUrl + "/xxx.loader.js"
    concat_match = re.search(
        r"""(?:const|let|var)\s+loaderUrl\s*=\s*buildUrl\s*\+\s*["']([^"']+?\.loader\.js(?:\?[^"']*)?)["']""",
        index_html,
        re.IGNORECASE,
    )
    if concat_match and normalized_prefix:
        candidate = html.unescape(concat_match.group(1)).replace("\\/", "/")
        if candidate.startswith("/") and not candidate.startswith("//"):
            candidate = normalized_prefix + candidate
        else:
            candidate = normalized_prefix + "/" + candidate.lstrip("/")
        return normalize_url(urllib.parse.urljoin(index_url, candidate))

    # Next: explicit loaderUrl string assignment.
    direct_match = re.search(
        r"""(?:const|let|var)\s+loaderUrl\s*=\s*["']([^"']+?\.loader\.js(?:\?[^"']*)?)["']""",
        index_html,
        re.IGNORECASE,
    )
    if direct_match:
        candidate = html.unescape(direct_match.group(1)).replace("\\/", "/")
        if normalized_prefix and candidate.startswith("/") and not candidate.startswith("//"):
            if candidate.lstrip("/").startswith(normalized_prefix + "/"):
                candidate = candidate.lstrip("/")
            else:
                candidate = normalized_prefix + candidate
        return normalize_url(urllib.parse.urljoin(index_url, candidate))

    # Prefer direct script src.
    script_pattern = re.compile(
        r"""<script[^>]+src=["']([^"']+?\.loader\.js(?:\?[^"']*)?)["']""",
        re.IGNORECASE,
    )
    script_matches = script_pattern.findall(index_html)
    if script_matches:
        candidate = html.unescape(script_matches[0]).replace("\\/", "/")
        return normalize_url(urllib.parse.urljoin(index_url, candidate))

    # Fallback: any quoted URL with .loader.js.
    generic_matches = extract_urls_with_suffix(index_html, index_url, r"\.loader\.js")
    if generic_matches:
        return generic_matches[0]

    raise FetchError("No Unity loader file URL (*.loader.js) found in index HTML.")


def extract_legacy_config_url(index_html: str, index_url: str) -> str:
    build_prefix = extract_build_url_prefix(index_html).strip("/")

    def absolutize(candidate: str) -> str:
        raw_value = html.unescape(candidate).replace("\\/", "/")
        if build_prefix and raw_value.startswith("/") and not raw_value.startswith("//"):
            if raw_value.lstrip("/").startswith(build_prefix + "/"):
                raw_value = raw_value.lstrip("/")
            else:
                raw_value = build_prefix + raw_value
        return normalize_url(urllib.parse.urljoin(index_url, raw_value))

    concat_match = re.search(
        r"""UnityLoader\.instantiate\(\s*[^,]+,\s*buildUrl\s*\+\s*["']([^"']+?\.json(?:\?[^"']*)?)["']""",
        index_html,
        re.IGNORECASE,
    )
    if concat_match:
        candidate = concat_match.group(1)
        if candidate.startswith("/") and not candidate.startswith("//"):
            candidate = build_prefix + candidate
        else:
            candidate = build_prefix + "/" + candidate.lstrip("/")
        return normalize_url(urllib.parse.urljoin(index_url, candidate))

    direct_match = re.search(
        r"""UnityLoader\.instantiate\(\s*[^,]+,\s*["']([^"']+?\.json(?:\?[^"']*)?)["']""",
        index_html,
        re.IGNORECASE,
    )
    if direct_match:
        return absolutize(direct_match.group(1))

    variable_match = re.search(
        r"""UnityLoader\.instantiate\(\s*[^,]+,\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*,""",
        index_html,
        re.IGNORECASE,
    )
    if variable_match:
        variable_name = re.escape(variable_match.group(1))
        concat_variable_match = re.search(
            rf"""{variable_name}\s*=\s*buildUrl\s*\+\s*["']([^"']+?\.json(?:\?[^"']*)?)["']""",
            index_html,
            re.IGNORECASE,
        )
        if concat_variable_match:
            candidate = concat_variable_match.group(1)
            if candidate.startswith("/") and not candidate.startswith("//"):
                candidate = build_prefix + candidate
            else:
                candidate = build_prefix + "/" + candidate.lstrip("/")
            return normalize_url(urllib.parse.urljoin(index_url, candidate))

        direct_variable_match = re.search(
            rf"""{variable_name}\s*=\s*["']([^"']+?\.json(?:\?[^"']*)?)["']""",
            index_html,
            re.IGNORECASE,
        )
        if direct_variable_match:
            return absolutize(direct_variable_match.group(1))

    raise FetchError("No legacy Unity JSON config URL found in entry HTML.")


def extract_legacy_loader_url(index_html: str, index_url: str, config_url: str) -> str:
    script_pattern = re.compile(
        r"""<script[^>]+src=["']([^"']+?UnityLoader\.js(?:\?[^"']*)?)["']""",
        re.IGNORECASE,
    )
    script_matches = script_pattern.findall(index_html)
    if script_matches:
        candidate = html.unescape(script_matches[0]).replace("\\/", "/")
        return normalize_url(urllib.parse.urljoin(index_url, candidate))

    quoted_matches = re.findall(
        r"""["']([^"']+?UnityLoader\.js(?:\?[^"']*)?)["']""",
        index_html,
        re.IGNORECASE,
    )
    if quoted_matches:
        candidate = html.unescape(quoted_matches[0]).replace("\\/", "/")
        return normalize_url(urllib.parse.urljoin(index_url, candidate))

    config_base_url = remove_query_and_fragment(config_url).rsplit("/", 1)[0] + "/"
    return normalize_url(urllib.parse.urljoin(config_base_url, "UnityLoader.js"))


def fetch_json_payload(url: str, referer_url: str = "") -> dict[str, Any]:
    resolved_url, raw, _, _ = fetch_url(url, referer_url=referer_url)
    try:
        payload = json.loads(decode_html_body(raw))
    except json.JSONDecodeError as exc:
        raise FetchError(f"{resolved_url} -> invalid JSON payload") from exc
    if not isinstance(payload, dict):
        raise FetchError(f"{resolved_url} -> JSON payload is not an object")
    return payload


def build_legacy_asset_candidate_urls(
    loader_url: str,
    legacy_config: dict[str, Any],
    config_url: str,
) -> dict[str, list[str]]:
    candidates: dict[str, list[str]] = {
        "loader": [remove_query_and_fragment(normalize_url(loader_url))]
    }

    for key, value in legacy_config.items():
        if not key.endswith("Url") or not isinstance(value, str):
            continue
        cleaned_value = html.unescape(value).replace("\\/", "/").strip()
        if not cleaned_value or cleaned_value.startswith("data:"):
            continue
        absolute = normalize_url(urllib.parse.urljoin(config_url, cleaned_value))
        filename = basename_from_url(absolute)
        if not filename or "." not in filename:
            continue
        candidates[key] = [remove_query_and_fragment(absolute)]

    return candidates


def remove_query_and_fragment(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def basename_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    return urllib.parse.unquote(path.rsplit("/", 1)[-1])


def detect_asset_compression(resolved_url: str, content_encoding: str) -> str:
    lower_url = resolved_url.lower()
    if lower_url.endswith(".unityweb"):
        return "unityweb"
    if lower_url.endswith(".br"):
        return "br"
    if lower_url.endswith(".gz"):
        return "gzip"

    encoding = (content_encoding or "").lower()
    if encoding == "br":
        return "br"
    if encoding in {"gzip", "x-gzip"}:
        return "gzip"
    return ""


def with_filename(base_url: str, filename: str) -> str:
    encoded = urllib.parse.quote(filename, safe="()[]-_.~ ")
    encoded = encoded.replace(" ", "%20")
    return urllib.parse.urljoin(base_url, encoded)


def download_first_valid(
    urls: Sequence[str],
    destination: Path,
    referer_url: str = "",
) -> tuple[str, str, str]:
    errors: list[str] = []

    for url in urls:
        try:
            resolved, raw, _, content_encoding = fetch_url(url, referer_url=referer_url)
        except FetchError as exc:
            errors.append(str(exc))
            continue

        if not raw:
            errors.append(f"{url} -> empty response")
            continue

        if looks_like_html(raw):
            errors.append(f"{url} -> returned HTML instead of Unity asset")
            continue

        destination.write_bytes(raw)
        compression_kind = detect_asset_compression(resolved, content_encoding)
        return resolved, destination.name, compression_kind

    joined = "\n  - ".join(errors) if errors else "No candidate URLs were tested."
    raise FetchError(f"Failed to download required asset.\n  - {joined}")


def build_asset_candidate_urls(loader_url: str, index_html: str, index_url: str) -> dict[str, list[str]]:
    loader_url = remove_query_and_fragment(loader_url)
    loader_name = basename_from_url(loader_url)
    if not loader_name.endswith(".loader.js"):
        raise FetchError(
            f"Loader file does not look like Unity naming (*.loader.js): {loader_name}"
        )

    loader_base_url = loader_url.rsplit("/", 1)[0] + "/"
    stem = loader_name[: -len(".loader.js")]

    # URLs from config object, then page content, then canonical inferred names.
    config_urls = extract_config_asset_urls(index_html, index_url)
    framework_found = extract_urls_with_suffix(
        index_html, index_url, r"\.framework\.js(?:\.(?:unityweb|gz|br))?"
    )
    data_found = extract_urls_with_suffix(
        index_html, index_url, r"\.data(?:\.(?:unityweb|gz|br))?"
    )
    wasm_found = extract_urls_with_suffix(
        index_html, index_url, r"\.wasm(?:\.(?:unityweb|gz|br))?"
    )

    framework_inferred = [
        with_filename(loader_base_url, f"{stem}.framework.js"),
        with_filename(loader_base_url, f"{stem}.framework.js.unityweb"),
        with_filename(loader_base_url, f"{stem}.framework.js.gz"),
        with_filename(loader_base_url, f"{stem}.framework.js.br"),
    ]
    data_inferred = [
        with_filename(loader_base_url, f"{stem}.data"),
        with_filename(loader_base_url, f"{stem}.data.unityweb"),
        with_filename(loader_base_url, f"{stem}.data.gz"),
        with_filename(loader_base_url, f"{stem}.data.br"),
    ]
    wasm_inferred = [
        with_filename(loader_base_url, f"{stem}.wasm"),
        with_filename(loader_base_url, f"{stem}.wasm.unityweb"),
        with_filename(loader_base_url, f"{stem}.wasm.gz"),
        with_filename(loader_base_url, f"{stem}.wasm.br"),
    ]

    def merge_candidates(found: Iterable[str], inferred: Iterable[str]) -> list[str]:
        merged: list[str] = []
        seen = set()
        for url in list(found) + list(inferred):
            normalized = remove_query_and_fragment(normalize_url(url))
            if normalized not in seen:
                merged.append(normalized)
                seen.add(normalized)

        def compression_rank(url: str) -> int:
            lower = url.lower()
            if lower.endswith(".unityweb"):
                return 1
            if lower.endswith(".gz"):
                return 2
            if lower.endswith(".br"):
                return 3
            return 0

        # Prefer raw, then .unityweb, then .gz, then .br
        merged.sort(key=lambda u: (compression_rank(u), u))
        return merged

    return {
        "loader": [loader_url],
        "framework": merge_candidates(config_urls["framework"] + framework_found, framework_inferred),
        "data": merge_candidates(config_urls["data"] + data_found, data_inferred),
        "wasm": merge_candidates(config_urls["wasm"] + wasm_found, wasm_inferred),
    }


def detect_entry_build(index_url: str, index_html: str) -> DetectedBuild:
    if "UnityLoader.instantiate" in index_html:
        config_url = extract_legacy_config_url(index_html, index_url)
        legacy_config = fetch_json_payload(config_url, referer_url=index_url)
        loader_url = extract_legacy_loader_url(index_html, index_url, config_url)
        candidates = build_legacy_asset_candidate_urls(loader_url, legacy_config, config_url)
        return DetectedBuild(
            build_kind="legacy_json",
            index_url=index_url,
            index_html=index_html,
            loader_url=loader_url,
            candidates=candidates,
            legacy_config=legacy_config,
        )

    loader_url = extract_loader_url(index_html, index_url)
    original_folder_url = extract_original_folder_url(index_html, index_url)
    streaming_assets_url = extract_streaming_assets_url(
        index_html,
        index_url,
        original_folder_url=original_folder_url,
    )
    page_config = extract_page_config(index_html)
    return DetectedBuild(
        build_kind="modern",
        index_url=index_url,
        index_html=index_html,
        loader_url=loader_url,
        candidates=build_asset_candidate_urls(loader_url, index_html, index_url),
        original_folder_url=original_folder_url,
        streaming_assets_url=streaming_assets_url,
        page_config=page_config,
    )


def build_asset_candidate_urls_from_direct(
    loader_url: str,
    framework_url: str,
    data_url: str,
    wasm_url: str,
) -> dict[str, list[str]]:
    return {
        "loader": [remove_query_and_fragment(normalize_url(loader_url))],
        "framework": [remove_query_and_fragment(normalize_url(framework_url))],
        "data": [remove_query_and_fragment(normalize_url(data_url))],
        "wasm": [remove_query_and_fragment(normalize_url(wasm_url))],
    }


def build_legacy_asset_candidate_urls_from_direct(
    loader_url: str,
    framework_url: str,
    data_url: str,
    wasm_url: str,
) -> dict[str, list[str]]:
    return {
        "loader": [remove_query_and_fragment(normalize_url(loader_url))],
        "dataUrl": [remove_query_and_fragment(normalize_url(data_url))],
        "wasmCodeUrl": [remove_query_and_fragment(normalize_url(wasm_url))],
        "wasmFrameworkUrl": [remove_query_and_fragment(normalize_url(framework_url))],
    }


def infer_legacy_config_from_direct_urls(
    loader_url: str,
    framework_url: str,
    data_url: str,
    wasm_url: str,
    existing_legacy_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(existing_legacy_config, dict) and existing_legacy_config:
        return json.loads(json.dumps(existing_legacy_config))

    product_name = basename_from_url(data_url).split(".", 1)[0] or "Unity Game"
    return {
        "companyName": "DefaultCompany",
        "productName": product_name,
        "productVersion": "1.0.0",
        "dataUrl": basename_from_url(data_url),
        "wasmCodeUrl": basename_from_url(wasm_url),
        "wasmFrameworkUrl": basename_from_url(framework_url),
        "graphicsAPI": ["WebGL 2.0", "WebGL 1.0"],
        "webglContextAttributes": {
            "preserveDrawingBuffer": False,
        },
        "splashScreenStyle": "Dark",
        "backgroundColor": "#231F20",
        "cacheControl": {
            "default": "must-revalidate",
        },
        "developmentBuild": False,
        "multithreading": False,
    }


def resolve_direct_build(
    loader_url: str,
    framework_url: str,
    data_url: str,
    wasm_url: str,
    progress_file: Path,
) -> tuple[str, dict[str, list[str]], dict[str, Any]]:
    existing_progress = load_json_file(progress_file)
    existing_legacy_config = (
        existing_progress.get("legacy_config")
        if existing_progress.get("build_kind") == "legacy_json"
        and isinstance(existing_progress.get("legacy_config"), dict)
        else None
    )

    loader_name = basename_from_url(loader_url).lower()
    framework_name = basename_from_url(framework_url).lower()
    wasm_name = basename_from_url(wasm_url).lower()
    looks_legacy = bool(existing_legacy_config) or (
        loader_name == "unityloader.js"
        or ".wasm.framework" in framework_name
        or ".wasm.code" in wasm_name
    )

    if looks_legacy:
        legacy_config = infer_legacy_config_from_direct_urls(
            loader_url=loader_url,
            framework_url=framework_url,
            data_url=data_url,
            wasm_url=wasm_url,
            existing_legacy_config=existing_legacy_config,
        )
        return (
            "legacy_json",
            build_legacy_asset_candidate_urls_from_direct(
                loader_url=loader_url,
                framework_url=framework_url,
                data_url=data_url,
                wasm_url=wasm_url,
            ),
            legacy_config,
        )

    return (
        "modern",
        build_asset_candidate_urls_from_direct(
            loader_url=loader_url,
            framework_url=framework_url,
            data_url=data_url,
            wasm_url=wasm_url,
        ),
        {},
    )


def analyze_framework(framework_path: Path) -> FrameworkAnalysis:
    raw_text = read_maybe_decompressed_bytes(framework_path).decode(
        "utf-8", errors="ignore"
    )

    # Pattern 1: explicit wrapper names such as _VendorBridgeGetSomething(...)
    wrapper_matches = re.findall(r"_([A-Za-z0-9_]*Bridge[A-Za-z0-9_]*)\s*\(", raw_text)

    # Pattern 2: function calls that explicitly target window.<name>(...)
    window_call_matches = re.findall(r"\bwindow\.([A-Za-z_$][A-Za-z0-9_$]*)\s*\(", raw_text)

    # Pattern 3: direct global calls used by Unity glue libraries, e.g. InitSDKJs()
    direct_call_matches = re.findall(
        r"(?<![.\w$])([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
        raw_text,
    )
    declared_function_names = set(
        re.findall(r"\bfunction\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(", raw_text)
    )

    excluded_function_names = {
        "addEventListener",
        "removeEventListener",
        "dispatchEvent",
        "setTimeout",
        "clearTimeout",
        "setInterval",
        "clearInterval",
        "requestAnimationFrame",
        "cancelAnimationFrame",
        "fetch",
        "open",
        "alert",
        "confirm",
        "prompt",
        "postMessage",
        "atob",
        "btoa",
        "decodeURIComponent",
        "encodeURIComponent",
        "escape",
        "unescape",
        "parseInt",
        "parseFloat",
        "isNaN",
        "isFinite",
        "setTempRet0",
        "getTempRet0",
    }
    excluded_direct_function_names = excluded_function_names | declared_function_names | {
        "Array",
        "Boolean",
        "Date",
        "Error",
        "EvalError",
        "Function",
        "JSON",
        "Map",
        "Math",
        "Number",
        "Object",
        "Promise",
        "RangeError",
        "ReferenceError",
        "RegExp",
        "Set",
        "String",
        "Symbol",
        "SyntaxError",
        "TypeError",
        "URIError",
        "Uint8Array",
        "Uint16Array",
        "Uint32Array",
        "Int8Array",
        "Int16Array",
        "Int32Array",
        "Float32Array",
        "Float64Array",
        "ArrayBuffer",
        "DataView",
        "BigInt64Array",
        "BigUint64Array",
        "Atomics",
        "Reflect",
        "Proxy",
        "console",
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "return",
        "typeof",
        "new",
        "delete",
        "in",
        "instanceof",
        "do",
        "void",
        "await",
        "yield",
        "class",
        "super",
        "import",
        "export",
        "case",
        "break",
        "continue",
        "throw",
        "try",
        "with",
    }
    excluded_window_roots = excluded_function_names | {
        "__unityStandaloneLocalPageUrl",
        "__unityStandaloneSourcePageUrl",
        "__unityStandaloneAuxiliaryAssetUrls",
        "__unityStandaloneAuxiliaryAssetRewriteInstalled",
        "document",
        "navigator",
        "location",
        "history",
        "screen",
        "performance",
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "mozIndexedDB",
        "webkitIndexedDB",
        "msIndexedDB",
        "CSS",
        "URL",
        "webkitURL",
        "AudioContext",
        "webkitAudioContext",
        "innerWidth",
        "innerHeight",
        "devicePixelRatio",
        "orientation",
        "scrollX",
        "scrollY",
        "pageXOffset",
        "pageYOffset",
    }

    required_function_names: set[str] = set()

    for wrapper_name in wrapper_matches:
        # Remove leading vendor bridge prefixes when present, keep callable suffix.
        # Example: VendorBridgeGetInterstitialState -> getInterstitialState
        if "Bridge" in wrapper_name:
            suffix = wrapper_name.split("Bridge", 1)[1]
            if suffix:
                required_function_names.add(suffix[0].lower() + suffix[1:])

    for name in window_call_matches:
        if name:
            required_function_names.add(name)

    for name in direct_call_matches:
        if not name or name in excluded_direct_function_names:
            continue
        if name.startswith(
            (
                "_",
                "$",
                "dynCall_",
                "invoke_",
                "UTF8",
                "Pointer_",
                "Browser",
                "GLctx",
                "HEAP",
                "stack",
                "temp",
                "___",
            )
        ):
            continue
        lower_name = name.lower()
        if not (
            name.startswith(
                (
                    "Init",
                    "Get",
                    "Set",
                    "Load",
                    "Save",
                    "Call",
                    "Open",
                    "Prompt",
                    "Review",
                    "Buy",
                    "Consume",
                    "Execute",
                    "Request",
                    "Reward",
                    "Full",
                    "Activity",
                    "Recalculate",
                    "Static",
                    "Sticky",
                    "Language",
                    "Game",
                    "Paint",
                    "Show",
                    "Has",
                    "Is",
                    "Wrap",
                    "Debug",
                    "Sync",
                    "Add",
                    "Copy",
                )
            )
            or name in {"showNextAd", "showReward", "ym", "getUserMedia"}
            or any(
                token in lower_name
                for token in (
                    "sdk",
                    "ads",
                    "reward",
                    "interstitial",
                    "banner",
                    "leader",
                    "cloud",
                    "auth",
                    "payment",
                    "purchase",
                    "game",
                    "prompt",
                    "review",
                    "lang",
                    "environ",
                    "metric",
                    "sticky",
                    "rbt",
                )
            )
        ):
            continue
        required_function_names.add(name)

    filtered_functions = {
        name for name in required_function_names if name not in excluded_function_names
    }

    window_roots: set[str] = set()
    window_callable_chains: set[str] = set()
    window_chain_pattern = re.compile(
        r"\bwindow\.([A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*){0,7})\s*(\()?"
    )
    for match in window_chain_pattern.finditer(raw_text):
        chain = match.group(1)
        if not chain:
            continue
        root_name = chain.split(".", 1)[0]
        if root_name in excluded_window_roots:
            continue
        window_roots.add(root_name)
        if match.group(2) == "(":
            window_callable_chains.add(chain)

    requires_crazygames_sdk = any(
        token in raw_text
        for token in (
            "/vs/crazygames-sdk-v2.js",
            "sdk.crazygames.com/crazygames-sdk-",
            "window.CrazyGames.SDK",
            "CrazyGames.SDK.",
        )
    )

    return FrameworkAnalysis(
        required_functions=sorted(filtered_functions),
        window_roots=sorted(window_roots),
        window_callable_chains=sorted(window_callable_chains),
        requires_crazygames_sdk=requires_crazygames_sdk,
    )


def empty_framework_analysis() -> FrameworkAnalysis:
    return FrameworkAnalysis(
        required_functions=[],
        window_roots=[],
        window_callable_chains=[],
        requires_crazygames_sdk=False,
    )


def validate_required_function_coverage(index_content: str, required_functions: Sequence[str]) -> None:
    match = re.search(
        r"const\s+dynamicFunctionNames\s*=\s*(\[[\s\S]*?\]);",
        index_content,
        re.MULTILINE,
    )
    if not match:
        raise FetchError("Generated index.html is missing dynamicFunctionNames list.")

    try:
        declared = set(json.loads(match.group(1)))
    except json.JSONDecodeError as exc:
        raise FetchError("Generated dynamicFunctionNames list is not valid JSON.") from exc

    expected = set(required_functions)
    missing = sorted(expected - declared)
    if missing:
        preview = ", ".join(missing[:20])
        suffix = " ..." if len(missing) > 20 else ""
        raise FetchError(
            f"Generated index.html is missing {len(missing)} required functions: {preview}{suffix}"
        )


def slugify_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._ -]+", "", value)
    value = value.strip().strip(".")
    value = re.sub(r"\s+", " ", value)
    return value or "unity-game"


def clean_inferred_title(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", html.unescape(title or "")).strip().strip("-|: ")
    if not cleaned:
        return ""

    cleaned = re.sub(r"\s+online\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    branding_patterns = (
        r"^(?:ocean|google(?:\.com)?|google sites?)\s*[-|:]\s*(.+)$",
        r"^(.+?)\s*[-|:]\s*(?:google(?:\.com)?|google sites?)$",
    )
    for pattern in branding_patterns:
        match = re.match(pattern, cleaned, re.IGNORECASE)
        if match:
            cleaned = match.group(1).strip().strip("-|: ")
            break

    if re.fullmatch(
        r"(?:google(?:\.com)?|google sites?|ocean|home|index|game|games)",
        cleaned,
        re.IGNORECASE,
    ):
        return ""
    return cleaned


def infer_title_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    generic_segments = {
        "all games click me",
        "all-games-click-me",
        "embed",
        "embeds",
        "game",
        "games",
        "play",
        "view",
    }
    path_segments = [segment for segment in parsed.path.split("/") if segment]
    for raw_segment in reversed(path_segments):
        segment = urllib.parse.unquote(raw_segment).strip()
        if not segment:
            continue
        if re.fullmatch(r"\d+(?:\.[A-Za-z0-9]{1,8})?", segment):
            continue
        if re.search(r"\.(?:xml|html?|php|aspx?|json)$", segment, re.IGNORECASE):
            continue
        normalized_segment = re.sub(r"[-_.]+", " ", segment).strip().lower()
        if not normalized_segment or normalized_segment in generic_segments:
            continue
        normalized_segment = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", normalized_segment).strip()
        if not normalized_segment:
            continue
        title = " ".join(word.capitalize() for word in normalized_segment.split())
        if title:
            return title

    host_part = parsed.netloc.split(".", 1)[0].strip()
    if not host_part:
        return ""
    host_title = re.sub(r"[-_.]+", " ", host_part)
    host_title = re.sub(r"\s+", " ", host_title).strip()
    if not host_title:
        return ""
    if re.fullmatch(r"www|google|sites", host_title, re.IGNORECASE):
        return ""
    return " ".join(word.capitalize() for word in host_title.split())


def infer_product_name_from_entry(index_html: str, fallback: str, source_url: str = "") -> str:
    cleaned = clean_inferred_title(extract_html_title(index_html))
    if cleaned:
        return cleaned
    if source_url:
        from_url = infer_title_from_url(source_url)
        if from_url:
            return from_url
    return fallback


def infer_display_title(
    title: str,
    root_url: str,
    fallback: str = "Standalone Game",
    source_page_url: str = "",
) -> str:
    cleaned_title = clean_inferred_title(title)
    if cleaned_title:
        return cleaned_title

    for candidate_url in (root_url, source_page_url):
        inferred = infer_title_from_url(candidate_url)
        if inferred:
            return inferred
    return fallback


def absolutize_markup_urls(document_html: str, source_url: str) -> str:
    if not source_url:
        return document_html

    attr_pattern = re.compile(
        r"(\b(?:src|href|action|poster)\s*=\s*)(['\"])([^\"']+)\2",
        re.IGNORECASE,
    )
    tag_pattern = re.compile(r"<[^>]+>", re.DOTALL)
    protected_block_pattern = re.compile(
        r"(<script\b[\s\S]*?</script>|<style\b[\s\S]*?</style>)",
        re.IGNORECASE,
    )

    def replace_attr(match: re.Match[str]) -> str:
        prefix, quote, raw_value = match.groups()
        value = html.unescape(raw_value).strip()
        lowered = value.lower()
        if (
            not value
            or lowered.startswith(("#", "data:", "javascript:", "mailto:", "tel:", "blob:"))
        ):
            return match.group(0)
        absolute = normalize_url(urllib.parse.urljoin(source_url, decode_js_string_literal(value)))
        return f"{prefix}{quote}{html.escape(absolute, quote=True)}{quote}"

    def rewrite_tag_attributes(fragment: str) -> str:
        return tag_pattern.sub(lambda tag_match: attr_pattern.sub(replace_attr, tag_match.group(0)), fragment)

    rebuilt: list[str] = []
    last_index = 0
    for block_match in protected_block_pattern.finditer(document_html):
        rebuilt.append(rewrite_tag_attributes(document_html[last_index:block_match.start()]))
        protected_block = block_match.group(0)
        opening_tag_end = protected_block.find(">")
        if opening_tag_end == -1:
            rebuilt.append(protected_block)
        else:
            rebuilt.append(attr_pattern.sub(replace_attr, protected_block[: opening_tag_end + 1]))
            rebuilt.append(protected_block[opening_tag_end + 1 :])
        last_index = block_match.end()
    rebuilt.append(rewrite_tag_attributes(document_html[last_index:]))
    return "".join(rebuilt)


def extract_html_external_links(document_html: str) -> dict[str, list[str]]:
    def dedupe(values: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            ordered.append(value)
        return ordered

    script_urls = dedupe(
        [
            html.unescape(match).strip()
            for match in re.findall(
                r"""<script[^>]+src=["']([^"']+)["']""",
                document_html,
                re.IGNORECASE,
            )
        ]
    )

    stylesheet_urls: list[str] = []
    other_link_urls: list[str] = []
    for tag in re.findall(r"""<link\b[^>]*>""", document_html, re.IGNORECASE):
        href_match = re.search(r"""href=["']([^"']+)["']""", tag, re.IGNORECASE)
        if not href_match:
            continue
        href = html.unescape(href_match.group(1)).strip()
        rel_match = re.search(r"""rel=["']([^"']+)["']""", tag, re.IGNORECASE)
        rel_value = rel_match.group(1).lower() if rel_match else ""
        if "stylesheet" in rel_value:
            stylesheet_urls.append(href)
        else:
            other_link_urls.append(href)

    iframe_urls = dedupe(
        [
            html.unescape(match).strip()
            for match in re.findall(
                r"""<iframe[^>]+src=["']([^"']+)["']""",
                document_html,
                re.IGNORECASE,
            )
        ]
    )

    return {
        "scripts": script_urls,
        "stylesheets": dedupe(stylesheet_urls),
        "frames": iframe_urls,
        "other_links": dedupe(other_link_urls),
    }


def strip_known_embedded_ad_markup(document_html: str) -> tuple[str, dict[str, int]]:
    cleaned = document_html
    removal_counts = {
        "style_blocks": 0,
        "script_blocks": 0,
        "iframe_blocks": 0,
        "mask_blocks": 0,
        "container_blocks": 0,
        "comment_blocks": 0,
    }

    replacements = (
        (
            "style_blocks",
            re.compile(
                r"""<style\b[^>]*>(?:(?!</style>).)*(?:#ad-container|#ad-iframe|#close-ad|#ad-right-mask|reklam)(?:(?!</style>).)*</style>""",
                re.IGNORECASE | re.DOTALL,
            ),
        ),
        (
            "script_blocks",
            re.compile(
                r"""<script\b[^>]*>(?:(?!</script>).)*(?:document\.getElementById\(['"]ad-container['"]\)|document\.getElementById\(['"]close-ad['"]\)|script\.google\.com/macros|countdownStart)(?:(?!</script>).)*</script>""",
                re.IGNORECASE | re.DOTALL,
            ),
        ),
        (
            "iframe_blocks",
            re.compile(
                r"""<iframe\b[^>]*(?:\bid\s*=\s*['"]ad-iframe['"]|src\s*=\s*['"][^"']*script\.google\.com/macros/)[\s\S]*?</iframe>""",
                re.IGNORECASE,
            ),
        ),
        (
            "mask_blocks",
            re.compile(
                r"""<div\b[^>]*\bid\s*=\s*['"]ad-right-mask['"][^>]*>\s*</div>""",
                re.IGNORECASE,
            ),
        ),
        (
            "container_blocks",
            re.compile(
                r"""<div\b[^>]*\bid\s*=\s*['"]ad-container['"][^>]*>[\s\S]*?</div>""",
                re.IGNORECASE,
            ),
        ),
        (
            "comment_blocks",
            re.compile(
                r"""<!--[\s\S]*?(?:reklam|advert|ad-container|ad-iframe)[\s\S]*?-->""",
                re.IGNORECASE,
            ),
        ),
    )

    for key, pattern in replacements:
        cleaned, count = pattern.subn("", cleaned)
        removal_counts[key] += count

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned, removal_counts


def looks_like_construct2_entry_html(index_html: str) -> bool:
    lower = index_html.lower()
    return (
        "c2runtime.js" in lower
        or "cr_createruntime" in lower
        or "c2_registersw" in lower
        or "construct 2" in lower
    )


def html_entry_source_root_url(source_url: str) -> str:
    normalized = remove_query_and_fragment(normalize_url(source_url))
    if normalized.endswith("/"):
        return normalized
    return normalized.rsplit("/", 1)[0] + "/"


def relative_asset_path_under_root(asset_url: str, source_root_url: str) -> str:
    normalized_asset = remove_query_and_fragment(normalize_url(asset_url))
    parsed_asset = urllib.parse.urlparse(normalized_asset)
    parsed_root = urllib.parse.urlparse(source_root_url)
    root_path = parsed_root.path if parsed_root.path.endswith("/") else parsed_root.path + "/"
    if parsed_asset.scheme != parsed_root.scheme or parsed_asset.netloc != parsed_root.netloc:
        return ""
    if not parsed_asset.path.startswith(root_path):
        return ""
    relative_path = urllib.parse.unquote(parsed_asset.path[len(root_path) :]).lstrip("/")
    if (
        not relative_path
        or relative_path.startswith("../")
        or "/../" in relative_path
        or "\\.." in relative_path
    ):
        return ""
    return relative_path


def fetch_json_document(url: str, referer_url: str = "") -> Any:
    resolved_url, raw, _, _ = fetch_url(url, referer_url=referer_url)
    try:
        return json.loads(raw.decode("utf-8-sig", errors="replace"))
    except json.JSONDecodeError as exc:
        raise FetchError(f"{resolved_url} -> invalid JSON payload") from exc


def rewrite_markup_urls_to_local(document_html: str, rewrite_map: Mapping[str, str]) -> str:
    if not rewrite_map:
        return document_html

    attr_pattern = re.compile(
        r"(\b(?:src|href|action|poster|content)\s*=\s*)(['\"])([^\"']+)\2",
        re.IGNORECASE,
    )

    def replace_attr(match: re.Match[str]) -> str:
        prefix, quote, raw_value = match.groups()
        value = html.unescape(raw_value).strip()
        normalized_value = remove_query_and_fragment(normalize_url(value)) if value else ""
        replacement = rewrite_map.get(normalized_value, "")
        if not replacement:
            return match.group(0)
        return f"{prefix}{quote}{html.escape(replacement, quote=True)}{quote}"

    return attr_pattern.sub(replace_attr, document_html)


def strip_nonessential_html_markup(document_html: str) -> tuple[str, dict[str, int]]:
    cleaned = document_html
    removal_counts = {
        "canonical_links": 0,
    }

    replacements = (
        (
            "canonical_links",
            re.compile(
                r"""<link\b[^>]*\brel\s*=\s*['"][^'"]*\bcanonical\b[^'"]*['"][^>]*>\s*""",
                re.IGNORECASE,
            ),
        ),
    )

    for key, pattern in replacements:
        cleaned, count = pattern.subn("", cleaned)
        removal_counts[key] += count

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned, removal_counts


def patch_inline_eagler_wrapper_html(document_html: str) -> tuple[str, dict[str, int]]:
    patched = re.sub(r"</\s*body\s*>", "</body>", document_html, flags=re.IGNORECASE)
    patched = re.sub(r"</\s*html\s*>", "</html>", patched, flags=re.IGNORECASE)
    patched = re.sub(r"(?im)^[ \t]*/body>\s*$", "</body>", patched)
    patched = re.sub(r"(?im)^[ \t]*body>\s*$", "</body>", patched)
    patch_counts = {
        "countdown_guarded": 0,
        "countdown_autostart_injected": 0,
        "mobile_autolaunch_injected": 0,
        "device_pixel_ratio_guarded": 0,
        "modapi_bridge_injected": 0,
    }
    if not looks_like_eagler_entry_html(patched):
        return patched, patch_counts

    modapi_bridge_script = """
<script>
(function (root) {
  root = root || (typeof globalThis !== "undefined" ? globalThis : null);
  if (!root || root.__oceanEaglerModApiBridgeInstalled) {
    return;
  }
  root.__oceanEaglerModApiBridgeInstalled = true;
  if (typeof root.ModAPI !== "object" || !root.ModAPI) {
    root.ModAPI = {};
  }
  if (typeof root.initAPI !== "function") {
    root.initAPI = function () {
      if (typeof root.ModAPI !== "object" || !root.ModAPI) {
        root.ModAPI = {};
      }
      return root.ModAPI;
    };
  }
})(typeof window !== "undefined" ? window : (typeof globalThis !== "undefined" ? globalThis : null));
</script>
""".strip()

    if "__oceanEaglerModApiBridgeInstalled" not in patched:
        lower_patched = patched.lower()
        first_script_index = lower_patched.find("<script")
        if first_script_index != -1:
            patched = patched[:first_script_index] + modapi_bridge_script + "\n" + patched[first_script_index:]
        else:
            head_close_index = lower_patched.find("</head>")
            if head_close_index != -1:
                patched = patched[:head_close_index] + modapi_bridge_script + "\n" + patched[head_close_index:]
            else:
                patched = modapi_bridge_script + "\n" + patched
        patch_counts["modapi_bridge_injected"] = 1

    patched, guarded_count = re.subn(
        r"""document\.getElementById\((['"])launch_countdown_screen\1\)\.remove\(\);\s*main\(\);""",
        (
            'var __oceanLaunchCountdown = document.getElementById("launch_countdown_screen"); '
            'if (__oceanLaunchCountdown) { __oceanLaunchCountdown.remove(); } '
            'if (!window.__oceanEaglerMainStarted && typeof main === "function") { '
            'window.__oceanEaglerMainStarted = true; main(); }'
        ),
        patched,
        flags=re.IGNORECASE,
    )
    patched, remove_child_guarded_count = re.subn(
        r"""document\.body\.removeChild\(\s*document\.getElementById\((['"])launch_countdown_screen\1\)\s*\);""",
        (
            'var __oceanLaunchCountdown = document.getElementById("launch_countdown_screen"); '
            'if (__oceanLaunchCountdown && __oceanLaunchCountdown.parentNode) { '
            '__oceanLaunchCountdown.parentNode.removeChild(__oceanLaunchCountdown); }'
        ),
        patched,
        flags=re.IGNORECASE,
    )
    patch_counts["countdown_guarded"] = guarded_count + remove_child_guarded_count
    patched, guarded_dpr_count = re.subn(
        r"""\bA\.ElB\.devicePixelRatio\b""",
        "((A.ElB || A.Eg_ || $rt_globals.window).devicePixelRatio || 1)",
        patched,
    )
    patch_counts["device_pixel_ratio_guarded"] = guarded_dpr_count

    needs_countdown_autostart = (
        "launch_countdown_screen" in patched and "launchCountdownNumber" in patched
    )
    needs_mobile_autolaunch = "_eaglercraftX_mobile_launch_client" in patched

    if needs_countdown_autostart:
        patched, direct_start_count = re.subn(
            r"""launchInterval\s*=\s*setInterval\(\s*launchTick\s*,\s*50\s*\)\s*;""",
            "launchCounter = 100; launchInterval = setInterval(launchTick, 50);",
            patched,
            count=1,
            flags=re.IGNORECASE,
        )
        if direct_start_count:
            patch_counts["countdown_autostart_injected"] = 1
            if not needs_mobile_autolaunch:
                return patched, patch_counts

    if not needs_countdown_autostart and not needs_mobile_autolaunch:
        return patched, patch_counts

    helper_script = """
<script>
(function () {
  var countdownHandled = false;
  var mobileLaunchHandled = false;

  function removeCountdownNode() {
    var countdown = document.getElementById("launch_countdown_screen");
    if (!countdown) {
      return false;
    }
    countdown.style.display = "none";
    return true;
  }

  function clickCountdownSkipButton() {
    var skipButton = document.getElementById("skipCountdown");
    if (!skipButton) {
      return false;
    }
    try {
      skipButton.click();
    } catch (err) {
      return false;
    }
    return true;
  }

  function fastForwardCountdown() {
    if (countdownHandled) {
      return false;
    }
    if (clickCountdownSkipButton()) {
      countdownHandled = true;
      return true;
    }
    if (typeof window.launchTick === "function") {
      countdownHandled = true;
      if (window.launchInterval) {
        try {
          clearInterval(window.launchInterval);
        } catch (err) {
        }
        window.launchInterval = null;
      }
      if (typeof window.launchCounter !== "number" || !isFinite(window.launchCounter)) {
        window.launchCounter = 100;
      } else {
        window.launchCounter = 100;
      }
      removeCountdownNode();
      try {
        window.launchTick();
      } catch (err) {
      }
      return true;
    }
    return false;
  }

  function clickMobileLaunchButton() {
    if (mobileLaunchHandled) {
      return false;
    }
    var button = document.querySelector("._eaglercraftX_mobile_launch_client");
    if (!button) {
      return false;
    }
    mobileLaunchHandled = true;
    try {
      button.click();
    } catch (err) {
      mobileLaunchHandled = false;
      return false;
    }
    var popup = button.closest ? button.closest("div") : button.parentNode;
    if (popup && popup.parentNode && popup !== document.body) {
      popup.style.display = "none";
    }
    return true;
  }

  function applyEaglerOverrides() {
    if (window.eaglercraftXOpts && typeof window.eaglercraftXOpts === "object") {
      window.eaglercraftXOpts.useVisualViewport = false;
    }
    var touchedMobile = clickMobileLaunchButton();
    var touchedCountdown = fastForwardCountdown();
    if (touchedMobile || touchedCountdown) {
      window.setTimeout(fastForwardCountdown, 0);
    }
  }

  if (document.readyState === "complete" || document.readyState === "interactive") {
    window.setTimeout(applyEaglerOverrides, 0);
  } else {
    window.addEventListener("load", function () {
      window.setTimeout(applyEaglerOverrides, 0);
    }, { once: true });
  }

  if (typeof MutationObserver === "function") {
    var observer = new MutationObserver(function () {
      var touchedMobile = clickMobileLaunchButton();
      var touchedCountdown = fastForwardCountdown();
      if (touchedMobile || touchedCountdown) {
        window.setTimeout(fastForwardCountdown, 0);
      }
    });
    observer.observe(document.documentElement || document.body, { childList: true, subtree: true });
    window.setTimeout(function () {
      observer.disconnect();
    }, 15000);
  }
})();
</script>
""".strip()

    lower_patched = patched.lower()
    body_close_index = lower_patched.rfind("</body>")
    if body_close_index != -1:
        patched = patched[:body_close_index] + helper_script + "\n" + patched[body_close_index:]
    else:
        html_close_index = lower_patched.rfind("</html>")
        if html_close_index != -1:
            patched = patched[:html_close_index] + helper_script + "\n" + patched[html_close_index:]
        else:
            patched += "\n" + helper_script
    patched = re.sub(r"<<\s*script\b", "<script", patched, flags=re.IGNORECASE)

    if needs_countdown_autostart:
        patch_counts["countdown_autostart_injected"] = 1
    if needs_mobile_autolaunch:
        patch_counts["mobile_autolaunch_injected"] = 1

    return patched, patch_counts


def generate_local_websdkwrapper_stub() -> str:
    return """globalThis.WebSdkWrapper = (function () {
  var listeners = {
    pause: [],
    resume: [],
    mute: [],
    unmute: [],
    adStarted: []
  };
  var unlockAllLevelsHandler = null;
  var gameplayStarted = false;
  var crazyListeners = {};

  function addListener(eventName, fn) {
    if (typeof fn !== "function") {
      return;
    }
    if (!Object.prototype.hasOwnProperty.call(listeners, eventName)) {
      listeners[eventName] = [];
    }
    listeners[eventName].push(fn);
  }

  function emit(eventName) {
    var args = Array.prototype.slice.call(arguments, 1);
    var queue = listeners[eventName] || [];
    for (var index = 0; index < queue.length; index += 1) {
      try {
        queue[index].apply(null, args);
      } catch (error) {
        console.warn("Local WebSdkWrapper listener failed:", error);
      }
    }
  }

  function addCrazyListener(eventName, fn) {
    if (typeof fn !== "function") {
      return;
    }
    if (!Object.prototype.hasOwnProperty.call(crazyListeners, eventName)) {
      crazyListeners[eventName] = [];
    }
    crazyListeners[eventName].push(fn);
  }

  function emitCrazy(eventName, payload) {
    var queue = crazyListeners[eventName] || [];
    for (var index = 0; index < queue.length; index += 1) {
      try {
        queue[index](payload || {});
      } catch (error) {
        console.warn("Local CrazyGames listener failed:", error);
      }
    }
  }

  function hideBannerContainer(containerId) {
    if (!containerId) {
      return;
    }
    var element = document.getElementById(containerId);
    if (!element) {
      return;
    }
    element.textContent = "";
    element.innerHTML = "";
    element.style.display = "none";
    element.style.visibility = "hidden";
    element.style.pointerEvents = "none";
  }

  function setAdConfig(config) {
    if (!config || typeof config !== "object") {
      return;
    }
    globalThis.adconfigRemoveSocials = config.removeSocials ? 1 : 0;
    globalThis.adconfigStopAudioInBackground = config.stopAudioInBackground ? 1 : 0;
    globalThis.adconfigRemoveMidrollRewarded = config.removeMidrollRewarded ? 1 : 0;
    globalThis.adconfigNoReligion = config.noReligion ? 1 : 0;
  }

  var localCrazySdk = {
    hasAdblock: false,
    init: function () {
      emitCrazy("adblockDetectionExecuted", { hasAdblock: false });
    },
    addEventListener: function (eventName, handler) {
      addCrazyListener(eventName, handler);
    },
    requestAd: function (adType) {
      var normalizedType = adType === "rewarded" ? "rewarded" : "interstitial";
      emit("adStarted", normalizedType);
      emit("mute");
      emitCrazy("adStarted", { type: normalizedType });
      return Promise.resolve().then(function () {
        emit("unmute");
        emitCrazy("adFinished", { type: normalizedType });
        return true;
      });
    },
    requestBanner: function (banners) {
      if (!Array.isArray(banners)) {
        return;
      }
      for (var index = 0; index < banners.length; index += 1) {
        var banner = banners[index] || {};
        var containerId = banner.containerId || "";
        hideBannerContainer(containerId);
        emitCrazy("bannerError", {
          containerId: containerId,
          error: "disabled"
        });
      }
    },
    gameplayStart: function () {},
    gameplayStop: function () {},
    happytime: function () {}
  };

  var crazyRoot = globalThis.CrazyGames = globalThis.CrazyGames || {};
  crazyRoot.CrazySDK = crazyRoot.CrazySDK || {
    getInstance: function () {
      return localCrazySdk;
    }
  };
  globalThis.Crazygames = globalThis.Crazygames || {};
  if (typeof globalThis.Crazygames.requestInviteUrl !== "function") {
    globalThis.Crazygames.requestInviteUrl = function () {};
  }
  globalThis.crazysdk = localCrazySdk;
  globalThis.adblockIsEnabled = false;

  var Wrapper = {
    get enabled() {
      return true;
    },
    get currentSdk() {
      return { name: "LocalNoAds" };
    },
    init: function (_name, _debug, data) {
      if (data && typeof data === "object") {
        setAdConfig(data);
      }
      return Promise.resolve();
    },
    onPause: function (fn) {
      addListener("pause", fn);
    },
    pause: function () {
      emit("pause");
    },
    onResume: function (fn) {
      addListener("resume", fn);
    },
    resume: function () {
      emit("resume");
    },
    onMute: function (fn) {
      addListener("mute", fn);
    },
    mute: function () {
      emit("mute");
    },
    onUnmute: function (fn) {
      addListener("unmute", fn);
    },
    unmute: function () {
      emit("unmute");
    },
    onUnlockAllLevels: function (fn) {
      unlockAllLevelsHandler = typeof fn === "function" ? fn : null;
    },
    unlockAllLevels: function () {
      if (typeof unlockAllLevelsHandler === "function") {
        unlockAllLevelsHandler();
      }
    },
    hasAdblock: function () {
      return false;
    },
    loadingStart: function () {},
    loadingProgress: function (_progress) {},
    loadingEnd: function () {},
    gameplayStart: function () {
      gameplayStarted = true;
    },
    gameplayStop: function () {
      gameplayStarted = false;
    },
    happyTime: function () {},
    levelStart: function (_level) {},
    replayLevel: function (_level) {},
    score: function (_score) {},
    banner: function (data) {
      if (Array.isArray(data)) {
        localCrazySdk.requestBanner(data);
      }
      return false;
    },
    interstitial: function (handleGameplayStart) {
      var shouldResume = Boolean(handleGameplayStart && gameplayStarted);
      if (shouldResume) {
        Wrapper.gameplayStop();
      }
      emit("adStarted", "interstitial");
      emit("mute");
      return Promise.resolve(true).then(function (success) {
        emit("unmute");
        if (shouldResume) {
          Wrapper.gameplayStart();
        }
        return success;
      });
    },
    rewarded: function (handleGameplayStart) {
      var shouldResume = Boolean(handleGameplayStart && gameplayStarted);
      if (shouldResume) {
        Wrapper.gameplayStop();
      }
      emit("adStarted", "rewarded");
      emit("mute");
      return Promise.resolve(true).then(function (success) {
        emit("unmute");
        if (shouldResume) {
          Wrapper.gameplayStart();
        }
        return success;
      });
    },
    onAdStarted: function (fn) {
      addListener("adStarted", fn);
    },
    hasAds: function () {
      return 0;
    }
  };

  return Wrapper;
})();\n"""


def generate_local_sdk_html_stub() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Local SDK Disabled</title>
</head>
<body>
<script>
window.parent && window.parent.postMessage({ type: "local-sdk-disabled" }, "*");
</script>
</body>
</html>
"""


def neutralize_construct2_ad_network_probes(script_text: str) -> tuple[str, list[str]]:
    patched = script_text
    applied: list[str] = []

    adinplay_probe = """this.adblock = false
        var self =  this
        var xhttp = new XMLHttpRequest ();
        xhttp.onreadystatechange = function () {
            if (this.readyState === 4 && this.status === 0)
                self.adblock = true
        }
        xhttp.open ("GET", "https://api.adinplay.com/libs/aiptag/assets/adsbygoogle.js", true);
        xhttp.send ();
"""
    if adinplay_probe in patched:
        patched = patched.replace(adinplay_probe, "this.adblock = false;\n", 1)
        applied.append("adinplay_adblock_probe")

    return patched, applied


def sanitize_construct2_local_runtime(output_dir: Path) -> dict[str, Any]:
    patched_files: list[str] = []
    result: dict[str, Any] = {
        "mode": "local_no_ads_runtime",
        "patched_files": patched_files,
    }

    websdk_path = output_dir / "websdkwrapper.js"
    if websdk_path.exists():
        websdk_path.write_text(generate_local_websdkwrapper_stub(), encoding="utf-8")
        patched_files.append("websdkwrapper.js")

    adconfig_path = output_dir / "adconfig.json"
    if adconfig_path.exists():
        adconfig_payload = {
            "networks": [],
            "name": "",
            "gameId": "",
            "removeSocials": True,
            "stopAudioInBackground": False,
            "removeMidrollRewarded": True,
            "noReligion": False,
            "removeServiceWorker": True,
        }
        adconfig_path.write_text(json.dumps(adconfig_payload, indent=2) + "\n", encoding="utf-8")
        patched_files.append("adconfig.json")
        result["adconfig"] = adconfig_payload

    sdk_path = output_dir / "sdk.html"
    if sdk_path.exists():
        sdk_path.write_text(generate_local_sdk_html_stub(), encoding="utf-8")
        patched_files.append("sdk.html")

    c2runtime_path = output_dir / "c2runtime.js"
    if c2runtime_path.exists():
        original_runtime = c2runtime_path.read_text(encoding="utf-8", errors="ignore")
        patched_runtime, runtime_patches = neutralize_construct2_ad_network_probes(original_runtime)
        if runtime_patches:
            c2runtime_path.write_text(patched_runtime, encoding="utf-8")
            patched_files.append("c2runtime.js")
            result["runtime_patches"] = runtime_patches

    if patched_files:
        log("Sanitized mirrored HTML runtime for local no-ads launch")

    return result


def mirror_construct2_entry_assets(
    document_html: str,
    source_url: str,
    output_dir: Path,
) -> tuple[str, dict[str, Any]]:
    source_root_url = html_entry_source_root_url(source_url)
    candidate_urls: list[str] = []
    seen_urls: set[str] = set()

    def add_candidate(url: str) -> None:
        normalized_url = remove_query_and_fragment(normalize_url(url))
        if not relative_asset_path_under_root(normalized_url, source_root_url):
            return
        if normalized_url in seen_urls:
            return
        seen_urls.add(normalized_url)
        candidate_urls.append(normalized_url)

    external_links = extract_html_external_links(document_html)
    for url in (
        external_links["scripts"]
        + external_links["stylesheets"]
        + external_links["other_links"]
    ):
        add_candidate(url)

    offline_manifest_url = normalize_url(urllib.parse.urljoin(source_root_url, "offline.js"))
    appmanifest_url = normalize_url(urllib.parse.urljoin(source_root_url, "appmanifest.json"))
    for url in (
        normalize_url(urllib.parse.urljoin(source_root_url, "c2runtime.js")),
        normalize_url(urllib.parse.urljoin(source_root_url, "data.js")),
        normalize_url(urllib.parse.urljoin(source_root_url, "offlineClient.js")),
        normalize_url(urllib.parse.urljoin(source_root_url, "sw.js")),
        offline_manifest_url,
        appmanifest_url,
    ):
        add_candidate(url)

    offline_manifest_file_count = 0
    try:
        offline_manifest = fetch_json_document(offline_manifest_url, referer_url=source_url)
    except FetchError:
        offline_manifest = {}
    if isinstance(offline_manifest, dict):
        file_list = offline_manifest.get("fileList")
        if isinstance(file_list, list):
            for raw_path in file_list:
                if isinstance(raw_path, str) and raw_path.strip():
                    add_candidate(urllib.parse.urljoin(source_root_url, raw_path.strip()))
            offline_manifest_file_count = len(
                [item for item in file_list if isinstance(item, str) and item.strip()]
            )

    manifest_icon_count = 0
    try:
        appmanifest_payload = fetch_json_document(appmanifest_url, referer_url=source_url)
    except FetchError:
        appmanifest_payload = {}
    if isinstance(appmanifest_payload, dict):
        icons = appmanifest_payload.get("icons")
        if isinstance(icons, list):
            for item in icons:
                if not isinstance(item, dict):
                    continue
                raw_src = item.get("src")
                if isinstance(raw_src, str) and raw_src.strip():
                    add_candidate(urllib.parse.urljoin(appmanifest_url, raw_src.strip()))
                    manifest_icon_count += 1

    rewrite_map: dict[str, str] = {}
    mirrored_files: list[str] = []
    total_candidates = len(candidate_urls)
    if total_candidates:
        log(f"Mirroring HTML runtime assets: {total_candidates} files")
    for index, candidate_url in enumerate(candidate_urls, start=1):
        relative_path = relative_asset_path_under_root(candidate_url, source_root_url)
        if not relative_path:
            continue
        log(f"Mirroring HTML runtime assets: {index}/{total_candidates} -> {relative_path}")
        destination = output_dir / Path(relative_path.replace("/", os.sep))
        destination.parent.mkdir(parents=True, exist_ok=True)
        resolved_url, raw, _, _ = fetch_url(candidate_url, referer_url=source_url)
        destination.write_bytes(raw)
        local_href = "./" + relative_path.replace("\\", "/")
        rewrite_map[remove_query_and_fragment(candidate_url)] = local_href
        rewrite_map[remove_query_and_fragment(normalize_url(resolved_url))] = local_href
        mirrored_files.append(relative_path.replace("\\", "/"))

    runtime_patch_summary = sanitize_construct2_local_runtime(output_dir)
    rewritten_html = rewrite_markup_urls_to_local(document_html, rewrite_map)
    summary = {
        "mode": "construct2_local_mirror",
        "source_root_url": source_root_url,
        "offline_manifest_url": offline_manifest_url,
        "offline_manifest_file_count": offline_manifest_file_count,
        "appmanifest_url": appmanifest_url,
        "manifest_icon_count": manifest_icon_count,
        "mirrored_file_count": len(mirrored_files),
        "mirrored_files_sample": mirrored_files[:25],
    }
    if runtime_patch_summary["patched_files"]:
        summary["runtime_sanitizer"] = runtime_patch_summary
    return rewritten_html, summary


def generate_crazygames_sdk_stub() -> str:
    return """(function () {
  var root = window.CrazyGames = window.CrazyGames || {};
  var sdk = root.SDK = root.SDK || {};
  var ad = sdk.ad = sdk.ad || {};
  var banner = sdk.banner = sdk.banner || {};
  var data = sdk.data = sdk.data || {};
  var environment = sdk.environment = sdk.environment || {};
  var game = sdk.game = sdk.game || {};
  var user = sdk.user = sdk.user || {};
  var storagePrefix = "__unity_standalone_crazygames__:";
  var authListeners = [];

  function resolved(value) {
    return Promise.resolve(value);
  }

  function safeCall(callback) {
    if (typeof callback !== "function") {
      return;
    }
    try {
      callback.apply(null, Array.prototype.slice.call(arguments, 1));
    } catch (_err) {}
  }

  function readStorageValue(key) {
    var normalized = String(key == null ? "" : key);
    try {
      var prefixed = window.localStorage.getItem(storagePrefix + normalized);
      if (prefixed !== null) {
        return prefixed;
      }
      return window.localStorage.getItem(normalized);
    } catch (_err) {
      return null;
    }
  }

  function writeStorageValue(key, value) {
    var normalized = String(key == null ? "" : key);
    var stringValue = String(value == null ? "" : value);
    try {
      window.localStorage.setItem(storagePrefix + normalized, stringValue);
      window.localStorage.setItem(normalized, stringValue);
    } catch (_err) {}
    return stringValue;
  }

  function removeStorageValue(key) {
    var normalized = String(key == null ? "" : key);
    try {
      window.localStorage.removeItem(storagePrefix + normalized);
      window.localStorage.removeItem(normalized);
    } catch (_err) {}
  }

  sdk.addInitCallback = function (callback) {
    safeCall(callback, {});
  };
  sdk.init = function () {
    return resolved({});
  };
  ad.hasAdblock = function (callback) {
    safeCall(callback, null, false);
    return resolved(false);
  };
  ad.requestAd = function (_adType, callbacks) {
    callbacks = callbacks || {};
    safeCall(callbacks.adStarted);
    safeCall(callbacks.adFinished);
    safeCall(callbacks.adComplete);
    safeCall(callbacks.adDismissed);
    return resolved("closed");
  };
  banner.requestOverlayBanners = function (_banners, callback) {
    safeCall(callback, "", "bannerRendered", null);
    return resolved("bannerRendered");
  };
  data.getItem = function (key) {
    return readStorageValue(key);
  };
  data.setItem = function (key, value) {
    return writeStorageValue(key, value);
  };
  data.removeItem = function (key) {
    removeStorageValue(key);
  };
  data.clear = function () {
    try {
      Object.keys(window.localStorage).forEach(function (key) {
        if (key.indexOf(storagePrefix) === 0) {
          window.localStorage.removeItem(key);
        }
      });
    } catch (_err) {}
  };
  data.syncUnityGameData = function () {
    return resolved();
  };
  game.gameplayStart = function () {
    return resolved();
  };
  game.gameplayStop = function () {
    return resolved();
  };
  game.happytime = function () {
    return resolved();
  };
  game.hideInviteButton = function () {
    return resolved();
  };
  game.showInviteButton = function () {
    return resolved();
  };
  game.inviteLink = function () {
    return resolved("");
  };
  user.addAuthListener = function (callback) {
    if (typeof callback === "function") {
      authListeners.push(callback);
    }
    safeCall(callback, {});
    return function () {};
  };
  user.addScore = function () {
    return resolved();
  };
  user.getUser = function () {
    return resolved({});
  };
  user.getUserToken = function () {
    return resolved("");
  };
  user.getXsollaUserToken = function () {
    return resolved("");
  };
  user.showAccountLinkPrompt = function () {
    return resolved({});
  };
  user.showAuthPrompt = function () {
    return resolved({});
  };
  if (typeof user.systemInfo !== "object" || !user.systemInfo) {
    user.systemInfo = {
      countryCode: "",
      locale: navigator.language || "en-US",
      os: navigator.platform || "",
      browser: navigator.userAgent || "",
    };
  }
  if (typeof user.isUserAccountAvailable !== "boolean") {
    user.isUserAccountAvailable = false;
  }
  if (typeof environment !== "object" || !environment) {
    environment = sdk.environment = {};
  }
  if (typeof environment.platform !== "string") {
    environment.platform = "web";
  }
  if (typeof environment.device !== "string") {
    environment.device = /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent)
      ? "mobile"
      : "desktop";
  }
  sdk.isQaTool = function () {
    return false;
  };

  var legacyRoot = window.Crazygames = window.Crazygames || {};
  if (typeof legacyRoot.requestInviteUrl !== "function") {
    legacyRoot.requestInviteUrl = function () {};
  }
  root.init = sdk.init;
})();\n"""


def write_vendor_support_files(output_dir: Path, framework_analysis: FrameworkAnalysis) -> None:
    if framework_analysis.requires_crazygames_sdk:
        vendor_dir = output_dir / "vs"
        vendor_dir.mkdir(parents=True, exist_ok=True)
        stub = generate_crazygames_sdk_stub()
        for file_name in (
            "crazygames-sdk-v2.js",
            "crazygames-sdk-v3.js",
        ):
            (vendor_dir / file_name).write_text(stub, encoding="utf-8")


def compute_asset_cache_buster(build_dir: Path, assets: DownloadedAssets) -> str:
    digest = hashlib.sha256()
    for asset_name in (
        assets.loader_name,
        assets.framework_name,
        assets.data_name,
        assets.wasm_name,
    ):
        if not asset_name:
            continue
        asset_path = build_dir / asset_name
        if not asset_path.exists():
            continue
        stats = asset_path.stat()
        digest.update(asset_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stats.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stats.st_mtime_ns).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def generate_index_html(
    product_name: str,
    assets: DownloadedAssets,
    required_functions: Sequence[str],
    window_roots: Sequence[str],
    window_callable_chains: Sequence[str],
    support_script_filenames: Sequence[str] = (),
    source_page_url: str = "",
    enable_source_url_spoof: bool = False,
    original_folder_url: str = "",
    streaming_assets_url: str = "",
    asset_cache_buster: str = "",
    page_config: Mapping[str, Any] | None = None,
    auxiliary_asset_rewrites: dict[str, str] | None = None,
    allowed_launch_modes: str = "both",
    recommended_launch_mode: str = "none",
) -> str:
    allowed_launch_modes, recommended_launch_mode = normalize_launch_preferences(
        allowed_launch_modes,
        recommended_launch_mode,
    )
    fn_list_js = json.dumps(list(required_functions), ensure_ascii=False)
    window_roots_js = json.dumps(list(window_roots), ensure_ascii=False)
    window_callable_chains_js = json.dumps(list(window_callable_chains), ensure_ascii=False)
    product_name_js = json.dumps(product_name, ensure_ascii=False)
    loader_name_js = json.dumps(assets.loader_name, ensure_ascii=False)
    data_name_js = json.dumps(assets.data_name, ensure_ascii=False)
    framework_name_js = json.dumps(assets.framework_name, ensure_ascii=False)
    wasm_name_js = json.dumps(assets.wasm_name, ensure_ascii=False)
    build_kind_js = json.dumps(assets.build_kind, ensure_ascii=False)
    legacy_config_js = json.dumps(assets.legacy_config, ensure_ascii=False)
    source_page_url_js = json.dumps(source_page_url, ensure_ascii=False)
    enable_source_url_spoof_js = "true" if enable_source_url_spoof else "false"
    original_folder_url_js = json.dumps(original_folder_url, ensure_ascii=False)
    streaming_assets_url_js = json.dumps(streaming_assets_url, ensure_ascii=False)
    asset_cache_buster_js = json.dumps(asset_cache_buster, ensure_ascii=False)
    page_config_js = json.dumps(page_config or {}, ensure_ascii=False)
    allowed_launch_modes_js = json.dumps(allowed_launch_modes, ensure_ascii=False)
    recommended_launch_mode_js = json.dumps(recommended_launch_mode, ensure_ascii=False)
    auxiliary_asset_rewrites_js = json.dumps(
        auxiliary_asset_rewrites or {},
        ensure_ascii=False,
    )
    support_script_tags = "\n".join(
        f'  <script src="./{html.escape(filename)}"></script>'
        for filename in support_script_filenames
    )
    decompression_fallback_line = (
        "  config.decompressionFallback = true;\n" if assets.used_br_assets else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no" />
  <meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate, max-age=0" />
  <meta http-equiv="Pragma" content="no-cache" />
  <meta http-equiv="Expires" content="0" />
  <title>{html.escape(product_name)}</title>
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 16 16%22%3E%3Crect width=%2216%22 height=%2216%22 rx=%224%22 fill=%22%2305070f%22/%3E%3Ccircle cx=%228%22 cy=%228%22 r=%223.5%22 fill=%22%2322d3ee%22/%3E%3C/svg%3E" />
  <style>
    :root {{
      color-scheme: dark;
      --bg: #05070f;
      --cyan: #22d3ee;
      --blue: #3b82f6;
      --violet: #a78bfa;
      --mint: #4ade80;
      --text: rgba(255, 255, 255, 0.92);
    }}
    * {{
      box-sizing: border-box;
    }}
    html, body {{
      margin: 0;
      height: 100%;
      overflow: hidden;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI Variable Text", "Segoe UI", "Trebuchet MS", system-ui, sans-serif;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }}
    body {{
      background: var(--bg);
    }}
    html[data-ocean-fullscreen-lock="1"],
    html[data-ocean-fullscreen-lock="1"] body,
    body[data-ocean-fullscreen-lock="1"] {{
      overflow: hidden !important;
      overscroll-behavior: none;
    }}
    html[data-ocean-fullscreen-lock="1"] #container,
    html[data-ocean-fullscreen-lock="1"] #loadingScreen {{
      touch-action: none;
    }}
    #container {{
      position: fixed;
      inset: 0;
      background: var(--bg);
    }}
    #unity-canvas {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      display: block;
      background: #000;
    }}
    #unity-legacy-container {{
      position: absolute;
      inset: 0;
      display: none;
      background: #000;
    }}
    #unity-legacy-container canvas {{
      width: 100% !important;
      height: 100% !important;
      display: block;
    }}
    #loadingScreen {{
      position: absolute;
      inset: 0;
      z-index: 20;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: clamp(20px, 3vw, 36px);
      box-sizing: border-box;
      overflow: hidden;
      animation: loadingScreenEnter 900ms ease both;
      background: var(--bg);
    }}
    #loadingScreen.is-exiting {{
      animation: loadingScreenExit 900ms ease forwards;
      pointer-events: none;
    }}
    #loadingBackdrop {{
      position: absolute;
      inset: 0;
      z-index: 0;
      overflow: hidden;
      pointer-events: none;
      background: var(--bg);
    }}
    #star-canvas {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      z-index: 0;
      filter: saturate(1.05) contrast(1.03);
    }}
    #wave-canvas {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      z-index: 1;
      opacity: 0.95;
      pointer-events: none;
    }}
    .nebula {{
      position: absolute;
      inset: -12%;
      z-index: 2;
      pointer-events: none;
      opacity: 0.55;
      background:
        radial-gradient(1200px 800px at 50% 20%, rgba(59, 130, 246, 0.14), transparent 62%),
        radial-gradient(900px 600px at 15% 60%, rgba(34, 211, 238, 0.10), transparent 58%),
        radial-gradient(800px 600px at 85% 70%, rgba(167, 139, 250, 0.10), transparent 58%);
      animation: nebulaFloatA 22s ease-in-out infinite;
      transform: translate3d(0, 0, 0);
      will-change: transform;
    }}
    .nebula::before {{
      content: "";
      position: absolute;
      inset: -14%;
      background:
        radial-gradient(900px 650px at 30% 25%, rgba(74, 222, 128, 0.08), transparent 62%),
        radial-gradient(1100px 700px at 70% 40%, rgba(59, 130, 246, 0.07), transparent 64%),
        radial-gradient(900px 700px at 60% 85%, rgba(34, 211, 238, 0.06), transparent 60%);
      opacity: 0.9;
      animation: nebulaFloatB 32s ease-in-out infinite;
      transform: translate3d(0, 0, 0);
    }}
    @keyframes nebulaFloatA {{
      0%, 100% {{
        transform: translate3d(-1%, -0.6%, 0) scale(1.02);
      }}
      50% {{
        transform: translate3d(1%, 0.6%, 0) scale(1.03);
      }}
    }}
    @keyframes nebulaFloatB {{
      0%, 100% {{
        transform: translate3d(0.6%, -1%, 0) scale(1.02);
      }}
      50% {{
        transform: translate3d(-0.6%, 1%, 0) scale(1.03);
      }}
    }}
    .overlay {{
      position: absolute;
      inset: 0;
      z-index: 3;
      pointer-events: none;
      background:
        radial-gradient(1200px 900px at 50% 30%, transparent 38%, rgba(0, 0, 0, 0.52) 86%),
        radial-gradient(900px 700px at 50% 80%, rgba(0, 0, 0, 0.10), rgba(0, 0, 0, 0.70));
      mix-blend-mode: multiply;
    }}
    .grain {{
      position: absolute;
      inset: -30%;
      z-index: 4;
      pointer-events: none;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.8' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='180' height='180' filter='url(%23n)' opacity='.22'/%3E%3C/svg%3E");
      opacity: 0.07;
      transform: rotate(6deg);
      animation: grainMove 10s steps(10) infinite;
    }}
    @keyframes grainMove {{
      0% {{
        transform: translate3d(-2%, -2%, 0) rotate(6deg);
      }}
      100% {{
        transform: translate3d(2%, 2%, 0) rotate(6deg);
      }}
    }}
    @keyframes loadingScreenEnter {{
      0% {{
        opacity: 0;
        transform: scale(1.02);
      }}
      100% {{
        opacity: 1;
        transform: scale(1);
      }}
    }}
    @keyframes loadingScreenExit {{
      0% {{
        opacity: 1;
        transform: scale(1);
      }}
      100% {{
        opacity: 0;
        transform: scale(1.015);
      }}
    }}
    #loadingCenter {{
      position: relative;
      z-index: 5;
      width: min(92vw, 560px);
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 16px;
      padding: 0 22px 18px;
      overflow: visible;
      text-align: center;
      text-shadow: 0 10px 30px rgba(0, 0, 0, 0.45);
    }}
    #loadingTitleGroup {{
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 4px;
      margin-bottom: 4px;
    }}
    #loadingTitle {{
      margin: 0;
      padding-left: 0.28em;
      font-size: clamp(3rem, 10vw, 6.4rem);
      font-weight: 900;
      letter-spacing: 0.28em;
      line-height: 0.88;
      text-transform: uppercase;
      background: linear-gradient(90deg, rgba(255, 255, 255, 0.98), rgba(171, 239, 255, 0.98), rgba(110, 189, 255, 0.98));
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
      filter: drop-shadow(0 10px 30px rgba(0, 0, 0, 0.35));
    }}
    #loadingSubtitle {{
      margin: 0;
      padding-left: 0.72em;
      color: rgba(225, 245, 255, 0.78);
      font-size: clamp(0.72rem, 1.8vw, 0.98rem);
      font-weight: 700;
      letter-spacing: 0.72em;
      line-height: 1;
    }}
    #launchPanel {{
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 12px;
      width: 100%;
      padding: 14px 18px 18px;
      margin: -14px -18px -18px;
      overflow: visible;
      transition:
        opacity 240ms ease,
        transform 240ms ease;
    }}
    #launchPanel.is-hidden {{
      opacity: 0;
      transform: translateY(10px);
      pointer-events: none;
    }}
    .launchOption {{
      width: 100%;
      padding: 15px 22px;
      border: 1px solid rgba(120, 196, 255, 0.30);
      border-radius: 999px;
      background: linear-gradient(180deg, rgba(10, 16, 31, 0.86), rgba(10, 18, 38, 0.96));
      color: #effcff;
      font: 800 14px/1.1 "Segoe UI Variable Text", "Segoe UI", "Trebuchet MS", system-ui, sans-serif;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      cursor: pointer;
      text-align: center;
      box-shadow:
        0 14px 34px rgba(0, 0, 0, 0.32),
        inset 0 1px 0 rgba(255, 255, 255, 0.08);
      transition:
        transform 180ms ease,
        box-shadow 180ms ease,
        background 180ms ease,
        border-color 180ms ease;
    }}
    .launchOption:hover {{
      transform: translateY(-1px);
      border-color: rgba(88, 200, 255, 0.62);
      background: linear-gradient(180deg, rgba(16, 28, 58, 0.94), rgba(10, 22, 46, 0.98));
      box-shadow:
        0 18px 36px rgba(0, 0, 0, 0.34),
        0 0 24px rgba(59, 130, 246, 0.22);
    }}
    #launchMenu {{
      display: flex;
      width: 100%;
      flex-direction: column;
      gap: 10px;
    }}
    .launchOption {{
      font-size: 14px;
      padding: 14px 18px;
    }}
    #playNote {{
      color: rgba(255, 255, 255, 0.62);
      font-size: 12px;
      letter-spacing: 0.04em;
      text-align: center;
      line-height: 1.35;
    }}
    #status {{
      color: rgba(255, 255, 255, 0.9);
      font-size: 15px;
      font-weight: 700;
      letter-spacing: -0.01em;
      text-shadow: 0 2px 18px rgba(0, 0, 0, 0.38);
    }}
    #stepLog {{
      position: absolute;
      right: 14px;
      bottom: 10px;
      z-index: 6;
      max-width: min(46vw, 540px);
      color: rgba(230, 244, 255, 0.58);
      font: 500 10px/1.38 "Cascadia Mono", Consolas, "Courier New", monospace;
      text-align: right;
      white-space: pre-line;
      pointer-events: none;
      text-shadow: none;
      opacity: 0.96;
    }}
    #progressTrack {{
      display: none;
      width: 100%;
      height: 8px;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(255, 255, 255, 0.10);
      border: 1px solid rgba(255, 255, 255, 0.08);
      box-shadow: inset 0 2px 8px rgba(0, 0, 0, 0.28);
    }}
    #progressTrack.is-visible {{
      display: block;
    }}
    #progressFill {{
      width: 0%;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--mint), var(--cyan), var(--blue), var(--violet));
      background-size: 300% 100%;
      box-shadow: 0 0 24px rgba(59, 130, 246, 0.34);
      transition: width 220ms ease;
      animation: progressGlow 6.5s ease-in-out infinite;
    }}
    @keyframes progressGlow {{
      0% {{
        background-position: 0% 50%;
      }}
      50% {{
        background-position: 100% 50%;
      }}
      100% {{
        background-position: 0% 50%;
      }}
    }}
    @media (max-width: 640px) {{
      #loadingCenter {{
        width: min(94vw, 560px);
        padding: 0 16px 16px;
      }}
      .launchOption {{
        font-size: 13px;
      }}
      #status {{
        font-size: 14px;
      }}
      #stepLog {{
        right: 10px;
        bottom: 8px;
        max-width: 72vw;
        font-size: 9px;
      }}
    }}
    @media (max-height: 760px) {{
      #loadingScreen {{
        padding-top: 16px;
        padding-bottom: 16px;
      }}
      #loadingCenter {{
        gap: 14px;
      }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      .grain,
      .nebula,
      #progressFill {{
        animation: none !important;
      }}
    }}
  </style>
</head>
<body>
  <div id="container">
    <canvas id="unity-canvas"></canvas>
    <div id="unity-legacy-container"></div>
    <div id="loadingScreen">
      <div id="loadingBackdrop" aria-hidden="true">
        <canvas id="star-canvas"></canvas>
        <canvas id="wave-canvas"></canvas>
        <div class="nebula"></div>
        <div class="overlay"></div>
        <div class="grain"></div>
      </div>
      <div id="loadingCenter">
        <div id="loadingTitleGroup">
          <h1 id="loadingTitle">Ocean</h1>
          <div id="loadingSubtitle">LAUNCHER</div>
        </div>
        <div id="launchPanel">
          <div id="launchMenu">
            <button id="launchFrameBtn" class="launchOption" type="button">LAUNCH HERE</button>
            <button id="launchFullscreenBtn" class="launchOption" type="button">LAUNCH FULLSCREEN</button>
          </div>
          <div id="playNote">Saves to local storage</div>
        </div>
        <div id="progressTrack" aria-hidden="true">
          <div id="progressFill"></div>
        </div>
        <div id="status">Awaiting launch-mode selection</div>
      </div>
      <div id="stepLog" aria-live="polite" aria-atomic="false"></div>
    </div>
  </div>

{support_script_tags}
  <script>
    (function () {{
      const TRUE = "true";
      const FALSE = "false";
      const EMPTY = "";
      const ZERO = "0";
      const LOCAL_PAGE_URL = window.__unityStandaloneLocalPageUrl || window.location.href;
      const SOURCE_PAGE_URL = {source_page_url_js};
      const ENABLE_SOURCE_URL_SPOOF = {enable_source_url_spoof_js};
      const SCRIPT_SRC_REDIRECTS = {{
        "/vs/crazygames-sdk-v2.js": "./vs/crazygames-sdk-v2.js",
        "https://sdk.crazygames.com/crazygames-sdk-v2.js": "./vs/crazygames-sdk-v2.js",
        "https://sdk.crazygames.com/crazygames-sdk-v3.js": "./vs/crazygames-sdk-v3.js",
      }};
      const STORAGE_PREFIX = "__unity_standalone_ls__:";
      const LEGACY_STORAGE_PREFIX = "__pg_standalone_ls__:";
      const AD_STATE_LOADING = "loading";
      const AD_STATE_OPENED = "opened";
      const AD_STATE_CLOSED = "closed";
      const AD_STATE_REWARDED = "rewarded";
      window.__unityStandaloneLocalPageUrl = LOCAL_PAGE_URL;
      if (SOURCE_PAGE_URL) {{
        window.__unityStandaloneSourcePageUrl = SOURCE_PAGE_URL;
        try {{
          window.__unityStandaloneSourceHost = new URL(SOURCE_PAGE_URL).hostname || "";
        }} catch (err) {{
          window.__unityStandaloneSourceHost = "";
        }}
      }} else if (typeof window.__unityStandaloneSourceHost !== "string") {{
        window.__unityStandaloneSourceHost = "";
      }}

      function rewriteVendorScriptUrl(value) {{
        if (typeof value !== "string") {{
          return value;
        }}
        if (Object.prototype.hasOwnProperty.call(SCRIPT_SRC_REDIRECTS, value)) {{
          return SCRIPT_SRC_REDIRECTS[value];
        }}
        try {{
          const parsed = new URL(value, LOCAL_PAGE_URL);
          const fileName = (parsed.pathname.split("/").pop() || "").toLowerCase();
          if (
            parsed.hostname === "sdk.crazygames.com" &&
            /^crazygames-sdk-v\\d+\\.js$/.test(fileName)
          ) {{
            return "./vs/" + fileName + parsed.search + parsed.hash;
          }}
          const localPage = new URL(LOCAL_PAGE_URL);
          const sameFileOrigin =
            parsed.protocol === "file:" && localPage.protocol === "file:";
          const sameHttpOrigin =
            parsed.origin === localPage.origin && parsed.origin !== "null";
          if (sameFileOrigin || sameHttpOrigin) {{
            const mapped = SCRIPT_SRC_REDIRECTS[parsed.pathname];
            if (mapped) {{
              return mapped + parsed.search + parsed.hash;
            }}
          }}
        }} catch (err) {{
          return value;
        }}
        return value;
      }}

      (function patchVendorScriptUrls() {{
        if (typeof HTMLScriptElement === "undefined") {{
          return;
        }}
        const descriptor = Object.getOwnPropertyDescriptor(
          HTMLScriptElement.prototype,
          "src"
        );
        if (
          descriptor &&
          typeof descriptor.get === "function" &&
          typeof descriptor.set === "function"
        ) {{
          Object.defineProperty(HTMLScriptElement.prototype, "src", {{
            configurable: true,
            enumerable: descriptor.enumerable,
            get: function () {{
              return descriptor.get.call(this);
            }},
            set: function (value) {{
              return descriptor.set.call(this, rewriteVendorScriptUrl(value));
            }},
          }});
        }}
        if (typeof Element === "undefined") {{
          return;
        }}
        const originalSetAttribute = Element.prototype.setAttribute;
        Element.prototype.setAttribute = function (name, value) {{
          if (this.tagName === "SCRIPT" && String(name).toLowerCase() === "src") {{
            value = rewriteVendorScriptUrl(value);
          }}
          return originalSetAttribute.call(this, name, value);
        }};
      }})();

      const noop = function () {{}};
      let interstitialState = AD_STATE_CLOSED;
      let rewardedState = AD_STATE_REWARDED;
      let bannerState = "hidden";
      let minimumDelayBetweenInterstitial = ZERO;

      const completeInterstitial = function () {{
        interstitialState = AD_STATE_OPENED;
        interstitialState = AD_STATE_CLOSED;
        return AD_STATE_CLOSED;
      }};
      const completeRewarded = function () {{
        rewardedState = AD_STATE_OPENED;
        rewardedState = AD_STATE_REWARDED;
        return AD_STATE_REWARDED;
      }};
      const storageSupported = typeof window !== "undefined" && typeof window.localStorage !== "undefined";
      const probeStorageAvailable = function () {{
        if (!storageSupported) {{
          return false;
        }}
        try {{
          const probeKey = STORAGE_PREFIX + "__probe__";
          window.localStorage.setItem(probeKey, "1");
          const ok = window.localStorage.getItem(probeKey) === "1";
          window.localStorage.removeItem(probeKey);
          return ok;
        }} catch (err) {{
          return false;
        }}
      }};
      let storageAvailable = probeStorageAvailable();
      const refreshStorageAvailability = function () {{
        storageAvailable = probeStorageAvailable();
        return storageAvailable;
      }};
      const storageKey = function (key) {{
        return STORAGE_PREFIX + String(key);
      }};
      const legacyStorageKey = function (key) {{
        return LEGACY_STORAGE_PREFIX + String(key);
      }};

      function buildMadHookSettings() {{
        const allowedLocalHosts = ["localhost", "127.0.0.1", "::1"];
        const allowedRemoteHosts = [];
        if (SOURCE_PAGE_URL) {{
          try {{
            const sourceUrl = new URL(SOURCE_PAGE_URL);
            if (sourceUrl.hostname) {{
              allowedRemoteHosts.push(sourceUrl.hostname);
            }}
          }} catch (err) {{
            console.warn("Failed to parse source page URL:", err);
          }}
        }}
        try {{
          const localUrl = new URL(LOCAL_PAGE_URL);
          if (localUrl.hostname) {{
            allowedRemoteHosts.push(localUrl.hostname);
          }}
        }} catch (err) {{
          console.warn("Failed to parse local page URL:", err);
        }}
        const uniqueRemoteHosts = Array.from(new Set(allowedRemoteHosts.filter(Boolean)));
        const whitelistedDomains = Array.from(
          new Set(uniqueRemoteHosts.concat(allowedLocalHosts))
        );
        return {{
          allowedLocalHosts: allowedLocalHosts,
          allowedRemoteHosts: uniqueRemoteHosts,
          whitelistedDomains: whitelistedDomains,
          sourcePageUrl: SOURCE_PAGE_URL || EMPTY,
          localPageUrl: LOCAL_PAGE_URL,
          isQaTool: false,
          hasAdblock: false,
          siteLockEnabled: ENABLE_SOURCE_URL_SPOOF,
        }};
      }}

      function getMadHookSettingsJson() {{
        return JSON.stringify(buildMadHookSettings());
      }}

      function getUrlParametersJson() {{
        const payload = {{}};
        try {{
          const localUrl = new URL(LOCAL_PAGE_URL);
          localUrl.searchParams.forEach(function (value, key) {{
            payload[key] = value;
          }});
        }} catch (err) {{
          console.warn("Failed to parse URL parameters:", err);
        }}
        return JSON.stringify(payload);
      }}

      function getOfflineUser() {{
        return {{
          userId: "offline_player",
          username: "Player",
          displayName: "Player",
        }};
      }}

      function getEnvironmentPayload() {{
        const language =
          (navigator.language || navigator.userLanguage || "en").split("-")[0] || "en";
        const deviceType = /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent || "")
          ? "mobile"
          : "desktop";
        return {{
          language: language,
          lang: language,
          deviceType: deviceType,
          isMobile: deviceType === "mobile",
          platform: "web",
          host: window.location.hostname || "localhost",
          sourceHost: window.__unityStandaloneSourceHost || "",
        }};
      }}

      function getUnitySendTargets() {{
        const targets = [
          window.myGameInstance,
          window.gameInstance,
          window.unityInstance,
        ];
        return targets.filter(function (target, index) {{
          return (
            target &&
            typeof target.SendMessage === "function" &&
            targets.indexOf(target) === index
          );
        }});
      }}

      function safeUnitySend(objectName, methodName, value) {{
        getUnitySendTargets().forEach(function (target) {{
          try {{
            if (typeof value === "undefined") {{
              target.SendMessage(objectName, methodName);
            }} else {{
              target.SendMessage(objectName, methodName, value);
            }}
          }} catch (err) {{
            // Ignore unsupported game-specific message bridges.
          }}
        }});
      }}

      const known = {{
        // Ads
        getInterstitialState: () => interstitialState,
        getRewardedState: () => rewardedState,
        getBannerState: () => bannerState,
        getRewardedPlacement: () => EMPTY,
        getMinimumDelayBetweenInterstitial: () => minimumDelayBetweenInterstitial,
        showInterstitial: () => completeInterstitial(),
        showRewarded: () => completeRewarded(),
        showReward: () => completeRewarded(),
        showBanner: () => {{
          bannerState = "shown";
          return "shown";
        }},
        hideBanner: () => {{
          bannerState = "hidden";
          return "hidden";
        }},
        setMinimumDelayBetweenInterstitial: (value) => {{
          minimumDelayBetweenInterstitial = String(value ?? ZERO);
          return minimumDelayBetweenInterstitial;
        }},
        getIsInterstitialSupported: () => TRUE,
        getIsRewardedSupported: () => TRUE,
        getIsBannerSupported: () => TRUE,
        // Storage
        getIsStorageSupported: () => (storageSupported ? TRUE : FALSE),
        getIsStorageAvailable: () => (refreshStorageAvailability() ? TRUE : FALSE),
        getStorageDefaultType: () => "local_storage",
        setStorageData: (key, value) => {{
          if (!refreshStorageAvailability()) {{
            return;
          }}
          try {{
            window.localStorage.setItem(storageKey(key), String(value));
          }} catch (err) {{
            console.warn("setStorageData failed:", err);
          }}
        }},
        getStorageData: (key) => {{
          if (!refreshStorageAvailability()) {{
            return EMPTY;
          }}
          try {{
            const value = window.localStorage.getItem(storageKey(key));
            if (value != null) {{
              return value;
            }}
            const legacyValue = window.localStorage.getItem(legacyStorageKey(key));
            return legacyValue == null ? EMPTY : legacyValue;
          }} catch (err) {{
            return EMPTY;
          }}
        }},
        deleteStorageData: (key) => {{
          if (!refreshStorageAvailability()) {{
            return;
          }}
          try {{
            window.localStorage.removeItem(storageKey(key));
            window.localStorage.removeItem(legacyStorageKey(key));
          }} catch (err) {{
            console.warn("deleteStorageData failed:", err);
          }}
        }},
        // Player / platform
        getPlayerId: () => "offline_player",
        getPlayerName: () => "Player",
        getPlayerPhotos: () => EMPTY,
        getPlayerExtra: () => EMPTY,
        getIsPlayerAuthorized: () => TRUE,
        getIsPlayerAuthorizationSupported: () => FALSE,
        authorizePlayer: noop,
        getPlatformId: () => "web",
        getPlatformLanguage: () => navigator.language || "en",
        getPlatformPayload: () => EMPTY,
        getPlatformTld: () => "local",
        getDeviceType: () => {{
          const ua = navigator.userAgent || "";
          return /Mobi|Android|iPhone|iPad|iPod/i.test(ua) ? "mobile" : "desktop";
        }},
        GetDeviceType: () => {{
          const ua = navigator.userAgent || "";
          return /Mobi|Android|iPhone|iPad|iPod/i.test(ua) ? "mobile" : "desktop";
        }},
        GetLanguage: () => (navigator.language || "en").split("-")[0] || "en",
        IsMobile: () => (/Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent || "") ? 1 : 0),
        getVisibilityState: () => document.visibilityState || "visible",
        getIsPlatformAudioEnabled: () => TRUE,
        getIsExternalLinksAllowed: () => TRUE,
        sendMessageToPlatform: (msg) => {{
          console.log("Platform message:", msg);
        }},
        // Achievements / social / leaderboards / payments
        achievementsGetList: noop,
        achievementsUnlock: noop,
        achievementsShowNativePopup: noop,
        addToFavorites: noop,
        addToHomeScreen: noop,
        inviteFriends: noop,
        joinCommunity: noop,
        share: noop,
        createPost: noop,
        rate: noop,
        leaderboardsGetEntries: noop,
        leaderboardsSetScore: noop,
        leaderboardsShowNativePopup: noop,
        paymentsGetCatalog: noop,
        paymentsGetPurchases: noop,
        paymentsPurchase: noop,
        paymentsConsumePurchase: noop,
        // Remote / misc
        remoteConfigGet: () => EMPTY,
        checkAdBlock: () => FALSE,
        getAllGames: noop,
        getGameById: noop,
        getServerTime: () => String(Date.now()),
        unityStringify: (value) => {{
          if (value == null) {{
            return EMPTY;
          }}
          if (typeof value === "string") {{
            return value;
          }}
          try {{
            if (typeof window.UTF8ToString === "function") {{
              return window.UTF8ToString(value);
            }}
          }} catch (err) {{
            // Fall back to String(value) below.
          }}
          return String(value);
        }},
        getUserMedia: (...args) => {{
          const legacyGetUserMedia =
            navigator.getUserMedia ||
            navigator.webkitGetUserMedia ||
            navigator.mozGetUserMedia;
          if (typeof legacyGetUserMedia === "function") {{
            return legacyGetUserMedia.apply(navigator, args);
          }}
          return EMPTY;
        }},
        InitSDKJs: function () {{
          patchUnitySdk();
          safeCall(window.UnitySDK && window.UnitySDK.onSdkScriptLoaded, []);
          window.setTimeout(function () {{
            safeUnitySend("RHMAdsManager", "InitSucceed", "standalone");
          }}, 0);
          return TRUE;
        }},
        InitGame: function () {{
          return JSON.stringify(getEnvironmentPayload());
        }},
        InitLeaderboard: () => TRUE,
        showNextAd: function () {{
          completeInterstitial();
          window.setTimeout(function () {{
            safeUnitySend("RHMAdsManager", "resumeGame");
          }}, 0);
          return AD_STATE_CLOSED;
        }},
        CallInterstitialAdsJs: function () {{
          completeInterstitial();
          window.setTimeout(function () {{
            safeUnitySend("RHMAdsManager", "resumeGame");
          }}, 0);
          return AD_STATE_CLOSED;
        }},
        CallInterstitialAdsPauseJs: function () {{
          completeInterstitial();
          window.setTimeout(function () {{
            safeUnitySend("RHMAdsManager", "resumeAudio");
          }}, 0);
          return AD_STATE_CLOSED;
        }},
        LoadRewardedAdsJs: function () {{
          window.setTimeout(function () {{
            safeUnitySend("RHMAdsManager", "isRewardedAdsLoaded", "true");
          }}, 0);
          return TRUE;
        }},
        CallRewardedAdsJs: function () {{
          completeRewarded();
          window.setTimeout(function () {{
            safeUnitySend("RHMAdsManager", "RewardedAdsSuccessfull");
          }}, 0);
          return AD_STATE_REWARDED;
        }},
        FullAdShow: () => completeInterstitial(),
        RewardedShow: () => completeRewarded(),
        GameReadyAPI: () => TRUE,
        RequestingEnvironmentData: () => JSON.stringify(getEnvironmentPayload()),
        LanguageRequest: () => (navigator.language || "en").split("-")[0] || "en",
        OpenAuthDialog: () => FALSE,
        PromptShow: noop,
        Review: noop,
        GetPayments: () => "[]",
        BuyPayments: () => FALSE,
        ConsumePurchase: () => FALSE,
        ConsumePurchases: () => FALSE,
        GetLeaderboardScores: () => "[]",
        SetLeaderboardScores: noop,
        LoadCloud: () => "{{}}",
        SaveCloud: () => TRUE,
        ActivityRTB1: noop,
        ActivityRTB2: noop,
        ExecuteCodeRTB1: noop,
        ExecuteCodeRTB2: noop,
        PaintRBT: noop,
        RecalculateRTB1: noop,
        RecalculateRTB2: noop,
        StaticRBTDeactivate: noop,
        StickyAdActivity: noop,
        ym: noop,
        // Mad Hook / CrazySDK bridge
        InitSDK: function (...args) {{
          patchUnitySdk();
          const callbacks = args.filter((arg) => typeof arg === "function");
          safeRunCallbacks(callbacks, [buildMadHookSettings()]);
          safeCall(window.UnitySDK && window.UnitySDK.onSdkScriptLoaded, []);
          return TRUE;
        }},
        RequestAdSDK: function (...args) {{
          interstitialState = AD_STATE_OPENED;
          rewardedState = AD_STATE_OPENED;
          const callbackBag = args.find((arg) => arg && typeof arg === "object" && !Array.isArray(arg));
          const callbacks = args.filter((arg) => typeof arg === "function");
          if (callbackBag) {{
            safeCall(callbackBag.adStarted, []);
            safeCall(callbackBag.adFinished, []);
            safeCall(callbackBag.adComplete, []);
            safeCall(callbackBag.complete, []);
          }}
          safeRunCallbacks(callbacks, [AD_STATE_CLOSED]);
          interstitialState = AD_STATE_CLOSED;
          rewardedState = AD_STATE_REWARDED;
          return AD_STATE_CLOSED;
        }},
        HappyTimeSDK: noop,
        GameplayStartSDK: noop,
        GameplayStopSDK: noop,
        RequestInviteUrlSDK: function (...args) {{
          const inviteUrl = SOURCE_PAGE_URL || LOCAL_PAGE_URL;
          const callbacks = args.filter((arg) => typeof arg === "function");
          safeRunCallbacks(callbacks, [inviteUrl]);
          return inviteUrl;
        }},
        ShowInviteButtonSDK: noop,
        HideInviteButtonSDK: noop,
        CopyToClipboardSDK: function (value) {{
          const text = value == null ? EMPTY : String(value);
          if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {{
            navigator.clipboard.writeText(text).catch(noop);
          }}
          return TRUE;
        }},
        GetUrlParametersSDK: () => getUrlParametersJson(),
        RequestBannersSDK: function (...args) {{
          bannerState = "shown";
          const callbackBag = args.find((arg) => arg && typeof arg === "object" && !Array.isArray(arg));
          const callbacks = args.filter((arg) => typeof arg === "function");
          if (callbackBag) {{
            safeCall(callbackBag.bannerRendered, []);
            safeCall(callbackBag.complete, []);
          }}
          safeRunCallbacks(callbacks, ["bannerRendered"]);
          return "bannerRendered";
        }},
        ShowAuthPromptSDK: function (...args) {{
          const callbacks = args.filter((arg) => typeof arg === "function");
          safeRunCallbacks(callbacks, [null, getOfflineUser()]);
          return JSON.stringify(getOfflineUser());
        }},
        ShowAccountLinkPromptSDK: function (...args) {{
          const callbacks = args.filter((arg) => typeof arg === "function");
          safeRunCallbacks(callbacks, [null, getOfflineUser()]);
          return JSON.stringify(getOfflineUser());
        }},
        GetUserSDK: function (...args) {{
          const callbacks = args.filter((arg) => typeof arg === "function");
          safeRunCallbacks(callbacks, [null, getOfflineUser()]);
          return JSON.stringify(getOfflineUser());
        }},
        GetUserTokenSDK: function (...args) {{
          const callbacks = args.filter((arg) => typeof arg === "function");
          safeRunCallbacks(callbacks, [null, EMPTY]);
          return EMPTY;
        }},
        GetXsollaUserTokenSDK: function (...args) {{
          const callbacks = args.filter((arg) => typeof arg === "function");
          safeRunCallbacks(callbacks, [null, EMPTY]);
          return EMPTY;
        }},
        AddUserScoreSDK: noop,
        SyncUnityGameDataSDK: noop,
        HasAdblock: () => false,
        GetSettings: () => getMadHookSettingsJson(),
        WrapGFFeature: (value) => value,
        IsQaTool: () => false,
        IsOnWhitelistedDomain: () => true,
        DebugLog: function (...args) {{
          console.log("[standalone-sdk]", ...args);
          return EMPTY;
        }},
      }};

      const dynamicFunctionNames = {fn_list_js};
      const dynamicWindowRootNames = {window_roots_js};
      const dynamicWindowCallableChains = {window_callable_chains_js};
      const fixedGlobalFunctionNames = [
        "InitSDK",
        "RequestAdSDK",
        "HappyTimeSDK",
        "GameplayStartSDK",
        "GameplayStopSDK",
        "RequestInviteUrlSDK",
        "ShowInviteButtonSDK",
        "HideInviteButtonSDK",
        "CopyToClipboardSDK",
        "GetUrlParametersSDK",
        "RequestBannersSDK",
        "ShowAuthPromptSDK",
        "ShowAccountLinkPromptSDK",
        "GetUserSDK",
        "GetUserTokenSDK",
        "GetXsollaUserTokenSDK",
        "AddUserScoreSDK",
        "SyncUnityGameDataSDK",
        "HasAdblock",
        "GetSettings",
        "WrapGFFeature",
        "IsQaTool",
        "IsOnWhitelistedDomain",
        "DebugLog",
      ];
      const fixedWindowRootNames = ["CrazySDK", "MadHook"];
      const fixedWindowCallableChains = fixedGlobalFunctionNames.map(function (name) {{
        return "CrazySDK." + name;
      }});
      const allDynamicFunctionNames = Array.from(
        new Set(dynamicFunctionNames.concat(fixedGlobalFunctionNames))
      );
      const allDynamicWindowRoots = Array.from(
        new Set(dynamicWindowRootNames.concat(fixedWindowRootNames))
      );
      const allDynamicWindowCallableChains = Array.from(
        new Set(dynamicWindowCallableChains.concat(fixedWindowCallableChains))
      );

      function safeCall(fn, args) {{
        if (typeof fn !== "function") {{
          return;
        }}
        try {{
          return fn.apply(null, args || []);
        }} catch (err) {{
          console.warn("integration callback failed:", err);
        }}
      }}

      function safeRunCallbacks(callbacks, args) {{
        if (!Array.isArray(callbacks)) {{
          return;
        }}
        for (const fn of callbacks) {{
          safeCall(fn, args);
        }}
      }}

      function inferStub(name) {{
        if (Object.prototype.hasOwnProperty.call(known, name)) {{
          return known[name];
        }}
        if (/Interstitial/i.test(name) && /^show/i.test(name)) {{
          return () => completeInterstitial();
        }}
        if (/Rewarded/i.test(name) && /^show/i.test(name)) {{
          return () => completeRewarded();
        }}
        if (/InterstitialState/i.test(name) && /^get/i.test(name)) {{
          return () => interstitialState;
        }}
        if (/RewardedState/i.test(name) && /^get/i.test(name)) {{
          return () => rewardedState;
        }}
        if (/BannerState/i.test(name) && /^get/i.test(name)) {{
          return () => bannerState;
        }}
        if (/^getIs[A-Z]/.test(name) || /^is[A-Z]/.test(name) || /^has[A-Z]/.test(name)) {{
          return () => FALSE;
        }}
        if (/^get[A-Z]/.test(name)) {{
          return () => EMPTY;
        }}
        return noop;
      }}

      function inferChainStub(chain) {{
        const name = String(chain || "").split(".").pop() || "";
        if (/requestAd/i.test(name)) {{
          return function (...args) {{
            interstitialState = AD_STATE_OPENED;
            rewardedState = AD_STATE_OPENED;
            const callbackBag = args.find((arg) => arg && typeof arg === "object" && !Array.isArray(arg));
            if (callbackBag) {{
              safeCall(callbackBag.adStarted, []);
              safeCall(callbackBag.adFinished, []);
              safeCall(callbackBag.adComplete, []);
              safeCall(callbackBag.complete, []);
            }}
            interstitialState = AD_STATE_CLOSED;
            rewardedState = AD_STATE_REWARDED;
            return AD_STATE_CLOSED;
          }};
        }}
        if (/requestBanner/i.test(name) || /requestOverlayBanners/i.test(name)) {{
          return function (...args) {{
            const callback = args.find((arg) => typeof arg === "function");
            safeCall(callback, [EMPTY, "bannerRendered", null]);
            return "bannerRendered";
          }};
        }}
        if (/hasAdblock/i.test(name)) {{
          return function (...args) {{
            const callbacks = args.filter((arg) => typeof arg === "function");
            safeRunCallbacks(callbacks, [null, false]);
            return false;
          }};
        }}
        if (/ensureLoaded|addInitCallback/i.test(name)) {{
          return function (...args) {{
            const callbacks = args.filter((arg) => typeof arg === "function");
            safeRunCallbacks(callbacks, [{{}}]);
          }};
        }}
        if (/addAuthListener/i.test(name)) {{
          return function (...args) {{
            const callbacks = args.filter((arg) => typeof arg === "function");
            safeRunCallbacks(callbacks, [{{}}]);
          }};
        }}
        if (/getUserToken|getXsollaUserToken/i.test(name)) {{
          return function (...args) {{
            const callbacks = args.filter((arg) => typeof arg === "function");
            safeRunCallbacks(callbacks, [null, EMPTY]);
            return EMPTY;
          }};
        }}
        if (/showAuthPrompt|showAccountLinkPrompt|getUser/i.test(name)) {{
          return function (...args) {{
            const callbacks = args.filter((arg) => typeof arg === "function");
            safeRunCallbacks(callbacks, [null, {{}}]);
            return EMPTY;
          }};
        }}
        if (/^get/i.test(name)) {{
          return function () {{
            return EMPTY;
          }};
        }}
        if (/^has|^is/i.test(name)) {{
          return function () {{
            return false;
          }};
        }}
        return noop;
      }}

      function ensurePath(path, leafAsFunction) {{
        if (!path) {{
          return;
        }}
        const parts = String(path).split(".").filter(Boolean);
        if (!parts.length) {{
          return;
        }}
        let scope = window;
        for (let idx = 0; idx < parts.length; idx += 1) {{
          const part = parts[idx];
          const isLeaf = idx === parts.length - 1;
          const existing = scope[part];
          if (isLeaf && leafAsFunction) {{
            if (typeof existing !== "function") {{
              scope[part] = inferChainStub(path);
            }}
            return;
          }}
          if (existing == null || (typeof existing !== "object" && typeof existing !== "function")) {{
            scope[part] = {{}};
          }}
          scope = scope[part];
        }}
      }}

      for (const name of allDynamicFunctionNames) {{
        if (typeof window[name] !== "function") {{
          window[name] = inferStub(name);
        }}
      }}

      for (const rootName of allDynamicWindowRoots) {{
        ensurePath(rootName, false);
      }}

      for (const chain of allDynamicWindowCallableChains) {{
        ensurePath(chain, true);
      }}

      if (
        typeof window.yandexMetricaCounterId !== "number" &&
        typeof window.yandexMetricaCounterId !== "string"
      ) {{
        window.yandexMetricaCounterId = 0;
      }}

      function patchUnitySdk() {{
        if (!window.UnitySDK || typeof window.UnitySDK !== "object") {{
          window.UnitySDK = {{}};
        }}
        const sdk = window.UnitySDK;
        if (!Array.isArray(sdk.waitingForLoad)) {{
          sdk.waitingForLoad = [];
        }}
        if (typeof sdk.objectName !== "string" || !sdk.objectName) {{
          sdk.objectName = "UnitySDK";
        }}
        if (typeof sdk.userObjectName !== "string" || !sdk.userObjectName) {{
          sdk.userObjectName = "UnitySDK.User";
        }}
        if (typeof sdk.unlockPointer !== "function") {{
          sdk.unlockPointer = noop;
        }}
        if (typeof sdk.lockPointer !== "function") {{
          sdk.lockPointer = noop;
        }}
        if (typeof sdk.ensureLoaded !== "function") {{
          sdk.ensureLoaded = function (callback) {{
            safeCall(callback, []);
          }};
        }}
        if (typeof sdk.onSdkScriptLoaded !== "function") {{
          sdk.onSdkScriptLoaded = function () {{
            sdk.isSdkLoaded = true;
            const queued = Array.isArray(sdk.waitingForLoad) ? sdk.waitingForLoad.splice(0) : [];
            safeRunCallbacks(queued, []);
          }};
        }}
        if (sdk.isSdkLoaded !== true) {{
          sdk.isSdkLoaded = true;
        }}
        if (sdk.waitingForLoad.length > 0) {{
          const queued = sdk.waitingForLoad.splice(0);
          safeRunCallbacks(queued, []);
        }}
      }}

      function patchCrazySdk() {{
        if (!window.CrazySDK || typeof window.CrazySDK !== "object") {{
          window.CrazySDK = {{}};
        }}
        const sdk = window.CrazySDK;
        const settings = buildMadHookSettings();
        sdk.settings = settings;
        if (!window.crazySdkInitOptions || typeof window.crazySdkInitOptions !== "object") {{
          window.crazySdkInitOptions = {{}};
        }}
        Object.assign(window.crazySdkInitOptions, settings);
        const methodMap = {{
          InitSDK: window.InitSDK,
          RequestAdSDK: window.RequestAdSDK,
          HappyTimeSDK: window.HappyTimeSDK,
          GameplayStartSDK: window.GameplayStartSDK,
          GameplayStopSDK: window.GameplayStopSDK,
          RequestInviteUrlSDK: window.RequestInviteUrlSDK,
          ShowInviteButtonSDK: window.ShowInviteButtonSDK,
          HideInviteButtonSDK: window.HideInviteButtonSDK,
          CopyToClipboardSDK: window.CopyToClipboardSDK,
          GetUrlParametersSDK: window.GetUrlParametersSDK,
          RequestBannersSDK: window.RequestBannersSDK,
          ShowAuthPromptSDK: window.ShowAuthPromptSDK,
          ShowAccountLinkPromptSDK: window.ShowAccountLinkPromptSDK,
          GetUserSDK: window.GetUserSDK,
          GetUserTokenSDK: window.GetUserTokenSDK,
          GetXsollaUserTokenSDK: window.GetXsollaUserTokenSDK,
          AddUserScoreSDK: window.AddUserScoreSDK,
          SyncUnityGameDataSDK: window.SyncUnityGameDataSDK,
          HasAdblock: window.HasAdblock,
          GetSettings: window.GetSettings,
          WrapGFFeature: window.WrapGFFeature,
          IsQaTool: window.IsQaTool,
          IsOnWhitelistedDomain: window.IsOnWhitelistedDomain,
          DebugLog: window.DebugLog,
        }};
        Object.entries(methodMap).forEach(function (entry) {{
          const name = entry[0];
          const fn = entry[1];
          if (typeof fn === "function") {{
            sdk[name] = fn;
          }}
        }});
        if (typeof sdk.init !== "function") {{
          sdk.init = window.InitSDK;
        }}
        if (typeof sdk.requestAd !== "function") {{
          sdk.requestAd = window.RequestAdSDK;
        }}
        if (typeof sdk.getSettings !== "function") {{
          sdk.getSettings = function () {{
            return buildMadHookSettings();
          }};
        }}
        if (typeof sdk.hasAdblock !== "function") {{
          sdk.hasAdblock = window.HasAdblock;
        }}
        if (typeof sdk.isOnWhitelistedDomain !== "function") {{
          sdk.isOnWhitelistedDomain = window.IsOnWhitelistedDomain;
        }}
      }}

      patchUnitySdk();
      patchCrazySdk();
      const sdkPatchInterval = setInterval(function () {{
        patchUnitySdk();
        patchCrazySdk();
      }}, 500);
      setTimeout(function () {{
        clearInterval(sdkPatchInterval);
      }}, 15000);
    }})();
  </script>

  <script>
    (function () {{
      const loadingScreen = document.getElementById("loadingScreen");
      const starCanvas = document.getElementById("star-canvas");
      const waveCanvas = document.getElementById("wave-canvas");
      if (!loadingScreen || !starCanvas || !waveCanvas) {{
        return;
      }}

      const starCtx = starCanvas.getContext("2d", {{ alpha: true }});
      const waveCtx = waveCanvas.getContext("2d", {{ alpha: true }});
      if (!starCtx || !waveCtx) {{
        return;
      }}

      let stars = [];
      let dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
      let waveTime = 0;
      let shootingStar = null;
      const isEmbeddedFrame = (function () {{
        try {{
          return Boolean(window.top && window.top !== window);
        }} catch (err) {{
          return true;
        }}
      }})();
      const reduceMotion =
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const lightweightBackdrop = reduceMotion || isEmbeddedFrame;
      const mouse = {{ x: 0.5, y: 0.5, tx: 0.5, ty: 0.5 }};

      function isVisible() {{
        return loadingScreen.style.display !== "none";
      }}

      function isBackdropActive() {{
        return isVisible() && !loadingScreen.classList.contains("is-loading");
      }}

      function resizeCanvas(canvas) {{
        const nextDpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
        canvas.width = Math.floor(window.innerWidth * nextDpr);
        canvas.height = Math.floor(window.innerHeight * nextDpr);
        canvas.style.width = window.innerWidth + "px";
        canvas.style.height = window.innerHeight + "px";
        return nextDpr;
      }}

      class Star {{
        constructor(depth) {{
          this.depth = depth;
          this.reset();
        }}

        reset() {{
          const width = window.innerWidth;
          const height = window.innerHeight;
          this.x = Math.random() * width;
          this.y = Math.random() * height;
          const base = 1 - this.depth;
          this.size = (base * 1.5 + 0.55) * (Math.random() * 0.9 + 0.6);
          const drift = base * 0.18 + 0.04;
          this.vx = (Math.random() - 0.5) * drift;
          this.vy = (Math.random() - 0.5) * drift;
          this.opacity = Math.random() * 0.4 + 0.32;
          this.twinkle = Math.random() * 0.01 + 0.005;
          this.direction = Math.random() < 0.5 ? -1 : 1;
        }}

        update() {{
          const width = window.innerWidth;
          const height = window.innerHeight;
          this.x += this.vx;
          this.y += this.vy;
          this.opacity += this.twinkle * this.direction;
          if (this.opacity > 1) {{
            this.opacity = 1;
            this.direction *= -1;
          }}
          if (this.opacity < 0.22) {{
            this.opacity = 0.22;
            this.direction *= -1;
          }}
          if (this.x < -20) this.x = width + 20;
          if (this.x > width + 20) this.x = -20;
          if (this.y < -20) this.y = height + 20;
          if (this.y > height + 20) this.y = -20;
        }}

        draw() {{
          starCtx.shadowBlur = 8 * (this.size / 2);
          starCtx.shadowColor = "rgba(255,255,255,.75)";
          starCtx.fillStyle = "rgba(255,255,255," + this.opacity + ")";
          starCtx.beginPath();
          starCtx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
          starCtx.fill();
          starCtx.shadowBlur = 0;
        }}
      }}

      class ShootingStar {{
        constructor() {{
          const width = window.innerWidth;
          const height = window.innerHeight;
          const startEdge = Math.random();
          this.x = startEdge < 0.5 ? Math.random() * width * 0.6 : -60;
          this.y = startEdge < 0.5 ? -60 : Math.random() * height * 0.4;
          const angle = (Math.random() * 0.25 + 0.35) * Math.PI;
          const speed = Math.random() * 10 + 18;
          this.vx = Math.cos(angle) * speed;
          this.vy = Math.sin(angle) * speed;
          this.life = 0;
          this.maxLife = Math.random() * 18 + 30;
          this.length = Math.random() * 160 + 220;
          this.width = Math.random() * 1.2 + 1.2;
        }}

        update() {{
          this.x += this.vx;
          this.y += this.vy;
          this.life += 1;
          return this.life < this.maxLife;
        }}

        draw(context) {{
          const progress = this.life / this.maxLife;
          const alpha = Math.sin(Math.PI * progress) * 0.75;
          const tailX = this.x - this.vx * 3;
          const tailY = this.y - this.vy * 3;
          const norm = Math.hypot(this.vx, this.vy) || 1;
          const lineX = tailX - (this.vx / norm) * this.length;
          const lineY = tailY - (this.vy / norm) * this.length;
          const gradient = context.createLinearGradient(tailX, tailY, lineX, lineY);
          gradient.addColorStop(0, "rgba(255,255,255," + alpha + ")");
          gradient.addColorStop(0.4, "rgba(34,211,238," + alpha * 0.45 + ")");
          gradient.addColorStop(1, "rgba(59,130,246,0)");
          context.save();
          context.globalCompositeOperation = "lighter";
          context.strokeStyle = gradient;
          context.lineWidth = this.width;
          context.lineCap = "round";
          context.shadowBlur = 14;
          context.shadowColor = "rgba(34,211,238," + alpha * 0.55 + ")";
          context.beginPath();
          context.moveTo(tailX, tailY);
          context.lineTo(lineX, lineY);
          context.stroke();
          context.restore();
        }}
      }}

      function seedStars() {{
        stars = [];
        const count = Math.round(
          Math.min(
            lightweightBackdrop ? 72 : 160,
            Math.max(lightweightBackdrop ? 42 : 90, (window.innerWidth * window.innerHeight) / 14000)
          )
        );
        for (let index = 0; index < count; index += 1) {{
          stars.push(new Star(Math.random()));
        }}
      }}

      function resizeAll() {{
        dpr = resizeCanvas(starCanvas);
        starCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
        resizeCanvas(waveCanvas);
        waveCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
        seedStars();
      }}

      function scheduleShootingStar() {{
        if (lightweightBackdrop || !isBackdropActive()) {{
          return;
        }}
        window.setTimeout(function () {{
          if (!shootingStar && isBackdropActive()) {{
            shootingStar = new ShootingStar();
          }}
          scheduleShootingStar();
        }}, Math.random() * 2500 + 3500);
      }}

      function animateStars() {{
        if (!isBackdropActive()) {{
          return;
        }}
        starCtx.clearRect(0, 0, window.innerWidth, window.innerHeight);
        for (const star of stars) {{
          star.update();
          star.draw();
        }}
        if (shootingStar) {{
          if (!shootingStar.update()) {{
            shootingStar = null;
          }} else {{
            shootingStar.draw(starCtx);
          }}
        }}
        window.requestAnimationFrame(animateStars);
      }}

      function smoothMouse() {{
        if (lightweightBackdrop || !isBackdropActive()) {{
          return;
        }}
        mouse.x += (mouse.tx - mouse.x) * 0.06;
        mouse.y += (mouse.ty - mouse.y) * 0.06;
        window.requestAnimationFrame(smoothMouse);
      }}

      function drawWaves() {{
        if (!isBackdropActive()) {{
          return;
        }}
        const width = window.innerWidth;
        const height = window.innerHeight;
        waveCtx.clearRect(0, 0, width, height);
        if (lightweightBackdrop) {{
          const horizon = height * 0.62;
          const gradient = waveCtx.createLinearGradient(0, horizon - 120, 0, height);
          gradient.addColorStop(0, "rgba(34,211,238,0.035)");
          gradient.addColorStop(1, "rgba(59,130,246,0.055)");
          waveCtx.fillStyle = gradient;
          waveCtx.fillRect(0, horizon, width, height - horizon);
          return;
        }}
        const ampBoost = 1 + (0.9 - mouse.y) * 0.65;
        const phaseShift = (mouse.x - 0.5) * 1.2;
        const horizon = height * (0.56 + (mouse.y - 0.5) * 0.08);
        const gradient = waveCtx.createLinearGradient(0, horizon - 200, 0, height);
        gradient.addColorStop(0, "rgba(34,211,238,0.05)");
        gradient.addColorStop(0.4, "rgba(59,130,246,0.10)");
        gradient.addColorStop(1, "rgba(59,130,246,0.08)");
        const bands = 8;

        for (let band = 0; band < bands; band += 1) {{
          const bandTime = waveTime * (0.75 + band * 0.045);
          const baseY = horizon + band * (height * 0.055);
          const amplitude = (12 + band * 7) * ampBoost;
          const frequency = 0.01 + band * 0.0012;
          const speed = 0.75 + band * 0.12;
          const wobble = 0.35 + band * 0.04;

          waveCtx.beginPath();
          waveCtx.moveTo(0, baseY);
          for (let x = 0; x <= width; x += 10) {{
            const nextX = x * frequency;
            const y =
              baseY +
              Math.sin(nextX + bandTime * 0.015 * speed + phaseShift) * amplitude +
              Math.sin(nextX * 1.8 - bandTime * 0.01 * speed) * (amplitude * wobble * 0.25);
            waveCtx.lineTo(x, y);
          }}
          waveCtx.lineTo(width, height);
          waveCtx.lineTo(0, height);
          waveCtx.closePath();
          waveCtx.fillStyle = gradient;
          waveCtx.fill();
          waveCtx.globalCompositeOperation = "lighter";
          waveCtx.strokeStyle = "rgba(34,211,238," + (0.05 + band * 0.008) + ")";
          waveCtx.lineWidth = 1;
          waveCtx.stroke();
          waveCtx.globalCompositeOperation = "source-over";
        }}

        waveTime += 0.95;
        window.requestAnimationFrame(drawWaves);
      }}

      window.addEventListener("mousemove", function (event) {{
        mouse.tx = event.clientX / Math.max(window.innerWidth, 1);
        mouse.ty = event.clientY / Math.max(window.innerHeight, 1);
      }}, {{ passive: true }});
      window.addEventListener("resize", resizeAll);

      resizeAll();
      if (lightweightBackdrop) {{
        mouse.tx = 0.5;
        mouse.ty = 0.5;
      }} else {{
        scheduleShootingStar();
        smoothMouse();
      }}
      animateStars();
      drawWaves();
    }})();
  </script>

  <script>
    (function () {{
      const PRODUCT_NAME = {product_name_js};
      const BUILD_KIND = {build_kind_js};
      const BUILD_DIR = "Build";
      const LOADER_FILE = {loader_name_js};
      const DATA_FILE = {data_name_js};
      const FRAMEWORK_FILE = {framework_name_js};
      const WASM_FILE = {wasm_name_js};
      const LEGACY_CONFIG = {legacy_config_js};
      const SOURCE_PAGE_URL =
        window.__unityStandaloneSourcePageUrl || {source_page_url_js};
      const ENABLE_SOURCE_URL_SPOOF = {enable_source_url_spoof_js};
      const ORIGINAL_FOLDER_URL = {original_folder_url_js};
      const STREAMING_ASSETS_URL = {streaming_assets_url_js};
      const BUILD_CACHE_BUSTER = {asset_cache_buster_js};
      const ENTRY_PAGE_CONFIG = {page_config_js};
      const AUXILIARY_ASSET_REWRITES = {auxiliary_asset_rewrites_js};
      const LOCAL_PAGE_URL =
        window.__unityStandaloneLocalPageUrl || window.location.href;
      const LOCAL_HOST_NAME = (function () {{
        try {{
          return new URL(LOCAL_PAGE_URL).hostname || "";
        }} catch (err) {{
          return window.location && window.location.hostname
            ? String(window.location.hostname)
            : "";
        }}
      }})();
      const LOCAL_BUILD_ROOT_URL = new URL(BUILD_DIR + "/", LOCAL_PAGE_URL).toString();
      const ROOT = document.documentElement;

      const canvas = document.getElementById("unity-canvas");
      const legacyContainer = document.getElementById("unity-legacy-container");
      const loadingScreen = document.getElementById("loadingScreen");
      const progressFill = document.getElementById("progressFill");
      const progressTrack = document.getElementById("progressTrack");
      const launchPanel = document.getElementById("launchPanel");
      const launchFullscreenBtn = document.getElementById("launchFullscreenBtn");
      const launchFrameBtn = document.getElementById("launchFrameBtn");
      const playNote = document.getElementById("playNote");
      const playNoteText = playNote ? playNote.textContent.trim() : "";
      const status = document.getElementById("status");
      const stepLog = document.getElementById("stepLog");
      const decisionDialog = (function () {{
        if (!loadingScreen) {{
          return null;
        }}
        const overlay = document.createElement("div");
        overlay.hidden = true;
        overlay.setAttribute("aria-hidden", "true");
        overlay.style.cssText =
          "position:absolute;inset:0;z-index:12;display:flex;align-items:center;justify-content:center;" +
          "padding:24px;background:rgba(2,6,23,0.58);backdrop-filter:blur(10px);";
        overlay.innerHTML =
          '<div role="dialog" aria-modal="true" aria-labelledby="oceanDecisionTitle" ' +
          'style="width:min(92vw,540px);border-radius:32px;padding:28px 28px 24px;' +
          'border:1px solid rgba(120,196,255,0.30);background:linear-gradient(180deg,rgba(2,6,23,0.96),rgba(7,18,41,0.98));' +
          'box-shadow:0 24px 80px rgba(0,0,0,0.42),0 0 0 1px rgba(59,130,246,0.12) inset;">' +
          '<h2 id="oceanDecisionTitle" style="margin:0 0 14px;color:#effcff;font:800 clamp(1.6rem,4vw,2.2rem)/1.05 \\"Segoe UI Variable Text\\",\\"Segoe UI\\",sans-serif;letter-spacing:-0.03em;"></h2>' +
          '<div id="oceanDecisionBody" style="margin:0 0 18px;color:rgba(225,245,255,0.76);font:700 1rem/1.45 \\"Segoe UI Variable Text\\",\\"Segoe UI\\",sans-serif;"></div>' +
          '<div id="oceanDecisionActions" style="display:flex;flex-direction:column;gap:12px;"></div>' +
          '</div>';
        loadingScreen.appendChild(overlay);
        return {{
          overlay: overlay,
          title: overlay.querySelector("#oceanDecisionTitle"),
          body: overlay.querySelector("#oceanDecisionBody"),
          actions: overlay.querySelector("#oceanDecisionActions"),
        }};
      }})();
      const ALLOWED_LAUNCH_MODES = {allowed_launch_modes_js};
      const RECOMMENDED_LAUNCH_MODE = {recommended_launch_mode_js};
      const launchFrameLabel = "LAUNCH HERE";
      const launchFullscreenLabel = "LAUNCH FULLSCREEN";

      let started = false;
      let loadingScreenDismissed = false;
      let launchPanelHideTimer = 0;
      let legacyConfigUrl = "";
      let loaderScriptPromise = null;
      let buildWarmupStarted = false;
      let sourceUrlSpoofApplied = false;
      const resourceHints = new Set();
      const stepLogEntries = [];
      const loaderStepEpoch = Date.now();
      let lastLoggedStep = "";
      let lastProgressBucket = -1;
      let activeDecisionResolve = null;
      let activeDecisionCancelValue = "";
      if (ORIGINAL_FOLDER_URL) {{
        window.originalFolder = ORIGINAL_FOLDER_URL;
      }}
      window.__unityStandaloneLocalHostName = LOCAL_HOST_NAME;
      globalThis.__unityStandaloneLocalHostName = LOCAL_HOST_NAME;
      const requestedLaunchMode = (function () {{
        try {{
          return new URL(LOCAL_PAGE_URL).searchParams.get("launchMode") || "";
        }} catch (err) {{
          return "";
        }}
      }})();
      function normalizeAllowedLaunchModes(value) {{
        const normalized = String(value || "").trim().toLowerCase();
        if (normalized === "frame" || normalized === "fullscreen" || normalized === "both") {{
          return normalized;
        }}
        return "both";
      }}
      function normalizeRecommendedLaunchMode(value, allowedModes) {{
        if (allowedModes === "frame" || allowedModes === "fullscreen") {{
          return allowedModes;
        }}
        const normalized = String(value || "").trim().toLowerCase();
        if (normalized === "frame" || normalized === "fullscreen" || normalized === "none") {{
          return normalized;
        }}
        return "none";
      }}
      const allowedLaunchModes = normalizeAllowedLaunchModes(ALLOWED_LAUNCH_MODES);
      const recommendedLaunchMode = normalizeRecommendedLaunchMode(
        RECOMMENDED_LAUNCH_MODE,
        allowedLaunchModes
      );
      const frameLaunchAllowed = allowedLaunchModes !== "fullscreen";
      const fullscreenLaunchAllowed = allowedLaunchModes !== "frame";
      const initialStatusText =
        allowedLaunchModes === "frame"
          ? "Frame launch selected by builder"
          : allowedLaunchModes === "fullscreen"
            ? "Fullscreen launch selected by builder"
            : "Awaiting launch-mode selection";
      const isEmbeddedFrame = (function () {{
        try {{
          return Boolean(window.top && window.top !== window);
        }} catch (err) {{
          return true;
        }}
      }})();
      const constrainedPerformanceMode = Boolean(
        isEmbeddedFrame ||
          (Number(navigator.deviceMemory) > 0 && Number(navigator.deviceMemory) <= 4) ||
          (Number(navigator.hardwareConcurrency) > 0 && Number(navigator.hardwareConcurrency) <= 4)
      );
      const forceFullscreenScrollLock = requestedLaunchMode === "fullscreen";
      const FULLSCREEN_SCROLL_LOCK_ATTR = "data-ocean-fullscreen-lock";
      const fullscreenScrollKeys = new Set([
        " ",
        "Spacebar",
        "ArrowUp",
        "ArrowDown",
        "PageUp",
        "PageDown",
        "Home",
        "End",
      ]);
      const fullscreenScrollCodes = new Set([
        "Space",
        "ArrowUp",
        "ArrowDown",
        "PageUp",
        "PageDown",
        "Home",
        "End",
      ]);

      function safeUrlHost(value) {{
        if (!value) {{
          return "";
        }}
        try {{
          return new URL(value, LOCAL_PAGE_URL).host || "";
        }} catch (err) {{
          return "";
        }}
      }}

      function safeFileName(value) {{
        const clean = String(value || "").split("?")[0].split("#")[0];
        const parts = clean.split("/");
        return parts[parts.length - 1] || clean;
      }}

      function buildLaunchModeLabel() {{
        return requestedLaunchMode || (isEmbeddedFrame ? "embed" : "page");
      }}

      function labelForLaunchMode(mode) {{
        return mode === "fullscreen" ? launchFullscreenLabel : launchFrameLabel;
      }}

      function isLaunchRecommendationActive() {{
        return allowedLaunchModes === "both" && recommendedLaunchMode !== "none";
      }}

      function closeDecisionDialog(value) {{
        if (!activeDecisionResolve || !decisionDialog) {{
          return;
        }}
        const resolve = activeDecisionResolve;
        activeDecisionResolve = null;
        activeDecisionCancelValue = "";
        decisionDialog.overlay.hidden = true;
        decisionDialog.overlay.setAttribute("aria-hidden", "true");
        decisionDialog.actions.textContent = "";
        resolve(String(value || ""));
      }}

      function showDecisionDialog(options) {{
        if (!decisionDialog) {{
          return Promise.resolve(
            typeof options.defaultValue === "string" ? options.defaultValue : ""
          );
        }}
        if (activeDecisionResolve) {{
          closeDecisionDialog(activeDecisionCancelValue);
        }}
        const titleText =
          typeof options.title === "string" && options.title.trim()
            ? options.title.trim()
            : "Choose option";
        const bodyText = typeof options.body === "string" ? options.body.trim() : "";
        const buttons = Array.isArray(options.buttons) && options.buttons.length
          ? options.buttons
          : [{{ label: "Continue", value: "continue", primary: true }}];
        let firstButton = null;
        activeDecisionCancelValue =
          typeof options.cancelValue === "string" ? options.cancelValue : "";
        decisionDialog.title.textContent = titleText;
        decisionDialog.body.textContent = bodyText;
        decisionDialog.actions.textContent = "";
        buttons.forEach(function (buttonConfig, index) {{
          const button = document.createElement("button");
          button.type = "button";
          button.textContent =
            buttonConfig && typeof buttonConfig.label === "string" && buttonConfig.label.trim()
              ? buttonConfig.label.trim()
              : "Continue";
          button.style.cssText =
            "width:100%;padding:15px 22px;border-radius:999px;cursor:pointer;text-align:center;" +
            "font:800 14px/1.1 \\"Segoe UI Variable Text\\",\\"Segoe UI\\",\\"Trebuchet MS\\",system-ui,sans-serif;" +
            "letter-spacing:0.08em;text-transform:uppercase;transition:transform 180ms ease,box-shadow 180ms ease,background 180ms ease,border-color 180ms ease;" +
            (buttonConfig && buttonConfig.primary
              ? "border:1px solid rgba(88,200,255,0.62);background:linear-gradient(180deg,rgba(16,28,58,0.94),rgba(10,22,46,0.98));color:#effcff;box-shadow:0 18px 36px rgba(0,0,0,0.34),0 0 24px rgba(59,130,246,0.22);"
              : "border:1px solid rgba(120,196,255,0.30);background:linear-gradient(180deg,rgba(10,16,31,0.86),rgba(10,18,38,0.96));color:#effcff;box-shadow:0 14px 34px rgba(0,0,0,0.32),inset 0 1px 0 rgba(255,255,255,0.08);");
          button.addEventListener("click", function () {{
            closeDecisionDialog(buttonConfig ? buttonConfig.value : "");
          }});
          decisionDialog.actions.appendChild(button);
          if (!firstButton || index === 0 || (buttonConfig && buttonConfig.primary)) {{
            firstButton = button;
          }}
        }});
        decisionDialog.overlay.hidden = false;
        decisionDialog.overlay.setAttribute("aria-hidden", "false");
        window.setTimeout(function () {{
          if (firstButton && typeof firstButton.focus === "function") {{
            firstButton.focus();
          }}
        }}, 0);
        return new Promise(function (resolve) {{
          activeDecisionResolve = resolve;
        }});
      }}

      function showMessageDialog(title, body) {{
        return showDecisionDialog({{
          title: title,
          body: body,
          buttons: [{{ label: "OK", value: "ok", primary: true }}],
          cancelValue: "ok",
          defaultValue: "ok",
        }});
      }}

      function updateLaunchModeUi() {{
        launchFrameBtn.textContent = launchFrameLabel;
        launchFullscreenBtn.textContent = launchFullscreenLabel;
        launchFrameBtn.style.display = frameLaunchAllowed ? "" : "none";
        launchFullscreenBtn.style.display = fullscreenLaunchAllowed ? "" : "none";
        launchFrameBtn.disabled = !frameLaunchAllowed;
        launchFullscreenBtn.disabled = !fullscreenLaunchAllowed;
        if (!playNote) {{
          return;
        }}
        const noteParts = [];
        if (playNoteText) {{
          noteParts.push(playNoteText);
        }}
        if (noteParts.length) {{
          playNote.textContent = noteParts.join("  ");
          playNote.style.display = "";
        }} else {{
          playNote.style.display = "none";
        }}
      }}

      function confirmRecommendedLaunchOverride(mode) {{
        if (!isLaunchRecommendationActive() || recommendedLaunchMode === mode) {{
          return Promise.resolve(mode);
        }}
        const selectedLabel = labelForLaunchMode(mode);
        return showDecisionDialog({{
          title:
            (recommendedLaunchMode === "fullscreen" ? "Fullscreen" : "Launch here") +
            " is recommended for this game!",
          body: "Are you sure you want to " + selectedLabel.toLowerCase() + "?",
          buttons: [
            {{ label: "Confirm", value: mode, primary: true }},
            {{ label: "Go Back", value: "" }},
          ],
          cancelValue: "",
          defaultValue: mode,
        }});
      }}

      function unityProgressPhase(percent) {{
        if (percent <= 0) {{
          return "init";
        }}
        if (percent < 20) {{
          return "loader-fetch";
        }}
        if (percent < 45) {{
          return "loader-eval";
        }}
        if (percent < 70) {{
          return "data-transfer";
        }}
        if (percent < 90) {{
          return "wasm-compile";
        }}
        if (percent < 100) {{
          return "runtime-warmup";
        }}
        return "first-frame";
      }}

      function formatTechnicalStatusText(message) {{
        const cleanMessage = String(message || "").replace(/\\s+/g, " ").trim();
        if (!cleanMessage) {{
          return "";
        }}
        if (
          cleanMessage === "Choose how you want to launch" ||
          cleanMessage === "Awaiting launch-mode selection"
        ) {{
          return "Awaiting launch-mode selection";
        }}
        const progressMatch = /^Loading (\\d+)%$/.exec(cleanMessage);
        if (progressMatch) {{
          const percent = Number(progressMatch[1]);
          return "Unity bootstrap progress=" + percent + "% phase=" + unityProgressPhase(percent);
        }}
        switch (cleanMessage) {{
          case "Use HTTP or HTTPS to run this build":
            return "Blocked: protocol=file; serve over http(s)";
          case "New tab blocked. Allow popups or use launch here.":
            return "Popup blocked; fullscreen handoff aborted";
          case "Opened fullscreen in a new tab":
            return "Fullscreen handoff opened in new tab";
          case "Loader error: createUnityInstance is missing":
            return "Fatal: createUnityInstance missing after loader eval";
          case "Loader error: UnityLoader.instantiate is missing":
            return "Fatal: UnityLoader.instantiate missing after loader eval";
          case "Failed to load Unity loader script":
            return "Fatal: loader script fetch/eval failed";
          case "Legacy Unity container is missing":
            return "Fatal: legacy container missing";
          case "Failed to prepare legacy Unity config":
            return "Fatal: legacy config assembly failed";
          case "Failed to load game":
            return "Fatal: runtime bootstrap failed";
          case "Ready":
            return "Runtime ready; canvas attached";
          default:
            return cleanMessage;
        }}
      }}

      function formatTechnicalStepMessage(message) {{
        const cleanMessage = String(message || "").replace(/\\s+/g, " ").trim();
        if (!cleanMessage) {{
          return "";
        }}
        const progressMatch = /^Loading (\\d+)%$/.exec(cleanMessage);
        if (progressMatch) {{
          const percent = Number(progressMatch[1]);
          return "[unity.progress] value=" + percent + "% phase=" + unityProgressPhase(percent) + " kind=" + BUILD_KIND;
        }}
        switch (cleanMessage) {{
          case "Awaiting launch-mode selection":
          case "Choose how you want to launch":
            return "[shell.idle] awaiting launch-mode selection";
          case "Shell initialized":
            return "[shell.init] launcher-ready kind=" + BUILD_KIND + " mode=" + buildLaunchModeLabel() + " embed=" + (isEmbeddedFrame ? "1" : "0") + " proto=" + window.location.protocol.replace(":", "") + " loader=" + safeFileName(LOADER_FILE);
          case "Launch requested":
            return "[launch] user-activation accepted mode=" + buildLaunchModeLabel();
          case "Storage access API unavailable":
            return "[storage] API unavailable; continuing";
          case "Checking storage access":
            return "[storage] hasStorageAccess() probe";
          case "Storage access already granted":
            return "[storage] access already granted";
          case "Requesting storage access":
            return "[storage] requestStorageAccess()";
          case "Storage access request failed":
            return "[storage] requestStorageAccess() failed; continuing";
          case "Storage access check failed":
            return "[storage] hasStorageAccess() failed; continuing";
          case "Preparing game config":
            return "[config] unityConfig loader=" + safeFileName(LOADER_FILE) + " data=" + safeFileName(DATA_FILE) + " wasm=" + safeFileName(WASM_FILE);
          case "Preparing legacy Unity config":
            return "[config.legacy] building JSON config payload";
          case "Loading Unity loader script":
            return "[net.loader] GET " + buildBuildAssetUrl(LOADER_FILE, true);
          case "Unity loader script loaded":
            return "[net.loader] ready file=" + safeFileName(LOADER_FILE);
          case "Unity loader script failed":
            return "[net.loader] failed file=" + safeFileName(LOADER_FILE);
          case "Unity loader script already ready":
            return "[net.loader] reuse existing loader instance";
          case "Legacy Unity loader already ready":
            return "[net.loader.legacy] reuse existing loader instance";
          case "Source URL spoof enabled":
            return "[compat] source-url-spoof enabled host=" + safeUrlHost(SOURCE_PAGE_URL);
          case "Starting Unity runtime":
            return "[unity.bootstrap] begin dpr=" + computeUnityDevicePixelRatio().toFixed(2) + " sourceHost=" + safeUrlHost(SOURCE_PAGE_URL || LOCAL_PAGE_URL);
          case "Creating Unity instance":
            return "[unity.bootstrap] createUnityInstance()";
          case "Unity instance ready":
            return "[unity.runtime] ready product=" + PRODUCT_NAME + " host=" + safeUrlHost(LOCAL_PAGE_URL);
          case "Creating legacy Unity instance":
            return "[unity.bootstrap.legacy] UnityLoader.instantiate()";
          case "Legacy Unity instance ready":
            return "[unity.runtime.legacy] ready product=" + PRODUCT_NAME + " host=" + safeUrlHost(LOCAL_PAGE_URL);
          default:
            return cleanMessage;
        }}
      }}

      if (BUILD_KIND === "legacy_json") {{
        if (canvas) {{
          canvas.style.display = "none";
        }}
        if (legacyContainer) {{
          legacyContainer.style.display = "block";
        }}
      }} else if (legacyContainer) {{
        legacyContainer.style.display = "none";
      }}

      function logLoaderStep(message) {{
        if (!stepLog || typeof message !== "string") {{
          return;
        }}
        const cleanMessage = message.replace(/\\s+/g, " ").trim();
        if (!cleanMessage) {{
          return;
        }}
        const progressMatch = /^Loading (\\d+)%$/.exec(cleanMessage);
        const formattedMessage = formatTechnicalStepMessage(cleanMessage);
        if (!formattedMessage) {{
          return;
        }}
        if (progressMatch) {{
          const percent = Number(progressMatch[1]);
          const bucket =
            percent >= 100 ? 100 : Math.max(0, Math.floor(percent / 10) * 10);
          if (bucket === lastProgressBucket && percent !== 0 && percent !== 100) {{
            return;
          }}
          lastProgressBucket = bucket;
        }} else if (formattedMessage === lastLoggedStep) {{
          return;
        }} else {{
          lastLoggedStep = formattedMessage;
        }}
        const elapsedSeconds = ((Date.now() - loaderStepEpoch) / 1000).toFixed(1);
        stepLogEntries.push(elapsedSeconds + "s  " + formattedMessage);
        while (stepLogEntries.length > 8) {{
          stepLogEntries.shift();
        }}
        stepLog.textContent = stepLogEntries.join("\\n");
      }}

      function setStatus(text) {{
        if (status) {{
          status.textContent = formatTechnicalStatusText(text);
        }}
        logLoaderStep(text);
      }}

      function setLoadState(value) {{
        if (!ROOT) {{
          return;
        }}
        ROOT.setAttribute("data-ocean-unity-state", value);
      }}

      function setProgress(progress) {{
        const numeric = Number(progress);
        const safeProgress = Number.isFinite(numeric) ? Math.min(1, Math.max(0, numeric)) : 0;
        const percent = Math.round(safeProgress * 100);
        if (progressFill) {{
          progressFill.style.width = percent + "%";
        }}
        if (loadingScreen) {{
          loadingScreen.setAttribute("data-progress", String(percent));
        }}
        return percent;
      }}

      function setProgressVisibility(isVisible) {{
        if (!progressTrack) {{
          return;
        }}
        progressTrack.classList.toggle("is-visible", Boolean(isVisible));
      }}

      function releaseLegacyConfigUrl() {{
        if (!legacyConfigUrl || typeof URL.revokeObjectURL !== "function") {{
          return;
        }}
        URL.revokeObjectURL(legacyConfigUrl);
        legacyConfigUrl = "";
      }}

      function dismissLoadingScreen() {{
        if (loadingScreenDismissed || !loadingScreen) {{
          return;
        }}
        setLoadState("ready");
        loadingScreenDismissed = true;
        loadingScreen.classList.add("is-exiting");
        window.setTimeout(function () {{
          loadingScreen.style.display = "none";
        }}, 880);
      }}

      function clearLaunchPanelHideTimer() {{
        if (!launchPanelHideTimer) {{
          return;
        }}
        window.clearTimeout(launchPanelHideTimer);
        launchPanelHideTimer = 0;
      }}

      function isFullscreenActive() {{
        return Boolean(
          document.fullscreenElement ||
            document.webkitFullscreenElement ||
            document.msFullscreenElement ||
            document.mozFullScreenElement
        );
      }}

      function shouldLockFullscreenScroll() {{
        return forceFullscreenScrollLock || isFullscreenActive();
      }}

      function setFullscreenScrollLock(isLocked) {{
        const root = document.documentElement;
        const body = document.body;
        if (root) {{
          if (isLocked) {{
            root.setAttribute(FULLSCREEN_SCROLL_LOCK_ATTR, "1");
          }} else {{
            root.removeAttribute(FULLSCREEN_SCROLL_LOCK_ATTR);
          }}
        }}
        if (body) {{
          if (isLocked) {{
            body.setAttribute(FULLSCREEN_SCROLL_LOCK_ATTR, "1");
          }} else {{
            body.removeAttribute(FULLSCREEN_SCROLL_LOCK_ATTR);
          }}
        }}
        if (isLocked && typeof window.scrollTo === "function") {{
          window.scrollTo(0, 0);
        }}
      }}

      function syncFullscreenScrollLock() {{
        setFullscreenScrollLock(shouldLockFullscreenScroll());
      }}

      function isFullscreenScrollKey(event) {{
        const key = typeof event.key === "string" ? event.key : "";
        const code = typeof event.code === "string" ? event.code : "";
        return fullscreenScrollKeys.has(key) || fullscreenScrollCodes.has(code);
      }}

      function preventFullscreenScroll(event) {{
        if (!shouldLockFullscreenScroll()) {{
          return;
        }}
        if (event.type === "keydown" && !isFullscreenScrollKey(event)) {{
          return;
        }}
        if (event.cancelable) {{
          event.preventDefault();
        }}
      }}

      function enforceFullscreenScrollTop() {{
        if (
          !shouldLockFullscreenScroll() ||
          (window.scrollX === 0 && window.scrollY === 0) ||
          typeof window.scrollTo !== "function"
        ) {{
          return;
        }}
        window.scrollTo(0, 0);
      }}

      function buildLaunchUrl(mode) {{
        const targetUrl = new URL(LOCAL_PAGE_URL);
        targetUrl.searchParams.set("autostart", "1");
        targetUrl.searchParams.set("launchMode", mode);
        return targetUrl.toString();
      }}

      function isLocalFileLaunch() {{
        try {{
          return new URL(LOCAL_PAGE_URL).protocol === "file:";
        }} catch (err) {{
          return window.location.protocol === "file:";
        }}
      }}

      function showHttpRequiredMessage() {{
        resetLaunchState();
        setStatus("Use HTTP or HTTPS to run this build");
        showMessageDialog(
          "HTTP or HTTPS required",
          "This Unity build must be served over HTTP or HTTPS. Opening index.html directly from disk can stall at 0%. Use GitHub Pages or run a local web server."
        );
      }}

      function buildLocalUrl(relativePath) {{
        const cleanPath = String(relativePath || "").replace(/^\\.?\\//, "");
        return new URL(cleanPath, LOCAL_PAGE_URL).toString();
      }}

      function buildBuildAssetUrl(name, includeCacheBuster) {{
        const cleanName = String(name || "").replace(/^\\.?\\//, "");
        const assetUrl = new URL(cleanName, LOCAL_BUILD_ROOT_URL);
        if (
          includeCacheBuster !== false &&
          BUILD_CACHE_BUSTER &&
          /(^|\\/)[^/?#]+\\.[^/?#]+$/.test(cleanName)
        ) {{
          assetUrl.searchParams.set("v", BUILD_CACHE_BUSTER);
        }}
        return assetUrl.toString();
      }}

      function appendResourceHint(url, rel, asValue, fetchPriority) {{
        if (!url || !document.head) {{
          return;
        }}
        const key = [rel || "", asValue || "", url].join("|");
        if (resourceHints.has(key)) {{
          return;
        }}
        resourceHints.add(key);
        const link = document.createElement("link");
        link.rel = rel;
        link.href = url;
        if (asValue) {{
          link.as = asValue;
        }}
        if (asValue === "fetch") {{
          link.crossOrigin = "anonymous";
        }}
        if (fetchPriority && "fetchPriority" in link) {{
          link.fetchPriority = fetchPriority;
        }}
        document.head.appendChild(link);
      }}

      function ensureLoaderScriptLoaded(loaderUrl) {{
        if (
          BUILD_KIND === "modern" &&
          typeof window.createUnityInstance === "function"
        ) {{
          logLoaderStep("Unity loader script already ready");
          return Promise.resolve();
        }}
        if (
          BUILD_KIND === "legacy_json" &&
          window.UnityLoader &&
          typeof window.UnityLoader.instantiate === "function"
        ) {{
          logLoaderStep("Legacy Unity loader already ready");
          return Promise.resolve();
        }}
        if (loaderScriptPromise) {{
          return loaderScriptPromise;
        }}

        loaderScriptPromise = new Promise(function (resolve, reject) {{
          logLoaderStep("Loading Unity loader script");
          const existing = document.querySelector("script[data-ocean-unity-loader='1']");
          if (existing) {{
            if (existing.getAttribute("data-ocean-loader-ready") === "1") {{
              logLoaderStep("Unity loader script loaded");
              resolve();
              return;
            }}
            if (existing.getAttribute("data-ocean-loader-error") === "1") {{
              loaderScriptPromise = null;
              logLoaderStep("Unity loader script failed");
              reject(new Error("Failed to load Unity loader script"));
              return;
            }}
            existing.addEventListener("load", function () {{
              logLoaderStep("Unity loader script loaded");
              resolve();
            }}, {{ once: true }});
            existing.addEventListener("error", function () {{
              loaderScriptPromise = null;
              logLoaderStep("Unity loader script failed");
              reject(new Error("Failed to load Unity loader script"));
            }}, {{ once: true }});
            return;
          }}

          const script = document.createElement("script");
          script.src = loaderUrl;
          script.async = true;
          script.setAttribute("data-ocean-unity-loader", "1");
          script.onload = function () {{
            script.setAttribute("data-ocean-loader-ready", "1");
            logLoaderStep("Unity loader script loaded");
            resolve();
          }};
          script.onerror = function () {{
            script.setAttribute("data-ocean-loader-error", "1");
            loaderScriptPromise = null;
            logLoaderStep("Unity loader script failed");
            reject(new Error("Failed to load Unity loader script"));
          }};
          document.body.appendChild(script);
        }});

        return loaderScriptPromise;
      }}

      function buildUnityCacheControl(url) {{
        const cleanUrl = String(url || "").split("?")[0].toLowerCase();
        const fileName = cleanUrl.split("/").pop() || "";
        const hashedFile =
          /^[0-9a-f]{8,}[._-]/i.test(fileName) ||
          /(?:^|[._-])[0-9a-f]{8,}(?:[._-]|$)/i.test(fileName);
        if (
          hashedFile ||
          cleanUrl.includes("/streamingassets/") ||
          cleanUrl.endsWith(".unityweb")
        ) {{
          return "immutable";
        }}
        return "must-revalidate";
      }}

      function computeUnityDevicePixelRatio() {{
        const nativeDpr = Number(window.devicePixelRatio) || 1;
        let cap = requestedLaunchMode === "fullscreen" ? 1.5 : 1.15;
        if (constrainedPerformanceMode && requestedLaunchMode === "fullscreen") {{
          cap = 1.25;
        }} else if (isEmbeddedFrame) {{
          cap = 1;
        }} else if (constrainedPerformanceMode) {{
          cap = 1.05;
        }}
        return Math.max(1, Math.min(nativeDpr, cap));
      }}

      function warmUnityBuild() {{
        if (buildWarmupStarted || isLocalFileLaunch()) {{
          return;
        }}
        buildWarmupStarted = true;

        const loaderUrl = buildBuildAssetUrl(LOADER_FILE);
        const assetUrls = [
          {{ url: loaderUrl, as: "script", fetchPriority: "high" }},
          {{ url: buildBuildAssetUrl(FRAMEWORK_FILE), as: "fetch", fetchPriority: "high" }},
          {{ url: buildBuildAssetUrl(WASM_FILE), as: "fetch", fetchPriority: "high" }},
        ];
        if (!isEmbeddedFrame) {{
          assetUrls.push({{ url: buildBuildAssetUrl(DATA_FILE), as: "fetch", fetchPriority: "high" }});
        }}

        assetUrls.forEach(function (entry) {{
          appendResourceHint(entry.url, "preload", entry.as, entry.fetchPriority);
        }});

        if (!ENABLE_SOURCE_URL_SPOOF) {{
          ensureLoaderScriptLoaded(loaderUrl).catch(function () {{
            // Ignore loader warmup failures until launch time.
          }});
        }}
      }}

      function defineGetter(target, key, getter) {{
        if (!target || typeof getter !== "function") {{
          return false;
        }}
        try {{
          Object.defineProperty(target, key, {{
            configurable: true,
            enumerable: true,
            get: getter,
          }});
          return true;
        }} catch (err) {{
          return false;
        }}
      }}

      function installAuxiliaryAssetUrlRewrites(rewriteMap) {{
        if (!rewriteMap || typeof rewriteMap !== "object") {{
          return;
        }}
        const entries = Object.entries(rewriteMap)
          .filter(function (entry) {{
            return (
              Array.isArray(entry) &&
              typeof entry[0] === "string" &&
              entry[0] &&
              typeof entry[1] === "string" &&
              entry[1]
            );
          }})
          .map(function (entry) {{
            return [entry[0], new URL(entry[1], LOCAL_PAGE_URL).toString()];
          }});
        if (!entries.length) {{
          return;
        }}
        const rewriteTable = new Map();
        entries.forEach(function (entry) {{
          const sourceUrl = entry[0];
          const localUrl = entry[1];
          rewriteTable.set(sourceUrl, localUrl);
          try {{
            const sourcePath = new URL(sourceUrl).pathname;
            if (sourcePath) {{
              rewriteTable.set(sourcePath, localUrl);
              rewriteTable.set(new URL(sourcePath, LOCAL_PAGE_URL).toString(), localUrl);
            }}
          }} catch (err) {{
            // Ignore URL parsing failures.
          }}
        }});
        window.__unityStandaloneAuxiliaryAssetUrls = rewriteTable;
        if (window.__unityStandaloneAuxiliaryAssetRewriteInstalled) {{
          return;
        }}
        window.__unityStandaloneAuxiliaryAssetRewriteInstalled = true;

        function rewriteUrlValue(value) {{
          if (typeof value !== "string" || !value) {{
            return value;
          }}
          if (rewriteTable.has(value)) {{
            return rewriteTable.get(value);
          }}
          try {{
            const absolute = new URL(value, LOCAL_PAGE_URL).toString();
            if (rewriteTable.has(absolute)) {{
              return rewriteTable.get(absolute);
            }}
          }} catch (err) {{
            // Ignore URL parsing failures.
          }}
          if (typeof SOURCE_PAGE_URL === "string" && SOURCE_PAGE_URL) {{
            try {{
              const absoluteSource = new URL(value, SOURCE_PAGE_URL).toString();
              if (rewriteTable.has(absoluteSource)) {{
                return rewriteTable.get(absoluteSource);
              }}
            }} catch (err) {{
              // Ignore URL parsing failures.
            }}
          }}
          try {{
            const absoluteLocal = new URL(value, LOCAL_PAGE_URL).toString();
            return rewriteTable.get(absoluteLocal) || value;
          }} catch (err) {{
            return value;
          }}
        }}

        if (typeof window.fetch === "function") {{
          const originalFetch = window.fetch.bind(window);
          window.fetch = function (input, init) {{
            if (typeof input === "string") {{
              return originalFetch(rewriteUrlValue(input), init);
            }}
            if (typeof Request !== "undefined" && input instanceof Request) {{
              return originalFetch(new Request(rewriteUrlValue(input.url), input), init);
            }}
            return originalFetch(input, init);
          }};
        }}

        if (window.XMLHttpRequest && window.XMLHttpRequest.prototype) {{
          const originalOpen = window.XMLHttpRequest.prototype.open;
          window.XMLHttpRequest.prototype.open = function (method, url) {{
            arguments[1] = rewriteUrlValue(typeof url === "string" ? url : String(url || ""));
            return originalOpen.apply(this, arguments);
          }};
        }}
      }}

      function maybeSpoofSourcePageUrl() {{
        if (
          sourceUrlSpoofApplied ||
          !ENABLE_SOURCE_URL_SPOOF ||
          typeof SOURCE_PAGE_URL !== "string" ||
          !SOURCE_PAGE_URL
        ) {{
          return;
        }}
        let spoofUrl;
        try {{
          spoofUrl = new URL(SOURCE_PAGE_URL);
        }} catch (err) {{
          console.warn("Invalid source page URL for spoofing:", err);
          return;
        }}
        sourceUrlSpoofApplied = true;
        logLoaderStep("Source URL spoof enabled");
        const actualLocation = window.location;
        const spoofLocation = {{
          href: spoofUrl.toString(),
          origin: spoofUrl.origin,
          protocol: spoofUrl.protocol,
          host: spoofUrl.host,
          hostname: spoofUrl.hostname,
          port: spoofUrl.port,
          pathname: spoofUrl.pathname,
          search: spoofUrl.search,
          hash: spoofUrl.hash,
          assign: function (value) {{
            return actualLocation.assign(value);
          }},
          replace: function (value) {{
            return actualLocation.replace(value);
          }},
          reload: function () {{
            return actualLocation.reload();
          }},
          toString: function () {{
            return spoofUrl.toString();
          }},
          valueOf: function () {{
            return spoofUrl.toString();
          }},
        }};
        defineGetter(document, "URL", function () {{
          return spoofUrl.toString();
        }});
        defineGetter(document, "documentURI", function () {{
          return spoofUrl.toString();
        }});
        defineGetter(document, "baseURI", function () {{
          return spoofUrl.toString();
        }});
        defineGetter(document, "referrer", function () {{
          return spoofUrl.origin + "/";
        }});
        defineGetter(document, "location", function () {{
          return spoofLocation;
        }});
        defineGetter(window, "origin", function () {{
          return spoofUrl.origin;
        }});
        defineGetter(window, "location", function () {{
          return spoofLocation;
        }});
        defineGetter(globalThis, "location", function () {{
          return spoofLocation;
        }});
      }}

      function getEffectiveSourceUrl() {{
        if (typeof SOURCE_PAGE_URL === "string" && SOURCE_PAGE_URL) {{
          return SOURCE_PAGE_URL;
        }}
        return LOCAL_PAGE_URL;
      }}

      function getEffectiveSourceHost() {{
        const candidateUrls = [getEffectiveSourceUrl(), LOCAL_PAGE_URL];
        for (const candidate of candidateUrls) {{
          try {{
            const parsed = new URL(candidate);
            if (parsed.hostname) {{
              return parsed.hostname;
            }}
          }} catch (err) {{
            // Ignore invalid URL candidates.
          }}
        }}
        return window.location && window.location.hostname
          ? String(window.location.hostname)
          : EMPTY;
      }}

      function getLocalHostName() {{
        if (typeof LOCAL_HOST_NAME === "string" && LOCAL_HOST_NAME) {{
          return LOCAL_HOST_NAME;
        }}
        return window.location && window.location.hostname
          ? String(window.location.hostname)
          : EMPTY;
      }}

      function getEffectiveSourceSiteUrl() {{
        const candidateUrls = [getEffectiveSourceUrl(), SOURCE_PAGE_URL];
        for (const candidate of candidateUrls) {{
          if (typeof candidate !== "string" || !candidate) {{
            continue;
          }}
          try {{
            return new URL("./", candidate).toString();
          }} catch (err) {{
            // Ignore invalid URL candidates.
          }}
        }}
        return EMPTY;
      }}

      function getEffectiveGmsoftParamPayload() {{
        const sharedConfig =
          window.GMSOFT_OPTIONS && typeof window.GMSOFT_OPTIONS === "object"
            ? window.GMSOFT_OPTIONS
            : window.config && typeof window.config === "object"
              ? window.config
              : {{}};
        const localHost = getLocalHostName();
        const sourceSiteUrl = getEffectiveSourceSiteUrl();
        if (localHost) {{
          sharedConfig.domainHost = localHost;
        }}
        if (!sharedConfig.sourceHtml && sourceSiteUrl) {{
          sharedConfig.sourceHtml = sourceSiteUrl;
        }}
        if (!sharedConfig.sourceHtml && window.config && typeof window.config.sourceHtml === "string") {{
          sharedConfig.sourceHtml = window.config.sourceHtml;
        }}
        if (
          !sharedConfig.pub_id &&
          typeof sharedConfig.pubId === "string" &&
          sharedConfig.pubId
        ) {{
          sharedConfig.pub_id = sharedConfig.pubId;
        }}
        if (
          !sharedConfig.pubId &&
          typeof sharedConfig.pub_id === "string" &&
          sharedConfig.pub_id
        ) {{
          sharedConfig.pubId = sharedConfig.pub_id;
        }}
        return JSON.stringify(sharedConfig);
      }}

      installAuxiliaryAssetUrlRewrites(AUXILIARY_ASSET_REWRITES);

      function resetLaunchState() {{
        started = false;
        clearLaunchPanelHideTimer();
        if (loadingScreen) {{
          loadingScreen.classList.remove("is-loading");
        }}
        if (launchPanel) {{
          launchPanel.style.display = "";
          launchPanel.classList.remove("is-hidden");
        }}
        releaseLegacyConfigUrl();
        setProgressVisibility(false);
        setProgress(0);
        setLoadState("idle");
        setStatus(initialStatusText);
      }}

      function requestFullscreenMode() {{
        const target = document.documentElement || document.body || canvas || legacyContainer;
        if (!target) {{
          return Promise.resolve(false);
        }}
        if (
          document.fullscreenElement ||
          document.webkitFullscreenElement ||
          document.msFullscreenElement ||
          document.mozFullScreenElement
        ) {{
          return Promise.resolve(true);
        }}
        const request =
          target.requestFullscreen ||
          target.webkitRequestFullscreen ||
          target.webkitRequestFullScreen ||
          target.msRequestFullscreen ||
          target.mozRequestFullScreen;
        if (typeof request !== "function") {{
          return Promise.resolve(false);
        }}
        setFullscreenScrollLock(true);
        try {{
          return Promise.resolve(request.call(target))
            .then(function () {{
              syncFullscreenScrollLock();
              return true;
            }})
            .catch(function (err) {{
              setFullscreenScrollLock(false);
              console.warn("Fullscreen request failed:", err);
              return false;
            }});
        }} catch (err) {{
          setFullscreenScrollLock(false);
          console.warn("Fullscreen request failed:", err);
          return Promise.resolve(false);
        }}
      }}

      function consumeAutoStartFlag() {{
        const currentUrl = new URL(LOCAL_PAGE_URL);
        const shouldAutoStart = currentUrl.searchParams.get("autostart") === "1";
        if (shouldAutoStart) {{
          currentUrl.searchParams.delete("autostart");
          currentUrl.searchParams.delete("launchMode");
          const cleanedUrl = currentUrl.pathname + currentUrl.search + currentUrl.hash;
          if (window.history && typeof window.history.replaceState === "function") {{
            window.history.replaceState(null, "", cleanedUrl || currentUrl.pathname);
          }}
        }}
        return shouldAutoStart;
      }}

      function startFullscreenGame() {{
        if (isLocalFileLaunch()) {{
          showHttpRequiredMessage();
          return;
        }}
        const popup = window.open(buildLaunchUrl("fullscreen"), "_blank");
        if (!popup || popup.closed) {{
          setStatus("New tab blocked. Allow popups or use launch here.");
          return;
        }}
        try {{
          popup.opener = null;
        }} catch (err) {{
          // Ignore opener hardening failures.
        }}
        setStatus("Opened fullscreen in a new tab");
      }}

      function handleFrameLaunchClick() {{
        if (!frameLaunchAllowed) {{
          return;
        }}
        confirmRecommendedLaunchOverride("frame").then(function (launchMode) {{
          if (!launchMode) {{
            return;
          }}
          startGame();
        }});
      }}

      function handleFullscreenLaunchClick() {{
        if (!fullscreenLaunchAllowed) {{
          return;
        }}
        confirmRecommendedLaunchOverride("fullscreen").then(function (launchMode) {{
          if (!launchMode) {{
            return;
          }}
          startFullscreenGame();
        }});
      }}

      function ensureStorageAccess() {{
        const hasApi =
          typeof document.hasStorageAccess === "function" &&
          typeof document.requestStorageAccess === "function";
        if (!hasApi) {{
          logLoaderStep("Storage access API unavailable");
          return Promise.resolve();
        }}
        logLoaderStep("Checking storage access");
        return document.hasStorageAccess()
          .then(function (hasAccess) {{
            if (hasAccess) {{
              logLoaderStep("Storage access already granted");
              return;
            }}
            logLoaderStep("Requesting storage access");
            return document.requestStorageAccess().catch(function () {{
              // Continue without hard-failing game load.
              logLoaderStep("Storage access request failed");
            }});
          }})
          .catch(function () {{
            // Continue without hard-failing game load.
            logLoaderStep("Storage access check failed");
          }});
      }}

      function buildLegacyConfig() {{
        const config = JSON.parse(JSON.stringify(LEGACY_CONFIG || {{}}));
        Object.keys(config).forEach(function (key) {{
          const value = config[key];
          if (typeof value !== "string" || !value || /^data:/i.test(value)) {{
            return;
          }}
          if (!key.endsWith("Url")) {{
            return;
          }}
          if (/^[a-z][a-z0-9+.-]*:/i.test(value)) {{
            return;
          }}
          const relativeValue = value.replace(/^\\.?\\//, "");
          config[key] = buildBuildAssetUrl(
            relativeValue,
            key !== "wasmCodeUrl"
          );
        }});
        return config;
      }}

      function createFirebaseRefStub() {{
        return {{
          transaction: function (updater) {{
            if (typeof updater === "function") {{
              try {{
                updater(null);
              }} catch (err) {{
                // Ignore local stub transaction updater errors.
              }}
            }}
            return Promise.resolve({{
              val: function () {{
                return null;
              }},
            }});
          }},
          once: function () {{
            return Promise.resolve({{
              val: function () {{
                return null;
              }},
            }});
          }},
          set: function () {{
            return Promise.resolve();
          }},
          update: function () {{
            return Promise.resolve();
          }},
          remove: function () {{
            return Promise.resolve();
          }},
          push: function () {{
            return createFirebaseRefStub();
          }},
          child: function () {{
            return createFirebaseRefStub();
          }},
          orderByChild: function () {{
            return this;
          }},
          equalTo: function () {{
            return this;
          }},
          limitToFirst: function () {{
            return this;
          }},
          on: function (_eventName, callback) {{
            if (typeof callback === "function") {{
              callback({{
                val: function () {{
                  return null;
                }},
              }});
            }}
            return callback;
          }},
          off: function () {{
            return;
          }},
        }};
      }}

      function createFirebaseStub() {{
        const analytics = {{
          setUserProperties: function () {{
            return;
          }},
          logEvent: function () {{
            return;
          }},
        }};
        const auth = {{
          currentUser: null,
          signInAnonymously: function () {{
            return Promise.resolve({{}});
          }},
          signInWithEmailAndPassword: function () {{
            return Promise.resolve({{}});
          }},
          createUserWithEmailAndPassword: function () {{
            return Promise.resolve({{}});
          }},
          signOut: function () {{
            return Promise.resolve();
          }},
          onAuthStateChanged: function (callback) {{
            if (typeof callback === "function") {{
              callback(null);
            }}
            return function () {{
              return;
            }};
          }},
        }};
        const firestoreDoc = {{
          get: function () {{
            return Promise.resolve({{
              exists: false,
              data: function () {{
                return {{}};
              }},
            }});
          }},
          set: function () {{
            return Promise.resolve();
          }},
          update: function () {{
            return Promise.resolve();
          }},
        }};
        const firestore = {{
          collection: function () {{
            return {{
              doc: function () {{
                return firestoreDoc;
              }},
              add: function () {{
                return Promise.resolve({{}});
              }},
              get: function () {{
                return Promise.resolve({{
                  docs: [],
                }});
              }},
            }};
          }},
        }};
        return {{
          initializeApp: function () {{
            return {{}};
          }},
          analytics: function () {{
            return analytics;
          }},
          database: function () {{
            return {{
              ref: function () {{
                return createFirebaseRefStub();
              }},
            }};
          }},
          auth: function () {{
            return auth;
          }},
          firestore: function () {{
            return firestore;
          }},
        }};
      }}

      function installGlobalSupportStubs() {{
        if (!window.firebase || typeof window.firebase !== "object") {{
          window.firebase = createFirebaseStub();
        }}
        globalThis.firebase = window.firebase;

        if (typeof window.firebaseSetUserProperties !== "function") {{
          window.firebaseSetUserProperties = function (props) {{
            try {{
              return window.firebase.analytics().setUserProperties(props || {{}});
            }} catch (err) {{
              return;
            }}
          }};
        }}
        if (typeof window.firebaseLogEvent !== "function") {{
          window.firebaseLogEvent = function (eventName) {{
            try {{
              return window.firebase.analytics().logEvent(eventName || "");
            }} catch (err) {{
              return;
            }}
          }};
        }}
        if (typeof window.firebaseLogEventParameter !== "function") {{
          window.firebaseLogEventParameter = function (eventName, eventParams) {{
            try {{
              return window.firebase.analytics().logEvent(eventName || "", eventParams || {{}});
            }} catch (err) {{
              return;
            }}
          }};
        }}

        if (!window.GMDEBUG || typeof window.GMDEBUG !== "object") {{
          window.GMDEBUG = {{}};
        }}
        globalThis.GMDEBUG = window.GMDEBUG;

        if (typeof window.adConfig !== "function") {{
          window.adConfig = function (options) {{
            if (options && typeof options.onReady === "function") {{
              options.onReady();
            }}
          }};
        }}

        if (!Array.isArray(window.adsbygoogle)) {{
          window.adsbygoogle = [];
        }}

        if (!window.LocalAds || typeof window.LocalAds !== "object") {{
          window.LocalAds = {{
            fetchAd: function (callback) {{
              if (typeof callback === "function") {{
                callback({{}});
              }}
            }},
            refetchAd: function (callback) {{
              if (typeof callback === "function") {{
                callback({{}});
              }}
            }},
            registerRewardCallbacks: function (callbacks) {{
              if (callbacks && typeof callbacks.onReady === "function") {{
                callbacks.onReady();
              }}
            }},
            showRewardAd: function () {{
              return;
            }},
            showAd: function () {{
              return;
            }},
            available: function () {{
              return false;
            }},
          }};
        }}

        if (!window.preroll || typeof window.preroll !== "object") {{
          window.preroll = {{
            config: {{
              loaderObjectName: "LocalAds",
            }},
          }};
        }}
        if (!window.preroll.config || typeof window.preroll.config !== "object") {{
          window.preroll.config = {{
            loaderObjectName: "LocalAds",
          }};
        }}
        if (!window.preroll.config.loaderObjectName) {{
          window.preroll.config.loaderObjectName = "LocalAds";
        }}

        if (typeof window.isDiffHost !== "function") {{
          window.isDiffHost = function () {{
            try {{
              if (window.top && window === window.top) {{
                return false;
              }}
              if (window.top.location.hostname === window.location.hostname) {{
                return false;
              }}
            }} catch (err) {{
              return true;
            }}
            return true;
          }};
        }}

        if (typeof window.isHostOnGD !== "function") {{
          window.isHostOnGD = function () {{
            const domainParts = String(window.location.hostname || "").split(".");
            const mainDomain = domainParts.slice(-2).join(".");
            return mainDomain === "gamedistribution.com";
          }};
        }}
        if (typeof window.isHostOnGDSDK !== "function") {{
          window.isHostOnGDSDK = window.isHostOnGD;
        }}
      }}

      function applyEntryPageSupport(unityConfig) {{
        logLoaderStep("Preparing game config");
        installGlobalSupportStubs();
        const extractedConfig = Object.assign({{}}, ENTRY_PAGE_CONFIG || {{}});
        if (extractedConfig.gdHost === "__standalone_isHostOnGD__") {{
          extractedConfig.gdHost = window.isHostOnGD();
        }}
        if (!extractedConfig.streamingAssetsUrl && STREAMING_ASSETS_URL) {{
          extractedConfig.streamingAssetsUrl = STREAMING_ASSETS_URL;
        }}
        extractedConfig.enableAds = false;
        if (Object.prototype.hasOwnProperty.call(extractedConfig, "eventLog")) {{
          extractedConfig.eventLog = false;
        }}
        if (Object.prototype.hasOwnProperty.call(extractedConfig, "enablePromotion")) {{
          extractedConfig.enablePromotion = false;
        }}
        if (Object.prototype.hasOwnProperty.call(extractedConfig, "enableMoreGame")) {{
          extractedConfig.enableMoreGame = "no";
        }}

        const mergedConfig = Object.assign({{}}, unityConfig, extractedConfig);
        mergedConfig.dataUrl = unityConfig.dataUrl;
        mergedConfig.frameworkUrl = unityConfig.frameworkUrl;
        mergedConfig.codeUrl = unityConfig.codeUrl;
        mergedConfig.cacheControl = unityConfig.cacheControl;
        mergedConfig.devicePixelRatio = unityConfig.devicePixelRatio;
        mergedConfig.matchWebGLToCanvasSize = unityConfig.matchWebGLToCanvasSize;
        mergedConfig.webglContextAttributes = unityConfig.webglContextAttributes;
        mergedConfig.referrer = document.referrer || "";
        mergedConfig.enableAds = false;
        mergedConfig.allow_embed = "yes";
        mergedConfig.allow_host = "yes";
        mergedConfig.allow_play = "yes";
        mergedConfig.sdkversion = Number(mergedConfig.sdkversion) || 5;
        mergedConfig.unlockTimer = Number(mergedConfig.unlockTimer) || 60;
        mergedConfig.timeShowInter = Number(mergedConfig.timeShowInter) || 60;
        mergedConfig.timeShowReward = Number(mergedConfig.timeShowReward) || 60;
        mergedConfig.adsDebug = true;
        mergedConfig.game = mergedConfig.game || null;
        mergedConfig.promotion = mergedConfig.promotion || null;
        mergedConfig.companyName = mergedConfig.companyName || PRODUCT_NAME;
        mergedConfig.productName = mergedConfig.productName || PRODUCT_NAME;
        mergedConfig.productVersion = mergedConfig.productVersion || "1.0.0";
        if (!mergedConfig.pub_id && typeof mergedConfig.pubId === "string") {{
          mergedConfig.pub_id = mergedConfig.pubId;
        }}
        if (!mergedConfig.pubId && typeof mergedConfig.pub_id === "string") {{
          mergedConfig.pubId = mergedConfig.pub_id;
        }}

        const gmsoftDefaults = {{
          allow_embed: "yes",
          allow_host: "yes",
          allow_play: "yes",
          debug_mode: "yes",
          enableAds: false,
          enablePreroll: false,
          domainHost: getLocalHostName(),
          sourceHtml: getEffectiveSourceSiteUrl(),
          gameId: typeof mergedConfig.gameId === "string" ? mergedConfig.gameId : "",
          hostindex: Number(extractedConfig.hostindex) || 0,
          sdkType: typeof mergedConfig.sdkType === "string" && mergedConfig.sdkType
            ? mergedConfig.sdkType
            : "disabled",
          sdkversion: Number(mergedConfig.sdkversion) || 5,
          unlockTimer: Number(mergedConfig.unlockTimer) || 60,
          timeShowInter: Number(mergedConfig.timeShowInter) || 60,
          timeShowReward: Number(mergedConfig.timeShowReward) || 60,
          adsDebug: true,
          game: mergedConfig.game || null,
          promotion: mergedConfig.promotion || null,
        }};
        Object.assign(mergedConfig, gmsoftDefaults);
        if (window.GMSOFT_OPTIONS && typeof window.GMSOFT_OPTIONS === "object") {{
          Object.assign(mergedConfig, window.GMSOFT_OPTIONS);
        }}
        mergedConfig.domainHost = getLocalHostName() || mergedConfig.domainHost || "";
        mergedConfig.sourceHtml =
          getEffectiveSourceSiteUrl() ||
          mergedConfig.sourceHtml ||
          "";
        mergedConfig.enableAds = false;
        mergedConfig.enablePreroll = false;
        mergedConfig.allow_embed = "yes";
        mergedConfig.allow_host = "yes";
        mergedConfig.allow_play = "yes";
        if (typeof mergedConfig.gameId === "string" && mergedConfig.gameId) {{
          mergedConfig.gameId = mergedConfig.gameId;
        }}

        window.config = mergedConfig;
        globalThis.config = mergedConfig;
        window.GMSOFT_OPTIONS = mergedConfig;
        globalThis.GMSOFT_OPTIONS = mergedConfig;
        return mergedConfig;
      }}

      function maybeBootstrapUnitySupport(unityInstance) {{
        if (
          !unityInstance ||
          typeof unityInstance.SendMessage !== "function" ||
          !ENTRY_PAGE_CONFIG ||
          typeof ENTRY_PAGE_CONFIG !== "object"
        ) {{
          return;
        }}
        const looksLikeGmsoftBuild =
          Boolean(ENTRY_PAGE_CONFIG.gameId) ||
          Boolean(ENTRY_PAGE_CONFIG.buildAPI) ||
          Object.prototype.hasOwnProperty.call(ENTRY_PAGE_CONFIG, "hostindex");
        if (!looksLikeGmsoftBuild) {{
          return;
        }}

        function safeSend(objectName, methodName, value) {{
          try {{
            if (typeof value === "undefined") {{
              unityInstance.SendMessage(objectName, methodName);
            }} else {{
              unityInstance.SendMessage(objectName, methodName, value);
            }}
          }} catch (err) {{
            // Ignore unsupported bridge calls for non-GmSoft builds.
          }}
        }}

        function syncGmsoftPayload() {{
          if (!window.GMSOFT_OPTIONS || typeof window.GMSOFT_OPTIONS !== "object") {{
            return;
          }}
          const localHost = getLocalHostName();
          if (localHost) {{
            window.GMSOFT_OPTIONS.domainHost = localHost;
          }}
          if (!window.GMSOFT_OPTIONS.sourceHtml) {{
            window.GMSOFT_OPTIONS.sourceHtml = getEffectiveSourceSiteUrl();
          }}
          if (
            !window.GMSOFT_OPTIONS.sourceHtml &&
            window.config &&
            typeof window.config.sourceHtml === "string"
          ) {{
            window.GMSOFT_OPTIONS.sourceHtml = window.config.sourceHtml;
          }}
          const payload = getEffectiveGmsoftParamPayload();
          safeSend("GmSoft", "SetUnityHostName", localHost || "");
          safeSend("GmSoft", "SetParam", payload);
        }}

        syncGmsoftPayload();
        [300, 900, 1800, 3200, 5200].forEach(function (delay) {{
          window.setTimeout(syncGmsoftPayload, delay);
        }});
        try {{
          document.dispatchEvent(new CustomEvent("gmsoftSdkReady"));
        }} catch (err) {{
          // Ignore event dispatch failures.
        }}
      }}

      function startModernGame(loaderUrl) {{
        logLoaderStep("Starting Unity runtime");
        const unityConfig = {{
          dataUrl: buildBuildAssetUrl(DATA_FILE),
          frameworkUrl: buildBuildAssetUrl(FRAMEWORK_FILE),
          codeUrl: buildBuildAssetUrl(WASM_FILE),
          streamingAssetsUrl: STREAMING_ASSETS_URL || buildBuildAssetUrl("StreamingAssets"),
          companyName: PRODUCT_NAME,
          productName: PRODUCT_NAME,
          productVersion: "1.0.0",
          cacheControl: buildUnityCacheControl,
          devicePixelRatio: computeUnityDevicePixelRatio(),
          matchWebGLToCanvasSize: true,
          webglContextAttributes: {{
            preserveDrawingBuffer: false,
            powerPreference: "high-performance",
          }},
        }};
        const config = applyEntryPageSupport(unityConfig);
{decompression_fallback_line}        ensureLoaderScriptLoaded(loaderUrl)
          .then(function () {{
          logLoaderStep("Creating Unity instance");
          if (typeof createUnityInstance !== "function") {{
            resetLaunchState();
            setLoadState("failed");
            setStatus("Loader error: createUnityInstance is missing");
            return;
          }}

          createUnityInstance(canvas, config, function (progress) {{
            const percent = setProgress(progress);
            setStatus("Loading " + percent + "%");
          }})
          .then(function (unityInstance) {{
            window.unityInstance = unityInstance;
            window.gameInstance = unityInstance;
            window.myGameInstance = unityInstance;
            maybeBootstrapUnitySupport(unityInstance);
            setProgress(1);
            logLoaderStep("Unity instance ready");
            setStatus("Ready");
            window.setTimeout(dismissLoadingScreen, 380);
          }})
          .catch(function (err) {{
            console.error(err);
            resetLaunchState();
            setLoadState("failed");
            setStatus("Failed to load game");
            showMessageDialog("Failed to load game", "Unity failed to load: " + err);
          }});
        }})
        .catch(function (err) {{
          console.error(err);
          resetLaunchState();
          setLoadState("failed");
          setStatus("Failed to load Unity loader script");
        }});
      }}

      function startLegacyGame(loaderUrl) {{
        if (!legacyContainer) {{
          resetLaunchState();
          setStatus("Legacy Unity container is missing");
          return;
        }}

        logLoaderStep("Preparing legacy Unity config");
        const configBlob = new Blob(
          [JSON.stringify(buildLegacyConfig())],
          {{ type: "application/json" }}
        );
        legacyConfigUrl =
          typeof URL.createObjectURL === "function" ? URL.createObjectURL(configBlob) : "";
        if (!legacyConfigUrl) {{
          resetLaunchState();
          setStatus("Failed to prepare legacy Unity config");
          return;
        }}

        ensureLoaderScriptLoaded(loaderUrl)
          .then(function () {{
          logLoaderStep("Creating legacy Unity instance");
          const instantiate =
            window.UnityLoader && typeof window.UnityLoader.instantiate === "function"
              ? window.UnityLoader.instantiate
              : null;
          if (!instantiate) {{
            resetLaunchState();
            setLoadState("failed");
            setStatus("Loader error: UnityLoader.instantiate is missing");
            return;
          }}

          try {{
            instantiate(legacyContainer, legacyConfigUrl, {{
              onProgress: function (_instance, progress) {{
                const percent = setProgress(progress);
                setStatus("Loading " + percent + "%");
                if (progress >= 1) {{
                  window.setTimeout(function () {{
                    releaseLegacyConfigUrl();
                    dismissLoadingScreen();
                    logLoaderStep("Legacy Unity instance ready");
                    setStatus("Ready");
                  }}, 380);
                }}
              }},
            }});
          }} catch (err) {{
            console.error(err);
            resetLaunchState();
            setLoadState("failed");
            setStatus("Failed to load game");
            showMessageDialog("Failed to load game", "Unity failed to load: " + err);
          }}
        }})
        .catch(function () {{
          resetLaunchState();
          setLoadState("failed");
          setStatus("Failed to load Unity loader script");
        }});
      }}

      function startGame() {{
        if (isLocalFileLaunch()) {{
          showHttpRequiredMessage();
          return;
        }}
        if (started) {{
          return;
        }}
        logLoaderStep("Launch requested");
        started = true;
        setLoadState("loading");
        if (loadingScreen) {{
          loadingScreen.classList.add("is-loading");
        }}
        if (launchPanel) {{
          clearLaunchPanelHideTimer();
          launchPanel.style.display = "";
          launchPanel.classList.add("is-hidden");
          launchPanelHideTimer = window.setTimeout(function () {{
            if (launchPanel && launchPanel.classList.contains("is-hidden")) {{
              launchPanel.style.display = "none";
            }}
            launchPanelHideTimer = 0;
          }}, 240);
        }}
        setProgressVisibility(true);
        setProgress(0);
        setStatus("Loading 0%");

        ensureStorageAccess().finally(function () {{
          const loaderUrl = buildBuildAssetUrl(LOADER_FILE);
          maybeSpoofSourcePageUrl();
          if (BUILD_KIND === "legacy_json") {{
            startLegacyGame(loaderUrl);
            return;
          }}
          startModernGame(loaderUrl);
        }});
      }}

      setProgressVisibility(false);
      setProgress(0);
      setLoadState("idle");
      logLoaderStep("Shell initialized");
      updateLaunchModeUi();
      setStatus(initialStatusText);

      window.addEventListener("wheel", preventFullscreenScroll, {{ passive: false }});
      window.addEventListener("touchmove", preventFullscreenScroll, {{ passive: false }});
      window.addEventListener("keydown", preventFullscreenScroll, {{ passive: false }});
      window.addEventListener("scroll", enforceFullscreenScrollTop, {{ passive: true }});
      window.addEventListener("fullscreenchange", syncFullscreenScrollLock);
      window.addEventListener("webkitfullscreenchange", syncFullscreenScrollLock);
      window.addEventListener("mozfullscreenchange", syncFullscreenScrollLock);
      window.addEventListener("MSFullscreenChange", syncFullscreenScrollLock);
      syncFullscreenScrollLock();

      launchFullscreenBtn.addEventListener("click", handleFullscreenLaunchClick);
      launchFrameBtn.addEventListener("click", handleFrameLaunchClick);

      if (typeof window.requestIdleCallback === "function") {{
        window.requestIdleCallback(warmUnityBuild, {{ timeout: 1200 }});
      }} else {{
        window.setTimeout(warmUnityBuild, 240);
      }}

      if (consumeAutoStartFlag()) {{
        startGame();
      }}
    }})();
  </script>
</body>
</html>
"""


def download_assets(
    output_build_dir: Path,
    candidates: dict[str, list[str]],
    progress_file: Path,
    referer_url: str = "",
) -> DownloadedAssets:
    progress = load_json_file(progress_file)
    if progress.get("candidate_urls") != candidates:
        progress = {
            "candidate_urls": candidates,
            "assets": {},
            "completed": False,
        }
        save_json_file(progress_file, progress)

    assets_state = progress.get("assets")
    if not isinstance(assets_state, dict):
        assets_state = {}
        progress["assets"] = assets_state

    def download_or_resume(kind: str) -> str:
        existing = assets_state.get(kind) if isinstance(assets_state, dict) else None
        if isinstance(existing, dict):
            existing_name = existing.get("filename", "")
            existing_path = output_build_dir / existing_name
            if existing_name and existing_path.exists() and existing_path.stat().st_size > 0:
                log(f"{kind}: reusing {existing_name}")
                return existing_name

        possible_names = [basename_from_url(url) for url in candidates[kind]]
        destination = output_build_dir / possible_names[0]
        resolved_url, _, compression_kind = download_first_valid(
            candidates[kind],
            destination,
            referer_url=referer_url,
        )

        resolved_name = basename_from_url(resolved_url)
        if kind != "loader":
            lower_name = resolved_name.lower()
            if compression_kind == "br" and not (
                lower_name.endswith(".br") or lower_name.endswith(".unityweb")
            ):
                resolved_name = resolved_name + ".br"
            elif compression_kind == "gzip" and not (
                lower_name.endswith(".gz") or lower_name.endswith(".unityweb")
            ):
                resolved_name = resolved_name + ".gz"
        if destination.name != resolved_name:
            corrected_path = output_build_dir / resolved_name
            destination.replace(corrected_path)
            final_path = corrected_path
        else:
            final_path = destination

        assets_state[kind] = {
            "filename": final_path.name,
            "url": resolved_url,
            "size": final_path.stat().st_size,
        }
        progress["assets"] = assets_state
        save_json_file(progress_file, progress)
        log(f"{kind}: downloaded {final_path.name}")
        return final_path.name

    loader_name = download_or_resume("loader")
    framework_name = download_or_resume("framework")
    data_name = download_or_resume("data")
    wasm_name = download_or_resume("wasm")

    used_br_assets = any(
        name.lower().endswith((".br", ".gz", ".unityweb"))
        for name in (framework_name, data_name, wasm_name)
    )

    return DownloadedAssets(
        loader_name=loader_name,
        framework_name=framework_name,
        data_name=data_name,
        wasm_name=wasm_name,
        used_br_assets=used_br_assets,
        build_kind="modern",
    )


def download_legacy_assets(
    output_build_dir: Path,
    candidates: dict[str, list[str]],
    legacy_config: dict[str, Any],
    progress_file: Path,
    referer_url: str = "",
) -> DownloadedAssets:
    progress = load_json_file(progress_file)
    expected_signature = {
        "build_kind": "legacy_json",
        "candidate_urls": candidates,
        "legacy_config": legacy_config,
    }
    if (
        progress.get("build_kind") != "legacy_json"
        or progress.get("candidate_urls") != candidates
        or progress.get("legacy_config") != legacy_config
    ):
        progress = {
            "build_kind": "legacy_json",
            "candidate_urls": candidates,
            "legacy_config": legacy_config,
            "assets": {},
            "completed": False,
        }
        save_json_file(progress_file, progress)
    else:
        progress.update(expected_signature)

    assets_state = progress.get("assets")
    if not isinstance(assets_state, dict):
        assets_state = {}
        progress["assets"] = assets_state

    def download_or_resume(kind: str) -> str:
        existing = assets_state.get(kind) if isinstance(assets_state, dict) else None
        if isinstance(existing, dict):
            existing_name = existing.get("filename", "")
            existing_path = output_build_dir / existing_name
            if existing_name and existing_path.exists() and existing_path.stat().st_size > 0:
                log(f"{kind}: reusing {existing_name}")
                return existing_name

        possible_names = [basename_from_url(url) for url in candidates[kind]]
        destination = output_build_dir / possible_names[0]
        resolved_url, _, compression_kind = download_first_valid(
            candidates[kind],
            destination,
            referer_url=referer_url,
        )

        resolved_name = basename_from_url(resolved_url)
        if kind != "loader":
            lower_name = resolved_name.lower()
            if compression_kind == "br" and not (
                lower_name.endswith(".br") or lower_name.endswith(".unityweb")
            ):
                resolved_name = resolved_name + ".br"
            elif compression_kind == "gzip" and not (
                lower_name.endswith(".gz") or lower_name.endswith(".unityweb")
            ):
                resolved_name = resolved_name + ".gz"
        if destination.name != resolved_name:
            corrected_path = output_build_dir / resolved_name
            destination.replace(corrected_path)
            final_path = corrected_path
        else:
            final_path = destination

        assets_state[kind] = {
            "filename": final_path.name,
            "url": resolved_url,
            "size": final_path.stat().st_size,
        }
        progress["assets"] = assets_state
        save_json_file(progress_file, progress)
        log(f"{kind}: downloaded {final_path.name}")
        return final_path.name

    downloaded_names: dict[str, str] = {}
    for kind in ["loader"] + sorted(key for key in candidates if key != "loader"):
        downloaded_names[kind] = download_or_resume(kind)

    localized_config = json.loads(json.dumps(legacy_config))
    for key, name in downloaded_names.items():
        if key != "loader" and key in localized_config:
            localized_config[key] = name

    used_br_assets = any(
        name.lower().endswith((".br", ".gz", ".unityweb"))
        for key, name in downloaded_names.items()
        if key != "loader"
    )

    return DownloadedAssets(
        loader_name=downloaded_names["loader"],
        framework_name=(
            downloaded_names.get("wasmFrameworkUrl")
            or downloaded_names.get("frameworkUrl")
            or downloaded_names.get("asmFrameworkUrl")
            or ""
        ),
        data_name=downloaded_names.get("dataUrl", ""),
        wasm_name=(
            downloaded_names.get("wasmCodeUrl")
            or downloaded_names.get("codeUrl")
            or downloaded_names.get("wasmUrl")
            or downloaded_names.get("asmCodeUrl")
            or ""
        ),
        used_br_assets=used_br_assets,
        build_kind="legacy_json",
        legacy_config=localized_config,
        legacy_asset_names={key: value for key, value in downloaded_names.items() if key != "loader"},
    )


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download a Unity WebGL build, Eagler bundle, or extracted HTML5 entry "
            "and generate a standalone package."
        )
    )
    parser.add_argument(
        "entry_url",
        nargs="?",
        help="Optional entry page URL (any host) to auto-detect a supported game entry.",
    )
    parser.add_argument(
        "--loader-url",
        default="",
        help="Direct URL for Unity loader file (*.loader.js)",
    )
    parser.add_argument(
        "--framework-url",
        default="",
        help="Direct URL for Unity framework file (*.framework.js / *.framework.js.br / *.framework.js.gz / *.framework.js.unityweb)",
    )
    parser.add_argument(
        "--data-url",
        default="",
        help="Direct URL for Unity data file (*.data / *.data.br / *.data.gz / *.data.unityweb)",
    )
    parser.add_argument(
        "--wasm-url",
        default="",
        help="Direct URL for Unity wasm file (*.wasm / *.wasm.br / *.wasm.gz / *.wasm.unityweb)",
    )
    parser.add_argument(
        "--out",
        dest="out_dir",
        default="",
        help="Output directory name/path (default: inferred from game)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output directory if it exists",
    )
    parser.add_argument(
        "--launch-options",
        default="both",
        choices=("frame", "fullscreen", "both"),
        help="Which launch options the generated launcher should expose",
    )
    parser.add_argument(
        "--recommended-launch",
        default="none",
        choices=("frame", "fullscreen", "none"),
        help="Recommended launch option when both frame and fullscreen are enabled",
    )
    return parser.parse_args(argv)


def normalize_launch_preferences(
    allowed_launch_modes: str,
    recommended_launch_mode: str,
) -> tuple[str, str]:
    normalized_allowed = str(allowed_launch_modes or "").strip().lower()
    if normalized_allowed not in {"frame", "fullscreen", "both"}:
        normalized_allowed = "both"

    if normalized_allowed in {"frame", "fullscreen"}:
        return normalized_allowed, normalized_allowed

    normalized_recommended = str(recommended_launch_mode or "").strip().lower()
    if normalized_recommended not in {"frame", "fullscreen", "none"}:
        normalized_recommended = "none"
    return normalized_allowed, normalized_recommended


def infer_output_name_from_url(root_url: str, loader_url: str) -> str:
    loader_name = basename_from_url(loader_url)
    if loader_name.endswith(".loader.js"):
        stem = loader_name[: -len(".loader.js")]
        return slugify_name(stem)

    parsed = urllib.parse.urlparse(root_url)
    path_segments = [segment for segment in parsed.path.split("/") if segment]
    if path_segments:
        return slugify_name(path_segments[-1])

    host_part = parsed.netloc.split(".")[0] or "unity-game"
    return slugify_name(host_part)


def infer_output_name_from_entry(
    title: str,
    root_url: str,
    fallback_name: str = "standalone-game",
    source_page_url: str = "",
) -> str:
    cleaned_title = clean_inferred_title(title)
    if cleaned_title:
        cleaned = slugify_name(cleaned_title)
        if cleaned:
            return cleaned

    for candidate_url in (root_url, source_page_url):
        inferred_title = infer_title_from_url(candidate_url)
        if inferred_title:
            cleaned = slugify_name(inferred_title)
            if cleaned:
                return cleaned

    host_part = urllib.parse.urlparse(root_url).netloc.split(".")[0] or fallback_name
    return slugify_name(host_part)


def sanitize_filename(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return cleaned or fallback


def decode_data_url_bytes(data_url: str) -> bytes:
    try:
        header, payload = data_url.split(",", 1)
    except ValueError as exc:
        raise FetchError("Invalid embedded data URL.") from exc

    if ";base64" in header.lower():
        try:
            return base64.b64decode(payload)
        except (ValueError, TypeError) as exc:
            raise FetchError("Invalid base64 payload in embedded data URL.") from exc

    return urllib.parse.unquote_to_bytes(payload)


def download_raw_asset(source_url: str, destination: Path, referer_url: str = "") -> str:
    if source_url.startswith("data:"):
        raw = decode_data_url_bytes(source_url)
        if not raw:
            raise FetchError("Embedded data URL produced an empty asset.")
        destination.write_bytes(raw)
        return "embedded-data-url"

    resolved, raw, _, _ = fetch_url(source_url, referer_url=referer_url)
    if not raw:
        raise FetchError(f"{source_url} -> empty response")
    if looks_like_html(raw):
        raise FetchError(f"{source_url} -> returned HTML instead of a downloadable asset")
    destination.write_bytes(raw)
    return resolved


def maybe_download_optional_asset(source_url: str, destination: Path, referer_url: str = "") -> str:
    try:
        resolved, raw, _, _ = fetch_url(source_url, referer_url=referer_url)
    except FetchError:
        return ""
    if not raw or looks_like_html(raw):
        return ""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    return resolved


def download_unity_support_scripts(
    output_dir: Path,
    script_urls: Sequence[str],
    *,
    referer_url: str = "",
) -> list[dict[str, str]]:
    if not script_urls:
        return []

    support_dir = output_dir / "support"
    support_dir.mkdir(parents=True, exist_ok=True)
    used_names: set[str] = set()
    downloaded: list[dict[str, str]] = []

    for index, script_url in enumerate(script_urls, start=1):
        fallback_name = f"support-{index}.js"
        script_name = sanitize_filename(basename_from_url(script_url), fallback_name)
        if script_name.lower() in used_names:
            stem, dot, suffix = script_name.rpartition(".")
            stem = stem or script_name
            dot = "." if dot else ""
            suffix = suffix if dot else ""
            counter = 2
            while True:
                candidate = f"{stem}-{counter}{dot}{suffix}"
                if candidate.lower() not in used_names:
                    script_name = candidate
                    break
                counter += 1
        used_names.add(script_name.lower())
        destination = support_dir / script_name
        try:
            resolved_url = download_raw_asset(
                script_url,
                destination,
                referer_url=referer_url,
            )
        except FetchError:
            log(f"Skipping optional Unity support script: {script_url}")
            continue
        downloaded.append(
            {
                "url": script_url,
                "resolved_url": resolved_url,
                "name": f"support/{script_name}",
            }
        )
        log(f"support-script: downloaded {script_name}")

    return downloaded


def collect_auxiliary_asset_rewrites(
    output_dir: Path,
    source_page_url: str,
    original_folder_url: str,
    analysis_paths: Sequence[Path],
) -> dict[str, str]:
    if not analysis_paths:
        return {}

    def references_any(*patterns: bytes) -> bool:
        return any(
            file_contains_any_bytes(path, patterns)
            for path in analysis_paths
            if path and path.name
        )

    rewrites: dict[str, str] = {}
    if source_page_url and references_any(b"setting.txt"):
        source_url = normalize_url(urllib.parse.urljoin(origin_root_url(source_page_url), "setting.txt"))
        resolved = maybe_download_optional_asset(
            source_url,
            output_dir / "setting.txt",
            referer_url=source_page_url,
        )
        if resolved:
            local_rewrite_path = "setting.txt"
            if should_route_setting_to_parent_root(source_page_url, source_url, original_folder_url):
                mirrored = maybe_download_optional_asset(
                    source_url,
                    output_dir.parent / "setting.txt",
                    referer_url=source_page_url,
                )
                if mirrored:
                    local_rewrite_path = "../setting.txt"
            rewrites[source_url] = local_rewrite_path
            log("auxiliary: downloaded setting.txt")

    return rewrites


def copy_eagler_support_files(output_dir: Path) -> list[str]:
    script_dir = Path(__file__).resolve().parent
    copied: list[str] = []
    for name in ("ocean-launcher.css", "ocean-launcher.js"):
        source = script_dir / name
        if not source.exists():
            raise FetchError(f"Missing support file next to unity_standalone.py: {source}")
        shutil.copyfile(source, output_dir / name)
        copied.append(name)
    return copied


def compute_launcher_support_cache_buster(output_dir: Path) -> str:
    digest = hashlib.sha256()
    has_content = False
    for name in ("ocean-launcher.css", "ocean-launcher.js"):
        path = output_dir / name
        if not path.exists():
            continue
        digest.update(name.encode("utf-8"))
        digest.update(path.read_bytes())
        has_content = True
    return digest.hexdigest()[:12] if has_content else ""


def compute_output_file_cache_buster(output_dir: Path, filenames: Sequence[str]) -> str:
    digest = hashlib.sha256()
    has_content = False
    for name in filenames:
        if not str(name).strip():
            continue
        path = output_dir / name
        if not path.exists():
            continue
        digest.update(name.encode("utf-8"))
        digest.update(path.read_bytes())
        has_content = True
    return digest.hexdigest()[:12] if has_content else ""


def suppress_known_html_alert_calls(document_html: str) -> str:
    return re.sub(
        r"""\b(?:window\.)?alert\(\s*(["'])Error loading game\1\s*\)\s*;?""",
        'console.error("Error loading game");',
        document_html,
        flags=re.IGNORECASE,
    )


def disable_known_html_console_muting(document_html: str) -> str:
    patched = re.sub(
        r"""\b(?:const|let|var)\s+muteConsole\s*=\s*true\s*;""",
        "const muteConsole = false;",
        document_html,
        count=1,
        flags=re.IGNORECASE,
    )
    patched = re.sub(
        r"""if\s*\(\s*muteConsole\s*\)\s*\{\s*console\.log\s*=\s*console\.warn\s*=\s*console\.error\s*=\s*console\.info\s*=\s*console\.debug\s*=\s*\(\)\s*=>\s*\{\s*\}\s*;\s*\}""",
        "if (muteConsole) { console.warn('[ocean] source attempted to mute console output'); }",
        patched,
        count=1,
        flags=re.IGNORECASE,
    )
    return patched


def download_eagler_mobile_script(output_dir: Path) -> dict[str, str]:
    script_name = "eaglermobile.user.js"
    resolved_url = download_raw_asset(
        EAGLER_MOBILE_USERSCRIPT_URL,
        output_dir / script_name,
        referer_url="https://github.com/FlamedDogo99/EaglerMobile",
    )
    return {
        "name": script_name,
        "resolved_url": resolved_url,
    }


def generate_html_entry_index_html(title: str, source_html: str) -> str:
    document = source_html.lstrip("\ufeff").strip()
    document = suppress_known_html_alert_calls(document)
    document = disable_known_html_console_muting(document)
    document = re.sub(r"(?im)^[ \t]*/body>\s*$", "", document)
    document = re.sub(r"(?im)^[ \t]*body>\s*$", "", document)
    if not document:
        raise FetchError("Detected HTML entry was empty.")

    title_tag = f"<title>{html.escape(title)}</title>"
    viewport_tag = (
        '<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />'
    )
    charset_tag = '<meta charset="utf-8" />'
    cache_meta_tags = (
        '<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate, max-age=0" />\n'
        '<meta http-equiv="Pragma" content="no-cache" />\n'
        '<meta http-equiv="Expires" content="0" />'
    )
    alert_shim_tag = (
        "<script>(function(){"
        "window.__oceanNativeAlert=window.alert;"
        "window.alert=function(message){"
        "try{console.error('[alert]',message);}catch(_e){}"
        "try{window.parent&&window.parent!==window&&window.parent.postMessage({type:'ocean-alert',message:String(message||'')},'*');}catch(_e){}"
        "};"
        "})();</script>"
    )
    preserved_base_tag = ""

    def strip_base_tag(match: re.Match[str]) -> str:
        nonlocal preserved_base_tag
        if not preserved_base_tag:
            target_match = re.search(r"\btarget\s*=\s*(['\"])(.*?)\1", match.group(0), re.IGNORECASE)
            if target_match:
                preserved_base_tag = (
                    f'<base target="{html.escape(target_match.group(2), quote=True)}" />'
                )
        return ""

    document = re.sub(r"<base\b[^>]*>", strip_base_tag, document, flags=re.IGNORECASE)

    injections: list[str] = []
    if not re.search(r"<meta\b[^>]*charset\s*=", document, re.IGNORECASE):
        injections.append(charset_tag)
    if not re.search(r"<meta\b[^>]*name\s*=\s*['\"]viewport['\"]", document, re.IGNORECASE):
        injections.append(viewport_tag)
    injections.append(cache_meta_tags)
    if preserved_base_tag:
        injections.append(preserved_base_tag)
    if title and not re.search(r"<title\b", document, re.IGNORECASE):
        injections.append(title_tag)
    injections.append(alert_shim_tag)

    injection = "\n".join(injections)
    if re.search(r"<head\b[^>]*>", document, re.IGNORECASE):
        if injection:
            document = re.sub(
                r"(<head\b[^>]*>)",
                r"\1\n" + injection + "\n",
                document,
                count=1,
                flags=re.IGNORECASE,
            )
    elif re.search(r"<html\b[^>]*>", document, re.IGNORECASE):
        head_block = "<head>\n"
        if injection:
            head_block += injection + "\n"
        head_block += "</head>\n"
        document = re.sub(
            r"(<html\b[^>]*>)",
            r"\1\n" + head_block,
            document,
            count=1,
            flags=re.IGNORECASE,
        )
    else:
        body = document
        document = (
            "<!DOCTYPE html>\n"
            "<html lang=\"en\">\n"
            "<head>\n"
            f"{charset_tag}\n"
            f"{viewport_tag}\n"
            f"{cache_meta_tags}\n"
            + (f"{preserved_base_tag}\n" if preserved_base_tag else "")
            + (f"{title_tag}\n" if title else "")
            + f"{alert_shim_tag}\n"
            + "</head>\n"
            "<body>\n"
            f"{body}\n"
            "</body>\n"
            "</html>\n"
        )

    if not re.match(r"<!doctype\s+html", document, re.IGNORECASE):
        document = "<!DOCTYPE html>\n" + document

    if re.search(r"<body\b", document, re.IGNORECASE) and not re.search(
        r"</body\s*>",
        document,
        re.IGNORECASE,
    ):
        if re.search(r"</html\s*>", document, re.IGNORECASE):
            document = re.sub(
                r"</html\s*>",
                "</body>\n</html>",
                document,
                count=1,
                flags=re.IGNORECASE,
            )
        else:
            document += "\n</body>\n"

    return document


def inject_head_script_tags(document_html: str, script_filenames: Sequence[str]) -> str:
    script_tags = "\n".join(
        f'<script type="text/javascript" src="./{html.escape(filename)}"></script>'
        for filename in script_filenames
        if str(filename).strip()
    )
    if not script_tags:
        return document_html
    if re.search(r"<head\b[^>]*>", document_html, re.IGNORECASE):
        return re.sub(
            r"(<head\b[^>]*>)",
            r"\1\n" + script_tags + "\n",
            document_html,
            count=1,
            flags=re.IGNORECASE,
        )
    if re.search(r"<body\b[^>]*>", document_html, re.IGNORECASE):
        return re.sub(
            r"(<body\b[^>]*>)",
            r"\1\n" + script_tags + "\n",
            document_html,
            count=1,
            flags=re.IGNORECASE,
        )
    return script_tags + "\n" + document_html


def generate_html_launcher_index_html(
    title: str,
    embed_filename: str = "",
    alternate_embed_filename: str = "",
    alternate_embed_label: str = "",
    alternate_embed_prompt: str = "",
    remote_url: str = "",
    play_note: str = "Saves to local storage",
    launch_here_label: str = "LAUNCH HERE",
    launch_fullscreen_label: str = "LAUNCH FULLSCREEN",
    initial_status: str = "Awaiting launch-mode selection",
    allowed_launch_modes: str = "both",
    recommended_launch_mode: str = "none",
    launcher_cache_buster: str = "",
    embed_cache_buster: str = "",
) -> str:
    allowed_launch_modes, recommended_launch_mode = normalize_launch_preferences(
        allowed_launch_modes,
        recommended_launch_mode,
    )
    embed_cache_suffix = f"?v={embed_cache_buster}" if embed_cache_buster else ""
    embed_url_js = json.dumps(
        f"./{embed_filename}{embed_cache_suffix}" if embed_filename else "",
        ensure_ascii=False,
    )
    alternate_embed_url_js = json.dumps(
        f"./{alternate_embed_filename}{embed_cache_suffix}" if alternate_embed_filename else "",
        ensure_ascii=False,
    )
    alternate_embed_label_js = json.dumps(alternate_embed_label or "", ensure_ascii=False)
    alternate_embed_prompt_js = json.dumps(alternate_embed_prompt or "", ensure_ascii=False)
    embed_title_js = json.dumps(title or "Game", ensure_ascii=False)
    remote_url_js = json.dumps(remote_url or "", ensure_ascii=False)
    play_note_js = json.dumps(play_note or "", ensure_ascii=False)
    launch_here_label_js = json.dumps(launch_here_label or "LAUNCH HERE", ensure_ascii=False)
    launch_fullscreen_label_js = json.dumps(
        launch_fullscreen_label or "LAUNCH FULLSCREEN",
        ensure_ascii=False,
    )
    initial_status_js = json.dumps(initial_status or "Awaiting launch-mode selection", ensure_ascii=False)
    allowed_launch_modes_js = json.dumps(allowed_launch_modes, ensure_ascii=False)
    recommended_launch_mode_js = json.dumps(recommended_launch_mode, ensure_ascii=False)
    launcher_cache_suffix = f"?v={launcher_cache_buster}" if launcher_cache_buster else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, minimum-scale=1.0, maximum-scale=1.0" />
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate, max-age=0" />
<meta http-equiv="Pragma" content="no-cache" />
<meta http-equiv="Expires" content="0" />
<title>{html.escape(title)}</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 16 16%22%3E%3Crect width=%2216%22 height=%2216%22 rx=%224%22 fill=%22%2305070f%22/%3E%3Ccircle cx=%228%22 cy=%228%22 r=%223.5%22 fill=%22%2322d3ee%22/%3E%3C/svg%3E" />
<link rel="stylesheet" href="./ocean-launcher.css{launcher_cache_suffix}" />
</head>
<body>
<div id="game_frame"></div>
<div id="loadingScreen">
<div id="loadingBackdrop" aria-hidden="true">
<canvas id="star-canvas"></canvas>
<canvas id="wave-canvas"></canvas>
<div class="nebula"></div>
<div class="overlay"></div>
<div class="grain"></div>
</div>
<div id="loadingCenter">
<div id="loadingTitleGroup">
<h1 id="loadingTitle">Ocean</h1>
<div id="loadingSubtitle">LAUNCHER</div>
</div>
<div id="launchPanel">
<div id="launchMenu">
<button id="launchFrameBtn" class="launchOption" type="button">LAUNCH HERE</button>
<button id="launchFullscreenBtn" class="launchOption" type="button">LAUNCH FULLSCREEN</button>
</div>
<div id="playNote">Saves to local storage</div>
</div>
<div id="progressTrack" aria-hidden="true">
<div id="progressFill"></div>
</div>
<div id="status">Awaiting launch-mode selection</div>
</div>
</div>
<script>
window.OCEAN_EMBED_URL = {embed_url_js};
window.OCEAN_ALT_EMBED_URL = {alternate_embed_url_js};
window.OCEAN_ALT_EMBED_LABEL = {alternate_embed_label_js};
window.OCEAN_ALT_EMBED_PROMPT = {alternate_embed_prompt_js};
window.OCEAN_EMBED_TITLE = {embed_title_js};
window.OCEAN_REMOTE_URL = {remote_url_js};
window.OCEAN_PLAY_NOTE = {play_note_js};
window.OCEAN_LAUNCH_FRAME_LABEL = {launch_here_label_js};
window.OCEAN_LAUNCH_FULLSCREEN_LABEL = {launch_fullscreen_label_js};
window.OCEAN_INITIAL_STATUS = {initial_status_js};
window.OCEAN_ALLOWED_LAUNCH_MODES = {allowed_launch_modes_js};
window.OCEAN_RECOMMENDED_LAUNCH_MODE = {recommended_launch_mode_js};
</script>
<script src="./ocean-launcher.js{launcher_cache_suffix}"></script>
</body>
</html>
"""


def export_custom_split_unity_entry(
    output_dir: Path,
    progress_file: Path,
    detected_entry: DetectedEntry,
    input_url: str,
    root_url: str,
    custom_bootstrap: Mapping[str, Any],
    allowed_launch_modes: str = "both",
    recommended_launch_mode: str = "none",
) -> dict[str, Any]:
    allowed_launch_modes, recommended_launch_mode = normalize_launch_preferences(
        allowed_launch_modes,
        recommended_launch_mode,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    build_dir = output_dir / "Build"
    build_dir.mkdir(parents=True, exist_ok=True)

    title = infer_display_title(
        extract_html_title(detected_entry.index_html),
        root_url,
        source_page_url=detected_entry.source_page_url or input_url,
    )
    source_page_url = canonicalize_source_page_url(
        detected_entry.source_page_url or input_url,
        str(custom_bootstrap.get("build_root_url") or ""),
    )
    unity_support_script_urls = collect_unity_support_script_urls(
        detected_entry.index_html,
        detected_entry.index_url,
        str(custom_bootstrap["loader_url"]),
    )

    progress_payload = load_json_file(progress_file)
    progress_payload.update(
        {
            "mode": "entry_auto",
            "entry_kind": "unity",
            "build_kind": "custom_split_modern",
            "root_url": root_url,
            "input_url": input_url,
            "resolved_entry_url": detected_entry.index_url,
            "title": title,
            "loader_url": custom_bootstrap["loader_url"],
            "framework_url": custom_bootstrap["framework_url"],
            "wasm_url": custom_bootstrap["wasm_url"],
            "data_part_urls": list(custom_bootstrap["data_part_urls"]),
            "completed": False,
        }
    )
    save_json_file(progress_file, progress_payload)

    loader_name = sanitize_filename(
        basename_from_url(str(custom_bootstrap["loader_url"])),
        "loader.js",
    )
    framework_name = sanitize_filename(
        basename_from_url(str(custom_bootstrap["framework_url"])),
        "framework.js",
    )
    data_name = infer_custom_split_data_name(
        [str(url) for url in custom_bootstrap["data_part_urls"]]
    )
    wasm_name = infer_custom_split_wasm_name(str(custom_bootstrap["wasm_url"]))

    loader_resolved_url = download_raw_asset(
        str(custom_bootstrap["loader_url"]),
        build_dir / loader_name,
        referer_url=detected_entry.index_url,
    )
    framework_resolved_url = download_raw_asset(
        str(custom_bootstrap["framework_url"]),
        build_dir / framework_name,
        referer_url=detected_entry.index_url,
    )

    if brotli is None:
        raise FetchError("brotli support is required to export this split Unity page.")

    data_part_urls = [str(url) for url in custom_bootstrap["data_part_urls"]]
    combined_data_parts = bytearray()
    for index, part_url in enumerate(data_part_urls, start=1):
        resolved_part_url, raw_part, _, _ = fetch_url(part_url, referer_url=detected_entry.index_url)
        if not raw_part:
            raise FetchError(f"{part_url} -> empty response")
        if looks_like_html(raw_part):
            raise FetchError(f"{part_url} -> returned HTML instead of split data")
        combined_data_parts.extend(raw_part)
        log(
            f"Custom split Unity data parts: {index}/{len(data_part_urls)} -> {basename_from_url(resolved_part_url)}"
        )

    try:
        data_bytes = brotli.decompress(bytes(combined_data_parts))
    except Exception as exc:
        raise FetchError("Failed to decompress combined split Unity data asset.") from exc
    (build_dir / data_name).write_bytes(data_bytes)

    wasm_resolved_url, wasm_raw, _, _ = fetch_url(
        str(custom_bootstrap["wasm_url"]),
        referer_url=detected_entry.index_url,
    )
    if not wasm_raw:
        raise FetchError(f"{custom_bootstrap['wasm_url']} -> empty response")
    if looks_like_html(wasm_raw):
        raise FetchError(f"{custom_bootstrap['wasm_url']} -> returned HTML instead of wasm")
    try:
        wasm_bytes = brotli.decompress(wasm_raw)
    except Exception as exc:
        raise FetchError("Failed to decompress split Unity wasm asset.") from exc
    (build_dir / wasm_name).write_bytes(wasm_bytes)

    assets = DownloadedAssets(
        loader_name=loader_name,
        framework_name=framework_name,
        data_name=data_name,
        wasm_name=wasm_name,
        used_br_assets=False,
        build_kind="modern",
    )
    downloaded_support_scripts = download_unity_support_scripts(
        output_dir,
        unity_support_script_urls,
        referer_url=detected_entry.index_url,
    )

    site_lock_framework_patched = False
    gmsoft_host_bridge_patched = patch_gmsoft_host_bridge(build_dir / assets.framework_name)
    gmsoft_sendmessage_defaults_patched = patch_gmsoft_sendmessage_defaults(
        build_dir / assets.framework_name
    )
    sendmessage_value_compat_patched = patch_sendmessage_value_compat(
        build_dir / assets.framework_name
    )
    framework_analysis = analyze_framework(build_dir / assets.framework_name)
    required_functions = framework_analysis.required_functions
    original_folder_url = str(custom_bootstrap.get("build_root_url") or "")
    streaming_assets_url = str(custom_bootstrap.get("streaming_assets_url") or "")
    page_config: dict[str, Any] = {}
    auxiliary_asset_rewrites = collect_auxiliary_asset_rewrites(
        output_dir,
        source_page_url,
        original_folder_url,
        (
            build_dir / assets.framework_name,
            build_dir / assets.data_name,
        ),
    )
    gmsoft_like_build = looks_like_gmsoft_page_config(page_config)
    source_url_spoof_patterns = [
        b"SiteLock",
        b"whitelistedDomains",
        b"allowedRemoteHosts",
        b"IsOnWhitelistedDomain",
        b"DomainLocker",
        b"check_domains_str",
        b"redirect_domain",
        b"ALLOW_DOMAINS",
    ]
    enable_source_url_spoof = any(
        file_contains_any_bytes(path, source_url_spoof_patterns)
        for path in (
            build_dir / assets.framework_name,
            build_dir / assets.data_name,
        )
        if path.name
    )
    asset_cache_buster = compute_asset_cache_buster(build_dir, assets)
    product_name = title
    index_content = generate_index_html(
        product_name,
        assets,
        required_functions,
        framework_analysis.window_roots,
        framework_analysis.window_callable_chains,
        support_script_filenames=[item["name"] for item in downloaded_support_scripts],
        source_page_url=source_page_url,
        enable_source_url_spoof=enable_source_url_spoof,
        original_folder_url=original_folder_url,
        streaming_assets_url=streaming_assets_url,
        asset_cache_buster=asset_cache_buster,
        page_config=page_config,
        auxiliary_asset_rewrites=auxiliary_asset_rewrites,
        allowed_launch_modes=allowed_launch_modes,
        recommended_launch_mode=recommended_launch_mode,
    )
    validate_required_function_coverage(index_content, required_functions)
    (output_dir / "index.html").write_text(index_content, encoding="utf-8")
    write_vendor_support_files(output_dir, framework_analysis)
    (output_dir / "required-functions.json").write_text(
        json.dumps(
            {
                "count": len(required_functions),
                "functions": required_functions,
                "window_root_count": len(framework_analysis.window_roots),
                "window_roots": framework_analysis.window_roots,
                "window_callable_chain_count": len(framework_analysis.window_callable_chains),
                "window_callable_chains": framework_analysis.window_callable_chains,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    summary = {
        "output_dir": str(output_dir),
        "index_html": str(output_dir / "index.html"),
        "required_functions_file": str(output_dir / "required-functions.json"),
        "loader": assets.loader_name,
        "framework": assets.framework_name,
        "data": assets.data_name,
        "wasm": assets.wasm_name,
        "required_function_count": len(required_functions),
        "window_root_count": len(framework_analysis.window_roots),
        "window_callable_chain_count": len(framework_analysis.window_callable_chains),
        "used_br_assets": False,
        "used_compressed_assets": False,
        "site_lock_framework_patched": site_lock_framework_patched,
        "gmsoft_host_bridge_patched": gmsoft_host_bridge_patched,
        "gmsoft_sendmessage_defaults_patched": gmsoft_sendmessage_defaults_patched,
        "sendmessage_value_compat_patched": sendmessage_value_compat_patched,
        "build_kind": "custom_split_modern",
        "mode": "entry_auto",
        "entry_kind": "unity",
        "title": title,
        "source_page_url": source_page_url,
        "source_url_spoof_enabled": enable_source_url_spoof,
        "original_folder_url": original_folder_url,
        "streaming_assets_url": streaming_assets_url,
        "asset_cache_buster": asset_cache_buster,
        "launch_options": allowed_launch_modes,
        "recommended_launch_mode": recommended_launch_mode,
        "gmsoft_like_build": gmsoft_like_build,
        "page_config_keys": sorted(page_config.keys()),
        "auxiliary_asset_rewrites": auxiliary_asset_rewrites,
        "support_script_files": [item["name"] for item in downloaded_support_scripts],
        "support_script_urls": [item["resolved_url"] for item in downloaded_support_scripts],
        "loader_url": loader_resolved_url,
        "framework_url": framework_resolved_url,
        "data_part_urls": data_part_urls,
        "wasm_url": wasm_resolved_url,
        "progress_file": str(progress_file),
    }
    (output_dir / "standalone-build-info.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    progress_payload = load_json_file(progress_file)
    progress_payload["completed"] = True
    progress_payload["summary"] = summary
    save_json_file(progress_file, progress_payload)

    return summary


def export_html_entry(
    output_dir: Path,
    progress_file: Path,
    detected_entry: DetectedEntry,
    input_url: str,
    root_url: str,
    allowed_launch_modes: str = "both",
    recommended_launch_mode: str = "none",
) -> dict[str, Any]:
    allowed_launch_modes, recommended_launch_mode = normalize_launch_preferences(
        allowed_launch_modes,
        recommended_launch_mode,
    )
    custom_split_bootstrap = extract_custom_split_unity_bootstrap(
        detected_entry.index_html,
        detected_entry.index_url,
    )
    if custom_split_bootstrap:
        return export_custom_split_unity_entry(
            output_dir,
            progress_file,
            detected_entry,
            input_url,
            root_url,
            custom_split_bootstrap,
            allowed_launch_modes=allowed_launch_modes,
            recommended_launch_mode=recommended_launch_mode,
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    title = infer_display_title(
        extract_html_title(detected_entry.index_html),
        root_url,
        source_page_url=detected_entry.source_page_url or input_url,
    )
    support_files = copy_eagler_support_files(output_dir)
    progress_payload = load_json_file(progress_file)
    progress_payload.update(
        {
            "mode": "entry_auto",
            "entry_kind": "html",
            "root_url": root_url,
            "input_url": input_url,
            "resolved_entry_url": detected_entry.index_url,
            "title": title,
            "completed": False,
        }
    )
    save_json_file(progress_file, progress_payload)

    normalized_source_html = absolutize_markup_urls(
        detected_entry.index_html,
        detected_entry.index_url,
    )
    sanitized_source_html, ad_removal_counts = strip_known_embedded_ad_markup(normalized_source_html)
    original_external_links = extract_html_external_links(sanitized_source_html)
    mirrored_html_summary: dict[str, Any] = {}
    localized_source_html = sanitized_source_html
    if looks_like_construct2_entry_html(sanitized_source_html):
        localized_source_html, mirrored_html_summary = mirror_construct2_entry_assets(
            sanitized_source_html,
            detected_entry.index_url,
            output_dir,
        )
    localized_source_html, nonessential_markup_removed = strip_nonessential_html_markup(
        localized_source_html
    )
    localized_source_html, eagler_wrapper_patches = patch_inline_eagler_wrapper_html(
        localized_source_html
    )
    eagler_mobile_option_enabled = looks_like_eagler_entry_html(localized_source_html)
    external_links = extract_html_external_links(localized_source_html)
    embedded_entry_name = "game-root.html"
    embedded_entry_content = generate_html_entry_index_html(
        title=title,
        source_html=localized_source_html,
    )
    (output_dir / embedded_entry_name).write_text(embedded_entry_content, encoding="utf-8")
    eagler_mobile_script: dict[str, str] | None = None
    mobile_embedded_entry_name = ""
    if eagler_mobile_option_enabled:
        eagler_mobile_script = download_eagler_mobile_script(output_dir)
        mobile_embedded_entry_name = "game-root-mobile.html"
        mobile_embedded_entry_content = inject_head_script_tags(
            embedded_entry_content,
            [eagler_mobile_script["name"]],
        )
        (output_dir / mobile_embedded_entry_name).write_text(
            mobile_embedded_entry_content,
            encoding="utf-8",
        )
    embed_cache_buster = compute_output_file_cache_buster(
        output_dir,
        [embedded_entry_name, mobile_embedded_entry_name],
    )

    index_content = generate_html_launcher_index_html(
        title=title,
        embed_filename=embedded_entry_name,
        alternate_embed_filename=mobile_embedded_entry_name,
        alternate_embed_label="Mobile controls",
        alternate_embed_prompt=(
            "Use the Eagler Mobile controls version for this build?"
        )
        if eagler_mobile_option_enabled
        else "",
        allowed_launch_modes=allowed_launch_modes,
        recommended_launch_mode=recommended_launch_mode,
        launcher_cache_buster=compute_launcher_support_cache_buster(output_dir),
        embed_cache_buster=embed_cache_buster,
    )
    (output_dir / "index.html").write_text(index_content, encoding="utf-8")

    required_functions_payload = {
        "count": 0,
        "functions": [],
        "window_root_count": 0,
        "window_roots": [],
        "window_callable_chain_count": 0,
        "window_callable_chains": [],
    }
    (output_dir / "required-functions.json").write_text(
        json.dumps(required_functions_payload, indent=2),
        encoding="utf-8",
    )

    summary = {
        "output_dir": str(output_dir),
        "index_html": str(output_dir / "index.html"),
        "embedded_entry_html": str(output_dir / embedded_entry_name),
        "mobile_embedded_entry_html": str(output_dir / mobile_embedded_entry_name)
        if mobile_embedded_entry_name
        else "",
        "required_functions_file": str(output_dir / "required-functions.json"),
        "mode": "entry_auto",
        "entry_kind": "html",
        "title": title,
        "input_url": input_url,
        "root_url": root_url,
        "resolved_entry_url": detected_entry.index_url,
        "html_source_mode": "absolutized_embed_root",
        "launcher": "ocean-launcher",
        "embed_cache_buster": embed_cache_buster,
        "support_files": support_files,
        "embedded_ad_blocks_removed": ad_removal_counts,
        "nonessential_markup_removed": nonessential_markup_removed,
        "html_runtime_mirror": mirrored_html_summary,
        "eagler_wrapper_patches": eagler_wrapper_patches,
        "eagler_mobile_option_enabled": eagler_mobile_option_enabled,
        "eagler_mobile_script_file": eagler_mobile_script["name"] if eagler_mobile_script else "",
        "eagler_mobile_script_url": (
            eagler_mobile_script["resolved_url"] if eagler_mobile_script else ""
        ),
        "launch_options": allowed_launch_modes,
        "recommended_launch_mode": recommended_launch_mode,
        "source_external_script_urls": original_external_links["scripts"],
        "source_external_stylesheet_urls": original_external_links["stylesheets"],
        "source_external_frame_urls": original_external_links["frames"],
        "source_external_other_urls": original_external_links["other_links"],
        "external_script_urls": external_links["scripts"],
        "external_stylesheet_urls": external_links["stylesheets"],
        "external_frame_urls": external_links["frames"],
        "external_other_urls": external_links["other_links"],
        "progress_file": str(progress_file),
    }
    (output_dir / "standalone-build-info.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    progress_payload = load_json_file(progress_file)
    progress_payload["completed"] = True
    progress_payload["summary"] = summary
    save_json_file(progress_file, progress_payload)

    return summary


def export_remote_stream_entry(
    output_dir: Path,
    progress_file: Path,
    detected_entry: DetectedEntry,
    input_url: str,
    root_url: str,
    allowed_launch_modes: str = "both",
    recommended_launch_mode: str = "none",
) -> dict[str, Any]:
    allowed_launch_modes, recommended_launch_mode = normalize_launch_preferences(
        allowed_launch_modes,
        recommended_launch_mode,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    title = infer_display_title(
        str(detected_entry.metadata.get("app_name") or extract_html_title(detected_entry.index_html)),
        root_url,
        source_page_url=detected_entry.source_page_url or input_url,
    )
    remote_url = str(detected_entry.metadata.get("remote_url") or detected_entry.index_url).strip()
    if not remote_url:
        raise FetchError("Remote stream entry did not provide a launch URL.")

    support_files = copy_eagler_support_files(output_dir)
    progress_payload = load_json_file(progress_file)
    progress_payload.update(
        {
            "mode": "entry_auto",
            "entry_kind": "remote_stream",
            "root_url": root_url,
            "input_url": input_url,
            "resolved_entry_url": detected_entry.index_url,
            "remote_url": remote_url,
            "title": title,
            "completed": False,
        }
    )
    save_json_file(progress_file, progress_payload)

    index_content = generate_html_launcher_index_html(
        title=title,
        remote_url=remote_url,
        play_note="Cloud-streamed on now.gg",
        launch_here_label="OPEN NOW.GG",
        launch_fullscreen_label="OPEN IN NEW TAB",
        initial_status="Remote stream detected; choose handoff mode",
        allowed_launch_modes=allowed_launch_modes,
        recommended_launch_mode=recommended_launch_mode,
        launcher_cache_buster=compute_launcher_support_cache_buster(output_dir),
    )
    (output_dir / "index.html").write_text(index_content, encoding="utf-8")

    required_functions_payload = {
        "count": 0,
        "functions": [],
        "window_root_count": 0,
        "window_roots": [],
        "window_callable_chain_count": 0,
        "window_callable_chains": [],
    }
    (output_dir / "required-functions.json").write_text(
        json.dumps(required_functions_payload, indent=2),
        encoding="utf-8",
    )

    summary = {
        "output_dir": str(output_dir),
        "index_html": str(output_dir / "index.html"),
        "required_functions_file": str(output_dir / "required-functions.json"),
        "mode": "entry_auto",
        "entry_kind": "remote_stream",
        "title": title,
        "input_url": input_url,
        "root_url": root_url,
        "resolved_entry_url": detected_entry.index_url,
        "remote_url": remote_url,
        "remote_provider": detected_entry.metadata.get("remote_provider", ""),
        "remote_kind": detected_entry.metadata.get("remote_kind", ""),
        "remote_stream_reason": detected_entry.metadata.get("remote_stream_reason", ""),
        "launcher": "ocean-launcher",
        "asset_strategy": "not_mirrored_remote_stream",
        "launch_options": allowed_launch_modes,
        "recommended_launch_mode": recommended_launch_mode,
        "support_files": support_files,
        "metadata": detected_entry.metadata,
        "progress_file": str(progress_file),
    }
    (output_dir / "standalone-build-info.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    progress_payload = load_json_file(progress_file)
    progress_payload["completed"] = True
    progress_payload["summary"] = summary
    save_json_file(progress_file, progress_payload)

    return summary


def generate_eagler_runtime_html(
    title: str,
    bootstrap_script: str,
    script_filenames: Sequence[str],
    assets_filename: str,
    locales_url: str,
    mobile_script_filename: str = "",
) -> str:
    bootstrap_script_js = json.dumps(bootstrap_script, ensure_ascii=False)
    assets_path_js = json.dumps(f"./{assets_filename}", ensure_ascii=False)
    locales_url_js = json.dumps(locales_url, ensure_ascii=False)
    modapi_bridge_script = """<script type="text/javascript">
(function (root) {
  root = root || (typeof globalThis !== "undefined" ? globalThis : null);
  if (!root || root.__oceanEaglerModApiBridgeInstalled) {
    return;
  }
  root.__oceanEaglerModApiBridgeInstalled = true;
  if (typeof root.ModAPI !== "object" || !root.ModAPI) {
    root.ModAPI = {};
  }
  if (typeof root.initAPI !== "function") {
    root.initAPI = function () {
      if (typeof root.ModAPI !== "object" || !root.ModAPI) {
        root.ModAPI = {};
      }
      return root.ModAPI;
    };
  }
})(typeof window !== "undefined" ? window : (typeof globalThis !== "undefined" ? globalThis : null));
</script>"""
    mobile_script_tag = (
        f'<script type="text/javascript" src="./{html.escape(mobile_script_filename)}"></script>\n'
        if mobile_script_filename
        else ""
    )
    entry_script_tags = "\n".join(
        f'<script type="text/javascript" src="./{html.escape(filename)}"></script>'
        for filename in script_filenames
    )

    locales_override = (
        f"  window.eaglercraftXOpts.localesURI = {locales_url_js};\n" if locales_url else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, minimum-scale=1.0, maximum-scale=1.0" />
<title>{html.escape(title)}</title>
<style>
html, body {{
  margin: 0;
  width: 100%;
  height: 100%;
  overflow: hidden;
  background: #000;
}}
#game_frame {{
  width: 100%;
  height: 100%;
}}
</style>
</head>
<body>
<div id="game_frame"></div>
{modapi_bridge_script}
{mobile_script_tag}{entry_script_tags}
<script type="text/javascript">
"use strict";
(function () {{
  var bootstrapScript = {bootstrap_script_js};
  var originalWindowAddEventListener = window.addEventListener;
  var originalDocumentAddEventListener = document.addEventListener;
  var originalMain = window.main;

  function createImmediateEvent(type, target) {{
    return {{
      type: type,
      target: target,
      currentTarget: target,
      preventDefault: function () {{}},
      stopPropagation: function () {{}}
    }};
  }}

  function fireImmediately(type, listener, target) {{
    if (typeof listener === "function") {{
      listener.call(target, createImmediateEvent(type, target));
      return;
    }}
    if (listener && typeof listener.handleEvent === "function") {{
      listener.handleEvent(createImmediateEvent(type, target));
    }}
  }}

  window.addEventListener = function (type, listener, options) {{
    if (type === "load") {{
      fireImmediately(type, listener, window);
      return;
    }}
    return originalWindowAddEventListener.call(this, type, listener, options);
  }};

  if (typeof originalDocumentAddEventListener === "function") {{
    document.addEventListener = function (type, listener, options) {{
      if (type === "DOMContentLoaded" || type === "load") {{
        fireImmediately(type, listener, document);
        return;
      }}
      return originalDocumentAddEventListener.call(this, type, listener, options);
    }};
  }}

  window.main = function () {{}};

  try {{
    (0, eval)(bootstrapScript);
  }} finally {{
    window.addEventListener = originalWindowAddEventListener;
    if (typeof originalDocumentAddEventListener === "function") {{
      document.addEventListener = originalDocumentAddEventListener;
    }}
    window.main = originalMain;
  }}

  if (typeof window.eaglercraftXOpts !== "object" || !window.eaglercraftXOpts) {{
    window.eaglercraftXOpts = {{}};
  }}

  window.eaglercraftXOpts.container = "game_frame";
  window.eaglercraftXOpts.assetsURI = {assets_path_js};
{locales_override}  if (!window.__oceanEaglerMainStarted && typeof window.main === "function") {{
    window.__oceanEaglerMainStarted = true;
    window.main();
  }}
}})();
</script>
</body>
</html>
"""


def export_eagler_entry(
    output_dir: Path,
    progress_file: Path,
    detected_entry: DetectedEaglerEntry,
    input_url: str,
    root_url: str,
    allowed_launch_modes: str = "both",
    recommended_launch_mode: str = "none",
) -> dict[str, Any]:
    allowed_launch_modes, recommended_launch_mode = normalize_launch_preferences(
        allowed_launch_modes,
        recommended_launch_mode,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    used_entry_script_names: set[str] = set()
    entry_script_files: list[dict[str, str]] = []
    for index, script_url in enumerate(detected_entry.script_urls, start=1):
        fallback_name = "classes.js" if index == 1 else f"support-{index}.js"
        script_name = sanitize_filename(basename_from_url(script_url), fallback_name)
        if script_name.lower() in used_entry_script_names:
            stem, dot, suffix = script_name.rpartition(".")
            stem = stem or script_name
            dot = "." if dot else ""
            counter = 2
            while True:
                candidate = f"{stem}-{counter}{dot}{suffix}"
                if candidate.lower() not in used_entry_script_names:
                    script_name = candidate
                    break
                counter += 1
        used_entry_script_names.add(script_name.lower())
        entry_script_files.append({"url": script_url, "name": script_name})

    assets_name = sanitize_filename(
        basename_from_url(detected_entry.assets_url) if not detected_entry.assets_url.startswith("data:") else "assets.epk",
        "assets.epk",
    )

    support_files = copy_eagler_support_files(output_dir)

    progress_payload = load_json_file(progress_file)
    progress_payload.update(
        {
            "mode": "entry_auto",
            "entry_kind": "eaglercraft",
            "root_url": root_url,
            "input_url": input_url,
            "resolved_entry_url": detected_entry.index_url,
            "title": detected_entry.title,
            "classes_url": detected_entry.classes_url,
            "script_urls": detected_entry.script_urls,
            "assets_url": detected_entry.assets_url,
            "locales_url": detected_entry.locales_url,
            "completed": False,
        }
    )
    save_json_file(progress_file, progress_payload)

    downloaded_entry_scripts: list[dict[str, str]] = []
    skipped_entry_scripts: list[str] = []
    for script_index, script_file in enumerate(entry_script_files):
        try:
            resolved_script_url = download_raw_asset(
                script_file["url"],
                output_dir / script_file["name"],
                referer_url=detected_entry.index_url,
            )
        except FetchError:
            if script_index == 0:
                raise
            skipped_entry_scripts.append(script_file["url"])
            log(f"Skipping optional Eagler support script: {script_file['url']}")
            continue
        downloaded_entry_scripts.append(
            {
                "url": script_file["url"],
                "name": script_file["name"],
                "resolved_url": resolved_script_url,
            }
        )

    if not downloaded_entry_scripts:
        raise FetchError("Failed to download the Eagler runtime bundle.")

    classes_name = downloaded_entry_scripts[0]["name"]
    assets_resolved_url = download_raw_asset(
        detected_entry.assets_url,
        output_dir / assets_name,
        referer_url=detected_entry.index_url,
    )
    eagler_mobile_script = download_eagler_mobile_script(output_dir)
    support_files.append(eagler_mobile_script["name"])
    embedded_entry_name = "game-root.html"
    mobile_embedded_entry_name = "game-root-mobile.html"
    embedded_entry_content = generate_eagler_runtime_html(
        title=detected_entry.title,
        bootstrap_script=detected_entry.bootstrap_script,
        script_filenames=[item["name"] for item in downloaded_entry_scripts],
        assets_filename=assets_name,
        locales_url=detected_entry.locales_url,
    )
    (output_dir / embedded_entry_name).write_text(embedded_entry_content, encoding="utf-8")
    mobile_embedded_entry_content = generate_eagler_runtime_html(
        title=detected_entry.title,
        bootstrap_script=detected_entry.bootstrap_script,
        script_filenames=[item["name"] for item in downloaded_entry_scripts],
        assets_filename=assets_name,
        locales_url=detected_entry.locales_url,
        mobile_script_filename=eagler_mobile_script["name"],
    )
    (output_dir / mobile_embedded_entry_name).write_text(
        mobile_embedded_entry_content,
        encoding="utf-8",
    )
    embed_cache_buster = compute_output_file_cache_buster(
        output_dir,
        [embedded_entry_name, mobile_embedded_entry_name],
    )
    index_content = generate_html_launcher_index_html(
        title=detected_entry.title,
        embed_filename=embedded_entry_name,
        alternate_embed_filename=mobile_embedded_entry_name,
        alternate_embed_label="Mobile controls",
        alternate_embed_prompt=(
            "Use the Eagler Mobile controls version for this build?"
        ),
        allowed_launch_modes=allowed_launch_modes,
        recommended_launch_mode=recommended_launch_mode,
        launcher_cache_buster=compute_launcher_support_cache_buster(output_dir),
        embed_cache_buster=embed_cache_buster,
    )
    (output_dir / "index.html").write_text(index_content, encoding="utf-8")

    required_functions_payload = {
        "count": 0,
        "functions": [],
        "window_root_count": 0,
        "window_roots": [],
        "window_callable_chain_count": 0,
        "window_callable_chains": [],
    }
    (output_dir / "required-functions.json").write_text(
        json.dumps(required_functions_payload, indent=2),
        encoding="utf-8",
    )

    summary = {
        "output_dir": str(output_dir),
        "index_html": str(output_dir / "index.html"),
        "embedded_entry_html": str(output_dir / embedded_entry_name),
        "mobile_embedded_entry_html": str(output_dir / mobile_embedded_entry_name),
        "required_functions_file": str(output_dir / "required-functions.json"),
        "mode": "entry_auto",
        "entry_kind": "eaglercraft",
        "title": detected_entry.title,
        "input_url": input_url,
        "root_url": root_url,
        "resolved_entry_url": detected_entry.index_url,
        "classes_file": classes_name,
        "classes_url": downloaded_entry_scripts[0]["resolved_url"],
        "entry_script_files": [item["name"] for item in downloaded_entry_scripts],
        "entry_script_urls": [item["resolved_url"] for item in downloaded_entry_scripts],
        "skipped_entry_script_urls": skipped_entry_scripts,
        "assets_file": assets_name,
        "assets_url": assets_resolved_url,
        "locales_url": detected_entry.locales_url,
        "eagler_mobile_option_enabled": True,
        "eagler_mobile_script_file": eagler_mobile_script["name"],
        "eagler_mobile_script_url": eagler_mobile_script["resolved_url"],
        "embed_cache_buster": embed_cache_buster,
        "launch_options": allowed_launch_modes,
        "recommended_launch_mode": recommended_launch_mode,
        "support_files": support_files,
        "progress_file": str(progress_file),
    }
    (output_dir / "standalone-build-info.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    progress_payload = load_json_file(progress_file)
    progress_payload["completed"] = True
    progress_payload["summary"] = summary
    save_json_file(progress_file, progress_payload)

    return summary


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    allowed_launch_modes, recommended_launch_mode = normalize_launch_preferences(
        args.launch_options,
        args.recommended_launch,
    )

    direct_values = [
        args.loader_url.strip(),
        args.framework_url.strip(),
        args.data_url.strip(),
        args.wasm_url.strip(),
    ]
    direct_mode = any(direct_values)
    if direct_mode and not all(direct_values):
        raise FetchError(
            "If you provide direct URLs, provide all of them: "
            "--loader-url, --framework-url, --data-url, --wasm-url."
        )
    if not direct_mode and not args.entry_url:
        raise FetchError(
            "Provide either an entry URL or all direct URLs "
            "(--loader-url --framework-url --data-url --wasm-url)."
        )

    input_url = ""
    entry_kind = "unity"
    build_kind = "modern"
    legacy_config: dict[str, Any] = {}
    detected_build: DetectedBuild | None = None
    detected_eagler_entry: DetectedEaglerEntry | None = None
    detected_html_entry: DetectedEntry | None = None
    detected_remote_entry: DetectedEntry | None = None

    if direct_mode:
        loader_url = normalize_url(args.loader_url)
        framework_url = normalize_url(args.framework_url)
        data_url = normalize_url(args.data_url)
        wasm_url = normalize_url(args.wasm_url)
        root_url = derive_game_root_url(loader_url)
        log("Mode: direct asset URLs")
        log(f"Loader URL: {loader_url}")
    else:
        input_url = normalize_url(args.entry_url)
        root_url = derive_game_root_url(input_url)

        log("Mode: entry URL auto-detect")
        log(f"Input URL: {input_url}")
        log(f"Game root URL: {root_url}")

        detected_entry = find_supported_entry(input_url, root_url)
        entry_kind = detected_entry.entry_kind
        log(f"Resolved entry URL: {detected_entry.index_url}")
        log(f"Detected entry kind: {entry_kind}")

        if entry_kind == "unity":
            if looks_like_split_unity_bootstrap_page(detected_entry.index_html):
                entry_kind = "html"
                detected_html_entry = DetectedEntry(
                    entry_kind="html",
                    index_url=detected_entry.index_url,
                    index_html=detected_entry.index_html,
                    source_page_url=detected_entry.source_page_url or detected_entry.index_url,
                )
                log("Falling back to HTML wrapper export for split-part Unity bootstrap page")
                log(f"Resolved HTML entry URL: {detected_html_entry.index_url}")
            else:
                detected_build = detect_entry_build(detected_entry.index_url, detected_entry.index_html)
                build_kind = detected_build.build_kind
                legacy_config = detected_build.legacy_config
                loader_url = detected_build.loader_url
                candidates = detected_build.candidates

                log(f"Detected build kind: {build_kind}")
                log(f"Resolved loader URL: {loader_url}")
        elif entry_kind == "eaglercraft":
            try:
                detected_eagler_entry = detect_eagler_entry(
                    detected_entry.index_url,
                    detected_entry.index_html,
                )
            except FetchError as exc:
                if looks_like_inline_eagler_payload_html(detected_entry.index_html):
                    entry_kind = "html"
                    detected_html_entry = DetectedEntry(
                        entry_kind="html",
                        index_url=detected_entry.index_url,
                        index_html=detected_entry.index_html,
                        source_page_url=detected_entry.source_page_url or detected_entry.index_url,
                    )
                    log(f"Falling back to HTML wrapper export for inline Eagler payload: {exc}")
                    log(f"Resolved HTML entry URL: {detected_html_entry.index_url}")
                else:
                    raise
            else:
                log(f"Resolved Eagler runtime URL: {detected_eagler_entry.classes_url}")
                log(f"Resolved Eagler assets URL: {detected_eagler_entry.assets_url}")
                if detected_eagler_entry.locales_url:
                    log(f"Resolved Eagler locales URL: {detected_eagler_entry.locales_url}")
        elif entry_kind == "remote_stream":
            detected_remote_entry = detected_entry
            log(f"Resolved remote stream URL: {detected_remote_entry.metadata.get('remote_url', detected_remote_entry.index_url)}")
            remote_provider = str(detected_remote_entry.metadata.get("remote_provider") or "").strip()
            if remote_provider:
                log(f"Detected remote provider: {remote_provider}")
        else:
            detected_html_entry = detected_entry
            log(f"Resolved HTML entry URL: {detected_html_entry.index_url}")

    if direct_mode:
        output_name = args.out_dir.strip() or infer_output_name_from_url(root_url, loader_url)
    elif entry_kind == "eaglercraft" and detected_eagler_entry is not None:
        output_name = args.out_dir.strip() or infer_output_name_from_entry(
            detected_eagler_entry.title,
            root_url,
            fallback_name="eaglercraft",
            source_page_url=input_url,
        )
    elif entry_kind == "html" and detected_html_entry is not None:
        output_name = args.out_dir.strip() or infer_output_name_from_entry(
            extract_html_title(detected_html_entry.index_html),
            root_url,
            fallback_name="html-game",
            source_page_url=detected_html_entry.source_page_url or input_url,
        )
    elif entry_kind == "remote_stream" and detected_remote_entry is not None:
        output_name = args.out_dir.strip() or infer_output_name_from_entry(
            str(detected_remote_entry.metadata.get("app_name") or extract_html_title(detected_remote_entry.index_html)),
            root_url,
            fallback_name="remote-stream",
            source_page_url=detected_remote_entry.source_page_url or input_url,
        )
    else:
        output_name = args.out_dir.strip() or infer_output_name_from_url(root_url, loader_url)
    output_dir = Path(output_name).resolve()
    build_dir = output_dir / "Build"
    progress_file = output_dir / ".standalone-progress.json"

    if direct_mode:
        build_kind, candidates, legacy_config = resolve_direct_build(
            loader_url=loader_url,
            framework_url=framework_url,
            data_url=data_url,
            wasm_url=wasm_url,
            progress_file=progress_file,
        )
        log(f"Detected build kind: {build_kind}")

    if output_dir.exists():
        if args.overwrite:
            shutil.rmtree(output_dir)
            log(f"Removed existing output directory: {output_dir}")
        else:
            log(f"Output directory exists, resuming if possible: {output_dir}")

    if entry_kind == "eaglercraft" and detected_eagler_entry is not None:
        summary = export_eagler_entry(
            output_dir=output_dir,
            progress_file=progress_file,
            detected_entry=detected_eagler_entry,
            input_url=input_url,
            root_url=root_url,
            allowed_launch_modes=allowed_launch_modes,
            recommended_launch_mode=recommended_launch_mode,
        )
        log("Done.")
        log(json.dumps(summary, indent=2))
        return 0

    if entry_kind == "html" and detected_html_entry is not None:
        summary = export_html_entry(
            output_dir=output_dir,
            progress_file=progress_file,
            detected_entry=detected_html_entry,
            input_url=input_url,
            root_url=root_url,
            allowed_launch_modes=allowed_launch_modes,
            recommended_launch_mode=recommended_launch_mode,
        )
        log("Done.")
        log(json.dumps(summary, indent=2))
        return 0

    if entry_kind == "remote_stream" and detected_remote_entry is not None:
        summary = export_remote_stream_entry(
            output_dir=output_dir,
            progress_file=progress_file,
            detected_entry=detected_remote_entry,
            input_url=input_url,
            root_url=root_url,
            allowed_launch_modes=allowed_launch_modes,
            recommended_launch_mode=recommended_launch_mode,
        )
        log("Done.")
        log(json.dumps(summary, indent=2))
        return 0

    build_dir.mkdir(parents=True, exist_ok=True)

    unity_support_script_urls: list[str] = []
    if not direct_mode and detected_build is not None:
        unity_support_script_urls = collect_unity_support_script_urls(
            detected_build.index_html,
            detected_build.index_url,
            loader_url,
        )

    progress_payload = load_json_file(progress_file)
    progress_payload.update(
        {
            "mode": "direct_urls" if direct_mode else "entry_auto",
            "entry_kind": "unity",
            "build_kind": build_kind,
            "root_url": root_url,
            "loader_url": loader_url,
            "launch_options": allowed_launch_modes,
            "recommended_launch_mode": recommended_launch_mode,
            "completed": False,
        }
    )
    if legacy_config:
        progress_payload["legacy_config"] = legacy_config
    save_json_file(progress_file, progress_payload)

    if build_kind == "legacy_json":
        assets = download_legacy_assets(
            build_dir,
            candidates,
            legacy_config,
            progress_file,
            referer_url=detected_build.index_url if detected_build is not None else "",
        )
    else:
        assets = download_assets(
            build_dir,
            candidates,
            progress_file,
            referer_url=detected_build.index_url if detected_build is not None else "",
        )

    downloaded_support_scripts = download_unity_support_scripts(
        output_dir,
        unity_support_script_urls,
        referer_url=detected_build.index_url if detected_build is not None else "",
    )

    patched_framework_path = (
        patch_redirect_domain_function(build_dir / assets.framework_name)
        if assets.framework_name
        else None
    )
    site_lock_framework_patched = patched_framework_path is not None
    if patched_framework_path is not None:
        assets.framework_name = patched_framework_path.name
        if assets.build_kind == "legacy_json":
            assets.legacy_asset_names["wasmFrameworkUrl"] = patched_framework_path.name
    gmsoft_host_bridge_patched = False
    gmsoft_sendmessage_defaults_patched = False
    sendmessage_value_compat_patched = False
    if assets.framework_name:
        framework_path = build_dir / assets.framework_name
        gmsoft_host_bridge_patched = patch_gmsoft_host_bridge(framework_path)
        gmsoft_sendmessage_defaults_patched = patch_gmsoft_sendmessage_defaults(framework_path)
        sendmessage_value_compat_patched = patch_sendmessage_value_compat(framework_path)
    analysis_target = (
        build_dir / assets.framework_name
        if assets.framework_name
        else build_dir / assets.loader_name
    )
    framework_analysis = (
        analyze_framework(analysis_target)
        if analysis_target.exists()
        else empty_framework_analysis()
    )
    required_functions = framework_analysis.required_functions
    original_folder_url = (
        detected_build.original_folder_url
        if (not direct_mode and detected_build is not None)
        else ""
    )
    streaming_assets_url = (
        detected_build.streaming_assets_url
        if (not direct_mode and detected_build is not None)
        else ""
    )
    page_config = (
        detected_build.page_config
        if (not direct_mode and detected_build is not None)
        else {}
    )
    source_page_url = canonicalize_source_page_url(
        (
            detected_entry.source_page_url
            if (not direct_mode and detected_entry is not None and detected_entry.source_page_url)
            else detected_build.index_url if (not direct_mode and detected_build is not None) else root_url
        ),
        original_folder_url,
    )
    auxiliary_asset_rewrites = collect_auxiliary_asset_rewrites(
        output_dir,
        source_page_url,
        original_folder_url,
        tuple(
            path
            for path in (
                build_dir / assets.framework_name,
                build_dir / assets.data_name,
            )
            if path.name
        ),
    )
    gmsoft_like_build = looks_like_gmsoft_page_config(page_config)
    source_url_spoof_patterns = [
        b"SiteLock",
        b"whitelistedDomains",
        b"allowedRemoteHosts",
        b"IsOnWhitelistedDomain",
        b"DomainLocker",
        b"check_domains_str",
        b"redirect_domain",
        b"ALLOW_DOMAINS",
    ]
    enable_source_url_spoof = any(
        file_contains_any_bytes(path, source_url_spoof_patterns)
        for path in (
            build_dir / assets.framework_name,
            build_dir / assets.data_name,
        )
        if path.name
    )

    asset_cache_buster = compute_asset_cache_buster(build_dir, assets)
    product_name = (
        infer_product_name_from_entry(
            detected_build.index_html,
            slugify_name(output_dir.name),
            source_url=source_page_url,
        )
        if (not direct_mode and detected_build is not None)
        else slugify_name(output_dir.name)
    )
    index_content = generate_index_html(
        product_name,
        assets,
        required_functions,
        framework_analysis.window_roots,
        framework_analysis.window_callable_chains,
        support_script_filenames=[item["name"] for item in downloaded_support_scripts],
        source_page_url=source_page_url,
        enable_source_url_spoof=enable_source_url_spoof,
        original_folder_url=original_folder_url,
        streaming_assets_url=streaming_assets_url,
        asset_cache_buster=asset_cache_buster,
        page_config=page_config,
        auxiliary_asset_rewrites=auxiliary_asset_rewrites,
        allowed_launch_modes=allowed_launch_modes,
        recommended_launch_mode=recommended_launch_mode,
    )
    validate_required_function_coverage(index_content, required_functions)
    (output_dir / "index.html").write_text(index_content, encoding="utf-8")
    write_vendor_support_files(output_dir, framework_analysis)
    (output_dir / "required-functions.json").write_text(
        json.dumps(
            {
                "count": len(required_functions),
                "functions": required_functions,
                "window_root_count": len(framework_analysis.window_roots),
                "window_roots": framework_analysis.window_roots,
                "window_callable_chain_count": len(framework_analysis.window_callable_chains),
                "window_callable_chains": framework_analysis.window_callable_chains,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    summary = {
        "output_dir": str(output_dir),
        "index_html": str(output_dir / "index.html"),
        "required_functions_file": str(output_dir / "required-functions.json"),
        "loader": assets.loader_name,
        "framework": assets.framework_name,
        "data": assets.data_name,
        "wasm": assets.wasm_name,
        "required_function_count": len(required_functions),
        "window_root_count": len(framework_analysis.window_roots),
        "window_callable_chain_count": len(framework_analysis.window_callable_chains),
        "used_br_assets": assets.used_br_assets,
        "used_compressed_assets": assets.used_br_assets,
        "site_lock_framework_patched": site_lock_framework_patched,
        "gmsoft_host_bridge_patched": gmsoft_host_bridge_patched,
        "gmsoft_sendmessage_defaults_patched": gmsoft_sendmessage_defaults_patched,
        "sendmessage_value_compat_patched": sendmessage_value_compat_patched,
        "build_kind": build_kind,
        "mode": "direct_urls" if direct_mode else "entry_auto",
        "source_page_url": source_page_url,
        "source_url_spoof_enabled": enable_source_url_spoof,
        "original_folder_url": original_folder_url,
        "streaming_assets_url": streaming_assets_url,
        "asset_cache_buster": asset_cache_buster,
        "launch_options": allowed_launch_modes,
        "recommended_launch_mode": recommended_launch_mode,
        "gmsoft_like_build": gmsoft_like_build,
        "page_config_keys": sorted(page_config.keys()),
        "auxiliary_asset_rewrites": auxiliary_asset_rewrites,
        "support_script_files": [item["name"] for item in downloaded_support_scripts],
        "support_script_urls": [item["resolved_url"] for item in downloaded_support_scripts],
        "progress_file": str(progress_file),
    }
    if assets.build_kind == "legacy_json":
        summary["legacy_asset_names"] = assets.legacy_asset_names
    (output_dir / "standalone-build-info.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    progress_payload = load_json_file(progress_file)
    progress_payload["completed"] = True
    progress_payload["summary"] = summary
    save_json_file(progress_file, progress_payload)

    log("Done.")
    log(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except FetchError as exc:
        print(f"[unity-standalone] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
