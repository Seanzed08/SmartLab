from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    if dictionary is None:
        return None
    return dictionary.get(key)
@register.filter
def short_name(full_name: str) -> str:
    """
    'John Michael Smith' -> 'J. Smith'
    'Maria' -> 'Maria'
    Handles extra spaces safely.
    """
    if not full_name:
        return ""
    parts = full_name.strip().split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].title()
    first, last = parts[0], parts[-1]
    return f"{first[0].upper()}. {last.title()}"