#!/usr/bin/env python3
"""
Create a standalone Unity WebGL package from direct asset URLs or an entry URL.

Usage:
  python unity_standalone.py "https://example.com/game/"
  python unity_standalone.py --loader-url "<...loader.js>" --framework-url "<...framework.js|...framework.js.unityweb>" --data-url "<...data|...data.unityweb>" --wasm-url "<...wasm|...wasm.unityweb>"
  python unity_standalone.py "<entry-url>" --out "My Game" --overwrite
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
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


def file_contains_any_bytes(path: Path, patterns: Sequence[bytes]) -> bool:
    if not path.exists() or not patterns:
        return False
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    return any(pattern in raw for pattern in patterns)


def log(message: str) -> None:
    print(f"[unity-standalone] {message}")


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
    if "://" not in url:
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise FetchError(f"Unsupported URL scheme: {parsed.scheme}")
    path = urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/:@%+")
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, parsed.fragment)
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


def fetch_url(url: str, timeout: int = 30) -> tuple[str, bytes, str, str]:
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            resolved_url = response.geturl()
            body = response.read()
            content_type = response.headers.get_content_type() or ""
            content_encoding = (response.headers.get("Content-Encoding") or "").lower()
            return resolved_url, body, content_type, content_encoding
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{url} -> HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"{url} -> {exc.reason}") from exc


def looks_like_html(raw: bytes) -> bool:
    sample = raw[:512].lower()
    return sample.startswith(b"<!doctype html") or b"<html" in sample


def candidate_index_urls(input_url: str, root_url: str) -> list[str]:
    candidates = []

    parsed_input = urllib.parse.urlparse(input_url)
    if parsed_input.path.endswith(".html"):
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


def looks_like_unity_entry_html(index_html: str) -> bool:
    return (
        ".loader.js" in index_html
        or "createUnityInstance" in index_html
        or "UnityLoader.instantiate" in index_html
    )


def is_ignored_embedded_url(url: str) -> bool:
    lower = url.lower()
    ignored_fragments = (
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "googletagmanager.com",
        "google-analytics.com",
        "gstatic.com/",
        "apis.google.com/js/api.js",
        "lh3.googleusercontent.com",
        "sites.google.com/u/",
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

    for raw in re.findall(r'data-code="([\s\S]*?)"', index_html, re.IGNORECASE):
        decoded = html.unescape(raw).strip()
        if decoded:
            snippets.append(decoded)

    user_html_patterns = (
        r'userHtml\\x22:\s*\\x22([\s\S]*?)\\x22,\s*\\x22ncc\\x22',
        r'"userHtml"\s*:\s*"([\s\S]*?)"\s*,\s*"ncc"',
    )
    for pattern in user_html_patterns:
        for raw in re.findall(pattern, index_html, re.IGNORECASE):
            decoded = html.unescape(decode_js_string_literal(raw)).strip()
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


def extract_embedded_candidate_urls(index_html: str, index_url: str) -> list[str]:
    raw_candidates: list[str] = []
    patterns = (
        r"""<iframe[^>]+src=["']([^"']+)["']""",
        r"""data-url=["']([^"']+)["']""",
        r"""(?:const|let|var)\s+[A-Za-z_$][A-Za-z0-9_$]*URL\s*=\s*["'](https?://[^"']+)["']""",
        r"""["'](https?://[^"']+)["']""",
    )

    for pattern in patterns:
        raw_candidates.extend(re.findall(pattern, index_html, re.IGNORECASE))

    urls: list[str] = []
    seen = set()
    for raw in raw_candidates:
        candidate = html.unescape(raw).replace("\\/", "/").strip()
        if not candidate:
            continue
        absolute = normalize_url(urllib.parse.urljoin(index_url, candidate))
        if is_ignored_embedded_url(absolute):
            continue
        if absolute not in seen:
            urls.append(absolute)
            seen.add(absolute)

    def url_priority(url: str) -> tuple[int, str]:
        lower = url.lower()
        score = 0
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
    errors: list[str] = []
    visited_urls: set[str] = set()
    visited_snippets: set[str] = set()

    def inspect_html(index_url: str, index_html: str, depth: int, source: str) -> tuple[str, str] | None:
        snippets = extract_embedded_html_snippets(index_html)
        for snippet in snippets:
            if looks_like_unity_entry_html(snippet):
                return index_url, snippet

        if looks_like_unity_entry_html(index_html):
            return index_url, index_html
        if depth >= 6:
            errors.append(f"{source} -> reached embed recursion limit")
            return None

        for snippet in snippets:
            snippet_key = snippet[:4096]
            if snippet_key in visited_snippets:
                continue
            visited_snippets.add(snippet_key)
            result = inspect_html(index_url, snippet, depth + 1, f"{source} -> embedded HTML")
            if result:
                return result

        for child_url in extract_embedded_candidate_urls(index_html, index_url):
            result = inspect_url(child_url, depth + 1)
            if result:
                return result

        errors.append(f"{source} -> fetched but no Unity build reference found")
        return None

    def inspect_url(candidate: str, depth: int) -> tuple[str, str] | None:
        normalized_candidate = normalize_url(candidate)
        if normalized_candidate in visited_urls:
            return None
        visited_urls.add(normalized_candidate)

        try:
            resolved, raw, _, _ = fetch_url(normalized_candidate)
        except FetchError as exc:
            errors.append(str(exc))
            return None

        text = decode_html_body(raw)
        return inspect_html(resolved, text, depth, resolved)

    for candidate in candidate_index_urls(input_url, root_url):
        result = inspect_url(candidate, 0)
        if result:
            return result

    joined = "\n  - ".join(errors) if errors else "No candidate URLs were tested."
    raise FetchError(f"Could not find Unity index HTML.\n  - {joined}")


def extract_build_url_prefix(index_html: str) -> str:
    match = re.search(
        r"""(?:const|let|var)\s+buildUrl\s*=\s*["']([^"']+)["']""",
        index_html,
        re.IGNORECASE,
    )
    if not match:
        return ""
    return html.unescape(match.group(1)).replace("\\/", "/").strip()


def extract_urls_with_suffix(index_html: str, index_url: str, suffix_regex: str) -> list[str]:
    # Find quoted URLs in script/html content.
    pattern = re.compile(rf"""["']([^"']+?{suffix_regex}(?:\?[^"']*)?)["']""", re.IGNORECASE)
    urls: list[str] = []
    build_prefix = extract_build_url_prefix(index_html)
    normalized_prefix = build_prefix.strip("/")

    for match in pattern.findall(index_html):
        unescaped = html.unescape(match).replace("\\/", "/")
        if normalized_prefix and unescaped.startswith("/") and not unescaped.startswith("//"):
            # Handle patterns like: loaderUrl = buildUrl + "/v111.loader.js"
            # so "/v111.loader.js" becomes "Build/v111.loader.js".
            if unescaped.lstrip("/").startswith(normalized_prefix + "/"):
                unescaped = unescaped.lstrip("/")
            else:
                unescaped = normalized_prefix + unescaped
        absolute = urllib.parse.urljoin(index_url, unescaped)
        absolute = normalize_url(absolute)
        urls.append(absolute)

    deduped: list[str] = []
    seen = set()
    for url in urls:
        if url not in seen:
            deduped.append(url)
            seen.add(url)
    return deduped


