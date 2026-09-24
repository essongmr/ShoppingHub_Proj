import requests
from django.utils.dateparse import parse_datetime

from linker.models import SocialAccount, SocialContent
from linker.services.comment_intake import upsert_comment, mark_comments_synced
from linker.services.instagram_oauth import credentials_for_account


class InstagramCommentSyncError(Exception):
    pass


def sync_instagram_comments(account, *, limit=25):
    token = credentials_for_account(account)
    contents = SocialContent.objects.filter(
        social_account=account,
        platform=SocialAccount.Platform.INSTAGRAM,
        status='ACTIVE',
    ).order_by('-published_at', '-id')[:limit]
    created = 0
    for content in contents:
        url = f'https://graph.instagram.com/{content.external_content_id}/comments'
        response = requests.get(url, params={'access_token': token, 'fields': 'id,text,from,timestamp', 'limit': 100}, timeout=15)
        if response.status_code >= 400:
            raise InstagramCommentSyncError('Instagram 새 댓글을 확인하지 못했습니다.')
        for item in response.json().get('data', []):
            author = item.get('from') or {}
            _comment, is_created = upsert_comment(
                content=content,
                external_comment_id=item.get('id', ''),
                comment_text=item.get('text', ''),
                external_user_id=str(author.get('id', '')),
                username=author.get('username', ''),
                commented_at=parse_datetime(item.get('timestamp', '')) if item.get('timestamp') else None,
                metadata={'source': 'instagram_sync'},
                trigger=True,
            )
            created += int(is_created)
        mark_comments_synced(content)
    return created
