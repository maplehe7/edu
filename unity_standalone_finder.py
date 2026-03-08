#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from typing import Sequence

from unity_standalone import (
    REQUEST_HEADERS,
    detect_entry_build,
    extract_html_title,
    find_supported_entry,
    infer_output_name_from_entry,
)


DUCKDUCKGO_HTML_ENDPOINTS = (
    "https://html.duckduckgo.com/html/",
    "https://duckduckgo.com/html/",
)
BING_RSS_ENDPOINT = "https://www.bing.com/search?format=rss&"
RESULT_LINK_PATTERN = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
TAG_PATTERN = re.compile(r"<[^>]+>")
WORD_PATTERN = re.compile(r"[a-z0-9]+")
SEARCH_QUERY_TEMPLATES = (
    '"{name}" unity webgl game',
    '"{name}" browser game',
    '"{name}" html5 game',
    '"{name}" online game',
    '"{name}" game',
    "{name} unity webgl",
    "{name} browser game",
    "{name} game",
)
BLOCKED_HOSTS = {
    "apps.microsoft.com",
    "dev.to",
    "docs.unity.cn",
    "docs.unity3d.com",
    "github.com",
    "learn.unity.com",
    "merriam-webster.com",
    "mindravel.com",
    "modelo.io",
    "play.google.com",
    "play.unity.com",
    "poki.com",
    "reddit.com",
    "www.poki.com",
    "www.reddit.com",
    "robtopgames.com",
    "thefreedictionary.com",
    "wikipedia.org",
    "www.robtopgames.com",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "en.wikipedia.org",
    "dictionary.cambridge.org",
    "dictionary.com",
}
BLOCKED_RESULT_TERMS = {
    "breed",
    "cambridge",
    "care",
    "definition",
    "dictionary",
    "meaning",
    "manual",
    "merriam-webster",
    "petfinder",
    "thesaurus",
    "tutorial",
    "what is",
    "wiki",
    "wikipedia",
}
GAME_HINT_TERMS = {
    "arcade",
    "crazygames",
    "free online",
    "game",
    "games",
    "html5",
    "io",
    "online",
    "play",
    "speedrun",
    "unblocked",
    "webgl",
}
HTML_GAME_SIGNAL_TERMS = {
    ".data.unityweb",
    ".framework.js",
    ".loader.js",
    ".wasm",
    "babylon",
    "c2runtime.js",
    "construct 2",
    "construct 3",
    "createunityinstance",
    "data-iframe",
    "eaglercraft",
    "game-iframe",
    "game_frame",
    "gamesnacks",
    "offline.js",
    "phaser",
    "pixi",
    "unityloader",
    "voodoo",
}
URL_GAME_SIGNAL_TERMS = {
    "arcade",
    "browser",
    "crazygames",
    "game",
    "games",
    "html5",
    "online",
    "play",
    "unblocked",
    "webgl",
}
UNITY_REQUIRED_ASSET_KEYS = {
    "modern": ("loader", "framework", "data", "wasm"),
    "legacy_json": ("loader", "dataUrl", "wasmCodeUrl", "wasmFrameworkUrl"),
}
PROBE_TIMEOUT_SECONDS = 12
SEARCH_TIMEOUT_SECONDS = 10
PROBE_CANDIDATE_LIMIT = 3
FINDER_EVAL_WORKERS = 4
PROBE_RESULT_CACHE: dict[tuple[str, str], bool] = {}
SUPPORTED_ENTRY_CACHE: dict[str, object | None] = {}
DETECTED_BUILD_CACHE: dict[str, object | None] = {}
PREFERRED_HOST_SCORES = {
    "games.crazygames.com": 16,
    "crazygames.com": 14,
    "gamecomets.com": 12,
    "1games.io": 11,
    "play2.1games.io": 10,
    "geometrydashlite.io": 10,
    "bitlifeonline.github.io": 9,
    "mortgagecalculator.org": 6,
    "sites.google.com": 4,
    "cdn.jsdelivr.net": 3,
}
ENTRY_KIND_BASE_SCORES = {
    "unity": 430,
    "eaglercraft": 310,
    "html": 220,
}
BUILD_KIND_SCORES = {
    "modern": 95,
    "legacy_json": 58,
    "legacy_unity_loader": 52,
}
ONLINE_REQUIRED_SIGNAL_GROUPS = (
    ("webrtc", 48, ("rtcpeerconnection", "createdatachannel", "iceservers", "stun:", "turn:")),
    ("websocket", 40, ("new websocket(", "websocket(", "wss://", "socket.io", "engine.io")),
    ("photon", 36, ("photonengine", "photon room", "loadbalancingclient", "pun2")),
    ("playfab", 34, ("playfabapi", "playfab", "client/loginwithcustomid", "client/login")),
    ("firebase", 30, ("firebaseio", "firebaseapp", "firestore", "realtime database")),
    ("colyseus", 32, ("colyseus",)),
    ("nakama", 32, ("nakama",)),
    ("braincloud", 28, ("braincloud",)),
    ("multiplayer", 20, ("multiplayer", "matchmaking", "lobby", "leaderboard", "guild", "clan")),
    ("auth", 14, ("/auth", "/login", "oauth", "signin", "accounts.")),
)
ONLINE_TITLE_URL_SIGNAL_GROUPS = (
    ("multiplayer", 18, ("multiplayer", "online multiplayer", "battle royale", "io game")),
    ("login", 10, ("login", "account", "sign in")),
)


