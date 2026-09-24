from django.db import models, transaction
from django.core.exceptions import ValidationError
from django.db.models import Max
from django.utils import timezone


class CreatorProfile(models.Model):
    class OnboardingStatus(models.TextChoices):
        ACCOUNT_CREATED = 'ACCOUNT_CREATED', 'Account created'
        STORE_NAMING = 'STORE_NAMING', 'Store naming'
        PROFILE_SELECTION = 'PROFILE_SELECTION', 'Profile selection'
        STORE_INTRO = 'STORE_INTRO', 'Store intro'
        STORE_ADDRESS = 'STORE_ADDRESS', 'Store address'
        COMPLETED = 'COMPLETED', 'Completed'

    user = models.OneToOneField('auth.User', on_delete=models.CASCADE, related_name='creator_profile')
    representative_social_account = models.ForeignKey(
        'SocialAccount',
        on_delete=models.SET_NULL,
        related_name='representative_creator_profiles',
        null=True,
        blank=True,
    )
    onboarding_status = models.CharField(max_length=32, choices=OnboardingStatus.choices, default=OnboardingStatus.ACCOUNT_CREATED, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.user.username} | {self.onboarding_status}'

class CreatorStore(models.Model):
    owner = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='creator_stores')
    store_name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=80, unique=True)
    tagline = models.CharField(max_length=240, blank=True)
    profile_image = models.ImageField(upload_to='creator/stores/profile/', blank=True)
    cover_image = models.ImageField(upload_to='creator/stores/cover/', blank=True)
    active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return self.store_name


class StoreCategory(models.Model):
    owner = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='store_categories')
    store = models.ForeignKey(CreatorStore, on_delete=models.CASCADE, related_name='categories', null=True, blank=True)
    name = models.CharField(max_length=80)
    display_order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['store', 'name'], name='uniq_store_category_store_name')]
        ordering = ['display_order', 'id']

    def __str__(self):
        return self.name

class SocialAccount(models.Model):
    class Platform(models.TextChoices): INSTAGRAM='INSTAGRAM','Instagram'; YOUTUBE='YOUTUBE','YouTube'; TIKTOK='TIKTOK','TikTok'
    class Status(models.TextChoices): ACTIVE='ACTIVE','Active'; INACTIVE='INACTIVE','Inactive'; SETUP_REQUIRED='SETUP_REQUIRED','Setup required'
    class ConnectionStatus(models.TextChoices):
        NOT_CONNECTED='NOT_CONNECTED','Not connected'
        PENDING='PENDING','Pending'
        CONNECTING='CONNECTING','Connecting'
        CONNECTED='CONNECTED','Connected'
        ERROR='ERROR','Error'
        REAUTH_REQUIRED='REAUTH_REQUIRED','Re-auth required'
    owner=models.ForeignKey('auth.User',on_delete=models.CASCADE,related_name='social_accounts',null=True,blank=True)
    store=models.ForeignKey('CreatorStore',on_delete=models.SET_NULL,related_name='social_accounts',null=True,blank=True)
    platform=models.CharField(max_length=20,choices=Platform.choices,db_index=True)
    account_name=models.CharField(max_length=160); external_account_id=models.CharField(max_length=255,blank=True,db_index=True)
    platform_user_id=models.CharField(max_length=255,blank=True,db_index=True)
    username=models.CharField(max_length=255,blank=True)
    profile_picture_url=models.URLField(max_length=1000,blank=True)
    profile_url=models.URLField(max_length=1000,blank=True); display_name=models.CharField(max_length=160,blank=True); memo=models.TextField(blank=True)
    status=models.CharField(max_length=20,choices=Status.choices,default=Status.ACTIVE,db_index=True); token_expires_at=models.DateTimeField(null=True,blank=True)
    connection_status=models.CharField(max_length=24,choices=ConnectionStatus.choices,default=ConnectionStatus.NOT_CONNECTED,db_index=True)
    granted_scopes=models.JSONField(default=list,blank=True)
    connected_at=models.DateTimeField(null=True,blank=True)
    last_verified_at=models.DateTimeField(null=True,blank=True)
    created_at=models.DateTimeField(auto_now_add=True); updated_at=models.DateTimeField(auto_now=True)
    class Meta:
        constraints=[
            models.UniqueConstraint(fields=['platform','external_account_id'],condition=~models.Q(external_account_id=''),name='uniq_platform_account_external_id'),
            models.UniqueConstraint(fields=['platform','platform_user_id'],condition=~models.Q(platform_user_id=''),name='uniq_platform_user_id'),
            models.UniqueConstraint(fields=['platform','profile_url'],condition=~models.Q(profile_url=''),name='uniq_platform_account_profile_url'),
        ]
    def __str__(self): return f'{self.platform}:{self.account_name}'

    @property
    def connection_status_label(self):
        return {
            self.ConnectionStatus.CONNECTED: '연결됨',
            self.ConnectionStatus.NOT_CONNECTED: '연결 해제됨',
            self.ConnectionStatus.REAUTH_REQUIRED: '다시 연결 필요',
            self.ConnectionStatus.ERROR: '연결 오류',
            self.ConnectionStatus.CONNECTING: '연결 중',
            self.ConnectionStatus.PENDING: '연결 중',
        }.get(self.connection_status, '상태 확인 필요')

    @property
    def youtube_credential(self):
        return getattr(self, 'credential', None) if self.platform == self.Platform.YOUTUBE else None

    @property
    def youtube_oauth_status(self):
        credential = self.youtube_credential
        if not credential or credential.revoked_at:
            return 'DISCONNECTED'
        if credential.expires_at and credential.expires_at <= timezone.now() and not credential.refresh_token_encrypted:
            return 'EXPIRED'
        return credential.oauth_status

    @property
    def youtube_comments_enabled(self):
        credential = self.youtube_credential
        return bool(credential and 'https://www.googleapis.com/auth/youtube.force-ssl' in (credential.scopes or []))


