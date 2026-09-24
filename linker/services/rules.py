import re
from django.utils import timezone
from linker.models import ResponseRule, SocialComment

def norm(s): return re.sub(r'\s+',' ',(s or '').casefold()).strip()
def _rule_matches(rule, comment):
    now = timezone.now()
    if not rule.enabled or rule.response_mode in {ResponseRule.Mode.OFF, ResponseRule.Mode.MANUAL}:
        return False
    if rule.starts_at and now < rule.starts_at:
        return False
    if rule.ends_at and now > rule.ends_at:
        return False
    text = norm(comment.comment_text)
    keywords = list(rule.keywords.filter(enabled=True))
    def matches(keyword):
        value = norm(keyword.keyword)
        return text == value if keyword.match_type == 'EXACT' else value in text
    if any(keyword.is_exclusion and matches(keyword) for keyword in keywords):
        return False
    if rule.response_mode == ResponseRule.Mode.ALL:
        return True
    return any(not keyword.is_exclusion and matches(keyword) for keyword in keywords)


def select_response_rule(comment):
    content = comment.social_content
    account = content.social_account
    store_id = account.store_id
    candidates = []
    if content:
        candidates.extend(ResponseRule.objects.filter(scope=ResponseRule.Scope.CONTENT, social_content=content).prefetch_related('keywords'))
    candidates.extend(ResponseRule.objects.filter(scope=ResponseRule.Scope.ACCOUNT, social_account=account).prefetch_related('keywords'))
    if store_id:
        candidates.extend(ResponseRule.objects.filter(scope=ResponseRule.Scope.STORE, store_id=store_id).prefetch_related('keywords'))
    for rule in candidates:
        if _rule_matches(rule, comment):
            return rule
    return None


def decide(comment):
    rule = select_response_rule(comment)
    if not rule: return SocialComment.Decision.NOT_MATCHED
    return SocialComment.Decision.MATCHED


def matched_keyword(comment):
    rule = select_response_rule(comment)
    if not rule: return None
    text = norm(comment.comment_text)
    return next((kw for kw in rule.keywords.filter(enabled=True) if (kw.match_type == 'EXACT' and text == norm(kw.keyword)) or (kw.match_type != 'EXACT' and norm(kw.keyword) in text)), None)


def rule_target(rule, comment=None):
    keyword = matched_keyword(comment) if comment else None
    if keyword:
        return keyword.target_type, keyword.target_linker or keyword.target_product_block or keyword.target_store
    target = rule.target_linker or rule.target_product_block or rule.target_store
    return rule.target_type, target


def rule_delivery_target(rule, comment):
    keyword = matched_keyword(comment)
    source = keyword or rule
    target = source.target_linker or source.target_product_block or source.target_store
    return (
        source.target_type,
        target,
        source.link_mode,
        source.direct_url,
        source.include_link_store,
    )