@dataclass
class FinderCandidate:
    query: str
    title: str
    source_url: str
    resolved_entry_url: str
    entry_kind: str
    build_kind: str
    source_page_url: str
    suggested_output_name: str
    score: int
    confidence: int
    confidence_label: str
    compatibility_summary: str
    school_network_risk: int
    school_network_risk_label: str
    school_network_summary: str
    reason: str


def normalize_text(value: str) -> str:
    return " ".join(WORD_PATTERN.findall(value.lower()))


def contains_any_term(text: str, terms: set[str]) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in terms)


def search_tokens(game_name: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in WORD_PATTERN.findall(game_name.lower()):
        if len(token) <= 2:
            continue
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def unwrap_result_url(raw_url: str) -> str:
    value = html.unescape(raw_url.strip())
    if value.startswith("//"):
        value = "https:" + value
    parsed = urllib.parse.urlparse(value)
    query = urllib.parse.parse_qs(parsed.query)
    uddg = query.get("uddg")
    if uddg:
        return uddg[0]
    return value


def fetch_search_results(query: str, limit: int = 8) -> list[tuple[str, str]]:
    bing_request = urllib.request.Request(
        BING_RSS_ENDPOINT + urllib.parse.urlencode({"q": query}),
        headers={**REQUEST_HEADERS, "Accept": "application/rss+xml, application/xml, text/xml"},
    )
    try:
        with urllib.request.urlopen(bing_request, timeout=SEARCH_TIMEOUT_SECONDS) as response:
            rss_text = response.read().decode("utf-8", errors="replace")
        root = ET.fromstring(rss_text)
        results: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            source_url = (item.findtext("link") or "").strip()
            normalized_url = source_url.rstrip("/")
            if not source_url or normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            results.append((title, source_url))
            if len(results) >= limit:
                break
        if results:
            return results
    except Exception:
        pass

    encoded_query = urllib.parse.urlencode({"q": query})
    last_error: Exception | None = None
    for endpoint in DUCKDUCKGO_HTML_ENDPOINTS:
        request = urllib.request.Request(
            endpoint + "?" + encoded_query,
            headers={**REQUEST_HEADERS, "Accept": "text/html"},
        )
        try:
            with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT_SECONDS) as response:
                page_html = response.read().decode("utf-8", errors="replace")
        except Exception as exc:
            last_error = exc
            continue

        results: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for match in RESULT_LINK_PATTERN.finditer(page_html):
            title = html.unescape(TAG_PATTERN.sub("", match.group(2))).strip()
            source_url = unwrap_result_url(match.group(1))
            normalized_url = source_url.rstrip("/")
            if not source_url or normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            results.append((title, source_url))
            if len(results) >= limit:
                break
        if results:
            return results

    if last_error is not None:
        raise RuntimeError(f"Search request failed: {last_error}")
    raise RuntimeError("No search results were returned.")


