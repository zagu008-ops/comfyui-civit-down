"""Normalize Civitai's decimal-bitmask scope, including OAuth library arrays."""


def scope_bitmask(value):
    values = value if isinstance(value, list) else [value]
    if not values:
        raise ValueError('授权返回的 scope 为空')
    result = 0
    for item in values:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise ValueError('授权返回的 scope 格式无效')
        if isinstance(item, str) and (not item.isascii() or not item.isdecimal()):
            raise ValueError('授权返回的 scope 必须是十进制权限值')
        number = int(item)
        if number < 0:
            raise ValueError('授权返回的 scope 不能为负数')
        result |= number
    return result
