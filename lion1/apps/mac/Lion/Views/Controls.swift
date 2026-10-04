import SwiftUI

/// The app's buttons: a filled primary, a bordered one, a red-text one, the borderless "ghost"
/// (plain or in the accent colour), and the icon button of the headers.
struct LionButton: ButtonStyle {
    enum Kind {
        case primary, boxed, danger, ghost, accent, icon
    }

    var kind: Kind

    func makeBody(configuration: Configuration) -> some View {
        Face(kind: kind, label: configuration.label, pressed: configuration.isPressed)
    }

    private struct Face: View {
        let kind: Kind
        let label: ButtonStyleConfiguration.Label
        let pressed: Bool
        @State private var hover = false
        @Environment(\.isEnabled) private var enabled

        var body: some View {
            label
                .font(kind == .primary ? Font.system(size: 13, weight: .semibold) : Font.body13)
                .lineLimit(1)
                .foregroundStyle(foreground)
                .padding(.horizontal, kind == .primary || kind == .boxed || kind == .danger ? 11 : 7)
                .padding(.vertical, kind == .icon ? 5 : (kind == .ghost || kind == .accent ? 2 : 5))
                .background(RoundedRectangle(cornerRadius: radius).fill(fill))
                .overlay {
                    if kind == .boxed || kind == .danger {
                        RoundedRectangle(cornerRadius: radius).strokeBorder(Palette.line)
                    }
                }
                .opacity(enabled ? (pressed ? 0.7 : 1) : 0.45)
                .contentShape(Rectangle())
                .onHover { hover = $0 && enabled }
        }

        private var radius: CGFloat {
            kind == .ghost || kind == .accent || kind == .icon ? 5 : 7
        }

        private var foreground: Color {
            switch kind {
            case .primary:
                return .white
            case .boxed:
                return Palette.tx
            case .danger:
                return Palette.bad
            case .ghost, .icon:
                return hover ? Palette.tx : Palette.tx2
            case .accent:
                return Palette.acc
            }
        }

        private var fill: Color {
            switch kind {
            case .primary:
                return Palette.acc
            case .boxed, .danger:
                return Palette.win
            case .ghost, .accent, .icon:
                return hover ? Palette.chip : .clear
            }
        }
    }
}

extension ButtonStyle where Self == LionButton {
    static var primary: LionButton { LionButton(kind: .primary) }
    static var boxed: LionButton { LionButton(kind: .boxed) }
    static var danger: LionButton { LionButton(kind: .danger) }
    static var ghost: LionButton { LionButton(kind: .ghost) }
    static var accent: LionButton { LionButton(kind: .accent) }
    static var icon: LionButton { LionButton(kind: .icon) }
}

/// A state dot: green for running (with a halo) and for well, amber, red, grey, or hollow for off.
struct Dot: View {
    enum Tone {
        case grey, ok, warn, bad, run, acc, hollow
    }

    var tone: Tone

    init(_ tone: Tone) {
        self.tone = tone
    }

    init(state: String) {
        tone = Dot.tone(for: state)
    }

    var body: some View {
        Circle()
            .fill(color)
            .overlay {
                if tone == .hollow {
                    Circle().strokeBorder(Palette.tx3)
                }
            }
            .frame(width: 8, height: 8)
            .background {
                if tone == .run {
                    Circle().fill(Palette.ok.opacity(0.25)).frame(width: 14, height: 14)
                }
            }
    }

    private var color: Color {
        switch tone {
        case .grey:
            return Palette.tx3
        case .ok, .run:
            return Palette.ok
        case .warn:
            return Palette.warn
        case .bad:
            return Palette.bad
        case .acc:
            return Palette.acc
        case .hollow:
            return .clear
        }
    }

    /// The contract's state words, for agents, areas, providers, models and servers.
    static func tone(for state: String) -> Tone {
        switch state {
        case "running":
            return .run
        case "ok", "idle", "connected", "on", "signed in":
            return .ok
        case "warn", "held":
            return .warn
        case "error", "failed", "bad":
            return .bad
        case "off", "stopped", "not running":
            return .hollow
        default:
            return .grey
        }
    }
}

struct Chip: View {
    var text: String
    var accent = false

    var body: some View {
        Text(text)
            .font(.system(size: 11))
            .lineLimit(1)
            .foregroundStyle(accent ? Palette.acc : Palette.tx2)
            .padding(.horizontal, 7)
            .padding(.vertical, 1)
            .background(RoundedRectangle(cornerRadius: 5).fill(accent ? Palette.accbg : Palette.chip))
    }
}

