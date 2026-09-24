from urllib.parse import urlsplit, urlunsplit

from django.db import IntegrityError, transaction

from linker.models import ResponseRule, SocialAccount


class DuplicateSocialAccountError(Exception):
    def __init__(self, account):
        self.account = account
        super().__init__('이미 등록된 계정입니다.')


def normalize_profile_url(value):
    value = (value or '').strip()
    if not value:
        return ''
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip('/'), parsed.query, ''))


def find_duplicate_account(platform, external_account_id='', profile_url=''):
    external_account_id = (external_account_id or '').strip()
    if external_account_id:
        return SocialAccount.objects.filter(platform=platform, external_account_id=external_account_id).first()
    normalized_url = normalize_profile_url(profile_url)
    if normalized_url:
        return SocialAccount.objects.filter(platform=platform, profile_url=normalized_url).first()
    return None


def create_social_account(*, platform, account_name, external_account_id='', profile_url='', display_name='', memo='', status='ACTIVE', apply_default_rules=False, owner=None, store=None):
    profile_url = normalize_profile_url(profile_url)
    with transaction.atomic():
        duplicate = find_duplicate_account(platform, external_account_id, profile_url)
        if duplicate:
            raise DuplicateSocialAccountError(duplicate)
        try:
            with transaction.atomic():
                account = SocialAccount.objects.create(
                    platform=platform,
                    owner=owner,
                    store=store,
                    account_name=account_name.strip(),
                    external_account_id=(external_account_id or '').strip(),
                    profile_url=profile_url,
                    display_name=(display_name or '').strip(),
                    memo=(memo or '').strip(),
                    status=status,
                )
        except IntegrityError:
            duplicate = find_duplicate_account(platform, external_account_id, profile_url)
            if duplicate:
                raise DuplicateSocialAccountError(duplicate)
            raise
        prepare_account_defaults(account, apply_default_rules=apply_default_rules)
        return account


def prepare_account_defaults(account, *, apply_default_rules=False):
    if not apply_default_rules:
        return 0
    created = 0
    for content in account.contents.all():
        _, is_created = ResponseRule.objects.get_or_create(
            social_content=content,
            defaults={'response_mode': ResponseRule.Mode.OFF},
        )
        created += int(is_created)
    return created
