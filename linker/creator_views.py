from django.contrib import messages
from django.contrib.auth import login
import secrets
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST
import requests
from urllib.parse import urlparse

from .forms import AccountOnboardingForm, CreatorProfileSelectionForm, CreatorStoreIntroForm, ProductBlockForm, ShoppingHubAutomationForm, ShoppingHubProductForm, TypedItemForm, SocialContentLinkForm, CreatorSignupForm, CreatorStoreNameForm, CreatorMessageTemplateForm, ensure_default_store_categories
from .forms import AccountOnboardingForm, SocialContentLinkForm, CreatorStoreForm
from .models import CreatorProfile, CreatorStore, Linker, Marketplace, MessageTemplate, MessageType, ProductBlock, ProductBlockItem, RecipeDetail, ResponseKeyword, ResponseRule, ServiceDetail, SocialAccount, SocialComment, SocialContent, SocialContentLink, SocialCredential, StoreCategory, StoreEvent
from .services.account_onboarding import DuplicateSocialAccountError, create_social_account
from .services.instagram_oauth import InstagramOAuthError, authorization_url as instagram_authorization_url, exchange_code as instagram_exchange_code, verify_account as instagram_verify_account, save_verified_credential as instagram_save_credential, revoke_credential as instagram_revoke_credential, credentials_for_account as instagram_credentials_for_account, fetch_profile_intro as instagram_fetch_profile_intro
from .services.product_drafts import ProductDraft, ProductDraftError, create_product_from_draft, fetch_product_page_draft, has_existing_product, scan_existing_service_product_drafts
from .services.youtube_oauth import YouTubeOAuthError, authorization_url as youtube_authorization_url, exchange_code as youtube_exchange_code, verify_channel as youtube_verify_channel, save_verified_credential as youtube_save_verified_credential, fetch_channel_intro as youtube_fetch_channel_intro, revoke_credential as youtube_revoke_credential
from .services.shoppinghub_dm import build_shoppinghub_target_url, default_dm_message, resolve_target, resolve_target_url
from .services.instagram_content import InstagramContentError, confirm_instagram_content, confirm_instagram_media_by_id, fetch_recent_instagram_media, sync_instagram_contents
from .services.youtube_content import YouTubeReadError, sync_youtube_contents
from .services.instagram_comments import InstagramCommentSyncError, sync_instagram_comments
from .services.active_store import get_active_store, set_active_store
from .services.store_assignment import assign_social_accounts_to_store, move_social_account, unassign_social_account
from .services.product_store_management import (
    ProductStoreManagementError,
    copy_product_to_store,
    delete_product_block_safely,
    delete_product_safely,
    hide_product,
    move_product_to_store,
)
from .services.message_templates import build_preview, clone_creator_template, clone_system_template_for_creator, get_creator_templates, get_system_templates, render_template, validate_message_config

logger = __import__('logging').getLogger(__name__)

PROFILE_IMAGE_CONTENT_TYPES = {
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/webp': '.webp',
}


def _creator_store_or_home(user):
    store = CreatorStore.objects.filter(owner=user).order_by('created_at', 'id').first()
    if store:
        return redirect('public_store', slug=store.slug)
    return redirect('creator_onboarding_start')


@login_required
def hidden_user_ui_redirect(request):
    profile = CreatorProfile.objects.filter(user=request.user).first()
    if profile and profile.onboarding_status != CreatorProfile.OnboardingStatus.COMPLETED:
        return redirect('creator_onboarding_start')
    return redirect('creator_home')


@login_required
def creator_store_redirect(request):
    return _creator_store_or_home(request.user)


def shoppinghub_entry(request):
    if not request.user.is_authenticated:
        return redirect('login')
    profile = CreatorProfile.objects.filter(user=request.user).first()
    if profile and profile.onboarding_status != CreatorProfile.OnboardingStatus.COMPLETED:
        return redirect('creator_onboarding_start')
    if not profile and not CreatorStore.objects.filter(owner=request.user).exists():
        return redirect('creator_onboarding_start')
    return redirect('shoppinghub_home')


def _oauth_return(request, context):
    if context.get('return_to') == 'onboarding_profile':
        return redirect(f'{reverse("creator_onboarding_store")}?step=1')
    return redirect('creator_channels')


def _existing_owned_account(owner, platform, external_id):
    account = SocialAccount.objects.filter(platform=platform).filter(
        Q(platform_user_id=external_id) | Q(external_account_id=external_id)
    ).first()
    if account and account.owner_id not in (None, owner.pk):
        raise ValueError('이 Instagram 계정은 이미 다른 Store에 연결되어 있습니다.')
    return account


def _log_integrity_error(logger, exc, *, stage, logical_key, model):
    cause = getattr(exc, '__cause__', None)
    diagnostics = getattr(cause, 'diag', None)
    logger.exception(
        'Instagram callback database failure: stage=%s model=%s logical_key=%s '
        'constraint=%s table=%s column=%s pgcode=%s',
        stage,
        model,
        logical_key,
        getattr(diagnostics, 'constraint_name', None),
        getattr(diagnostics, 'table_name', None),
        getattr(diagnostics, 'column_name', None),
        getattr(cause, 'pgcode', None) or getattr(exc, 'pgcode', None),
    )


def _store_accounts(user):
    """Return this creator's accounts while retaining legacy owner-only rows."""
    store = CreatorStore.objects.filter(owner=user).order_by('created_at', 'id').first()
    query = Q(owner=user)
    if store:
        query &= Q(store=store) | Q(store__isnull=True)
    return SocialAccount.objects.filter(query)


def _active_store_or_onboarding(request):
    store = get_active_store(request)
    if not store:
        return None
    return store


def _store_scope(store):
    first_store_id = CreatorStore.objects.filter(owner=store.owner).order_by('created_at', 'id').values_list('pk', flat=True).first()
    return Q(store=store) | Q(store__isnull=True) if store.pk == first_store_id else Q(store=store)


@login_required
@require_POST
def switch_store(request):
    store = get_object_or_404(CreatorStore, pk=request.POST.get('store_id'), owner=request.user, active=True)
    set_active_store(request, store)
    return redirect(request.POST.get('next') or 'shoppinghub_home')


CREATE_STORE_WIZARD_SESSION_KEY = 'creator_store_wizard'


def _new_store_slug(store_name):
    base = slugify(store_name) or 'store'
    if not slugify(store_name):
        base = f'store-{secrets.token_hex(3)}'
    candidate = base[:80]
    suffix = 2
    while CreatorStore.objects.filter(slug__iexact=candidate).exists():
        tail = f'-{suffix}'
        candidate = f'{base[:80 - len(tail)]}{tail}'
        suffix += 1
    return candidate


@login_required
def create_store(request):
    wizard = request.session.get(CREATE_STORE_WIZARD_SESSION_KEY, {})
    try:
        step = int(request.GET.get('step') or request.POST.get('step') or wizard.get('step', 1))
    except ValueError:
        step = 1
    step = min(2, max(1, step))
    if request.method == 'POST' and step == 1:
        form = CreatorStoreNameForm(request.POST)
        if form.is_valid():
            request.session[CREATE_STORE_WIZARD_SESSION_KEY] = {
                'step': 2,
                'store_name': form.cleaned_data['store_name'],
            }
            request.session.modified = True
            return redirect(f'{reverse("creator_store_new")}?step=2')
        return render(request, 'linker/creator/store_setup.html', {'form': form, 'store': None, 'accounts': [], 'step': 1})

    store_name = wizard.get('store_name', '')
    if step == 2 and not store_name:
        return redirect('creator_store_new')
    form = CreatorStoreNameForm(initial={'store_name': store_name})
    accounts = SocialAccount.objects.filter(
        owner=request.user,
        platform__in=[SocialAccount.Platform.INSTAGRAM, SocialAccount.Platform.YOUTUBE],
        status=SocialAccount.Status.ACTIVE,
    ).select_related('store').order_by('platform', 'id')
    selected_ids = {str(value) for value in request.POST.getlist('account_ids')} if request.method == 'POST' else set()
    conflicts = [account for account in accounts if str(account.pk) in selected_ids and account.store_id]
    if request.method == 'POST' and step == 2:
        confirmed = request.POST.get('confirm_move') == '1'
        if conflicts and not confirmed:
            return render(request, 'linker/creator/store_setup.html', {
                'form': form,
                'store': None,
                'accounts': accounts,
                'step': 2,
                'store_name': store_name,
                'selected_ids': selected_ids,
                'move_conflicts': conflicts,
            })
        try:
            with transaction.atomic():
                store = CreatorStore.objects.create(
                    owner=request.user,
                    store_name=store_name,
                    slug=_new_store_slug(store_name),
                    active=True,
                )
                assign_social_accounts_to_store(request.user, store, selected_ids)
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            request.session.pop(CREATE_STORE_WIZARD_SESSION_KEY, None)
            set_active_store(request, store)
            messages.success(request, f'{store.store_name} Store를 만들었습니다.')
            return redirect('shoppinghub_home')
    return render(request, 'linker/creator/store_setup.html', {
        'form': form,
        'store': None,
        'accounts': accounts,
        'step': step,
        'store_name': store_name,
        'selected_ids': selected_ids,
        'move_conflicts': conflicts if request.method == 'POST' else [],
    })


def _set_primary_instagram(user, account):
    if account.platform != SocialAccount.Platform.INSTAGRAM or account.owner_id != user.pk:
        raise Http404
    profile, _ = CreatorProfile.objects.get_or_create(user=user)
    profile.representative_social_account = account
    profile.save(update_fields=['representative_social_account', 'updated_at'])


def _promote_primary_if_needed(user, account):
    profile = CreatorProfile.objects.filter(user=user, representative_social_account=account).first()
    if not profile:
        return
    replacement = SocialAccount.objects.filter(owner=user).filter(
        platform=SocialAccount.Platform.INSTAGRAM,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    ).exclude(pk=account.pk).order_by('id').first()
    profile.representative_social_account = replacement
    profile.save(update_fields=['representative_social_account', 'updated_at'])


def signup(request):
    form = CreatorSignupForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                user = form.save()
                CreatorProfile.objects.get_or_create(
                    user=user,
                    defaults={
                        'onboarding_status': CreatorProfile.OnboardingStatus.ACCOUNT_CREATED,
                    },
                )
        except IntegrityError:
            form.add_error('username', '이미 사용 중인 아이디입니다.')
        else:
            login(request, user)
            return redirect('creator_onboarding_start')
    return render(request, 'linker/creator/signup.html', {'form': form})


def creator_onboarding_start(request):
    if request.user.is_authenticated:
        profile, _ = CreatorProfile.objects.get_or_create(user=request.user)
        if profile and profile.onboarding_status == CreatorProfile.OnboardingStatus.COMPLETED:
            return redirect('creator_home')
        return redirect('creator_onboarding_store')

    return redirect('signup')


def _reserved_store_slugs():
    return {
        'admin', 'login', 'logout', 'signup', 'workspace', 'creator', 'api',
        'oauth', 'ui-preview', 'static', 'media', 'settings',
    }


def _store_slug_for(user):
    base = slugify(user.username) or 'creator'
    if base in _reserved_store_slugs():
        base = f'creator-{user.pk}'
    candidate = base[:80]
    suffix = 2
    while CreatorStore.objects.filter(slug__iexact=candidate).exclude(owner=user).exists():
        tail = f'-{suffix}'
        candidate = f'{base[:80 - len(tail)]}{tail}'
        suffix += 1
    return candidate


def _onboarding_step_url(step):
    return f'{reverse("creator_onboarding_store")}?step={step}'


def _wizard_step_from_status(profile):
    return {
        CreatorProfile.OnboardingStatus.ACCOUNT_CREATED: 1,
        CreatorProfile.OnboardingStatus.STORE_NAMING: 2,
        CreatorProfile.OnboardingStatus.PROFILE_SELECTION: 3,
        CreatorProfile.OnboardingStatus.STORE_INTRO: 4,
        CreatorProfile.OnboardingStatus.STORE_ADDRESS: 5,
    }.get(profile.onboarding_status, 1)


