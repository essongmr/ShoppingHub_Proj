from django.db import transaction

from linker.models import CreatorStore, SocialAccount


ASSIGNABLE_PLATFORMS = {
    SocialAccount.Platform.INSTAGRAM,
    SocialAccount.Platform.YOUTUBE,
}


def _validate_owner(owner, value, label):
    if value is None or getattr(value, 'pk', None) != getattr(owner, 'pk', None):
        raise ValueError(f'{label} ownership mismatch')


def get_assignable_social_accounts(owner, store):
    """Return this owner's SNS accounts that can be assigned to a Store."""
    _validate_owner(owner, store.owner if store else None, 'Store')
    return SocialAccount.objects.filter(
        owner=owner,
        platform__in=ASSIGNABLE_PLATFORMS,
        status=SocialAccount.Status.ACTIVE,
    ).order_by('platform', 'id')


def _lock_owned_accounts(owner, account_ids):
    ids = {int(account_id) for account_id in account_ids}
    accounts = list(
        SocialAccount.objects.select_for_update().filter(
            pk__in=ids,
            owner=owner,
            platform__in=ASSIGNABLE_PLATFORMS,
            status=SocialAccount.Status.ACTIVE,
        )
    )
    if len(accounts) != len(ids):
        raise ValueError('소유한 SNS 계정만 Store에 배정할 수 있습니다.')
    return accounts


def assign_social_accounts_to_store(owner, store, account_ids):
    """Assign exactly the requested owned accounts to the Store."""
    _validate_owner(owner, store.owner, 'Store')
    with transaction.atomic():
        accounts = _lock_owned_accounts(owner, account_ids)
        for account in accounts:
            account.store = store
            account.save(update_fields=['store', 'updated_at'])
    return accounts


def move_social_account(owner, account, target_store):
    """Move one owned account without changing its owner or credentials."""
    _validate_owner(owner, account.owner, 'SocialAccount')
    _validate_owner(owner, target_store.owner, 'Store')
    with transaction.atomic():
        locked_account = SocialAccount.objects.select_for_update().get(pk=account.pk)
        if locked_account.owner_id != owner.pk:
            raise ValueError('소유한 SNS 계정만 이동할 수 있습니다.')
        locked_account.store = target_store
        locked_account.save(update_fields=['store', 'updated_at'])
    return locked_account


def unassign_social_account(owner, account):
    """Leave an owned account connected while clearing its Store assignment."""
    _validate_owner(owner, account.owner, 'SocialAccount')
    with transaction.atomic():
        locked_account = SocialAccount.objects.select_for_update().get(pk=account.pk)
        if locked_account.owner_id != owner.pk:
            raise ValueError('소유한 SNS 계정만 배정 해제할 수 있습니다.')
        locked_account.store = None
        locked_account.save(update_fields=['store', 'updated_at'])
    return locked_account