/// The small capitals over a figure or a panel: "RUNNING NOW", "AT A GLANCE", "AREAS".
struct Caps: View {
    var text: String

    var body: some View {
        Text(text.uppercased())
            .font(.caps11)
            .tracking(0.45)
            .foregroundStyle(Palette.tx3)
            .lineLimit(1)
    }
}

/// A heading inside a pane, with an optional quiet note after it and controls at its right.
struct SectionTitle<Accessory: View>: View {
    var title: String
    var note: String?
    var accessory: Accessory

    init(_ title: String, note: String? = nil, @ViewBuilder accessory: () -> Accessory) {
        self.title = title
        self.note = note
        self.accessory = accessory()
    }

    var body: some View {
        HStack(spacing: 8) {
            Text(title).font(.system(size: 13, weight: .semibold)).foregroundStyle(Palette.tx2)
            if let note {
                Text(note).font(.small12).foregroundStyle(Palette.tx2).lineLimit(1)
            }
            Spacer(minLength: 0)
            accessory
        }
        .padding(.top, 20)
        .padding(.bottom, 8)
    }
}

extension SectionTitle where Accessory == EmptyView {
    init(_ title: String, note: String? = nil) {
        self.init(title, note: note) { EmptyView() }
    }
}

/// A form section's heading: bold, with its quiet explanation beside it.
struct FormHeading: View {
    var title: String
    var note: String?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(title).font(.system(size: 13, weight: .semibold))
            if let note {
                Text(note).font(.small12).foregroundStyle(Palette.tx2).lineLimit(2)
            }
        }
        .padding(.bottom, 10)
    }
}

/// A form section: its content, then a rule under it.
struct FormSection<Content: View>: View {
    var title: String
    var note: String?
    var content: Content

    init(_ title: String, note: String? = nil, @ViewBuilder content: () -> Content) {
        self.title = title
        self.note = note
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            FormHeading(title: title, note: note)
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 4)
        .padding(.bottom, 18)
        .overlay(alignment: .bottom) { Rectangle().fill(Palette.line).frame(height: 1) }
        .padding(.bottom, 14)
    }
}

/// The name of a field in a form grid.
struct FieldName: View {
    var text: String
    var width: CGFloat = 110

    var body: some View {
        Text(text)
            .font(.small12)
            .foregroundStyle(Palette.tx3)
            .lineLimit(1)
            .frame(width: width, alignment: .leading)
    }
}

/// A value shown in a form that this screen does not change.
struct ReadOnly: View {
    var text: String
    var mono = false

    var body: some View {
        Text(text)
            .font(mono ? Font.mono12 : Font.system(size: 12.5))
            .foregroundStyle(Palette.tx3)
            .lineLimit(2)
            .frame(maxWidth: .infinity, alignment: .leading)
            .textSelection(.enabled)
    }
}

/// The quiet second line of text.
struct Hint: View {
    var text: String
    var color: Color = Palette.tx2

    var body: some View {
        Text(text).font(.small12).foregroundStyle(color)
    }
}

/// A scrolling box as tall as what it holds, between a floor and a limit; past the limit it scrolls.
struct FitScroll<Content: View>: View {
    var limit: CGFloat
    var floor: CGFloat = 0
    @ViewBuilder var content: Content
    @State private var height: CGFloat = 0

    var body: some View {
        ScrollView {
            content.background(GeometryReader { box in
                Color.clear.preference(key: HeldHeight.self, value: box.size.height)
            })
        }
        .onPreferenceChange(HeldHeight.self) { height = $0 }
        .frame(height: min(max(height, floor), max(limit, floor)))
    }
}

private struct HeldHeight: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

extension View {
    /// A card: a hairline border and rounded corners on the window colour.
    func card() -> some View {
        padding(.vertical, 12)
            .padding(.horizontal, 14)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(RoundedRectangle(cornerRadius: 10).fill(Palette.win))
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Palette.line))
    }

    /// A bordered list: rows separated by hairlines inside one rounded frame.
    func boxed(_ radius: CGFloat = 10) -> some View {
        clipShape(RoundedRectangle(cornerRadius: radius))
            .overlay(RoundedRectangle(cornerRadius: radius).strokeBorder(Palette.line))
    }

    func hairline() -> some View {
        overlay(alignment: .bottom) { Rectangle().fill(Palette.line).frame(height: 1) }
    }

    /// A row's background while the pointer is over it.
    func hoverWash(_ on: Bool) -> some View {
        background(on ? Palette.chip.opacity(0.55) : Color.clear)
    }
}

