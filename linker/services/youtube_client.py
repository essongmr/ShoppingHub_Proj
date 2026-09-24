from linker.services.youtube_oauth import credentials_for_account, YouTubeOAuthError


def authenticated_client(account):
    if account.platform != account.Platform.YOUTUBE:
        raise YouTubeOAuthError('YouTube 계정만 사용할 수 있습니다.')
    credentials = credentials_for_account(account)
    from googleapiclient.discovery import build
    return build('youtube', 'v3', credentials=credentials, cache_discovery=False)