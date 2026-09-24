from django.core.management.base import BaseCommand, CommandError

from linker.models import MessageTemplate, MessageType


class Command(BaseCommand):
    help = 'Create the standard MessageType and SYSTEM template content.'

    def add_arguments(self, parser):
        parser.add_argument('--content-v1', action='store_true', help='Seed the operational Content V1 set.')

    def _content_v1(self):
        return [
            ('RECIPE_INFO', '레시피 안내', '레시피, 재료, 만드는 방법 등을 안내할 때 사용합니다.', {'fields': [{'key': 'recipe_name', 'label': '레시피 이름', 'type': 'text', 'required': True, 'display_order': 10, 'auto_source': 'selected_item.title'}]}, [
                ('기본 레시피 안내', '요청하신 {{ recipe_name }} 레시피예요 🍽️\n재료와 만드는 방법을 정리해두었어요.\n아래에서 자세히 확인해보세요.', True, True),
                ('친근한 레시피 안내', '궁금하셨던 {{ recipe_name }} 레시피 보내드려요 😊\n필요한 재료와 만드는 방법을 한 번에 정리해두었어요.\n아래에서 확인해보세요.', False, True),
                ('짧은 레시피 안내', '{{ recipe_name }} 레시피예요.\n재료와 만드는 방법은 아래에서 확인해보세요.', False, False),
            ]),
            ('PRODUCT_INFO', '상품 안내', '상품 정보와 관련 구매 정보를 안내할 때 사용합니다.', {'fields': [{'key': 'product_name', 'label': '상품명', 'type': 'text', 'required': True, 'display_order': 10, 'auto_source': 'selected_item.title'}]}, [
                ('기본 상품 안내', '요청하신 {{ product_name }} 정보예요.\n상품 정보와 자세한 내용은 아래에서 확인해보세요.', True, True),
                ('친근한 상품 안내', '궁금하셨던 {{ product_name }} 정보 보내드려요 😊\n자세한 내용은 아래에서 확인해보세요.', False, True),
                ('짧은 상품 안내', '{{ product_name }} 정보예요.\n아래에서 바로 확인해보세요.', False, False),
            ]),
            ('SERVICE_INFO', '서비스 안내', '서비스 내용, 이용 안내, 상담·예약 정보를 안내할 때 사용합니다.', {'fields': [{'key': 'service_name', 'label': '서비스명', 'type': 'text', 'required': True, 'display_order': 10, 'auto_source': 'selected_item.title'}]}, [
                ('기본 서비스 안내', '요청하신 {{ service_name }} 안내예요.\n이용 방법과 자세한 내용을 아래에서 확인해보세요.', True, True),
                ('상담 안내형', '궁금하셨던 {{ service_name }} 정보를 정리해두었어요.\n이용 안내와 상담·예약 정보는 아래에서 확인해보세요.', False, True),
                ('짧은 서비스 안내', '{{ service_name }} 안내예요.\n자세한 내용은 아래에서 확인해보세요.', False, False),
            ]),
            ('STORE_INFO', '스토어 안내', 'ShoppingHub 스토어의 상품과 콘텐츠를 안내할 때 사용합니다.', {'fields': []}, [
                ('기본 스토어 안내', '관련 상품과 정보를 {{ store_name }} 스토어에 정리해두었어요.\n아래에서 한 번에 확인해보세요.', True, True),
                ('둘러보기형', '더 궁금한 상품과 정보는 {{ store_name }} 스토어에서 확인하실 수 있어요.\n아래에서 편하게 둘러보세요.', False, False),
            ]),
            ('DIRECT', '직접 작성', '정해진 표준 메시지 대신 직접 메시지를 작성합니다.', {'fields': []}, []),
        ]

    def handle(self, *args, **options):
        if options.get('content_v1'):
            for index, (code, name, description, schema, templates) in enumerate(self._content_v1(), start=1):
                message_type, _ = MessageType.objects.get_or_create(code=code, defaults={'name': name, 'description': description, 'input_schema': schema, 'display_order': index})
                for order, (template_name, body, is_default, is_recommended) in enumerate(templates, start=1):
                    MessageTemplate.objects.get_or_create(owner_type=MessageTemplate.OwnerType.SYSTEM, creator=None, message_type=message_type, name=template_name, defaults={'description': description, 'body_template': body, 'is_default': is_default, 'is_recommended': is_recommended, 'display_order': order})
            self.stdout.write(self.style.SUCCESS('Message Content V1 seeded.'))
            return

        raise CommandError(
            'Seed set을 명시해야 합니다. '
            '현재 canonical seed는 --content-v1 입니다.'
        )
