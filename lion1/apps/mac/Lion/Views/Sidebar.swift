import SwiftUI

/// Today, the areas, and the daemon's line with the settings gear.
struct Sidebar: View {
    @Environment(Store.self) private var store

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // the window's own buttons sit in this strip
            Color.clear.frame(height: 34)
            ScrollView {
                VStack(alignment: .leading, spacing: 1) {
                    Item(selected: store.screen == .today, count: "\(store.today?.totals.running ?? 0) running") {
                        store.visit(.today)
                    } label: {
                        Image(systemName: "smallcircle.filled.circle").font(.system(size: 12))
                        Text("Today")
                    }
                    Caps(text: "Areas")
                        .padding(.horizontal, 8)
                        .padding(.top, 12)
                        .padding(.bottom, 4)
                    ForEach(store.today?.areas ?? []) { area in
                        Item(selected: showsArea(area.name), count: Format.count(area.agents.count, "agent", "agents")) {
                            store.visit(.area(area.name))
                        } label: {
                            Dot(state: area.state)
                            Text(area.name)
                        }
                    }
                }
            }
            .scrollIndicators(.automatic)
            footer
        }
        .padding(.horizontal, 8)
        .padding(.bottom, 10)
        .background(Palette.side)
    }

    private var footer: some View {
        HStack(alignment: .center, spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Dot(daemonTone)
                    Text(daemonLine).lineLimit(1)
                }
                if let totals = store.today?.totals {
                    Text("today \(Format.money(totals.cost)) · \(Format.count(totals.wakes, "wake", "wakes"))")
                        .lineLimit(1)
                }
            }
            .font(.small12)
            .foregroundStyle(Palette.tx2)
            Spacer(minLength: 0)
            Button {
                store.visit(.settings(.providers))
            } label: {
                Image(systemName: "gearshape").font(.system(size: 15))
            }
            .buttonStyle(.icon)
            .help("Settings  ⌘,")
        }
        .padding(.top, 10)
        .padding(.horizontal, 8)
        .overlay(alignment: .top) { Rectangle().fill(Palette.line).frame(height: 1) }
    }

    private var daemonTone: Dot.Tone {
        switch store.connection {
        case .demo, .live:
            return .ok
        case .connecting:
            return .grey
        case .down:
            return .bad
        }
    }

    private var daemonLine: String {
        switch store.connection {
        case .demo:
            return "demo · recorded"
        case .connecting:
            return "daemon · connecting"
        case .live:
            return "daemon · " + (store.today?.daemon.host ?? "this Mac")
        case .down:
            return "daemon · not answering"
        }
    }

    /// The area's own page, an agent of it, or a new agent for it.
    private func showsArea(_ name: String) -> Bool {
        switch store.screen {
        case let .area(shown):
            return shown == name
        case let .agent(id, _):
            return store.row(id)?.area == name
        case let .newAgent(area, _):
            return area == name
        default:
            return false
        }
    }

    private struct Item<Label: View>: View {
        var selected: Bool
        var count: String
        var action: () -> Void
        var label: Label
        @State private var hover = false

        init(selected: Bool, count: String, action: @escaping () -> Void, @ViewBuilder label: () -> Label) {
            self.selected = selected
            self.count = count
            self.action = action
            self.label = label()
        }

        var body: some View {
            Button(action: action) {
                HStack(spacing: 8) {
                    label
                        .font(.system(size: 13, weight: selected ? .semibold : .regular))
                        .foregroundStyle(selected ? Palette.acc : Palette.tx)
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    Text(count).font(.system(size: 11)).foregroundStyle(Palette.tx3).lineLimit(1)
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 5)
                .background(RoundedRectangle(cornerRadius: 6).fill(selected ? Palette.accbg : (hover ? Palette.chip : .clear)))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .onHover { hover = $0 }
        }
    }
}
