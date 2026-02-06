# core/network.py
def vht80_center(channel: int) -> int:
    if 36 <= channel <= 48:
        return 42
    if 149 <= channel <= 161:
        return 155
    raise ValueError("Unsupported 80MHz channel")