def iter_search_queries(game_name: str) -> list[str]:
    compact_name = " ".join(game_name.split())
    queries: list[str] = []
    seen: set[str] = set()
    for template in SEARCH_QUERY_TEMPLATES:
        query = template.format(name=compact_name).strip()
        if not query or query in seen:
            continue
        seen.add(query)
        queries.append(query)
    return queries


def host_score(url: str) -> int:
    host = urllib.parse.urlparse(url).netloc.lower()
    best = 0
    for suffix, score in PREFERRED_HOST_SCORES.items():
        if host == suffix or host.endswith("." + suffix):
            best = max(best, score)
    return best


def url_game_hint_score(url: str) -> int:
    parsed = urllib.parse.urlparse(url)
    sample = " ".join(
        part
        for part in (
            urllib.parse.unquote(parsed.netloc or ""),
            urllib.parse.unquote(parsed.path or ""),
        )
        if part
    ).lower()
    score = 0
    for term in URL_GAME_SIGNAL_TERMS:
        if term in sample:
            score += 1
    return score


def is_blocked_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if host in BLOCKED_HOSTS:
        return True
    normalized = url.lower()
    return any(
        needle in normalized
        for needle in (
            "youtube.com/embed/",
            "youtube.com/watch",
            "youtu.be/",
            "play.google.com/store",
            "apps.microsoft.com/",
        )
    )


def probe_url_exists(url: str, referer_url: str = "") -> bool:
    cache_key = (url, referer_url)
    cached = PROBE_RESULT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    parsed = urllib.parse.urlparse(url)
    fallback_referer = (
        urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))
        if parsed.scheme in {"http", "https"} and parsed.netloc
        else ""
    )
    referer_candidates: list[str] = []
    for candidate in (referer_url, "", fallback_referer):
        if candidate not in referer_candidates:
            referer_candidates.append(candidate)

    for request_referer in referer_candidates:
        for method in ("HEAD", "GET_RANGE", "GET"):
            headers = dict(REQUEST_HEADERS)
            if request_referer:
                headers["Referer"] = request_referer
            request_method = "GET"
            if method == "GET_RANGE":
                headers["Range"] = "bytes=0-0"
            elif method == "HEAD":
                request_method = "HEAD"
            request = urllib.request.Request(url, headers=headers, method=request_method)
            try:
                with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
                    if method != "HEAD":
                        response.read(1)
                    PROBE_RESULT_CACHE[cache_key] = True
                    return True
            except urllib.error.HTTPError as exc:
                if method == "HEAD" and exc.code in {400, 403, 405, 406, 501}:
                    continue
                if method == "GET_RANGE" and exc.code == 416:
                    PROBE_RESULT_CACHE[cache_key] = True
                    return True
                if method == "GET_RANGE" and exc.code in {400, 403, 405, 406, 501}:
                    continue
            except (urllib.error.URLError, http.client.InvalidURL, ValueError):
                continue

    PROBE_RESULT_CACHE[cache_key] = False
    return False


def token_match_score(game_name: str, *texts: str) -> int:
    tokens = search_tokens(game_name)
    if not tokens:
        return 0
    matched = count_token_matches(game_name, *texts)
    if matched == 0:
        return -45
    if matched == len(tokens):
        return 18 * matched + 24
    return 12 * matched


def count_token_matches(game_name: str, *texts: str) -> int:
    tokens = search_tokens(game_name)
    if not tokens:
        return 0
    haystack_words = set(WORD_PATTERN.findall(" ".join(texts).lower()))
    return sum(1 for token in tokens if token in haystack_words)