def _connected_profile_accounts(user):
    accounts = SocialAccount.objects.filter(
        owner=user,
        platform__in=[SocialAccount.Platform.INSTAGRAM, SocialAccount.Platform.YOUTUBE],
        status=SocialAccount.Status.ACTIVE,
    ).select_related('credential').order_by('platform', 'id')
    return [
        account for account in accounts
        if (
            account.platform == SocialAccount.Platform.INSTAGRAM
            and getattr(account, 'credential', None)
            and account.credential.provider == 'META_INSTAGRAM'
            and not account.credential.revoked_at
        ) or (
            account.platform == SocialAccount.Platform.YOUTUBE
            and account.youtube_oauth_status == 'CONNECTED'
        )
    ]


def _store_name_candidates(accounts):
    seen = set()
    rows = []
    for account in accounts:
        name = (account.display_name or account.account_name or account.username or '').strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            rows.append({'account': account, 'name': name})
    return rows


def _store_intro_sources(accounts):
    rows = []
    for account in accounts:
        text = (account.memo or '').strip()
        if text:
            rows.append({'account': account, 'text': text})
    return rows


def _default_onboarding_account(profile, accounts):
    if profile.representative_social_account_id:
        for account in accounts:
            if account.pk == profile.representative_social_account_id:
                return account
    return accounts[0] if accounts else None


def _account_display_name(account):
    if not account:
        return ''
    return (account.display_name or account.account_name or account.username or '').strip()


def _slug_candidate_for_store(store, user):
    if store and store.slug:
        return store.slug
    base = slugify(store.store_name if store and store.store_name else user.username)
    return base or _store_slug_for(user)


def _clean_store_slug(value, user, store=None):
    value = slugify((value or '').strip())
    if not value:
        raise ValueError('Store 주소를 입력해 주세요.')
    if value in _reserved_store_slugs():
        raise ValueError('사용할 수 없는 Store 주소입니다.')
    if CreatorStore.objects.filter(slug__iexact=value).exclude(owner=user).exists():
        raise ValueError('이미 사용 중인 Store 주소입니다.')
    return value[:80]


def _save_social_profile_image(store, account):
    if not account or not account.profile_picture_url:
        return False
    try:
        response = requests.get(account.profile_picture_url, timeout=10)
        response.raise_for_status()
    except requests.RequestException:
        logger.info('Social profile image download failed: account_id=%s', account.pk, exc_info=True)
        return False
    content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
    extension = PROFILE_IMAGE_CONTENT_TYPES.get(content_type)
    if not extension:
        logger.info('Social profile image skipped due to unsupported type: account_id=%s content_type=%s', account.pk, content_type)
        return False
    store.profile_image.save(
        f'social-profile-{account.pk}{extension}',
        ContentFile(response.content),
        save=False,
    )
    store.save(update_fields=['profile_image', 'updated_at'])
    return True


SHOPPINGHUB_IMPORT_SESSION_KEY = 'shoppinghub_migration_drafts'


def _migration_session_payload(owner, store, source_url, drafts):
    return {
        'owner_id': owner.pk,
        'store_id': store.pk if store else None,
        'source_url': source_url,
        'drafts': [draft.as_dict() for draft in drafts],
    }


def _migration_payload(request, store):
    payload = request.session.get(SHOPPINGHUB_IMPORT_SESSION_KEY)
    if not isinstance(payload, dict):
        return None
    if payload.get('owner_id') != request.user.pk:
        return None
    expected_store_id = store.pk if store else None
    if payload.get('store_id') != expected_store_id:
        return None
    drafts = payload.get('drafts')
    if not isinstance(drafts, list):
        return None
    return payload


def _clear_migration_payload(request):
    request.session.pop(SHOPPINGHUB_IMPORT_SESSION_KEY, None)


def _migration_preview(owner, request, store):
    payload = _migration_payload(request, store)
    if payload is None:
        return []
    rows = []
    for index, item in enumerate(payload.get('drafts', [])):
        if not isinstance(item, dict):
            continue
        draft = ProductDraft.from_dict(item)
        available = bool(draft.title.strip() and draft.destination_url.strip())
        duplicate = has_existing_product(owner, draft, store=store) if available else False
        rows.append({
            'index': index,
            'draft': draft,
            'duplicate': duplicate,
            'available': available and not duplicate,
        })
    return rows


@login_required
def creator_onboarding_store(request):
    profile, _ = CreatorProfile.objects.get_or_create(user=request.user)
    if profile.onboarding_status == CreatorProfile.OnboardingStatus.COMPLETED:
        return redirect('creator_home')
    store = CreatorStore.objects.filter(owner=request.user).first()
    accounts = _connected_profile_accounts(request.user)
    selected_account = _default_onboarding_account(profile, accounts)
    name_candidates = _store_name_candidates(accounts)
    intro_sources = _store_intro_sources(accounts)
    requested_step = request.GET.get('step') or request.POST.get('step')
    try:
        step = int(requested_step) if requested_step else _wizard_step_from_status(profile)
    except ValueError:
        step = _wizard_step_from_status(profile)
    step = min(5, max(1, step))
    if not store:
        suggested_name = name_candidates[0]['name'] if name_candidates else request.user.username
        store = CreatorStore(owner=request.user, store_name=suggested_name, slug=_store_slug_for(request.user), active=True)
    errors = []
    refreshed_intro_text = ''

    if request.method == 'POST':
        action = request.POST.get('action', 'next')
        if action == 'prev':
            return redirect(_onboarding_step_url(max(1, step - 1)))
        if step == 1:
            if selected_account and profile.representative_social_account_id != selected_account.pk:
                profile.representative_social_account = selected_account
            profile.onboarding_status = CreatorProfile.OnboardingStatus.STORE_NAMING
            profile.save(update_fields=['representative_social_account', 'onboarding_status', 'updated_at'])
            return redirect(_onboarding_step_url(2))
        if step == 2:
            store_name = request.POST.get('store_name', '').strip()
            if not store_name:
                errors.append('Store 이름을 입력해 주세요.')
            else:
                with transaction.atomic():
                    store, _ = CreatorStore.objects.select_for_update().get_or_create(
                        owner=request.user,
                        defaults={'store_name': store_name, 'slug': _store_slug_for(request.user), 'active': True},
                    )
                    if store.store_name != store_name:
                        store.store_name = store_name
                        store.save(update_fields=['store_name', 'updated_at'])
                    profile.onboarding_status = CreatorProfile.OnboardingStatus.PROFILE_SELECTION
                    profile.save(update_fields=['onboarding_status', 'updated_at'])
                return redirect(_onboarding_step_url(3))
        elif step == 3:
            form = CreatorProfileSelectionForm(request.POST, request.FILES)
            if form.errors.get('profile_image'):
                errors.extend(form.errors['profile_image'])
            if form.is_valid():
                account = None
                account_id = form.cleaned_data.get('account_id')
                if account_id:
                    account = get_object_or_404(
                        SocialAccount,
                        pk=account_id,
                        owner=request.user,
                        status=SocialAccount.Status.ACTIVE,
                        platform__in=[SocialAccount.Platform.INSTAGRAM, SocialAccount.Platform.YOUTUBE],
                    )
                    if account.pk not in {item.pk for item in accounts}:
                        raise Http404
                selected_account_ids = request.POST.getlist('account_ids')
                if 'account_ids' not in request.POST:
                    selected_account_ids = [str(account.pk)] if account else [str(item.pk) for item in accounts] if len(accounts) == 1 else []
                selected_ids = {int(value) for value in selected_account_ids if value.isdigit()}
                available_ids = {item.pk for item in accounts}
                if not selected_ids.issubset(available_ids):
                    raise Http404
                with transaction.atomic():
                    store, _ = CreatorStore.objects.select_for_update().get_or_create(
                        owner=request.user,
                        defaults={'store_name': request.user.username, 'slug': _store_slug_for(request.user), 'active': True},
                    )
                    assign_social_accounts_to_store(request.user, store, selected_ids)
                    for item in accounts:
                        if item.pk not in selected_ids and item.store_id == store.pk:
                            unassign_social_account(request.user, item)
                    profile.representative_social_account = account
                    if form.cleaned_data.get('profile_image'):
                        store.profile_image = form.cleaned_data['profile_image']
                        store.save(update_fields=['profile_image', 'updated_at'])
                    elif account and request.POST.get('profile_image_source') == 'social':
                        _save_social_profile_image(store, account)
                    profile.onboarding_status = CreatorProfile.OnboardingStatus.STORE_INTRO
                    profile.save(update_fields=['representative_social_account', 'onboarding_status', 'updated_at'])
                return redirect(_onboarding_step_url(4))
            errors.extend(form.non_field_errors())
        elif step == 4 and action == 'refresh_intro':
            account = get_object_or_404(
                SocialAccount,
                pk=request.POST.get('intro_account_id') or request.GET.get('intro_account_id'),
                owner=request.user,
                status=SocialAccount.Status.ACTIVE,
                platform__in=[SocialAccount.Platform.INSTAGRAM, SocialAccount.Platform.YOUTUBE],
            )
            if account.pk not in {item.pk for item in accounts}:
                raise Http404
            try:
                if account.platform == SocialAccount.Platform.INSTAGRAM:
                    refreshed_intro_text = instagram_fetch_profile_intro(account)
                else:
                    refreshed_intro_text = youtube_fetch_channel_intro(account)
            except (InstagramOAuthError, YouTubeOAuthError):
                errors.append(f'{account.get_platform_display()} 다시 연결하기가 필요합니다.')
            else:
                if not refreshed_intro_text:
                    errors.append(f'{account.get_platform_display()}에서 가져올 소개문구가 없습니다.')
                accounts = _connected_profile_accounts(request.user)
                intro_sources = _store_intro_sources(accounts)
        elif step == 4:
            tagline = request.POST.get('tagline', '').strip()
            with transaction.atomic():
                store = CreatorStore.objects.select_for_update().get(owner=request.user)
                store.tagline = tagline
                store.save(update_fields=['tagline', 'updated_at'])
                profile.onboarding_status = CreatorProfile.OnboardingStatus.STORE_ADDRESS
                profile.save(update_fields=['onboarding_status', 'updated_at'])
            return redirect(_onboarding_step_url(5))
        elif step == 5:
            try:
                slug = _clean_store_slug(request.POST.get('slug'), request.user, store)
            except ValueError as exc:
                errors.append(str(exc))
            else:
                with transaction.atomic():
                    store = CreatorStore.objects.select_for_update().get(owner=request.user)
                    store.slug = slug
                    if not store.active:
                        store.active = True
                    store.save(update_fields=['slug', 'active', 'updated_at'])
                    profile.onboarding_status = CreatorProfile.OnboardingStatus.COMPLETED
                    profile.save(update_fields=['onboarding_status', 'updated_at'])
                return redirect('creator_onboarding_complete')

    suggested_store_name = store.store_name or (name_candidates[0]['name'] if name_candidates else '')
    account_name = _account_display_name(selected_account)
    preview_profile_image_url = ''
    if getattr(store, 'profile_image', None):
        preview_profile_image_url = store.profile_image.url
    elif selected_account and selected_account.profile_picture_url:
        preview_profile_image_url = selected_account.profile_picture_url

    context = {
        'step': step,
        'steps': range(1, 6),
        'errors': errors,
        'accounts': accounts,
        'store': store,
        'selected_account': selected_account,
        'name_candidates': name_candidates,
        'intro_sources': intro_sources,
        'refreshed_intro_text': refreshed_intro_text,
        'suggested_store_name': suggested_store_name,
        'preview_store_name': suggested_store_name or account_name,
        'preview_profile_image_url': preview_profile_image_url,
        'suggested_slug': _slug_candidate_for_store(store, request.user),
    }
    return render(request, 'linker/creator/onboarding_store.html', context)


@login_required
def creator_onboarding_profile(request):
    return redirect(_onboarding_step_url(3))


@login_required
def creator_onboarding_intro(request):
    return redirect(_onboarding_step_url(4))


@login_required
def creator_onboarding_address(request):
    profile, _ = CreatorProfile.objects.get_or_create(user=request.user)
    if profile.onboarding_status == CreatorProfile.OnboardingStatus.COMPLETED:
        return redirect('creator_home')
    return redirect(_onboarding_step_url(5))


@login_required
def creator_onboarding_complete(request):
    profile, _ = CreatorProfile.objects.get_or_create(user=request.user)
    if profile.onboarding_status != CreatorProfile.OnboardingStatus.COMPLETED:
        return redirect(_onboarding_step_url(5))
    store = get_object_or_404(CreatorStore, owner=request.user)
    accounts = SocialAccount.objects.filter(owner=request.user, status=SocialAccount.Status.ACTIVE, platform__in=[SocialAccount.Platform.INSTAGRAM, SocialAccount.Platform.YOUTUBE])
    return render(request, 'linker/creator/onboarding_complete.html', {'store': store, 'profile': profile, 'accounts': accounts})