class SocialCredential(models.Model):
    class OAuthStatus(models.TextChoices): CONNECTED='CONNECTED','Connected'; ERROR='ERROR','Error'
    social_account=models.OneToOneField(SocialAccount,on_delete=models.CASCADE,related_name='credential')
    provider=models.CharField(max_length=30,default='GOOGLE_YOUTUBE',db_index=True)
    access_token_encrypted=models.TextField()
    refresh_token_encrypted=models.TextField(blank=True)
    token_uri=models.URLField(max_length=500,default='https://oauth2.googleapis.com/token')
    scopes=models.JSONField(default=list)
    expires_at=models.DateTimeField(null=True,blank=True)
    channel_id=models.CharField(max_length=255,blank=True)
    channel_name=models.CharField(max_length=300,blank=True)
    channel_url=models.URLField(max_length=1000,blank=True)
    last_verified_at=models.DateTimeField(null=True,blank=True)
    oauth_status=models.CharField(max_length=20,choices=OAuthStatus.choices,default=OAuthStatus.CONNECTED,db_index=True)
    revoked_at=models.DateTimeField(null=True,blank=True)
    created_at=models.DateTimeField(auto_now_add=True); updated_at=models.DateTimeField(auto_now=True)

class Marketplace(models.Model):
    code=models.CharField(max_length=20,unique=True)
    name=models.CharField(max_length=120)
    active=models.BooleanField(default=True,db_index=True)
    sort_order=models.PositiveIntegerField(default=0)
    memo=models.TextField(blank=True)
    created_at=models.DateTimeField(auto_now_add=True); updated_at=models.DateTimeField(auto_now=True)
    class Meta: ordering=['sort_order','code']
    def __str__(self): return f'{self.code} | {self.name}'