def has_phrase_match(game_name: str, *texts: str) -> bool:
    phrase = normalize_text(game_name)
    if not phrase:
        return False
    return any(phrase in normalize_text(text) for text in texts if text)


def has_compact_name_match(game_name: str, *texts: str) -> bool:
    compact_name = "".join(search_tokens(game_name))
    if not compact_name:
        return False
    return any(
        compact_name in re.sub(r"[^a-z0-9]+", "", text.lower())
        for text in texts
        if text
    )


def game_hint_score(*texts: str) -> int:
    lowered = " ".join(texts).lower()
    score = 0
    for term in GAME_HINT_TERMS:
        if term in lowered:
            score += 1
    return score


def passes_result_prefilter(game_name: str, result_title: str, source_url: str) -> bool:
    if is_blocked_url(source_url):
        return False
    if contains_any_term(result_title, BLOCKED_RESULT_TERMS):
        return False
    if has_phrase_match(game_name, result_title, source_url):
        return True
    if has_compact_name_match(game_name, result_title, source_url):
        return True
    if count_token_matches(game_name, result_title, source_url) > 0:
        return True
    if url_game_hint_score(source_url) >= 2 and game_hint_score(result_title, source_url) >= 2:
        return True
    return False


def html_game_signal_score(index_html: str, *texts: str) -> int:
    lowered_html = index_html.lower()
    score = 0
    if "<canvas" in lowered_html:
        score += 2
    if "<iframe" in lowered_html:
        score += 1
    for term in HTML_GAME_SIGNAL_TERMS:
        if term in lowered_html:
            score += 2
    score += game_hint_score(*texts)
    return score


def evaluate_unity_asset_completeness(
    build_kind: str,
    candidates: dict[str, list[str]],
    *,
    referer_url: str,
) -> tuple[int, int, list[str]]:
    required_asset_keys = UNITY_REQUIRED_ASSET_KEYS.get(build_kind, ())
    if not required_asset_keys:
        return 0, 0, []

    def check_asset_key(asset_key: str) -> tuple[str, bool]:
        candidate_urls = list(candidates.get(asset_key, ())[:PROBE_CANDIDATE_LIMIT])
        if not candidate_urls:
            return asset_key, False
        return asset_key, any(
            probe_url_exists(candidate_url, referer_url=referer_url)
            for candidate_url in candidate_urls
        )

    available = 0
    missing: list[str] = []
    max_workers = min(len(required_asset_keys), FINDER_EVAL_WORKERS)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_asset = {
            executor.submit(check_asset_key, asset_key): asset_key
            for asset_key in required_asset_keys
        }
        for future in concurrent.futures.as_completed(future_to_asset):
            asset_key, asset_exists = future.result()
            if asset_exists:
                available += 1
            else:
                missing.append(asset_key)
    missing.sort(key=required_asset_keys.index)
    return available, len(required_asset_keys), missing


def get_supported_entry(source_url: str):
    cached = SUPPORTED_ENTRY_CACHE.get(source_url, Ellipsis)
    if cached is not Ellipsis:
        return cached
    try:
        detected_entry = find_supported_entry(source_url, source_url)
    except Exception:
        SUPPORTED_ENTRY_CACHE[source_url] = None
        return None
    SUPPORTED_ENTRY_CACHE[source_url] = detected_entry
    return detected_entry


def get_detected_build(index_url: str, index_html: str):
    cached = DETECTED_BUILD_CACHE.get(index_url, Ellipsis)
    if cached is not Ellipsis:
        return cached
    try:
        detected_build = detect_entry_build(index_url, index_html)
    except Exception:
        DETECTED_BUILD_CACHE[index_url] = None
        return None
    DETECTED_BUILD_CACHE[index_url] = detected_build
    return detected_build


def confidence_label_for_score(score: int) -> str:
    if score >= 85:
        return "High"
    if score >= 65:
        return "Medium"
    return "Low"


def school_network_risk_label_for_score(score: int) -> str:
    if score >= 65:
        return "High"
    if score >= 30:
        return "Medium"
    return "Low"