def extract_config_asset_urls(index_html: str, index_url: str) -> dict[str, list[str]]:
    build_prefix = extract_build_url_prefix(index_html).strip("/")

    def absolutize(raw_value: str) -> str:
        candidate = html.unescape(raw_value).replace("\\/", "/")
        if build_prefix and candidate.startswith("/") and not candidate.startswith("//"):
            if candidate.lstrip("/").startswith(build_prefix + "/"):
                candidate = candidate.lstrip("/")
            else:
                candidate = build_prefix + candidate
        return normalize_url(urllib.parse.urljoin(index_url, candidate))

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
            absolute = absolutize(raw)
            if absolute not in seen:
                found.append(absolute)
                seen.add(absolute)

        for raw in direct_pattern.findall(index_html):
            absolute = absolutize(raw)
            if absolute not in seen:
                found.append(absolute)
                seen.add(absolute)

        return found

    return {
        "data": collect_for_key("dataUrl"),
        "framework": collect_for_key("frameworkUrl"),
        "wasm": collect_for_key("codeUrl") + collect_for_key("wasmUrl"),
    }


def extract_loader_url(index_html: str, index_url: str) -> str:
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


def fetch_json_payload(url: str) -> dict[str, Any]:
    resolved_url, raw, _, _ = fetch_url(url)
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


