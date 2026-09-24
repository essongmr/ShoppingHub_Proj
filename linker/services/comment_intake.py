from django.db import IntegrityError, transaction
from django.utils import timezone

from linker.models import SocialComment


def upsert_comment(*, content, external_comment_id, comment_text='', external_user_id='', username='', commented_at=None, metadata=None, trigger=True):
    """Store one comment idempotently and optionally enqueue Instagram processing."""
    if not external_comment_id:
        return None, False
    defaults = {
        'social_content': content,
        'external_user_id': external_user_id or '',
        'username': username or '',
        'comment_text': comment_text or '',
        'commented_at': commented_at,
        'metadata': metadata or {},
    }
    try:
        with transaction.atomic():
            comment, created = SocialComment.objects.get_or_create(
                external_comment_id=str(external_comment_id),
                defaults=defaults,
            )
    except IntegrityError:
        comment = SocialComment.objects.get(external_comment_id=str(external_comment_id))
        created = False
    if created and trigger and content.platform == 'INSTAGRAM':
        from linker.tasks import process_comment
        process_comment.delay(comment.pk)
    return comment, created


def comments_last_synced_at(content):
    value = (content.metadata or {}).get('comments_last_synced_at')
    return value


def mark_comments_synced(content):
    metadata = {**(content.metadata or {}), 'comments_last_synced_at': timezone.now().isoformat()}
    content.metadata = metadata
    content.save(update_fields=['metadata', 'updated_at'])
