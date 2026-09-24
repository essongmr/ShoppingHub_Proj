from celery import shared_task
from django.db import transaction
from django.utils import timezone
from linker.models import SocialComment, ResponseLog
from linker.services.rules import decide, rule_delivery_target, select_response_rule
from linker.services.meta import MetaClient
@shared_task
def process_comment(comment_id):
    with transaction.atomic():
        c=SocialComment.objects.select_for_update(of=('self',)).select_related('social_content__social_account__store','social_content__response_rule').get(pk=comment_id); c.processing_status='PROCESSING'; c.save(update_fields=['processing_status'])
        c.decision_status=decide(c); c.save(update_fields=['decision_status'])
        if c.decision_status!='MATCHED': c.processing_status='SUCCESS'; c.save(update_fields=['processing_status']); return
        rule=select_response_rule(c)
        if not rule:
            c.processing_status='SUCCESS'; c.save(update_fields=['processing_status']); return
        try:
            if rule.public_reply_enabled:
                public_log, _ = ResponseLog.objects.get_or_create(social_comment=c,response_rule=rule,response_type='PUBLIC_REPLY')
                if public_log.status != 'SUCCESS':
                    data=MetaClient(account=c.social_content.social_account).public_reply(c.external_comment_id,rule.public_reply_text)
                    public_log.status='SUCCESS'; public_log.response_external_id=str(data.get('id','')); public_log.completed_at=timezone.now(); public_log.save(update_fields=['status','response_external_id','completed_at'])
            if rule.private_reply_enabled:
                private_log, _ = ResponseLog.objects.get_or_create(social_comment=c,response_rule=rule,response_type='PRIVATE_REPLY')
                if private_log.status != 'SUCCESS':
                    target_type, target, link_mode, direct_url, include_link_store = rule_delivery_target(rule, c)
                    store = rule.target_store or c.social_content.social_account.store or getattr(getattr(target, 'owner', None), 'creator_store', None)
                    if rule.rendered_message_snapshot:
                        message = rule.rendered_message_snapshot
                    elif rule.test_private_reply_text:
                        # Legacy safety fallback only.
                        # New Auto DM rules must always persist rendered_message_snapshot.
                        message = rule.test_private_reply_text
                    else:
                        raise ValueError('전송할 DM 메시지 snapshot이 없습니다.')
                    data=MetaClient(account=c.social_content.social_account).private_reply(c.external_comment_id,message)
                    private_log.status='SUCCESS'; private_log.response_external_id=str(data.get('message_id',data.get('id',''))); private_log.completed_at=timezone.now(); private_log.save(update_fields=['status','response_external_id','completed_at'])
            c.processing_status='SUCCESS'
        except Exception as e:
            ResponseLog.objects.update_or_create(social_comment=c,response_rule=rule,response_type='PRIVATE_REPLY',defaults={'status':'FAILED','error_message':str(e),'completed_at':timezone.now()}); c.processing_status='FAILED'
        c.save(update_fields=['processing_status'])