@login_required
def home(request):
    profile = CreatorProfile.objects.filter(user=request.user).first()
    legacy_store = CreatorStore.objects.filter(owner=request.user).exists()
    if profile and profile.onboarding_status != CreatorProfile.OnboardingStatus.COMPLETED:
        return redirect('creator_onboarding_start')
    if not profile and not legacy_store:
        return redirect('creator_onboarding_start')
    store = _active_store_or_onboarding(request)
    if not store:
        return redirect('creator_onboarding_start')
    accounts = SocialAccount.objects.filter(owner=request.user).filter(Q(store=store) | Q(store__isnull=True))
    contents = SocialContent.objects.filter(social_account__in=accounts)
    comments = SocialComment.objects.filter(social_content__in=contents)
    products = Linker.objects.filter(owner=request.user, status='ACTIVE').filter(_store_scope(store))
    product_blocks = ProductBlock.objects.filter(owner=request.user, active=True).filter(_store_scope(store))
    stats = StoreEvent.objects.filter(store=store).values('event_type').annotate(total=Count('id'))
    stats = {item['event_type']: item['total'] for item in stats}
    profile = CreatorProfile.objects.filter(user=request.user).first()
    return render(request, 'linker/creator/home.html', {'accounts': accounts, 'contents': contents, 'comments': comments, 'products': products, 'product_blocks': product_blocks, 'store': store, 'primary_account_id': profile.representative_social_account_id if profile else None, 'unlinked_contents': contents.filter(product_links__isnull=True).distinct(), 'store_views': stats.get(StoreEvent.EventType.STORE_VIEW, 0), 'product_views': stats.get(StoreEvent.EventType.PRODUCT_VIEW, 0), 'outbound_clicks': stats.get(StoreEvent.EventType.OUTBOUND_CLICK, 0), 'block_views': stats.get(StoreEvent.EventType.BLOCK_VIEW, 0)})

@login_required
def shoppinghub_stats(request):
    store = get_active_store(request)
    if not store:
        return redirect('creator_onboarding_start')
    counts = StoreEvent.objects.filter(store=store).values('event_type').annotate(total=Count('id'))
    stats = {item['event_type']: item['total'] for item in counts}
    popular_products = StoreEvent.objects.filter(store=store, linker__isnull=False).values('linker__product_no', 'linker__title').annotate(total=Count('id')).order_by('-total')[:4]
    popular_blocks = StoreEvent.objects.filter(store=store, product_block__isnull=False).values('product_block__block_number', 'product_block__title').annotate(total=Count('id')).order_by('-total')[:4]
    return render(request, 'linker/creator/stats.html', {'store': store, 'store_views': stats.get(StoreEvent.EventType.STORE_VIEW, 0), 'product_views': stats.get(StoreEvent.EventType.PRODUCT_VIEW, 0), 'block_views': stats.get(StoreEvent.EventType.BLOCK_VIEW, 0), 'outbound_clicks': stats.get(StoreEvent.EventType.OUTBOUND_CLICK, 0), 'popular_products': popular_products, 'popular_blocks': popular_blocks})


@login_required
def creator_billing(request):
    store = CreatorStore.objects.filter(owner=request.user).first()
    return render(request, 'linker/creator/billing.html', {'store': store})


@login_required
def creator_message_templates(request):
    message_type = MessageType.objects.filter(pk=request.GET.get('type'), active=True).first() if request.GET.get('type', '').isdigit() else None
    return render(request, 'linker/creator/message_templates.html', {
        'message_types': MessageType.objects.filter(active=True),
        'system_templates': get_system_templates(message_type),
        'creator_templates': get_creator_templates(request.user, message_type),
        'selected_type': message_type,
    })


@login_required
@require_POST
def creator_message_template_copy(request, pk):
    template = get_object_or_404(MessageTemplate, pk=pk, owner_type=MessageTemplate.OwnerType.SYSTEM, active=True)
    clone_system_template_for_creator(template, request.user)
    messages.success(request, '내 메시지로 복사했습니다.')
    return redirect('creator_message_templates')


@login_required
def creator_message_template_edit(request, pk=None):
    template = get_object_or_404(MessageTemplate, pk=pk, owner_type=MessageTemplate.OwnerType.CREATOR, creator=request.user) if pk else None
    form = CreatorMessageTemplateForm(request.POST or None, instance=template)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.owner_type = MessageTemplate.OwnerType.CREATOR
        obj.creator = request.user
        obj.active = True
        obj.save()
        messages.success(request, '내 메시지를 저장했습니다.')
        return redirect('creator_message_templates')
    return render(request, 'linker/creator/message_template_form.html', {'form': form, 'template': template})


@login_required
@require_POST
def creator_message_template_action(request, pk, action):
    template = get_object_or_404(MessageTemplate, pk=pk, owner_type=MessageTemplate.OwnerType.CREATOR, creator=request.user)
    if action == 'copy':
        clone_creator_template(template, request.user)
        messages.success(request, '내 메시지를 복사했습니다.')
    elif action == 'disable':
        template.active = False
        template.save(update_fields=['active', 'updated_at'])
        messages.success(request, '내 메시지를 사용중지했습니다.')
    return redirect('creator_message_templates')


AUTO_DM_WIZARD_SESSION_KEY = 'creator_auto_dm_wizard'


def _wizard_store_target(request, store, target_type, target_id):
    if target_type == ResponseRule.TargetType.PRODUCT:
        return get_object_or_404(Linker, pk=target_id, owner=request.user, store=store, status='ACTIVE')
    if target_type == ResponseRule.TargetType.PRODUCT_BLOCK:
        return get_object_or_404(ProductBlock, pk=target_id, owner=request.user, store=store, active=True)
    if target_type == ResponseRule.TargetType.STORE:
        if str(target_id) != str(store.pk):
            raise Http404
        return store
    raise Http404


def _wizard_target_allowed(message_type, target):
    if message_type.code == 'DIRECT':
        return True
    if isinstance(target, CreatorStore):
        return message_type.code == 'STORE_INFO'
    if isinstance(target, ProductBlock):
        if message_type.code == 'RECIPE_INFO':
            return target.items.filter(linker__item_type=Linker.ItemType.RECIPE).exists()
        return message_type.code in {'PRODUCT_INFO', 'SERVICE_INFO'}
    item_type = getattr(target, 'item_type', Linker.ItemType.PRODUCT)
    return {
        'RECIPE_INFO': item_type == Linker.ItemType.RECIPE,
        'PRODUCT_INFO': item_type == Linker.ItemType.PRODUCT,
        'SERVICE_INFO': item_type == Linker.ItemType.SERVICE,
        'STORE_INFO': False,
    }.get(message_type.code, True)


def _wizard_target_options(request, store, message_type):
    preferred = {
        'RECIPE_INFO': Linker.ItemType.RECIPE,
        'PRODUCT_INFO': Linker.ItemType.PRODUCT,
        'SERVICE_INFO': Linker.ItemType.SERVICE,
    }.get(message_type.code)
    items = Linker.objects.filter(owner=request.user, store=store, status='ACTIVE').order_by('item_type', 'title', 'id')
    if preferred:
        items = sorted(items, key=lambda item: (item.item_type != preferred, item.title.casefold(), item.pk))
    blocks = ProductBlock.objects.filter(owner=request.user, store=store, active=True).order_by('title', 'id')
    return items, blocks


def _wizard_parse_target_token(value, store):
    target_type, separator, target_id = (value or '').partition(':')
    if not separator or not target_id.isdigit():
        raise ValidationError('보낼 항목을 선택해 주세요.')
    target_type = ResponseRule.TargetType.STORE if target_type == 'STORE' else target_type
    if target_type == 'ITEM':
        target_type = ResponseRule.TargetType.PRODUCT
    if target_type not in {ResponseRule.TargetType.PRODUCT, ResponseRule.TargetType.PRODUCT_BLOCK, ResponseRule.TargetType.STORE}:
        raise ValidationError('보낼 항목을 확인해 주세요.')
    return target_type, int(target_id)


