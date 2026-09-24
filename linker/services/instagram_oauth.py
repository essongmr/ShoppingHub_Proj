import secrets
from datetime import timedelta

import requests
from cryptography.fernet import Fernet
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from linker.models import SocialCredential

INSTAGRAM_BASIC_SCOPE = 'instagram_business_basic'
INSTAGRAM_MANAGE_COMMENTS_SCOPE = 'instagram_business_manage_comments'
INSTAGRAM_MANAGE_MESSAGES_SCOPE = 'instagram_business_manage_messages'
INSTAGRAM_OAUTH_SCOPES = [INSTAGRAM_BASIC_SCOPE, INSTAGRAM_MANAGE_COMMENTS_SCOPE, INSTAGRAM_MANAGE_MESSAGES_SCOPE]


class InstagramOAuthError(Exception):
    def __init__(self, message, account_name='', username=''):
        super().__init__(message)
        self.account_name = account_name
        self.username = username


def _fernet():
    if not settings.GOOGLE_YOUTUBE_CREDENTIAL_KEY:
        raise InstagramOAuthError('Instagram credential 암호화 키가 설정되지 않았습니다.')
    try:
        return Fernet(settings.GOOGLE_YOUTUBE_CREDENTIAL_KEY.encode())
    except Exception as exc:
        raise InstagramOAuthError('Instagram credential 암호화 키 설정이 올바르지 않습니다.') from exc


def _encrypt(value):
    return _fernet().encrypt((value or '').encode()).decode() if value else ''


def _decrypt(value):
    return _fernet().decrypt(value.encode()).decode() if value else ''


def _require_config():
    if not settings.META_APP_ID or not settings.META_APP_SECRET:
        raise InstagramOAuthError('Instagram OAuth 설정이 없습니다.')


def authorization_url(redirect_uri=None, force_reauth=False):
    _require_config()
    state = secrets.token_urlsafe(32)
    params = {'client_id': settings.META_APP_ID, 'redirect_uri': redirect_uri or settings.META_INSTAGRAM_REDIRECT_URI, 'response_type': 'code', 'scope': ','.join(INSTAGRAM_OAUTH_SCOPES), 'state': state}
    if force_reauth:
        params['force_reauth'] = 'true'
    request = requests.Request('GET', 'https://www.instagram.com/oauth/authorize', params=params).prepare()
    return request.url, state


def _post_token(code, redirect_uri=None):
    _require_config()
    response = requests.post('https://api.instagram.com/oauth/access_token', data={'client_id': settings.META_APP_ID, 'client_secret': settings.META_APP_SECRET, 'grant_type': 'authorization_code', 'redirect_uri': redirect_uri or settings.META_INSTAGRAM_REDIRECT_URI, 'code': code}, timeout=15)
    if not response.ok:
        raise InstagramOAuthError('Instagram 인증 토큰을 발급하지 못했습니다.')
    return response.json()


def exchange_code(code, redirect_uri=None):
    if not code:
        raise InstagramOAuthError('Instagram 인증 코드가 없습니다.')
    short_lived = _post_token(code, redirect_uri)
    token = short_lived.get('access_token', '')
    if not token:
        raise InstagramOAuthError('Instagram access token을 받지 못했습니다.')
    response = requests.get('https://graph.instagram.com/' + settings.META_INSTAGRAM_GRAPH_VERSION + '/access_token', params={'grant_type': 'ig_exchange_token', 'client_secret': settings.META_APP_SECRET, 'access_token': token}, timeout=15)
    if response.ok:
        long_lived = response.json()
        token = long_lived.get('access_token', token)
        expires_in = long_lived.get('expires_in')
    else:
        expires_in = None
    return {'access_token': token, 'expires_in': expires_in, 'scopes': short_lived.get('permissions') or INSTAGRAM_OAUTH_SCOPES}


def verify_account(token):
    response = requests.get('https://graph.instagram.com/' + settings.META_INSTAGRAM_GRAPH_VERSION + '/me', params={'fields': 'user_id,username,name,account_type,profile_picture_url', 'access_token': token}, timeout=15)
    if not response.ok:
        raise InstagramOAuthError('Instagram 계정 정보를 확인하지 못했습니다.')
    data = response.json()
    if not data.get('user_id') or not data.get('username'):
        raise InstagramOAuthError('Instagram Professional 계정 정보를 찾을 수 없습니다.')
    for intro_field in ('biography', 'bio'):
        intro_response = requests.get('https://graph.instagram.com/' + settings.META_INSTAGRAM_GRAPH_VERSION + '/me', params={'fields': intro_field, 'access_token': token}, timeout=15)
        if intro_response.ok:
            intro = (intro_response.json().get(intro_field) or '').strip()
            if intro:
                data['biography'] = intro
                break
    return data


