import json
import logging
import secrets
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from functools import wraps
from django.db.models import Q
from django.db import IntegrityError
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from .models import SocialComment, SocialContent, Linker, SocialAccount, ResponseRule, ResponseLog, SocialContentLink, Marketplace
from .forms import LinkerForm, SocialAccountForm, SocialContentForm, ResponseRuleForm, AccountOnboardingForm, SocialContentLinkForm, MarketplaceForm
from .services.account_onboarding import DuplicateSocialAccountError, create_social_account
from .services.youtube_oauth import YouTubeOAuthError, authorization_url, exchange_code, save_verified_credential, verify_channel, credentials_for_account, revoke_credential
from .services.youtube_content import YouTubeReadError, list_uploads, register_selected_videos
from .services.youtube_comments import sync_comments
from .tasks import process_comment
from .services.comment_intake import upsert_comment

logger = logging.getLogger(__name__)


def _safe_oauth_error_detail(exc):
    error_code = getattr(exc, 'error', '') or type(exc).__name__
    description = getattr(exc, 'description', '') or 'provider rejected the authorization grant'
    allowed = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._- ')
    safe_code = ''.join(char for char in str(error_code) if char in allowed)[:80]
    safe_description = ''.join(char for char in str(description) if char in allowed)[:160]
    return safe_code, safe_description

def operator_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), '/admin/login/')
        if not request.user.is_staff:
            return HttpResponseForbidden('Staff access required.')
        return view_func(request, *args, **kwargs)
    return wrapped


@operator_required
def dashboard(request):
    return render(request,'linker/dashboard.html',{'linkers':Linker.objects.count(),'contents':SocialContent.objects.count(),'comments':SocialComment.objects.count(),'pending':SocialComment.objects.filter(processing_status='PENDING').count(),'failed':SocialComment.objects.filter(processing_status='FAILED').count(),'accounts':SocialAccount.objects.count(),'active_accounts':SocialAccount.objects.filter(status='ACTIVE').count(),'setup_accounts':SocialAccount.objects.filter(status='SETUP_REQUIRED').count()})

def privacy_policy(request):
    return render(request, "linker/privacy_policy.html")

def data_deletion(request):
    return render(request, "linker/data_deletion.html")

def _crud(request, model, form_cls, template, title):
    edit=request.GET.get('edit'); obj=get_object_or_404(model,pk=edit) if edit else None
    if request.method=='POST':
        obj=get_object_or_404(model,pk=request.POST.get('id')) if request.POST.get('id') else None
        form=form_cls(request.POST,instance=obj)
        if form.is_valid(): form.save(); messages.success(request,'저장했습니다.'); return redirect(request.path)
    else: form=form_cls(instance=obj)
    q=request.GET.get('q','').strip(); items=model.objects.all().order_by('-id')
    if q:
        if model is Linker: items=items.filter(Q(linker_code__icontains=q)|Q(title__icontains=q)|Q(description__icontains=q))
        elif model is SocialAccount: items=items.filter(Q(account_name__icontains=q)|Q(external_account_id__icontains=q))
        elif model is SocialContent: items=items.filter(Q(title__icontains=q)|Q(caption__icontains=q)|Q(external_content_id__icontains=q)|Q(linker__linker_code__icontains=q)|Q(linker__title__icontains=q)|Q(product_links__linker__linker_code__icontains=q)|Q(product_links__linker__title__icontains=q)).distinct()
    if model is SocialAccount:
        items = items.select_related('credential')
    if model is SocialContent:
        items = items.select_related('social_account', 'linker').prefetch_related('product_links__linker__marketplace')
    return render(request,template,{'form':form,'items':items[:300],'q':q,'edit_obj':obj,'page_title':title})

