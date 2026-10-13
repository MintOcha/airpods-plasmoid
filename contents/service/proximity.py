"""Apple "Proximity Pairing" BLE advertisements (manufacturer 0x004C, type 0x07).

AirPods advertise these while out of the case or with the lid open, even when not
connected to this computer. The address is a resolvable private address, matched
with the identity resolving key (IRK) the pods hand out over AAP; the encryption
key from the same exchange unlocks exact battery levels.
"""

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

APPLE = 0x004C
TYPE_PROXIMITY = 0x07
UNKNOWN_LEVEL = 0x7F
NO_LEVEL = 0x0F


def _aes(key, block, decrypt=False):
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    op = cipher.decryptor() if decrypt else cipher.encryptor()
    return op.update(block) + op.finalize()


def matches(address, irk_hex):
    """True if a resolvable private address was generated from this IRK."""
    raw = bytes.fromhex(address.replace(':', ''))
    if len(raw) != 6 or raw[0] >> 6 != 0b01:
        return False
    prand, hash_ = raw[:3], raw[3:]
    return _aes(bytes.fromhex(irk_hex)[::-1], bytes(13) + prand)[-3:] == hash_


def parse(data, enc_hex=None):
    """Decode manufacturer data; with the device key the levels are exact."""
    if len(data) < 11 or data[0] != TYPE_PROXIMITY or data[2] == 0x00:
        return None
    status, pods, flags_case, lid = data[5], data[6], data[7], data[8]
    left_primary = bool(status & 0x20)
    in_case = bool(status & 0x40)
    flags = flags_case >> 4

    def nibble(v):
        return None if v == NO_LEVEL else min(100, v * 10)

    first, second = nibble(pods >> 4), nibble(pods & 0x0F)
    left, right = (second, first) if left_primary else (first, second)
    charging = {'left': bool(flags & (0x01 if left_primary else 0x02)),
                'right': bool(flags & (0x02 if left_primary else 0x01)),
                'case': bool(flags & 0x04)}
    levels = {'left': left, 'right': right, 'case': nibble(flags_case & 0x0F)}
    exact = False

    if enc_hex and len(data) >= 27:
        plain = _aes(bytes.fromhex(enc_hex), bytes(data[-16:]), decrypt=True)
        order = ('left', 'right') if left_primary else ('right', 'left')
        for name, byte in zip(order + ('case',), plain[1:4]):
            if byte & 0x7F != UNKNOWN_LEVEL:
                levels[name] = byte & 0x7F
                charging[name] = bool(byte & 0x80)
        exact = True

    return {
        'battery': {k: {'level': v, 'charging': charging[k]} for k, v in levels.items() if v is not None},
        'exact': exact,
        'lid_open': in_case and not (lid >> 3) & 1,
        'in_case': in_case,
    }
