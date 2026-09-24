import json
import logging

from googleapiclient.errors import HttpError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from linker.models import SocialComment
from linker.services.comment_intake import upsert_comment, mark_comments_synced
from linker.services.youtube_content import YouTubeReadError, _api_error
from linker.services.youtube_client import authenticated_client
from linker.services.youtube_oauth import YouTubeOAuthError, YOUTUBE_FORCE_SSL_SCOPE

logger = logging.getLogger(__name__)


def _safe_api_error(exc):
    status = getattr(getattr(exc, 'resp', None), 'status', None)
    reason = ''
    message = ''
    for detail in getattr(exc, 'error_details', []) or []:
        if isinstance(detail, dict):
            reason = reason or str(detail.get('reason', ''))
            message = message or str(detail.get('message', ''))
    if isinstance(exc, HttpError) and not message:
        try:
            body = json.loads(exc.content.decode('utf-8', errors='replace'))
            errors = body.get('error', {}).get('errors', [])
            if errors:
                reason = reason or str(errors[0].get('reason', ''))
                message = message or str(errors[0].get('message', ''))
            message = message or str(body.get('error', {}).get('message', ''))
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            pass
    allowed = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._- ')
    clean = lambda value: ''.join(char for char in value if char in allowed)[:160]
    return status or '', clean(reason), clean(message or 'YouTube API request failed')


def sync_comments(content, page_token='', max_results=25):
    logger.warning('YouTube comment sync started: content_found=%s platform_check_passed=%s', bool(content.pk), content.platform == 'YOUTUBE')
    account = content.social_account
    logger.warning('YouTube comment sync account resolved: account_found=%s', bool(account.pk))
    credential = account.youtube_credential
    logger.warning('YouTube comment sync credential check: credential_found=%s revoked=%s', bool(credential), bool(credential and credential.revoked_at))
    if not credential or credential.revoked_at:
        logger.warning('YouTube comment sync permission check failed: reason=credentialUnavailable')
        raise YouTubeReadError('YouTube 연결을 다시 확인하세요.', 'credentialUnavailable')
    if YOUTUBE_FORCE_SSL_SCOPE not in (credential.scopes or []):
        logger.warning('YouTube comment sync permission check failed: reason=insufficientPermissions')
        raise YouTubeReadError('댓글 기능을 사용하려면 YouTube 댓글 권한을 추가로 승인해야 합니다.', 'insufficientPermissions')
    try:
        logger.warning('YouTube authenticated client creation started')
        client = authenticated_client(account)
        logger.warning('YouTube authenticated client creation succeeded')
        video_id = content.external_content_id
        logger.info('YouTube comment sync video check: video_id_present=%s', bool(video_id))
        if not video_id:
            raise YouTubeReadError('YouTube 영상 ID를 찾을 수 없습니다.', 'videoNotFound')
        logger.warning('YouTube commentThreads.list started')
        response = client.commentThreads().list(part='snippet,replies', videoId=video_id, maxResults=max_results, pageToken=page_token).execute()
        logger.warning('YouTube commentThreads.list succeeded: item_count=%s next_page_token_present=%s', len(response.get('items', [])), bool(response.get('nextPageToken')))
    except YouTubeOAuthError as exc:
        logger.warning('YouTube comment sync credential failure: type=%s', type(exc).__name__)
        raise YouTubeReadError('YouTube 연결을 다시 확인하세요.') from exc
    except Exception as exc:
        reason = _api_error(exc)
        status, safe_reason, safe_message = _safe_api_error(exc)
        logger.error('YouTube comment sync failed: status=%s reason=%s type=%s message=%s', status, safe_reason or reason, type(exc).__name__, safe_message)
        if reason == 'commentsDisabled':
            raise YouTubeReadError('이 YouTube 영상에서는 댓글을 조회할 수 없습니다.', reason) from exc
        if reason == 'forbidden':
            raise YouTubeReadError('YouTube 연결을 다시 확인하세요.', reason) from exc
        raise YouTubeReadError('YouTube 댓글을 조회하지 못했습니다.', reason) from exc
    created = updated = 0
    logger.warning('YouTube SocialComment upsert started: item_count=%s', len(response.get('items', [])))
    for item in response.get('items', []):
        top = item.get('snippet', {}).get('topLevelComment', {})
        snippet = top.get('snippet', {})
        comment_id = top.get('id')
        if not comment_id:
            continue
        defaults = {'social_content': content, 'external_user_id': snippet.get('authorChannelId', {}).get('value', ''), 'username': snippet.get('authorDisplayName', ''), 'comment_text': snippet.get('textOriginal') or snippet.get('textDisplay', ''), 'commented_at': parse_datetime(snippet.get('publishedAt', '')), 'metadata': {'like_count': snippet.get('likeCount', 0), 'updated_at': snippet.get('updatedAt'), 'reply_count': item.get('snippet', {}).get('totalReplyCount', 0), 'parent_id': snippet.get('parentId', ''), 'text_format': 'textOriginal' if snippet.get('textOriginal') else 'textDisplay'}}
        obj, is_created = upsert_comment(
            content=content,
            external_comment_id=comment_id,
            comment_text=defaults['comment_text'],
            external_user_id=defaults['external_user_id'],
            username=defaults['username'],
            commented_at=defaults['commented_at'],
            metadata=defaults['metadata'],
            trigger=False,
        )
        created += int(is_created)
        updated += int(not is_created)
        logger.warning('YouTube SocialComment upsert succeeded: comment_id_present=%s created=%s', bool(comment_id), is_created)
    mark_comments_synced(content)
    logger.warning('YouTube comment sync completed: fetched=%s created=%s updated=%s next_page_token_present=%s', created + updated, created, updated, bool(response.get('nextPageToken')))
    return {'created': created, 'updated': updated, 'fetched': created + updated, 'next_page_token': response.get('nextPageToken', '')}