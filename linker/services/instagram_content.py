from urllib.parse import urlparse, urlunparse

import requests
from django.conf import settings
from django.utils.dateparse import parse_datetime

from linker.models import SocialAccount, SocialContent
from linker.services.instagram_oauth import InstagramOAuthError, credentials_for_account


class InstagramContentError(Exception):
    pass


def normalize_instagram_permalink(value):
    parsed = urlparse((value or '').strip())
    if parsed.scheme not in {'http', 'https'} or parsed.netloc.lower() not in {'instagram.com', 'www.instagram.com'}:
        raise InstagramContentError('Instagram 게시물 또는 Reel URL을 입력해 주세요.')
    parts = [part for part in parsed.path.split('/') if part]
    if len(parts) != 2 or parts[0] not in {'p', 'reel', 'tv'}:
        raise InstagramContentError('Instagram 게시물 또는 Reel URL을 입력해 주세요.')
    return urlunparse(('https', 'www.instagram.com', f'/{parts[0]}/{parts[1]}/', '', '', ''))


def _content_type(media_type):
    return {
        'IMAGE': SocialContent.ContentType.IMAGE,
        'CAROUSEL_ALBUM': SocialContent.ContentType.CAROUSEL,
        'VIDEO': SocialContent.ContentType.REEL,
        'REELS': SocialContent.ContentType.REEL,
    }.get((media_type or '').upper(), SocialContent.ContentType.VIDEO)


def _upsert_media(account, media):
    external_id = str(media.get('id') or '')
    if not external_id:
        raise InstagramContentError('Instagram 콘텐츠 ID를 확인할 수 없습니다.')
    existing = SocialContent.objects.filter(platform=SocialAccount.Platform.INSTAGRAM, external_content_id=external_id).first()
    if existing and existing.social_account_id != account.pk:
        raise InstagramContentError('다른 계정에 연결된 Instagram 콘텐츠입니다.')
    caption = media.get('caption') or ''
    permalink = media.get('permalink') or ''
    try:
        content_url = normalize_instagram_permalink(permalink) if permalink else ''
    except InstagramContentError:
        content_url = permalink

    content, _ = SocialContent.objects.update_or_create(
        platform=SocialAccount.Platform.INSTAGRAM,
        external_content_id=external_id,
        defaults={
            'social_account': account,
            'content_type': _content_type(media.get('media_type')),
            'title': caption.splitlines()[0][:300] if caption else '제목 없음',
            'caption': caption,
            'content_url': content_url,
            'thumbnail_url': media.get('thumbnail_url') or media.get('media_url') or '',
            'published_at': parse_datetime(media.get('timestamp') or '') if media.get('timestamp') else None,
            'status': SocialAccount.Status.ACTIVE,
            'metadata': {'instagram_media_type': media.get('media_type', '')},
        },
    )
    return content


def fetch_recent_instagram_media(account, limit=25):
    if account.platform != SocialAccount.Platform.INSTAGRAM or account.status != SocialAccount.Status.ACTIVE:
        raise InstagramContentError('활성 Instagram 계정을 선택해 주세요.')
    try:
        token = credentials_for_account(account)
    except Exception as exc:
        raise InstagramContentError('Instagram 계정 인증을 확인할 수 없습니다.') from exc

    url = f'https://graph.instagram.com/{settings.META_INSTAGRAM_GRAPH_VERSION}/me/media'
    params = {
        'fields': 'id,media_type,caption,media_url,thumbnail_url,permalink,timestamp',
        'limit': min(limit, 100),
        'access_token': token,
    }
    try:
        response = requests.get(url, params=params, timeout=15)
    except requests.RequestException as exc:
        raise InstagramContentError('Instagram 콘텐츠를 조회하지 못했습니다.') from exc
    if not response.ok:
        raise InstagramContentError('Instagram 콘텐츠를 조회하지 못했습니다.')
    data = response.json()
    return data.get('data', [])


def sync_instagram_contents(account, limit=50):
    media_list = fetch_recent_instagram_media(account, limit=limit)
    synced = []
    for media in media_list:
        try:
            content = _upsert_media(account, media)
            synced.append(content)
        except InstagramContentError:
            continue
    return synced


def confirm_instagram_media_by_id(account, media_id):
    if account.platform != SocialAccount.Platform.INSTAGRAM or account.status != SocialAccount.Status.ACTIVE:
        raise InstagramContentError('활성 Instagram 계정을 선택해 주세요.')
    media_id = str(media_id or '').strip()
    if not media_id:
        raise InstagramContentError('선택할 Instagram 콘텐츠를 지정해 주세요.')
    try:
        token = credentials_for_account(account)
    except Exception as exc:
        raise InstagramContentError('Instagram 계정 인증을 확인할 수 없습니다.') from exc

    url = f'https://graph.instagram.com/{settings.META_INSTAGRAM_GRAPH_VERSION}/{media_id}'
    params = {
        'fields': 'id,media_type,caption,media_url,thumbnail_url,permalink,timestamp',
        'access_token': token,
    }
    try:
        response = requests.get(url, params=params, timeout=15)
    except requests.RequestException as exc:
        raise InstagramContentError('Instagram 콘텐츠를 조회하지 못했습니다.') from exc
    if not response.ok:
        raise InstagramContentError('Instagram 콘텐츠를 조회하지 못했습니다.')
    media = response.json()
    return _upsert_media(account, media)


def confirm_instagram_content(account, permalink):
    if account.platform != SocialAccount.Platform.INSTAGRAM or account.status != SocialAccount.Status.ACTIVE:
        raise InstagramContentError('활성 Instagram 계정을 선택해 주세요.')
    wanted_permalink = normalize_instagram_permalink(permalink)
    try:
        token = credentials_for_account(account)
    except Exception as exc:
        raise InstagramContentError('Instagram 계정 인증을 확인할 수 없습니다.') from exc

    url = f'https://graph.instagram.com/{settings.META_INSTAGRAM_GRAPH_VERSION}/me/media'
    params = {
        'fields': 'id,media_type,caption,media_url,thumbnail_url,permalink,timestamp',
        'limit': 100,
        'access_token': token,
    }
    for _page in range(20):
        try:
            response = requests.get(url, params=params, timeout=15)
        except requests.RequestException as exc:
            raise InstagramContentError('Instagram 콘텐츠를 조회하지 못했습니다.') from exc
        if not response.ok:
            raise InstagramContentError('Instagram 콘텐츠를 조회하지 못했습니다.')
        data = response.json()
        for media in data.get('data', []):
            try:
                media_permalink = normalize_instagram_permalink(media.get('permalink') or '')
            except InstagramContentError:
                continue
            if media_permalink == wanted_permalink:
                return _upsert_media(account, media)
        next_url = (data.get('paging') or {}).get('next')
        if not next_url or urlparse(next_url).netloc.lower() != 'graph.instagram.com':
            break
        url, params = next_url, None
    raise InstagramContentError('선택한 URL의 Instagram 콘텐츠를 찾지 못했습니다.')
