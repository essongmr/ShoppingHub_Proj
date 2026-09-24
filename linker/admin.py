from django.contrib import admin
from django.utils.html import format_html
from django.core.exceptions import ValidationError
from .models import *


@admin.register(MessageType)
class MessageTypeAdmin(admin.ModelAdmin):
	list_display = ('code', 'name', 'active', 'display_order', 'updated_at')
	list_filter = ('active',)
	ordering = ('display_order', 'id')
	search_fields = ('code', 'name')

	def save_model(self, request, obj, form, change):
		obj.full_clean()
		super().save_model(request, obj, form, change)


@admin.register(MessageTemplate)
class MessageTemplateAdmin(admin.ModelAdmin):
	list_display = ('name', 'owner_type', 'creator', 'message_type', 'active', 'is_default', 'is_recommended', 'version', 'display_order', 'updated_at')
	list_filter = ('owner_type', 'message_type', 'active', 'is_recommended')
	search_fields = ('name', 'creator__username', 'message_type__name')
	ordering = ('message_type', 'display_order', 'id')
	autocomplete_fields = ('creator', 'message_type', 'source_template')
	actions = ('clone_system_templates',)
	readonly_fields = ('preview',)

	@admin.display(description='미리보기')
	def preview(self, obj):
		if not obj or not obj.body_template:
			return '-'
		return format_html('<pre style="white-space:pre-wrap;max-width:720px">{}</pre>', obj.body_template)

	@admin.action(description='선택한 SYSTEM 템플릿 복제')
	def clone_system_templates(self, request, queryset):
		created = 0
		for template in queryset.filter(owner_type=MessageTemplate.OwnerType.SYSTEM):
			MessageTemplate.objects.create(
				owner_type=MessageTemplate.OwnerType.SYSTEM,
				message_type=template.message_type,
				name=f'{template.name} 복사본',
				description=template.description,
				body_template=template.body_template,
				active=False,
				version=template.version + 1,
				source_template=template,
			)
			created += 1
		self.message_user(request, f'{created}개 템플릿을 복제했습니다.')

	def save_model(self, request, obj, form, change):
		obj.full_clean()
		super().save_model(request, obj, form, change)


@admin.register(Linker)
class LinkerAdmin(admin.ModelAdmin):
	list_display = ('title', 'item_type', 'owner', 'store', 'status', 'store_visible', 'updated_at')
	list_filter = ('item_type', 'status', 'store_visible')
	search_fields = ('title', 'product_no', 'linker_code')


for m in [CreatorProfile,CreatorStore,StoreCategory,StoreEvent,SocialAccount,SocialCredential,Marketplace,ProductBlock,ProductBlockItem,SocialContent,SocialContentLink,ResponseRule,ResponseKeyword,SocialComment,ResponseLog,RecipeDetail,ServiceDetail]: admin.site.register(m)


