"""User-facing AirPods settings, mirroring the iOS AirPods settings page.

Every setting is written to the AirPods themselves, so it follows them to other
devices. `reported` controls appear once the connected AirPods report a value,
so one table serves every model. `stored` controls are write-only on current
firmware (the pods never echo them); the last written value is remembered and
`stored` holds the factory value. `index` selects the byte for controls that keep
one value per bud (press-and-hold: byte 0 right, byte 1 left).
"""

ON, OFF = 0x01, 0x02
SPEED = [(0, 'Default'), (1, 'Slower'), (2, 'Slowest')]
HOLD_ACTIONS = [(0x01, 'Noise Control'), (0x05, 'Siri')]

LISTENING_MODE = 0x0D
LISTENING_CYCLE = 0x1A
ALLOW_OFF = 0x34
# Modes in the order a stem press cycles through them (verified on AirPods Pro 2),
# with their bit in the pods' cycle mask.
LISTENING_MODES = [
    {'value': 1, 'bit': 0x01, 'label': 'Off'},
    {'value': 3, 'bit': 0x04, 'label': 'Transparency'},
    {'value': 4, 'bit': 0x08, 'label': 'Adaptive'},
    {'value': 2, 'bit': 0x02, 'label': 'Noise Cancellation'},
]
DEFAULT_CYCLE = 0x0E
MIN_CYCLE_MODES = 2


def _entry(section, ident, label, kind, options=None, index=0, stored=None):
    return {'section': section, 'id': ident, 'label': label, 'kind': kind,
            'options': [{'value': v, 'label': t} for v, t in options or []], 'index': index,
            'stored': stored}


CONTROLS = [
    _entry('Noise Control', 0x2E, 'Adaptive Audio', 'range', [(0, 'Less noise'), (100, 'More noise')]),
    _entry('Noise Control', 0x28, 'Conversation Awareness', 'toggle'),
    _entry('Noise Control', 0x26, 'Personalized Volume', 'toggle'),
    _entry('Noise Control', 0x1B, 'Noise Control with One AirPod', 'toggle'),
    _entry('Press and Hold', 0x16, 'Left AirPod', 'choice', HOLD_ACTIONS, index=1, stored=[0x01, 0x01]),
    _entry('Press and Hold', 0x16, 'Right AirPod', 'choice', HOLD_ACTIONS, index=0, stored=[0x01, 0x01]),
    _entry('Press and Hold', 0x17, 'Press speed', 'choice', SPEED),
    _entry('Press and Hold', 0x18, 'Press and hold duration', 'choice', SPEED),
    _entry('Press and Hold', 0x25, 'Swipe to change volume', 'toggle'),
    _entry('Press and Hold', 0x23, 'Volume swipe length', 'choice', [(0, 'Default'), (1, 'Longer'), (2, 'Longest')]),
    _entry('Press and Hold', 0x1C, 'Digital Crown', 'choice', [(0x02, 'Back to Front'), (0x01, 'Front to Back')]),
    _entry('General', 0x0A, 'Automatic Ear Detection', 'toggle', stored=[ON]),
    _entry('General', 0x01, 'Microphone', 'choice', [(0, 'Automatic'), (1, 'Always Right'), (2, 'Always Left')],
           stored=[0]),
    _entry('General', 0x35, 'Pause Media When Falling Asleep', 'toggle'),
    _entry('General', 0x1F, 'Sounds volume', 'range', [(0, 'Quiet'), (100, 'Loud')]),
]

PREFERENCES = {
    'earPause': {'label': 'Pause media when', 'default': 'one',
                 'options': [{'value': 'one', 'label': 'An AirPod is removed'},
                             {'value': 'both', 'label': 'Both AirPods are removed'},
                             {'value': 'off', 'label': 'Never'}]},
    'spatial': {'label': 'Spatial Audio', 'default': 'off',
                'options': [{'value': 'off', 'label': 'Off'},
                            {'value': 'fixed', 'label': 'Fixed'},
                            {'value': 'head', 'label': 'Head Tracked'}]},
}
