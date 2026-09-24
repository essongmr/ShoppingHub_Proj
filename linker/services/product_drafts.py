import html
import ipaddress
import json
import re
import socket
from io import BytesIO
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q
from PIL import Image

from linker.models import CreatorStore, Linker, Marketplace


class ProductDraftError(ValueError):
    pass


INPOCK_REDIRECT_HOSTS = {'link.inpock.co.kr', 'inpk.link', 'www.inpk.link'}
SHOPPINGHUB_STORE_HOSTS = {'shophub.kr', 'www.shophub.kr', 'shoppinghub.kr', 'www.shoppinghub.kr'}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
REQUEST_HEADERS = {'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.7'}
INPOCK_IMAGE_CDN = 'https://d13k46lqgoj3d6.cloudfront.net/'
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PAGE_BYTES = 2 * 1024 * 1024


@dataclass
class ProductDraft:
    title: str = ''
    destination_url: str = ''
    thumbnail_url: str = ''
    description: str = ''
    product_no: str = ''
    marketplace_code: str = ''
    source_identifier: str = ''
    source_type: str = ''
    source_url: str = ''

    def as_initial(self):
        return {
            'title': self.title,
            'destination_url': self.destination_url,
            'thumbnail_url': self.thumbnail_url,
            'description': self.description,
            'product_no': self.product_no,
        }

    def as_dict(self):
        return {
            'title': self.title,
            'destination_url': self.destination_url,
            'thumbnail_url': self.thumbnail_url,
            'description': self.description,
            'product_no': self.product_no,
            'marketplace_code': self.marketplace_code,
            'source_identifier': self.source_identifier,
            'source_type': self.source_type,
            'source_url': self.source_url,
        }

    @classmethod
    def from_dict(cls, value):
        data = value or {}
        return cls(
            title=data.get('title', ''),
            destination_url=data.get('destination_url', ''),
            thumbnail_url=data.get('thumbnail_url', ''),
            description=data.get('description', ''),
            product_no=data.get('product_no', ''),
            marketplace_code=data.get('marketplace_code', ''),
            source_identifier=data.get('source_identifier', ''),
            source_type=data.get('source_type', ''),
            source_url=data.get('source_url', ''),
        )


class _ProductLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._stack = []
        self._elements = []
        self._ignored_depth = 0

    def _current_block_id(self):
        for _tag, block_id in reversed(self._elements):
            if block_id:
                return block_id
        return ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        tag = tag.lower()
        block_id = attrs.get('data-preview-block-id') or self._current_block_id()
        self._elements.append((tag, block_id))
        if tag in {'style', 'script', 'noscript', 'template'}:
            self._ignored_depth += 1
        if tag == 'a' and attrs.get('href'):
            self._stack.append({'href': attrs.get('href', ''), 'text': '', 'image': '', 'block_id': block_id})
        if self._stack and not self._ignored_depth:
            image = image_from_attrs(attrs)
            if image and not self._stack[-1]['image']:
                self._stack[-1]['image'] = image

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self._elements:
            self._elements.pop()
        if tag.lower() in {'style', 'script', 'noscript', 'template'} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data):
        if self._stack and not self._ignored_depth and not is_css_text(data):
            self._stack[-1]['text'] += data

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {'style', 'script', 'noscript', 'template'} and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == 'a' and self._stack:
            item = self._stack.pop()
            item['text'] = clean_product_title(item['text'])
            self.links.append(item)
        for index in range(len(self._elements) - 1, -1, -1):
            if self._elements[index][0] == tag:
                del self._elements[index:]
                break


def is_css_text(value: str) -> bool:
    text = (value or '').strip()
    if not text:
        return True
    lowered = text.lower()
    css_markers = (
        'background-color:',
        'box-shadow:',
        'transition:',
        '-webkit-',
        'border-radius:',
        'var(--',
        'display:',
        'position:',
    )
    if lowered.startswith(('.css-', '.ids-', '@media', '@keyframes')):
        return True
    if '{' in lowered and '}' in lowered and any(marker in lowered for marker in css_markers):
        return True
    if sum(lowered.count(marker) for marker in css_markers) >= 2:
        return True
    if lowered.count(':') >= 3 and lowered.count(';') >= 2 and any(marker in lowered for marker in css_markers):
        return True
    return False


