from flask import Flask, request, jsonify, render_template
import requests
import re
import traceback
import json
import os
import base64
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

# Ensure Playwright finds browsers (project-local dir from build.sh)
_proj_browsers = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pw-browsers')
if os.path.isdir(_proj_browsers):
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = _proj_browsers

from playwright.sync_api import sync_playwright
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

# --- Playwright on a dedicated single thread (sync API is not thread-safe) ---
_pw_local = threading.local()  # thread-local so new threads auto-create fresh state

def _ensure_browser():
    """Called ONLY on the dedicated PW thread. Auto-recovers if thread respawned."""
    try:
        if hasattr(_pw_local, 'browser') and _pw_local.browser.is_connected():
            return _pw_local.browser
    except Exception:
        pass  # browser ref is stale, recreate

    # Clean up any dead state
    try:
        if hasattr(_pw_local, 'browser'):
            _pw_local.browser.close()
    except Exception:
        pass
    try:
        if hasattr(_pw_local, 'pw'):
            _pw_local.pw.stop()
    except Exception:
        pass

    _pw_local.pw = sync_playwright().start()
    _pw_local.browser = _pw_local.pw.chromium.launch(
        headless=True,
        args=['--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage',
              '--disable-extensions']
    )
    print('[BROWSER] Persistent browser launched on PW thread', flush=True)
    return _pw_local.browser

# Single-thread executor ensures all Playwright calls happen on the SAME thread
_pw_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='playwright')

def _run_playwright_fetch(url):
    """Runs entirely on the dedicated Playwright thread."""
    t0 = time.time()
    context = None
    try:
        browser = _ensure_browser()
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
        )
        page = context.new_page()
        page.route('**/*.{png,jpg,jpeg,gif,svg,webp,woff,woff2,ttf,mp4,mp3}', lambda route: route.abort())
        response = page.goto(url, wait_until='domcontentloaded', timeout=15000)
        t_nav = time.time() - t0
        print(f"[FETCH] Navigation done ({t_nav:.2f}s)", flush=True)

        # Smart CF polling: check every 500ms up to 6s
        title = page.title() or ''
        if 'just a moment' in title.lower():
            print(f"[FETCH] CF challenge detected, polling...", flush=True)
            for i in range(12):
                page.wait_for_timeout(500)
                title = page.title() or ''
                if 'just a moment' not in title.lower():
                    print(f"[FETCH] CF cleared after {(i+1)*0.5:.1f}s", flush=True)
                    break

        html = page.content()
        final_url = page.url
        status = response.status if response else 0
        context.close()
        context = None
        elapsed = time.time() - t0
        print(f"[FETCH] Playwright done {url} -> {status} ({elapsed:.2f}s)", flush=True)
        return html, final_url, status
    except Exception as e:
        # Clean up context on failure
        if context:
            try: context.close()
            except: pass
        print(f"[FETCH] PW thread error, will recreate browser next call: {e}", flush=True)
        # Force browser recreation on next call
        try: _pw_local.browser.close()
        except: pass
        if hasattr(_pw_local, 'browser'):
            del _pw_local.browser
        raise

def _create_fast_session():
    """Create a requests.Session with connection pooling and retries."""
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=0.1, status_forcelist=[502, 503, 504])
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=retry)
    s.mount('http://', adapter)
    s.mount('https://', adapter)
    return s

# Helper functions for scraping

def unique_strings(lst):
    return list(set(lst))

def decode_html(value):
    if not value:
        return ''
    value = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), value, flags=re.I)
    value = re.sub(r'&#x([0-9a-f]+);', lambda m: chr(int(m.group(1), 16)), value, flags=re.I)
    return value.replace('&quot;', '"').replace('&#039;', "'").replace('&apos;', "'").replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')

def pick_first_match(html, expressions):
    for expr in expressions:
        match = expr.search(html)
        if match and match.group(1):
            return decode_html(match.group(1).strip())
    return ''

def absolute_url(value, base_url):
    if not value:
        return ''
    try:
        return urljoin(base_url, value)
    except:
        return ''

