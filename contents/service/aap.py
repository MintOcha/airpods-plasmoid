"""Apple Accessory Protocol (AAP) spoken by AirPods over L2CAP PSM 0x1001.

Packet layouts follow the LibrePods reverse-engineering notes and were
verified against AirPods Pro 2 (USB-C) firmware 81.2675.
"""

import struct

PSM = 0x1001
SERVICE_UUID = '74ec2172-0bad-4d01-8f77-997b2be0722a'

HEADER = b'\x04\x00\x04\x00'
HANDSHAKE = bytes.fromhex('00000400010002000000000000000000')
HANDSHAKE_ACK = bytes.fromhex('01000400')
SET_FEATURES = bytes.fromhex('040004004d00d700000000000000')
REQUEST_NOTIFICATIONS = bytes.fromhex('040004000f00ffffffffff')
REQUEST_PROXIMITY_KEYS = bytes.fromhex('0400040030000500')
HEAD_TRACKING_START = bytes.fromhex('040004001700000010000f000873420b081010021a0501409c0000')
HEAD_TRACKING_STOP = bytes.fromhex('040004001700000010000f000875420b081010021a050100000000')

OP_BATTERY = 0x04
OP_EARS = 0x06
OP_CONTROL = 0x09
OP_HEAD_TRACKING = 0x17
OP_METADATA = 0x1D
OP_FEATURES_ACK = 0x2B
OP_CONVERSATION = 0x4B
OP_PROXIMITY_KEYS = 0x31

COMPONENTS = {0x01: 'headset', 0x02: 'right', 0x04: 'left', 0x08: 'case'}
BATTERY_CHARGING = 0x01
BATTERY_DISCONNECTED = 0x04
EAR_STATES = {0x00: 'in', 0x01: 'out', 0x02: 'case'}
# Bluetooth product IDs (Modalias "bluetooth:v004Cp<ID>...").
MODELS = {0x2002: 'AirPods', 0x200F: 'AirPods (2nd generation)', 0x2013: 'AirPods (3rd generation)',
          0x2019: 'AirPods 4', 0x201B: 'AirPods 4 (ANC)', 0x200E: 'AirPods Pro',
          0x2014: 'AirPods Pro 2', 0x2024: 'AirPods Pro 2 (USB-C)', 0x200A: 'AirPods Max',
          0x201F: 'AirPods Max (USB-C)'}


def control(ident, values):
    """Control packets always carry 4 value bytes; unused ones are zero."""
    return HEADER + bytes([OP_CONTROL, 0, ident]) + bytes((list(values) + [0, 0, 0, 0])[:4])


def parse(packet):
    """Return (kind, payload) for packets the service consumes, else None."""
    if packet.startswith(HANDSHAKE_ACK):
        return 'handshake', None
    if not packet.startswith(HEADER) or len(packet) < 6:
        return None
    op = packet[4] | packet[5] << 8
    body = packet[6:]

    if op == OP_FEATURES_ACK:
        return 'features', None
    if op == OP_CONTROL and len(body) >= 5:
        return 'control', (body[0], list(body[1:5]))
    if op == OP_BATTERY and body and len(body) == 1 + 5 * body[0]:
        return 'battery', _battery(body)
    if op == OP_EARS and len(body) >= 2:
        return 'ears', (EAR_STATES.get(body[0]), EAR_STATES.get(body[1]))
    if op == OP_HEAD_TRACKING and len(packet) > 60 and packet[10] in (0x44, 0x45):
        return 'orientation', _orientation(packet)
    if op == OP_CONVERSATION and len(body) >= 4:
        return 'conversation', body[3]
    if op == OP_METADATA and len(packet) > 11:
        fields = packet[11:].split(b'\0')
        return 'metadata', {'name': fields[0].decode(errors='replace'),
                            'model_number': fields[1].decode(errors='replace') if len(fields) > 1 else '',
                            'firmware': fields[4].decode(errors='replace') if len(fields) > 4 else ''}
    if op == OP_PROXIMITY_KEYS and len(packet) >= 47:
        return 'keys', _proximity_keys(packet)
    return None


def _battery(body):
    """Ordered list of (component, level or None, charging); the first pod is the primary."""
    entries = []
    for i in range(body[0]):
        comp, _, level, status, _ = body[1 + 5 * i: 6 + 5 * i]
        if comp in COMPONENTS:
            connected = status != BATTERY_DISCONNECTED
            entries.append((COMPONENTS[comp], level if connected else None, status == BATTERY_CHARGING))
    return entries


def _orientation(packet):
    """Vector part (x, y, z) of the head rotation quaternion, sent as Q15 integers.

    Blank samples (sent while the sensors start up) return None.
    """
    vector = tuple(v / 32768.0 for v in struct.unpack_from('<3h', packet, 43))
    return vector if any(vector) else None


def _proximity_keys(packet):
    # 04000400 3100 02 | 01 len(2) 00 IRK(16) | 04 len(2) 00 ENC(16)
    if packet[7] != 0x01 or packet[27] != 0x04:
        return None
    return {'irk': packet[11:27].hex(), 'enc': packet[31:47].hex()}
