from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q
from .models import Linker, SocialAccount, SocialContent, ResponseRule, ResponseKeyword, SocialContentLink, Marketplace, MessageType, MessageTemplate
from .models import CreatorStore, Linker, Marketplace, ProductBlock, ProductBlockItem, ResponseKeyword, ResponseLog, ResponseRule, SocialAccount, SocialComment, SocialContent, SocialContentLink, StoreCategory
from .services.account_onboarding import find_duplicate_account, normalize_profile_url
from .services.message_templates import validate_template_variables


DEFAULT_STORE_CATEGORY_NAMES = [
    '생활/주방',
    '식품',
    '뷰티',
    '패션',
    '디지털/가전',
    '자동차',
    '반려동물',
    '육아',
    '건강',
    '기타',
]


def ensure_default_store_categories(owner, store=None):
    if not owner:
        return StoreCategory.objects.none()
    for index, name in enumerate(DEFAULT_STORE_CATEGORY_NAMES):
        lookup = {'owner': owner, 'name': name}
        if store is not None:
            lookup['store'] = store
        StoreCategory.objects.get_or_create(
            **lookup,
            defaults={'store': store, 'display_order': index},
        )
    categories = StoreCategory.objects.filter(owner=owner, active=True)
    if store is not None:
        categories = categories.filter(store=store)
    return categories.order_by('display_order', 'id')


class CreatorSignupForm(UserCreationForm):
    email = None

    class Meta:
        model = User
        fields = ('username', 'password1', 'password2')
        labels = {'username': '사용자명'}

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        if User.objects.filter(username__iexact=username).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('이미 사용 중인 아이디입니다.')
        return username

    def clean_password2(self):
        password = self.cleaned_data.get('password1')
        confirmation = self.cleaned_data.get('password2')
        if password and confirmation and password != confirmation:
            raise forms.ValidationError('비밀번호가 서로 일치하지 않습니다.')
        return confirmation


class CreatorStoreNameForm(forms.Form):
    store_name = forms.CharField(
        max_length=160,
        required=True,
        label='Store 이름',
        strip=True,
        widget=forms.TextInput(attrs={
            'class': 'builder-input',
            'placeholder': '예) 싱송의생활템',
        }),
        error_messages={
            'required': 'Store 이름을 입력해주세요.',
            'max_length': 'Store 이름이 너무 깁니다.',
        },
    )

    def clean_store_name(self):
        value = self.cleaned_data['store_name'].strip()
        if not value:
            raise forms.ValidationError('Store 이름을 입력해주세요.')
        return value


class CreatorProfileSelectionForm(forms.Form):
    account_id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    profile_image = forms.ImageField(
        required=False,
        error_messages={'invalid_image': '유효한 이미지 파일을 업로드해주세요.'},
    )
    skip = forms.BooleanField(required=False, widget=forms.HiddenInput)

    def clean_profile_image(self):
        image = self.cleaned_data.get('profile_image')
        if image and image.size > 5 * 1024 * 1024:
            raise forms.ValidationError('이미지는 5MB 이하로 업로드해주세요.')
        return image


class CreatorStoreIntroForm(forms.Form):
    tagline = forms.CharField(
        max_length=240,
        required=False,
        strip=True,
        widget=forms.Textarea(attrs={'class': 'builder-input', 'rows': 3}),
    )


class LinkerForm(forms.ModelForm):
    class Meta:
        model=Linker; fields=['linker_group_code','title','marketplace','destination_url','product_no','description','thumbnail_url','memo','status']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['marketplace'].queryset = Marketplace.objects.filter(active=True)