def clean_product_title(value: str) -> str:
    parts = [part for part in re.split(r'\s+', html.unescape(value or '')) if not is_css_text(part)]
    title = re.sub(r'\s+', ' ', ' '.join(parts)).strip()
    return '' if is_css_text(title) else title


def srcset_url(value: str) -> str:
    candidates = []
    for item in (value or '').split(','):
        url = item.strip().split(' ', 1)[0]
        if url:
            candidates.append(url)
    return candidates[-1] if candidates else ''


def background_image_url(value: str) -> str:
    match = re.search(r'background(?:-image)?\s*:\s*[^;]*url\((?P<quote>["\']?)(?P<url>.*?)(?P=quote)\)', value or '', re.I)
    return match.group('url').strip() if match else ''


def is_product_image_candidate(value: str) -> bool:
    url = (value or '').strip()
    if not url or url.startswith(('data:', 'blob:', '#')):
        return False
    lowered = url.lower()
    blocked = ('favicon', 'logo', 'sprite', 'placeholder', 'blank.', 'transparent')
    return not any(marker in lowered for marker in blocked)


def image_from_attrs(attrs: dict) -> str:
    candidates = [
        attrs.get('src', ''),
        attrs.get('data-src', ''),
        attrs.get('data-lazy-src', ''),
        srcset_url(attrs.get('srcset', '')),
        background_image_url(attrs.get('style', '')),
    ]
    for candidate in candidates:
        if is_product_image_candidate(candidate):
            return candidate
    return ''


def normalize_inpock_image_url(value: str) -> str:
    path = (value or '').strip().lstrip('/')
    if not path or not is_product_image_candidate(path):
        return ''
    if re.match(r'^https?://', path, re.I):
        return path
    if path.startswith('images/'):
        path = path[len('images/'):]
    return urljoin(INPOCK_IMAGE_CDN, path)


def inpock_hydration_images(html_text: str, base_url: str) -> dict[str, str]:
    match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(?P<value>.*?)</script>', html_text or '', re.I | re.S)
    if not match:
        return {}
    try:
        data = json.loads(html.unescape(match.group('value')))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    blocks = data.get('props', {}).get('pageProps', {}).get('blocks', [])
    if not isinstance(blocks, list):
        return {}
    images = {}
    for block in blocks:
        if not isinstance(block, dict) or block.get('block_type') != 'link' or not block.get('image'):
            continue
        if block.get('style') not in {None, '', 'thumbnail'}:
            continue
        image_url = normalize_inpock_image_url(block.get('image', ''))
        if not image_url:
            continue
        block_id = str(block.get('id') or '')
        if block_id:
            images[f'id:{block_id}'] = image_url
        block_url = block.get('url') or ''
        if block_url:
            images[f'url:{urljoin(base_url, block_url)}'] = image_url
            images[f'href:{block_url}'] = image_url
        title = clean_product_title(block.get('title', ''))
        if title:
            images.setdefault(f'title:{title}', image_url)
    return images


def hydration_image_for_item(item: dict, title: str, target: str, hydration_images: dict[str, str]) -> str:
    keys = [
        f'id:{item.get("block_id", "")}',
        f'url:{target}',
        f'href:{item.get("href", "")}',
        f'title:{title}',
    ]
    for key in keys:
        image = hydration_images.get(key)
        if image:
            return image
    return ''


def ensure_url_scheme(url: str) -> str:
    value = (url or '').strip()
    if value and not re.match(r'^https?://', value, re.I):
        value = 'https://' + value
    return value


