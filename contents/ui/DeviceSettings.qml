import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents

ColumnLayout {
    id: settings

    property var controls: []
    property var preferences: ({})
    // AirPods settings need a live connection; "This Computer" preferences do not.
    property bool controlsEnabled: true
    readonly property var sections: [...new Set(controls.map(c => c.section))]

    signal controlEdited(int id, int index, int value)
    signal preferenceEdited(string key, string value)

    spacing: Kirigami.Units.largeSpacing

    Repeater {
        model: settings.sections
        ColumnLayout {
            required property string modelData
            Layout.fillWidth: true
            enabled: settings.controlsEnabled
            spacing: Kirigami.Units.smallSpacing

            Kirigami.Heading {
                level: 5
                text: modelData
                opacity: 0.75
            }

            Repeater {
                model: settings.controls.filter(c => c.section === modelData)
                delegate: Loader {
                    required property var modelData
                    Layout.fillWidth: true
                    sourceComponent: modelData.kind === "toggle" ? toggle
                        : modelData.kind === "range" ? range : choice
                    property var entry: modelData
                }
            }
        }
    }

    ColumnLayout {
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing

        Kirigami.Heading {
            level: 5
            text: "This Computer"
            opacity: 0.75
        }

        Repeater {
            model: Object.keys(settings.preferences)
            RowLayout {
                required property string modelData
                readonly property var pref: settings.preferences[modelData]
                Layout.fillWidth: true
                PlasmaComponents.Label {
                    Layout.fillWidth: true
                    text: pref.label
                    elide: Text.ElideRight
                }
                PlasmaComponents.ComboBox {
                    model: pref.options
                    textRole: "label"
                    valueRole: "value"
                    currentIndex: pref.options.findIndex(o => o.value === pref.value)
                    onActivated: settings.preferenceEdited(modelData, currentValue)
                }
            }
        }
    }

    Component {
        id: toggle
        PlasmaComponents.Switch {
            text: entry.label
            checked: entry.value
            onToggled: settings.controlEdited(entry.id, entry.index, checked ? 1 : 2)
        }
    }

    Component {
        id: choice
        RowLayout {
            PlasmaComponents.Label {
                Layout.fillWidth: true
                text: entry.label
                elide: Text.ElideRight
            }
            PlasmaComponents.ComboBox {
                model: entry.options
                textRole: "label"
                valueRole: "value"
                currentIndex: entry.options.findIndex(o => o.value === entry.value)
                onActivated: settings.controlEdited(entry.id, entry.index, currentValue)
            }
        }
    }

    Component {
        id: range
        ColumnLayout {
            spacing: 0
            PlasmaComponents.Label {
                text: entry.label
            }
            RowLayout {
                PlasmaComponents.Label {
                    text: entry.options[0].label
                    font: Kirigami.Theme.smallFont
                    opacity: 0.7
                }
                PlasmaComponents.Slider {
                    Layout.fillWidth: true
                    from: entry.options[0].value
                    to: entry.options[1].value
                    stepSize: 1
                    value: entry.value
                    onPressedChanged: if (!pressed) settings.controlEdited(entry.id, entry.index, Math.round(value))
                }
                PlasmaComponents.Label {
                    text: entry.options[1].label
                    font: Kirigami.Theme.smallFont
                    opacity: 0.7
                }
            }
        }
    }
}