@login_required
def auto_dm_wizard(request):
    store = get_active_store(request)
    if not store:
        return redirect('creator_onboarding_start')
    state = request.session.get(AUTO_DM_WIZARD_SESSION_KEY, {})

    saved_rule = None
    saved_id = request.GET.get('saved', '')
    if request.method == 'GET' and saved_id.isdigit():
        saved_rule = (
            ResponseRule.objects
            .filter(pk=int(saved_id), store=store)
            .select_related(
                'social_content',
                'target_linker',
                'target_product_block',
                'target_store',
                'message_template__message_type',
            )
            .first()
        )

    # Existing Auto DM edit entry.
    edit_id = request.GET.get('edit', '')
    if request.method == 'GET' and edit_id.isdigit():
        edit_rule = get_object_or_404(
            ResponseRule.objects
            .select_related(
                'social_account',
                'social_content',
                'target_linker',
                'target_product_block',
                'target_store',
                'message_template__message_type',
            )
            .prefetch_related('keywords'),
            pk=int(edit_id),
            store=store,
        )

        include_keywords = [
            item.keyword
            for item in edit_rule.keywords.all()
            if not item.is_exclusion
        ]
        exclude_keywords = [
            item.keyword
            for item in edit_rule.keywords.all()
            if item.is_exclusion
        ]

        first_keyword = next(iter(edit_rule.keywords.all()), None)
        match_type = (
            first_keyword.match_type
            if first_keyword
            else 'CONTAINS'
        )

        if edit_rule.message_template:
            message_type_id = edit_rule.message_template.message_type_id
        else:
            direct_type = MessageType.objects.filter(
                code='DIRECT',
                active=True,
            ).first()
            message_type_id = direct_type.pk if direct_type else None

        target_type = edit_rule.target_type

        if target_type == ResponseRule.TargetType.PRODUCT:
            target_id = edit_rule.target_linker_id
        elif target_type == ResponseRule.TargetType.PRODUCT_BLOCK:
            target_id = edit_rule.target_product_block_id
        else:
            target_id = edit_rule.target_store_id or store.pk

        state = {
            'step': 1,
            'edit_rule_id': edit_rule.pk,
            'account_id': (
                edit_rule.social_account_id
                or (
                    edit_rule.social_content.social_account_id
                    if edit_rule.social_content
                    else None
                )
            ),
            'content_id': edit_rule.social_content_id,
            'mode': edit_rule.response_mode,
            'include_keywords': ', '.join(include_keywords),
            'exclude_keywords': ', '.join(exclude_keywords),
            'match_type': match_type,
            'starts_at': (
                timezone.localtime(edit_rule.starts_at).strftime('%Y-%m-%dT%H:%M')
                if edit_rule.starts_at
                else ''
            ),
            'ends_at': (
                timezone.localtime(edit_rule.ends_at).strftime('%Y-%m-%dT%H:%M')
                if edit_rule.ends_at
                else ''
            ),
            'message_type_id': message_type_id,
            'template_id': edit_rule.message_template_id,
            'target_type': target_type,
            'target_id': target_id,
            'message_config': edit_rule.message_config or {},
            'preview': edit_rule.rendered_message_snapshot or '',
        }

        request.session[AUTO_DM_WIZARD_SESSION_KEY] = state
        request.session.modified = True

    # A plain GET means "start a new Auto DM".
    # Explicit edit entry and ?step=N continue wizard state.
    elif request.method == 'GET' and not request.GET.get('step'):
        state = {}

        requested_type = request.GET.get('target_type', '').upper()
        requested_id = request.GET.get('target_id', '')

        if requested_id.isdigit():
            requested_type = (
                ResponseRule.TargetType.PRODUCT
                if requested_type == 'ITEM'
                else requested_type
            )
            if requested_type in {
                ResponseRule.TargetType.PRODUCT,
                ResponseRule.TargetType.PRODUCT_BLOCK,
                ResponseRule.TargetType.STORE,
            }:
                state.update({
                    'target_type': requested_type,
                    'target_id': int(requested_id),
                })

        request.session[AUTO_DM_WIZARD_SESSION_KEY] = state
        request.session.modified = True

    try:
        step = int(request.GET.get('step') or request.POST.get('step') or state.get('step', 1))
    except ValueError:
        step = 1
    step = min(4, max(1, step))
    accounts = SocialAccount.objects.filter(owner=request.user, store=store, platform=SocialAccount.Platform.INSTAGRAM, status=SocialAccount.Status.ACTIVE).order_by('id')
    account = accounts.filter(pk=state.get('account_id')).first() if state.get('account_id') else accounts.first()
    contents = SocialContent.objects.filter(social_account__in=accounts, status='ACTIVE').order_by('-published_at', '-id')
    content = contents.filter(pk=state.get('content_id')).first() if state.get('content_id') else contents.first()
    if request.method == 'POST':
        if step == 1:
            account = get_object_or_404(accounts, pk=request.POST.get('account_id'))
            content = get_object_or_404(contents.filter(social_account=account), pk=request.POST.get('content_id'))
            starts_at = parse_datetime(request.POST.get('starts_at', '')) if request.POST.get('starts_at') else None
            ends_at = parse_datetime(request.POST.get('ends_at', '')) if request.POST.get('ends_at') else None
            if starts_at and timezone.is_naive(starts_at): starts_at = timezone.make_aware(starts_at)
            if ends_at and timezone.is_naive(ends_at): ends_at = timezone.make_aware(ends_at)
            if starts_at and ends_at and starts_at > ends_at:
                messages.error(request, '사용 기간을 확인해 주세요.')
            else:
                state.update({'step': 2, 'account_id': account.pk, 'content_id': content.pk, 'mode': request.POST.get('mode', 'ALL'), 'include_keywords': request.POST.get('include_keywords', ''), 'exclude_keywords': request.POST.get('exclude_keywords', ''), 'match_type': request.POST.get('match_type', 'CONTAINS'), 'starts_at': starts_at.isoformat() if starts_at else '', 'ends_at': ends_at.isoformat() if ends_at else ''})
                request.session[AUTO_DM_WIZARD_SESSION_KEY] = state
                request.session.modified = True
                return redirect(f'{reverse("shoppinghub_auto_dm")}?step=2')
        elif step == 2:
            message_type = MessageType.objects.filter(pk=request.POST.get('message_type_id'), active=True).first()
            if not message_type:
                messages.warning(request, '사용할 메시지 유형을 선택해 주세요.')
                return redirect(f'{reverse("shoppinghub_auto_dm")}?step=2')
            state.update({'step': 3, 'message_type_id': message_type.pk})
            request.session[AUTO_DM_WIZARD_SESSION_KEY] = state
            request.session.modified = True
            return redirect(f'{reverse("shoppinghub_auto_dm")}?step=3')
        elif step == 3:
            message_type = MessageType.objects.filter(pk=state.get('message_type_id'), active=True).first()
            if not message_type:
                messages.warning(request, '사용할 메시지 유형이 아직 준비되지 않았습니다.')
                return redirect(f'{reverse("shoppinghub_auto_dm")}?step=2')
            try:
                template = None if message_type.code == 'DIRECT' else get_object_or_404(MessageTemplate.objects.filter(active=True, message_type=message_type).filter(Q(owner_type=MessageTemplate.OwnerType.SYSTEM) | Q(owner_type=MessageTemplate.OwnerType.CREATOR, creator=request.user)), pk=request.POST.get('template_id'))
                token = request.POST.get('target_token')
                if token:
                    target_type, target_id = _wizard_parse_target_token(token, store)
                else:
                    target_type = request.POST.get('target_type') or state.get('target_type', ResponseRule.TargetType.STORE)
                    target_id = request.POST.get('target_id') or state.get('target_id') or store.pk
                target = _wizard_store_target(request, store, target_type, target_id)
                if not _wizard_target_allowed(message_type, target):
                    raise ValidationError('선택한 메시지 유형과 보낼 항목을 확인해 주세요.')
                config = {'store_name': store.store_name, 'store_url': request.build_absolute_uri(reverse('public_store', args=[store.slug])), 'intro': request.POST.get('intro', '')}
                schema = message_type.input_schema if template is None else template.message_type.input_schema
                for field in schema.get('fields', []):
                    config[field['key']] = request.POST.get(f"field_{field['key']}", '')
                if isinstance(target, Linker):
                    config.update({'product_name': config.get('product_name') or target.title, 'recipe_name': config.get('recipe_name') or target.title, 'service_name': config.get('service_name') or target.title, 'product_no': target.product_no})
                target_url = request.build_absolute_uri(resolve_target_url(store, target_type, target))
                config.update({'product_url': target_url, 'recipe_url': target_url, 'service_url': target_url})
                body = request.POST.get('direct_message', '').strip() if template is None else render_template(template, config)
                validate_message_config(message_type, config)
                if not body:
                    raise ValidationError('메시지 내용을 입력해 주세요.')
                preview = body.rstrip() + '\n\n' + target_url
            except (ValidationError, ValueError, Http404) as exc:
                print(
                    f"AUTO_DM_PREVIEW_ERROR type={type(exc).__name__} "
                    f"message={exc!r}",
                    flush=True,
                )
                messages.error(request, str(exc) or '선택한 보낼 항목을 확인해 주세요.')
                message_types = MessageType.objects.filter(active=True).order_by('display_order', 'id')
                selected_type = message_type
                templates = MessageTemplate.objects.filter(active=True, message_type=message_type).filter(Q(owner_type=MessageTemplate.OwnerType.SYSTEM) | Q(owner_type=MessageTemplate.OwnerType.CREATOR, creator=request.user)) if message_type.code != 'DIRECT' else MessageTemplate.objects.none()
                products, blocks = _wizard_target_options(request, store, message_type)
                return render(request, 'linker/creator/auto_dm_wizard.html', {'step': 3, 'store': store, 'accounts': accounts, 'contents': contents, 'account': account, 'content': content, 'message_types': message_types, 'templates': templates, 'selected_type': selected_type, 'selected_template': None, 'products': products, 'blocks': blocks, 'state': state, 'allow_store_target': message_type.code in {'STORE_INFO', 'DIRECT'}})
            state.update({'step': 4, 'template_id': template.pk if template else None, 'target_type': target_type, 'target_id': target.pk, 'message_config': config, 'preview': preview})
            request.session[AUTO_DM_WIZARD_SESSION_KEY] = state
            request.session.modified = True
            return redirect(f'{reverse("shoppinghub_auto_dm")}?step=4')
        elif step == 4:
            template = (
                MessageTemplate.objects
                .filter(pk=state.get('template_id'), active=True)
                .filter(
                    Q(owner_type=MessageTemplate.OwnerType.SYSTEM)
                    | Q(
                        owner_type=MessageTemplate.OwnerType.CREATOR,
                        creator=request.user,
                    )
                )
                .first()
                if state.get('template_id')
                else None
            )

            target_type = state.get(
                'target_type',
                ResponseRule.TargetType.STORE,
            )

            target = _wizard_store_target(
                request,
                store,
                target_type,
                state.get('target_id', store.pk),
            )

            config = state.get('message_config', {})

            if template:
                validate_message_config(
                    template.message_type,
                    config,
                )

            # ------------------------------------------
            # Edit or Create
            # ------------------------------------------
            edit_rule_id = state.get('edit_rule_id')

            edit_rule = None
            if edit_rule_id:
                edit_rule = get_object_or_404(
                    ResponseRule,
                    pk=edit_rule_id,
                    store=store,
                )

            # social_content is OneToOne.
            # Editing the current rule is allowed,
            # but another rule may not own the selected content.
            conflict_qs = ResponseRule.objects.filter(
                social_content=content
            )

            if edit_rule:
                conflict_qs = conflict_qs.exclude(pk=edit_rule.pk)

            if conflict_qs.exists():
                messages.warning(
                    request,
                    '이 게시물에는 이미 다른 자동 DM이 설정되어 있습니다.'
                )
                state['step'] = 1
                request.session[AUTO_DM_WIZARD_SESSION_KEY] = state
                request.session.modified = True
                return redirect(
                    f'{reverse("shoppinghub_auto_dm")}?step=1'
                )

            starts_at = (
                parse_datetime(state['starts_at'])
                if state.get('starts_at')
                else None
            )
            ends_at = (
                parse_datetime(state['ends_at'])
                if state.get('ends_at')
                else None
            )

            if starts_at and timezone.is_naive(starts_at):
                starts_at = timezone.make_aware(starts_at)

            if ends_at and timezone.is_naive(ends_at):
                ends_at = timezone.make_aware(ends_at)

            if edit_rule:
                rule = edit_rule

                rule.scope = ResponseRule.Scope.CONTENT
                rule.store = store
                rule.social_account = account
                rule.social_content = content
                rule.response_mode = state.get('mode', 'ALL')
                rule.private_reply_enabled = True

                rule.target_type = target_type
                rule.target_linker = (
                    target
                    if target_type == ResponseRule.TargetType.PRODUCT
                    else None
                )
                rule.target_product_block = (
                    target
                    if target_type == ResponseRule.TargetType.PRODUCT_BLOCK
                    else None
                )
                rule.target_store = store

                rule.message_template = template
                rule.message_config = config
                rule.rendered_message_snapshot = state.get('preview', '')
                rule.test_private_reply_text = state.get('preview', '')
                rule.starts_at = starts_at
                rule.ends_at = ends_at

                rule.save()

                # 현재 폼의 키워드 세트로 완전히 교체
                rule.keywords.all().delete()

                was_updated = True

            else:
                rule = ResponseRule.objects.create(
                    scope=ResponseRule.Scope.CONTENT,
                    store=store,
                    social_account=account,
                    social_content=content,
                    response_mode=state.get('mode', 'ALL'),
                    private_reply_enabled=True,
                    target_type=target_type,
                    target_linker=(
                        target
                        if target_type == ResponseRule.TargetType.PRODUCT
                        else None
                    ),
                    target_product_block=(
                        target
                        if target_type == ResponseRule.TargetType.PRODUCT_BLOCK
                        else None
                    ),
                    target_store=store,
                    message_template=template,
                    message_config=config,
                    rendered_message_snapshot=state.get('preview', ''),
                    test_private_reply_text=state.get('preview', ''),
                    starts_at=starts_at,
                    ends_at=ends_at,
                )

                was_updated = False

            includes = [
                value.strip()
                for value in state.get('include_keywords', '').split(',')
                if value.strip()
            ]

            excludes = [
                value.strip()
                for value in state.get('exclude_keywords', '').split(',')
                if value.strip()
            ]

            for keyword in includes:
                ResponseKeyword.objects.create(
                    response_rule=rule,
                    keyword=keyword,
                    match_type=state.get('match_type', 'CONTAINS'),
                    is_exclusion=False,
                )

            for keyword in excludes:
                ResponseKeyword.objects.create(
                    response_rule=rule,
                    keyword=keyword,
                    match_type=state.get('match_type', 'CONTAINS'),
                    is_exclusion=True,
                )

            request.session.pop(AUTO_DM_WIZARD_SESSION_KEY, None)

            if was_updated:
                messages.success(request, '자동 DM을 수정했습니다.')
                return redirect(
                    f'{reverse("shoppinghub_auto_dm")}'
                    f'?saved={rule.pk}&updated=1'
                )

            messages.success(request, '자동 DM을 시작했습니다.')
            return redirect(
                f'{reverse("shoppinghub_auto_dm")}?saved={rule.pk}'
            )

    message_types = MessageType.objects.filter(active=True).order_by('display_order', 'id')
    selected_type = MessageType.objects.filter(pk=state.get('message_type_id'), active=True).first()
    templates = MessageTemplate.objects.filter(active=True, message_type=selected_type).filter(Q(owner_type=MessageTemplate.OwnerType.SYSTEM) | Q(owner_type=MessageTemplate.OwnerType.CREATOR, creator=request.user)) if selected_type else MessageTemplate.objects.none()
    products = Linker.objects.filter(owner=request.user, store=store, status='ACTIVE')
    blocks = ProductBlock.objects.filter(owner=request.user, store=store, active=True)
    if selected_type:
        products, blocks = _wizard_target_options(request, store, selected_type)
    selected_template = templates.filter(pk=state.get('template_id')).first()

    message_fields = []
    if selected_type:
        saved_config = state.get('message_config') or {}
        for field in selected_type.input_schema.get('fields', []):
            item = dict(field)
            item['value'] = saved_config.get(field.get('key'), '')
            message_fields.append(item)

    current_rules = (
        ResponseRule.objects
        .filter(store=store)
        .select_related(
            'social_content',
            'social_account',
            'target_linker',
            'target_product_block',
            'target_store',
            'message_template',
        )
        .prefetch_related('keywords')
        .order_by('-created_at', '-id')
    )

    return render(request, 'linker/creator/auto_dm_wizard.html', {
        'step': step,
        'store': store,
        'accounts': accounts,
        'contents': contents,
        'account': account,
        'content': content,
        'message_types': message_types,
        'templates': templates,
        'selected_type': selected_type,
        'selected_template': selected_template,
        'message_fields': message_fields,
        'products': products,
        'blocks': blocks,
        'state': state,
        'saved_rule': saved_rule,
        'current_rules': current_rules,
        'allow_store_target': selected_type and selected_type.code in {'STORE_INFO', 'DIRECT'},
    })