class Linker(models.Model):
    class ItemType(models.TextChoices):
        PRODUCT = 'PRODUCT', '상품'
        RECIPE = 'RECIPE', '레시피'
        SERVICE = 'SERVICE', '서비스'

    owner=models.ForeignKey('auth.User',on_delete=models.CASCADE,related_name='linkers',null=True,blank=True)
    store=models.ForeignKey(CreatorStore,on_delete=models.CASCADE,related_name='linkers',null=True,blank=True)
    linker_code=models.CharField(max_length=40,unique=True,db_index=True,blank=True)
    linker_group_code=models.CharField(max_length=20,db_index=True,blank=True)
    destination_seq=models.PositiveIntegerField(default=1)
    item_type=models.CharField(max_length=20,choices=ItemType.choices,default=ItemType.PRODUCT,db_index=True)
    title=models.CharField(max_length=240,db_index=True); description=models.TextField(blank=True); status=models.CharField(max_length=20,default='ACTIVE',db_index=True)
    marketplace=models.ForeignKey(Marketplace,on_delete=models.PROTECT,related_name='linkers',null=True,blank=True)
    destination_url=models.URLField(max_length=1000,blank=True)
    product_no=models.CharField(max_length=40,blank=True,db_index=True)
    thumbnail_url=models.URLField(max_length=1000,blank=True)
    product_image=models.ImageField(upload_to='creator/products/',blank=True)
    store_category=models.ForeignKey(StoreCategory,on_delete=models.SET_NULL,related_name='products',null=True,blank=True)
    store_visible=models.BooleanField(default=True,db_index=True)
    display_order=models.PositiveIntegerField(default=0,db_index=True)
    memo=models.TextField(blank=True)
    created_at=models.DateTimeField(auto_now_add=True); updated_at=models.DateTimeField(auto_now=True)
    def save(self,*args,**kwargs):
        if not self.linker_code:
            with transaction.atomic():
                if self.linker_group_code:
                    last=Linker.objects.select_for_update().filter(linker_group_code=self.linker_group_code).aggregate(m=Max('destination_seq'))['m'] or 0
                    self.destination_seq=last + 1
                    self.linker_code=f'{self.linker_group_code}_{self.destination_seq:02d}'
                else:
                    last=Linker.objects.select_for_update().aggregate(m=Max('id'))['m'] or 0
                    self.linker_group_code=f'LK-{last+1:06d}'
                    self.destination_seq=1
                    self.linker_code=f'{self.linker_group_code}_{self.destination_seq:02d}'
        super().save(*args,**kwargs)
        if self.product_image and self.thumbnail_url != self.product_image.url:
            self.thumbnail_url = self.product_image.url
            super().save(update_fields=['thumbnail_url', 'updated_at'])

    @property
    def display_image_url(self):
        return self.product_image.url if self.product_image else self.thumbnail_url

    @property
    def public_query(self):
        return f'product={self.product_no}' if self.item_type == self.ItemType.PRODUCT else f'item={self.pk}'

    def __str__(self): return f'{self.linker_code} {self.title}'


class RecipeDetail(models.Model):
    linker = models.OneToOneField(Linker, on_delete=models.CASCADE, related_name='recipe_detail')
    ingredients = models.TextField()
    instructions = models.TextField()
    tips = models.TextField(blank=True)
    servings = models.CharField(max_length=80, blank=True)
    prep_time = models.CharField(max_length=80, blank=True)
    cook_time = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class ServiceDetail(models.Model):
    linker = models.OneToOneField(Linker, on_delete=models.CASCADE, related_name='service_detail')
    service_description = models.TextField(blank=True)
    price_info = models.TextField(blank=True)
    duration = models.CharField(max_length=120, blank=True)
    location_info = models.TextField(blank=True)
    contact_info = models.TextField(blank=True)
    booking_url = models.URLField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class ProductBlock(models.Model):
    owner = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='product_blocks')
    store = models.ForeignKey(CreatorStore, on_delete=models.CASCADE, related_name='product_blocks', null=True, blank=True)
    block_number = models.CharField(max_length=40, db_index=True)
    title = models.CharField(max_length=240)
    representative_image = models.ImageField(upload_to='creator/product-blocks/', blank=True)
    description = models.TextField(blank=True)
    store_category = models.ForeignKey(StoreCategory, on_delete=models.SET_NULL, related_name='product_blocks', null=True, blank=True)
    store_visible = models.BooleanField(default=True, db_index=True)
    display_order = models.PositiveIntegerField(default=0, db_index=True)
    active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner', 'block_number'], name='uniq_product_block_owner_number')]

    @property
    def display_image_url(self):
        if self.representative_image:
            return self.representative_image.url
        first_item = next(iter(self.items.all()), None)
        return first_item.linker.display_image_url if first_item else ''


