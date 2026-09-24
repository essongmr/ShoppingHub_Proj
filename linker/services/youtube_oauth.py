import secrets
from datetime import datetime, timezone as dt_timezone

from cryptography.fernet import Fernet
from google.auth.exceptions import RefreshError
from django.conf import settings
from django.utils import timezone
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from linker.models import SocialCredential

YOUTUBE_READONLY_SCOPE = 'https://www.googleapis.com/auth/youtube.readonly'
YOUTUBE_FORCE_SSL_SCOPE = 'https://www.googleapis.com/auth/youtube.force-ssl'
YOUTUBE_OAUTH_SCOPES = [YOUTUBE_READONLY_SCOPE, YOUTUBE_FORCE_SSL_SCOPE]


class YouTubeOAuthError(Exception):
    def __init__(self, message, account_name='', channel_name=''):
        super().__init__(message)
        self.account_name = account_name
        self.channel_name = channel_name


def _fernet():
    if not settings.GOOGLE_YOUTUBE_CREDENTIAL_KEY:
        raise YouTubeOAuthError('YouTube credential 암호화 키가 설정되지 않았습니다.')
    try:
        return Fernet(settings.GOOGLE_YOUTUBE_CREDENTIAL_KEY.encode())
    except Exception as exc:
        raise YouTubeOAuthError('YouTube credential 암호화 키 설정이 올바르지 않습니다.') from exc


def _encrypt(value):
    return _fernet().encrypt((value or '').encode()).decode() if value else ''


def _decrypt(value):
    return _fernet().decrypt(value.encode()).decode() if value else ''


def client_config():
    if not settings.GOOGLE_YOUTUBE_CLIENT_ID or not settings.GOOGLE_YOUTUBE_CLIENT_SECRET:
        raise YouTubeOAuthError('Google YouTube OAuth client 설정이 없습니다.')
    return {'web': {'client_id': settings.GOOGLE_YOUTUBE_CLIENT_ID, 'client_secret': settings.GOOGLE_YOUTUBE_CLIENT_SECRET, 'auth_uri': 'https://accounts.google.com/o/oauth2/auth', 'token_uri': 'https://oauth2.googleapis.com/token'}}


def authorization_url(redirect_uri=None):
    redirect_uri = redirect_uri or settings.GOOGLE_YOUTUBE_REDIRECT_URI
    flow = Flow.from_client_config(client_config(), scopes=YOUTUBE_OAUTH_SCOPES, redirect_uri=redirect_uri)
    url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true', prompt='consent')
    if not flow.code_verifier:
        raise YouTubeOAuthError('PKCE verifier를 생성하지 못했습니다.')
    return url, state, flow.code_verifier


def exchange_code(code, code_verifier, redirect_uri=None):
    if not code_verifier:
        raise YouTubeOAuthError('PKCE verifier가 없습니다.')
    redirect_uri = redirect_uri or settings.GOOGLE_YOUTUBE_REDIRECT_URI
    flow = Flow.from_client_config(client_config(), scopes=YOUTUBE_OAUTH_SCOPES, redirect_uri=redirect_uri, code_verifier=code_verifier)
    flow.fetch_token(code=code)
    return flow.credentials


def _aware_expiry(expiry):
    if not expiry:
        return None
    return timezone.make_aware(expiry, dt_timezone.utc) if expiry.tzinfo is None else expiry


def verify_channel(credentials):
    service = build('youtube', 'v3', credentials=credentials, cache_discovery=False)
    result = service.channels().list(part='snippet', mine=True).execute()
    channels = result.get('items', [])
    if not channels:
        raise YouTubeOAuthError('인증된 YouTube 채널을 찾을 수 없습니다.')
    channel = channels[0]
    snippet = channel.get('snippet', {})
    thumbnails = snippet.get('thumbnails') or {}
    thumbnail = thumbnails.get('high') or thumbnails.get('medium') or thumbnails.get('default') or {}
    return {
        'id': channel['id'],
        'title': snippet.get('title', ''),
        'custom_url': snippet.get('customUrl', ''),
        'thumbnail_url': thumbnail.get('url', ''),
        'description': snippet.get('description', ''),
    }