def extract_embed_urls(html, page_url):
    discovered = []
    patterns = [
        re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I),
        re.compile(r'(?:href|src|data-link|data-src)=["\']([^"\']*/svid/[^"\']+)["\']', re.I),
        re.compile(r'(?:href|src|data-link|data-src)=["\']([^"\']*/(?:e|v|evid)/[^"\']+)["\']', re.I),
        re.compile(r'(?:href|src|data-link|data-src)=["\']([^"\']+\.html?(?:\?[^"\']*)?)["\']', re.I),
        re.compile(r'["\']((?:https?:)?//[^"\'<>]+/svid/[a-z0-9_-]{6,})["\']', re.I),
        re.compile(r'["\']((?:https?:)?//[^"\'<>]+/(?:e|v|evid)/[a-z0-9_-]{6,})["\']', re.I),
        re.compile(r'["\']((?:https?:)?//[^"\'<>]+\.html?(?:\?[^"\']*)?)["\']', re.I)
    ]
    for pattern in patterns:
        for match in pattern.finditer(html):
            url = absolute_url(match.group(1), page_url)
            if url and is_likely_helper_url(url, page_url):
                discovered.append(url)
    return unique_strings(discovered)

def is_likely_helper_url(url, page_url):
    if not url:
        return False
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or '').lower()
        path = (parsed.path or '').lower()
        if any(h in hostname for h in ['youtube.com', 'youtu.be']):
            return False
        if 'iqsmartgames.com' in hostname or '/svid/' in path or '/evid/' in path or '/embed' in path or re.search(r'/e|v|evid/', path) or path.endswith('.html'):
            return True
        if any(h in hostname for h in ['multimovies', 'rpmhub.site', 'uns.bio', 'p2pplay.pro', 'smoothpre.com']):
            return True
    except:
        return False
    return False

def extract_server_items(html, player_url):
    html_text = html or ''
    if 'server-item' not in html_text:
        return [{
            'sourceKey': key,
            'serverName': key.upper(),
            'meta': '',
            'url': '',
            'preferred': True,
            'available': False
        } for key in ['smwh', 'rpmshre', 'upnshr', 'strmp2', 'flls']]
    server_items = []
    li_pattern = re.compile(r'<li\b([^>]*)>([\s\S]*?)<\/li>', re.I)
    for match in li_pattern.finditer(html_text):
        attrs = match.group(1) or ''
        body = match.group(2) or ''
        if 'server-item' not in attrs.lower():
            continue
        raw_url = pick_first_match(attrs, [re.compile(r'data-link=["\']([^"\']+)["\']', re.I)])
        if not raw_url:
            continue
        raw_key = pick_first_match(attrs, [
            re.compile(r'data-source-key=["\']([^"\']+)["\']', re.I),
            re.compile(r'data-sourcekey=["\']([^"\']+)["\']', re.I),
            re.compile(r'data-sourceKey=["\']([^"\']+)["\']', re.I)
        ])
        absolute = absolute_url(raw_url, player_url)
        inferred = infer_server_item_from_url(absolute) if not raw_key else None
        source_key = (raw_key or (inferred['sourceKey'] if inferred else '')).strip().lower()
        if not source_key:
            continue
        server_name = pick_first_match(body, [re.compile(r'<div[^>]+class=["\']server-name["\'][^>]*>([\s\S]*?)<\/div>', re.I)]) or (inferred['serverName'] if inferred else '') or source_key.upper()
        meta = pick_first_match(body, [re.compile(r'<div[^>]+class=["\']server-meta["\'][^>]*>([\s\S]*?)<\/div>', re.I)])
        server_items.append({
            'sourceKey': source_key,
            'serverName': re.sub(r'<[^>]+>', '', decode_html(server_name)).strip(),
            'meta': re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', decode_html(meta))).strip(),
            'url': absolute,
            'preferred': source_key in ['smwh', 'rpmshre', 'upnshr', 'strmp2', 'flls'],
            'available': True
        })
    by_key = {item['sourceKey']: item for item in server_items}
    preferred_keys = ['smwh', 'rpmshre', 'upnshr', 'strmp2', 'flls']
    result = []
    for key in preferred_keys:
        if key in by_key:
            entry = dict(by_key[key])
            entry['available'] = True
            result.append(entry)
        else:
            result.append({'sourceKey': key, 'serverName': key.upper(), 'meta': '', 'url': '', 'preferred': True, 'available': False})
    return result

