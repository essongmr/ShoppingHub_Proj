from datetime import datetime

from django.utils import timezone

from linker.models import SocialContent
from linker.services.youtube_client import authenticated_client
from linker.services.youtube_oauth import YouTubeOAuthError


class YouTubeReadError(Exception):
    def __init__(self, message, code=''):
        super().__init__(message)
        self.code = code


def _api_error(exc):
    reason = ''
    for error in getattr(exc, 'error_details', []) or []:
        reason = error.get('reason', '')
        if reason:
            break
    if reason in ('commentsDisabled', 'videoNotFound', 'forbidden', 'quotaExceeded'):
        return reason
    return ''


def _published_at(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def list_uploads(account, page_token='', max_results=25):
    try:
        client = authenticated_client(account)
        channel = client.channels().list(part='contentDetails', mine=True).execute()
        items = channel.get('items', [])
        if not items:
            raise YouTubeReadError('인증된 YouTube 채널을 찾을 수 없습니다.')
        uploads_id = items[0].get('contentDetails', {}).get('relatedPlaylists', {}).get('uploads')
        if not uploads_id:
            raise YouTubeReadError('YouTube 업로드 목록을 찾을 수 없습니다.')
        playlist = client.playlistItems().list(part='snippet,contentDetails', playlistId=uploads_id, maxResults=max_results, pageToken=page_token).execute()
        video_ids = [item.get('contentDetails', {}).get('videoId') for item in playlist.get('items', [])]
        details = {}
        if video_ids:
            result = client.videos().list(part='snippet,contentDetails,statistics,status', id=','.join(video_ids)).execute()
            details = {item['id']: item for item in result.get('items', [])}
        videos = []
        for item in playlist.get('items', []):
            video_id = item.get('contentDetails', {}).get('videoId')
            detail = details.get(video_id, {})
            snippet = detail.get('snippet', item.get('snippet', {}))
            stats = detail.get('statistics', {})
            content_details = detail.get('contentDetails', {})
            videos.append({'video_id': video_id, 'title': snippet.get('title', ''), 'description': snippet.get('description', ''), 'published_at': _published_at(snippet.get('publishedAt')), 'thumbnail_url': (snippet.get('thumbnails', {}).get('high') or snippet.get('thumbnails', {}).get('default') or {}).get('url', ''), 'duration': content_details.get('duration', ''), 'view_count': stats.get('viewCount'), 'like_count': stats.get('likeCount'), 'comment_count': stats.get('commentCount'), 'privacy_status': detail.get('status', {}).get('privacyStatus', ''), 'live_broadcast_content': snippet.get('liveBroadcastContent', ''), 'channel_id': snippet.get('channelId', '')})
        return videos, playlist.get('nextPageToken', '')
    except YouTubeReadError:
        raise
    except YouTubeOAuthError as exc:
        raise YouTubeReadError('YouTube 연결을 다시 확인하세요.') from exc
    except Exception as exc:
        raise YouTubeReadError('YouTube 콘텐츠를 조회하지 못했습니다.', _api_error(exc)) from exc


def register_selected_videos(account, videos, linker=None):
    registered = []
    for video in videos:
        existing = SocialContent.objects.filter(external_content_id=video['video_id']).first()
        if existing and existing.social_account_id != account.pk:
            raise YouTubeReadError('다른 YouTube 계정의 콘텐츠에는 접근할 수 없습니다.')
        content, created = SocialContent.objects.update_or_create(
            social_account=account, external_content_id=video['video_id'],
            defaults={'linker': linker, 'platform': 'YOUTUBE', 'content_type': 'VIDEO', 'title': video['title'], 'caption': video['description'], 'content_url': f"https://www.youtube.com/watch?v={video['video_id']}", 'thumbnail_url': video['thumbnail_url'], 'published_at': video['published_at'], 'metadata': {'youtube_last_synced_at': timezone.now().isoformat(), 'duration': video['duration'], 'view_count': video['view_count'], 'like_count': video['like_count'], 'comment_count': video['comment_count'], 'privacy_status': video['privacy_status'], 'live_broadcast_content': video['live_broadcast_content'], 'channel_id': video['channel_id']}})
        registered.append((content, created))
    return registered

def sync_youtube_contents(account, limit=50):
    """
    Creator ShoppingHub용 YouTube 콘텐츠 동기화.

    - 최근 업로드를 SocialContent에 upsert한다.
    - 기존 linker 연결은 변경하지 않는다.
    - API상 동일 video_id가 다른 YouTube 계정에 귀속되어 있으면 거부한다.
    """
    limit = max(1, min(int(limit or 50), 50))
    videos, _ = list_uploads(account, max_results=limit)

    synced = []

    for video in videos:
        existing = SocialContent.objects.filter(
            external_content_id=video['video_id']
        ).first()

        if existing and existing.social_account_id != account.pk:
            raise YouTubeReadError(
                '다른 YouTube 계정의 콘텐츠에는 접근할 수 없습니다.'
            )

        defaults = {
            'platform': 'YOUTUBE',
            'content_type': 'VIDEO',
            'title': video['title'],
            'caption': video['description'],
            'content_url': (
                f"https://www.youtube.com/watch?v={video['video_id']}"
            ),
            'thumbnail_url': video['thumbnail_url'],
            'published_at': video['published_at'],
            'status': 'ACTIVE',
            'metadata': {
                'youtube_last_synced_at': timezone.now().isoformat(),
                'duration': video['duration'],
                'view_count': video['view_count'],
                'like_count': video['like_count'],
                'comment_count': video['comment_count'],
                'privacy_status': video['privacy_status'],
                'live_broadcast_content': video['live_broadcast_content'],
                'channel_id': video['channel_id'],
            },
        }

        content, _ = SocialContent.objects.update_or_create(
            social_account=account,
            external_content_id=video['video_id'],
            defaults=defaults,
        )

        synced.append(content)

    return synced

