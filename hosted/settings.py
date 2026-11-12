import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ['OPR_LOCAL_SECRET_KEY']
ALLOWED_HOSTS = ['localhost', '127.0.0.1', '[::1]']
if os.environ.get('CODESPACES') == 'true':
    # Accept the exact GitHub-forwarded origin while keeping the service on loopback.
    port = int(os.environ.get('OPR_WEB_PORT', '8765'))
    forwarded_host = f"{os.environ['CODESPACE_NAME']}-{port}.{os.environ['GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN']}"
    ALLOWED_HOSTS.append(forwarded_host)
    CSRF_TRUSTED_ORIGINS = [f'https://{forwarded_host}']
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
ROOT_URLCONF = 'hosted.urls'
INSTALLED_APPS = [
    'django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'hosted',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'APP_DIRS': True,
    'OPTIONS': {'context_processors': ['django.template.context_processors.request']},
}]
DATABASES = {'default': {
    'ENGINE': 'django.db.backends.sqlite3',
    'NAME': os.environ.get('OPR_SAMPLE_DB', BASE_DIR / 'sample.sqlite3'),
    # Reserve the SQLite write lock before queue transactions read their current state.
    'OPTIONS': {'transaction_mode': 'IMMEDIATE'},
}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
USE_TZ = True
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'