def assert_public_url(url: str) -> str:
    value = ensure_url_scheme(url)
    parsed = urlparse(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise ProductDraftError('http 또는 https 공개 URL을 입력하세요.')
    host = parsed.hostname.lower()
    if host in {'localhost', 'localhost.localdomain'}:
        raise ProductDraftError('로컬 주소는 사용할 수 없습니다.')
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == 'https' else 80))
    except socket.gaierror as exc:
        raise ProductDraftError('도메인을 확인할 수 없습니다.') from exc
    for entry in addresses:
        ip = ipaddress.ip_address(entry[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ProductDraftError('내부 네트워크 주소는 사용할 수 없습니다.')
    return value


def parse_coupang_product_id(url: str) -> str:
    parsed = urlparse(url or '')
    match = re.search(r'/(?:vp/)?products/(\d+)', parsed.path)
    if match:
        return match.group(1)
    query = parse_qs(parsed.query)
    for key in ('pageKey', 'productId', 'itemId', 'vendorItemId', 'ctag'):
        value = (query.get(key) or [''])[0]
        if value.isdigit():
            return value
    return ''


def is_inpock_redirect_url(url: str) -> bool:
    parsed = urlparse(url or '')
    return (parsed.hostname or '').lower() in INPOCK_REDIRECT_HOSTS and parsed.path.startswith('/api/r/')


def is_inpock_host(url: str) -> bool:
    return (urlparse(url or '').hostname or '').lower() in INPOCK_REDIRECT_HOSTS


def is_shoppinghub_store_url(url: str) -> bool:
    parsed = urlparse(url or '')
    host = (parsed.hostname or '').lower()
    public_base_host = (
        urlparse(getattr(settings, 'SHOPPINGHUB_PUBLIC_BASE_URL', '') or '').hostname
        or ''
    ).lower()
    store_hosts = set(SHOPPINGHUB_STORE_HOSTS)
    if public_base_host:
        store_hosts.add(public_base_host)
    path = re.sub(r'/{2,}', '/', parsed.path or '/')
    return host in store_hosts and path.startswith('/s/')


def resolve_inpock_redirect(url: str, max_redirects: int = 6) -> tuple[str, str]:
    current = url
    first_external = ''
    final_url = url
    seen = set()
    try:
        for _ in range(max_redirects):
            if current in seen:
                break
            seen.add(current)
            response = requests.get(current, timeout=10, headers=REQUEST_HEADERS, allow_redirects=False, stream=True)
            try:
                status = getattr(response, 'status_code', 0)
                location = response.headers.get('location') if getattr(response, 'headers', None) else None
                final_url = response.url or current
            finally:
                close = getattr(response, 'close', None)
                if callable(close):
                    close()
            if status not in REDIRECT_STATUSES or not location:
                break
            next_url = urljoin(current, location)
            if not first_external and not is_inpock_host(next_url):
                first_external = next_url
            if (urlparse(next_url).hostname or '').lower() == 'link.coupang.com':
                first_external = next_url
                final_url = follow_redirect_target(next_url, max_redirects=max_redirects)
                break
            current = next_url
        return first_external, final_url
    except requests.RequestException:
        return '', ''


def follow_redirect_target(url: str, max_redirects: int = 5) -> str:
    current = url
    seen = set()
    try:
        for _ in range(max_redirects):
            if current in seen:
                return current
            seen.add(current)
            response = requests.get(current, timeout=10, headers=REQUEST_HEADERS, allow_redirects=False, stream=True)
            try:
                status = getattr(response, 'status_code', 0)
                location = response.headers.get('location') if getattr(response, 'headers', None) else None
                final_url = response.url or current
            finally:
                close = getattr(response, 'close', None)
                if callable(close):
                    close()
            if status not in REDIRECT_STATUSES or not location:
                return final_url
            current = urljoin(current, location)
        return current
    except requests.RequestException:
        return ''


def normalized_product_url(url: str) -> str:
    parsed = urlparse(ensure_url_scheme(url))
    host = (parsed.hostname or '').lower()
    path = re.sub(r'/{2,}', '/', parsed.path or '/').rstrip('/') or '/'
    if host in {'www.coupang.com', 'm.coupang.com', 'coupang.com', 'link.coupang.com', 'coupa.ng'}:
        product_id = parse_coupang_product_id(url)
        if product_id:
            return f'coupang:{product_id}'
    return f'{parsed.scheme.lower()}://{host}{path}'


def draft_identity_candidates(draft: ProductDraft) -> set[str]:
    values = set()
    if draft.destination_url:
        values.add(normalized_product_url(draft.destination_url))
    source = (draft.source_identifier or '').strip()
    if source:
        values.add(source)
        if source.isdigit() and (
            draft.marketplace_code == 'CP'
            or 'coupang' in (urlparse(ensure_url_scheme(draft.destination_url)).hostname or '').lower()
        ):
            values.add(f'coupang:{source}')
    return values


def marketplace_code_for_url(url: str) -> str:
    host = (urlparse(ensure_url_scheme(url)).hostname or '').lower()
    if 'coupang' in host or host == 'coupa.ng':
        return 'CP'
    if 'naver' in host:
        return 'NV'
    return 'DM'


def _meta(html_text: str, *names: str) -> str:
    for name in names:
        pattern = rf'<meta[^>]+(?:property|name)=["\']{re.escape(name)}["\'][^>]+content=["\'](?P<value>.*?)["\']'
        match = re.search(pattern, html_text, re.I | re.S)
        if match:
            return html.unescape(match.group('value')).strip()
    return ''


def _title(html_text: str) -> str:
    match = re.search(r'<title[^>]*>(?P<value>.*?)</title>', html_text, re.I | re.S)
    return html.unescape(re.sub(r'\s+', ' ', match.group('value')).strip()) if match else ''


def fetch_product_page_draft(url: str) -> ProductDraft:
    source_url = assert_public_url(url)
    try:
        response = requests.get(
            source_url,
            timeout=15,
            headers=REQUEST_HEADERS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ProductDraftError(f'상품페이지를 가져오지 못했습니다: {exc}') from exc
    final_url = response.url or source_url
    text = response.text[:2_000_000]
    title = _meta(text, 'og:title', 'twitter:title') or _title(text)
    image = _meta(text, 'og:image', 'twitter:image')
    description = _meta(text, 'og:description', 'description')[:500]
    if not title:
        raise ProductDraftError('상품명을 찾지 못했습니다. 직접 입력해 주세요.')
    return ProductDraft(
        title=title[:240],
        destination_url=final_url[:1000],
        thumbnail_url=urljoin(final_url, image)[:1000] if image else '',
        description=description,
        product_no=parse_coupang_product_id(final_url),
        marketplace_code=marketplace_code_for_url(final_url),
        source_identifier=normalized_product_url(final_url),
    )


def _html_attributes(tag: str) -> dict[str, str]:
    return {
        name.lower(): html.unescape(value)
        for name, _quote, value in re.findall(r'([\w:-]+)\s*=\s*(["\'])(.*?)\2', tag or '', re.S)
    }


def _first_tag_text(html_text: str, tag: str) -> str:
    match = re.search(rf'<{tag}\b[^>]*>(?P<value>.*?)</{tag}>', html_text or '', re.I | re.S)
    if not match:
        return ''
    return clean_product_title(re.sub(r'<[^>]+>', ' ', match.group('value')))


def _shoppinghub_detail_image(html_text: str, base_url: str) -> str:
    for tag in re.findall(r'<img\b[^>]*>', html_text or '', re.I | re.S):
        attrs = _html_attributes(tag)
        if 'detail-image' in attrs.get('class', '') and attrs.get('src'):
            return urljoin(base_url, attrs['src'])[:1000]
    return ''


def _shoppinghub_outbound_url(html_text: str, base_url: str) -> str:
    for tag in re.findall(r'<a\b[^>]*>', html_text or '', re.I | re.S):
        href = _html_attributes(tag).get('href', '')
        target = urljoin(base_url, href)
        if re.search(r'/out/\d+/?$', urlparse(target).path):
            return target
    return ''


def _shoppinghub_source_url(url: str) -> str:
    source_url = assert_public_url(url)
    if not is_shoppinghub_store_url(source_url):
        raise ProductDraftError('ShoppingHub 운영 Store 주소를 입력하세요.')
    return source_url


def _get_shoppinghub_page(url: str, follow_source_redirects: bool = True):
    source_url = _shoppinghub_source_url(url)
    try:
        for _ in range(4):
            response = requests.get(source_url, timeout=15, headers=REQUEST_HEADERS, allow_redirects=False, stream=True)
            location = (getattr(response, 'headers', {}) or {}).get('location')
            if follow_source_redirects and getattr(response, 'status_code', 0) in REDIRECT_STATUSES and location:
                close = getattr(response, 'close', None)
                if callable(close):
                    close()
                source_url = _shoppinghub_source_url(urljoin(source_url, location))
                continue
            response.raise_for_status()
            return response, source_url
    except requests.RequestException as exc:
        raise ProductDraftError(f'ShoppingHub Store 페이지를 가져오지 못했습니다: {exc}') from exc
    raise ProductDraftError('ShoppingHub Store 리다이렉트가 너무 많습니다.')


def _bounded_response_text(response) -> str:
    headers = getattr(response, 'headers', {}) or {}
    if int(headers.get('Content-Length', '0') or 0) > MAX_PAGE_BYTES:
        raise ProductDraftError('ShoppingHub Store 페이지가 너무 큽니다.')
    iterator = getattr(response, 'iter_content', None)
    if not callable(iterator):
        text = getattr(response, 'text', '')
        if len(text.encode('utf-8')) > MAX_PAGE_BYTES:
            raise ProductDraftError('ShoppingHub Store 페이지가 너무 큽니다.')
        return text
    chunks = []
    total = 0
    for chunk in iterator(64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_PAGE_BYTES:
            raise ProductDraftError('ShoppingHub Store 페이지가 너무 큽니다.')
        chunks.append(chunk)
    encoding = getattr(response, 'encoding', None) or 'utf-8'
    return b''.join(chunks).decode(encoding, errors='replace')


def _resolve_shoppinghub_outbound(url: str) -> str:
    if not url:
        return ''
    response, source_url = _get_shoppinghub_page(url, follow_source_redirects=False)
    try:
        if getattr(response, 'status_code', 0) not in REDIRECT_STATUSES:
            return ''
        location = (getattr(response, 'headers', {}) or {}).get('location')
        if not location:
            return ''
        destination_url = urljoin(source_url, location)
        return assert_public_url(destination_url)
    finally:
        close = getattr(response, 'close', None)
        if callable(close):
            close()


def scan_shoppinghub_store_drafts(store_url: str, limit: int = 50) -> list[ProductDraft]:
    response, base_url = _get_shoppinghub_page(store_url)
    try:
        parser = _ProductLinkParser()
        parser.feed(_bounded_response_text(response))
    finally:
        close = getattr(response, 'close', None)
        if callable(close):
            close()
    drafts = []
    seen_codes = set()
    for item in parser.links:
        detail_url = urljoin(base_url, item.get('href', ''))
        parsed = urlparse(detail_url)
        product_code = (parse_qs(parsed.query).get('product') or [''])[0].strip()
        if not product_code or product_code in seen_codes or not is_shoppinghub_store_url(detail_url):
            continue
        seen_codes.add(product_code)
        detail_response, detail_url = _get_shoppinghub_page(detail_url)
        try:
            detail_html = _bounded_response_text(detail_response)
        finally:
            close = getattr(detail_response, 'close', None)
            if callable(close):
                close()
        destination_url = _resolve_shoppinghub_outbound(_shoppinghub_outbound_url(detail_html, detail_url))
        title = _first_tag_text(detail_html, 'h1') or (item.get('text') or '').strip()
        description = _first_tag_text(detail_html, 'p')[:500]
        if not title:
            continue
        drafts.append(ProductDraft(
            title=title[:240],
            destination_url=destination_url[:1000],
            thumbnail_url=_shoppinghub_detail_image(detail_html, detail_url),
            description=description,
            product_no=product_code[:40],
            marketplace_code=marketplace_code_for_url(destination_url),
            source_identifier=f'shoppinghub:{urlparse(base_url).path.strip("/")}:{product_code}',
            source_type='shoppinghub',
            source_url=detail_url[:1000],
        ))
        if len(drafts) >= limit:
            break
    if not drafts:
        raise ProductDraftError('공개된 ShoppingHub 상품을 찾지 못했습니다.')
    return drafts


def scan_existing_service_product_drafts(source_url: str, limit: int = 50) -> list[ProductDraft]:
    url = assert_public_url(source_url)
    return scan_shoppinghub_store_drafts(url, limit=limit) if is_shoppinghub_store_url(url) else scan_inpock_product_drafts(url, limit=limit)


def scan_inpock_product_drafts(profile_url: str, limit: int = 50) -> list[ProductDraft]:
    url = assert_public_url(profile_url)
    host = (urlparse(url).hostname or '').lower()
    if 'inpock' not in host and host not in {'inpk.link', 'www.inpk.link'}:
        raise ProductDraftError('현재 지원하지 않는 서비스 주소입니다.')
    try:
        response = requests.get(
            url,
            timeout=15,
            headers=REQUEST_HEADERS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ProductDraftError(f'기존 서비스 페이지를 가져오지 못했습니다: {exc}') from exc
    parser = _ProductLinkParser()
    parser.feed(response.text[:2_000_000])
    base_url = response.url or url
    hydration_images = inpock_hydration_images(response.text[:2_000_000], base_url)
    drafts = []
    seen = set()
    for item in parser.links:
        target = urljoin(base_url, item.get('href', ''))
        destination_url = target
        identity_url = target
        parsed = urlparse(target)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            continue
        if parsed.hostname.lower() in {host, 'link.inpock.co.kr', 'inpk.link', 'www.inpk.link'} and not parsed.path.startswith('/api/r/'):
            continue
        title = (item.get('text') or '').strip()
        if len(title) < 2:
            continue
        image = item.get('image', '') or hydration_image_for_item(item, title, target, hydration_images)
        thumbnail_url = urljoin(base_url, image)[:1000] if image else ''
        if is_inpock_redirect_url(target):
            partner_url, final_url = resolve_inpock_redirect(target)
            if not partner_url:
                key = normalized_product_url(target)
                if key in seen:
                    continue
                seen.add(key)
                drafts.append(ProductDraft(
                    title=title[:240],
                    destination_url='',
                    thumbnail_url=thumbnail_url,
                    product_no=str(len(drafts) + 1).zfill(3),
                    marketplace_code='',
                    source_identifier=key,
                ))
                if len(drafts) >= limit:
                    break
                continue
            destination_url = partner_url
            identity_url = final_url or partner_url
        key = normalized_product_url(identity_url)
        if key in seen:
            continue
        seen.add(key)
        drafts.append(ProductDraft(
            title=title[:240],
            destination_url=destination_url[:1000],
            thumbnail_url=thumbnail_url,
            product_no=parse_coupang_product_id(identity_url) or str(len(drafts) + 1).zfill(3),
            marketplace_code=marketplace_code_for_url(identity_url),
            source_identifier=key,
        ))
        if len(drafts) >= limit:
            break
    if not drafts:
        raise ProductDraftError('가져올 상품 링크를 찾지 못했습니다.')
    return drafts


def has_existing_product(owner, draft: ProductDraft, store=None) -> bool:
    identities = draft_identity_candidates(draft)
    products = Linker.objects.filter(owner=owner)
    if store is not None:
        first_store_id = CreatorStore.objects.filter(owner=owner).order_by('created_at', 'id').values_list('pk', flat=True).first()
        products = products.filter(Q(store=store) | Q(store__isnull=True) if store.pk == first_store_id else Q(store=store))
    for url in products.values_list('destination_url', flat=True):
        if normalized_product_url(url) in identities:
            return True
    return False


def validate_product_draft(draft: ProductDraft):
    if not draft.title.strip():
        raise ProductDraftError('상품명이 필요합니다.')
    if not draft.destination_url.strip():
        raise ProductDraftError('상품 링크가 필요합니다.')
    assert_public_url(draft.destination_url)


def marketplace_for_draft(draft: ProductDraft):
    code = draft.marketplace_code or marketplace_code_for_url(draft.destination_url)
    marketplace = Marketplace.objects.filter(code=code, active=True).first()
    if marketplace:
        return marketplace
    return Marketplace.objects.filter(active=True).order_by('sort_order', 'id').first()


def next_product_no(owner, preferred: str = '', store=None) -> str:
    value = (preferred or '').strip()
    products = Linker.objects.filter(owner=owner, store=store) if store is not None else Linker.objects.filter(owner=owner)
    if store is not None:
        first_store_id = CreatorStore.objects.filter(owner=owner).order_by('created_at', 'id').values_list('pk', flat=True).first()
        if store.pk == first_store_id:
            products = Linker.objects.filter(owner=owner).filter(Q(store=store) | Q(store__isnull=True))
    if value and not products.filter(product_no=value).exists():
        return value[:40]
    numbers = []
    for current in products.values_list('product_no', flat=True):
        if str(current).isdigit():
            numbers.append(int(current))
    return str((max(numbers) if numbers else 0) + 1).zfill(3)


def copy_shoppinghub_draft_image(product: Linker, draft: ProductDraft) -> bool:
    if draft.source_type != 'shoppinghub' or not draft.thumbnail_url:
        return True
    try:
        current_url = assert_public_url(draft.thumbnail_url)
        for _ in range(4):
            response = requests.get(current_url, timeout=10, headers=REQUEST_HEADERS, allow_redirects=False, stream=True)
            try:
                status = getattr(response, 'status_code', 0)
                location = (getattr(response, 'headers', {}) or {}).get('location')
                if status in REDIRECT_STATUSES and location:
                    current_url = assert_public_url(urljoin(current_url, location))
                    continue
                response.raise_for_status()
                content_type = (getattr(response, 'headers', {}) or {}).get('Content-Type', '').split(';', 1)[0].lower()
                if content_type not in {'image/jpeg', 'image/png', 'image/webp'}:
                    raise ProductDraftError('지원하지 않는 상품 이미지 형식입니다.')
                if int((getattr(response, 'headers', {}) or {}).get('Content-Length', '0') or 0) > MAX_IMAGE_BYTES:
                    raise ProductDraftError('상품 이미지가 너무 큽니다.')
                content = b''.join(response.iter_content(64 * 1024))
                if not content or len(content) > MAX_IMAGE_BYTES:
                    raise ProductDraftError('상품 이미지를 저장할 수 없습니다.')
                image = Image.open(BytesIO(content))
                image.verify()
                extension = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp'}.get(image.format)
                if not extension:
                    raise ProductDraftError('지원하지 않는 상품 이미지 형식입니다.')
                product.product_image.save(f'imported-{product.pk}{extension}', ContentFile(content), save=True)
                return True
            finally:
                close = getattr(response, 'close', None)
                if callable(close):
                    close()
    except (ProductDraftError, ValueError, requests.RequestException, OSError):
        return False
    return False


def create_product_from_draft(owner, draft: ProductDraft, store=None) -> Linker:
    validate_product_draft(draft)
    if has_existing_product(owner, draft, store=store):
        raise ProductDraftError('이미 등록된 상품입니다.')
    marketplace = marketplace_for_draft(draft)
    if marketplace is None:
        raise ProductDraftError('활성화된 판매사이트가 없습니다.')
    product = Linker.objects.create(
        owner=owner,
        title=draft.title.strip()[:240],
        description=draft.description.strip(),
        destination_url=draft.destination_url.strip()[:1000],
        thumbnail_url=draft.thumbnail_url.strip()[:1000],
        product_no=next_product_no(owner, draft.product_no, store=store),
        store=store,
        marketplace=marketplace,
        status='ACTIVE',
        store_visible=True,
    )
    product.image_copy_failed = not copy_shoppinghub_draft_image(product, draft)
    return product


def import_inpock_products(owner, profile_url: str, limit: int = 50) -> tuple[int, int]:
    drafts = scan_inpock_product_drafts(profile_url, limit=limit)
    created = skipped = 0
    with transaction.atomic():
        for draft in drafts:
            if has_existing_product(owner, draft):
                skipped += 1
                continue
            create_product_from_draft(owner, draft)
            created += 1
    return created, skipped
