from urllib.parse import urlencode

from django import template

register = template.Library()


@register.simple_tag
def querystring_with(request, **kwargs):
    params = request.GET.copy()
    for key, value in kwargs.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    return urlencode(params, doseq=True)


@register.filter
def split_csv(value, delimiter=","):
    if not value:
        return []
    return [item.strip() for item in str(value).split(delimiter)]