@login_required
def store_setup(request):
    store = CreatorStore.objects.filter(owner=request.user).first()
    form = CreatorStoreForm(request.POST or None, request.FILES or None, instance=store)
    if request.method == 'POST' and form.errors:
        if 'slug' in form.errors:
            messages.error(request, '이미 사용 중이거나 사용할 수 없는 Store 주소예요. 다른 주소를 선택해 주세요.')
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.owner = request.user
        obj.save()
        return redirect('creator_store_ready')
    return render(request, 'linker/creator/store_setup.html', {'form': form, 'store': store, 'accounts': SocialAccount.objects.filter(owner=request.user)})

@login_required
def store_ready(request):
    store = get_object_or_404(CreatorStore, owner=request.user)
    return render(request, 'linker/creator/store_ready.html', {'store': store})


@login_required
def social_accounts(request):
    store = get_active_store(request)
    if not store:
        return redirect('creator_onboarding_start')
    form = AccountOnboardingForm(request.POST or None)
    confirm_account = None
    confirm_action = ''
    if request.method == 'POST' and form.is_valid():
        try:
            account = create_social_account(owner=request.user, store=store, **form.cleaned_data)
            if account.platform == SocialAccount.Platform.INSTAGRAM and not CreatorProfile.objects.filter(user=request.user, representative_social_account__platform=SocialAccount.Platform.INSTAGRAM).exists():
                _set_primary_instagram(request.user, account)
        except DuplicateSocialAccountError:
            form.add_error(None, '이미 연결된 SNS 계정입니다. 연결 정보를 확인해 주세요.')
        except ValueError as exc:
            form.add_error(None, str(exc))
        except IntegrityError:
            form.add_error(None, '이미 등록된 SNS 계정입니다.')
        else:
            messages.success(request, 'SNS 계정을 등록했습니다.')
            return redirect('creator_channels')
    if request.method == 'POST' and request.POST.get('action') in {'assign', 'unassign'}:
        account = get_object_or_404(SocialAccount, owner=request.user, pk=request.POST.get('account_id'))
        action = request.POST['action']
        confirmed = request.POST.get('confirm') == '1'
        if action == 'assign' and account.store_id and account.store_id != store.pk and not confirmed:
            confirm_account, confirm_action = account, action
        elif action == 'unassign' and not confirmed:
            confirm_account, confirm_action = account, action
        else:
            try:
                if action == 'assign':
                    if account.store_id and account.store_id != store.pk:
                        move_social_account(request.user, account, store)
                    else:
                        assign_social_accounts_to_store(request.user, store, [account.pk])
                    messages.success(request, f'{account.account_name} 계정을 {store.store_name} Store에 배정했습니다.')
                else:
                    unassign_social_account(request.user, account)
                    messages.success(request, 'Store에서 SNS를 제거했습니다. SNS 연결은 유지됩니다.')
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                return redirect('creator_channels')
    accounts = SocialAccount.objects.filter(
        owner=request.user,
        store=store,
    ).select_related('credential').order_by('platform', '-connection_status', 'id')
    unassigned_accounts = SocialAccount.objects.filter(
        owner=request.user,
        store__isnull=True,
        platform__in=[SocialAccount.Platform.INSTAGRAM, SocialAccount.Platform.YOUTUBE],
        status=SocialAccount.Status.ACTIVE,
    ).select_related('credential').order_by('platform', 'id')
    other_accounts = SocialAccount.objects.filter(
        owner=request.user,
        store__isnull=False,
    ).exclude(store=store).select_related('store', 'credential').order_by('store_id', 'platform', 'id')
    profile = CreatorProfile.objects.filter(user=request.user).first()
    return render(request, 'linker/creator/accounts.html', {
        'accounts': accounts,
        'current_accounts': accounts,
        'unassigned_accounts': unassigned_accounts,
        'other_accounts': other_accounts,
        'confirm_account': confirm_account,
        'confirm_action': confirm_action,
        'store': store,
        'form': form,
        'primary_account_id': profile.representative_social_account_id if profile and profile.representative_social_account and profile.representative_social_account.platform == SocialAccount.Platform.INSTAGRAM else None,
    })


@login_required
@require_POST
def youtube_disconnect(request, pk):
    account = get_object_or_404(SocialAccount, owner=request.user, pk=pk, platform=SocialAccount.Platform.YOUTUBE)
    if account.youtube_credential:
        youtube_revoke_credential(account.youtube_credential)
        account.connection_status = SocialAccount.ConnectionStatus.NOT_CONNECTED
        account.save(update_fields=['connection_status', 'updated_at'])
        messages.success(request, 'YouTube 연결을 해제했습니다. 계정과 콘텐츠는 유지됩니다.')
    return redirect('creator_channels')


@login_required
@require_POST
def instagram_sync(request, pk):
    store = get_active_store(request)
    account = get_object_or_404(
        SocialAccount.objects.filter(
            owner=request.user,
            store=store,
            platform=SocialAccount.Platform.INSTAGRAM,
            status=SocialAccount.Status.ACTIVE,
        ).select_related('credential'),
        pk=pk,
    )
    try:
        synced = sync_instagram_contents(account, limit=50)
    except InstagramContentError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f'Instagram 콘텐츠 {len(synced)}개를 동기화했습니다.')
    return redirect('creator_channels')


@login_required
@require_POST
def creator_set_primary(request, pk):
    account = get_object_or_404(
        SocialAccount.objects.filter(owner=request.user, store=get_active_store(request)),
        pk=pk,
        platform=SocialAccount.Platform.INSTAGRAM,
    )
    _set_primary_instagram(request.user, account)
    messages.success(request, f'@{account.username or account.account_name}을(를) 대표 Instagram으로 설정했습니다.')
    return redirect('creator_channels')

@login_required
def youtube_auto_connect(request):
    return_to = request.GET.get('return') if request.GET.get('return') == 'onboarding_profile' else ''
    logger.info('YouTube creator OAuth started: owner_present=%s flow=%s', bool(request.user.pk), 'onboarding' if return_to else 'channel')
    try:
        url, state, verifier = youtube_authorization_url(settings.GOOGLE_YOUTUBE_AUTO_REDIRECT_URI)
    except YouTubeOAuthError as exc:
        messages.error(request, str(exc))
        return redirect('creator_channels')
    store = get_active_store(request)
    request.session['youtube_auto_oauth'] = {'state': state, 'code_verifier': verifier, 'flow': 'auto_onboarding' if return_to else 'channel', 'store_id': store.pk if store else None, 'return_to': return_to}
    return redirect(url)

@login_required
def youtube_auto_callback(request):
    context = request.session.get('youtube_auto_oauth')
    if not context or not secrets.compare_digest(request.GET.get('state', ''), context.get('state', '')):
        messages.error(request, 'YouTube OAuth 인증 상태가 유효하지 않습니다.')
        return _oauth_return(request, context or {})
    if request.GET.get('error'):
        messages.error(request, 'YouTube 인증이 취소되었거나 실패했습니다.')
        return _oauth_return(request, context)
    try:
        credentials = youtube_exchange_code(request.GET.get('code', ''), context.get('code_verifier', ''), settings.GOOGLE_YOUTUBE_AUTO_REDIRECT_URI)
        channel = youtube_verify_channel(credentials)
        account = _existing_owned_account(request.user, 'YOUTUBE', channel['id'])
        if not account:
            store = get_object_or_404(CreatorStore, owner=request.user, pk=context.get('store_id')) if context.get('store_id') else get_active_store(request)
            account = SocialAccount.objects.create(owner=request.user, store=store, platform='YOUTUBE', external_account_id=channel['id'], platform_user_id=channel['id'], account_name=channel['title'], display_name=channel['title'])
        elif not account.store_id and context.get('store_id'):
            store = get_object_or_404(CreatorStore, owner=request.user, pk=context['store_id'])
            account.store = store
            account.save(update_fields=['store', 'updated_at'])
        youtube_save_verified_credential(account, credentials, channel)
        request.session.pop('youtube_auto_oauth', None)
    except ValueError as exc:
        messages.error(request, str(exc))
    except YouTubeOAuthError as exc:
        messages.error(request, str(exc))
    except Exception as exc:
        logger.error('YouTube auto onboarding failed: type=%s message=connection_failed', type(exc).__name__)
        messages.error(request, 'YouTube 계정 연결에 실패했습니다. 잠시 후 다시 시도해 주세요.')
    else:
        messages.success(request, 'YouTube 계정을 자동으로 등록하고 연결했습니다.')
    return _oauth_return(request, context)

@login_required
def instagram_auto_connect(request):
    return_to = request.GET.get('return') if request.GET.get('return') == 'onboarding_profile' else ''
    logger.info('Instagram creator OAuth started: owner_present=%s flow=%s', bool(request.user.pk), 'onboarding' if return_to else 'channel')
    try:
        url, state = instagram_authorization_url(settings.META_INSTAGRAM_AUTO_REDIRECT_URI, force_reauth=True)
    except InstagramOAuthError as exc:
        messages.error(request, str(exc))
        return redirect('creator_channels')
    store = get_active_store(request)
    request.session['instagram_auto_oauth'] = {'state': state, 'flow': 'auto_onboarding' if return_to else 'channel', 'store_id': store.pk if store else None, 'return_to': return_to}
    return redirect(url)

@login_required
def instagram_auto_callback(request):
    context = request.session.get('instagram_auto_oauth')
    if not context or not secrets.compare_digest(request.GET.get('state', ''), context.get('state', '')):
        messages.error(request, 'Instagram OAuth 인증 상태가 유효하지 않습니다.')
        return _oauth_return(request, context or {})
    if request.GET.get('error'):
        messages.error(request, 'Instagram 인증이 취소되었거나 실패했습니다.')
        return _oauth_return(request, context)
    stage = 'OAuth code received'
    profile = {}
    try:
        logger.info('Instagram callback stage succeeded: stage=%s', stage)
        stage = 'short-lived token exchange'
        token_data = instagram_exchange_code(request.GET.get('code', ''), settings.META_INSTAGRAM_AUTO_REDIRECT_URI)
        logger.info('Instagram callback stage succeeded: stage=%s', stage)
        stage = 'long-lived token exchange'
        logger.info('Instagram callback stage succeeded: stage=%s', stage)
        stage = '/me lookup'
        profile = instagram_verify_account(token_data['access_token'])
        logger.info('Instagram callback stage succeeded: stage=%s', stage)
        stage = 'Store context lookup'
        store = get_object_or_404(CreatorStore, owner=request.user, pk=context.get('store_id')) if context.get('store_id') else CreatorStore.objects.filter(owner=request.user).first()
        logger.info('Instagram callback stage succeeded: stage=%s store_id=%s', stage, store.pk if store else None)
        stage = 'platform_user_id and username acquired'
        logical_key = f"platform=INSTAGRAM platform_user_id={profile.get('user_id')} username={profile.get('username')}"
        logger.info('Instagram callback stage succeeded: stage=%s logical_key=%s', stage, logical_key)
        with transaction.atomic():
            stage = 'SocialAccount lookup'
            account = _existing_owned_account(request.user, 'INSTAGRAM', profile['user_id'])
            logger.info('Instagram callback stage succeeded: stage=%s account_id=%s', stage, account.pk if account else None)
            if not account:
                stage = 'SocialAccount create'
                account = SocialAccount.objects.create(owner=request.user, store=store, platform='INSTAGRAM', external_account_id=profile['user_id'], platform_user_id=profile['user_id'], username=profile['username'], account_name=profile['username'], display_name=profile.get('name') or profile['username'], connection_status=SocialAccount.ConnectionStatus.CONNECTING)
            else:
                stage = 'SocialAccount update'
                if not account.store_id and store:
                    account.store = store
                account.save(update_fields=['store', 'updated_at'])
            logger.info('Instagram callback stage succeeded: stage=%s account_id=%s', stage, account.pk)
            stage = 'SocialCredential create/update'
            instagram_save_credential(account, token_data, profile)
            logger.info('Instagram callback stage succeeded: stage=%s account_id=%s', stage, account.pk)
            stage = 'representative account processing'
            if not CreatorProfile.objects.filter(user=request.user, representative_social_account__platform=SocialAccount.Platform.INSTAGRAM).exists():
                _set_primary_instagram(request.user, account)
            logger.info('Instagram callback stage succeeded: stage=%s account_id=%s', stage, account.pk)
            stage = 'CONNECTED state persisted'
            logger.info('Instagram callback stage succeeded: stage=%s account_id=%s', stage, account.pk)
        request.session.pop('instagram_auto_oauth', None)
    except ValueError as exc:
        messages.error(request, str(exc))
    except InstagramOAuthError as exc:
        messages.error(request, str(exc))
    except IntegrityError as exc:
        logical_key = f"platform=INSTAGRAM platform_user_id={profile.get('user_id')} username={profile.get('username')}"
        _log_integrity_error(logger, exc, stage=stage, logical_key=logical_key, model='linker.SocialAccount/SocialCredential')
        messages.error(request, '이미 연결된 Instagram 계정이거나 연결 정보를 저장할 수 없습니다. 기존 연결을 갱신해 주세요.')
    except Exception as exc:
        logger.exception('Instagram auto onboarding failed: type=%s stage=%s', type(exc).__name__, stage)
        messages.error(request, 'Instagram 계정 연결에 실패했습니다. 잠시 후 다시 시도해 주세요.')
    else:
        messages.success(request, 'Instagram 계정을 자동으로 등록하고 연결했습니다.')
    return _oauth_return(request, context)