def infer_server_item_from_url(player_url):
    if not player_url:
        return None
    try:
        hostname = urlparse(player_url).hostname.lower()
    except:
        return None
    mappings = [
        {'match': ['multimoviesshg.com'], 'sourceKey': 'smwh', 'serverName': 'SMWH'},
        {'match': ['multimovies.rpmhub.site'], 'sourceKey': 'rpmshre', 'serverName': 'RPMSHRE'},
        {'match': ['server1.uns.bio'], 'sourceKey': 'upnshr', 'serverName': 'UPNSHR'},
        {'match': ['multimovies.p2pplay.pro'], 'sourceKey': 'strmp2', 'serverName': 'STRMP2'},
        {'match': ['smoothpre.com'], 'sourceKey': 'flls', 'serverName': 'FLLS'}
    ]
    for mapping in mappings:
        if any(domain in hostname or hostname.endswith('.' + domain) for domain in mapping['match']):
            return {
                'sourceKey': mapping['sourceKey'],
                'serverName': mapping['serverName'],
                'meta': '',
                'url': player_url,
                'preferred': True,
                'available': True
            }
    return None

def extract_download_urls(html, base_url):
    return unique_strings([url for url in extract_any_urls(html, base_url) if 'ddn.iqsmartgames.com/file/' in url])

def extract_any_urls(value, base_url):
    text = decode_html(value or '')
    found = []
    patterns = [
        re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I),
        re.compile(r'(?:href|src|data-link|data-src)=["\']([^"\']+)["\']', re.I),
        re.compile(r'["\']((?:https?:)?\/\/[^"\'<>]+)["\']', re.I),
        re.compile(r'\b(https?:\/\/[^\s"\'<>]+)\b', re.I)
    ]
    for pattern in patterns:
        for match in pattern.finditer(text):
            url = absolute_url(re.sub(r'\s+', '', match.group(1)), base_url)
            if url:
                found.append(url)
    return unique_strings(found)

def extract_helper_sid(html):
    gdmrfid = pick_first_match(html, [re.compile(r'<input[^>]+id=["\']gdmrfid["\'][^>]+value=["\']([^"\']+)["\']', re.I), re.compile(r'<input[^>]+value=["\']([^"\']+)["\'][^>]+id=["\']gdmrfid["\']', re.I)])
    if gdmrfid:
        return gdmrfid
    return pick_first_match(html, [
        re.compile(r'const\s+sid\s*=\s*["\']([^"\']+)["\']', re.I),
        re.compile(r'let\s+sid\s*=\s*["\']([^"\']+)["\']', re.I),
        re.compile(r'var\s+sid\s*=\s*["\']([^"\']+)["\']', re.I),
        re.compile(r'sid\s*[:=]\s*["\']([^"\']+)["\']', re.I)
    ])

def create_download_url_candidates(embed_url, sid):
    candidates = []
    trimmed_sid = (sid or '').strip()
    if trimmed_sid:
        candidates.append(f'https://ddn.iqsmartgames.com/file/{trimmed_sid}')
    try:
        parsed = urlparse(embed_url)
        parts = [p for p in parsed.path.split('/') if p]
        if parts:
            candidates.append(f'https://ddn.iqsmartgames.com/file/{parts[-1]}')
    except:
        pass
    return unique_strings(candidates)

def normalize_server_items(server_items):
    by_key = {item['sourceKey']: item for item in server_items if item.get('sourceKey')}
    return [by_key.get(key, {
        'sourceKey': key,
        'serverName': key.upper(),
        'meta': '',
        'url': '',
        'preferred': True,
        'available': False
    }) for key in ['smwh', 'rpmshre', 'upnshr', 'strmp2', 'flls']]

class MockResponse:
    def __init__(self, status, ok, url, text):
        self.status_code = status
        self.ok = ok
        self.url = url
        self.text = text

