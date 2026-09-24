import os
from pathlib import Path
BASE_DIR=Path(__file__).resolve().parent.parent
SECRET_KEY=os.getenv('DJANGO_SECRET_KEY','dev-only')
DEBUG=os.getenv('DJANGO_DEBUG','1')=='1'
ALLOWED_HOSTS=[x.strip() for x in os.getenv('DJANGO_ALLOWED_HOSTS','*').split(',')]
CSRF_TRUSTED_ORIGINS = [
    x.strip()
    for x in os.getenv(
        "DJANGO_CSRF_TRUSTED_ORIGINS",
        ""
    ).split(",")
    if x.strip()
]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', '1') == '1'
CSRF_COOKIE_SECURE = os.getenv('CSRF_COOKIE_SECURE', '1') == '1'
SESSION_COOKIE_SAMESITE = 'Lax'
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'loggers': {'linker': {'handlers': ['console'], 'level': 'INFO', 'propagate': False}},
}
INSTALLED_APPS=['django.contrib.admin','django.contrib.auth','django.contrib.contenttypes','django.contrib.sessions','django.contrib.messages','django.contrib.staticfiles','linker']
MIDDLEWARE=['django.middleware.security.SecurityMiddleware','django.contrib.sessions.middleware.SessionMiddleware','django.middleware.common.CommonMiddleware','django.middleware.csrf.CsrfViewMiddleware','django.contrib.auth.middleware.AuthenticationMiddleware','django.contrib.messages.middleware.MessageMiddleware']
ROOT_URLCONF='config.urls'
LOGIN_URL='/login/'
LOGIN_REDIRECT_URL='/shoppinghub/'
LOGOUT_REDIRECT_URL='/login/'
TEMPLATES=[{'BACKEND':'django.template.backends.django.DjangoTemplates','DIRS':[],'APP_DIRS':True,'OPTIONS':{'context_processors':['django.template.context_processors.request','django.contrib.auth.context_processors.auth','django.contrib.messages.context_processors.messages','linker.context_processors.shoppinghub_store_context']}}]
WSGI_APPLICATION='config.wsgi.application'
DATABASES={'default':{'ENGINE':'django.db.backends.postgresql','NAME':os.getenv('POSTGRES_DB','content_linker'),'USER':os.getenv('POSTGRES_USER','content_linker'),'PASSWORD':os.getenv('POSTGRES_PASSWORD','change-me'),'HOST':os.getenv('POSTGRES_HOST','db'),'PORT':os.getenv('POSTGRES_PORT','5432')}}
AUTH_PASSWORD_VALIDATORS=[
    {'NAME':'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME':'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME':'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME':'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
LANGUAGE_CODE='ko-kr'; TIME_ZONE='Asia/Seoul'; USE_I18N=True; USE_TZ=True
STATIC_URL='/static/'
STATIC_ROOT=BASE_DIR/'staticfiles'

MEDIA_URL='/media/'
MEDIA_ROOT=BASE_DIR/'media'

DEFAULT_AUTO_FIELD='django.db.models.BigAutoField'
ENABLE_UI_PREVIEW=False
CELERY_BROKER_URL=os.getenv('REDIS_URL','redis://redis:6379/0'); CELERY_RESULT_BACKEND=CELERY_BROKER_URL
META_VERIFY_TOKEN=os.getenv('META_VERIFY_TOKEN',''); META_ACCESS_TOKEN=os.getenv('META_ACCESS_TOKEN','')
GOOGLE_YOUTUBE_CLIENT_ID=os.getenv('GOOGLE_YOUTUBE_CLIENT_ID','')
GOOGLE_YOUTUBE_CLIENT_SECRET=os.getenv('GOOGLE_YOUTUBE_CLIENT_SECRET','')
GOOGLE_YOUTUBE_REDIRECT_URI=os.getenv('GOOGLE_YOUTUBE_REDIRECT_URI','https://linker.dev.realtyhub.cloud/oauth/youtube/callback/')
GOOGLE_YOUTUBE_CREDENTIAL_KEY=os.getenv('GOOGLE_YOUTUBE_CREDENTIAL_KEY','')
META_APP_ID=os.getenv('META_APP_ID','')
META_APP_SECRET=os.getenv('META_APP_SECRET','')
META_INSTAGRAM_REDIRECT_URI=os.getenv('META_INSTAGRAM_REDIRECT_URI','https://linker.dev.realtyhub.cloud/oauth/instagram/callback/')
META_INSTAGRAM_GRAPH_VERSION=os.getenv('META_INSTAGRAM_GRAPH_VERSION','v25.0')
META_INSTAGRAM_AUTO_REDIRECT_URI=os.getenv('META_INSTAGRAM_AUTO_REDIRECT_URI','https://linker.dev.realtyhub.cloud/oauth/instagram/auto/callback/')
GOOGLE_YOUTUBE_AUTO_REDIRECT_URI=os.getenv('GOOGLE_YOUTUBE_AUTO_REDIRECT_URI','https://linker.dev.realtyhub.cloud/oauth/youtube/auto/callback/')
SHOPPINGHUB_PUBLIC_BASE_URL=os.getenv('SHOPPINGHUB_PUBLIC_BASE_URL','https://linker.dev.realtyhub.cloud').rstrip('/')