def analyze_school_network_risk(
    result_title: str,
    source_url: str,
    resolved_entry_url: str,
    source_page_url: str,
    index_html: str,
) -> tuple[int, str, str]:
    html_sample = index_html.lower()
    title_url_sample = " ".join(
        part for part in (result_title, source_url, resolved_entry_url, source_page_url) if part
    ).lower()
    risk_score = 0
    reasons: list[str] = []

    for label, score, needles in ONLINE_REQUIRED_SIGNAL_GROUPS:
        if any(needle in html_sample for needle in needles):
            risk_score += score
            reasons.append(label)
    for label, score, needles in ONLINE_TITLE_URL_SIGNAL_GROUPS:
        if any(needle in title_url_sample for needle in needles):
            risk_score += score
            reasons.append(label)

    unique_reasons: list[str] = []
    seen_reasons: set[str] = set()
    for reason in reasons:
        if reason in seen_reasons:
            continue
        seen_reasons.add(reason)
        unique_reasons.append(reason)

    risk_score = min(risk_score, 99)
    risk_label = school_network_risk_label_for_score(risk_score)
    if risk_label == "Low":
        return risk_score, risk_label, "school network risk low"
    if not unique_reasons:
        return risk_score, risk_label, "online-service dependency detected"
    joined_reasons = ", ".join(unique_reasons[:3])
    if risk_label == "High":
        summary = f"likely blocked on school networks ({joined_reasons})"
    else:
        summary = f"online-service dependency detected ({joined_reasons})"
    return risk_score, risk_label, summary