def fetch_html_text(url, session, use_playwright=False):
    t0 = time.time()
    if not use_playwright:
        headers = {'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'}
        try:
            response = session.get(url, headers=headers, timeout=8)
            elapsed = time.time() - t0
            print(f"[FETCH] Requests {url} -> {response.status_code} ({elapsed:.2f}s)", flush=True)
            return {'response': response, 'html': response.text, 'finalUrl': response.url}
        except Exception as e:
            print(f"[FETCH] FAILED {url} ({time.time()-t0:.2f}s): {e}", flush=True)
            raise

    print(f"[FETCH] Playwright {url}...", flush=True)
    try:
        # Submit to the dedicated PW thread and wait for result
        future = _pw_executor.submit(_run_playwright_fetch, url)
        html, final_url, status = future.result(timeout=25)
        return {
            'response': MockResponse(status, status < 400, final_url, html),
            'html': html,
            'finalUrl': final_url
        }
    except Exception as e:
        print(f"[FETCH] Playwright FAILED {url} ({time.time()-t0:.2f}s): {e}", flush=True)
        raise



KNOWN_PROVIDER_HOSTS = [
    'multimoviesshg.com', 'multimovies.rpmhub.site',
    'server1.uns.bio', 'multimovies.p2pplay.pro', 'smoothpre.com'
]

def extract_known_provider_server_items(html, player_url):
    items = []
    normalized = html.replace('\\/', '/').replace('&quot;', '"')
    for url in extract_any_urls(normalized, player_url):
        try:
            hostname = (urlparse(url).hostname or '').lower()
            if any(hostname == h or hostname.endswith('.' + h) for h in KNOWN_PROVIDER_HOSTS):
                inferred = infer_server_item_from_url(url)
                if inferred and inferred.get('available'):
                    items.append(inferred)
        except:
            pass
    return items

def fetch_embedhelper_servers(embed_url, html, session, logger):
    sid = extract_helper_sid(html)
    if not sid:
        return []
    helper_url = urljoin(embed_url, '/embedhelper.php')
    parsed_embed = urlparse(embed_url)
    origin = f"{parsed_embed.scheme}://{parsed_embed.hostname}"
    current_domain = json.dumps(unique_strings(['multimovies.fyi', parsed_embed.hostname]))
    payload_data = {'sid': sid, 'UserFavSite': '', 'currentDomain': current_domain}
    query_string = re.sub(r'\s+', '+', '&'.join([f'{k}={v}' for k, v in payload_data.items()]))
    variants = [
        {'method': 'POST', 'url': helper_url, 'data': payload_data},
        {'method': 'GET', 'url': f"{helper_url}?{query_string}", 'data': None}
    ]
    
    for variant in variants:
        try:
            headers = {
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'accept': 'application/json, text/plain, */*',
                'origin': origin, 'referer': embed_url
            }
            if variant['method'] == 'POST':
                headers['content-type'] = 'application/x-www-form-urlencoded'
                resp = session.post(variant['url'], data=variant['data'], headers=headers, timeout=10)
            else:
                resp = session.get(variant['url'], headers=headers, timeout=10)
                
            if not resp.ok: continue
            payload = resp.json()
            site_urls = payload.get('siteUrls', {})
            mresult = payload.get('mresult', {})
            if isinstance(mresult, str):
                try: mresult = json.loads(base64.b64decode(mresult).decode('utf-8'))
                except: mresult = {}
            
            api_keys = payload.get('encryptedApiKeys', {})
            server_items = []
            for sk in api_keys:
                base = site_urls.get(sk, '')
                code = mresult.get(sk, '')
                if base and code:
                    server_items.append({
                        'sourceKey': sk.strip().lower(), 'serverName': sk.strip().upper(),
                        'meta': '', 'url': absolute_url(f"{base}{code}", embed_url),
                        'preferred': sk.strip().lower() in ['smwh', 'rpmshre', 'upnshr', 'strmp2', 'flls'],
                        'available': True
                    })
            if server_items:
                return normalize_server_items(server_items)
        except Exception as e:
            logger(f'[embedhelper] variant {variant["method"]} error: {e}')
    return []

