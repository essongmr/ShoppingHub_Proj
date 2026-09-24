from unittest.mock import patch
from io import BytesIO
from types import SimpleNamespace
import tempfile

import requests
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from django.urls import NoReverseMatch, reverse
from PIL import Image
from .services.instagram_oauth import InstagramOAuthError, save_verified_credential as save_instagram_credential, verify_account as verify_instagram_account
from .services.product_drafts import ProductDraft, ProductDraftError, draft_identity_candidates, normalized_product_url, scan_existing_service_product_drafts, scan_inpock_product_drafts
from .services.youtube_oauth import save_verified_credential as save_youtube_credential
from .services.store_assignment import assign_social_accounts_to_store, move_social_account
from .models import MessageTemplate, MessageType
from .services.message_templates import (
    build_preview,
    clone_creator_template,
    clone_system_template_for_creator,
    validate_input_schema,
    validate_template_variables,
)
from .services.comment_intake import upsert_comment
from .services.rules import decide
from .creator_views import AUTO_DM_WIZARD_SESSION_KEY

from .models import (
    CreatorProfile,
    CreatorStore,
    Linker,
    Marketplace,
    ProductBlock,
    ProductBlockItem,
    RecipeDetail,
    ResponseKeyword,
    ResponseLog,
    ResponseRule,
    SocialAccount,
    SocialComment,
    SocialContent,
    SocialContentLink,
    SocialCredential,
    StoreCategory,
    ServiceDetail,
)
from .services.product_store_management import (
    ProductStoreManagementError,
    copy_product_to_store,
    delete_product_block_safely,
    delete_product_safely,
    move_product_to_store,
)


class MessageTemplatePhaseBTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='message-owner', password='password')
        self.other = User.objects.create_user(username='message-other', password='password')
        self.message_type = MessageType.objects.create(
            code='PRODUCT_LINK_TEST',
            name='상품 링크',
            input_schema={'fields': [
                {'key': 'product_name', 'label': '상품명', 'type': 'text', 'required': True, 'display_order': 10},
                {'key': 'store_url', 'label': '스토어 링크', 'type': 'store_url', 'required': True, 'display_order': 20},
            ]},
        )
        self.system_template = MessageTemplate.objects.create(
            owner_type=MessageTemplate.OwnerType.SYSTEM,
            message_type=self.message_type,
            name='기본 상품 안내',
            body_template='{{ product_name }} 안내\n{{ store_url }}',
            is_default=True,
            is_recommended=True,
        )

    def test_schema_validation_rejects_duplicate_keys_and_unknown_types(self):
        with self.assertRaises(Exception):
            validate_input_schema({'fields': [
                {'key': 'product', 'label': '상품', 'type': 'text'},
                {'key': 'product', 'label': '상품2', 'type': 'text'},
            ]})
        with self.assertRaises(Exception):
            validate_input_schema({'fields': [{'key': 'product', 'label': '상품', 'type': 'json'}]})

    def test_content_v1_seed_is_idempotent_and_template_bodies_have_no_urls(self):
        call_command('seed_message_templates', content_v1=True)
        call_command('seed_message_templates', content_v1=True)
        codes = {'RECIPE_INFO', 'PRODUCT_INFO', 'SERVICE_INFO', 'STORE_INFO', 'DIRECT'}
        self.assertEqual(MessageType.objects.filter(code__in=codes).count(), 5)
        templates = MessageTemplate.objects.filter(owner_type='SYSTEM', message_type__code__in=codes)
        self.assertEqual(templates.count(), 11)
        self.assertFalse(any('http://' in item.body_template or 'https://' in item.body_template for item in templates))

    def test_template_variable_validation_rejects_unknown_variables(self):
        with self.assertRaises(Exception):
            validate_template_variables('{{ unknown_value }}')

    def test_creator_copies_system_template_and_original_changes_do_not_propagate(self):
        copied = clone_system_template_for_creator(self.system_template, self.owner)
        self.assertEqual(copied.owner_type, MessageTemplate.OwnerType.CREATOR)
        self.assertEqual(copied.creator, self.owner)
        self.assertEqual(copied.source_template, self.system_template)
        self.system_template.body_template = '변경된 표준 문구'
        self.system_template.save(update_fields=['body_template', 'updated_at'])
        copied.refresh_from_db()
        self.assertIn('{{ product_name }}', copied.body_template)

    def test_creator_can_copy_only_own_template(self):
        copied = clone_system_template_for_creator(self.system_template, self.owner)
        with self.assertRaises(Exception):
            clone_creator_template(copied, self.other)

    def test_preview_renders_and_required_config_is_enforced(self):
        with self.assertRaises(Exception):
            build_preview(self.system_template, {'product_name': '상품'})
        self.assertEqual(
            build_preview(self.system_template, {'product_name': '커튼', 'store_url': '/s/matfit/'}),
            '커튼 안내\n/s/matfit/',
        )

    def test_response_rule_snapshot_is_independent_of_template(self):
        owner_store = CreatorStore.objects.create(owner=self.owner, store_name='MatFit', slug='message-matfit')
        account = SocialAccount.objects.create(owner=self.owner, store=owner_store, platform='INSTAGRAM', account_name='matfit', username='matfit')
        content = SocialContent.objects.create(social_account=account, platform='INSTAGRAM', content_type='REEL', external_content_id='message-content')
        rule = ResponseRule.objects.create(social_content=content, store=owner_store, response_mode='ALL', private_reply_enabled=True)
        from .services.message_templates import snapshot_template_for_rule
        snapshot_template_for_rule(rule, self.system_template, {'product_name': '커튼', 'store_url': '/s/matfit/'})
        self.system_template.body_template = '바뀐 표준 문구'
        self.system_template.save(update_fields=['body_template', 'updated_at'])
        rule.refresh_from_db()
        self.assertEqual(rule.rendered_message_snapshot, '커튼 안내\n/s/matfit/')
        self.assertEqual(rule.test_private_reply_text, rule.rendered_message_snapshot)

    def test_creator_template_screen_copies_system_template(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('creator_message_templates'))
        self.assertContains(response, '기본 상품 안내')
        response = self.client.post(reverse('creator_message_template_copy', args=[self.system_template.pk]))
        self.assertRedirects(response, reverse('creator_message_templates'), fetch_redirect_response=False)
        self.assertTrue(MessageTemplate.objects.filter(creator=self.owner, source_template=self.system_template).exists())

    def test_creator_cannot_edit_other_creator_template(self):
        template = clone_system_template_for_creator(self.system_template, self.owner)
        self.client.force_login(self.other)
        response = self.client.get(reverse('creator_message_template_edit', args=[template.pk]))
        self.assertEqual(response.status_code, 404)


class CommentTriggerPhaseCOneTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='comment-owner', password='password')
        self.store = CreatorStore.objects.create(owner=self.user, store_name='MatFit', slug='comment-matfit')
        self.account = SocialAccount.objects.create(owner=self.user, store=self.store, platform='INSTAGRAM', account_name='matfit', connection_status='CONNECTED')
        self.content = SocialContent.objects.create(social_account=self.account, platform='INSTAGRAM', content_type='REEL', external_content_id='comment-content')

    def rule(self, mode='KEYWORD', **kwargs):
        return ResponseRule.objects.create(scope='CONTENT', store=self.store, social_content=self.content, response_mode=mode, enabled=True, private_reply_enabled=True, **kwargs)

    def test_common_intake_deduplicates_and_only_triggers_instagram(self):
        with patch('linker.tasks.process_comment.delay') as enqueue:
            first, created = upsert_comment(content=self.content, external_comment_id='comment-1', comment_text='링크', trigger=True)
            second, created_again = upsert_comment(content=self.content, external_comment_id='comment-1', comment_text='링크', trigger=True)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SocialComment.objects.filter(external_comment_id='comment-1').count(), 1)
        enqueue.assert_called_once_with(first.pk)

    def test_exclusion_keyword_wins_over_include(self):
        rule = self.rule()
        ResponseKeyword.objects.create(response_rule=rule, keyword='링크')
        ResponseKeyword.objects.create(response_rule=rule, keyword='광고', is_exclusion=True)
        comment = SocialComment.objects.create(social_content=self.content, external_comment_id='comment-2', comment_text='광고 링크')
        self.assertEqual(decide(comment), SocialComment.Decision.NOT_MATCHED)

    def test_manual_off_and_period_rules_do_not_auto_match(self):
        comment = SocialComment.objects.create(social_content=self.content, external_comment_id='comment-3', comment_text='링크')
        self.assertEqual(decide(comment), SocialComment.Decision.NOT_MATCHED)
        rule = self.rule(mode='MANUAL')
        self.assertEqual(decide(comment), SocialComment.Decision.NOT_MATCHED)
        rule.response_mode = 'ALL'
        rule.starts_at = timezone.now() + timezone.timedelta(hours=1)
        rule.save(update_fields=['response_mode', 'starts_at', 'updated_at'])
        self.assertEqual(decide(comment), SocialComment.Decision.NOT_MATCHED)
        rule.starts_at = None
        rule.ends_at = timezone.now() - timezone.timedelta(hours=1)
        rule.save(update_fields=['starts_at', 'ends_at', 'updated_at'])
        self.assertEqual(decide(comment), SocialComment.Decision.NOT_MATCHED)


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost'])
class EasyAutoDMPhaseCTwoTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='wizard-owner', password='password')
        CreatorProfile.objects.create(user=self.user, onboarding_status=CreatorProfile.OnboardingStatus.COMPLETED)
        self.store = CreatorStore.objects.create(owner=self.user, store_name='MatFit', slug='wizard-matfit')
        self.account = SocialAccount.objects.create(owner=self.user, store=self.store, platform='INSTAGRAM', account_name='matfit', username='matfit', status='ACTIVE')
        self.content = SocialContent.objects.create(social_account=self.account, platform='INSTAGRAM', content_type='REEL', external_content_id='wizard-content', title='레시피')
        self.product = Linker.objects.create(owner=self.user, store=self.store, product_no='001', title='상품', status='ACTIVE', destination_url='https://example.com/product')
        self.message_type = MessageType.objects.create(code='WIZARD_TYPE', name='상품 정보', active=True, input_schema={'fields': []})
        self.template = MessageTemplate.objects.create(owner_type='SYSTEM', message_type=self.message_type, name='기본', body_template='{{ product_name }} {{ store_url }}', active=True)
        self.client.force_login(self.user)
        session = self.client.session
        session['active_store_id'] = self.store.pk
        session.save()

    def test_wizard_saves_scoped_rule_and_snapshot(self):
        response = self.client.post(reverse('shoppinghub_auto_dm') + '?step=1', {'step': '1', 'account_id': self.account.pk, 'content_id': self.content.pk, 'mode': 'KEYWORD', 'include_keywords': '링크, 구매'})
        self.assertRedirects(response, reverse('shoppinghub_auto_dm') + '?step=2', fetch_redirect_response=False)
        self.client.post(reverse('shoppinghub_auto_dm') + '?step=2', {'step': '2', 'message_type_id': self.message_type.pk})
        response = self.client.post(reverse('shoppinghub_auto_dm') + '?step=3', {'step': '3', 'template_id': self.template.pk, 'target_type': 'PRODUCT', 'target_id': self.product.pk})
        self.assertRedirects(response, reverse('shoppinghub_auto_dm') + '?step=4', fetch_redirect_response=False)
        response = self.client.post(reverse('shoppinghub_auto_dm') + '?step=4', {'step': '4'})
        rule = ResponseRule.objects.get(social_content=self.content)
        self.assertRedirects(
            response,
            reverse('shoppinghub_auto_dm') + f'?saved={rule.pk}',
            fetch_redirect_response=False,
        )
        self.assertEqual(rule.store, self.store)
        self.assertEqual(rule.message_template, self.template)
        self.assertIn('상품 http://testserver/s/wizard-matfit/', rule.rendered_message_snapshot)
        self.assertIn('/s/wizard-matfit/?product=001', rule.rendered_message_snapshot)
        self.assertEqual(set(rule.keywords.values_list('keyword', flat=True)), {'링크', '구매'})

    def test_plain_get_starts_from_step_one(self):
        session = self.client.session
        session[AUTO_DM_WIZARD_SESSION_KEY] = {
            'step': 3,
            'account_id': self.account.pk,
            'content_id': self.content.pk,
        }
        session.save()

        response = self.client.get(reverse('shoppinghub_auto_dm'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['step'], 1)

    def test_saved_rule_result_is_visible(self):
        rule = ResponseRule.objects.create(
            scope=ResponseRule.Scope.CONTENT,
            store=self.store,
            social_account=self.account,
            social_content=self.content,
            response_mode='KEYWORD',
            private_reply_enabled=True,
            target_type=ResponseRule.TargetType.PRODUCT,
            target_linker=self.product,
            target_store=self.store,
            rendered_message_snapshot='상품 안내',
            test_private_reply_text='상품 안내',
        )

        response = self.client.get(
            reverse('shoppinghub_auto_dm') + f'?saved={rule.pk}'
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['saved_rule'], rule)
        self.assertContains(response, '자동 DM을 시작했습니다.')

    def test_duplicate_rule_for_same_content_is_blocked_without_500(self):
        existing = ResponseRule.objects.create(
            scope=ResponseRule.Scope.CONTENT,
            store=self.store,
            social_account=self.account,
            social_content=self.content,
            response_mode='KEYWORD',
            private_reply_enabled=True,
            target_type=ResponseRule.TargetType.PRODUCT,
            target_linker=self.product,
            target_store=self.store,
            message_template=self.template,
            rendered_message_snapshot='기존 메시지',
            test_private_reply_text='기존 메시지',
        )

        session = self.client.session
        session[AUTO_DM_WIZARD_SESSION_KEY] = {
            'step': 4,
            'account_id': self.account.pk,
            'content_id': self.content.pk,
            'mode': 'KEYWORD',
            'include_keywords': '링크',
            'exclude_keywords': '',
            'match_type': 'CONTAINS',
            'message_type_id': self.message_type.pk,
            'template_id': self.template.pk,
            'target_type': ResponseRule.TargetType.PRODUCT,
            'target_id': self.product.pk,
            'message_config': {
                'product_name': self.product.title,
                'product_url': 'http://testserver/s/wizard-matfit/?product=001',
                'store_name': self.store.store_name,
                'store_url': 'http://testserver/s/wizard-matfit/',
                'intro': '',
            },
            'preview': '새 메시지',
            'starts_at': '',
            'ends_at': '',
        }
        session.save()

        response = self.client.post(
            reverse('shoppinghub_auto_dm') + '?step=4',
            {'step': '4'},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            ResponseRule.objects.filter(social_content=self.content).count(),
            1,
        )

        existing.refresh_from_db()
        self.assertEqual(existing.rendered_message_snapshot, '기존 메시지')
        self.assertContains(
            response,
            '이 게시물에는 이미 다른 자동 DM이 설정되어 있습니다.'
        )

    def test_current_auto_dm_list_shows_only_active_store_rules(self):
        ResponseRule.objects.create(
            scope=ResponseRule.Scope.CONTENT,
            store=self.store,
            social_account=self.account,
            social_content=self.content,
            response_mode='KEYWORD',
            private_reply_enabled=True,
            target_type=ResponseRule.TargetType.PRODUCT,
            target_linker=self.product,
            target_store=self.store,
            rendered_message_snapshot='상품 안내',
            test_private_reply_text='상품 안내',
        )

        other_user = User.objects.create_user(
            username='other-auto-dm-owner',
            password='password',
        )
        other_store = CreatorStore.objects.create(
            owner=other_user,
            store_name='Other Store',
            slug='other-auto-dm-store',
        )
        other_account = SocialAccount.objects.create(
            owner=other_user,
            store=other_store,
            platform='INSTAGRAM',
            account_name='other',
            username='other',
            status='ACTIVE',
        )
        other_content = SocialContent.objects.create(
            social_account=other_account,
            platform='INSTAGRAM',
            content_type='REEL',
            external_content_id='other-content',
            title='다른 스토어 게시물',
            status='ACTIVE',
        )
        ResponseRule.objects.create(
            scope=ResponseRule.Scope.CONTENT,
            store=other_store,
            social_account=other_account,
            social_content=other_content,
            response_mode='ALL',
            private_reply_enabled=True,
            target_type=ResponseRule.TargetType.STORE,
            target_store=other_store,
            rendered_message_snapshot='다른 스토어 안내',
            test_private_reply_text='다른 스토어 안내',
        )

        response = self.client.get(reverse('shoppinghub_auto_dm'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '현재 자동 DM')
        self.assertContains(response, self.content.title)
        self.assertContains(response, self.product.title)
        self.assertNotContains(response, '다른 스토어 게시물')

    def test_auto_dm_toggle_changes_enabled_state(self):
        rule = ResponseRule.objects.create(
            scope=ResponseRule.Scope.CONTENT,
            store=self.store,
            social_account=self.account,
            social_content=self.content,
            response_mode='KEYWORD',
            private_reply_enabled=True,
            target_type=ResponseRule.TargetType.PRODUCT,
            target_linker=self.product,
            target_store=self.store,
            rendered_message_snapshot='상품 안내',
            test_private_reply_text='상품 안내',
            enabled=True,
        )

        response = self.client.post(
            reverse('shoppinghub_auto_dm_toggle', args=[rule.pk])
        )

        self.assertRedirects(
            response,
            reverse('shoppinghub_auto_dm'),
            fetch_redirect_response=False,
        )

        rule.refresh_from_db()
        self.assertFalse(rule.enabled)

        response = self.client.post(
            reverse('shoppinghub_auto_dm_toggle', args=[rule.pk])
        )

        rule.refresh_from_db()
        self.assertTrue(rule.enabled)

    def test_auto_dm_delete_removes_rule_and_get_is_not_allowed(self):
        rule = ResponseRule.objects.create(
            scope=ResponseRule.Scope.CONTENT,
            store=self.store,
            social_account=self.account,
            social_content=self.content,
            response_mode='KEYWORD',
            private_reply_enabled=True,
            target_type=ResponseRule.TargetType.PRODUCT,
            target_linker=self.product,
            target_store=self.store,
            rendered_message_snapshot='상품 안내',
            test_private_reply_text='상품 안내',
        )

        get_response = self.client.get(
            reverse('shoppinghub_auto_dm_delete', args=[rule.pk])
        )
        self.assertEqual(get_response.status_code, 405)
        self.assertTrue(ResponseRule.objects.filter(pk=rule.pk).exists())

        post_response = self.client.post(
            reverse('shoppinghub_auto_dm_delete', args=[rule.pk])
        )

        self.assertRedirects(
            post_response,
            reverse('shoppinghub_auto_dm'),
            fetch_redirect_response=False,
        )
        self.assertFalse(ResponseRule.objects.filter(pk=rule.pk).exists())

    def test_auto_dm_edit_updates_existing_rule_without_creating_new_rule(self):
        rule = ResponseRule.objects.create(
            scope=ResponseRule.Scope.CONTENT,
            store=self.store,
            social_account=self.account,
            social_content=self.content,
            response_mode='KEYWORD',
            private_reply_enabled=True,
            target_type=ResponseRule.TargetType.PRODUCT,
            target_linker=self.product,
            target_store=self.store,
            message_template=self.template,
            rendered_message_snapshot='기존 메시지',
            test_private_reply_text='기존 메시지',
            enabled=True,
        )
        ResponseKeyword.objects.create(
            response_rule=rule,
            keyword='기존키워드',
            match_type='CONTAINS',
            is_exclusion=False,
        )

        session = self.client.session
        session[AUTO_DM_WIZARD_SESSION_KEY] = {
            'step': 4,
            'edit_rule_id': rule.pk,
            'account_id': self.account.pk,
            'content_id': self.content.pk,
            'mode': 'KEYWORD',
            'include_keywords': '새키워드',
            'exclude_keywords': '광고',
            'match_type': 'CONTAINS',
            'message_type_id': self.message_type.pk,
            'template_id': self.template.pk,
            'target_type': ResponseRule.TargetType.PRODUCT,
            'target_id': self.product.pk,
            'message_config': {
                'product_name': self.product.title,
                'store_name': self.store.store_name,
                'store_url': 'http://testserver/s/wizard-matfit/',
                'product_url': 'http://testserver/s/wizard-matfit/?product=001',
                'intro': '',
            },
            'preview': '수정된 메시지',
            'starts_at': '',
            'ends_at': '',
        }
        session.save()

        before_count = ResponseRule.objects.count()

        response = self.client.post(
            reverse('shoppinghub_auto_dm') + '?step=4',
            {'step': '4'},
        )

        self.assertEqual(ResponseRule.objects.count(), before_count)

        rule.refresh_from_db()
        self.assertEqual(rule.rendered_message_snapshot, '수정된 메시지')

        keywords = list(
            rule.keywords.order_by('is_exclusion', 'id')
            .values_list('keyword', 'is_exclusion')
        )
        self.assertEqual(
            keywords,
            [('새키워드', False), ('광고', True)],
        )

        self.assertRedirects(
            response,
            reverse('shoppinghub_auto_dm')
            + f'?saved={rule.pk}&updated=1',
            fetch_redirect_response=False,
        )

    def test_wizard_does_not_show_youtube_account(self):
        SocialAccount.objects.create(owner=self.user, store=self.store, platform='YOUTUBE', account_name='yt', status='ACTIVE')
        response = self.client.get(reverse('shoppinghub_auto_dm') + '?step=1')
        self.assertContains(response, '@matfit')
        self.assertNotContains(response, 'yt')

class StoreAssignmentPhaseOneTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='assignment-owner', password='password')
        self.other_owner = User.objects.create_user(username='assignment-other', password='password')
        self.store = CreatorStore.objects.create(owner=self.owner, store_name='MatFit', slug='assignment-matfit')
        self.other_store = CreatorStore.objects.create(owner=self.other_owner, store_name='Other', slug='assignment-other')

    def account(self, owner=None, store=None, platform='INSTAGRAM', name='matfit_daily'):
        return SocialAccount.objects.create(
            owner=owner or self.owner,
            store=store,
            platform=platform,
            account_name=name,
            username=name,
            external_account_id=f'{platform}-{name}',
            platform_user_id=f'{platform}-{name}',
            status=SocialAccount.Status.ACTIVE,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )

    def test_assigns_unassigned_account_without_changing_owner(self):
        account = self.account(store=None)
        assign_social_accounts_to_store(self.owner, self.store, [account.pk])
        account.refresh_from_db()
        self.assertEqual(account.store, self.store)
        self.assertEqual(account.owner, self.owner)

    def test_rejects_other_owner_account(self):
        account = self.account(owner=self.other_owner, store=self.other_store, name='other')
        with self.assertRaises(ValueError):
            assign_social_accounts_to_store(self.owner, self.store, [account.pk])

    def test_move_keeps_representative_account_independent(self):
        account = self.account(store=self.store)
        profile = CreatorProfile.objects.create(user=self.owner, representative_social_account=account)
        target = CreatorStore.objects.create(owner=self.owner, store_name='Second', slug='assignment-second')
        move_social_account(self.owner, account, target)
        account.refresh_from_db()
        profile.refresh_from_db()
        self.assertEqual(account.store, target)
        self.assertEqual(profile.representative_social_account, account)

    def test_store_delete_unassigns_account_and_keeps_credential(self):
        account = self.account(store=self.store)
        credential = SocialCredential.objects.create(
            social_account=account,
            provider='META_INSTAGRAM',
            access_token_encrypted='encrypted',
        )
        self.store.delete()
        account.refresh_from_db()
        credential.refresh_from_db()
        self.assertIsNone(account.store)
        self.assertTrue(SocialAccount.objects.filter(pk=account.pk).exists())
        self.assertTrue(SocialCredential.objects.filter(pk=credential.pk).exists())


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost'])
class StoreAssignmentPhaseTwoTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='onboarding-assignment', password='password')
        CreatorProfile.objects.create(user=self.user)
        self.client.force_login(self.user)

    def account(self, platform, name):
        account = SocialAccount.objects.create(
            owner=self.user,
            platform=platform,
            account_name=name,
            display_name=name,
            username=name,
            external_account_id=f'{platform}-{name}',
            platform_user_id=f'{platform}-{name}',
            status=SocialAccount.Status.ACTIVE,
        )
        SocialCredential.objects.create(
            social_account=account,
            provider='META_INSTAGRAM' if platform == SocialAccount.Platform.INSTAGRAM else 'GOOGLE_YOUTUBE',
            access_token_encrypted='encrypted',
        )
        return account

    def complete_to_assignment(self, account_ids):
        self.client.post(reverse('creator_onboarding_store'), {'step': '1'})
        self.client.post(
            reverse('creator_onboarding_store') + '?step=2',
            {'step': '2', 'store_name': 'MatFit'},
        )
        return self.client.post(
            reverse('creator_onboarding_store') + '?step=3',
            {
                'step': '3',
                'account_ids': [str(account_id) for account_id in account_ids],
            },
        )

    def test_onboarding_assigns_only_selected_accounts(self):
        instagram = self.account(SocialAccount.Platform.INSTAGRAM, 'matfit_daily')
        youtube = self.account(SocialAccount.Platform.YOUTUBE, 'singsong')
        response = self.complete_to_assignment([instagram.pk])
        self.assertRedirects(response, reverse('creator_onboarding_store') + '?step=4', fetch_redirect_response=False)
        instagram.refresh_from_db()
        youtube.refresh_from_db()
        store = CreatorStore.objects.get(owner=self.user)
        self.assertEqual(instagram.store, store)
        self.assertIsNone(youtube.store)

    def test_single_connected_account_is_assigned_by_legacy_post(self):
        instagram = self.account(SocialAccount.Platform.INSTAGRAM, 'single')
        self.client.post(reverse('creator_onboarding_store'), {'step': '1'})
        self.client.post(
            reverse('creator_onboarding_store') + '?step=2',
            {'step': '2', 'store_name': 'Single Store'},
        )
        response = self.client.post(
            reverse('creator_onboarding_store') + '?step=3',
            {'step': '3', 'account_id': str(instagram.pk), 'skip': '1'},
        )
        instagram.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(instagram.store, CreatorStore.objects.get(owner=self.user))

    def test_public_store_uses_only_connected_assigned_accounts(self):
        store = CreatorStore.objects.create(owner=self.user, store_name='Public MatFit', slug='public-assignment')
        assigned = self.account(SocialAccount.Platform.INSTAGRAM, 'assigned')
        assigned.store = store
        assigned.connection_status = SocialAccount.ConnectionStatus.CONNECTED
        assigned.save(update_fields=['store', 'connection_status', 'updated_at'])
        unassigned = self.account(SocialAccount.Platform.YOUTUBE, 'unassigned')
        disconnected = self.account(SocialAccount.Platform.INSTAGRAM, 'disconnected')
        disconnected.store = store
        disconnected.connection_status = SocialAccount.ConnectionStatus.ERROR
        disconnected.save(update_fields=['store', 'connection_status', 'updated_at'])
        response = self.client.get(reverse('public_store', kwargs={'slug': store.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['socials']), [assigned])
        self.assertNotContains(response, 'YouTube')


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost'])
class StoreCreationPhaseThreeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='phase-three-owner', password='password')
        CreatorProfile.objects.create(user=self.user, onboarding_status=CreatorProfile.OnboardingStatus.COMPLETED)
        self.matfit = CreatorStore.objects.create(owner=self.user, store_name='MatFit', slug='phase-three-matfit')
        self.instagram = SocialAccount.objects.create(
            owner=self.user,
            store=self.matfit,
            platform=SocialAccount.Platform.INSTAGRAM,
            account_name='matfit_daily',
            username='matfit_daily',
            status=SocialAccount.Status.ACTIVE,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.youtube = SocialAccount.objects.create(
            owner=self.user,
            platform=SocialAccount.Platform.YOUTUBE,
            account_name='싱송의생활템',
            display_name='싱송의생활템',
            status=SocialAccount.Status.ACTIVE,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.client.force_login(self.user)

    def create_store_step_one(self, name='싱송의생활템'):
        response = self.client.post(reverse('creator_store_new'), {'step': '1', 'store_name': name})
        self.assertRedirects(response, reverse('creator_store_new') + '?step=2', fetch_redirect_response=False)

    def test_creates_second_store_and_assigns_only_selected_unassigned_account(self):
        self.create_store_step_one()
        response = self.client.post(
            reverse('creator_store_new') + '?step=2',
            {'step': '2', 'account_ids': [str(self.youtube.pk)]},
        )
        self.assertRedirects(response, reverse('shoppinghub_home'), fetch_redirect_response=False)
        self.youtube.refresh_from_db()
        self.instagram.refresh_from_db()
        second_store = CreatorStore.objects.get(store_name='싱송의생활템')
        self.assertEqual(self.youtube.store, second_store)
        self.assertEqual(self.instagram.store, self.matfit)
        self.assertEqual(self.client.session['active_store_id'], second_store.pk)

    def test_unselected_existing_store_account_stays_in_original_store(self):
        self.create_store_step_one()
        response = self.client.post(
            reverse('creator_store_new') + '?step=2',
            {'step': '2', 'account_ids': [str(self.youtube.pk)]},
        )
        self.assertEqual(response.status_code, 302)
        self.instagram.refresh_from_db()
        self.assertEqual(self.instagram.store, self.matfit)

    def test_existing_store_account_requires_confirmation_before_move(self):
        self.create_store_step_one()
        response = self.client.post(
            reverse('creator_store_new') + '?step=2',
            {'step': '2', 'account_ids': [str(self.instagram.pk)]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '현재 MatFit Store에 연결되어 있습니다')
        self.assertFalse(CreatorStore.objects.filter(store_name='싱송의생활템').exists())
        self.instagram.refresh_from_db()
        self.assertEqual(self.instagram.store, self.matfit)

    def test_confirmed_existing_store_account_moves_to_new_store(self):
        self.create_store_step_one()
        response = self.client.post(
            reverse('creator_store_new') + '?step=2',
            {'step': '2', 'account_ids': [str(self.instagram.pk)], 'confirm_move': '1'},
        )
        self.assertRedirects(response, reverse('shoppinghub_home'), fetch_redirect_response=False)
        self.instagram.refresh_from_db()
        self.assertEqual(self.instagram.store.store_name, '싱송의생활템')

    def test_other_users_accounts_are_not_visible_or_assignable(self):
        other = User.objects.create_user(username='phase-three-other', password='password')
        SocialAccount.objects.create(
            owner=other,
            platform=SocialAccount.Platform.YOUTUBE,
            account_name='other-channel',
            status=SocialAccount.Status.ACTIVE,
        )
        self.create_store_step_one()
        response = self.client.get(reverse('creator_store_new') + '?step=2')
        self.assertNotContains(response, 'other-channel')

    def test_selector_lists_only_active_owned_stores(self):
        inactive = CreatorStore.objects.create(owner=self.user, store_name='Inactive', slug='phase-three-inactive', active=False)
        response = self.client.get(reverse('creator_home'))
        self.assertContains(response, 'MatFit')
        self.assertNotContains(response, 'Inactive')
        switch = self.client.post(reverse('creator_store_switch'), {'store_id': inactive.pk})
        self.assertEqual(switch.status_code, 404)


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost'])
class StoreChannelsPhaseFourTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='phase-four-owner', password='password')
        CreatorProfile.objects.create(user=self.user, onboarding_status=CreatorProfile.OnboardingStatus.COMPLETED)
        self.store = CreatorStore.objects.create(owner=self.user, store_name='MatFit', slug='phase-four-matfit')
        self.other_store = CreatorStore.objects.create(owner=self.user, store_name='싱송의생활템', slug='phase-four-singsong')
        self.instagram = SocialAccount.objects.create(
            owner=self.user, store=None, platform=SocialAccount.Platform.INSTAGRAM,
            account_name='matfit_daily', username='matfit_daily',
            status=SocialAccount.Status.ACTIVE,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.youtube = SocialAccount.objects.create(
            owner=self.user, store=self.other_store, platform=SocialAccount.Platform.YOUTUBE,
            account_name='싱송의생활템', display_name='싱송의생활템',
            status=SocialAccount.Status.ACTIVE,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        SocialCredential.objects.create(
            social_account=self.instagram, provider='META_INSTAGRAM',
            access_token_encrypted='instagram-token',
        )
        SocialCredential.objects.create(
            social_account=self.youtube, provider='GOOGLE_YOUTUBE',
            access_token_encrypted='youtube-token',
        )
        self.client.force_login(self.user)
        session = self.client.session
        session['active_store_id'] = self.store.pk
        session.save()

    def test_channels_show_current_unassigned_and_other_owned_accounts(self):
        response = self.client.get(reverse('creator_channels'))
        self.assertContains(response, 'matfit_daily')
        self.assertContains(response, '싱송의생활템')
        self.assertContains(response, '미배정')
        self.assertContains(response, '다른 Store에 연결된 내 SNS')

    def test_unassigned_account_is_assigned_without_oauth_and_keeps_credential(self):
        response = self.client.post(reverse('creator_channels'), {'action': 'assign', 'account_id': self.instagram.pk})
        self.assertRedirects(response, reverse('creator_channels'), fetch_redirect_response=False)
        self.instagram.refresh_from_db()
        self.assertEqual(self.instagram.store, self.store)
        self.assertTrue(SocialCredential.objects.filter(social_account=self.instagram).exists())

    def test_other_store_account_requires_confirmation_then_moves(self):
        response = self.client.post(reverse('creator_channels'), {'action': 'assign', 'account_id': self.youtube.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'MatFit Store로 이동하시겠습니까')
        self.youtube.refresh_from_db()
        self.assertEqual(self.youtube.store, self.other_store)
        response = self.client.post(reverse('creator_channels'), {'action': 'assign', 'account_id': self.youtube.pk, 'confirm': '1'})
        self.assertRedirects(response, reverse('creator_channels'), fetch_redirect_response=False)
        self.youtube.refresh_from_db()
        self.assertEqual(self.youtube.store, self.store)

    def test_move_cancel_keeps_original_store(self):
        response = self.client.post(reverse('creator_channels'), {'action': 'assign', 'account_id': self.youtube.pk})
        self.assertEqual(response.status_code, 200)
        self.youtube.refresh_from_db()
        self.assertEqual(self.youtube.store, self.other_store)

    def test_store_removal_clears_assignment_but_keeps_connection(self):
        self.instagram.store = self.store
        self.instagram.save(update_fields=['store', 'updated_at'])
        response = self.client.post(reverse('creator_channels'), {'action': 'unassign', 'account_id': self.instagram.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'SNS 로그인 연결 자체는 유지됩니다')
        self.instagram.refresh_from_db()
        self.assertEqual(self.instagram.store, self.store)
        response = self.client.post(reverse('creator_channels'), {'action': 'unassign', 'account_id': self.instagram.pk, 'confirm': '1'})
        self.assertRedirects(response, reverse('creator_channels'), fetch_redirect_response=False)
        self.instagram.refresh_from_db()
        self.assertIsNone(self.instagram.store)
        self.assertEqual(self.instagram.connection_status, SocialAccount.ConnectionStatus.CONNECTED)
        self.assertTrue(SocialCredential.objects.filter(social_account=self.instagram).exists())

    @patch('linker.creator_views.instagram_save_credential')
    @patch('linker.creator_views.instagram_verify_account')
    @patch('linker.creator_views.instagram_exchange_code')
    def test_channel_instagram_oauth_assigns_new_account_to_active_store(self, exchange, verify, save):
        session = self.client.session
        session['instagram_auto_oauth'] = {'state': 'state-i', 'flow': 'channel', 'store_id': self.store.pk, 'return_to': ''}
        session.save()
        exchange.return_value = {'access_token': 'token', 'scopes': []}
        verify.return_value = {'user_id': 'new-ig', 'username': 'singsong_life', 'name': 'Singsong'}
        response = self.client.get(reverse('instagram_auto_callback') + '?state=state-i&code=code')
        self.assertRedirects(response, reverse('creator_channels'), fetch_redirect_response=False)
        account = SocialAccount.objects.get(platform='INSTAGRAM', platform_user_id='new-ig')
        self.assertEqual(account.store, self.store)
        save.assert_called_once()

    @patch('linker.creator_views.youtube_save_verified_credential')
    @patch('linker.creator_views.youtube_verify_channel')
    @patch('linker.creator_views.youtube_exchange_code')
    def test_channel_youtube_oauth_assigns_new_account_to_active_store(self, exchange, verify, save):
        session = self.client.session
        session['youtube_auto_oauth'] = {'state': 'state-y', 'code_verifier': 'verifier', 'flow': 'channel', 'store_id': self.store.pk, 'return_to': ''}
        session.save()
        exchange.return_value = SimpleNamespace()
        verify.return_value = {'id': 'new-yt', 'title': 'New Channel', 'custom_url': '@new-channel'}
        response = self.client.get(reverse('youtube_auto_callback') + '?state=state-y&code=code')
        self.assertRedirects(response, reverse('creator_channels'), fetch_redirect_response=False)
        account = SocialAccount.objects.get(platform='YOUTUBE', platform_user_id='new-yt')
        self.assertEqual(account.store, self.store)
        save.assert_called_once()

    @patch('linker.creator_views.instagram_save_credential')
    @patch('linker.creator_views.instagram_verify_account')
    @patch('linker.creator_views.instagram_exchange_code')
    def test_same_instagram_oauth_updates_existing_account_without_duplicate(self, exchange, verify, save):
        session = self.client.session
        session['instagram_auto_oauth'] = {'state': 'state-i2', 'flow': 'channel', 'store_id': self.store.pk, 'return_to': ''}
        session.save()
        exchange.return_value = {'access_token': 'new-token', 'scopes': []}
        verify.return_value = {'user_id': 'matfit_daily-id', 'username': 'matfit_daily', 'name': 'MatFit'}
        self.instagram.platform_user_id = 'matfit_daily-id'
        self.instagram.external_account_id = 'matfit_daily-id'
        self.instagram.save(update_fields=['platform_user_id', 'external_account_id', 'updated_at'])
        self.client.get(reverse('instagram_auto_callback') + '?state=state-i2&code=code')
        self.assertEqual(SocialAccount.objects.filter(platform='INSTAGRAM', platform_user_id='matfit_daily-id').count(), 1)

    def test_other_users_account_is_not_visible_or_assignable(self):
        other = User.objects.create_user(username='phase-four-other', password='password')
        account = SocialAccount.objects.create(owner=other, platform='INSTAGRAM', account_name='secret-account', status='ACTIVE')
        response = self.client.get(reverse('creator_channels'))
        self.assertNotContains(response, 'secret-account')
        response = self.client.post(reverse('creator_channels'), {'action': 'assign', 'account_id': account.pk})
        self.assertEqual(response.status_code, 404)

    def test_korean_store_name_gets_random_url_safe_slug(self):
        self.client.post(reverse('creator_store_new'), {'step': '1', 'store_name': '새로운생활템'})
        response = self.client.post(reverse('creator_store_new') + '?step=2', {'step': '2'})
        self.assertEqual(response.status_code, 302)
        store = CreatorStore.objects.get(store_name='새로운생활템')
        self.assertRegex(store.slug, r'^store-[0-9a-f]{6}$')


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost'])
class ShoppingHubV2VisibleFlowTests(TestCase):
    def signup_payload(self, username='new-creator'):
        return {
            'username': username,
            'password1': 'Strong-pass-123',
            'password2': 'Strong-pass-123',
        }

    def complete_creator(self, username='owner'):
        user = User.objects.create_user(username=username, password='Strong-pass-123')
        CreatorProfile.objects.create(user=user, onboarding_status=CreatorProfile.OnboardingStatus.COMPLETED)
        store = CreatorStore.objects.create(owner=user, store_name=f'{username} Store', slug=username, tagline='Useful finds')
        return user, store

    def http_response(self, url, text='', status_code=200, location=''):
        return SimpleNamespace(
            url=url,
            text=text,
            status_code=status_code,
            headers={'location': location} if location else {},
            raise_for_status=lambda: None,
            close=lambda: None,
        )

    def test_01_signup_creates_profile_then_enters_onboarding(self):
        response = self.client.get(reverse('signup'))
        self.assertContains(response, '01 / 06')
        self.assertContains(response, '무료로 시작하기')
        self.assertNotContains(response, 'form.email')

        response = self.client.post(reverse('signup'), self.signup_payload())
        user = User.objects.get(username='new-creator')

        self.assertRedirects(response, reverse('creator_onboarding_start'), fetch_redirect_response=False)
        self.assertEqual(user.creator_profile.onboarding_status, CreatorProfile.OnboardingStatus.ACCOUNT_CREATED)
        self.assertFalse(CreatorStore.objects.filter(owner=user).exists())

    def test_deployed_signup_prefix_alias_is_available(self):
        response = self.client.get('/shoppinghub/signup/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '무료로 시작하기')

    def test_01_to_06_onboarding_uses_store_reuse_and_auto_slug(self):
        user = User.objects.create_user(username='journey-owner', password='Strong-pass-123')
        CreatorProfile.objects.create(user=user)
        self.client.force_login(user)

        response = self.client.get(reverse('creator_onboarding_start'))
        self.assertRedirects(response, reverse('creator_onboarding_store'), fetch_redirect_response=False)

        response = self.client.get(reverse('creator_onboarding_store'))
        self.assertContains(response, '02 / 06')
        self.assertContains(response, 'STEP 1 / 5')
        self.assertContains(response, reverse('creator_instagram_connect') + '?return=onboarding_profile')
        self.assertContains(response, reverse('creator_youtube_connect') + '?return=onboarding_profile')
        self.assertNotContains(response, 'name="slug"')

        response = self.client.post(reverse('creator_onboarding_store'), {'step': '1'})
        self.assertRedirects(response, reverse('creator_onboarding_store') + '?step=2', fetch_redirect_response=False)

        response = self.client.get(reverse('creator_onboarding_store') + '?step=2')
        self.assertContains(response, 'name="store_name"')
        response = self.client.post(reverse('creator_onboarding_store') + '?step=2', {'step': '2', 'store_name': 'Journey Store'})
        store = CreatorStore.objects.get(owner=user)
        self.assertRedirects(response, reverse('creator_onboarding_store') + '?step=3', fetch_redirect_response=False)
        self.assertEqual(store.slug, 'journey-owner')
        self.assertTrue(store.active)

        response = self.client.get(reverse('creator_onboarding_profile'))
        self.assertRedirects(response, reverse('creator_onboarding_store') + '?step=3', fetch_redirect_response=False)

        response = self.client.get(reverse('creator_onboarding_store') + '?step=3')
        self.assertContains(response, 'STEP 3 / 5')
        response = self.client.post(reverse('creator_onboarding_store') + '?step=3', {'step': '3', 'skip': '1'})
        self.assertRedirects(response, reverse('creator_onboarding_store') + '?step=4', fetch_redirect_response=False)

        response = self.client.get(reverse('creator_onboarding_intro'))
        self.assertRedirects(response, reverse('creator_onboarding_store') + '?step=4', fetch_redirect_response=False)
        response = self.client.get(reverse('creator_onboarding_store') + '?step=4')
        self.assertContains(response, 'STEP 4 / 5')
        response = self.client.post(reverse('creator_onboarding_store') + '?step=4', {'step': '4', 'tagline': '  Daily picks  '})
        store.refresh_from_db()
        self.assertRedirects(response, reverse('creator_onboarding_store') + '?step=5', fetch_redirect_response=False)
        self.assertEqual(store.tagline, 'Daily picks')

        response = self.client.get(reverse('creator_onboarding_address'))
        self.assertRedirects(response, reverse('creator_onboarding_store') + '?step=5', fetch_redirect_response=False)
        response = self.client.get(reverse('creator_onboarding_store') + '?step=5')
        self.assertContains(response, 'STEP 5 / 5')
        self.assertIn('shoppinghub / <span id="slug-preview">journey-owner</span>', response.content.decode())
        self.assertContains(response, 'name="slug"')

        response = self.client.post(reverse('creator_onboarding_store') + '?step=5', {'step': '5', 'slug': 'journey-shop'})
        store.refresh_from_db()
        user.creator_profile.refresh_from_db()
        self.assertRedirects(response, reverse('creator_onboarding_complete'), fetch_redirect_response=False)
        self.assertEqual(store.slug, 'journey-shop')
        self.assertTrue(store.active)
        self.assertEqual(user.creator_profile.onboarding_status, CreatorProfile.OnboardingStatus.COMPLETED)

        response = self.client.get(reverse('creator_onboarding_complete'))
        self.assertContains(response, '06 / 06')
        self.assertContains(response, reverse('shoppinghub_products'))
        self.assertContains(response, reverse('public_store', kwargs={'slug': store.slug}))

    def test_store_name_step_reuses_existing_store_without_duplicate(self):
        user = User.objects.create_user(username='repeat-store', password='Strong-pass-123')
        CreatorProfile.objects.create(user=user)
        store = CreatorStore.objects.create(owner=user, store_name='First Store', slug='repeat-store')
        self.client.force_login(user)

        self.client.post(reverse('creator_onboarding_store') + '?step=2', {'step': '2', 'store_name': 'Updated Store'})
        store.refresh_from_db()

        self.assertEqual(CreatorStore.objects.filter(owner=user).count(), 1)
        self.assertEqual(store.store_name, 'Updated Store')
        self.assertEqual(store.slug, 'repeat-store')

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    @patch('linker.creator_views.requests.get')
    def test_03_social_profile_choice_saves_store_profile_image(self, mocked_get):
        user = User.objects.create_user(username='social-photo', password='Strong-pass-123')
        CreatorProfile.objects.create(user=user, onboarding_status=CreatorProfile.OnboardingStatus.PROFILE_SELECTION)
        store = CreatorStore.objects.create(owner=user, store_name='Social Store', slug='social-photo')
        account = SocialAccount.objects.create(
            owner=user,
            store=store,
            platform=SocialAccount.Platform.INSTAGRAM,
            account_name='social-photo',
            external_account_id='ig-social-photo',
            profile_picture_url='https://cdn.example.com/profile.jpg',
            status=SocialAccount.Status.ACTIVE,
        )
        SocialCredential.objects.create(
            social_account=account,
            provider='META_INSTAGRAM',
            access_token_encrypted='token',
            scopes=[],
        )
        mocked_get.return_value.headers = {'Content-Type': 'image/jpeg'}
        mocked_get.return_value.content = b'jpeg-bytes'
        mocked_get.return_value.raise_for_status.return_value = None
        self.client.force_login(user)

        response = self.client.post(
            reverse('creator_onboarding_store') + '?step=3',
            {'step': '3', 'account_id': str(account.pk), 'profile_image_source': 'social'},
        )
        store.refresh_from_db()
        user.creator_profile.refresh_from_db()

        self.assertRedirects(response, reverse('creator_onboarding_store') + '?step=4', fetch_redirect_response=False)
        self.assertEqual(user.creator_profile.representative_social_account, account)
        self.assertTrue(store.profile_image.name.endswith('.jpg'))
        response = self.client.get(reverse('creator_onboarding_store') + '?step=3')
        self.assertContains(response, store.profile_image.url)
        self.assertContains(response, f'value="{account.pk}"', html=False)

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    @patch('linker.creator_views.requests.get')
    def test_03_youtube_profile_choice_saves_store_profile_image(self, mocked_get):
        user = User.objects.create_user(username='youtube-photo', password='Strong-pass-123')
        CreatorProfile.objects.create(user=user, onboarding_status=CreatorProfile.OnboardingStatus.PROFILE_SELECTION)
        store = CreatorStore.objects.create(owner=user, store_name='YouTube Store', slug='youtube-photo')
        account = SocialAccount.objects.create(
            owner=user,
            store=store,
            platform=SocialAccount.Platform.YOUTUBE,
            account_name='YouTube Channel',
            external_account_id='yt-channel',
            platform_user_id='yt-channel',
            display_name='YouTube Channel',
            profile_picture_url='https://yt.example.com/avatar.png',
            status=SocialAccount.Status.ACTIVE,
        )
        SocialCredential.objects.create(
            social_account=account,
            provider='GOOGLE_YOUTUBE',
            access_token_encrypted='token',
            scopes=[],
        )
        mocked_get.return_value.headers = {'Content-Type': 'image/png'}
        mocked_get.return_value.content = b'png-bytes'
        mocked_get.return_value.raise_for_status.return_value = None
        self.client.force_login(user)

        response = self.client.post(
            reverse('creator_onboarding_store') + '?step=3',
            {'step': '3', 'account_id': str(account.pk), 'profile_image_source': 'social'},
        )
        store.refresh_from_db()
        user.creator_profile.refresh_from_db()

        self.assertRedirects(response, reverse('creator_onboarding_store') + '?step=4', fetch_redirect_response=False)
        self.assertEqual(user.creator_profile.representative_social_account, account)
        self.assertTrue(store.profile_image.name.endswith('.png'))
        response = self.client.get(reverse('creator_onboarding_store') + '?step=3')
        self.assertContains(response, store.profile_image.url)
        self.assertContains(response, 'https://yt.example.com/avatar.png')

    def test_connected_sns_profile_is_used_as_store_setup_default(self):
        user = User.objects.create_user(username='sns-default', password='Strong-pass-123')
        profile = CreatorProfile.objects.create(user=user, onboarding_status=CreatorProfile.OnboardingStatus.ACCOUNT_CREATED)
        account = SocialAccount.objects.create(
            owner=user,
            platform=SocialAccount.Platform.INSTAGRAM,
            account_name='singsong_pick',
            username='singsong_pick',
            display_name='싱송의 생활템',
            profile_picture_url='https://cdn.example.com/singsong.jpg',
            status=SocialAccount.Status.ACTIVE,
        )
        SocialCredential.objects.create(
            social_account=account,
            provider='META_INSTAGRAM',
            access_token_encrypted='token',
            scopes=[],
        )
        self.client.force_login(user)

        response = self.client.get(reverse('creator_onboarding_store'))
        self.assertContains(response, '싱송의 생활템')
        self.assertContains(response, 'https://cdn.example.com/singsong.jpg')

        response = self.client.post(reverse('creator_onboarding_store'), {'step': '1'})
        profile.refresh_from_db()

        self.assertRedirects(response, reverse('creator_onboarding_store') + '?step=2', fetch_redirect_response=False)
        self.assertEqual(profile.representative_social_account, account)

        response = self.client.get(reverse('creator_onboarding_store') + '?step=3')
        self.assertContains(response, f'value="{account.pk}"', html=False)
        self.assertContains(response, 'https://cdn.example.com/singsong.jpg')

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_03_profile_image_upload_saves_and_persists(self):
        image = BytesIO()
        Image.new('RGB', (1, 1), color='white').save(image, format='PNG')
        image.seek(0)
        user = User.objects.create_user(username='upload-photo', password='Strong-pass-123')
        CreatorProfile.objects.create(user=user, onboarding_status=CreatorProfile.OnboardingStatus.PROFILE_SELECTION)
        store = CreatorStore.objects.create(owner=user, store_name='Upload Store', slug='upload-photo')
        self.client.force_login(user)

        response = self.client.post(
            reverse('creator_onboarding_store') + '?step=3',
            {'step': '3', 'profile_image': SimpleUploadedFile('profile.png', image.read(), content_type='image/png')},
        )
        store.refresh_from_db()

        self.assertRedirects(response, reverse('creator_onboarding_store') + '?step=4', fetch_redirect_response=False)
        self.assertTrue(store.profile_image.name)
        self.assertContains(self.client.get(reverse('creator_onboarding_store') + '?step=4'), 'STEP 4 / 5')

    @override_settings(GOOGLE_YOUTUBE_CREDENTIAL_KEY='K' * 43 + '=')
    def test_youtube_oauth_persists_reusable_profile_fields(self):
        user = User.objects.create_user(username='yt-oauth', password='Strong-pass-123')
        store = CreatorStore.objects.create(owner=user, store_name='YT Store', slug='yt-oauth')
        account = SocialAccount.objects.create(
            owner=user,
            store=store,
            platform=SocialAccount.Platform.YOUTUBE,
            account_name='Old Name',
            external_account_id='',
        )
        credentials = SimpleNamespace(
            token='access-token',
            refresh_token='refresh-token',
            token_uri='https://oauth2.googleapis.com/token',
            scopes=['https://www.googleapis.com/auth/youtube.readonly'],
            granted_scopes=['https://www.googleapis.com/auth/youtube.readonly'],
            expiry=None,
        )

        save_youtube_credential(account, credentials, {
            'id': 'channel-123',
            'title': 'Creator Channel',
            'custom_url': '@creator',
            'thumbnail_url': 'https://yt.example.com/high.jpg',
            'description': 'Original channel intro',
        })
        account.refresh_from_db()

        self.assertEqual(account.external_account_id, 'channel-123')
        self.assertEqual(account.platform_user_id, 'channel-123')
        self.assertEqual(account.username, 'creator')
        self.assertEqual(account.display_name, 'Creator Channel')
        self.assertEqual(account.profile_url, 'https://www.youtube.com/@creator')
        self.assertEqual(account.profile_picture_url, 'https://yt.example.com/high.jpg')
        self.assertEqual(account.memo, 'Original channel intro')
        self.assertEqual(account.connection_status, SocialAccount.ConnectionStatus.CONNECTED)

    @override_settings(GOOGLE_YOUTUBE_CREDENTIAL_KEY='K' * 43 + '=')
    @patch('linker.services.instagram_oauth.requests.get')
    def test_instagram_oauth_bio_is_saved_for_onboarding_intro(self, mocked_get):
        core_response = SimpleNamespace(
            ok=True,
            json=lambda: {
                'user_id': 'ig-123',
                'username': 'creator_ig',
                'name': 'Creator IG',
                'account_type': 'BUSINESS',
                'profile_picture_url': 'https://cdn.example.com/ig.jpg',
            },
        )
        bio_response = SimpleNamespace(ok=True, json=lambda: {'biography': 'Original Instagram bio'})
        mocked_get.side_effect = [core_response, bio_response]
        profile = verify_instagram_account('access-token')

        user = User.objects.create_user(username='ig-bio-owner', password='Strong-pass-123')
        store = CreatorStore.objects.create(owner=user, store_name='IG Bio Store', slug='ig-bio-owner')
        account = SocialAccount.objects.create(
            owner=user,
            store=store,
            platform=SocialAccount.Platform.INSTAGRAM,
            account_name='creator_ig',
        )
        save_instagram_credential(account, {'access_token': 'access-token', 'scopes': []}, profile)
        account.refresh_from_db()

        requested_fields = [call.kwargs['params']['fields'] for call in mocked_get.call_args_list]
        self.assertEqual(requested_fields[0], 'user_id,username,name,account_type,profile_picture_url')
        self.assertIn('biography', requested_fields)
        self.assertEqual(account.memo, 'Original Instagram bio')

    @patch('linker.services.instagram_oauth.requests.get')
    def test_instagram_oauth_without_bio_keeps_fallback_flow(self, mocked_get):
        core_response = SimpleNamespace(ok=True, json=lambda: {'user_id': 'ig-empty', 'username': 'empty_ig'})
        unsupported_biography = SimpleNamespace(ok=False, json=lambda: {})
        unsupported_bio = SimpleNamespace(ok=False, json=lambda: {})
        mocked_get.side_effect = [core_response, unsupported_biography, unsupported_bio]

        profile = verify_instagram_account('access-token')

        self.assertNotIn('biography', profile)
        self.assertEqual(mocked_get.call_count, 3)

    def test_store_setup_uses_sns_name_and_intro_without_mutating_source(self):
        user = User.objects.create_user(username='sns-source', password='Strong-pass-123')
        CreatorProfile.objects.create(user=user, onboarding_status=CreatorProfile.OnboardingStatus.STORE_NAMING)
        store = CreatorStore.objects.create(owner=user, store_name='Draft Store', slug='sns-source')
        instagram = SocialAccount.objects.create(
            owner=user,
            store=store,
            platform=SocialAccount.Platform.INSTAGRAM,
            account_name='insta',
            username='insta_user',
            display_name='Instagram Name',
            profile_picture_url='https://cdn.example.com/ig.jpg',
            memo='Instagram original intro',
            status=SocialAccount.Status.ACTIVE,
        )
        youtube = SocialAccount.objects.create(
            owner=user,
            store=store,
            platform=SocialAccount.Platform.YOUTUBE,
            account_name='youtube',
            username='youtube_user',
            display_name='YouTube Name',
            profile_picture_url='https://cdn.example.com/yt.jpg',
            memo='YouTube original intro',
            status=SocialAccount.Status.ACTIVE,
        )
        SocialCredential.objects.create(social_account=instagram, provider='META_INSTAGRAM', access_token_encrypted='token', scopes=[])
        SocialCredential.objects.create(social_account=youtube, provider='GOOGLE_YOUTUBE', access_token_encrypted='token', scopes=[])
        self.client.force_login(user)

        response = self.client.get(reverse('creator_onboarding_store') + '?step=2')
        self.assertContains(response, 'Instagram Name')
        self.assertContains(response, 'YouTube Name')

        response = self.client.post(reverse('creator_onboarding_store') + '?step=2', {'step': '2', 'store_name': 'Creator Edited Store'})
        self.assertRedirects(response, reverse('creator_onboarding_store') + '?step=3', fetch_redirect_response=False)
        store.refresh_from_db()
        self.assertEqual(store.store_name, 'Creator Edited Store')

        response = self.client.get(reverse('creator_onboarding_store') + '?step=4')
        self.assertContains(response, 'Instagram SNS 소개 다시 가져오기')
        self.assertContains(response, 'YouTube SNS 소개 다시 가져오기')
        self.assertContains(response, 'Instagram original intro')
        self.assertContains(response, 'YouTube original intro')
        response = self.client.post(reverse('creator_onboarding_store') + '?step=4', {'step': '4', 'tagline': 'ShoppingHub edited intro'})
        self.assertRedirects(response, reverse('creator_onboarding_store') + '?step=5', fetch_redirect_response=False)
        store.refresh_from_db()
        instagram.refresh_from_db()
        youtube.refresh_from_db()
        self.assertEqual(store.tagline, 'ShoppingHub edited intro')
        self.assertEqual(instagram.memo, 'Instagram original intro')
        self.assertEqual(youtube.memo, 'YouTube original intro')
        response = self.client.get(reverse('creator_onboarding_store') + '?step=4')
        self.assertContains(response, 'ShoppingHub edited intro')
        self.assertContains(response, 'data-intro="Instagram original intro"', html=False)

    @override_settings(GOOGLE_YOUTUBE_CREDENTIAL_KEY='K' * 43 + '=')
    @patch('linker.services.instagram_oauth.requests.get')
    def test_step4_refresh_fetches_instagram_bio_without_reconnect_when_memo_empty(self, mocked_get):
        user = User.objects.create_user(username='ig-refresh', password='Strong-pass-123')
        CreatorProfile.objects.create(user=user, onboarding_status=CreatorProfile.OnboardingStatus.STORE_INTRO)
        store = CreatorStore.objects.create(owner=user, store_name='IG Refresh Store', slug='ig-refresh', tagline='Edited Store intro')
        account = SocialAccount.objects.create(
            owner=user,
            store=store,
            platform=SocialAccount.Platform.INSTAGRAM,
            account_name='creator_ig',
            external_account_id='ig-refresh-id',
            platform_user_id='ig-refresh-id',
            username='creator_ig',
            status=SocialAccount.Status.ACTIVE,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        save_instagram_credential(account, {'access_token': 'access-token', 'scopes': []}, {'user_id': 'ig-refresh-id', 'username': 'creator_ig'})
        account.memo = ''
        account.save(update_fields=['memo', 'updated_at'])
        mocked_get.return_value = SimpleNamespace(ok=True, json=lambda: {'biography': 'Fresh Instagram bio'})
        self.client.force_login(user)

        response = self.client.post(
            reverse('creator_onboarding_store') + f'?step=4&intro_account_id={account.pk}',
            {'step': '4', 'action': 'refresh_intro', 'tagline': 'Unsaved local edit'},
        )
        account.refresh_from_db()
        store.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Fresh Instagram bio')
        self.assertEqual(account.memo, 'Fresh Instagram bio')
        self.assertEqual(store.tagline, 'Edited Store intro')

    @patch('linker.creator_views.instagram_fetch_profile_intro')
    def test_step4_refresh_instagram_token_error_shows_reconnect_without_overwriting_store(self, fetch_intro):
        user = User.objects.create_user(username='ig-expired', password='Strong-pass-123')
        CreatorProfile.objects.create(user=user, onboarding_status=CreatorProfile.OnboardingStatus.STORE_INTRO)
        store = CreatorStore.objects.create(owner=user, store_name='IG Expired Store', slug='ig-expired', tagline='Kept Store intro')
        account = SocialAccount.objects.create(
            owner=user,
            store=store,
            platform=SocialAccount.Platform.INSTAGRAM,
            account_name='creator_ig',
            external_account_id='ig-expired-id',
            platform_user_id='ig-expired-id',
            status=SocialAccount.Status.ACTIVE,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        SocialCredential.objects.create(social_account=account, provider='META_INSTAGRAM', access_token_encrypted='token', scopes=[])
        fetch_intro.side_effect = InstagramOAuthError('expired')
        self.client.force_login(user)

        response = self.client.post(
            reverse('creator_onboarding_store') + f'?step=4&intro_account_id={account.pk}',
            {'step': '4', 'action': 'refresh_intro', 'tagline': 'Do not save this'},
        )
        store.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Instagram 다시 연결하기가 필요합니다.')
        self.assertContains(response, 'Kept Store intro')
        self.assertEqual(store.tagline, 'Kept Store intro')

    def test_step4_get_does_not_auto_overwrite_store_tagline_from_sns_memo(self):
        user = User.objects.create_user(username='no-auto-intro', password='Strong-pass-123')
        CreatorProfile.objects.create(user=user, onboarding_status=CreatorProfile.OnboardingStatus.STORE_INTRO)
        store = CreatorStore.objects.create(owner=user, store_name='No Auto Store', slug='no-auto-intro', tagline='Creator edited intro')
        account = SocialAccount.objects.create(
            owner=user,
            store=store,
            platform=SocialAccount.Platform.INSTAGRAM,
            account_name='creator_ig',
            external_account_id='ig-no-auto',
            platform_user_id='ig-no-auto',
            memo='SNS original intro',
            status=SocialAccount.Status.ACTIVE,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        SocialCredential.objects.create(social_account=account, provider='META_INSTAGRAM', access_token_encrypted='token', scopes=[])
        self.client.force_login(user)

        response = self.client.get(reverse('creator_onboarding_store') + '?step=4')
        store.refresh_from_db()

        self.assertContains(response, 'Creator edited intro')
        self.assertContains(response, 'data-intro="SNS original intro"', html=False)
        self.assertEqual(store.tagline, 'Creator edited intro')

    @patch('linker.creator_views.youtube_fetch_channel_intro')
    def test_step4_refresh_fetches_youtube_description_without_reconnect(self, fetch_intro):
        user = User.objects.create_user(username='yt-refresh', password='Strong-pass-123')
        CreatorProfile.objects.create(user=user, onboarding_status=CreatorProfile.OnboardingStatus.STORE_INTRO)
        store = CreatorStore.objects.create(owner=user, store_name='YT Refresh Store', slug='yt-refresh', tagline='YT kept intro')
        account = SocialAccount.objects.create(
            owner=user,
            store=store,
            platform=SocialAccount.Platform.YOUTUBE,
            account_name='Creator YouTube',
            external_account_id='yt-refresh-id',
            platform_user_id='yt-refresh-id',
            status=SocialAccount.Status.ACTIVE,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        SocialCredential.objects.create(social_account=account, provider='GOOGLE_YOUTUBE', access_token_encrypted='token', scopes=[])
        def refresh_youtube_intro(item):
            SocialAccount.objects.filter(pk=item.pk).update(memo='Fresh YouTube description')
            return 'Fresh YouTube description'
        fetch_intro.side_effect = refresh_youtube_intro
        self.client.force_login(user)

        response = self.client.post(
            reverse('creator_onboarding_store') + f'?step=4&intro_account_id={account.pk}',
            {'step': '4', 'action': 'refresh_intro', 'tagline': 'Unsaved YouTube edit'},
        )
        account.refresh_from_db()
        store.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Fresh YouTube description')
        self.assertEqual(account.memo, 'Fresh YouTube description')
        self.assertEqual(store.tagline, 'YT kept intro')

    def test_store_setup_slug_validation_and_no_sns_completion(self):
        other = User.objects.create_user(username='other-owner', password='Strong-pass-123')
        CreatorProfile.objects.create(user=other, onboarding_status=CreatorProfile.OnboardingStatus.COMPLETED)
        CreatorStore.objects.create(owner=other, store_name='Other Store', slug='taken')
        user = User.objects.create_user(username='no-sns-owner', password='Strong-pass-123')
        CreatorProfile.objects.create(user=user)
        self.client.force_login(user)

        self.client.post(reverse('creator_onboarding_store'), {'step': '1'})
        self.client.post(reverse('creator_onboarding_store') + '?step=2', {'step': '2', 'store_name': 'No SNS Store'})
        self.client.post(reverse('creator_onboarding_store') + '?step=3', {'step': '3', 'skip': '1'})
        self.client.post(reverse('creator_onboarding_store') + '?step=4', {'step': '4', 'tagline': ''})

        response = self.client.post(reverse('creator_onboarding_store') + '?step=5', {'step': '5', 'slug': 'taken'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '이미 사용 중인 Store 주소입니다.')

        response = self.client.post(reverse('creator_onboarding_store') + '?step=5', {'step': '5', 'slug': 'no-sns-shop'})
        store = CreatorStore.objects.get(owner=user)
        user.creator_profile.refresh_from_db()
        self.assertRedirects(response, reverse('creator_onboarding_complete'), fetch_redirect_response=False)
        self.assertEqual(store.slug, 'no-sns-shop')
        self.assertEqual(user.creator_profile.onboarding_status, CreatorProfile.OnboardingStatus.COMPLETED)

    def test_07_to_11_creator_workspace_and_billing_are_visible(self):
        user, store = self.complete_creator('workspace-owner')
        self.client.force_login(user)

        response = self.client.get(reverse('creator_home'))
        self.assertContains(response, '07 / CREATOR HOME')
        self.assertContains(response, reverse('shoppinghub_products'))
        self.assertContains(response, reverse('shoppinghub_auto_dm'))
        self.assertContains(response, reverse('shoppinghub_display'))
        self.assertContains(response, reverse('shoppinghub_stats'))
        self.assertContains(response, reverse('public_store', kwargs={'slug': store.slug}))

        response = self.client.get(reverse('shoppinghub_products'))
        self.assertContains(response, '08 / PRODUCTS')

        response = self.client.get(reverse('shoppinghub_auto_dm'))
        self.assertEqual(response.status_code, 200)

        response = self.client.get(reverse('shoppinghub_display'))
        self.assertContains(response, '10 / STORE DISPLAY')

        response = self.client.get(reverse('shoppinghub_stats'))
        self.assertContains(response, '11 / STATS')

        response = self.client.get(reverse('creator_billing'))
        self.assertContains(response, '요금제')
        self.assertContains(response, '요금제·결제')

    def test_08_product_category_select_creates_updates_and_preserves_existing(self):
        user, store = self.complete_creator('category-owner')
        marketplace = Marketplace.objects.create(code='COUPANG', name='Coupang')
        legacy_category = StoreCategory.objects.create(owner=user, store=store, name='Legacy', display_order=99)
        legacy_product = Linker.objects.create(
            owner=user,
            title='Legacy Product',
            product_no='900',
            marketplace=marketplace,
            destination_url='https://example.com/legacy',
            store_category=legacy_category,
            status='ACTIVE',
        )
        self.client.force_login(user)

        response = self.client.get(reverse('shoppinghub_product_new'))
        self.assertContains(response, '<select', html=False)
        food = StoreCategory.objects.get(owner=user, name='식품')
        fashion = StoreCategory.objects.get(owner=user, name='패션')
        legacy_product.refresh_from_db()
        self.assertEqual(legacy_product.store_category, legacy_category)

        response = self.client.post(reverse('shoppinghub_product_new'), {
            'flow': 'single',
            'product_no': '100',
            'title': 'New Product',
            'thumbnail_url': '',
            'marketplace': marketplace.pk,
            'destination_url': 'https://example.com/new',
            'description': '',
            'store_category': food.pk,
            'display_order': '0',
            'store_visible': 'on',
        })
        self.assertRedirects(response, reverse('shoppinghub_products'), fetch_redirect_response=False)
        product = Linker.objects.get(owner=user, product_no='100')
        self.assertEqual(product.store_category, food)

        response = self.client.get(reverse('shoppinghub_product_edit', kwargs={'pk': product.pk}))
        self.assertContains(response, f'value="{food.pk}" selected', html=False)

        response = self.client.post(reverse('shoppinghub_product_edit', kwargs={'pk': product.pk}), {
            'product_no': product.product_no,
            'title': product.title,
            'thumbnail_url': product.thumbnail_url,
            'marketplace': marketplace.pk,
            'destination_url': product.destination_url,
            'description': product.description,
            'store_category': fashion.pk,
            'display_order': str(product.display_order),
            'store_visible': 'on',
        })
        self.assertRedirects(response, reverse('shoppinghub_products'), fetch_redirect_response=False)
        product.refresh_from_db()
        legacy_product.refresh_from_db()
        self.assertEqual(product.store_category, fashion)
        self.assertEqual(legacy_product.store_category, legacy_category)

        response = self.client.get(reverse('shoppinghub_product_edit', kwargs={'pk': product.pk}))
        self.assertContains(response, f'value="{fashion.pk}" selected', html=False)

    def test_product_management_ui_separates_empty_import_and_registered_items(self):
        user, store = self.complete_creator('products-ui-owner')
        category = StoreCategory.objects.create(owner=user, store=store, name='생활', display_order=1)
        marketplace = Marketplace.objects.create(code='COUPANG', name='Coupang')
        self.client.force_login(user)

        response = self.client.get(reverse('shoppinghub_products'))

        self.assertContains(response, '상품 관리')
        self.assertContains(response, 'ShoppingHub에서 소개할 상품을 등록하고 관리하세요.')
        self.assertContains(response, reverse('shoppinghub_product_new'))
        self.assertContains(response, reverse('shoppinghub_block_new'))
        self.assertContains(response, '아직 등록된 상품이 없습니다.')
        self.assertContains(response, '첫 상품을 등록하거나 기존 사이트의 상품을 가져와 보세요.')
        self.assertContains(response, '기존 사이트에서 가져오기')
        self.assertContains(response, '기존 상품 페이지 URL')

        product = Linker.objects.create(
            owner=user,
            title='Visible Product',
            product_no='P-001',
            marketplace=marketplace,
            destination_url='https://example.com/product',
            store_category=category,
            status='ACTIVE',
        )
        block = ProductBlock.objects.create(owner=user, block_number='B001', title='Video Picks', store_category=category)
        ProductBlockItem.objects.create(product_block=block, linker=product)

        response = self.client.get(reverse('shoppinghub_products'))

        self.assertContains(response, '등록된 상품')
        self.assertContains(response, 'Visible Product')
        self.assertContains(response, 'Video Picks')
        self.assertContains(response, '상품')
        self.assertContains(response, '상품묶음')
        self.assertContains(response, '카테고리 관리')
        self.assertNotContains(response, '아직 등록된 상품이 없습니다.')

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_product_page_url_fetch_prefills_draft_without_saving_product(self, mocked_get, mocked_addr):
        user, _ = self.complete_creator('draft-owner')
        marketplace, _ = Marketplace.objects.get_or_create(code='CP', defaults={'name': '쿠팡', 'active': True})
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        mocked_get.return_value = SimpleNamespace(
            url='https://www.coupang.com/vp/products/12345',
            text='<html><head><meta property="og:title" content="Draft Product"><meta property="og:image" content="/image.jpg"><meta name="description" content="Draft intro"></head></html>',
            raise_for_status=lambda: None,
        )
        self.client.force_login(user)

        response = self.client.post(reverse('shoppinghub_product_new'), {
            'flow': 'single',
            'action': 'fetch_product_url',
            'source_product_url': 'https://www.coupang.com/vp/products/12345',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Draft Product')
        self.assertContains(response, 'https://www.coupang.com/image.jpg')
        self.assertContains(response, f'<option value="{marketplace.pk}" selected', html=False)
        self.assertContains(response, '파트너스 링크')
        self.assertNotContains(response, 'name="destination_url" value="https://www.coupang.com/vp/products/12345"', html=False)
        self.assertFalse(Linker.objects.filter(owner=user).exists())

    def test_product_new_requires_valid_partners_link_in_single_flow(self):
        user, _ = self.complete_creator('partners-url-owner')
        marketplace = Marketplace.objects.create(code='COUPANG', name='Coupang')
        self.client.force_login(user)

        response = self.client.post(reverse('shoppinghub_product_new'), {
            'flow': 'single',
            'product_no': 'P-404',
            'title': 'Invalid Product',
            'thumbnail_url': '',
            'marketplace': marketplace.pk,
            'destination_url': '',
            'description': '',
            'display_order': '0',
            'store_visible': 'on',
        })

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Linker.objects.filter(owner=user, product_no='P-404').exists())

        response = self.client.post(reverse('shoppinghub_product_new'), {
            'flow': 'single',
            'product_no': 'P-405',
            'title': 'Invalid Product',
            'thumbnail_url': '',
            'marketplace': marketplace.pk,
            'destination_url': 'not-a-url',
            'description': '',
            'display_order': '0',
            'store_visible': 'on',
        })

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Linker.objects.filter(owner=user, product_no='P-405').exists())

        response = self.client.post(reverse('shoppinghub_product_new'), {
            'flow': 'single',
            'product_no': 'P-406',
            'title': 'Valid Product',
            'thumbnail_url': '',
            'marketplace': marketplace.pk,
            'destination_url': 'https://example.com/partners',
            'description': '',
            'display_order': '0',
            'store_visible': 'on',
        })

        self.assertRedirects(response, reverse('shoppinghub_products'), fetch_redirect_response=False)
        self.assertTrue(Linker.objects.filter(owner=user, product_no='P-406').exists())

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_product_page_url_fetch_prefill_then_manual_edit_saves_product(self, mocked_get, mocked_addr):
        user, _ = self.complete_creator('draft-save-owner')
        marketplace, _ = Marketplace.objects.get_or_create(code='CP', defaults={'name': '쿠팡', 'active': True})
        category = StoreCategory.objects.create(owner=user, name='생활')
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        mocked_get.return_value = SimpleNamespace(
            url='https://www.coupang.com/vp/products/54321',
            text='<html><head><meta property="og:title" content="Fetched Product"><meta property="og:image" content="https://cdn.example.com/fetched.jpg"></head></html>',
            raise_for_status=lambda: None,
        )
        self.client.force_login(user)

        self.client.post(reverse('shoppinghub_product_new'), {
            'flow': 'single',
            'action': 'fetch_product_url',
            'source_product_url': 'https://www.coupang.com/vp/products/54321',
        })
        response = self.client.post(reverse('shoppinghub_product_new'), {
            'flow': 'single',
            'product_no': '54321',
            'title': 'User Edited Product',
            'thumbnail_url': 'https://cdn.example.com/edited.jpg',
            'marketplace': marketplace.pk,
            'destination_url': 'https://www.coupang.com/vp/products/54321',
            'description': 'Edited intro',
            'store_category': category.pk,
            'display_order': '0',
            'store_visible': 'on',
        })
        product = Linker.objects.get(owner=user, product_no='54321')

        self.assertRedirects(response, reverse('shoppinghub_products'), fetch_redirect_response=False)
        self.assertEqual(product.title, 'User Edited Product')
        self.assertEqual(product.thumbnail_url, 'https://cdn.example.com/edited.jpg')
        self.assertEqual(product.store_category, category)

    def test_product_block_form_renders_search_filter_and_selected_products(self):
        user, store = self.complete_creator('block-ui-owner')
        category = StoreCategory.objects.create(owner=user, store=store, name='생활', display_order=1)
        marketplace = Marketplace.objects.create(code='COUPANG', name='Coupang')
        product = Linker.objects.create(
            owner=user,
            title='Block Product',
            product_no='B-P-001',
            marketplace=marketplace,
            destination_url='https://example.com/block-product',
            store_category=category,
            status='ACTIVE',
        )
        block = ProductBlock.objects.create(owner=user, block_number='B900', title='Existing Block')
        ProductBlockItem.objects.create(product_block=block, linker=product)
        self.client.force_login(user)

        response = self.client.get(reverse('shoppinghub_block_new'))

        self.assertContains(response, '상품묶음이란?')
        self.assertContains(response, 'product-search')
        self.assertContains(response, 'category-filter')
        self.assertContains(response, 'class="product-card available"', html=False)
        self.assertContains(response, f'data-id="{product.pk}"', html=False)
        self.assertContains(response, 'Block Product')

        response = self.client.get(reverse('shoppinghub_block_edit', kwargs={'pk': block.pk}))

        self.assertContains(response, '이 상품묶음에 담을 상품')
        self.assertContains(response, f'name="products" value="{product.pk}"', html=False)

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_inpock_parser_removes_css_text_and_keeps_visible_product_title(self, mocked_get, mocked_addr):
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        mocked_get.return_value = self.http_response(
            'https://link.inpock.co.kr/creator',
            '''<html><body>
                <a href="https://www.coupang.com/vp/products/111">
                    <style>.css-umv36l{background-color:var(--ids-white);box-shadow:var(--visitor-block-shadow);transition:transform 0.2s ease}</style>
                    <span>정상 상품명</span>
                    <img src="https://cdn.example.com/product.jpg">
                </a>
            </body></html>''',
        )

        drafts = scan_inpock_product_drafts('https://link.inpock.co.kr/creator')

        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].title, '정상 상품명')
        self.assertNotIn('css-umv36', drafts[0].title)
        self.assertEqual(drafts[0].thumbnail_url, 'https://cdn.example.com/product.jpg')

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_inpock_parser_extracts_srcset_and_background_images(self, mocked_get, mocked_addr):
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        mocked_get.return_value = self.http_response(
            'https://link.inpock.co.kr/creator',
            '''<html><body>
                <a href="https://www.coupang.com/vp/products/222">
                    <picture>
                        <source srcset="https://cdn.example.com/small.jpg 1x, https://cdn.example.com/large.jpg 2x">
                    </picture>
                    Srcset Product
                </a>
                <a href="https://shop.example.com/items/333" style="background-image:url('/images/bg-product.jpg')">Background Product</a>
            </body></html>''',
        )

        drafts = scan_inpock_product_drafts('https://link.inpock.co.kr/creator')

        self.assertEqual(drafts[0].thumbnail_url, 'https://cdn.example.com/large.jpg')
        self.assertEqual(drafts[1].thumbnail_url, 'https://link.inpock.co.kr/images/bg-product.jpg')

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_inpock_next_data_image_fallback_uses_block_id_and_strips_images_prefix(self, mocked_get, mocked_addr):
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        mocked_get.return_value = self.http_response(
            'https://link.inpock.co.kr/creator',
            '''<html><body>
                <div data-preview-block-id="111"><a href="https://www.coupang.com/vp/products/111"><img alt="First">First Product</a></div>
                <div data-preview-block-id="222"><a href="https://www.coupang.com/vp/products/222"><img alt="First">First Product</a></div>
                <script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"blocks":[
                    {"id":111,"block_type":"link","title":"First Product","url":"https://www.coupang.com/vp/products/111","image":"images/2026/8/1/first.webp","style":"thumbnail"},
                    {"id":222,"block_type":"link","title":"First Product","url":"https://www.coupang.com/vp/products/222","image":"images/2026/8/2/second.webp","style":"thumbnail"}
                ]}}}</script>
            </body></html>''',
        )

        drafts = scan_inpock_product_drafts('https://link.inpock.co.kr/creator')

        self.assertEqual(drafts[0].thumbnail_url, 'https://d13k46lqgoj3d6.cloudfront.net/2026/8/1/first.webp')
        self.assertEqual(drafts[1].thumbnail_url, 'https://d13k46lqgoj3d6.cloudfront.net/2026/8/2/second.webp')

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_inpock_next_data_missing_image_still_creates_draft(self, mocked_get, mocked_addr):
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        mocked_get.return_value = self.http_response(
            'https://link.inpock.co.kr/creator',
            '''<html><body>
                <div data-preview-block-id="111"><a href="https://www.coupang.com/vp/products/111">No Image Product</a></div>
                <script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"blocks":[
                    {"id":111,"block_type":"link","title":"No Image Product","url":"https://www.coupang.com/vp/products/111","style":"thumbnail"}
                ]}}}</script>
            </body></html>''',
        )

        drafts = scan_inpock_product_drafts('https://link.inpock.co.kr/creator')

        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].title, 'No Image Product')
        self.assertEqual(drafts[0].thumbnail_url, '')

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_inpock_dom_image_is_not_overwritten_by_next_data_fallback(self, mocked_get, mocked_addr):
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        mocked_get.return_value = self.http_response(
            'https://link.inpock.co.kr/creator',
            '''<html><body>
                <div data-preview-block-id="111"><a href="https://www.coupang.com/vp/products/111"><img src="https://cdn.example.com/dom.jpg">DOM Product</a></div>
                <script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"blocks":[
                    {"id":111,"block_type":"link","title":"DOM Product","url":"https://www.coupang.com/vp/products/111","image":"images/2026/8/1/fallback.webp","style":"thumbnail"}
                ]}}}</script>
            </body></html>''',
        )

        drafts = scan_inpock_product_drafts('https://link.inpock.co.kr/creator')

        self.assertEqual(drafts[0].thumbnail_url, 'https://cdn.example.com/dom.jpg')

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_inpock_malformed_next_data_does_not_fail_parser(self, mocked_get, mocked_addr):
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        mocked_get.return_value = self.http_response(
            'https://link.inpock.co.kr/creator',
            '''<html><body>
                <div data-preview-block-id="111"><a href="https://www.coupang.com/vp/products/111">Broken Hydration Product</a></div>
                <script id="__NEXT_DATA__" type="application/json">not-json</script>
            </body></html>''',
        )

        drafts = scan_inpock_product_drafts('https://link.inpock.co.kr/creator')

        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].title, 'Broken Hydration Product')
        self.assertEqual(drafts[0].thumbnail_url, '')

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_inpock_redirect_preserves_coupang_partner_url_and_product_identity(self, mocked_get, mocked_addr):
        user, _ = self.complete_creator('redirect-import-owner')
        Marketplace.objects.get_or_create(code='CP', defaults={'name': '쿠팡', 'active': True})
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        mocked_get.side_effect = [
            self.http_response(
                'https://link.inpock.co.kr/creator',
                '<html><body><a href="/api/r/abc"><img src="/item.jpg">Redirect Product</a></body></html>',
            ),
            self.http_response(
                'https://link.inpock.co.kr/api/r/abc',
                status_code=302,
                location='https://link.coupang.com/a/geTuoJYDe0',
            ),
            self.http_response(
                'https://link.coupang.com/a/geTuoJYDe0',
                status_code=302,
                location='https://www.coupang.com/vp/products/9252723935?itemId=27368519464&lptag=AF2571045',
            ),
            self.http_response(
                'https://www.coupang.com/vp/products/9252723935?itemId=27368519464&lptag=AF2571045',
                status_code=403,
            ),
        ]
        self.client.force_login(user)

        response = self.client.post(reverse('shoppinghub_products'), {
            'action': 'existing_service_scan',
            'existing_service_url': 'https://link.inpock.co.kr/creator',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Redirect Product')
        self.assertContains(response, 'https://link.coupang.com/a/geTuoJYDe0')
        self.assertNotContains(response, 'https://link.inpock.co.kr/api/r/abc')
        self.assertFalse(Linker.objects.filter(owner=user).exists())

        payload = self.client.session['shoppinghub_migration_drafts']
        draft = ProductDraft.from_dict(payload['drafts'][0])
        self.assertEqual(draft.destination_url, 'https://link.coupang.com/a/geTuoJYDe0')
        self.assertEqual(draft.product_no, '9252723935')
        self.assertEqual(draft.marketplace_code, 'CP')
        self.assertIn('coupang:9252723935', draft_identity_candidates(draft))

        response = self.client.post(reverse('shoppinghub_products'), {
            'action': 'existing_service_import',
            'selected_drafts': ['0'],
        })
        product = Linker.objects.get(owner=user, title='Redirect Product')

        self.assertRedirects(response, reverse('shoppinghub_products'), fetch_redirect_response=False)
        self.assertEqual(product.destination_url, 'https://link.coupang.com/a/geTuoJYDe0')
        self.assertEqual(product.product_no, '9252723935')
        self.assertEqual(product.thumbnail_url, 'https://link.inpock.co.kr/item.jpg')
        self.assertEqual(product.marketplace.code, 'CP')

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_inpock_redirect_timeout_marks_draft_unavailable_without_saving_api_url(self, mocked_get, mocked_addr):
        user, _ = self.complete_creator('redirect-timeout-owner')
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        mocked_get.side_effect = [
            self.http_response(
                'https://link.inpock.co.kr/creator',
                '<html><body><a href="/api/r/timeout">Timeout Product</a></body></html>',
            ),
            requests.Timeout('redirect timed out'),
        ]
        self.client.force_login(user)

        response = self.client.post(reverse('shoppinghub_products'), {
            'action': 'existing_service_scan',
            'existing_service_url': 'https://link.inpock.co.kr/creator',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Timeout Product')
        self.assertContains(response, '가져오기 불가')
        self.assertNotContains(response, 'https://link.inpock.co.kr/api/r/timeout')

        response = self.client.post(reverse('shoppinghub_products'), {
            'action': 'existing_service_import',
            'selected_drafts': ['0'],
        })

        self.assertRedirects(response, reverse('shoppinghub_products'), fetch_redirect_response=False)
        self.assertFalse(Linker.objects.filter(owner=user, title='Timeout Product').exists())

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_existing_service_scan_previews_only_and_hides_internal_brand(self, mocked_get, mocked_addr):
        user, _ = self.complete_creator('service-preview-owner')
        marketplace, _ = Marketplace.objects.get_or_create(code='CP', defaults={'name': '쿠팡', 'active': True})
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        mocked_get.return_value = SimpleNamespace(
            url='https://link.inpock.co.kr/creator',
            text='''<html><body>
                <a href="https://www.coupang.com/vp/products/111"><img src="https://cdn.example.com/one.jpg">First Item</a>
                <a href="https://www.coupang.com/vp/products/222"><img src="https://cdn.example.com/two.jpg">Second Item</a>
            </body></html>''',
            raise_for_status=lambda: None,
        )
        self.client.force_login(user)

        response = self.client.post(reverse('shoppinghub_products'), {
            'action': 'existing_service_scan',
            'existing_service_url': 'https://link.inpock.co.kr/creator',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '기존 상품 가져오기')
        self.assertContains(response, '상품 불러오기')
        self.assertContains(response, 'First Item')
        self.assertContains(response, 'https://www.coupang.com/vp/products/111')
        self.assertContains(response, '등록 가능')
        self.assertNotContains(response, 'Inpock')
        self.assertFalse(Linker.objects.filter(owner=user).exists())
        self.assertEqual(marketplace.code, 'CP')

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_shoppinghub_store_import_uses_existing_preview_and_copies_selected_image(self, mocked_get, mocked_addr):
        user, store = self.complete_creator('shoppinghub-source-owner')
        Marketplace.objects.get_or_create(code='DM', defaults={'name': 'Direct', 'active': True})
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        image_file = BytesIO()
        Image.new('RGB', (1, 1), 'white').save(image_file, format='PNG')
        image_content = image_file.getvalue()
        mocked_get.side_effect = [
            self.http_response(
                'https://shophub.kr/s/live/',
                '''<html><body>
                    <a href="/s/live/?product=LIVE-1">First card</a>
                    <a href="/s/live/?product=LIVE-2">Second card</a>
                </body></html>''',
            ),
            self.http_response(
                'https://shophub.kr/s/live/?product=LIVE-1',
                '''<html><body><img class="detail-image" src="https://cdn.example.com/one.png">
                <h1>Live One</h1><p>First description</p>
                <a class="btn" href="/s/live/out/101/">상품 확인하기</a></body></html>''',
            ),
            self.http_response(
                'https://shophub.kr/s/live/out/101/',
                status_code=302,
                location='https://affiliate.example.com/one',
            ),
            self.http_response(
                'https://shophub.kr/s/live/?product=LIVE-2',
                '''<html><body><h1>Live Two</h1><p>Second description</p>
                <a class="btn" href="/s/live/out/102/">상품 확인하기</a></body></html>''',
            ),
            self.http_response(
                'https://shophub.kr/s/live/out/102/',
                status_code=302,
                location='https://affiliate.example.com/two',
            ),
            SimpleNamespace(
                url='https://cdn.example.com/one.png',
                status_code=200,
                headers={'Content-Type': 'image/png', 'Content-Length': str(len(image_content))},
                iter_content=lambda _size: [image_content],
                raise_for_status=lambda: None,
                close=lambda: None,
            ),
        ]
        self.client.force_login(user)

        preview = self.client.post(reverse('shoppinghub_products'), {
            'action': 'existing_service_scan',
            'existing_service_url': 'https://shophub.kr/s/live/',
        })

        self.assertContains(preview, 'Live One')
        self.assertContains(preview, 'Live Two')
        drafts = self.client.session['shoppinghub_migration_drafts']['drafts']
        self.assertEqual(drafts[0]['source_type'], 'shoppinghub')
        self.assertEqual(drafts[0]['destination_url'], 'https://affiliate.example.com/one')

        response = self.client.post(reverse('shoppinghub_products'), {
            'action': 'existing_service_import',
            'selected_drafts': ['0'],
        })
        product = Linker.objects.get(owner=user, store=store, title='Live One')

        self.assertRedirects(response, reverse('shoppinghub_products'), fetch_redirect_response=False)
        self.assertFalse(Linker.objects.filter(owner=user, title='Live Two').exists())
        self.assertTrue(product.product_image.name)
        self.assertEqual(product.thumbnail_url, product.product_image.url)

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_shoppinghub_store_duplicate_is_unavailable_in_preview(self, mocked_get, mocked_addr):
        user, store = self.complete_creator('shoppinghub-duplicate-owner')
        marketplace, _ = Marketplace.objects.get_or_create(code='DM', defaults={'name': 'Direct'})
        Linker.objects.create(
            owner=user,
            store=store,
            product_no='OLD',
            title='Existing',
            marketplace=marketplace,
            destination_url='https://affiliate.example.com/one',
        )
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        mocked_get.side_effect = [
            self.http_response('https://shophub.kr/s/live/', '<a href="/s/live/?product=LIVE-1">Card</a>'),
            self.http_response(
                'https://shophub.kr/s/live/?product=LIVE-1',
                '<h1>Live One</h1><a href="/s/live/out/101/">상품 확인하기</a>',
            ),
            self.http_response(
                'https://shophub.kr/s/live/out/101/',
                status_code=302,
                location='https://affiliate.example.com/one',
            ),
        ]
        self.client.force_login(user)

        response = self.client.post(reverse('shoppinghub_products'), {
            'action': 'existing_service_scan',
            'existing_service_url': 'https://shophub.kr/s/live/',
        })

        self.assertContains(response, '이미 등록된 상품')
        self.assertContains(response, 'disabled', html=False)

    @override_settings(SHOPPINGHUB_PUBLIC_BASE_URL='https://dev.shophub.kr')
    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_actual_public_store_base_host_is_shoppinghub_source(self, mocked_get, mocked_addr):
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        mocked_get.side_effect = [
            self.http_response('https://dev.shophub.kr/s/live/', '<a href="/s/live/?product=LIVE-1">Card</a>'),
            self.http_response(
                'https://dev.shophub.kr/s/live/?product=LIVE-1',
                '<h1>Live One</h1><a href="/s/live/out/101/">Check</a>',
            ),
            self.http_response(
                'https://dev.shophub.kr/s/live/out/101/',
                status_code=302,
                location='https://affiliate.example.com/one',
            ),
        ]

        drafts = scan_existing_service_product_drafts('https://dev.shophub.kr/s/live/')

        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].source_type, 'shoppinghub')
        self.assertEqual(drafts[0].product_no, 'LIVE-1')
        self.assertEqual(drafts[0].destination_url, 'https://affiliate.example.com/one')

    @override_settings(SHOPPINGHUB_PUBLIC_BASE_URL='https://dev.shophub.kr')
    @patch('linker.services.product_drafts.socket.getaddrinfo')
    def test_public_base_host_non_store_path_is_not_shoppinghub_source(self, mocked_addr):
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]

        with self.assertRaises(ProductDraftError):
            scan_existing_service_product_drafts('https://dev.shophub.kr/shoppinghub/')

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_shoppinghub_store_without_public_products_has_clear_error(self, mocked_get, mocked_addr):
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        mocked_get.return_value = self.http_response('https://shophub.kr/s/empty/', '<html><body></body></html>')

        with self.assertRaisesRegex(Exception, '공개된 ShoppingHub 상품을 찾지 못했습니다'):
            scan_existing_service_product_drafts('https://shophub.kr/s/empty/')

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_existing_service_import_saves_selected_and_select_all_items(self, mocked_get, mocked_addr):
        user, _ = self.complete_creator('service-import-owner')
        Marketplace.objects.get_or_create(code='CP', defaults={'name': '쿠팡', 'active': True})
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        mocked_get.return_value = SimpleNamespace(
            url='https://link.inpock.co.kr/creator',
            text='''<html><body>
                <a href="https://www.coupang.com/vp/products/111">First Item</a>
                <a href="https://www.coupang.com/vp/products/222">Second Item</a>
            </body></html>''',
            raise_for_status=lambda: None,
        )
        self.client.force_login(user)
        self.client.post(reverse('shoppinghub_products'), {
            'action': 'existing_service_scan',
            'existing_service_url': 'https://link.inpock.co.kr/creator',
        })

        response = self.client.post(reverse('shoppinghub_products'), {
            'action': 'existing_service_import',
            'selected_drafts': ['0', '1'],
        })
        products = Linker.objects.filter(owner=user).order_by('product_no')

        self.assertRedirects(response, reverse('shoppinghub_products'), fetch_redirect_response=False)
        self.assertEqual(products.count(), 2)
        self.assertEqual([item.title for item in products], ['First Item', 'Second Item'])

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_existing_service_import_saves_selected_only_and_marks_duplicate(self, mocked_get, mocked_addr):
        user, _ = self.complete_creator('service-duplicate-owner')
        marketplace, _ = Marketplace.objects.get_or_create(code='CP', defaults={'name': '쿠팡', 'active': True})
        Linker.objects.create(owner=user, title='Already', product_no='999', marketplace=marketplace, destination_url='https://www.coupang.com/vp/products/111')
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        mocked_get.return_value = SimpleNamespace(
            url='https://link.inpock.co.kr/creator',
            text='''<html><body>
                <a href="https://www.coupang.com/vp/products/111">Already Imported</a>
                <a href="https://www.coupang.com/vp/products/222">New Item</a>
            </body></html>''',
            raise_for_status=lambda: None,
        )
        self.client.force_login(user)

        preview = self.client.post(reverse('shoppinghub_products'), {
            'action': 'existing_service_scan',
            'existing_service_url': 'https://link.inpock.co.kr/creator',
        })
        response = self.client.post(reverse('shoppinghub_products'), {
            'action': 'existing_service_import',
            'selected_drafts': ['0', '1'],
        })

        self.assertContains(preview, '이미 등록된 상품')
        self.assertRedirects(response, reverse('shoppinghub_products'), fetch_redirect_response=False)
        self.assertEqual(Linker.objects.filter(owner=user).count(), 2)
        self.assertTrue(Linker.objects.filter(owner=user, title='New Item').exists())

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    def test_existing_service_unsupported_url_is_general_message(self, mocked_addr):
        user, _ = self.complete_creator('unsupported-service-owner')
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        self.client.force_login(user)

        response = self.client.post(reverse('shoppinghub_products'), {
            'action': 'existing_service_scan',
            'existing_service_url': 'https://example.com/store',
        }, follow=True)

        self.assertContains(response, '현재 지원하지 않는 서비스 주소입니다.')
        self.assertNotContains(response, 'Inpock')

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    def test_existing_service_import_continues_after_partial_failure(self, mocked_addr):
        user, store = self.complete_creator('partial-import-owner')
        Marketplace.objects.get_or_create(code='CP', defaults={'name': '쿠팡', 'active': True})
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        session = self.client.session
        session['shoppinghub_migration_drafts'] = {
            'owner_id': user.pk,
            'store_id': store.pk,
            'drafts': [
                {'title': '', 'destination_url': 'https://www.coupang.com/vp/products/111', 'marketplace_code': 'CP', 'source_identifier': 'coupang:111'},
                {'title': 'Working Item', 'destination_url': 'https://www.coupang.com/vp/products/222', 'marketplace_code': 'CP', 'source_identifier': 'coupang:222'},
            ],
        }
        session.save()
        self.client.force_login(user)

        response = self.client.post(reverse('shoppinghub_products'), {
            'action': 'existing_service_import',
            'selected_drafts': ['0', '1'],
        }, follow=True)

        self.assertContains(response, '등록 성공 1건')
        self.assertContains(response, '실패 1건')
        self.assertTrue(Linker.objects.filter(owner=user, title='Working Item').exists())

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    @patch('linker.services.product_drafts.requests.get')
    def test_existing_service_repeated_import_does_not_duplicate_same_url(self, mocked_get, mocked_addr):
        user, _ = self.complete_creator('repeat-import-owner')
        Marketplace.objects.get_or_create(code='CP', defaults={'name': 'Coupang', 'active': True})
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        mocked_get.return_value = SimpleNamespace(
            url='https://link.inpock.co.kr/creator',
            text='<html><body><a href="https://www.coupang.com/vp/products/111">First Item</a></body></html>',
            raise_for_status=lambda: None,
        )
        self.client.force_login(user)

        for _ in range(2):
            self.client.post(reverse('shoppinghub_products'), {
                'action': 'existing_service_scan',
                'existing_service_url': 'https://link.inpock.co.kr/creator',
            })
            self.client.post(reverse('shoppinghub_products'), {
                'action': 'existing_service_import',
                'selected_drafts': ['0'],
            })

        self.assertEqual(Linker.objects.filter(owner=user).count(), 1)

    def test_product_identity_normalization(self):
        self.assertEqual(
            normalized_product_url('https://www.coupang.com/vp/products/12345?itemId=9'),
            'coupang:12345',
        )
        self.assertEqual(
            normalized_product_url('HTTPS://Shop.Example.com/items/123/?utm_source=x'),
            'https://shop.example.com/items/123',
        )

    @patch('linker.services.product_drafts.socket.getaddrinfo')
    def test_general_url_identity_prevents_owner_scoped_duplicates(self, mocked_addr):
        user, store = self.complete_creator('general-duplicate-owner')
        marketplace, _ = Marketplace.objects.get_or_create(code='DM', defaults={'name': 'Direct', 'active': True})
        mocked_addr.return_value = [(None, None, None, None, ('93.184.216.34', 443))]
        Linker.objects.create(
            owner=user,
            title='Already',
            marketplace=marketplace,
            product_no='001',
            destination_url='https://shop.example.com/items/123/?utm_source=old',
        )
        session = self.client.session
        session['shoppinghub_migration_drafts'] = {
            'owner_id': user.pk,
            'store_id': store.pk,
            'drafts': [
                ProductDraft(
                    title='Same General URL',
                    destination_url='https://shop.example.com/items/123?utm_source=new',
                    marketplace_code='DM',
                    source_identifier='https://shop.example.com/items/123',
                ).as_dict(),
            ],
        }
        session.save()
        self.client.force_login(user)

        response = self.client.post(reverse('shoppinghub_products'), {
            'action': 'existing_service_import',
            'selected_drafts': ['0'],
        })

        self.assertRedirects(response, reverse('shoppinghub_products'), fetch_redirect_response=False)
        self.assertEqual(Linker.objects.filter(owner=user).count(), 1)

    def test_stale_or_cross_store_session_import_is_rejected(self):
        user, _ = self.complete_creator('stale-owner')
        other, other_store = self.complete_creator('stale-other')
        Marketplace.objects.get_or_create(code='CP', defaults={'name': 'Coupang', 'active': True})
        session = self.client.session
        session['shoppinghub_migration_drafts'] = {
            'owner_id': other.pk,
            'store_id': other_store.pk,
            'drafts': [
                ProductDraft(
                    title='Bad Import',
                    destination_url='https://www.coupang.com/vp/products/777',
                    marketplace_code='CP',
                ).as_dict(),
            ],
        }
        session.save()
        self.client.force_login(user)

        response = self.client.post(reverse('shoppinghub_products'), {
            'action': 'existing_service_import',
            'selected_drafts': ['0'],
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Linker.objects.filter(title='Bad Import').exists())

    def test_malformed_session_draft_is_not_created(self):
        user, store = self.complete_creator('malformed-import-owner')
        Marketplace.objects.get_or_create(code='CP', defaults={'name': 'Coupang', 'active': True})
        session = self.client.session
        session['shoppinghub_migration_drafts'] = {
            'owner_id': user.pk,
            'store_id': store.pk,
            'drafts': [
                {'title': '', 'destination_url': 'https://www.coupang.com/vp/products/888', 'marketplace_code': 'CP'},
            ],
        }
        session.save()
        self.client.force_login(user)

        response = self.client.post(reverse('shoppinghub_products'), {
            'action': 'existing_service_import',
            'selected_drafts': ['0'],
        })

        self.assertRedirects(response, reverse('shoppinghub_products'), fetch_redirect_response=False)
        self.assertFalse(Linker.objects.filter(destination_url__contains='products/888').exists())

    def test_12_to_15_customer_store_states(self):
        user, store = self.complete_creator('public-owner')
        marketplace = Marketplace.objects.create(code='COUPANG', name='쿠팡')
        category = StoreCategory.objects.create(owner=user, store=store, name='생활')
        product = Linker.objects.create(
            owner=user,
            title='무타공 전동커튼',
            description='공간을 더 편리하게 만들어주는 아이템',
            product_no='007',
            marketplace=marketplace,
            destination_url='https://example.com/product',
            store_category=category,
            store_visible=True,
            status='ACTIVE',
        )
        block = ProductBlock.objects.create(
            owner=user,
            block_number='B007',
            title='무타공 전동커튼 모아보기',
            description='여러 제품을 한곳에서 둘러보세요.',
            store_category=category,
            store_visible=True,
        )
        ProductBlockItem.objects.create(product_block=block, linker=product)

        response = self.client.get(reverse('public_store', kwargs={'slug': store.slug}))
        self.assertContains(response, store.store_name)
        self.assertContains(response, 'Creator PICK')
        self.assertContains(response, '무타공 전동커튼')
        self.assertContains(response, '?product=007')
        self.assertContains(response, '?block=B007')

        response = self.client.get(reverse('public_store', kwargs={'slug': store.slug}) + '?q=007')
        self.assertContains(response, '007 검색결과')
        self.assertContains(response, '?product=007')

        response = self.client.get(reverse('public_store', kwargs={'slug': store.slug}) + '?product=007')
        self.assertContains(response, '상품 확인하기')
        self.assertContains(response, '이 상품과 함께 보면 좋은 상품')

        response = self.client.get(reverse('public_store', kwargs={'slug': store.slug}) + '?block=B007')
        self.assertContains(response, 'B007')
        self.assertContains(response, '?product=007')

    def test_store_home_uses_block_and_product_image_fallbacks_before_placeholder(self):
        user, store = self.complete_creator('store-image-fallbacks')
        original_only = Linker.objects.create(
            owner=user,
            store=store,
            product_no='B001',
            title='Original image only',
            status='ACTIVE',
            store_visible=True,
        )
        Linker.objects.filter(pk=original_only.pk).update(
            product_image='creator/products/original-only.jpg',
            thumbnail_url='',
        )
        no_image = Linker.objects.create(
            owner=user,
            store=store,
            product_no='B002',
            title='No image',
            status='ACTIVE',
            store_visible=True,
        )
        block = ProductBlock.objects.create(
            owner=user,
            store=store,
            block_number='BLOCK-IMAGE',
            title='Block product fallback',
            store_visible=True,
        )
        ProductBlockItem.objects.create(product_block=block, linker=original_only)
        block_cover = ProductBlock.objects.create(
            owner=user,
            store=store,
            block_number='BLOCK-COVER',
            title='Block representative image',
            store_visible=True,
        )
        ProductBlock.objects.filter(pk=block_cover.pk).update(
            representative_image='creator/product-blocks/cover.jpg',
        )

        response = self.client.get(reverse('public_store', kwargs={'slug': store.slug}))
        content = response.content.decode()

        self.assertContains(response, '/media/creator/products/original-only.jpg')
        self.assertContains(response, '/media/creator/product-blocks/cover.jpg')
        self.assertContains(response, '<div class="card-image">✦</div>', html=True)
        self.assertLess(content.index('Creator PICK'), content.index('<h2>상품</h2>'))
        self.assertIsNotNone(no_image.pk)

    def test_store_home_omits_creator_pick_when_there_are_no_blocks(self):
        user, store = self.complete_creator('store-without-blocks')
        Linker.objects.create(
            owner=user,
            store=store,
            product_no='P001',
            title='Product without a block',
            status='ACTIVE',
            store_visible=True,
        )

        response = self.client.get(reverse('public_store', kwargs={'slug': store.slug}))

        self.assertContains(response, 'Product without a block')
        self.assertNotContains(response, 'Creator PICK')

    def test_empty_customer_store_has_ready_state(self):
        _user, store = self.complete_creator('empty-public')
        response = self.client.get(reverse('public_store', kwargs={'slug': store.slug}))
        self.assertContains(response, '아직 준비 중이에요')


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost'])
class ShoppingHubV2HiddenAndBackendRouteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='route-owner', password='Strong-pass-123')
        CreatorProfile.objects.create(user=self.user, onboarding_status=CreatorProfile.OnboardingStatus.COMPLETED)
        self.store = CreatorStore.objects.create(owner=self.user, store_name='Route Store', slug='route-owner')
        self.client.force_login(self.user)

    def test_hidden_legacy_user_ui_routes_redirect(self):
        route_names = [
            'creator_store_settings',
            'creator_store_setup',
            'creator_store_ready',
            'shoppinghub_comments',
            'creator_reactions',
        ]

        for route_name in route_names:
            with self.subTest(route_name=route_name):
                response = self.client.get(reverse(route_name))
                if route_name in {'shoppinghub_comments', 'creator_reactions'}:
                    self.assertEqual(response.status_code, 200)
                else:
                    self.assertRedirects(response, reverse('creator_home'), fetch_redirect_response=False)

    def test_creator_channels_and_contents_are_store_scoped(self):
        other_store = CreatorStore.objects.create(owner=self.user, store_name='Other Store', slug='other-route-owner')
        current_account = SocialAccount.objects.create(owner=self.user, store=self.store, platform='INSTAGRAM', account_name='Current', username='current_ig', status='ACTIVE', connection_status='CONNECTED')
        other_account = SocialAccount.objects.create(owner=self.user, store=other_store, platform='INSTAGRAM', account_name='Other', username='other_ig', status='ACTIVE', connection_status='CONNECTED')
        current_content = SocialContent.objects.create(social_account=current_account, platform='INSTAGRAM', content_type='REEL', external_content_id='current-media', caption='Current caption', status='ACTIVE')
        SocialContent.objects.create(social_account=other_account, platform='INSTAGRAM', content_type='REEL', external_content_id='other-media', caption='Other caption', status='ACTIVE')
        self.client.force_login(self.user)
        session = self.client.session
        session['active_store_id'] = self.store.pk
        session.save()

        channels = self.client.get(reverse('creator_channels'))
        contents = self.client.get(reverse('shoppinghub_contents'))

        self.assertContains(channels, '@current_ig')
        self.assertContains(channels, '@other_ig')
        self.assertEqual(list(channels.context['other_accounts']), [other_account])
        self.assertContains(contents, 'current-media')
        self.assertNotContains(contents, 'other-media')
        self.assertContains(contents, 'Current caption')
        self.assertNotContains(contents, 'Other caption')

    @patch('linker.creator_views.sync_instagram_contents')
    def test_instagram_sync_route_is_store_scoped(self, sync_contents):
        account = SocialAccount.objects.create(owner=self.user, store=self.store, platform='INSTAGRAM', account_name='Current', username='current_ig', status='ACTIVE')
        sync_contents.return_value = []
        response = self.client.post(reverse('creator_instagram_sync', args=[account.pk]))
        self.assertRedirects(response, reverse('creator_channels'), fetch_redirect_response=False)
        sync_contents.assert_called_once_with(account, limit=50)

    def test_removed_ui_preview_is_not_routable(self):
        with self.assertRaises(NoReverseMatch):
            reverse('ui_preview_screen', kwargs={'screen': '01'})

        response = self.client.get('/ui-preview/')
        self.assertEqual(response.status_code, 404)

    def test_backend_oauth_and_webhook_routes_are_preserved(self):
        with patch('linker.creator_views.instagram_authorization_url', return_value=('https://provider.example/auth', 'state')):
            response = self.client.get(reverse('creator_instagram_connect') + '?return=onboarding_profile')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session['instagram_auto_oauth']['return_to'], 'onboarding_profile')

        with patch('linker.creator_views.youtube_authorization_url', return_value=('https://provider.example/youtube', 'ystate', 'verifier')):
            response = self.client.get(reverse('creator_youtube_connect') + '?return=onboarding_profile')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session['youtube_auto_oauth']['return_to'], 'onboarding_profile')

        response = self.client.get(reverse('instagram_callback'))
        self.assertIn(response.status_code, {302, 400})

        response = self.client.get(reverse('instagram_webhook'))
        self.assertIn(response.status_code, {200, 403})

    @patch('linker.creator_views.instagram_save_credential')
    @patch('linker.creator_views.instagram_verify_account')
    @patch('linker.creator_views.instagram_exchange_code')
    def test_instagram_auto_callback_returns_to_store_setup_wizard(self, exchange, verify, save_credential):
        session = self.client.session
        session['instagram_auto_oauth'] = {
            'state': 'state-i',
            'flow': 'auto_onboarding',
            'store_id': self.store.pk,
            'return_to': 'onboarding_profile',
        }
        session.save()
        exchange.return_value = {'access_token': 'token', 'scopes': []}
        verify.return_value = {
            'user_id': 'ig-user',
            'username': 'creator_ig',
            'name': 'Creator IG',
            'profile_picture_url': 'https://cdn.example.com/ig.jpg',
        }

        response = self.client.get(reverse('instagram_auto_callback') + '?state=state-i&code=code')

        self.assertRedirects(response, reverse('creator_onboarding_store') + '?step=1', fetch_redirect_response=False)
        account = SocialAccount.objects.get(platform=SocialAccount.Platform.INSTAGRAM, platform_user_id='ig-user')
        self.assertEqual(account.display_name, 'Creator IG')

    @patch('linker.creator_views.youtube_save_verified_credential')
    @patch('linker.creator_views.youtube_verify_channel')
    @patch('linker.creator_views.youtube_exchange_code')
    def test_youtube_auto_callback_returns_to_store_setup_wizard(self, exchange, verify, save_credential):
        session = self.client.session
        session['youtube_auto_oauth'] = {
            'state': 'state-y',
            'code_verifier': 'verifier-y',
            'flow': 'auto_onboarding',
            'return_to': 'onboarding_profile',
        }
        session.save()
        exchange.return_value = SimpleNamespace()
        verify.return_value = {'id': 'yt-user', 'title': 'Creator YouTube', 'custom_url': '@creator-youtube'}

        response = self.client.get(reverse('youtube_auto_callback') + '?state=state-y&code=code')

        self.assertRedirects(response, reverse('creator_onboarding_store') + '?step=1', fetch_redirect_response=False)
        account = SocialAccount.objects.get(platform=SocialAccount.Platform.YOUTUBE, platform_user_id='yt-user')
        self.assertEqual(account.display_name, 'Creator YouTube')



class ProductStoreManagementTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='product-owner', password='password')
        self.other = User.objects.create_user(username='product-other', password='password')
        self.source_store = CreatorStore.objects.create(owner=self.owner, store_name='원본', slug='product-source')
        self.target_store = CreatorStore.objects.create(owner=self.owner, store_name='대상', slug='product-target')
        self.other_store = CreatorStore.objects.create(owner=self.other, store_name='타인', slug='product-other')
        self.product = Linker.objects.create(
            owner=self.owner,
            store=self.source_store,
            title='테스트 상품',
            product_no='P-1',
            destination_url='https://example.com/p-1',
            description='설명',
            thumbnail_url='https://example.com/p-1.jpg',
            memo='메모',
        )

    def test_move_clears_current_store_relations_and_keeps_block_and_other_product(self):
        other_product = Linker.objects.create(owner=self.owner, store=self.source_store, title='다른 상품', product_no='P-2')
        block = ProductBlock.objects.create(owner=self.owner, store=self.source_store, block_number='B-1', title='묶음')
        ProductBlockItem.objects.create(product_block=block, linker=self.product)
        ProductBlockItem.objects.create(product_block=block, linker=other_product)
        rule = ResponseRule.objects.create(store=self.source_store, target_linker=self.product, enabled=True)
        keyword = ResponseKeyword.objects.create(response_rule=rule, target_linker=self.product)
        account = SocialAccount.objects.create(owner=self.owner, store=self.source_store, platform='INSTAGRAM', account_name='계정')
        content = SocialContent.objects.create(social_account=account, linker=self.product, platform='INSTAGRAM', content_type='REEL', external_content_id='move-1')
        SocialContentLink.objects.create(social_content=content, linker=self.product)

        move_product_to_store(self.product.pk, self.owner, self.source_store, self.target_store.pk)

        self.product.refresh_from_db()
        self.assertEqual(self.product.store, self.target_store)
        self.assertTrue(ProductBlock.objects.filter(pk=block.pk).exists())
        self.assertFalse(ProductBlockItem.objects.filter(product_block=block, linker=self.product).exists())
        self.assertTrue(ProductBlockItem.objects.filter(product_block=block, linker=other_product).exists())
        rule.refresh_from_db()
        keyword.refresh_from_db()
        self.assertFalse(rule.enabled)
        self.assertIsNone(rule.target_linker)
        self.assertIsNone(keyword.target_linker)
        self.assertFalse(SocialContentLink.objects.filter(linker=self.product).exists())
        content.refresh_from_db()
        self.assertIsNone(content.linker)

    def test_copy_creates_unlinked_product_and_rejects_other_owner_target(self):
        copied, target = copy_product_to_store(self.product.pk, self.owner, self.source_store, self.target_store.pk)
        self.assertEqual(target, self.target_store)
        self.assertNotEqual(copied.pk, self.product.pk)
        self.assertEqual(copied.store, self.target_store)
        self.assertEqual(copied.destination_url, self.product.destination_url)
        self.assertFalse(ProductBlockItem.objects.filter(linker=copied).exists())
        with self.assertRaises(ProductStoreManagementError):
            copy_product_to_store(self.product.pk, self.owner, self.source_store, self.other_store.pk)

    def test_delete_product_and_block_preserve_unrelated_records(self):
        other_product = Linker.objects.create(owner=self.owner, store=self.source_store, title='다른 상품', product_no='P-2')
        block = ProductBlock.objects.create(owner=self.owner, store=self.source_store, block_number='B-1', title='묶음')
        ProductBlockItem.objects.create(product_block=block, linker=self.product)
        ProductBlockItem.objects.create(product_block=block, linker=other_product)
        delete_product_safely(self.product.pk, self.owner, self.source_store)
        self.assertFalse(Linker.objects.filter(pk=self.product.pk).exists())
        self.assertTrue(ProductBlock.objects.filter(pk=block.pk).exists())
        self.assertTrue(Linker.objects.filter(pk=other_product.pk).exists())
        delete_product_block_safely(block.pk, self.owner, self.source_store)
        self.assertFalse(ProductBlock.objects.filter(pk=block.pk).exists())
        self.assertTrue(Linker.objects.filter(pk=other_product.pk).exists())

    def test_move_rejects_product_outside_current_store(self):
        with self.assertRaises(ProductStoreManagementError):
            move_product_to_store(self.product.pk, self.owner, self.target_store, self.source_store.pk)


class ProductImageUploadTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='image-owner', password='password')
        self.store = CreatorStore.objects.create(owner=self.owner, store_name='이미지 스토어', slug='image-store')
        self.client.force_login(self.owner)

    def image(self, name='product.jpg', color=(40, 80, 120)):
        from PIL import Image
        from io import BytesIO
        output = BytesIO()
        Image.new('RGB', (20, 20), color).save(output, format='JPEG')
        return SimpleUploadedFile(name, output.getvalue(), content_type='image/jpeg')

    def test_new_product_uploads_one_image_and_shows_upload_ux(self):
        response = self.client.get(reverse('shoppinghub_product_new'))
        self.assertContains(response, 'id="image-dropzone"')
        response = self.client.post(reverse('shoppinghub_product_new'), {
            'flow': 'single', 'product_no': 'IMG-1', 'title': '업로드 상품',
            'destination_url': 'https://example.com/img-1', 'description': '',
            'store_visible': 'on', 'product_image': self.image(),
        })
        self.assertRedirects(response, reverse('shoppinghub_products'), fetch_redirect_response=False)
        product = Linker.objects.get(product_no='IMG-1')
        self.assertTrue(product.product_image.name)
        self.assertEqual(product.thumbnail_url, product.product_image.url)

    def test_edit_replaces_and_removes_image_only_on_save(self):
        product = Linker.objects.create(owner=self.owner, store=self.store, product_no='IMG-2', title='기존', product_image=self.image('old.jpg'))
        old_name = product.product_image.name
        response = self.client.post(reverse('shoppinghub_product_edit', args=[product.pk]), {
            'product_no': 'IMG-2', 'title': '기존', 'destination_url': 'https://example.com/img-2',
            'store_visible': 'on', 'product_image': self.image('new.jpg'),
        })
        self.assertRedirects(response, reverse('shoppinghub_products'), fetch_redirect_response=False)
        product.refresh_from_db()
        self.assertNotEqual(product.product_image.name, old_name)
        new_name = product.product_image.name
        response = self.client.post(reverse('shoppinghub_product_edit', args=[product.pk]), {
            'product_no': 'IMG-2', 'title': '기존', 'destination_url': 'https://example.com/img-2',
            'store_visible': 'on', 'remove_product_image': 'true',
        })
        self.assertRedirects(response, reverse('shoppinghub_products'), fetch_redirect_response=False)
        product.refresh_from_db()
        self.assertFalse(product.product_image.name)
        self.assertNotEqual(new_name, product.product_image.name)

    def test_imported_external_thumbnail_remains_without_uploaded_image(self):
        product = Linker.objects.create(owner=self.owner, store=self.store, product_no='IMG-3', title='가져온 상품', thumbnail_url='https://cdn.example.com/import.jpg')
        self.assertFalse(product.product_image.name)
        self.assertEqual(product.display_image_url, 'https://cdn.example.com/import.jpg')


class TypedItemArchitectureTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='typed-owner', password='password')
        self.store = CreatorStore.objects.create(owner=self.owner, store_name='Typed Store', slug='typed-store')
        self.target_store = CreatorStore.objects.create(owner=self.owner, store_name='Typed Target', slug='typed-target')
        self.client.force_login(self.owner)
        session = self.client.session
        session['active_store_id'] = self.store.pk
        session.save()

    def post_item(self, item_type, **extra):
        data = {
            'flow': 'single', 'item_type': item_type, 'product_no': f'{item_type}-1',
            'title': f'{item_type} 항목', 'description': '소개', 'store_visible': 'on',
            'destination_url': '',
        }
        data.update(extra)
        return self.client.post(reverse('shoppinghub_product_new'), data)

    def test_recipe_and_service_are_created_without_external_links(self):
        response = self.post_item('RECIPE', ingredients='재료', instructions='만드는 법', tips='팁', servings='2인', prep_time='10분', cook_time='20분')
        self.assertRedirects(response, reverse('shoppinghub_products'), fetch_redirect_response=False)
        recipe = Linker.objects.get(item_type=Linker.ItemType.RECIPE)
        self.assertEqual(recipe.recipe_detail.ingredients, '재료')
        response = self.post_item('SERVICE', product_no='SERVICE-1', service_description='상세 안내', price_info='3만원', duration='60분', location_info='서울', contact_info='010')
        self.assertRedirects(response, reverse('shoppinghub_products'), fetch_redirect_response=False)
        service = Linker.objects.get(item_type=Linker.ItemType.SERVICE)
        self.assertEqual(service.service_detail.price_info, '3만원')

    def test_typed_items_copy_move_and_delete_with_details(self):
        recipe = Linker.objects.create(owner=self.owner, store=self.store, item_type='RECIPE', title='레시피', product_no='R-1')
        RecipeDetail.objects.create(linker=recipe, ingredients='재료', instructions='순서')
        copied, _ = copy_product_to_store(recipe.pk, self.owner, self.store, self.target_store.pk)
        self.assertEqual(copied.item_type, 'RECIPE')
        self.assertEqual(copied.recipe_detail.instructions, '순서')
        moved, _, _ = move_product_to_store(recipe.pk, self.owner, self.store, self.target_store.pk)
        self.assertEqual(moved.store, self.target_store)
        delete_product_safely(copied.pk, self.owner, self.target_store)
        self.assertFalse(Linker.objects.filter(pk=copied.pk).exists())
        self.assertFalse(RecipeDetail.objects.filter(linker_id=copied.pk).exists())

    def test_mixed_product_block_and_public_typed_detail(self):
        recipe = Linker.objects.create(owner=self.owner, store=self.store, item_type='RECIPE', title='레시피', product_no='R-2', store_visible=True)
        service = Linker.objects.create(owner=self.owner, store=self.store, item_type='SERVICE', title='서비스', product_no='S-2', store_visible=True)
        RecipeDetail.objects.create(linker=recipe, ingredients='재료', instructions='순서')
        block = ProductBlock.objects.create(owner=self.owner, store=self.store, block_number='B-TYPED', title='혼합 묶음')
        ProductBlockItem.objects.create(product_block=block, linker=recipe)
        ProductBlockItem.objects.create(product_block=block, linker=service)
        response = self.client.get(reverse('public_store', args=[self.store.slug]) + f'?item={recipe.pk}')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '재료')
        self.assertTrue(ProductBlockItem.objects.filter(product_block=block, linker__item_type='SERVICE').exists())