class ProductBlockItem(models.Model):
    product_block = models.ForeignKey(ProductBlock, on_delete=models.CASCADE, related_name='items')
    linker = models.ForeignKey(Linker, on_delete=models.CASCADE, related_name='product_block_items')
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['product_block', 'linker'], name='uniq_product_block_item')]
        ordering = ['sort_order', 'id']


class StoreEvent(models.Model):
    class EventType(models.TextChoices):
        STORE_VIEW = 'STORE_VIEW', 'Store view'
        PRODUCT_VIEW = 'PRODUCT_VIEW', 'Product view'
        BLOCK_VIEW = 'BLOCK_VIEW', 'Block view'
        OUTBOUND_CLICK = 'OUTBOUND_CLICK', 'Outbound click'

    store = models.ForeignKey(CreatorStore, on_delete=models.CASCADE, related_name='events')
    event_type = models.CharField(max_length=30, choices=EventType.choices, db_index=True)
    linker = models.ForeignKey(Linker, on_delete=models.SET_NULL, related_name='store_events', null=True, blank=True)
    product_block = models.ForeignKey(ProductBlock, on_delete=models.SET_NULL, related_name='store_events', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']

class SocialContent(models.Model):
    class ContentType(models.TextChoices): IMAGE='IMAGE','Image'; CAROUSEL='CAROUSEL','Carousel'; REEL='REEL','Reel'; SHORTS='SHORTS','Shorts'; VIDEO='VIDEO','Video'
    social_account=models.ForeignKey(SocialAccount,on_delete=models.CASCADE,related_name='contents'); linker=models.ForeignKey(Linker,on_delete=models.SET_NULL,related_name='contents',null=True,blank=True)
    platform=models.CharField(max_length=20,choices=SocialAccount.Platform.choices,db_index=True); content_type=models.CharField(max_length=20,choices=ContentType.choices,db_index=True)
    external_content_id=models.CharField(max_length=255,db_index=True); title=models.CharField(max_length=300,blank=True,db_index=True); caption=models.TextField(blank=True); content_url=models.URLField(max_length=1000,blank=True); thumbnail_url=models.URLField(max_length=1000,blank=True)
    published_at=models.DateTimeField(null=True,blank=True,db_index=True); status=models.CharField(max_length=20,default='ACTIVE',db_index=True); metadata=models.JSONField(default=dict,blank=True); created_at=models.DateTimeField(auto_now_add=True); updated_at=models.DateTimeField(auto_now=True)
    class Meta: constraints=[models.UniqueConstraint(fields=['platform','external_content_id'],name='uniq_platform_content')]

class SocialContentLink(models.Model):
    social_content=models.ForeignKey(SocialContent,on_delete=models.CASCADE,related_name='product_links')
    linker=models.ForeignKey(Linker,on_delete=models.CASCADE,related_name='content_links')
    display_order=models.PositiveIntegerField(default=0)
    response_code=models.CharField(max_length=40,blank=True)
    active=models.BooleanField(default=True,db_index=True)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['social_content','linker'],name='uniq_social_content_linker')]
        ordering=['display_order','id']


class MessageType(models.Model):
    code = models.CharField(max_length=80, unique=True)
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    active = models.BooleanField(default=True, db_index=True)
    display_order = models.PositiveIntegerField(default=0, db_index=True)
    input_schema = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['display_order', 'id']

    def clean(self):
        from linker.services.message_templates import validate_input_schema
        validate_input_schema(self.input_schema)

    def __str__(self):
        return self.name


