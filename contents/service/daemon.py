#!/usr/bin/env python3
"""AirPods service: talks AAP to connected AirPods and publishes their state on the
session bus for the Plasma widget."""

import json
import os
import sys
import time

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aap  # noqa: E402
import controls  # noqa: E402
import media  # noqa: E402
import proximity  # noqa: E402
import spatial  # noqa: E402
from session import Session  # noqa: E402

BUS_NAME = 'org.mintocha.airpods'
OBJECT_PATH = '/org/mintocha/airpods'
IFACE = 'org.mintocha.airpods'
BLUEZ = 'org.bluez'
DEVICE_IFACE = 'org.bluez.Device1'
CONFIG_PATH = os.path.join(GLib.get_user_config_dir(), 'airpods-plugin.json')
SINK_RETRY_MS = 1000
SINK_RETRIES = 20
RECONNECT_MS = 3000
# While disconnected, listen for battery adverts this long, this often (scanning costs power).
DISCOVERY_SECONDS = 8
DISCOVERY_INTERVAL_S = 120


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class Service(dbus.service.Object):
    def __init__(self, session_bus, system_bus):
        super().__init__(dbus.service.BusName(BUS_NAME, session_bus), OBJECT_PATH)
        self.system = system_bus
        self.players = media.Players(session_bus)
        self.ducker = media.Ducker()
        self.spatial = spatial.Spatializer(self.publish)
        self.pose = spatial.HeadPose()
        self.config = load_config()
        self.session = None
        self.sink = None
        self.tracking = False
        self.device_path = None
        self.adapter = None
        self.discovering = False
        self.load_device(self.config.get('last'))

        self.system.add_signal_receiver(self.on_bluez_change, 'PropertiesChanged',
                                        dbus.PROPERTIES_IFACE, BLUEZ, path_keyword='path')
        self.system.add_signal_receiver(self.on_bluez_added, 'InterfacesAdded',
                                        'org.freedesktop.DBus.ObjectManager', BLUEZ)
        GLib.timeout_add_seconds(DISCOVERY_INTERVAL_S, self.discover)
        self.scan()
        self.discover()

    # ---------------------------------------------------------------- device

    def cache(self, address=None):
        """Per-device record that outlives the connection: identity, keys, settings, battery."""
        return self.config.setdefault('devices', {}).setdefault(address or self.device['address'], {})

    def load_device(self, address):
        """Show the last-known state of `address` (or nothing) while it is not connected."""
        cached = self.config.get('devices', {}).get(address, {}) if address else {}
        self.device = {'connected': False, 'address': address or '', 'name': cached.get('name', ''),
                       'model': cached.get('model', ''), 'firmware': cached.get('firmware', '')}
        self.values = {int(k): v for k, v in cached.get('values', {}).items()}
        self.battery = cached.get('battery', {})
        self.battery_seen = cached.get('batterySeen', 0)
        self.battery_live = False
        self.ears = {}
        self.primary = 'left'

    def save_device(self):
        if not self.device['address']:
            return
        record = self.cache()
        record.update({k: self.device.get(k, '') for k in ('name', 'model', 'firmware')})
        record['values'] = {str(k): v for k, v in self.values.items()}
        record['battery'] = self.battery
        record['batterySeen'] = self.battery_seen
        self.config['last'] = self.device['address']
        self.save()

    def objects(self):
        manager = dbus.Interface(self.system.get_object(BLUEZ, '/'), 'org.freedesktop.DBus.ObjectManager')
        return manager.GetManagedObjects().items()

    def scan(self):
        if self.session:
            return
        paired = []
        for path, ifaces in self.objects():
            props = ifaces.get(DEVICE_IFACE)
            if not props or aap.SERVICE_UUID not in props.get('UUIDs', []):
                continue
            if props.get('Connected'):
                self.attach(str(path), props)
                return
            if props.get('Paired'):
                paired.append((str(path), props))
        # Show the last-used paired AirPods, else any paired pair, else nothing.
        current = next((p for p in paired if str(p[1]['Address']) == self.device['address']), None)
        chosen = current or (paired[0] if paired else None)
        if not chosen:
            if self.device['address']:
                self.load_device(None)
                self.publish()
            return
        path, props = chosen
        self.device_path, self.adapter = path, str(props['Adapter'])
        if not current:
            self.load_device(str(props['Address']))
            self.identify(props)
            self.publish()

    def identify(self, props):
        """Name and model from BlueZ, for AirPods not yet seen over AAP."""
        modalias = str(props.get('Modalias', ''))
        product = int(modalias.split('p')[-1][:4], 16) if 'v004Cp' in modalias else 0
        self.device['name'] = self.device['name'] or str(props.get('Alias', 'AirPods'))
        self.device['model'] = aap.MODELS.get(product, self.device['model'] or 'AirPods')

    def on_bluez_added(self, path, ifaces):
        props = ifaces.get(DEVICE_IFACE)
        if props:
            self.on_advert(str(props.get('Address', '')), props.get('ManufacturerData'))
        self.scan()

    def on_bluez_change(self, iface, changed, _invalidated, path):
        if iface != DEVICE_IFACE:
            return
        if 'ManufacturerData' in changed:
            self.on_advert(path.rsplit('dev_', 1)[-1].replace('_', ':'), changed['ManufacturerData'])
        if 'Connected' not in changed:
            return
        if changed['Connected']:
            GLib.timeout_add(RECONNECT_MS, lambda: self.scan() and False)
        elif self.session and path == self.device_path:
            self.session.close()

    def attach(self, path, props):
        self.stop_discovery()
        self.device_path, self.adapter = path, str(props['Adapter'])
        address = str(props['Address'])
        self.load_device(address)
        self.identify(props)
        self.session = Session(address, self.on_packet, self.on_closed)
        self.session.open()

    def on_closed(self):
        self.session = None
        self.players.forget()
        self.ducker.restore()
        self.spatial.stop()
        self.tracking = False
        self.sink = None
        self.save_device()
        self.load_device(self.device['address'])
        self.publish()
        GLib.timeout_add(RECONNECT_MS, lambda: self.scan() and False)

    # ------------------------------------------------------- nearby (BLE)

    def discover(self):
        """Listen for battery adverts in short bursts while the AirPods are not connected."""
        keys = self.cache().get('keys') if self.device['address'] else None
        if self.session or not keys or not self.adapter or self.discovering:
            return True
        try:
            adapter = dbus.Interface(self.system.get_object(BLUEZ, self.adapter), 'org.bluez.Adapter1')
            adapter.SetDiscoveryFilter({'Transport': 'le', 'DuplicateData': True})
            adapter.StartDiscovery()
        except dbus.DBusException:
            return True
        self.discovering = True
        GLib.timeout_add_seconds(DISCOVERY_SECONDS, lambda: self.stop_discovery() and False)
        return True

    def stop_discovery(self):
        if not self.discovering:
            return
        self.discovering = False
        try:
            dbus.Interface(self.system.get_object(BLUEZ, self.adapter), 'org.bluez.Adapter1').StopDiscovery()
        except dbus.DBusException:
            pass

    def on_advert(self, address, manufacturer):
        if self.session or not manufacturer or proximity.APPLE not in manufacturer:
            return
        keys = self.cache().get('keys') if self.device['address'] else None
        if not keys or not proximity.matches(address, keys['irk']):
            return
        advert = proximity.parse(bytes(manufacturer[proximity.APPLE]), keys['enc'])
        if not advert:
            return
        self.battery = advert['battery']
        self.battery_seen = int(time.time())
        self.battery_live = True
        self.save_device()
        self.publish()

    def on_packet(self, kind, payload):
        if kind == 'ready':
            self.device['connected'] = True
            self.find_sink(SINK_RETRIES)
        elif kind == 'battery':
            self.battery = {comp: {'level': level, 'charging': charging}
                            for comp, level, charging in payload if level is not None}
            self.battery_seen = int(time.time())
            self.battery_live = True
            pods = [comp for comp, _, _ in payload if comp in ('left', 'right')]
            self.primary = pods[0] if pods else 'left'
        elif kind == 'ears':
            self.on_ears(payload)
        elif kind == 'control':
            ident, values = payload
            self.values[ident] = values
        elif kind == 'metadata':
            self.device['name'] = payload['name'] or self.device['name']
            self.device['firmware'] = payload['firmware']
        elif kind == 'keys':
            if payload:
                self.cache()['keys'] = payload
                self.save_device()
            return
        elif kind == 'conversation':
            if self.values.get(0x28, [controls.OFF])[0] == controls.ON and self.sink_in_use():
                self.ducker.level(media.default_sink(), payload)
            return
        elif kind == 'orientation':
            if self.tracking and payload:
                self.spatial.steer(*self.pose.update(payload))
            return
        else:
            return
        self.publish()

    # ----------------------------------------------------------------- audio

    def find_sink(self, retries):
        self.sink = spatial.airpods_sink(self.device['address'])
        if self.sink:
            self.route()
            self.apply_spatial()
        elif retries and self.session:
            GLib.timeout_add(SINK_RETRY_MS, lambda: self.find_sink(retries - 1) and False)

    def route(self):
        if self.sink:
            media.set_default_sink(spatial.SINK_NAME if self.spatial.active else self.sink)

    def sink_in_use(self):
        return media.default_sink() in (self.sink, spatial.SINK_NAME)

    def on_ears(self, payload):
        primary, secondary = payload
        other = 'right' if self.primary == 'left' else 'left'
        before = dict(self.ears)
        self.ears = {self.primary: primary, other: secondary}
        was = [e == 'in' for e in before.values()]
        now = [e == 'in' for e in self.ears.values()]
        if not before:
            return
        policy = self.preference('earPause')
        if any(now) and not any(was):
            self.route()
            self.players.resume()
        elif policy == 'one' and all(was) and not all(now) and self.sink_in_use():
            self.players.pause()
        elif policy != 'off' and any(was) and not any(now) and self.sink_in_use():
            self.players.pause()
        elif all(now) and self.players.paused:
            self.players.resume()

    def apply_spatial(self):
        mode = self.preference('spatial')
        if mode == 'off' or not self.sink:
            self.spatial.stop()
        else:
            self.spatial.start(self.sink, f"{self.device['name']} (Spatial Audio)", tracked=mode == 'head')
        want_tracking = mode == 'head' and self.session is not None
        if want_tracking != self.tracking and self.session:
            self.session.send(aap.HEAD_TRACKING_START if want_tracking else aap.HEAD_TRACKING_STOP)
            self.tracking = want_tracking
            self.pose.recenter()
            if not want_tracking:
                self.spatial.center()

    # ---------------------------------------------------------------- config

    def preference(self, key):
        return self.config.get('preferences', {}).get(key, controls.PREFERENCES[key]['default'])

    def save(self):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, 'w') as f:
            json.dump(self.config, f, indent=2)

    def control_values(self, entry):
        return self.values.get(entry['id']) or entry['stored']

    # ----------------------------------------------------------------- state

    def state(self):
        listed = []
        for entry in controls.CONTROLS:
            values = self.control_values(entry)
            if values is None:
                continue
            value = values[entry['index']]
            if entry['kind'] == 'toggle':
                value = value == controls.ON
            listed.append({**entry, 'value': value})
        cycle = self.values.get(controls.LISTENING_CYCLE, [controls.DEFAULT_CYCLE])[0]
        return {
            **self.device,
            'battery': self.battery,
            'batteryLive': self.battery_live,
            'batterySeen': self.battery_seen,
            'ears': self.ears,
            'mode': self.values.get(controls.LISTENING_MODE, [0])[0],
            'cycle': cycle,
            'modes': controls.LISTENING_MODES,
            'minCycle': controls.MIN_CYCLE_MODES,
            'controls': listed,
            'preferences': {k: {**v, 'value': self.preference(k)} for k, v in controls.PREFERENCES.items()},
            'spatialActive': self.spatial.active,
            'sink': self.sink or '',
        }

    def publish(self):
        self.PropertiesChanged(IFACE, {'State': json.dumps(self.state())}, [])

    # ------------------------------------------------------------------ D-Bus

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='ss', out_signature='v')
    def Get(self, iface, prop):
        return self.GetAll(iface)[prop]

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, iface):
        return {'State': json.dumps(self.state())} if iface == IFACE else {}

    @dbus.service.signal(dbus.PROPERTIES_IFACE, signature='sa{sv}as')
    def PropertiesChanged(self, iface, changed, invalidated):
        pass

    @dbus.service.method(IFACE, in_signature='ss')
    def Set(self, key, value):
        """key: 'mode' | 'cycle' | 'pref.<name>' | 'control.<id>.<index>'."""
        if key.startswith('pref.'):
            name = key[5:]
            if name in controls.PREFERENCES:
                self.config.setdefault('preferences', {})[name] = value
                self.save()
                if name == 'spatial':
                    self.apply_spatial()
                    self.route()
                self.publish()
            return
        if key == 'refresh':
            self.discover()
            return
        if not self.session or not self.session.ready:
            return
        number = int(value)
        if key == 'mode':
            if number == controls.LISTENING_MODES[0]['value']:
                self.session.send(aap.control(controls.ALLOW_OFF, [controls.ON]))
            self.session.send(aap.control(controls.LISTENING_MODE, [number]))
        elif key == 'cycle':
            if number & controls.LISTENING_MODES[0]['bit']:
                self.session.send(aap.control(controls.ALLOW_OFF, [controls.ON]))
            self.values[controls.LISTENING_CYCLE] = [number, 0, 0, 0]
            self.session.send(aap.control(controls.LISTENING_CYCLE, [number]))
            self.publish()
        elif key.startswith('control.'):
            _, ident, index = key.split('.')
            entry = next(e for e in controls.CONTROLS if e['id'] == int(ident) and e['index'] == int(index))
            values = list(self.control_values(entry) or [0, 0, 0, 0])
            values += [0] * (4 - len(values))
            values[entry['index']] = number
            self.session.send(aap.control(entry['id'], values))
            if entry['stored']:
                self.values[entry['id']] = values
                self.save_device()
                self.publish()


def main():
    DBusGMainLoop(set_as_default=True)
    service = Service(dbus.SessionBus(), dbus.SystemBus())
    loop = GLib.MainLoop()
    try:
        loop.run()
    finally:
        service.spatial.stop()


if __name__ == '__main__':
    main()