@operator_required
def linkers(request):
    edit = request.GET.get('edit')
    group = request.GET.get('group')
    obj = get_object_or_404(Linker, pk=edit) if edit else None
    form = LinkerForm(request.POST or None, instance=obj)
    if request.method == 'GET' and group and not obj:
        form.initial['linker_group_code'] = group
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Linker를 저장했습니다.')
        return redirect('linkers')
    query = request.GET.get('q', '').strip()
    destinations = Linker.objects.select_related('marketplace').prefetch_related('content_links').order_by('linker_group_code', 'destination_seq', 'id')
    if query:
        destinations = destinations.filter(Q(linker_code__icontains=query) | Q(linker_group_code__icontains=query) | Q(product_no__icontains=query) | Q(title__icontains=query) | Q(description__icontains=query) | Q(marketplace__code__icontains=query) | Q(marketplace__name__icontains=query))
    groups = []
    grouped = {}
    for linker in destinations:
        key = linker.linker_group_code or linker.linker_code
        grouped.setdefault(key, []).append(linker)
    for group_code, items in grouped.items():
        first = items[0]
        groups.append({'code': group_code, 'title': first.title, 'product_no': first.product_no, 'status': first.status, 'destinations': items, 'content_count': sum(item.content_links.count() for item in items)})
    return render(request, 'linker/linkers.html', {'form': form, 'items': groups, 'q': query, 'edit_obj': obj, 'page_title': '링커관리'})
@operator_required
def accounts(request): return _crud(request,SocialAccount,SocialAccountForm,'linker/accounts.html','계정관리')

def _account_for_request(request, **filters):
    if request.user.is_authenticated and not request.user.is_staff:
        filters['owner'] = request.user
    return get_object_or_404(SocialAccount, **filters)

@operator_required
def youtube_connect(request, pk):
    account = _account_for_request(request, pk=pk, platform=SocialAccount.Platform.YOUTUBE)
    logger.info('YouTube OAuth connection started: account_present=%s credential_present=%s flow=new', bool(account), bool(account.youtube_credential))
    try:
        url, state, code_verifier = authorization_url()
    except YouTubeOAuthError as exc:
        messages.error(request, str(exc))
        return redirect('accounts')
    request.session['youtube_oauth'] = {'state': state, 'account_id': account.pk, 'code_verifier': code_verifier, 'flow': 'new'}
    return redirect(url)

@operator_required
def youtube_reauthorize(request, pk):
    account = _account_for_request(request, pk=pk, platform=SocialAccount.Platform.YOUTUBE)
    logger.info('YouTube scope reauthorization started: account_present=%s credential_present=%s', bool(account), bool(account.youtube_credential))
    try:
        url, state, code_verifier = authorization_url()
    except YouTubeOAuthError as exc:
        messages.error(request, str(exc))
        return redirect('contents')
    request.session['youtube_oauth'] = {'state': state, 'account_id': account.pk, 'code_verifier': code_verifier, 'flow': 'reauthorize'}
    return redirect(url)

@operator_required
def youtube_callback(request):
    oauth_data = request.session.get('youtube_oauth')
    logger.info('YouTube OAuth callback received: state_present=%s code_present=%s error_present=%s', bool(request.GET.get('state')), bool(request.GET.get('code')), bool(request.GET.get('error')))
    if not oauth_data or not secrets.compare_digest(request.GET.get('state', ''), oauth_data.get('state', '')):
        logger.warning('YouTube OAuth state validation failed: session_state_present=%s callback_state_present=%s', bool(oauth_data and oauth_data.get('state')), bool(request.GET.get('state')))
        messages.error(request, 'YouTube OAuth 인증 상태가 유효하지 않습니다.')
        return redirect('accounts')
    if request.GET.get('error'):
        messages.error(request, 'YouTube 인증이 취소되었거나 실패했습니다.')
        return redirect('accounts')
    account = get_object_or_404(SocialAccount, pk=oauth_data['account_id'], platform=SocialAccount.Platform.YOUTUBE)
    if not oauth_data.get('code_verifier'):
        logger.warning('YouTube OAuth PKCE verifier missing: account_id=%s', account.pk)
        messages.error(request, 'YouTube OAuth 인증 정보를 확인할 수 없습니다. 다시 연결해 주세요.')
        return redirect('accounts')
    try:
        logger.info('YouTube OAuth flow accepted: flow=%s account_id=%s', oauth_data.get('flow', 'new'), account.pk)
        logger.info('YouTube OAuth token exchange started: account_id=%s', account.pk)
        logger.info('YouTube OAuth PKCE verifier present: %s', bool(oauth_data.get('code_verifier')))
        credentials = exchange_code(request.GET.get('code', ''), oauth_data.get('code_verifier', ''))
        logger.info('YouTube OAuth token exchange succeeded: access_token_present=%s refresh_token_present=%s', bool(credentials.token), bool(credentials.refresh_token))
        logger.info('YouTube channel verification started: account_id=%s', account.pk)
        channel = verify_channel(credentials)
        logger.info('YouTube channel verification succeeded: channel_id_present=%s channel_name_present=%s', bool(channel.get('id')), bool(channel.get('title')))
        logger.info('YouTube channel ID comparison: existing_id_present=%s matches=%s', bool(account.external_account_id), not account.external_account_id or account.external_account_id == channel['id'])
        save_verified_credential(account, credentials, channel)
        logger.info('YouTube credential persistence succeeded: account_id=%s', account.pk)
        request.session.pop('youtube_oauth', None)
    except YouTubeOAuthError as exc:
        logger.warning('YouTube OAuth controlled failure: type=%s message=%s', type(exc).__name__, str(exc))
        if exc.account_name and exc.channel_name:
            messages.error(request, f'{str(exc)} 계정: {exc.account_name}. 선택된 채널: {exc.channel_name}. Google 계정에 여러 YouTube 채널 또는 브랜드 계정이 있는 경우 연결하려는 채널을 다시 선택해 주세요.')
        else:
            messages.error(request, str(exc))
    except Exception as exc:
        error_code, description = _safe_oauth_error_detail(exc)
        logger.error('YouTube OAuth unexpected failure: type=%s error=%s description=%s', type(exc).__name__, error_code, description)
        messages.error(request, 'YouTube 계정 연결에 실패했습니다. 잠시 후 다시 시도해 주세요.')
    else:
        messages.success(request, 'YouTube 채널을 확인하고 계정에 연결했습니다.')
    return redirect('accounts')