/// A segmented switch: equal parts, the chosen one in the accent colours.
struct Segment<Value: Hashable>: View {
    var items: [Value]
    @Binding var selection: Value
    var title: (Value) -> String
    var fill = true

    var body: some View {
        HStack(spacing: 0) {
            ForEach(items, id: \.self) { item in
                Button {
                    selection = item
                } label: {
                    Text(title(item))
                        .font(.system(size: 12, weight: item == selection ? .semibold : .regular))
                        .foregroundStyle(item == selection ? Palette.acc : Palette.tx2)
                        .lineLimit(1)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 4)
                        .frame(maxWidth: fill ? .infinity : nil)
                        .background(item == selection ? Palette.accbg : Color.clear)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .fixedSize(horizontal: !fill, vertical: true)
        .boxed(7)
    }
}

/// A pop-up menu over string or flag values, each with the words it shows.
struct MenuPicker<Value: Hashable & CustomStringConvertible>: View {
    @Binding var selection: Value
    var options: [(Value, String)]

    var body: some View {
        Picker("", selection: $selection) {
            ForEach(shown.indices, id: \.self) { index in
                Text(shown[index].1).tag(shown[index].0)
            }
        }
        .labelsHidden()
        .pickerStyle(.menu)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// A value the options do not name is still shown, as itself.
    private var shown: [(Value, String)] {
        options.contains { $0.0 == selection } ? options : options + [(selection, selection.description)]
    }
}

/// A text field holding a number that is shown with its unit ("$2.00 a day", "15 min"); empty is
/// no value. The text is the person's while they type; the number follows it.
struct NumberField: View {
    @Binding var value: Double?
    var show: (Double) -> String
    var read: (String) -> Double? = Format.number
    var prompt = "no limit"
    @State private var text = ""
    @FocusState private var focused: Bool

    var body: some View {
        TextField("", text: $text, prompt: Text(prompt))
            .textFieldStyle(.roundedBorder)
            .focused($focused)
            .onAppear {
                text = value.map(show) ?? ""
            }
            .onChange(of: text) { _, new in
                let parsed = read(new)
                if parsed != value {
                    value = parsed
                }
            }
            .onChange(of: value) { _, new in
                if !focused {
                    text = new.map(show) ?? ""
                }
            }
            .onChange(of: focused) { _, now in
                if !now {
                    text = value.map(show) ?? ""
                }
            }
    }
}

extension Binding where Value == Int? {
    /// The whole number as a Double?, for a NumberField.
    var real: Binding<Double?> {
        Binding<Double?>(
            get: { wrappedValue.map(Double.init) },
            set: { wrappedValue = $0.map { Int($0.rounded()) } }
        )
    }
}

/// Text that wraps onto as many lines as the width needs: chips, mostly.
struct Flow: Layout {
    var spacing: CGFloat = 6
    var lineSpacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = arrange(width: proposal.width ?? .infinity, subviews: subviews)
        let width = rows.compactMap { row in row.last.map { spot in spot.x + spot.size.width } }.max() ?? 0
        let height = rows.last.flatMap { row in row.map { $0.y + $0.size.height }.max() } ?? 0
        return CGSize(width: proposal.width ?? width, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var index = 0
        for row in arrange(width: bounds.width, subviews: subviews) {
            for spot in row {
                subviews[index].place(at: CGPoint(x: bounds.minX + spot.x, y: bounds.minY + spot.y),
                                      proposal: ProposedViewSize(spot.size))
                index += 1
            }
        }
    }

    private struct Spot {
        var x: CGFloat
        var y: CGFloat
        var size: CGSize
    }

    private func arrange(width: CGFloat, subviews: Subviews) -> [[Spot]] {
        var rows: [[Spot]] = [[]]
        var x: CGFloat = 0
        var y: CGFloat = 0
        var tallest: CGFloat = 0
        for view in subviews {
            var size = view.sizeThatFits(.unspecified)
            size.width = min(size.width, width)
            if x > 0, x + size.width > width {
                y += tallest + lineSpacing
                x = 0
                tallest = 0
                rows.append([])
            }
            rows[rows.count - 1].append(Spot(x: x, y: y, size: size))
            x += size.width + spacing
            tallest = max(tallest, size.height)
        }
        return rows
    }
}