@login_required
def instagram_connect(request, pk):
    account = get_object_or_404(SocialAccount, pk=pk, owner=request.user, platform='INSTAGRAM')
    logger.info('Instagram OAuth started: account_present=%s credential_present=%s', bool(account), bool(getattr(account, 'credential', None)))
    try:
        url, state = instagram_authorization_url()
    except InstagramOAuthError as exc:
        messages.error(request, str(exc))
        return redirect('creator_channels')
    request.session['instagram_oauth'] = {'state': state, 'account_id': account.pk, 'flow': 'new'}
    return redirect(url)

@login_required
def instagram_reauthorize(request, pk):
    account = get_object_or_404(SocialAccount, pk=pk, owner=request.user, platform='INSTAGRAM')
    logger.info('Instagram OAuth reauthorization started: account_present=%s credential_present=%s', bool(account), bool(getattr(account, 'credential', None)))
    try:
        url, state = instagram_authorization_url()
    except InstagramOAuthError as exc:
        messages.error(request, str(exc))
        return redirect('creator_channels')
    request.session['instagram_oauth'] = {'state': state, 'account_id': account.pk, 'flow': 'reauthorize'}
    return redirect(url)

@login_required
def instagram_callback(request):
    oauth_data = request.session.get('instagram_oauth')
    logger.info('Instagram OAuth callback received: state_present=%s code_present=%s error_present=%s', bool(request.GET.get('state')), bool(request.GET.get('code')), bool(request.GET.get('error')))
    if not oauth_data or not secrets.compare_digest(request.GET.get('state', ''), oauth_data.get('state', '')):
        messages.error(request, 'Instagram OAuth 인증 상태가 유효하지 않습니다.')
        return redirect('creator_channels')
    if request.GET.get('error'):
        messages.error(request, 'Instagram 인증이 취소되었거나 실패했습니다.')
        return redirect('creator_channels')
    account = get_object_or_404(SocialAccount, pk=oauth_data['account_id'], owner=request.user, platform='INSTAGRAM')
    try:
        logger.info('Instagram token exchange started: account_id=%s', account.pk)
        token_data = instagram_exchange_code(request.GET.get('code', ''))
        logger.info('Instagram token exchange succeeded: access_token_present=%s', bool(token_data.get('access_token')))
        logger.info('Instagram account verification started: account_id=%s', account.pk)
        profile = instagram_verify_account(token_data['access_token'])
        logger.info('Instagram account verification succeeded: account_id_present=%s username_present=%s', bool(profile.get('user_id')), bool(profile.get('username')))
        instagram_save_credential(account, token_data, profile)
        if not CreatorProfile.objects.filter(user=request.user, representative_social_account__platform=SocialAccount.Platform.INSTAGRAM).exists():
            _set_primary_instagram(request.user, account)
        logger.info('Instagram credential persistence succeeded: account_id=%s', account.pk)
        request.session.pop('instagram_oauth', None)
    except InstagramOAuthError as exc:
        if exc.account_name and exc.username:
            messages.error(request, f'{str(exc)} 계정: {exc.account_name}. 선택된 Instagram: @{exc.username}. 다시 연결해 주세요.')
        else:
            messages.error(request, str(exc))
    except Exception as exc:
        logger.error('Instagram OAuth unexpected failure: type=%s message=connection_failed', type(exc).__name__)
        messages.error(request, 'Instagram 계정 연결에 실패했습니다. 잠시 후 다시 시도해 주세요.')
    else:
        messages.success(request, 'Instagram 계정을 확인하고 연결했습니다.')
    return redirect('creator_channels')

@login_required
def instagram_verify(request, pk):
    account = get_object_or_404(SocialAccount, pk=pk, owner=request.user, platform='INSTAGRAM')
    try:
        token = instagram_credentials_for_account(account)
        profile = instagram_verify_account(token)
        instagram_save_credential(account, {'access_token': token, 'scopes': account.credential.scopes}, profile)
    except InstagramOAuthError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Instagram 연결을 확인했습니다.')
    return redirect('creator_channels')

@login_required
def instagram_disconnect(request, pk):
    account = get_object_or_404(SocialAccount, pk=pk, owner=request.user, platform='INSTAGRAM')
    if request.method == 'POST' and getattr(account, 'credential', None) and account.credential.provider == 'META_INSTAGRAM':
        instagram_revoke_credential(account.credential)
        account.connection_status = SocialAccount.ConnectionStatus.NOT_CONNECTED
        account.save(update_fields=['connection_status', 'updated_at'])
        _promote_primary_if_needed(request.user, account)
        messages.success(request, 'Instagram 연결을 해제했습니다. 계정과 콘텐츠는 유지됩니다.')
    return redirect('creator_channels')


@login_required
def creator_contents(request):
    store = get_active_store(request)
    if not store:
        return redirect('creator_onboarding_start')
    accounts = SocialAccount.objects.filter(
        owner=request.user,
        store=store,
    ).select_related('credential').order_by('platform', 'id')

    if request.method == 'POST' and request.POST.get('action') == 'sync_youtube':
        account = get_object_or_404(
            accounts,
            pk=request.POST.get('account_id'),
            platform=SocialAccount.Platform.YOUTUBE,
            status=SocialAccount.Status.ACTIVE,
        )

        try:
            synced = sync_youtube_contents(account, limit=50)
        except YouTubeReadError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f'YouTube 콘텐츠 {len(synced)}개를 동기화했습니다.'
            )

        return redirect('shoppinghub_contents')

    youtube_accounts = accounts.filter(
        platform=SocialAccount.Platform.YOUTUBE,
        status=SocialAccount.Status.ACTIVE,
    )

    platform = request.GET.get('platform', '').upper()
    if platform not in {SocialAccount.Platform.INSTAGRAM, SocialAccount.Platform.YOUTUBE}:
        platform = ''
    scoped_accounts = accounts.filter(platform=platform) if platform else accounts
    account_id = request.GET.get('account', '')
    if account_id.isdigit() and scoped_accounts.filter(pk=account_id).exists():
        selected_account = int(account_id)
    else:
        selected_account = ''
    items = SocialContent.objects.filter(social_account_id__in=scoped_accounts.values('pk'))
    if selected_account:
        items = items.filter(social_account_id=selected_account)
    items = items.select_related('social_account').prefetch_related('product_links__linker__marketplace').order_by('-published_at', '-id')
    return render(request, 'linker/creator/contents.html', {'items': items, 'accounts': scoped_accounts, 'selected_account': selected_account, 'selected_platform': platform, 'store': store, 'youtube_accounts': youtube_accounts})


@login_required
def creator_content_detail(request, pk):
    store = get_active_store(request)
    content = get_object_or_404(SocialContent.objects.filter(social_account__owner=request.user, social_account__store=store), pk=pk)
    return render(request, 'linker/creator/content_detail.html', {'content': content, 'comments': content.comments.order_by('-received_at')[:100]})


@login_required
def creator_link_add(request, pk):
    store = get_active_store(request)
    content = get_object_or_404(SocialContent.objects.filter(social_account__owner=request.user, social_account__store=store), pk=pk)
    form = SocialContentLinkForm(request.POST or None)
    form.fields['linker'].queryset = Linker.objects.filter(owner=request.user, status='ACTIVE').select_related('marketplace')
    if request.method == 'POST' and form.is_valid():
        link = form.save(commit=False)
        link.social_content = content
        link.display_order = 0
        try:
            link.save()
        except IntegrityError:
            form.add_error('linker', '이미 연결된 상품입니다.')
        else:
            messages.success(request, '상품을 연결했습니다.')
            return redirect('shoppinghub_content_detail', pk=content.pk)
    return render(request, 'linker/creator/link_add.html', {'content': content, 'form': form})