@operator_required
def youtube_verify(request, pk):
    account = _account_for_request(request, pk=pk, platform=SocialAccount.Platform.YOUTUBE)
    try:
        credentials = credentials_for_account(account)
        channel = verify_channel(credentials)
        save_verified_credential(account, credentials, channel)
    except YouTubeOAuthError as exc:
        messages.error(request, str(exc))
    except Exception:
        messages.error(request, 'YouTube 연결 확인에 실패했습니다.')
    else:
        messages.success(request, 'YouTube 연결을 확인했습니다.')
    return redirect('accounts')

@operator_required
def youtube_disconnect(request, pk):
    account = _account_for_request(request, pk=pk, platform=SocialAccount.Platform.YOUTUBE)
    if request.method == 'POST' and account.youtube_credential:
        revoke_credential(account.youtube_credential)
        messages.success(request, 'YouTube 연결을 해제했습니다. 계정과 콘텐츠는 유지됩니다.')
    return redirect('accounts')

@operator_required
def account_onboarding(request):
    form = AccountOnboardingForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            account = create_social_account(**form.cleaned_data)
        except DuplicateSocialAccountError as exc:
            form.add_error(None, f'이미 등록된 계정입니다. 기존 계정 #{exc.account.pk}을 확인하세요.')
        except Exception:
            form.add_error(None, '계정을 저장하지 못했습니다. 입력 내용을 확인한 뒤 다시 시도해 주세요.')
        else:
            messages.success(request, '계정과 기본 설정을 등록했습니다.')
            return redirect('accounts')
    return render(request, 'linker/account_onboarding.html', {'form': form, 'page_title': '추가 계정 등록'})

@operator_required
def toggle_account(request, pk):
    account = get_object_or_404(SocialAccount, pk=pk)
    if request.method == 'POST':
        account.status = 'INACTIVE' if account.status == 'ACTIVE' else 'ACTIVE'
        account.save(update_fields=['status', 'updated_at'])
        messages.success(request, '계정 상태를 변경했습니다.')
    return redirect('accounts')
@operator_required
def contents(request): return _crud(request,SocialContent,SocialContentForm,'linker/contents.html','콘텐츠관리')

@operator_required
def content_link_add(request, pk):
    content = get_object_or_404(SocialContent, pk=pk)
    form = SocialContentLinkForm(request.POST or None)
    if request.method == 'GET' and request.GET.get('linker'):
        form.initial['linker'] = request.GET['linker']
    if request.method == 'POST' and form.is_valid():
        link = form.save(commit=False)
        link.social_content = content
        try:
            link.save()
        except IntegrityError:
            form.add_error('linker', '이미 연결된 상품입니다.')
        else:
            messages.success(request, '연결상품을 추가했습니다.')
            return redirect('contents')
    return render(request, 'linker/content_link_add.html', {'form': form, 'content': content})

