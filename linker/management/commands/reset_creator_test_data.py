from django.core.management.base import BaseCommand
from django.db import transaction
from django.contrib.auth import get_user_model

from linker.models import (CreatorProfile, CreatorStore, Linker, ProductBlock, ProductBlockItem,
    ResponseKeyword, ResponseLog, ResponseRule, SocialAccount, SocialComment, SocialContent,
    SocialCredential,
    SocialContentLink, StoreCategory, StoreEvent)


class Command(BaseCommand):
    help = 'Safely remove non-staff Creator test data without flushing the database.'

    def add_arguments(self, parser):
        parser.add_argument('--confirm', action='store_true', help='Actually delete the listed Creator data.')

    def handle(self, *args, **options):
        User = get_user_model()
        users = User.objects.filter(is_staff=False, is_superuser=False).distinct()
        user_ids = list(users.values_list('id', flat=True))
        staff_superuser_count = User.objects.filter(is_staff=True).count()
        superuser_count = User.objects.filter(is_superuser=True).count()
        stores = CreatorStore.objects.filter(owner_id__in=user_ids)
        accounts = SocialAccount.objects.filter(owner_id__in=user_ids) | SocialAccount.objects.filter(store__in=stores)
        accounts = accounts.distinct()
        credentials = SocialCredential.objects.filter(social_account__in=accounts)
        contents = SocialContent.objects.filter(social_account__in=accounts)
        comments = SocialComment.objects.filter(social_content__in=contents)
        rules = ResponseRule.objects.filter(
            store__in=stores) | ResponseRule.objects.filter(social_account__in=accounts) | ResponseRule.objects.filter(social_content__in=contents) | ResponseRule.objects.filter(target_linker__owner_id__in=user_ids) | ResponseRule.objects.filter(target_product_block__owner_id__in=user_ids) | ResponseRule.objects.filter(target_store__in=stores)
        rules = rules.distinct()
        linkers = Linker.objects.filter(owner_id__in=user_ids)
        blocks = ProductBlock.objects.filter(owner_id__in=user_ids)
        categories = StoreCategory.objects.filter(owner_id__in=user_ids)
        content_links = SocialContentLink.objects.filter(social_content__in=contents) | SocialContentLink.objects.filter(linker__in=linkers)
        content_links = content_links.distinct()
        block_items = ProductBlockItem.objects.filter(product_block__in=blocks) | ProductBlockItem.objects.filter(linker__in=linkers)
        block_items = block_items.distinct()
        events = StoreEvent.objects.filter(store__in=stores) | StoreEvent.objects.filter(linker__in=linkers) | StoreEvent.objects.filter(product_block__in=blocks)
        events = events.distinct()
        response_logs = ResponseLog.objects.filter(social_comment__in=comments) | ResponseLog.objects.filter(response_rule__in=rules)
        response_logs = response_logs.distinct()
        counts = {
            'Creator users': users.count(),
            'CreatorProfiles': CreatorProfile.objects.filter(user_id__in=user_ids).count(),
            'Stores': stores.count(),
            'SocialAccounts': accounts.count(),
            'SocialCredentials': credentials.count(),
            'SocialContents': contents.count(),
            'SocialComments': comments.count(),
            'SocialContentLinks': content_links.count(),
            'ResponseRules': rules.count(),
            'ResponseKeywords': ResponseKeyword.objects.filter(response_rule__in=rules).count(),
            'ResponseLogs': response_logs.count(),
            'Products': linkers.count(),
            'ProductBlocks': blocks.count(),
            'ProductBlockItems': block_items.count(),
            'StoreCategories': categories.count(),
            'StoreEvents': events.count(),
        }
        self.stdout.write('삭제 예정')
        for label, count in counts.items():
            self.stdout.write(f'{label}: {count}')
        self.stdout.write(f'보존: staff={staff_superuser_count} / superuser={superuser_count} / marketplaces remain untouched')
        if not options['confirm']:
            self.stdout.write(self.style.WARNING('Creator 테스트 데이터 삭제가 수행되지 않았습니다. --confirm 옵션을 사용하십시오.'))
            return
        with transaction.atomic():
            response_logs.delete()
            ResponseKeyword.objects.filter(response_rule__in=rules).delete()
            rules.delete()
            SocialComment.objects.filter(pk__in=comments.values('pk')).delete()
            content_links.delete()
            contents.delete()
            events.delete()
            block_items.delete()
            blocks.delete()
            linkers.delete()
            StoreCategory.objects.filter(pk__in=categories.values('pk')).delete()
            CreatorProfile.objects.filter(user_id__in=user_ids).delete()
            credentials.delete()
            accounts.delete()
            stores.delete()
            users.delete()
        remaining = {
            'Creator users': User.objects.filter(is_staff=False, is_superuser=False).count(),
            'Stores': CreatorStore.objects.filter(owner__is_staff=False, owner__is_superuser=False).count(),
            'SocialAccounts': SocialAccount.objects.filter(owner__is_staff=False, owner__is_superuser=False).count(),
            'Contents': SocialContent.objects.filter(social_account__owner_id__in=user_ids).count(),
            'Comments': SocialComment.objects.filter(social_content__social_account__owner_id__in=user_ids).count(),
            'ResponseRules': rules.count(), 'ResponseLogs': ResponseLog.objects.filter(social_comment__social_content__social_account__owner_id__in=user_ids).count(),
        }
        self.stdout.write(self.style.SUCCESS('Creator 테스트 데이터 초기화 완료'))
        for label, count in remaining.items():
            self.stdout.write(f'{label}: {count}')
        self.stdout.write(f'보존 확인: staff={User.objects.filter(is_staff=True).count()} / superuser={User.objects.filter(is_superuser=True).count()}')