@login_required
def creator_products(request):
    store = get_active_store(request)
    if not store:
        return redirect('creator_onboarding_start')
    ensure_default_store_categories(request.user, store)
    migration_preview = _migration_preview(request.user, request, store)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action in {'product_copy', 'product_move', 'product_hide', 'product_delete', 'block_delete'}:
            try:
                if action == 'product_copy':
                    _, target = copy_product_to_store(
                        request.POST.get('item_id'), request.user, store, request.POST.get('target_store_id')
                    )
                    messages.success(request, f'{target.store_name} 스토어에도 상품을 추가했습니다.')
                elif action == 'product_move':
                    _, target, _ = move_product_to_store(
                        request.POST.get('item_id'), request.user, store, request.POST.get('target_store_id')
                    )
                    messages.success(request, f'{target.store_name} 스토어로 상품을 이동했습니다.')
                elif action == 'product_hide':
                    visible = request.POST.get('visible') == '1'
                    hide_product(request.POST.get('item_id'), request.user, store, visible=visible)
                    messages.success(request, '상품을 다시 보이게 했습니다.' if visible else '상품을 숨겼습니다.')
                elif action == 'product_delete':
                    delete_product_safely(request.POST.get('item_id'), request.user, store)
                    messages.success(request, '상품을 삭제했습니다.')
                else:
                    delete_product_block_safely(request.POST.get('item_id'), request.user, store)
                    messages.success(request, '상품묶음을 삭제했습니다. 포함된 상품은 그대로 유지됩니다.')
            except ProductStoreManagementError as exc:
                messages.error(request, str(exc))
            return redirect('shoppinghub_products')
        if action in {'existing_service_scan', 'inpock_import'}:
            source_url = request.POST.get('existing_service_url') or request.POST.get('inpock_profile_url', '')
            try:
                drafts = scan_existing_service_product_drafts(source_url)
            except ProductDraftError as exc:
                _clear_migration_payload(request)
                messages.error(request, str(exc))
            else:
                request.session[SHOPPINGHUB_IMPORT_SESSION_KEY] = _migration_session_payload(request.user, store, source_url, drafts)
                request.session.modified = True
                migration_preview = _migration_preview(request.user, request, store)
                messages.success(request, f'이전 후보 {len(migration_preview)}개를 찾았습니다. 등록할 상품을 선택해 주세요.')
        elif action == 'existing_service_import':
            payload = _migration_payload(request, store)
            if payload is None:
                _clear_migration_payload(request)
                messages.error(request, '가져올 상품 목록이 만료되었습니다. 다시 상품 가져오기를 실행해 주세요.')
                return redirect('shoppinghub_products')
            selected = {int(value) for value in request.POST.getlist('selected_drafts') if value.isdigit()}
            if not selected:
                messages.error(request, '가져올 상품을 선택해 주세요.')
                migration_preview = _migration_preview(request.user, request, store)
            else:
                created = skipped = failed = image_failed = 0
                for index, item in enumerate(payload.get('drafts', [])):
                    if index not in selected or not isinstance(item, dict):
                        continue
                    draft = ProductDraft.from_dict(item)
                    if has_existing_product(request.user, draft, store=store):
                        skipped += 1
                        continue
                    try:
                        product = create_product_from_draft(request.user, draft, store=store)
                    except ProductDraftError:
                        failed += 1
                    else:
                        created += 1
                        image_failed += bool(getattr(product, 'image_copy_failed', False))
                _clear_migration_payload(request)
                message = f'등록 성공 {created}건 · 중복 제외 {skipped}건 · 실패 {failed}건'
                if image_failed:
                    message += f' · 이미지 저장 실패 {image_failed}건'
                messages.success(request, message)
                return redirect('shoppinghub_products')
        if action == 'category_create':
            name = request.POST.get('name', '').strip()
            if name:
                StoreCategory.objects.get_or_create(owner=request.user, store=store, name=name, defaults={'display_order': StoreCategory.objects.filter(owner=request.user, store=store).count()})
            return redirect('shoppinghub_display' if request.resolver_match.url_name == 'shoppinghub_display' else 'shoppinghub_products')
        if action == 'category_delete':
            category = get_object_or_404(StoreCategory, pk=request.POST.get('category_id'), owner=request.user, store=store)
            category.delete()
            return redirect('shoppinghub_products')
        if action in {'product_display', 'block_display'}:
            model = Linker if action == 'product_display' else ProductBlock
            item = get_object_or_404(model, pk=request.POST.get('item_id'), owner=request.user, store=store)
            category_id = request.POST.get('store_category') or None
            item.store_category = StoreCategory.objects.filter(pk=category_id, owner=request.user, store=store, active=True).first() if category_id else None
            item.store_visible = bool(request.POST.get('store_visible'))
            try:
                item.display_order = max(0, int(request.POST.get('display_order') or 0))
            except ValueError:
                item.display_order = 0
            item.save(update_fields=['store_category', 'store_visible', 'display_order', 'updated_at'])
            return redirect('shoppinghub_display' if request.resolver_match.url_name == 'shoppinghub_display' else 'shoppinghub_products')
    products = Linker.objects.filter(owner=request.user, status='ACTIVE').filter(_store_scope(store)).select_related('marketplace', 'store_category').prefetch_related('content_links').order_by('display_order', 'id')
    blocks = ProductBlock.objects.filter(owner=request.user, active=True).filter(_store_scope(store)).select_related('store_category').prefetch_related('items__linker').order_by('display_order', 'id')
    categories = ensure_default_store_categories(request.user, store)
    context = {
        'products': products,
        'blocks': blocks,
        'categories': categories,
        'store': store,
        'stores': CreatorStore.objects.filter(owner=request.user, active=True).exclude(pk=store.pk),
        'migration_preview': migration_preview,
    }
    template = 'linker/creator/display.html' if request.resolver_match.url_name == 'shoppinghub_display' else 'linker/creator/products.html'
    return render(request, template, context)


def _save_item_detail(item, cleaned_data):
    if item.item_type == Linker.ItemType.RECIPE:
        RecipeDetail.objects.update_or_create(linker_id=item.pk, defaults={
            'ingredients': cleaned_data.get('ingredients', ''),
            'instructions': cleaned_data.get('instructions', ''),
            'tips': cleaned_data.get('tips', ''),
            'servings': cleaned_data.get('servings', ''),
            'prep_time': cleaned_data.get('prep_time', ''),
            'cook_time': cleaned_data.get('cook_time', ''),
        })
    elif item.item_type == Linker.ItemType.SERVICE:
        ServiceDetail.objects.update_or_create(linker_id=item.pk, defaults={
            'service_description': cleaned_data.get('service_description', ''),
            'price_info': cleaned_data.get('price_info', ''),
            'duration': cleaned_data.get('duration', ''),
            'location_info': cleaned_data.get('location_info', ''),
            'contact_info': cleaned_data.get('contact_info', ''),
            'booking_url': cleaned_data.get('booking_url', ''),
        })


def _configure_typed_form(form):
    is_recipe = form.data.get('item_type') == Linker.ItemType.RECIPE if hasattr(form, 'data') else False
    form.fields['ingredients'].required = is_recipe
    form.fields['instructions'].required = is_recipe
    return form


@login_required
def creator_product_new(request):
    store = get_active_store(request)
    if not store:
        return redirect('creator_onboarding_start')
    ensure_default_store_categories(request.user, store)
    data = request.POST.copy() if request.method == 'POST' else None
    if data is not None and 'store_visible' not in data:
        data['store_visible'] = 'on'
    if data is not None and not data.get('display_order'):
        data['display_order'] = '0'
    form = ShoppingHubProductForm(data, request.FILES or None, owner=request.user, store=store)
    if request.method == 'POST' and form.is_valid():
        product = form.save(commit=False)
        product.owner = request.user
        product.store = store
        product.thumbnail_url = product.thumbnail_url or request.POST.get('thumbnail_url', '').strip()[:1000] or request.POST.get('imported_thumbnail_url', '').strip()[:1000]
        product.save()
        return redirect('shoppinghub_products')
    return render(request, 'linker/creator/product_form.html', {'form': form})


@login_required
def shoppinghub_product_new(request):
    store = get_active_store(request)
    if not store:
        return redirect('creator_onboarding_start')
    ensure_default_store_categories(request.user, store)
    state = request.session.get('shoppinghub_product_wizard', {})
    step = int(request.GET.get('step', state.get('step', 1)))
    if request.method == 'POST':
        post_data = request.POST.copy()
        if not post_data.get('item_type'):
            post_data['item_type'] = Linker.ItemType.PRODUCT
        if 'store_visible' not in post_data:
            post_data['store_visible'] = 'on'
        if not post_data.get('display_order'):
            post_data['display_order'] = '0'
        if post_data.get('action') == 'fetch_product_url':
            try:
                draft = fetch_product_page_draft(post_data.get('source_product_url') or post_data.get('destination_url', ''))
            except ProductDraftError as exc:
                messages.error(request, str(exc))
                form = TypedItemForm(post_data, request.FILES or None, owner=request.user, store=store)
            else:
                marketplace = Marketplace.objects.filter(code=draft.marketplace_code, active=True).first()
                initial = {**post_data.dict(), **draft.as_initial()}
                initial['destination_url'] = post_data.get('destination_url', '')
                state['imported_thumbnail_url'] = draft.thumbnail_url
                request.session['shoppinghub_product_wizard'] = state
                request.session.modified = True
                if marketplace:
                    initial['marketplace'] = marketplace.pk
                form = TypedItemForm(owner=request.user, store=store, initial=initial)
                messages.success(request, '상품페이지 URL에서 상품 draft를 가져왔습니다.')
            return render(request, 'linker/creator/shoppinghub_product_wizard.html', {'form': form, 'step': 1, 'state': {}, 'single_flow': True, 'store': store, 'source_product_url': post_data.get('source_product_url', ''), 'source_thumbnail_url': draft.thumbnail_url if 'draft' in locals() else ''})
        if post_data.get('flow') == 'single':
            form = TypedItemForm(post_data, request.FILES or None, owner=request.user, store=store)
            _configure_typed_form(form)
            form.fields['destination_url'].required = post_data.get('item_type') == Linker.ItemType.PRODUCT
            if form.is_valid():
                product = form.save(commit=False)
                product.owner = request.user
                product.store = store
                product.thumbnail_url = product.thumbnail_url or request.POST.get('thumbnail_url', '').strip()[:1000] or request.POST.get('imported_thumbnail_url', '').strip()[:1000]
                product.save()
                _save_item_detail(product, form.cleaned_data)
                request.session.pop('shoppinghub_product_wizard', None)
                return redirect('shoppinghub_products')
            return render(request, 'linker/creator/shoppinghub_product_wizard.html', {'form': form, 'step': 1, 'state': {}, 'single_flow': True, 'store': store})
        step = int(request.POST.get('step', step))
        form = TypedItemForm(post_data, request.FILES or None, owner=request.user, store=store)
        _configure_typed_form(form)
        for field in form.fields.values():
            field.required = False
        if step == 1:
            form.fields['product_no'].required = True
            form.fields['title'].required = True
        elif step == 3:
            form.fields['marketplace'].required = True
        elif step == 2:
            form.fields['product_image'].required = False
        elif step == 4:
            form.fields['destination_url'].required = True
        if form.is_valid():
            cleaned = {key: value for key, value in form.cleaned_data.items() if value not in (None, '')}
            if 'marketplace' in cleaned:
                cleaned['marketplace_id'] = cleaned.pop('marketplace').pk
            if 'store_category' in cleaned:
                cleaned['store_category_id'] = cleaned.pop('store_category').pk
            state.update(cleaned)
            if step < 5:
                state['step'] = step + 1
                request.session['shoppinghub_product_wizard'] = state
                return redirect(f'{reverse("shoppinghub_product_new")}?step={step + 1}')
            with transaction.atomic():
                state.pop('step', None)
                imported_thumbnail_url = state.pop('imported_thumbnail_url', '')
                product = Linker.objects.create(owner=request.user, store=store, status='ACTIVE', **state)
                _save_item_detail(product, state)
                if imported_thumbnail_url:
                    product.thumbnail_url = imported_thumbnail_url[:1000]
                    product.save(update_fields=['thumbnail_url', 'updated_at'])
            request.session.pop('shoppinghub_product_wizard', None)
            return redirect('shoppinghub_products')
    else:
        form = TypedItemForm(owner=request.user, store=store, initial=state)
    return render(request, 'linker/creator/shoppinghub_product_wizard.html', {'form': form, 'step': step, 'state': state, 'single_flow': True, 'store': store})


@login_required
def shoppinghub_product_edit(request, pk):
    store = get_active_store(request)
    if not store:
        return redirect('creator_onboarding_start')
    ensure_default_store_categories(request.user, store)
    product = get_object_or_404(Linker, pk=pk, owner=request.user, store=store)
    data = request.POST.copy() if request.method == 'POST' else None
    if data is not None and not data.get('item_type'):
        data['item_type'] = product.item_type
    if data is not None and 'store_visible' not in data:
        data['store_visible'] = 'on'
    if data is not None and not data.get('display_order'):
        data['display_order'] = str(product.display_order or 0)
    form = TypedItemForm(data, request.FILES or None, instance=product, owner=request.user, store=store)
    _configure_typed_form(form)
    if request.method == 'POST' and form.is_valid():
        product = form.save()
        _save_item_detail(product, form.cleaned_data)
        if not request.FILES.get('product_image') and request.POST.get('thumbnail_url'):
            product.thumbnail_url = request.POST.get('thumbnail_url', '').strip()[:1000]
            product.save(update_fields=['thumbnail_url', 'updated_at'])
        return redirect('shoppinghub_products')
    return render(request, 'linker/creator/product_form.html', {'form': form, 'product': product, 'store': store})


@login_required
def shoppinghub_block_new(request):
    store = get_active_store(request)
    if not store:
        return redirect('creator_onboarding_start')
    products = Linker.objects.filter(owner=request.user, status='ACTIVE').filter(_store_scope(store)).order_by('product_no', 'id')
    data = request.POST.copy() if request.method == 'POST' else None
    if data is not None and 'store_visible' not in data:
        data['store_visible'] = 'on'
    if data is not None and not data.get('display_order'):
        data['display_order'] = '0'
    form = ProductBlockForm(data, request.FILES or None, owner=request.user, store=store)
    if request.method == 'POST' and form.is_valid():
        selected_ids = request.POST.getlist('products')
        selected = [products.get(pk=product_id) for product_id in selected_ids if product_id.isdigit() and products.filter(pk=product_id).exists()]
        if not selected:
            form.add_error(None, '상품블록에 담을 상품을 하나 이상 선택해주세요.')
        else:
            with transaction.atomic():
                block = form.save(commit=False)
                block.owner = request.user
                block.store = store
                block.active = True
                block.save()
                ProductBlockItem.objects.bulk_create([
                    ProductBlockItem(product_block=block, linker=product, sort_order=index)
                    for index, product in enumerate(selected)
                ])
            return redirect('shoppinghub_products')
    return render(request, 'linker/creator/product_block_form.html', {'form': form, 'products': products, 'block': None, 'selected_ids': set(), 'is_edit': False, 'store': store})


