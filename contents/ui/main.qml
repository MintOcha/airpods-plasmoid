import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.extras as PlasmaExtras
import org.kde.plasma.plasmoid
import org.kde.plasma.plasma5support as Plasma5Support
import org.kde.plasma.workspace.dbus as DBus

PlasmoidItem {
    id: root

    readonly property string service: "org.mintocha.airpods"
    readonly property string objectPath: "/org/mintocha/airpods"
    property var airpods: ({})
    readonly property bool connected: airpods.connected === true
    // Paired AirPods seen before: their last-known state is shown, controls greyed out.
    readonly property bool known: (airpods.address || "") !== ""
    readonly property bool batteryLive: airpods.batteryLive === true
    readonly property var pods: ["left", "right", "case"]
    readonly property var podNames: ({ left: "Left", right: "Right", case: "Case" })

    preferredRepresentation: compactRepresentation
    Plasmoid.icon: Qt.resolvedUrl("../icons/airpods.svg")
    Plasmoid.status: known ? PlasmaCore.Types.ActiveStatus : PlasmaCore.Types.PassiveStatus
    toolTipMainText: known ? airpods.name : "AirPods"
    toolTipSubText: !known ? "Not connected" : [batterySummary(), statusText()].filter(s => s).join("\n")

    onExpandedChanged: if (expanded && known && !connected) set("refresh", "")

    // KDE Store installs ship only this package; start the bundled service on first run.
    Plasma5Support.DataSource {
        id: launcher
        engine: "executable"
        onNewData: sourceName => disconnectSource(sourceName)
    }

    Timer {
        interval: Kirigami.Units.humanMoment
        running: !watcher.registered
        onTriggered: launcher.connectSource("bash '" + decodeURIComponent(
            Qt.resolvedUrl("../service/setup.sh").toString().replace("file://", "")) + "'")
    }

    DBus.DBusServiceWatcher {
        id: watcher
        busType: DBus.BusType.Session
        watchedService: root.service
        onRegisteredChanged: if (registered) airpodsState.updateAll(); else root.airpods = ({})
    }

    DBus.Properties {
        id: airpodsState
        busType: DBus.BusType.Session
        service: root.service
        path: root.objectPath
        iface: root.service
        onRefreshed: root.load(properties.State)
        onPropertiesChanged: (iface, changed) => { if (changed.State !== undefined) root.load(changed.State) }
    }

    function load(json) {
        try {
            airpods = JSON.parse(json || "{}")
        } catch (e) {
            airpods = ({})
        }
    }

    function set(key, value) {
        DBus.SessionBus.asyncCall({
            service: root.service, path: root.objectPath, iface: root.service,
            member: "Set", arguments: [String(key), String(value)]
        })
    }

    function level(pod) {
        const b = (airpods.battery || {})[pod]
        return b ? b.level : -1
    }

    function lowestPod() {
        const levels = ["left", "right"].map(level).filter(l => l >= 0)
        return levels.length ? Math.min(...levels) : -1
    }

    function batterySummary() {
        return pods.filter(p => level(p) >= 0)
            .map(p => podNames[p] + " " + level(p) + "%" + (airpods.battery[p].charging ? " ⚡" : ""))
            .join(" · ")
    }

    function statusText() {
        if (connected) return ""
        if (batteryLive) return "Nearby · not connected"
        if (!airpods.batterySeen) return "Not connected"
        return "Not connected · battery from " + ago(airpods.batterySeen)
    }

    function ago(seconds) {
        const minutes = Math.round((Date.now() / 1000 - seconds) / 60)
        if (minutes < 1) return "just now"
        if (minutes < 60) return minutes + " min ago"
        const hours = Math.round(minutes / 60)
        if (hours < 24) return hours + " h ago"
        return Qt.formatDate(new Date(seconds * 1000), Qt.locale().dateFormat(Locale.ShortFormat))
    }

    compactRepresentation: MouseArea {
        Layout.minimumWidth: row.implicitWidth + Kirigami.Units.smallSpacing * 2
        Layout.minimumHeight: Kirigami.Units.iconSizes.smallMedium
        onClicked: root.expanded = !root.expanded

        RowLayout {
            id: row
            anchors.centerIn: parent
            spacing: Kirigami.Units.smallSpacing

            Kirigami.Icon {
                Layout.preferredWidth: Kirigami.Units.iconSizes.smallMedium
                Layout.preferredHeight: Layout.preferredWidth
                source: Plasmoid.icon
                isMask: true
                opacity: root.connected || root.batteryLive ? 1 : 0.5
            }
            PlasmaComponents.Label {
                visible: root.lowestPod() >= 0
                text: root.lowestPod() + "%"
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize + 2
                opacity: root.connected || root.batteryLive ? 1 : 0.5
            }
        }
    }

    fullRepresentation: ColumnLayout {
        spacing: Kirigami.Units.smallSpacing
        Layout.minimumWidth: Kirigami.Units.gridUnit * 20
        Layout.maximumWidth: Kirigami.Units.gridUnit * 24
        Layout.preferredHeight: Kirigami.Units.gridUnit * 30

        PlasmaExtras.PlasmoidHeading {
            Layout.fillWidth: true
            contentItem: ColumnLayout {
                spacing: 0
                PlasmaComponents.Label {
                    Layout.fillWidth: true
                    text: root.known ? root.airpods.name : "AirPods"
                    elide: Text.ElideRight
                    font.pixelSize: Kirigami.Theme.defaultFont.pixelSize + 2
                }
                PlasmaComponents.Label {
                    Layout.fillWidth: true
                    text: !watcher.registered ? "AirPods service is not running"
                        : root.connected ? [root.airpods.model, root.airpods.firmware].filter(s => s).join(" • ")
                        : root.statusText()
                    elide: Text.ElideRight
                    font: Kirigami.Theme.smallFont
                    opacity: 0.75
                }
            }
        }

        PlasmaExtras.PlaceholderMessage {
            visible: !root.known
            Layout.fillWidth: true
            Layout.fillHeight: true
            iconName: "audio-headphones"
            text: watcher.registered ? "No AirPods yet" : "AirPods service is not running"
            explanation: watcher.registered ? "Connect your AirPods once and they'll show up here" : ""
        }

        PlasmaComponents.ScrollView {
            visible: root.known
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: availableWidth

            ColumnLayout {
                width: parent.width
                spacing: Kirigami.Units.largeSpacing

                RowLayout {
                    Layout.fillWidth: true
                    Layout.margins: Kirigami.Units.largeSpacing
                    opacity: root.connected || root.batteryLive ? 1 : 0.6
                    Repeater {
                        model: root.pods
                        BatteryGauge {
                            required property string modelData
                            Layout.fillWidth: true
                            label: root.podNames[modelData]
                            level: root.level(modelData)
                            charging: root.level(modelData) >= 0 && root.airpods.battery[modelData].charging
                            worn: (root.airpods.ears || {})[modelData] === "in"
                        }
                    }
                }

                ListeningModes {
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.largeSpacing
                    Layout.rightMargin: Kirigami.Units.largeSpacing
                    enabled: root.connected
                    modes: root.airpods.modes || []
                    mode: root.connected ? root.airpods.mode || 0 : 0
                    cycle: root.airpods.cycle || 0
                    minCycle: root.airpods.minCycle || 2
                    onModeSelected: value => root.set("mode", value)
                    onCycleEdited: mask => root.set("cycle", mask)
                }

                DeviceSettings {
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.largeSpacing
                    Layout.rightMargin: Kirigami.Units.largeSpacing
                    controlsEnabled: root.connected
                    controls: root.airpods.controls || []
                    preferences: root.airpods.preferences || ({})
                    onControlEdited: (id, index, value) => root.set("control." + id + "." + index, value)
                    onPreferenceEdited: (key, value) => root.set("pref." + key, value)
                }
            }
        }
    }
}
