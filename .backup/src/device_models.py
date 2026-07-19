"""iPhone model detection and logical screen sizes.

Maps Apple ProductType identifiers to a friendly device name and the
logical screen size in points (portrait orientation).
"""

_MODELS: dict[str, tuple[str, int, int]] = {
    "iPhone14,4": ("iPhone 13 mini", 375, 812),
    "iPhone14,5": ("iPhone 13", 390, 844),
    "iPhone14,2": ("iPhone 13 Pro", 390, 844),
    "iPhone14,3": ("iPhone 13 Pro Max", 428, 926),
    "iPhone14,6": ("iPhone SE (3rd gen)", 375, 667),
    "iPhone14,7": ("iPhone 14", 390, 844),
    "iPhone14,8": ("iPhone 14 Plus", 428, 926),
    "iPhone15,2": ("iPhone 14 Pro", 393, 852),
    "iPhone15,3": ("iPhone 14 Pro Max", 430, 932),
    "iPhone15,4": ("iPhone 15", 393, 852),
    "iPhone15,5": ("iPhone 15 Plus", 430, 932),
    "iPhone16,1": ("iPhone 15 Pro", 393, 852),
    "iPhone16,2": ("iPhone 15 Pro Max", 430, 932),
    "iPhone17,3": ("iPhone 16", 393, 852),
    "iPhone17,4": ("iPhone 16 Plus", 430, 932),
    "iPhone17,1": ("iPhone 16 Pro", 402, 874),
    "iPhone17,2": ("iPhone 16 Pro Max", 440, 956),
    "iPhone17,5": ("iPhone 16e", 390, 844),
    "iPhone18,3": ("iPhone 17", 402, 874),
    "iPhone18,1": ("iPhone 17 Pro", 402, 874),
    "iPhone18,2": ("iPhone 17 Pro Max", 440, 956),
    "iPhone18,4": ("iPhone Air", 402, 874),
}


def lookup(product_type: str) -> tuple[str, int, int] | None:
    return _MODELS.get(product_type)


def friendly_name(product_type: str) -> str:
    m = lookup(product_type)
    return m[0] if m else product_type


def screen_size(product_type: str) -> tuple[int, int] | None:
    m = lookup(product_type)
    return (m[1], m[2]) if m else None