@operator_required
def content_link_remove(request, pk):
    link = get_object_or_404(SocialContentLink, pk=pk)
    if request.method == 'POST':
        link.delete()
        messages.success(request, '연결상품을 삭제했습니다.')
    return redirect('contents')

@operator_required
def linker_search(request):
    query = request.GET.get('q', '').strip()
    items = Linker.objects.filter(status='ACTIVE').select_related('marketplace')
    if query:
        items = items.filter(Q(linker_code__icontains=query) | Q(linker_group_code__icontains=query) | Q(product_no__icontains=query) | Q(title__icontains=query) | Q(description__icontains=query) | Q(marketplace__code__icontains=query) | Q(marketplace__name__icontains=query))
    return render(request, 'linker/linker_search.html', {'items': items[:50], 'q': query})

@operator_required
def marketplaces(request):
    edit = request.GET.get('edit')
    obj = get_object_or_404(Marketplace, pk=edit) if edit else None
    form = MarketplaceForm(request.POST or None, instance=obj)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, '판매처를 저장했습니다.')
        return redirect('marketplaces')
    return render(request, 'linker/marketplaces.html', {'form': form, 'edit_obj': obj, 'items': Marketplace.objects.all()})

@operator_required
def youtube_content_import(request, pk):
    account = _account_for_request(request, pk=pk, platform=SocialAccount.Platform.YOUTUBE)
    if account.youtube_oauth_status != 'CONNECTED':
        messages.error(request, 'YouTube 계정을 먼저 연결하세요.')
        return redirect('accounts')
    try:
        videos, next_page_token = list_uploads(account, request.GET.get('page_token', ''))
    except YouTubeReadError as exc:
        messages.error(request, str(exc))
        return redirect('accounts')
    registered_ids = set(account.contents.values_list('external_content_id', flat=True))
    return render(request, 'linker/youtube_content_import.html', {'account': account, 'videos': videos, 'registered_ids': registered_ids, 'next_page_token': next_page_token, 'linkers': Linker.objects.filter(status='ACTIVE').order_by('title')})

@operator_required
def youtube_content_register(request, pk):
    account = _account_for_request(request, pk=pk, platform=SocialAccount.Platform.YOUTUBE)
    if request.method != 'POST' or account.youtube_oauth_status != 'CONNECTED':
        return redirect('youtube_content_import', pk=account.pk)
    selected_ids = set(request.POST.getlist('video_ids'))
    if not selected_ids:
        messages.warning(request, '등록할 콘텐츠를 선택하세요.')
        return redirect('youtube_content_import', pk=account.pk)
    try:
        videos, _ = list_uploads(account, max_results=50)
        selected = [video for video in videos if video['video_id'] in selected_ids]
        linker = Linker.objects.filter(pk=request.POST.get('linker')).first() if request.POST.get('linker') else None
        registered = register_selected_videos(account, selected, linker)
    except YouTubeReadError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f'YouTube 콘텐츠 {len(registered)}개를 등록했습니다.')
    return redirect('contents')

@operator_required
def youtube_comment_sync(request, pk):
    logger.warning('YouTube comment sync route entered: method=%s content_id_present=%s', request.method, bool(pk))
    content = get_object_or_404(SocialContent.objects.select_related('social_account'), pk=pk, platform='YOUTUBE')
    logger.warning('YouTube comment sync content found: content_found=True platform_check_passed=%s account_found=%s credential_found=%s', content.platform == 'YOUTUBE', bool(content.social_account_id), bool(content.social_account.youtube_credential))
    if content.social_account.youtube_oauth_status != 'CONNECTED':
        logger.warning('YouTube comment sync redirect: reason=oauth_not_connected')
        messages.error(request, 'YouTube 연결을 다시 확인하세요.')
        return redirect('contents')
    try:
        result = sync_comments(content, request.GET.get('page_token', ''))
    except YouTubeReadError as exc:
        logger.warning('YouTube comment sync redirect: reason=read_error error_reason=%s', exc.code or 'unknown')
        messages.error(request, str(exc))
    else:
        logger.warning('YouTube comment sync redirect: reason=sync_complete fetched=%s created=%s updated=%s', result['fetched'], result['created'], result['updated'])
        messages.success(request, f"댓글 {result['fetched']}개 확인: 신규 {result['created']}개, 업데이트 {result['updated']}개")
    return redirect('contents')