def download_first_valid(urls: Sequence[str], destination: Path) -> tuple[str, str, str]:
    errors: list[str] = []

    for url in urls:
        try:
            resolved, raw, _, content_encoding = fetch_url(url)
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
        legacy_config = fetch_json_payload(config_url)
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
    return DetectedBuild(
        build_kind="modern",
        index_url=index_url,
        index_html=index_html,
        loader_url=loader_url,
        candidates=build_asset_candidate_urls(loader_url, index_html, index_url),
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


def analyze_framework(framework_path: Path) -> FrameworkAnalysis:
    raw_text = framework_path.read_text(encoding="utf-8", errors="ignore")

    # Pattern 1: explicit wrapper names such as _VendorBridgeGetSomething(...)
    wrapper_matches = re.findall(r"_([A-Za-z0-9_]*Bridge[A-Za-z0-9_]*)\s*\(", raw_text)

    # Pattern 2: function calls that explicitly target window.<name>(...)
    window_call_matches = re.findall(r"\bwindow\.([A-Za-z_$][A-Za-z0-9_$]*)\s*\(", raw_text)

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
    }
    excluded_window_roots = excluded_function_names | {
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

    requires_crazygames_sdk = "/vs/crazygames-sdk-v2.js" in raw_text

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


def generate_crazygames_sdk_stub() -> str:
    return """(function () {
  var root = window.CrazyGames = window.CrazyGames || {};
  var sdk = root.SDK = root.SDK || {};
  var ad = sdk.ad = sdk.ad || {};
  var banner = sdk.banner = sdk.banner || {};
  var game = sdk.game = sdk.game || {};
  var user = sdk.user = sdk.user || {};

  if (typeof sdk.addInitCallback !== "function") {
    sdk.addInitCallback = function (callback) {
      if (typeof callback === "function") {
        callback({});
      }
    };
  }
  if (typeof ad.hasAdblock !== "function") {
    ad.hasAdblock = function (callback) {
      if (typeof callback === "function") {
        callback(null, false);
      }
      return false;
    };
  }
  if (typeof ad.requestAd !== "function") {
    ad.requestAd = function (_adType, callbacks) {
      callbacks = callbacks || {};
      if (typeof callbacks.adStarted === "function") {
        callbacks.adStarted();
      }
      if (typeof callbacks.adFinished === "function") {
        callbacks.adFinished();
      }
      return "closed";
    };
  }
  if (typeof banner.requestOverlayBanners !== "function") {
    banner.requestOverlayBanners = function (_banners, callback) {
      if (typeof callback === "function") {
        callback("", "bannerRendered", null);
      }
      return "bannerRendered";
    };
  }
  if (typeof game.gameplayStart !== "function") {
    game.gameplayStart = function () {};
  }
  if (typeof game.gameplayStop !== "function") {
    game.gameplayStop = function () {};
  }
  if (typeof game.happytime !== "function") {
    game.happytime = function () {};
  }
  if (typeof user.addAuthListener !== "function") {
    user.addAuthListener = function (callback) {
      if (typeof callback === "function") {
        callback({});
      }
    };
  }
  if (typeof user.addScore !== "function") {
    user.addScore = function () {};
  }
  if (typeof user.getUser !== "function") {
    user.getUser = function (callback) {
      if (typeof callback === "function") {
        callback(null, {});
      }
    };
  }
  if (typeof user.getUserToken !== "function") {
    user.getUserToken = function (callback) {
      if (typeof callback === "function") {
        callback(null, "");
      }
      return "";
    };
  }
  if (typeof user.getXsollaUserToken !== "function") {
    user.getXsollaUserToken = function (callback) {
      if (typeof callback === "function") {
        callback(null, "");
      }
      return "";
    };
  }
  if (typeof user.showAccountLinkPrompt !== "function") {
    user.showAccountLinkPrompt = function (callback) {
      if (typeof callback === "function") {
        callback(null, {});
      }
    };
  }
  if (typeof user.showAuthPrompt !== "function") {
    user.showAuthPrompt = function (callback) {
      if (typeof callback === "function") {
        callback(null, {});
      }
    };
  }

  var legacyRoot = window.Crazygames = window.Crazygames || {};
  if (typeof legacyRoot.requestInviteUrl !== "function") {
    legacyRoot.requestInviteUrl = function () {};
  }
})();\n"""


def write_vendor_support_files(output_dir: Path, framework_analysis: FrameworkAnalysis) -> None:
    if framework_analysis.requires_crazygames_sdk:
        vendor_dir = output_dir / "vs"
        vendor_dir.mkdir(parents=True, exist_ok=True)
        (vendor_dir / "crazygames-sdk-v2.js").write_text(
            generate_crazygames_sdk_stub(),
            encoding="utf-8",
        )


def generate_index_html(
    product_name: str,
    assets: DownloadedAssets,
    required_functions: Sequence[str],
    window_roots: Sequence[str],
    window_callable_chains: Sequence[str],
    source_page_url: str = "",
    enable_source_url_spoof: bool = False,
) -> str:
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
    decompression_fallback_line = (
        "  config.decompressionFallback = true;\n" if assets.used_br_assets else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no" />
  <title>{html.escape(product_name)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@600;700;800;900&display=swap" rel="stylesheet" />
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
      font-family: "Inter", -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
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
      font: 800 14px/1.1 "Inter", -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
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
        <div id="status">Choose how you want to launch</div>
      </div>
    </div>
  </div>

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

      const known = {{
        // Ads
        getInterstitialState: () => interstitialState,
        getRewardedState: () => rewardedState,
        getBannerState: () => bannerState,
        getRewardedPlacement: () => EMPTY,
        getMinimumDelayBetweenInterstitial: () => minimumDelayBetweenInterstitial,
        showInterstitial: () => completeInterstitial(),
        showRewarded: () => completeRewarded(),
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
      const reduceMotion =
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const mouse = {{ x: 0.5, y: 0.5, tx: 0.5, ty: 0.5 }};

      function isVisible() {{
        return loadingScreen.style.display !== "none";
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
          Math.min(160, Math.max(90, (window.innerWidth * window.innerHeight) / 14000))
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
        if (reduceMotion || !isVisible()) {{
          return;
        }}
        window.setTimeout(function () {{
          if (!shootingStar && isVisible()) {{
            shootingStar = new ShootingStar();
          }}
          scheduleShootingStar();
        }}, Math.random() * 2500 + 3500);
      }}

      function animateStars() {{
        if (!isVisible()) {{
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
        if (!isVisible()) {{
          return;
        }}
        mouse.x += (mouse.tx - mouse.x) * 0.06;
        mouse.y += (mouse.ty - mouse.y) * 0.06;
        window.requestAnimationFrame(smoothMouse);
      }}

      function drawWaves() {{
        if (!isVisible()) {{
          return;
        }}
        const width = window.innerWidth;
        const height = window.innerHeight;
        waveCtx.clearRect(0, 0, width, height);
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
      if (reduceMotion) {{
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
      const LOCAL_PAGE_URL =
        window.__unityStandaloneLocalPageUrl || window.location.href;
      const LOCAL_BUILD_ROOT_URL = new URL(BUILD_DIR + "/", LOCAL_PAGE_URL).toString();

      const canvas = document.getElementById("unity-canvas");
      const legacyContainer = document.getElementById("unity-legacy-container");
      const loadingScreen = document.getElementById("loadingScreen");
      const progressFill = document.getElementById("progressFill");
      const progressTrack = document.getElementById("progressTrack");
      const launchPanel = document.getElementById("launchPanel");
      const launchFullscreenBtn = document.getElementById("launchFullscreenBtn");
      const launchFrameBtn = document.getElementById("launchFrameBtn");
      const status = document.getElementById("status");

      let started = false;
      let loadingScreenDismissed = false;
      let launchPanelHideTimer = 0;
      let legacyConfigUrl = "";
      let sourceUrlSpoofApplied = false;
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

      function setStatus(text) {{
        if (status) {{
          status.textContent = text;
        }}
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
        setFullscreenScrollLock(isFullscreenActive());
      }}

      function isFullscreenScrollKey(event) {{
        const key = typeof event.key === "string" ? event.key : "";
        const code = typeof event.code === "string" ? event.code : "";
        return fullscreenScrollKeys.has(key) || fullscreenScrollCodes.has(code);
      }}

      function preventFullscreenScroll(event) {{
        if (!isFullscreenActive()) {{
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
          !isFullscreenActive() ||
          (window.scrollX === 0 && window.scrollY === 0) ||
          typeof window.scrollTo !== "function"
        ) {{
          return;
        }}
        window.scrollTo(0, 0);
      }}

      function buildLocalUrl(relativePath) {{
        const cleanPath = String(relativePath || "").replace(/^\\.?\\//, "");
        return new URL(cleanPath, LOCAL_PAGE_URL).toString();
      }}

      function buildBuildAssetUrl(name) {{
        const cleanName = String(name || "").replace(/^\\.?\\//, "");
        return new URL(cleanName, LOCAL_BUILD_ROOT_URL).toString();
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
        requestFullscreenMode().then(function (enabled) {{
          if (!enabled && !started) {{
            setStatus("Fullscreen unavailable here. Launching here.");
          }}
        }});
        startGame();
      }}

      function ensureStorageAccess() {{
        const hasApi =
          typeof document.hasStorageAccess === "function" &&
          typeof document.requestStorageAccess === "function";
        if (!hasApi) {{
          return Promise.resolve();
        }}
        return document.hasStorageAccess()
          .then(function (hasAccess) {{
            if (hasAccess) {{
              return;
            }}
            return document.requestStorageAccess().catch(function () {{
              // Continue without hard-failing game load.
            }});
          }})
          .catch(function () {{
            // Continue without hard-failing game load.
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
          config[key] = buildBuildAssetUrl(relativeValue);
        }});
        return config;
      }}

      function startModernGame(loaderUrl) {{
        const config = {{
          dataUrl: buildBuildAssetUrl(DATA_FILE),
          frameworkUrl: buildBuildAssetUrl(FRAMEWORK_FILE),
          codeUrl: buildBuildAssetUrl(WASM_FILE),
          streamingAssetsUrl: buildBuildAssetUrl("StreamingAssets"),
          companyName: PRODUCT_NAME,
          productName: PRODUCT_NAME,
          productVersion: "1.0.0",
        }};
{decompression_fallback_line}        const script = document.createElement("script");
        script.src = loaderUrl;
        script.onload = function () {{
          if (typeof createUnityInstance !== "function") {{
            resetLaunchState();
            setStatus("Loader error: createUnityInstance is missing");
            return;
          }}

          createUnityInstance(canvas, config, function (progress) {{
            const percent = setProgress(progress);
            setStatus("Loading " + percent + "%");
          }})
          .then(function () {{
            setProgress(1);
            setStatus("Ready");
            window.setTimeout(dismissLoadingScreen, 380);
          }})
          .catch(function (err) {{
            console.error(err);
            resetLaunchState();
            setStatus("Failed to load game");
            alert("Unity failed to load: " + err);
          }});
        }};
        script.onerror = function () {{
          resetLaunchState();
          setStatus("Failed to load Unity loader script");
        }};
        document.body.appendChild(script);
      }}

      function startLegacyGame(loaderUrl) {{
        if (!legacyContainer) {{
          resetLaunchState();
          setStatus("Legacy Unity container is missing");
          return;
        }}

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

        const script = document.createElement("script");
        script.src = loaderUrl;
        script.onload = function () {{
          const instantiate =
            window.UnityLoader && typeof window.UnityLoader.instantiate === "function"
              ? window.UnityLoader.instantiate
              : null;
          if (!instantiate) {{
            resetLaunchState();
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
                    setStatus("Ready");
                  }}, 380);
                }}
              }},
            }});
          }} catch (err) {{
            console.error(err);
            resetLaunchState();
            setStatus("Failed to load game");
            alert("Unity failed to load: " + err);
          }}
        }};
        script.onerror = function () {{
          resetLaunchState();
          setStatus("Failed to load Unity loader script");
        }};
        document.body.appendChild(script);
      }}

      function startGame() {{
        if (started) {{
          return;
        }}
        started = true;
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
      setStatus("Choose how you want to launch");

      window.addEventListener("wheel", preventFullscreenScroll, {{ passive: false }});
      window.addEventListener("touchmove", preventFullscreenScroll, {{ passive: false }});
      window.addEventListener("keydown", preventFullscreenScroll, {{ passive: false }});
      window.addEventListener("scroll", enforceFullscreenScrollTop, {{ passive: true }});
      window.addEventListener("fullscreenchange", syncFullscreenScrollLock);
      window.addEventListener("webkitfullscreenchange", syncFullscreenScrollLock);
      window.addEventListener("mozfullscreenchange", syncFullscreenScrollLock);
      window.addEventListener("MSFullscreenChange", syncFullscreenScrollLock);

      launchFullscreenBtn.addEventListener("click", startFullscreenGame);
      launchFrameBtn.addEventListener("click", startGame);

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
        resolved_url, _, compression_kind = download_first_valid(candidates[kind], destination)

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
        resolved_url, _, compression_kind = download_first_valid(candidates[kind], destination)

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
            "Download a Unity WebGL build and generate a standalone "
            "index.html with runtime stubs for missing integration APIs."
        )
    )
    parser.add_argument(
        "entry_url",
        nargs="?",
        help="Optional entry page URL (any host) to auto-detect Unity asset URLs.",
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
    return parser.parse_args(argv)


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


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)

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

    build_kind = "modern"
    legacy_config: dict[str, Any] = {}

    if direct_mode:
        loader_url = normalize_url(args.loader_url)
        framework_url = normalize_url(args.framework_url)
        data_url = normalize_url(args.data_url)
        wasm_url = normalize_url(args.wasm_url)
        root_url = derive_game_root_url(loader_url)
        candidates = build_asset_candidate_urls_from_direct(
            loader_url=loader_url,
            framework_url=framework_url,
            data_url=data_url,
            wasm_url=wasm_url,
        )
        log("Mode: direct asset URLs")
        log(f"Loader URL: {loader_url}")
    else:
        input_url = normalize_url(args.entry_url)
        root_url = derive_game_root_url(input_url)

        log("Mode: entry URL auto-detect")
        log(f"Input URL: {input_url}")
        log(f"Game root URL: {root_url}")

        index_url, index_html = find_index_html(input_url, root_url)
        log(f"Resolved index URL: {index_url}")

        detected_build = detect_entry_build(index_url, index_html)
        build_kind = detected_build.build_kind
        legacy_config = detected_build.legacy_config
        loader_url = detected_build.loader_url
        candidates = detected_build.candidates

        log(f"Detected build kind: {build_kind}")
        log(f"Resolved loader URL: {loader_url}")

    output_name = args.out_dir.strip() or infer_output_name_from_url(root_url, loader_url)
    output_dir = Path(output_name).resolve()
    build_dir = output_dir / "Build"
    progress_file = output_dir / ".standalone-progress.json"

    if output_dir.exists():
        if args.overwrite:
            shutil.rmtree(output_dir)
            log(f"Removed existing output directory: {output_dir}")
        else:
            log(f"Output directory exists, resuming if possible: {output_dir}")

    build_dir.mkdir(parents=True, exist_ok=True)

    progress_payload = load_json_file(progress_file)
    progress_payload.update(
        {
            "mode": "direct_urls" if direct_mode else "entry_auto",
            "build_kind": build_kind,
            "root_url": root_url,
            "loader_url": loader_url,
            "completed": False,
        }
    )
    if legacy_config:
        progress_payload["legacy_config"] = legacy_config
    save_json_file(progress_file, progress_payload)

    if build_kind == "legacy_json":
        assets = download_legacy_assets(build_dir, candidates, legacy_config, progress_file)
    else:
        assets = download_assets(build_dir, candidates, progress_file)

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
    source_page_url = detected_build.index_url if not direct_mode else root_url
    enable_source_url_spoof = file_contains_any_bytes(
        build_dir / assets.data_name,
        [
            b"SiteLock",
            b"whitelistedDomains",
            b"allowedRemoteHosts",
            b"IsOnWhitelistedDomain",
        ],
    )

    product_name = slugify_name(output_dir.name).replace("-", " ")
    index_content = generate_index_html(
        product_name,
        assets,
        required_functions,
        framework_analysis.window_roots,
        framework_analysis.window_callable_chains,
        source_page_url=source_page_url,
        enable_source_url_spoof=enable_source_url_spoof,
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
        "build_kind": build_kind,
        "mode": "direct_urls" if direct_mode else "entry_auto",
        "source_page_url": source_page_url,
        "source_url_spoof_enabled": enable_source_url_spoof,
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
