import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents

ColumnLayout {
    id: gauge

    property string label
    property int level: -1
    property bool charging: false
    property bool worn: false

    spacing: Kirigami.Units.smallSpacing
    opacity: level >= 0 ? 1 : 0.4

    Item {
        Layout.alignment: Qt.AlignHCenter
        Layout.preferredWidth: Kirigami.Units.iconSizes.large
        Layout.preferredHeight: Layout.preferredWidth

        Canvas {
            id: ring
            anchors.fill: parent
            property real fraction: Math.max(0, gauge.level) / 100
            property color track: Qt.alpha(Kirigami.Theme.textColor, 0.15)
            property color fill: gauge.level >= 0 && gauge.level <= 20 && !gauge.charging
                ? Kirigami.Theme.negativeTextColor
                : gauge.charging ? Kirigami.Theme.positiveTextColor : Kirigami.Theme.highlightColor
            onFractionChanged: requestPaint()
            onFillChanged: requestPaint()
            onTrackChanged: requestPaint()
            onPaint: {
                const ctx = getContext("2d")
                const stroke = width * 0.09
                const r = (width - stroke) / 2
                ctx.reset()
                ctx.lineWidth = stroke
                ctx.lineCap = "round"
                ctx.strokeStyle = track
                ctx.beginPath()
                ctx.arc(width / 2, height / 2, r, 0, 2 * Math.PI)
                ctx.stroke()
                if (fraction > 0) {
                    ctx.strokeStyle = fill
                    ctx.beginPath()
                    ctx.arc(width / 2, height / 2, r, -Math.PI / 2, -Math.PI / 2 + 2 * Math.PI * fraction)
                    ctx.stroke()
                }
            }
        }

        PlasmaComponents.Label {
            anchors.centerIn: parent
            text: gauge.level >= 0 ? gauge.level + "%" : "–"
            font.bold: true
        }
    }

    PlasmaComponents.Label {
        Layout.alignment: Qt.AlignHCenter
        text: gauge.label + (gauge.charging ? " ⚡" : "")
        font: Kirigami.Theme.smallFont
        opacity: gauge.worn ? 1 : 0.7
    }
}