class MessageTemplate(models.Model):
    class OwnerType(models.TextChoices):
        SYSTEM = 'SYSTEM', 'System'
        CREATOR = 'CREATOR', 'Creator'

    owner_type = models.CharField(max_length=20, choices=OwnerType.choices, default=OwnerType.SYSTEM, db_index=True)
    creator = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='message_templates', null=True, blank=True)
    message_type = models.ForeignKey(MessageType, on_delete=models.PROTECT, related_name='templates')
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    body_template = models.TextField()
    active = models.BooleanField(default=True, db_index=True)
    is_default = models.BooleanField(default=False)
    is_recommended = models.BooleanField(default=False)
    source_template = models.ForeignKey('self', on_delete=models.SET_NULL, related_name='copies', null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    display_order = models.PositiveIntegerField(default=0, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['display_order', 'id']

    def clean(self):
        from linker.services.message_templates import validate_template_variables
        if self.owner_type == self.OwnerType.SYSTEM and self.creator_id:
            raise ValidationError('SYSTEM template cannot have a creator.')
        if self.owner_type == self.OwnerType.CREATOR and not self.creator_id:
            raise ValidationError('CREATOR template requires a creator.')
        validate_template_variables(self.body_template)

    def __str__(self):
        return self.name

class ResponseRule(models.Model):
    class Mode(models.TextChoices): ALL='ALL','All'; KEYWORD='KEYWORD','Keyword'; MANUAL='MANUAL','Manual'; OFF='OFF','Off'
    class Scope(models.TextChoices): STORE='STORE','Store'; ACCOUNT='ACCOUNT','Account'; CONTENT='CONTENT','Content'
    class TargetType(models.TextChoices): PRODUCT='PRODUCT','Product'; PRODUCT_BLOCK='PRODUCT_BLOCK','Product block'; STORE='STORE','Store'
    class LinkMode(models.TextChoices): LINK_STORE='LINK_STORE','Link Store'; DIRECT='DIRECT','Direct'
    scope=models.CharField(max_length=20,choices=Scope.choices,default=Scope.CONTENT,db_index=True)
    store=models.ForeignKey(CreatorStore,on_delete=models.CASCADE,related_name='scope_response_rules',null=True,blank=True)
    social_account=models.ForeignKey(SocialAccount,on_delete=models.CASCADE,related_name='scope_response_rules',null=True,blank=True)
    social_content=models.OneToOneField(SocialContent,on_delete=models.CASCADE,related_name='response_rule',null=True,blank=True); response_mode=models.CharField(max_length=20,choices=Mode.choices,default=Mode.OFF,db_index=True)
    target_type=models.CharField(max_length=20,choices=TargetType.choices,default=TargetType.STORE); target_linker=models.ForeignKey(Linker,on_delete=models.SET_NULL,null=True,blank=True,related_name='response_rules'); target_product_block=models.ForeignKey(ProductBlock,on_delete=models.SET_NULL,null=True,blank=True,related_name='response_rules'); target_store=models.ForeignKey(CreatorStore,on_delete=models.SET_NULL,null=True,blank=True,related_name='response_rules')
    public_reply_enabled=models.BooleanField(default=False); private_reply_enabled=models.BooleanField(default=False); public_reply_text=models.TextField(blank=True); test_private_reply_text=models.TextField(default='Content Linker 자동응답 테스트입니다.'); link_mode=models.CharField(max_length=20,choices=LinkMode.choices,default=LinkMode.LINK_STORE); direct_url=models.URLField(max_length=1000,blank=True); include_link_store=models.BooleanField(default=True); enabled=models.BooleanField(default=True); starts_at=models.DateTimeField(null=True,blank=True); ends_at=models.DateTimeField(null=True,blank=True)
    message_template=models.ForeignKey(MessageTemplate,on_delete=models.SET_NULL,related_name='response_rules',null=True,blank=True)
    rendered_message_snapshot=models.TextField(blank=True)
    message_config=models.JSONField(default=dict,blank=True)
    created_at=models.DateTimeField(auto_now_add=True); updated_at=models.DateTimeField(auto_now=True)

class ResponseKeyword(models.Model):
    response_rule=models.ForeignKey(ResponseRule,on_delete=models.CASCADE,related_name='keywords'); keyword=models.CharField(max_length=160,db_index=True); match_type=models.CharField(max_length=20,default='CONTAINS'); enabled=models.BooleanField(default=True); is_exclusion=models.BooleanField(default=False); target_type=models.CharField(max_length=20,choices=ResponseRule.TargetType.choices,default=ResponseRule.TargetType.STORE); target_linker=models.ForeignKey(Linker,on_delete=models.SET_NULL,null=True,blank=True,related_name='response_keyword_targets'); target_product_block=models.ForeignKey(ProductBlock,on_delete=models.SET_NULL,null=True,blank=True,related_name='response_keyword_block_targets'); target_store=models.ForeignKey(CreatorStore,on_delete=models.SET_NULL,null=True,blank=True,related_name='response_keyword_store_targets'); link_mode=models.CharField(max_length=20,choices=ResponseRule.LinkMode.choices,default=ResponseRule.LinkMode.LINK_STORE); direct_url=models.URLField(max_length=1000,blank=True); include_link_store=models.BooleanField(default=True)

class SocialComment(models.Model):
    class Decision(models.TextChoices): PENDING='PENDING','Pending'; MATCHED='MATCHED','Matched'; NOT_MATCHED='NOT_MATCHED','Not matched'; MANUAL='MANUAL','Manual'; EXCLUDED='EXCLUDED','Excluded'
    class Processing(models.TextChoices): PENDING='PENDING','Pending'; PROCESSING='PROCESSING','Processing'; SUCCESS='SUCCESS','Success'; FAILED='FAILED','Failed'
    social_content=models.ForeignKey(SocialContent,on_delete=models.CASCADE,related_name='comments'); external_comment_id=models.CharField(max_length=255,unique=True,db_index=True); external_user_id=models.CharField(max_length=255,blank=True,db_index=True); username=models.CharField(max_length=255,blank=True,db_index=True); comment_text=models.TextField()
    commented_at=models.DateTimeField(null=True,blank=True,db_index=True); received_at=models.DateTimeField(auto_now_add=True); decision_status=models.CharField(max_length=20,choices=Decision.choices,default=Decision.PENDING,db_index=True); processing_status=models.CharField(max_length=20,choices=Processing.choices,default=Processing.PENDING,db_index=True); metadata=models.JSONField(default=dict,blank=True)
    class Meta: ordering=['processing_status','-received_at']

class ResponseLog(models.Model):
    class Type(models.TextChoices): PUBLIC_REPLY='PUBLIC_REPLY','Public reply'; PRIVATE_REPLY='PRIVATE_REPLY','Private reply'
    response_rule=models.ForeignKey(ResponseRule,on_delete=models.SET_NULL,null=True,blank=True,related_name='response_logs')
    social_comment=models.ForeignKey(SocialComment,on_delete=models.CASCADE,related_name='response_logs'); response_type=models.CharField(max_length=20,choices=Type.choices,db_index=True); response_external_id=models.CharField(max_length=255,blank=True); status=models.CharField(max_length=20,default='PENDING',db_index=True); error_code=models.CharField(max_length=100,blank=True); error_message=models.TextField(blank=True); attempted_at=models.DateTimeField(auto_now_add=True); completed_at=models.DateTimeField(null=True,blank=True)
    class Meta: constraints=[models.UniqueConstraint(fields=['social_comment','response_type'],condition=models.Q(status='SUCCESS'),name='uniq_success_response_type'),models.UniqueConstraint(fields=['social_comment','response_rule','response_type'],name='uniq_response_rule_attempt')]
