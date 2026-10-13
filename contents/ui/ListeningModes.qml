import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents

ColumnLayout {
    id: picker

    property var modes: []
    property int mode: 0
    property int cycle: 0
    property int minCycle: 2
    readonly property var icons: ({ 1: "audio-volume-muted", 2: "view-hidden", 3: "view-visible", 4: "auto-transition" })

    signal modeSelected(int value)
    signal cycleEdited(int mask)

    spacing: Kirigami.Units.smallSpacing

    Kirigami.Heading {
        level: 5
        text: "Listening Mode"
        opacity: 0.75
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing
        Repeater {
            model: picker.modes
            PlasmaComponents.ToolButton {
                required property var modelData
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                display: PlasmaComponents.AbstractButton.TextUnderIcon
                icon.name: picker.icons[modelData.value]
                text: modelData.label
                checkable: true
                checked: picker.mode === modelData.value
                onClicked: picker.modeSelected(modelData.value)
            }
        }
    }

    Kirigami.Heading {
        Layout.topMargin: Kirigami.Units.smallSpacing
        level: 5
        text: "Press and hold cycles between"
        opacity: 0.75
    }

    Repeater {
        model: picker.modes
        PlasmaComponents.CheckBox {
            required property var modelData
            readonly property bool included: (picker.cycle & modelData.bit) !== 0
            readonly property int count: picker.modes.filter(m => picker.cycle & m.bit).length
            text: modelData.label
            checked: included
            // AirPods need at least two modes to cycle through.
            enabled: !included || count > picker.minCycle
            onToggled: picker.cycleEdited(picker.cycle ^ modelData.bit)
        }
    }
}