def extract_loose_helper_urls(html, page_url):
    base = pick_first_match(html, [re.compile(r'\bplayer_base\s*=\s*["\']([^"\']+)["\']', re.I), re.compile(r'\bplayerBase\s*=\s*["\']([^"\']+)["\']', re.I)])
    base_url = absolute_url(base, page_url) or page_url
    discovered = []
    patterns = [
        re.compile(r'((?:https?:)?//[^\s"\'<>!]+/(?:svid|evid)/[a-z0-9_-]{6,})', re.I),
        re.compile(r'(/(?:svid|evid)/[a-z0-9_-]{6,})', re.I)
    ]
    for pattern in patterns:
        for match in pattern.finditer(html):
            url = absolute_url(match.group(1), base_url)
            if url and is_likely_helper_url(url, page_url):
                discovered.append(url)
    return unique_strings(discovered)

def fetch_iq_smart_games_evid_urls(embed_url, html, session, logger):
    final_id = pick_first_match(html, [re.compile(r'\bFinalID\s*=\s*["\']([^"\']+)["\']', re.I)])
    player_base = pick_first_match(html, [re.compile(r'\bplayer_base\s*=\s*["\']([^"\']+)["\']', re.I), re.compile(r'\bplayerBase\s*=\s*["\']([^"\']+)["\']', re.I)])
    if not final_id or not player_base:
        return []
    id_type = pick_first_match(html, [re.compile(r'\bidType\s*=\s*["\']([^"\']+)["\']', re.I)]) or 'imdbid'
    my_key = pick_first_match(html, [re.compile(r'\bmyKey\s*=\s*["\']([^"\']+)["\']', re.I)])
    api_url = pick_first_match(html, [re.compile(r'\bapi_url\s*=\s*["\']([^"\']+)["\']', re.I), re.compile(r'\bapiUrl\s*=\s*["\']([^"\']+)["\']', re.I)])
    
    logger(f"[iqsmart] config: final_id={final_id}, id_type={id_type}, player_base={player_base}, api_url={api_url}")
    
    eff_api_base = absolute_url(api_url or '', embed_url) or embed_url
    eff_player_base = absolute_url(player_base, embed_url) or player_base
    endpoint = f"{eff_api_base.rstrip('/')}/mymovieapi?{id_type}={final_id}{f'&key={my_key}' if my_key else ''}"
    
    try:
        resp = session.get(endpoint, headers={'referer': embed_url, 'user-agent': 'Mozilla/5.0'}, timeout=10)
        if not resp.ok: 
            logger(f"[iqsmart] API fail: {resp.status_code}")
            return []
        payload = resp.json()
        logger(f"[iqsmart] API success: {len(payload.get('data', [])) if isinstance(payload.get('data'), list) else 0} items found")
        items = payload.get('data', []) if isinstance(payload.get('data'), list) else []
        urls = []
        for item in items:
            slug = str(item.get('fileslug', '')).strip()
            if slug:
                urls.append({'url': f"{eff_player_base.rstrip('/')}/evid/{slug}", 'slug': slug})
        return urls
    except Exception as e:
        logger(f'[iqsmart] error: {e}')
        return []