def evaluate_candidate(
    game_name: str,
    query: str,
    result_title: str,
    source_url: str,
) -> FinderCandidate | None:
    if is_blocked_url(source_url):
        return None
    if contains_any_term(result_title, BLOCKED_RESULT_TERMS):
        return None

    detected_entry = get_supported_entry(source_url)
    if detected_entry is None:
        return None

    if detected_entry.entry_kind == "remote_stream":
        return None
    if is_blocked_url(detected_entry.index_url):
        return None

    build_kind = ""
    unity_asset_available = 0
    unity_asset_total = 0
    unity_missing_assets: list[str] = []
    compatibility_summary = ""
    if detected_entry.entry_kind == "unity":
        detected_build = get_detected_build(
            detected_entry.index_url,
            detected_entry.index_html,
        )
        if detected_build is None:
            return None
        build_kind = detected_build.build_kind
        unity_asset_available, unity_asset_total, unity_missing_assets = evaluate_unity_asset_completeness(
            build_kind,
            detected_build.candidates,
            referer_url=detected_entry.index_url,
        )
        if unity_asset_total and unity_asset_available == 0:
            return None
        compatibility_summary = (
            f"assets {unity_asset_available}/{unity_asset_total}"
            if unity_asset_total
            else "assets unknown"
        )
        if unity_missing_assets:
            compatibility_summary += f" missing {', '.join(unity_missing_assets)}"

    title_hint = (
        extract_html_title(detected_entry.index_html)
        or result_title
        or game_name
    )
    if contains_any_term(title_hint, BLOCKED_RESULT_TERMS):
        return None
    source_page_url = detected_entry.source_page_url or source_url
    matched_tokens = count_token_matches(
        game_name,
        result_title,
        title_hint,
        source_url,
        detected_entry.index_url,
        source_page_url,
    )
    strongest_host_score = max(
        host_score(source_url),
        host_score(detected_entry.index_url),
        host_score(source_page_url),
    )
    strongest_url_hint_score = max(
        url_game_hint_score(source_url),
        url_game_hint_score(detected_entry.index_url),
        url_game_hint_score(source_page_url),
    )
    source_trust_signal = strongest_host_score + strongest_url_hint_score * 4
    phrase_match = has_phrase_match(
        game_name,
        result_title,
        title_hint,
        source_url,
        detected_entry.index_url,
        source_page_url,
    )
    display_phrase_match = has_phrase_match(game_name, result_title, title_hint)
    url_phrase_match = has_phrase_match(
        game_name,
        source_url,
        detected_entry.index_url,
        source_page_url,
    )
    source_url_compact_match = has_compact_name_match(
        game_name,
        source_url,
        source_page_url,
    )
    game_hints = game_hint_score(
        result_title,
        title_hint,
        source_url,
        detected_entry.index_url,
        source_page_url,
    )
    html_signal_score = html_game_signal_score(
        detected_entry.index_html,
        result_title,
        title_hint,
        source_url,
        detected_entry.index_url,
        source_page_url,
    )
    token_count = len(search_tokens(game_name))
    if token_count <= 2:
        required_html_match_count = token_count or 1
    else:
        required_html_match_count = token_count - 1
    if (
        detected_entry.entry_kind == "html"
        and source_trust_signal < 8
        and not phrase_match
        and matched_tokens < required_html_match_count
    ):
        return None
    if detected_entry.entry_kind == "html":
        if source_trust_signal < 8 and game_hints < 2 and html_signal_score < 4:
            return None
        if matched_tokens == 0 and not phrase_match:
            return None
        compatibility_summary = f"html signals {html_signal_score}"
    elif detected_entry.entry_kind == "eaglercraft":
        compatibility_summary = "eagler bundle"

    suggested_output_name = infer_output_name_from_entry(
        title_hint,
        source_url,
        fallback_name=game_name,
        source_page_url=source_page_url,
    )

    score = ENTRY_KIND_BASE_SCORES.get(detected_entry.entry_kind, 0)
    score += BUILD_KIND_SCORES.get(build_kind, 0)
    score += host_score(source_url)
    score += host_score(detected_entry.index_url)
    score += host_score(source_page_url)
    score += strongest_url_hint_score * 8
    score += token_match_score(
        game_name,
        result_title,
        title_hint,
        source_url,
        detected_entry.index_url,
        source_page_url,
    )
    if display_phrase_match:
        score += 18
    elif url_phrase_match:
        score += 8
    if source_url_compact_match:
        score += 12

    if detected_entry.index_url.rstrip("/") == source_url.rstrip("/"):
        score += 8
    if detected_entry.entry_kind == "html" and detected_entry.index_url.endswith((".xml", ".html", ".php")):
        score += 10
    if detected_entry.entry_kind == "unity" and unity_asset_total:
        score += unity_asset_available * 16
        if unity_asset_available == unity_asset_total:
            score += 22
        else:
            score -= len(unity_missing_assets) * 18
    elif detected_entry.entry_kind == "html":
        score += min(html_signal_score * 3, 24)

    confidence = 0
    if detected_entry.entry_kind == "unity":
        completeness_ratio = (
            unity_asset_available / unity_asset_total if unity_asset_total else 0.0
        )
        confidence = 42
        if build_kind == "modern":
            confidence += 20
        elif build_kind == "legacy_json":
            confidence += 12
        confidence += int(completeness_ratio * 28)
        confidence += min(source_trust_signal, 10)
        if phrase_match:
            confidence += 6
        if display_phrase_match:
            confidence += 4
        if source_url_compact_match:
            confidence += 4
        confidence += min(matched_tokens * 3, 9)
        missing_asset_count = max(unity_asset_total - unity_asset_available, 0)
        confidence -= missing_asset_count * 14
        if unity_asset_total and unity_asset_available < unity_asset_total:
            confidence = min(confidence, 79)
        if unity_asset_total and unity_asset_available <= max(1, unity_asset_total // 2):
            confidence = min(confidence, 59)
    elif detected_entry.entry_kind == "html":
        confidence = 30
        if phrase_match:
            confidence += 12
        if display_phrase_match:
            confidence += 4
        if source_url_compact_match:
            confidence += 4
        confidence += min(matched_tokens * 5, 15)
        confidence += min(game_hints * 3, 12)
        confidence += min(html_signal_score * 2, 16)
        confidence += min(source_trust_signal, 12)
    else:
        confidence = 72

    confidence = max(0, min(confidence, 99))
    confidence_label = confidence_label_for_score(confidence)
    school_network_risk, school_network_risk_label, school_network_summary = analyze_school_network_risk(
        result_title,
        source_url,
        detected_entry.index_url,
        source_page_url,
        detected_entry.index_html,
    )
    if school_network_risk >= 65:
        score -= 40
        confidence = max(0, confidence - 22)
    elif school_network_risk >= 30:
        score -= 18
        confidence = max(0, confidence - 10)
    confidence_label = confidence_label_for_score(confidence)

    reason_parts = [detected_entry.entry_kind]
    if build_kind:
        reason_parts.append(build_kind)
    if source_page_url and source_page_url != source_url:
        reason_parts.append("wrapper-resolved")
    resolved_host_bonus = host_score(detected_entry.index_url)
    if resolved_host_bonus:
        reason_parts.append(f"host+{resolved_host_bonus}")
    if compatibility_summary:
        reason_parts.append(compatibility_summary)
    if school_network_risk >= 30:
        reason_parts.append(school_network_summary)

    return FinderCandidate(
        query=query,
        title=result_title,
        source_url=source_url,
        resolved_entry_url=detected_entry.index_url,
        entry_kind=detected_entry.entry_kind,
        build_kind=build_kind,
        source_page_url=source_page_url,
        suggested_output_name=suggested_output_name,
        score=score,
        confidence=confidence,
        confidence_label=confidence_label,
        compatibility_summary=compatibility_summary,
        school_network_risk=school_network_risk,
        school_network_risk_label=school_network_risk_label,
        school_network_summary=school_network_summary,
        reason=", ".join(reason_parts),
    )


def is_strong_candidate(candidate: FinderCandidate) -> bool:
    if candidate.confidence < 88:
        return False
    if candidate.entry_kind == "unity":
        if candidate.build_kind == "modern" and "missing" not in candidate.compatibility_summary:
            return True
        return candidate.confidence >= 93
    if candidate.entry_kind == "html":
        return candidate.confidence >= 92 and "html signals" in candidate.compatibility_summary
    return candidate.confidence >= 88


def should_stop_search(processed_queries: int, candidates: list[FinderCandidate]) -> bool:
    if not candidates:
        return False
    ranked = sorted(
        candidates,
        key=lambda item: (
            item.score,
            item.entry_kind == "unity",
            item.build_kind == "modern",
        ),
        reverse=True,
    )
    strong_candidates = [candidate for candidate in ranked if is_strong_candidate(candidate)]
    best = ranked[0]
    if processed_queries >= 2 and is_strong_candidate(best):
        return True
    if processed_queries >= 3 and len(strong_candidates) >= 2:
        return True
    if processed_queries >= 4 and best.confidence >= 84:
        return True
    return False


def find_best_source(
    game_name: str,
    *,
    max_results_per_query: int = 8,
    max_unique_candidates: int = 24,
) -> tuple[FinderCandidate, list[FinderCandidate]]:
    seen_urls: set[str] = set()
    candidates: list[FinderCandidate] = []
    consecutive_queries_without_candidate = 0

    processed_queries = 0
    for query in iter_search_queries(game_name):
        processed_queries += 1
        print(f"[finder] Search query: {query}")
        try:
            results = fetch_search_results(query, limit=max_results_per_query)
        except Exception as exc:
            print(f"[finder] Search failed for query: {exc}")
            continue
        pending_results: list[tuple[str, str]] = []
        for result_title, source_url in results:
            normalized_source_url = source_url.rstrip("/")
            if normalized_source_url in seen_urls:
                continue
            if not passes_result_prefilter(game_name, result_title, source_url):
                print(f"[finder] Prefiltered: {source_url}")
                continue
            seen_urls.add(normalized_source_url)
            print(f"[finder] Inspecting: {result_title or source_url}")
            pending_results.append((result_title, source_url))
            if len(seen_urls) >= max_unique_candidates:
                break

        if pending_results:
            accepted_in_query = 0
            max_workers = min(FINDER_EVAL_WORKERS, len(pending_results))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_result = {
                    executor.submit(evaluate_candidate, game_name, query, result_title, source_url): (
                        result_title,
                        source_url,
                    )
                    for result_title, source_url in pending_results
                }
                for future in concurrent.futures.as_completed(future_to_result):
                    _, source_url = future_to_result[future]
                    try:
                        candidate = future.result()
                    except Exception:
                        candidate = None
                    if candidate is None:
                        print(f"[finder] Rejected: {source_url}")
                        continue
                    candidates.append(candidate)
                    accepted_in_query += 1
                    print(
                        "[finder] Accepted "
                        f"score={candidate.score} "
                        f"kind={candidate.entry_kind} "
                        f"build={candidate.build_kind or 'n/a'} "
                        f"-> {candidate.source_url}"
                    )
            if accepted_in_query:
                consecutive_queries_without_candidate = 0
            else:
                consecutive_queries_without_candidate += 1
        else:
            consecutive_queries_without_candidate += 1

        if should_stop_search(processed_queries, candidates):
            print("[finder] Search stopped early after finding strong candidates.")
            break
        if not candidates and processed_queries >= 6 and consecutive_queries_without_candidate >= 6:
            print("[finder] Search stopped early after repeated empty or incompatible queries.")
            break
        if len(seen_urls) >= max_unique_candidates:
            break

    if not candidates:
        raise RuntimeError("No compatible supported source was found.")

    ranked = sorted(
        candidates,
        key=lambda item: (
            item.score,
            item.entry_kind == "unity",
            item.build_kind == "modern",
        ),
        reverse=True,
    )
    return ranked[0], ranked


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Search the web for a game name and pick the best source URL for "
            "unity_standalone.py."
        )
    )
    parser.add_argument("game_name", help="Game name to search for")
    parser.add_argument(
        "--max-results-per-query",
        type=int,
        default=8,
        help="Maximum parsed search results per query template (default: 8)",
    )
    parser.add_argument(
        "--max-unique-candidates",
        type=int,
        default=24,
        help="Maximum unique result URLs to inspect (default: 24)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = parse_args(argv)
    print(f"[finder] Looking for the best supported source for: {args.game_name}")
    try:
        best, ranked = find_best_source(
            args.game_name,
            max_results_per_query=max(1, args.max_results_per_query),
            max_unique_candidates=max(1, args.max_unique_candidates),
        )
    except Exception as exc:
        print(f"[finder] ERROR: {exc}")
        return 1

    print("[finder] Top candidates:")
    for index, candidate in enumerate(ranked[:5], start=1):
        print(
            f"[finder] {index}. score={candidate.score} "
            f"confidence={candidate.confidence_label}({candidate.confidence}) "
            f"school={candidate.school_network_risk_label}({candidate.school_network_risk}) "
            f"{candidate.entry_kind}/{candidate.build_kind or 'n/a'} "
            f"{candidate.source_url}"
        )

    payload = {
        "game_name": args.game_name,
        "best_url": best.source_url,
        "resolved_entry_url": best.resolved_entry_url,
        "source_page_url": best.source_page_url,
        "entry_kind": best.entry_kind,
        "build_kind": best.build_kind,
        "score": best.score,
        "confidence": best.confidence,
        "confidence_label": best.confidence_label,
        "compatibility_summary": best.compatibility_summary,
        "school_network_risk": best.school_network_risk,
        "school_network_risk_label": best.school_network_risk_label,
        "school_network_summary": best.school_network_summary,
        "suggested_output_name": best.suggested_output_name,
        "top_candidates": [asdict(candidate) for candidate in ranked[:5]],
    }
    print(f"[finder] Recommended source URL: {best.source_url}")
    print(f"[finder] School network risk: {best.school_network_summary}")
    print(f"[finder-result] {json.dumps(payload, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