def save_verified_credential(account, credentials, channel):
    from django.db import transaction
    from django.utils import timezone as django_timezone

    if account.external_account_id and account.external_account_id != channel['id']:
        raise YouTubeOAuthError('선택한 YouTube 채널이 등록된 채널과 다릅니다.', account_name=account.account_name, channel_name=channel.get('title', ''))
    with transaction.atomic():
        credential, _ = SocialCredential.objects.update_or_create(
            social_account=account,
            defaults={
                'provider': 'GOOGLE_YOUTUBE',
                'access_token_encrypted': _encrypt(credentials.token),
                'refresh_token_encrypted': _encrypt(credentials.refresh_token),
                'token_uri': credentials.token_uri or 'https://oauth2.googleapis.com/token',
                'scopes': list(getattr(credentials, 'granted_scopes', None) or credentials.scopes or YOUTUBE_OAUTH_SCOPES),
                'expires_at': _aware_expiry(credentials.expiry),
                'channel_id': channel['id'],
                'channel_name': channel['title'],
                'channel_url': f"https://www.youtube.com/{channel['custom_url']}" if channel['custom_url'] else f"https://www.youtube.com/channel/{channel['id']}",
                'last_verified_at': django_timezone.now(),
                'oauth_status': SocialCredential.OAuthStatus.CONNECTED,
                'revoked_at': None,
            },
        )
        if not account.external_account_id:
            account.external_account_id = channel['id']
        account.platform_user_id = channel['id']
        account.username = channel.get('custom_url', '').lstrip('@')
        account.display_name = channel['title']
        if not account.profile_url:
            account.profile_url = credential.channel_url
        account.profile_picture_url = channel.get('thumbnail_url', '')
        if channel.get('description') and not account.memo:
            account.memo = channel['description']
        account.connection_status = account.ConnectionStatus.CONNECTED
        account.granted_scopes = list(getattr(credentials, 'granted_scopes', None) or credentials.scopes or YOUTUBE_OAUTH_SCOPES)
        account.connected_at = account.connected_at or django_timezone.now()
        account.last_verified_at = django_timezone.now()
        account.save(update_fields=[
            'external_account_id',
            'platform_user_id',
            'username',
            'display_name',
            'profile_url',
            'profile_picture_url',
            'memo',
            'connection_status',
            'granted_scopes',
            'connected_at',
            'last_verified_at',
            'updated_at',
        ])
    return credential


def fetch_channel_intro(account):
    try:
        channel = verify_channel(credentials_for_account(account))
    except (RefreshError, HttpError) as exc:
        raise YouTubeOAuthError('YouTube 다시 연결이 필요합니다.') from exc
    intro = (channel.get('description') or '').strip()
    if intro:
        account.memo = intro
        account.save(update_fields=['memo', 'updated_at'])
    return intro


def credentials_for_account(account):
    credential = account.youtube_credential
    if not credential or credential.revoked_at:
        raise YouTubeOAuthError('YouTube 계정이 연결되지 않았습니다.')
    expiry = credential.expires_at.replace(tzinfo=None) if credential.expires_at else None
    credentials = Credentials(token=_decrypt(credential.access_token_encrypted), refresh_token=_decrypt(credential.refresh_token_encrypted), token_uri=credential.token_uri, client_id=settings.GOOGLE_YOUTUBE_CLIENT_ID, client_secret=settings.GOOGLE_YOUTUBE_CLIENT_SECRET, scopes=credential.scopes, expiry=expiry)
    if credentials.expired and credentials.refresh_token:
        from google.auth.transport.requests import Request
        credentials.refresh(Request())
        credential.access_token_encrypted = _encrypt(credentials.token)
        credential.expires_at = _aware_expiry(credentials.expiry)
        credential.save(update_fields=['access_token_encrypted', 'expires_at', 'updated_at'])
    return credentials


def revoke_credential(credential):
    from requests import post
    token = _decrypt(credential.refresh_token_encrypted) or _decrypt(credential.access_token_encrypted)
    try:
        response = post('https://oauth2.googleapis.com/revoke', params={'token': token}, timeout=15)
        response.raise_for_status()
    except Exception:
        pass
    credential.delete()