def resolve_servers_from_player_page(embed_url, html, session, logger, depth=0, visited=None):
    if visited is None: visited = set()
    visit_key = f"{depth}:{embed_url}"
    if visit_key in visited or depth > 2: return normalize_server_items([])
    visited.add(visit_key)
    collected_servers = []
    def push_servers(items):
        collected_servers.extend([s for s in items if s.get('available') and s.get('url')])

    push_servers(extract_server_items(html, embed_url))
    push_servers(extract_known_provider_server_items(html, embed_url))
    push_servers(fetch_embedhelper_servers(embed_url, html, session, logger))

    nested = [u for u in extract_embed_urls(html, embed_url) if u and u != embed_url and not any(h in u for h in ['youtube.com', 'youtu.be'])]
    for nu in nested[:3]:
        try:
            r = fetch_html_text(nu, session, use_playwright=False)
            if r['response'].ok:
                push_servers(resolve_servers_from_player_page(r['finalUrl'] or nu, r['html'], session, logger, depth + 1, visited))
        except Exception as e: logger(f'[nested] {nu}: {e}')

    if not collected_servers:
        loose = [u for u in extract_loose_helper_urls(html, embed_url) if u != embed_url]
        for lu in loose[:4]:
            try:
                r = fetch_html_text(lu, session, use_playwright=False)
                if r['response'].ok:
                    push_servers(resolve_servers_from_player_page(r['finalUrl'] or lu, r['html'], session, logger, depth + 1, visited))
            except Exception as e: logger(f'[loose] {lu}: {e}')

    if not collected_servers and depth == 0:
        try:
            evid_results = fetch_iq_smart_games_evid_urls(embed_url, html, session, logger)
            for item in evid_results[:5]:
                try:
                    r = fetch_html_text(item['url'], session, use_playwright=False)
                    if r['response'].ok:
                        push_servers(resolve_servers_from_player_page(r['finalUrl'] or item['url'], r['html'], session, logger, depth + 1, visited))
                except Exception as e: logger(f'[evid] {item["url"]}: {e}')
        except Exception as e: logger(f'[iqsmart_evid] {e}')

    if not collected_servers:
        helper_sid = extract_helper_sid(html)
        candidates = create_download_url_candidates(embed_url, helper_sid)
        for cu in [c for c in candidates if 'ddn.iqsmartgames.com/file/' in c][:2]:
            try:
                r = fetch_html_text(cu, session, use_playwright=False)
                if r['response'].ok:
                    push_servers(resolve_servers_from_player_page(r['finalUrl'] or cu, r['html'], session, logger, depth + 1, visited))
            except Exception as e: logger(f'[ddn_candidate] {cu}: {e}')

    inferred = infer_server_item_from_url(embed_url)
    if inferred: push_servers([inferred])
    return normalize_server_items(collected_servers)

def fetch_ajax_embed_urls(page_html, page_url, session, logger):

    dt_match = re.search(r'var\s+dtAjax\s*=\s*(\{[\s\S]*?\})\s*;', page_html, re.I)
    if not dt_match:
        return []
    try:
        config = json.loads(dt_match.group(1))
    except:
        return []
    ajax_url = config.get('url', '')
    if not ajax_url or config.get('play_method', '').strip().lower() != 'admin_ajax':
        return []
    ajax_url = absolute_url(ajax_url, page_url)
    parsed_page = urlparse(page_url)
    origin = f'{parsed_page.scheme}://{parsed_page.hostname}'
    options = []
    for tag in re.findall(r'<li\b[^>]*class=["\'][^"\']*dooplay_player_option[^"\']*["\'][^>]*>', page_html, re.I):
        p = re.search(r'data-post=["\']([^"\']+)["\']', tag, re.I)
        n = re.search(r'data-nume=["\']([^"\']+)["\']', tag, re.I)
        t = re.search(r'data-type=["\']([^"\']+)["\']', tag, re.I)
        if p and n and t:
            options.append({'post': p.group(1), 'nume': n.group(1), 'type': t.group(1)})
    embed_urls = []
    for opt in options:
        if opt['nume'].lower() == 'trailer':
            continue
        try:
            resp = session.post(ajax_url, data={
                'action': 'doo_player_ajax', 'post': opt['post'],
                'nume': opt['nume'], 'type': opt['type']
            }, headers={
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'accept': 'application/json, text/plain, */*',
                'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'origin': origin, 'referer': page_url, 'x-requested-with': 'XMLHttpRequest'
            }, timeout=10)
            payload = resp.json()
            raw_embed = (payload.get('embed_url') or '').strip()
            if not raw_embed or 'youtube' in raw_embed or str(payload.get('type','')).lower() == 'trailer':
                continue
            url = absolute_url(raw_embed, page_url)
            if url:
                embed_urls.append(url)
        except Exception as e:
            logger(f'[ajax] {opt}: {e}')
    return unique_strings(embed_urls)



def _process_single_embed(embed_url, session, logger):
    """Process one embed URL — returns (player_result, downloads) or None."""
    t0 = time.time()
    try:
        result = fetch_html_text(embed_url, session, use_playwright=False)
        effective_url = result['finalUrl'] or embed_url
        servers = resolve_servers_from_player_page(effective_url, result['html'], session, logger)
        avail = [s for s in servers if s.get('available')]
        downloads = extract_download_urls(result['html'], effective_url)
        elapsed = time.time() - t0
        print(f"[EMBED] {embed_url} -> {len(avail)} servers ({elapsed:.2f}s)", flush=True)
        player = {'playerUrl': effective_url, 'servers': servers} if avail else None
        return player, downloads
    except Exception as e:
        print(f"[EMBED] FAIL {embed_url} ({time.time()-t0:.2f}s): {e}", flush=True)
        return None, []