@login_required
def shoppinghub_block_edit(request, pk):
    store = get_active_store(request)
    if not store:
        return redirect('creator_onboarding_start')
    block = get_object_or_404(ProductBlock.objects.filter(owner=request.user).filter(_store_scope(store)), pk=pk)
    products = Linker.objects.filter(owner=request.user, status='ACTIVE').filter(_store_scope(store)).order_by('product_no', 'id')
    data = request.POST.copy() if request.method == 'POST' else None
    if data is not None and 'store_visible' not in data:
        data['store_visible'] = 'on'
    if data is not None and not data.get('display_order'):
        data['display_order'] = str(block.display_order or 0)
    form = ProductBlockForm(data, request.FILES or None, instance=block, owner=request.user, store=store)
    if request.method == 'POST' and form.is_valid():
        selected_ids = request.POST.getlist('products')
        selected = [products.get(pk=product_id) for product_id in selected_ids if product_id.isdigit() and products.filter(pk=product_id).exists()]
        if not selected:
            form.add_error(None, '상품블록에 담을 상품을 하나 이상 선택해주세요.')
        else:
            with transaction.atomic():
                form.save()
                ProductBlockItem.objects.filter(product_block=block).delete()
                ProductBlockItem.objects.bulk_create([
                    ProductBlockItem(product_block=block, linker=product, sort_order=index)
                    for index, product in enumerate(selected)
                ])
            return redirect('shoppinghub_products')
    selected_ids = set(block.items.values_list('linker_id', flat=True))
    return render(request, 'linker/creator/product_block_form.html', {'form': form, 'products': products, 'selected_ids': selected_ids, 'block': block, 'is_edit': True, 'store': store})


def _shoppinghub_keyword_rows(request, owner, store):
    rows = []
    keywords = request.POST.getlist('keyword')
    target_types = request.POST.getlist('keyword_target_type')
    target_ids = request.POST.getlist('keyword_target_id')
    link_modes = request.POST.getlist('keyword_link_mode')
    direct_urls = request.POST.getlist('keyword_direct_url')
    include_link_stores = request.POST.getlist('keyword_include_link_store')
    for index, keyword in enumerate(keywords):
        keyword = keyword.strip().casefold()
        if not keyword:
            continue
        target_type = (target_types[index] if index < len(target_types) else '').strip()
        target_id = (target_ids[index] if index < len(target_ids) else '').strip()
        if target_type == ResponseRule.TargetType.STORE and not target_id and store:
            target_id = store.pk
        target = resolve_target(owner, target_type, target_id, store=store)
        link_mode = link_modes[index] if index < len(link_modes) else ResponseRule.LinkMode.LINK_STORE
        direct_url = direct_urls[index].strip() if index < len(direct_urls) else ''
        include_link_store = str(index) in include_link_stores
        if link_mode == ResponseRule.LinkMode.DIRECT:
            parsed_url = urlparse(direct_url)
            if target_type not in {ResponseRule.TargetType.PRODUCT, ResponseRule.TargetType.PRODUCT_BLOCK, ResponseRule.TargetType.STORE} or parsed_url.scheme not in {'http', 'https'} or not parsed_url.netloc:
                raise ValueError('invalid direct keyword target')
        elif link_mode != ResponseRule.LinkMode.LINK_STORE or direct_url:
            raise ValueError('invalid link store keyword target')
        rows.append((keyword, target_type, target, link_mode, direct_url, include_link_store))
    if len({row[0] for row in rows}) != len(rows):
        raise ValueError('duplicate keyword')
    return rows


def _dm_target_type(value):
    return {
        'product': ResponseRule.TargetType.PRODUCT,
        'product_block': ResponseRule.TargetType.PRODUCT_BLOCK,
        'store': ResponseRule.TargetType.STORE,
        ResponseRule.TargetType.PRODUCT: ResponseRule.TargetType.PRODUCT,
        ResponseRule.TargetType.PRODUCT_BLOCK: ResponseRule.TargetType.PRODUCT_BLOCK,
        ResponseRule.TargetType.STORE: ResponseRule.TargetType.STORE,
    }.get((value or '').strip())


def _dm_preselected_target(request):
    target_type = _dm_target_type(request.GET.get('target_type'))
    target_id = request.GET.get('target_id', '')
    if not target_type and not target_id:
        return None, None
    if not target_type or not target_id.isdigit():
        raise Http404
    try:
        return target_type, resolve_target(request.user, target_type, target_id, store=get_active_store(request))
    except (CreatorStore.DoesNotExist, Linker.DoesNotExist, ProductBlock.DoesNotExist):
        raise Http404


@login_required
def auto_dm_toggle(request, pk):
    rule = get_object_or_404(ResponseRule, pk=pk, store=get_active_store(request))
    if request.method == 'POST':
        rule.enabled = not rule.enabled
        rule.save(update_fields=['enabled', 'updated_at'])
    return redirect('shoppinghub_auto_dm')


@login_required
@require_POST
def auto_dm_delete(request, pk):
    rule = get_object_or_404(
        ResponseRule,
        pk=pk,
        store=get_active_store(request),
    )

    rule.delete()
    messages.success(request, '자동 DM을 삭제했습니다.')
    return redirect('shoppinghub_auto_dm')


@login_required
def creator_reactions(request):
    store = get_active_store(request)
    if not store:
        return redirect('creator_onboarding_start')
    if request.method == 'POST' and request.POST.get('action') == 'sync_comments':
        account = get_object_or_404(SocialAccount, owner=request.user, store=store, platform=SocialAccount.Platform.INSTAGRAM, pk=request.POST.get('account_id'))
        try:
            created = sync_instagram_comments(account)
        except InstagramCommentSyncError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f'새 댓글 {created}개를 확인했습니다.')
        return redirect('creator_reactions')
    accounts = SocialAccount.objects.filter(owner=request.user, store=store).order_by('platform', 'id')
    platform = request.GET.get('platform', '').upper()
    if platform not in {SocialAccount.Platform.INSTAGRAM, SocialAccount.Platform.YOUTUBE}:
        platform = ''
    scoped_accounts = accounts.filter(platform=platform) if platform else accounts
    account_id = request.GET.get('account', '')
    selected_account = int(account_id) if account_id.isdigit() and scoped_accounts.filter(pk=account_id).exists() else ''
    comments = SocialComment.objects.filter(social_content__social_account_id__in=scoped_accounts.values('pk'))
    if selected_account:
        comments = comments.filter(social_content__social_account_id=selected_account)
    comments = (
        comments
        .select_related('social_content__social_account')
        .prefetch_related('response_logs__response_rule')
        .order_by('-received_at')[:200]
    )

    return render(request, 'linker/creator/reactions.html', {
        'comments': comments,
        'accounts': scoped_accounts,
        'selected_account': selected_account,
        'selected_platform': platform,
        'store': store,
    })


@login_required
def creator_store(request):
    store = get_active_store(request)
    if not store:
        return redirect('creator_onboarding_start')
    products = Linker.objects.filter(owner=request.user, store=store, status='ACTIVE').select_related('marketplace')
    return render(request, 'linker/creator/store.html', {'products': products, 'store': store, 'preview': request.GET.get('preview') == '1'})

def public_store(request, slug):
    store = get_object_or_404(CreatorStore.objects.filter(active=True), slug=slug)
    products = Linker.objects.filter(status='ACTIVE', store_visible=True).filter(_store_scope(store)).select_related('marketplace', 'store_category')
    item_id = request.GET.get('item', '')
    if item_id.isdigit():
        item = products.select_related('recipe_detail', 'service_detail').filter(pk=item_id).first()
        if item:
            return render(request, 'linker/creator/typed_item_detail.html', {'store': store, 'item': item})
    first_store_id = CreatorStore.objects.filter(owner=store.owner).order_by('created_at', 'id').values_list('pk', flat=True).first()
    linker_scope = Q(linker__store=store) | Q(linker__store__isnull=True) if store.pk == first_store_id else Q(linker__store=store)
    public_block_items = ProductBlockItem.objects.filter(
        linker_scope,
        linker__status='ACTIVE',
        linker__store_visible=True,
    ).select_related('linker').order_by('sort_order', 'id')
    blocks = ProductBlock.objects.filter(active=True, store_visible=True).filter(_store_scope(store)).select_related('store_category').prefetch_related(Prefetch('items', queryset=public_block_items)).order_by('display_order', 'id')
    categories = StoreCategory.objects.filter(active=True).filter(_store_scope(store)).order_by('display_order', 'id')
    public_category_ids = set(products.values_list('store_category_id', flat=True)) | set(blocks.values_list('store_category_id', flat=True))
    categories = categories.filter(pk__in=[category_id for category_id in public_category_ids if category_id])
    socials = SocialAccount.objects.filter(
        owner=store.owner,
        store=store,
        status=SocialAccount.Status.ACTIVE,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        platform__in=[SocialAccount.Platform.INSTAGRAM, SocialAccount.Platform.YOUTUBE],
    )
    query = request.GET.get('q', '').strip()
    all_products = products
    if query:
        products = products.filter(Q(product_no__icontains=query) | Q(title__icontains=query) | Q(description__icontains=query)).distinct()
        blocks = blocks.filter(Q(block_number__icontains=query) | Q(title__icontains=query) | Q(description__icontains=query) | Q(items__linker__product_no__icontains=query) | Q(items__linker__title__icontains=query)).distinct()
    selected_category = None
    category_value = request.GET.get('category', '').strip()
    if category_value:
        selected_category = categories.filter(Q(pk=category_value) if category_value.isdigit() else Q(name__iexact=category_value)).first()
        if selected_category:
            products = products.filter(store_category=selected_category)
            blocks = blocks.filter(store_category=selected_category)
    products = products.order_by('-created_at', '-id')
    highlighted_product = None
    highlighted_block = None
    product_no = request.GET.get('product', '').strip()
    block_number = request.GET.get('block', '').strip().upper()
    if product_no:
        highlighted_product = products.filter(product_no__iexact=product_no).first()
        if highlighted_product and highlighted_product.item_type != Linker.ItemType.PRODUCT:
            return render(request, 'linker/creator/typed_item_detail.html', {'store': store, 'item': highlighted_product})
    if block_number:
        highlighted_block = blocks.filter(block_number__iexact=block_number).first()
    search_products = products[:10] if query else Linker.objects.none()
    store_products = products[:10] if not query else all_products[:10]
    import_products = products[:50] if not query else Linker.objects.none()
    remaining_products = all_products.exclude(pk__in=search_products.values('pk'))[:10] if query else Linker.objects.none()
    StoreEvent.objects.create(store=store, event_type=StoreEvent.EventType.STORE_VIEW)
    if highlighted_product:
        StoreEvent.objects.create(store=store, event_type=StoreEvent.EventType.PRODUCT_VIEW, linker=highlighted_product)
    if highlighted_block:
        StoreEvent.objects.create(store=store, event_type=StoreEvent.EventType.BLOCK_VIEW, product_block=highlighted_block)
    related_products = products.exclude(pk=highlighted_product.pk)[:6] if highlighted_product else products[:6]
    template_name = 'linker/creator/public_store_home.html' if not query and not highlighted_product and not highlighted_block else 'linker/creator/public_store.html'
    return render(request, template_name, {'store': store, 'products': products, 'store_products': store_products, 'import_products': import_products, 'search_products': search_products, 'remaining_products': remaining_products, 'blocks': blocks, 'categories': categories, 'socials': socials, 'selected_category': selected_category, 'highlighted_product': highlighted_product, 'highlighted_block': highlighted_block, 'related_products': related_products, 'query': query})


def public_store_outbound(request, slug, pk):
    store = get_object_or_404(CreatorStore.objects.filter(active=True), slug=slug)
    product = get_object_or_404(Linker.objects.filter(status='ACTIVE', store_visible=True).filter(_store_scope(store)), pk=pk)
    if not product.destination_url:
        return redirect(reverse('public_store', args=[store.slug]) + f'?item={product.pk}')
    StoreEvent.objects.create(store=store, event_type=StoreEvent.EventType.OUTBOUND_CLICK, linker=product)
    return redirect(product.destination_url)