class ShoppingHubProductForm(forms.ModelForm):
    remove_product_image = forms.BooleanField(required=False, widget=forms.HiddenInput)
    destination_url = forms.URLField(
        required=False,
        label='상품링크',
        widget=forms.URLInput(attrs={'placeholder': 'https://'}),
        error_messages={'invalid': '상품링크는 http 또는 https 주소여야 합니다.'},
    )

    class Meta:
        model = Linker
        fields = ['product_no', 'title', 'product_image', 'marketplace', 'destination_url', 'description', 'store_category', 'display_order', 'store_visible']
        labels = {'product_no': '상품번호', 'title': '상품명', 'product_image': '대표이미지', 'marketplace': '판매사이트', 'destination_url': '상품링크', 'description': '짧은 소개'}
        widgets = {'destination_url': forms.URLInput(attrs={'placeholder': 'https://'}), 'product_image': forms.ClearableFileInput(attrs={'accept': 'image/*'})}

    def __init__(self, *args, owner=None, store=None, **kwargs):
        self.owner = owner
        self.store = store
        super().__init__(*args, **kwargs)
        self.fields['marketplace'].queryset = Marketplace.objects.filter(active=True)
        self.fields['store_category'].required = False
        queryset = ensure_default_store_categories(owner, self.store) if owner else StoreCategory.objects.none()
        first_store_id = CreatorStore.objects.filter(owner=owner).order_by('created_at', 'id').values_list('pk', flat=True).first() if owner else None
        if self.store and self.store.pk == first_store_id:
            queryset = StoreCategory.objects.filter(owner=owner).filter(Q(store=self.store) | Q(store__isnull=True), active=True).order_by('display_order', 'id')
        if self.instance and self.instance.pk and self.instance.store_category_id:
            queryset = StoreCategory.objects.filter(owner=owner).filter(Q(store=self.store) | (Q(store__isnull=True) if self.store and self.store.pk == first_store_id else Q(pk=-1))).filter(
                models.Q(active=True) | models.Q(pk=self.instance.store_category_id)
            ).order_by('display_order', 'id')
        self.fields['store_category'].queryset = queryset
        self.fields['store_category'].empty_label = '카테고리 선택'
        self.fields['display_order'].required = False
        self.fields['store_visible'].required = False

    def save(self, commit=True):
        old_image = self.instance.product_image if self.instance and self.instance.pk else None
        remove_image = self.cleaned_data.get('remove_product_image', False)
        product = super().save(commit=False)
        if remove_image:
            product.product_image = None
            if old_image and old_image.name and product.thumbnail_url.startswith('/media/'):
                product.thumbnail_url = ''
        if commit:
            product.save()
            if remove_image and old_image and old_image.name:
                old_image.delete(save=False)
            elif old_image and old_image.name and old_image.name != product.product_image.name:
                old_image.delete(save=False)
        return product

    def clean_product_no(self):
        value = self.cleaned_data['product_no'].strip()
        if not value:
            if not self.fields['product_no'].required:
                return value
            raise forms.ValidationError('상품번호를 입력해주세요.')
        if self.owner and Linker.objects.filter(owner=self.owner, store=self.store, product_no=value).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('이미 사용 중인 상품번호입니다.')
        return value

    def clean_destination_url(self):
        value = self.cleaned_data['destination_url'].strip()
        if not value:
            return value
        if not value.startswith(('http://', 'https://')):
            raise forms.ValidationError('상품링크는 http 또는 https 주소여야 합니다.')
        return value