def scrape_from_page(html, page_url, session):
    t_start = time.time()
    logger = lambda *args: print(f"[SCRAPE] {args[0] if args else ''}", flush=True)

    # --- Phase 1: Extract embed URLs (static + ajax concurrently) ---
    t0 = time.time()
    static_embed_urls = extract_embed_urls(html, page_url)
    print(f"[TIMING] Static embed extraction: {time.time()-t0:.2f}s -> {len(static_embed_urls)} URLs", flush=True)

    t0 = time.time()
    ajax_embed_urls = fetch_ajax_embed_urls(html, page_url, session, logger)
    print(f"[TIMING] Ajax embed extraction: {time.time()-t0:.2f}s -> {len(ajax_embed_urls)} URLs", flush=True)

    embed_urls = unique_strings(static_embed_urls + ajax_embed_urls)
    print(f"[TIMING] Total unique embeds: {len(embed_urls)}", flush=True)

    direct_servers = extract_server_items(html, page_url)
    player_results = []
    if any(s.get('available') for s in direct_servers):
        player_results.append({'playerUrl': page_url, 'servers': direct_servers})

    recovered_downloads = extract_download_urls(html, page_url)

    # --- Phase 2: Process ALL embeds concurrently ---
    t0 = time.time()
    if embed_urls:
        with ThreadPoolExecutor(max_workers=min(6, len(embed_urls))) as pool:
            futures = {pool.submit(_process_single_embed, eu, session, logger): eu for eu in embed_urls}
            for future in as_completed(futures):
                player, downloads = future.result()
                if player:
                    player_results.append(player)
                recovered_downloads.extend(downloads)
    print(f"[TIMING] All embeds processed: {time.time()-t0:.2f}s", flush=True)

    all_servers = normalize_server_items([s for p in player_results for s in p.get('servers', []) if s.get('available') and s.get('url')])
    total = time.time() - t_start
    avail = len([s for s in all_servers if s.get('available')])
    print(f"[TIMING] scrape_from_page total: {total:.2f}s | {avail} servers found", flush=True)
    return {
        'embedUrls': embed_urls,
        'servers': all_servers,
        'downloads': unique_strings(recovered_downloads),
        'playerPages': [p['playerUrl'] for p in player_results]
    }


@app.route('/scrape', methods=['POST'])
def scrape():
    t_total = time.time()
    print(f"\n{'='*60}", flush=True)
    print(f"[SERVER] Scrape request received at {time.strftime('%H:%M:%S')}", flush=True)
    url = request.json.get('url')
    if not url:
        return jsonify({'error': 'No url provided'}), 400
    try:
        session = _create_fast_session()

        # Phase 1: Fetch main page with Playwright
        t0 = time.time()
        result = fetch_html_text(url, session, use_playwright=True)
        print(f"[TIMING] Main page fetch: {time.time()-t0:.2f}s", flush=True)

        if not result['response'].ok:
            return jsonify({'error': f'Failed to fetch page: {result["response"].status_code}'}), 400

        # Phase 2: Scrape embeds & servers
        t0 = time.time()
        scraped = scrape_from_page(result['html'], url, session)
        print(f"[TIMING] Scrape processing: {time.time()-t0:.2f}s", flush=True)

        total = time.time() - t_total
        avail = len([s for s in scraped.get('servers', []) if s.get('available')])
        print(f"[TIMING] === TOTAL REQUEST: {total:.2f}s | {avail} servers ===", flush=True)
        print(f"{'='*60}\n", flush=True)
        return jsonify(scraped)
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[scrape] exception ({time.time()-t_total:.2f}s):', tb)
        return jsonify({'error': str(e), 'traceback': tb}), 500



@app.route('/ping', methods=['GET'])
def ping():
    return "chamkila chetan!", 200
@app.route('/')
def home():
    print("[SERVER] Homepage visited", flush=True)
    return render_template('index.html')
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"[SERVER] Starting server on port {port}...", flush=True)
    app.run(host='0.0.0.0', port=port)