@operator_required
def rules(request):
    edit=request.GET.get('edit'); obj=get_object_or_404(ResponseRule,pk=edit) if edit else None
    if request.method=='POST':
        obj=get_object_or_404(ResponseRule,pk=request.POST.get('id')) if request.POST.get('id') else None
        form=ResponseRuleForm(request.POST,instance=obj)
        if form.is_valid(): form.save(); messages.success(request,'응답규칙을 저장했습니다.'); return redirect('rules')
    else: form=ResponseRuleForm(instance=obj)
    return render(request,'linker/rules.html',{'form':form,'items':ResponseRule.objects.select_related('social_content__linker').order_by('-id')[:300],'edit_obj':obj})

@operator_required
def comments(request):
    q=request.GET.get('q','').strip(); qs=SocialComment.objects.select_related('social_content__linker').all()
    if q:
        digits=''.join(ch for ch in q if ch.isdigit()); cond=Q(comment_text__icontains=q)|Q(username__icontains=q)|Q(social_content__title__icontains=q)|Q(social_content__linker__title__icontains=q)|Q(social_content__linker__linker_code__icontains=q)
        if digits: cond |= Q(social_content__linker__linker_code__icontains=digits.zfill(6))
        qs=qs.filter(cond)
    status=request.GET.get('status'); decision=request.GET.get('decision'); platform=request.GET.get('platform')
    if status: qs=qs.filter(processing_status=status)
    if decision: qs=qs.filter(decision_status=decision)
    if platform: qs=qs.filter(social_content__platform=platform)
    return render(request,'linker/comments.html',{'items':qs[:300],'q':q,'status':status or '','decision':decision or '','platform':platform or ''})

@operator_required
def retry_comment(request,pk):
    c=get_object_or_404(SocialComment,pk=pk); c.processing_status='PENDING'; c.decision_status='PENDING'; c.save(update_fields=['processing_status','decision_status']); process_comment.delay(c.id); messages.success(request,'재처리를 요청했습니다.'); return redirect('comments')

@operator_required
def exclude_comment(request,pk):
    c=get_object_or_404(SocialComment,pk=pk); c.decision_status='EXCLUDED'; c.processing_status='SUCCESS'; c.save(update_fields=['decision_status','processing_status']); return redirect('comments')

@operator_required
def logs(request): return render(request,'linker/logs.html',{'items':ResponseLog.objects.select_related('response_rule','social_comment__social_content__social_account','social_comment__social_content__linker').order_by('-attempted_at')[:500]})

@csrf_exempt
def instagram_webhook(request):
    if request.method=='GET':
        if request.GET.get('hub.verify_token')==settings.META_VERIFY_TOKEN: return HttpResponse(request.GET.get('hub.challenge',''))
        return HttpResponse('verification failed',status=403)
    try: payload=json.loads(request.body or b'{}')
    except json.JSONDecodeError: return JsonResponse({'ok':False},status=400)
    created=[]
    for entry in payload.get('entry',[]):
        entry_account_id = str(entry.get('id') or '')
        for change in entry.get('changes',[]):
            v=change.get('value',{}); cid=str(v.get('id') or v.get('comment_id') or ''); media=v.get('media',{}) if isinstance(v.get('media',{}),dict) else {}; media_id=str(media.get('id') or v.get('media_id') or '')
            if not cid or not media_id: continue
            content_query = SocialContent.objects.filter(platform='INSTAGRAM', external_content_id=media_id)
            if entry_account_id:
                content_query = content_query.filter(social_account__platform_user_id=entry_account_id)
            content=content_query.select_related('social_account').first()
            if not content: continue
            author=v.get('from',{}) if isinstance(v.get('from',{}),dict) else {}
            obj,is_new=upsert_comment(content=content, external_comment_id=cid, external_user_id=str(author.get('id','')), username=author.get('username',''), comment_text=v.get('text',''), trigger=True)
            if is_new: created.append(obj.id)
    return JsonResponse({'ok':True,'created':created})