class TypedItemForm(ShoppingHubProductForm):
    ingredients = forms.CharField(required=False, widget=forms.Textarea)
    instructions = forms.CharField(required=False, widget=forms.Textarea)
    tips = forms.CharField(required=False, widget=forms.Textarea)
    servings = forms.CharField(required=False)
    prep_time = forms.CharField(required=False)
    cook_time = forms.CharField(required=False)
    service_description = forms.CharField(required=False, widget=forms.Textarea)
    price_info = forms.CharField(required=False, widget=forms.Textarea)
    duration = forms.CharField(required=False)
    location_info = forms.CharField(required=False, widget=forms.Textarea)
    contact_info = forms.CharField(required=False, widget=forms.Textarea)
    booking_url = forms.URLField(required=False)

    class Meta(ShoppingHubProductForm.Meta):
        fields = ShoppingHubProductForm.Meta.fields + ['item_type']
        labels = {**ShoppingHubProductForm.Meta.labels, 'item_type': '등록 유형'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['item_type'].required = True
        self.fields['destination_url'].required = False


class ProductBlockForm(forms.ModelForm):
    class Meta:
        model = ProductBlock
        fields = ['block_number', 'title', 'representative_image', 'description', 'store_category', 'display_order', 'store_visible']
        labels = {'block_number': '상품블록 번호', 'title': '상품블록 이름', 'representative_image': '대표이미지', 'description': '짧은 소개'}

    def __init__(self, *args, owner=None, store=None, **kwargs):
        self.owner = owner
        self.store = store
        super().__init__(*args, **kwargs)
        self.fields['store_category'].required = False
        self.fields['store_category'].queryset = StoreCategory.objects.filter(owner=owner, store=store, active=True) if owner else StoreCategory.objects.none()
        self.fields['display_order'].required = False
        self.fields['store_visible'].required = False

    def clean_block_number(self):
        value = self.cleaned_data['block_number'].strip().upper()
        if not value.startswith('B'):
            value = f'B{value}'
        if self.owner and ProductBlock.objects.filter(owner=self.owner, store=self.store, block_number=value).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('이미 사용 중인 상품블록 번호입니다.')
        return value


class SocialContentLinkForm(forms.ModelForm):
    class Meta:
        model = SocialContentLink
        fields = ['linker', 'response_code', 'active']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['linker'].queryset = Linker.objects.filter(status='ACTIVE').select_related('marketplace')

class MarketplaceForm(forms.ModelForm):
    class Meta:
        model = Marketplace
        fields = ['code', 'name', 'active', 'sort_order', 'memo']

class CreatorStoreForm(forms.ModelForm):
    class Meta:
        model = CreatorStore
        fields = ['store_name', 'slug', 'tagline', 'profile_image', 'cover_image', 'active']
        labels = {
            'store_name': '스토어 이름',
            'slug': '스토어 주소',
            'tagline': '스토어 소개 문구',
            'profile_image': '프로필 이미지',
            'cover_image': '커버 이미지',
            'active': '스토어 공개',
        }
        help_texts = {
            'slug': '스토어의 고유 주소로 사용됩니다. 영문, 숫자, 하이픈(-)을 사용할 수 있습니다.',
        }
        widgets = {'profile_image': forms.ClearableFileInput(), 'cover_image': forms.ClearableFileInput()}

    def clean_slug(self):
        value = self.cleaned_data['slug'].lower().strip()
        if value in {'admin', 'login', 'signup', 'workspace', 'api', 'accounts', 'contents', 'linkers', 'marketplaces', 'rules', 'comments', 'logs', 's'}:
            raise forms.ValidationError('사용할 수 없는 Store 주소입니다.')
        return value

class SocialAccountForm(forms.ModelForm):
    class Meta:
        model=SocialAccount; fields=['platform','account_name','external_account_id','profile_url','display_name','memo','status']


class AccountOnboardingForm(forms.ModelForm):
    apply_default_rules = forms.BooleanField(required=False, label='기본 응답규칙 적용')

    class Meta:
        model = SocialAccount
        fields = ['platform', 'account_name', 'external_account_id', 'profile_url', 'display_name', 'memo', 'status']

    def clean_profile_url(self):
        return normalize_profile_url(self.cleaned_data.get('profile_url', ''))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('platform'):
            duplicate = find_duplicate_account(
                cleaned['platform'],
                cleaned.get('external_account_id', ''),
                cleaned.get('profile_url', ''),
            )
            if duplicate:
                raise forms.ValidationError(f'이미 등록된 계정입니다. 기존 계정 #{duplicate.pk}을 확인하세요.')
        return cleaned


class ShoppingHubAutomationForm(forms.Form):
    scope = forms.ChoiceField(choices=[('STORE', 'Store 전체'), ('ACCOUNT', '특정 Instagram 계정'), ('CONTENT', '특정 콘텐츠')], required=False, initial='CONTENT')
    account_id = forms.IntegerField(required=False)
    content_id = forms.IntegerField(required=False)
    mode = forms.ChoiceField(choices=[('KEYWORD', '특정 댓글에만'), ('ALL', '모든 댓글에'), ('MANUAL', '댓글에 따라 다르게')])
    keywords = forms.CharField(required=False)
    target_type = forms.ChoiceField(choices=[('PRODUCT', '상품'), ('PRODUCT_BLOCK', '상품 모아보기'), ('STORE', 'Store')])
    target_id = forms.IntegerField(required=False)
    link_mode = forms.ChoiceField(choices=ResponseRule.LinkMode.choices, initial=ResponseRule.LinkMode.LINK_STORE)
    direct_url = forms.URLField(required=False, max_length=1000)
    include_link_store = forms.BooleanField(required=False, initial=True)
    message_body = forms.CharField(required=False, max_length=2000)
    greeting = forms.CharField(required=False, max_length=200, initial='안녕하세요 😊')
    guidance = forms.CharField(required=False, max_length=500)
    store_routing = forms.BooleanField(required=False, initial=True)

    def clean_keywords(self):
        values = [value.strip().casefold() for value in self.cleaned_data.get('keywords', '').split(',') if value.strip()]
        if len(values) != len(set(values)):
            raise forms.ValidationError('같은 댓글 키워드는 한 번만 사용할 수 있습니다.')
        return values

    def clean(self):
        cleaned = super().clean()
        scope = cleaned.get('scope') or 'CONTENT'
        cleaned['scope'] = scope
        if scope == 'ACCOUNT' and not cleaned.get('account_id'):
            self.add_error('account_id', '계정을 선택해 주세요.')
        if scope == 'CONTENT' and not cleaned.get('content_id'):
            self.add_error('content_id', '콘텐츠를 선택해 주세요.')
        return cleaned

class SocialContentForm(forms.ModelForm):
    class Meta:
        model=SocialContent; fields=['social_account','linker','platform','content_type','external_content_id','title','caption','content_url','thumbnail_url','published_at','status']
        widgets={'published_at':forms.DateTimeInput(attrs={'type':'datetime-local'})}
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['linker'].required = False

class ResponseRuleForm(forms.ModelForm):
    keywords=forms.CharField(required=False,help_text='쉼표로 여러 키워드를 입력')
    class Meta:
        model=ResponseRule; fields=['scope','store','social_account','social_content','response_mode','public_reply_enabled','private_reply_enabled','public_reply_text','test_private_reply_text','enabled']
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        if self.instance.pk: self.fields['keywords'].initial=', '.join(self.instance.keywords.filter(enabled=True).values_list('keyword',flat=True))
    def clean(self):
        cleaned = super().clean()
        scope = cleaned.get('scope')
        store = cleaned.get('store')
        account = cleaned.get('social_account')
        content = cleaned.get('social_content')
        if scope == ResponseRule.Scope.STORE and (not store or account or content):
            raise forms.ValidationError('Store 범위는 Store만 선택해야 합니다.')
        if scope == ResponseRule.Scope.ACCOUNT and (not account or content or (store and account.store_id != store.pk)):
            raise forms.ValidationError('Account 범위는 선택한 Store의 계정이어야 합니다.')
        if scope == ResponseRule.Scope.CONTENT and (not content or account or (store and content.social_account.store_id != store.pk)):
            raise forms.ValidationError('Content 범위는 선택한 Store의 콘텐츠여야 합니다.')
        return cleaned
    def save(self,commit=True):
        obj=super().save(commit=commit)
        if commit:
            obj.keywords.all().delete()
            for kw in [x.strip() for x in self.cleaned_data.get('keywords','').split(',') if x.strip()]: ResponseKeyword.objects.create(response_rule=obj,keyword=kw)
        return obj


class CreatorMessageTemplateForm(forms.ModelForm):
    class Meta:
        model = MessageTemplate
        fields = ['message_type', 'name', 'description', 'body_template']
        labels = {
            'message_type': '메시지 유형',
            'name': '메시지 이름',
            'description': '설명',
            'body_template': '메시지 내용',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['message_type'].queryset = MessageType.objects.filter(active=True).order_by('display_order', 'id')

    def clean_body_template(self):
        value = self.cleaned_data['body_template']
        validate_template_variables(value)
        return value