def fetch_profile_intro(account):
    token = credentials_for_account(account)
    for intro_field in ('biography', 'bio'):
        try:
            response = requests.get('https://graph.instagram.com/' + settings.META_INSTAGRAM_GRAPH_VERSION + '/me', params={'fields': intro_field, 'access_token': token}, timeout=15)
        except requests.RequestException as exc:
            raise InstagramOAuthError('Instagram 소개문구를 다시 조회하지 못했습니다.') from exc
        if not response.ok:
            headers = getattr(response, 'headers', {})
            error = response.json().get('error', {}) if headers.get('Content-Type', '').startswith('application/json') else {}
            if error.get('code') in {10, 190, 200}:
                raise InstagramOAuthError('Instagram 다시 연결이 필요합니다.')
            continue
        intro = (response.json().get(intro_field) or '').strip()
        if intro:
            account.memo = intro
            account.save(update_fields=['memo', 'updated_at'])
            return intro
    return ''


def save_verified_credential(account, token_data, profile):
    if account.external_account_id and account.external_account_id != profile['user_id']:
        raise InstagramOAuthError('선택한 Instagram 계정이 등록된 계정과 다릅니다.', account_name=account.account_name, username=profile.get('username', ''))
    expires_at = timezone.now() + timedelta(seconds=token_data['expires_in']) if token_data.get('expires_in') else None
    with transaction.atomic():
        credential, _ = SocialCredential.objects.update_or_create(social_account=account, defaults={'provider': 'META_INSTAGRAM', 'access_token_encrypted': _encrypt(token_data['access_token']), 'refresh_token_encrypted': '', 'token_uri': 'https://graph.instagram.com/' + settings.META_INSTAGRAM_GRAPH_VERSION + '/refresh_access_token', 'scopes': list(token_data.get('scopes') or INSTAGRAM_OAUTH_SCOPES), 'expires_at': expires_at, 'channel_id': profile['user_id'], 'channel_name': profile.get('username', ''), 'channel_url': 'https://www.instagram.com/' + profile['username'] + '/', 'last_verified_at': timezone.now(), 'oauth_status': SocialCredential.OAuthStatus.CONNECTED, 'revoked_at': None})
        account.external_account_id = profile['user_id']
        account.platform_user_id = profile['user_id']
        account.username = profile.get('username', '')
        account.display_name = profile.get('name') or profile['username']
        account.profile_picture_url = profile.get('profile_picture_url', '')
        if (profile.get('biography') or profile.get('bio')) and not account.memo:
            account.memo = profile.get('biography') or profile.get('bio')
        account.profile_url = credential.channel_url
        account.connection_status = account.ConnectionStatus.CONNECTED
        account.granted_scopes = list(token_data.get('scopes') or INSTAGRAM_OAUTH_SCOPES)
        account.connected_at = account.connected_at or timezone.now()
        account.last_verified_at = timezone.now()
        account.save(update_fields=['external_account_id', 'platform_user_id', 'username', 'display_name', 'profile_picture_url', 'memo', 'profile_url', 'connection_status', 'granted_scopes', 'connected_at', 'last_verified_at', 'updated_at'])
    return credential


def credentials_for_account(account):
    credential = account.youtube_credential if account.platform == 'YOUTUBE' else getattr(account, 'credential', None)
    if not credential or credential.revoked_at or credential.provider != 'META_INSTAGRAM':
        raise InstagramOAuthError('Instagram 계정이 연결되지 않았습니다.')
    if credential.expires_at and credential.expires_at <= timezone.now():
        response = requests.get('https://graph.instagram.com/' + settings.META_INSTAGRAM_GRAPH_VERSION + '/refresh_access_token', params={'grant_type': 'ig_refresh_token', 'access_token': _decrypt(credential.access_token_encrypted)}, timeout=15)
        if not response.ok:
            raise InstagramOAuthError('Instagram 연결을 다시 확인하세요.')
        data = response.json()
        if data.get('access_token'):
            credential.access_token_encrypted = _encrypt(data['access_token'])
            credential.expires_at = timezone.now() + timedelta(seconds=data['expires_in']) if data.get('expires_in') else credential.expires_at
            credential.save(update_fields=['access_token_encrypted', 'expires_at', 'updated_at'])
    return _decrypt(credential.access_token_encrypted)


def revoke_credential(credential):
    token = _decrypt(credential.access_token_encrypted)
    try:
        requests.delete('https://graph.instagram.com/' + settings.META_INSTAGRAM_GRAPH_VERSION + '/me/permissions', params={'access_token': token}, timeout=15)
    finally:
        credential.delete()
