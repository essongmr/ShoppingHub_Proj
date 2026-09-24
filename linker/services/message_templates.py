import re
from django.core.exceptions import ValidationError

from linker.models import MessageTemplate, MessageType, ResponseRule


ALLOWED_FIELD_TYPES = {'text', 'textarea', 'url', 'select', 'checkbox', 'product_select', 'store_url', 'multi_product'}
ALLOWED_VARIABLES = {
    'product_name', 'product_url', 'product_no', 'product_image', 'store_name', 'store_url', 'intro',
    'recipe_name', 'recipe_url', 'service_name', 'service_url', 'button_text',
}
_VARIABLE_PATTERN = re.compile(r'\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}')


def validate_input_schema(schema):
    if schema in (None, {}):
        return {'fields': []}
    if not isinstance(schema, dict) or not isinstance(schema.get('fields'), list):
        raise ValidationError('input_schema must contain a fields list.')
    keys = set()
    for field in schema['fields']:
        if not isinstance(field, dict):
            raise ValidationError('Each schema field must be an object.')
        key = field.get('key')
        if not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', key):
            raise ValidationError('Schema field keys must use lowercase snake_case.')
        if key in keys:
            raise ValidationError(f'Duplicate schema field key: {key}')
        keys.add(key)
        if not isinstance(field.get('label'), str) or not field['label'].strip():
            raise ValidationError(f'Schema field label is required: {key}')
        if field.get('type') not in ALLOWED_FIELD_TYPES:
            raise ValidationError(f'Unknown schema field type: {field.get("type")}')
        if not isinstance(field.get('required', False), bool):
            raise ValidationError(f'Schema required must be boolean: {key}')
        if not isinstance(field.get('display_order', 0), int):
            raise ValidationError(f'Schema display_order must be an integer: {key}')
    return schema


def template_variables(body_template):
    return set(_VARIABLE_PATTERN.findall(body_template or ''))


def validate_template_variables(body_template):
    unknown = template_variables(body_template) - ALLOWED_VARIABLES
    if unknown:
        raise ValidationError(f'Unknown template variables: {", ".join(sorted(unknown))}')
    return True


def validate_message_config(message_type, config):
    schema = validate_input_schema(message_type.input_schema)
    config = config or {}
    fields = {field['key']: field for field in schema.get('fields', [])}
    if not fields:
        return config
    unknown = set(config) - set(fields) - ALLOWED_VARIABLES
    if unknown:
        raise ValidationError(f'Unknown message config fields: {", ".join(sorted(unknown))}')
    missing = [key for key, field in fields.items() if field.get('required') and config.get(key) in (None, '', [])]
    if missing:
        raise ValidationError(f'Required message fields are missing: {", ".join(missing)}')
    return config


def get_system_templates(message_type=None, *, active_only=True):
    query = MessageTemplate.objects.filter(owner_type=MessageTemplate.OwnerType.SYSTEM)
    if message_type:
        query = query.filter(message_type=message_type)
    if active_only:
        query = query.filter(active=True, message_type__active=True)
    return query.select_related('message_type').order_by('display_order', 'id')


def get_creator_templates(creator, message_type=None, *, active_only=True):
    query = MessageTemplate.objects.filter(owner_type=MessageTemplate.OwnerType.CREATOR, creator=creator)
    if message_type:
        query = query.filter(message_type=message_type)
    if active_only:
        query = query.filter(active=True, message_type__active=True)
    return query.select_related('message_type', 'source_template').order_by('display_order', 'id')


def render_template(template, config=None):
    validate_template_variables(template.body_template)
    validate_message_config(template.message_type, config or {})
    values = {key: str(value) for key, value in (config or {}).items()}
    values.setdefault('store_name', '')
    values.setdefault('store_url', '')
    values.setdefault('product_name', '')
    values.setdefault('product_url', '')
    values.setdefault('product_no', '')
    values.setdefault('product_image', '')
    values.setdefault('intro', '')
    values.setdefault('recipe_name', '')
    values.setdefault('recipe_url', '')
    values.setdefault('service_name', '')
    values.setdefault('service_url', '')
    values.setdefault('button_text', '')
    return _VARIABLE_PATTERN.sub(lambda match: values.get(match.group(1), ''), template.body_template)


def build_preview(template, config=None):
    return render_template(template, config or {})


def clone_system_template_for_creator(template, creator):
    if template.owner_type != MessageTemplate.OwnerType.SYSTEM or not template.active:
        raise ValidationError('Only active SYSTEM templates can be copied.')
    return MessageTemplate.objects.create(
        owner_type=MessageTemplate.OwnerType.CREATOR,
        creator=creator,
        message_type=template.message_type,
        name=template.name,
        description=template.description,
        body_template=template.body_template,
        active=True,
        source_template=template,
        version=1,
    )


def clone_creator_template(template, creator):
    if template.owner_type != MessageTemplate.OwnerType.CREATOR or template.creator_id != creator.pk:
        raise ValidationError('You can only copy your own templates.')
    return MessageTemplate.objects.create(
        owner_type=MessageTemplate.OwnerType.CREATOR,
        creator=creator,
        message_type=template.message_type,
        name=f'{template.name} 복사본',
        description=template.description,
        body_template=template.body_template,
        active=True,
        source_template=template.source_template or template,
        version=1,
    )


def snapshot_template_for_rule(rule, template, config=None):
    owner_id = None
    if rule.social_content_id:
        owner_id = rule.social_content.social_account.owner_id
    elif rule.social_account_id:
        owner_id = rule.social_account.owner_id
    elif rule.store_id:
        owner_id = rule.store.owner_id
    if template.owner_type == MessageTemplate.OwnerType.CREATOR and template.creator_id != owner_id:
        raise ValidationError('Template owner does not match the rule owner.')
    config = validate_message_config(template.message_type, config or {})
    rule.message_template = template
    rule.message_config = config
    rule.rendered_message_snapshot = render_template(template, config)
    rule.test_private_reply_text = rule.rendered_message_snapshot
    rule.save(update_fields=['message_template', 'message_config', 'rendered_message_snapshot', 'test_private_reply_text', 'updated_at'])
    return rule
