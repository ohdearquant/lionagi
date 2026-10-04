import SwiftUI

/// An area: its chair and agents as one aligned list, and what happened in it today.
struct AreaView: View {
    @Environment(Store.self) private var store
    let name: String

    var body: some View {
        VStack(spacing: 0) {
            ScreenBar {
                BarTitle(title: name, sub: sub)
            } trailing: {
                if store.can(.addAgent) {
                    Button("＋ Add an agent") { store.visit(.newAgent(area: name, profile: nil)) }
                        .buttonStyle(.accent)
                }
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    if let area = store.areas[name] {
                        list(area)
                        SectionTitle("Today")
                        timeline(area)
                    } else {
                        ProgressView().frame(maxWidth: .infinity).padding(.top, 60)
                    }
                }
                .padding(18)
            }
        }
    }

    private var sub: String? {
        guard let area = store.areas[name] else { return nil }
        return "a chair and \(Format.count(area.agents.count, "agent", "agents")) · \(Format.money(area.today.cost)) today · "
            + Format.count(area.today.wakes, "wake", "wakes")
    }

    private func list(_ area: Area) -> some View {
        VStack(spacing: 0) {
            AgentLine(row: nil)
            ForEach([area.chair].compactMap { $0 } + area.agents) { row in
                AgentLine(row: row)
            }
        }
        .boxed()
    }

    private func timeline(_ area: Area) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            if area.timeline.isEmpty {
                Hint(text: "Nothing yet today.", color: Palette.tx3)
            }
            ForEach(Array(area.timeline.enumerated()), id: \.offset) { _, entry in
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Text(Format.clock(entry.at))
                        .font(.small12)
                        .monospacedDigit()
                        .foregroundStyle(Palette.tx3)
                        .frame(width: 44, alignment: .leading)
                    Dot(entry.raised ? .acc : .grey)
                    (Text(store.row(entry.agent)?.name ?? entry.agent).fontWeight(.semibold).foregroundStyle(Palette.tx)
                        + Text(" " + entry.text).foregroundStyle(Palette.tx2))
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.vertical, 5)
                .padding(.horizontal, 2)
            }
        }
    }
}

/// One agent in an area's list, or the list's header when `row` is nil. Pause and run now appear
/// on hover; a click opens the agent.
private struct AgentLine: View {
    @Environment(Store.self) private var store
    let row: AgentRow?
    @State private var hover = false

    var body: some View {
        Group {
            if let row {
                cells(row)
                    .hoverWash(hover)
                    .onHover { hover = $0 }
                    .onTapGesture { store.visit(.agent(row.id, .activity)) }
            } else {
                header
            }
        }
        .hairline()
    }

    private var header: some View {
        HStack(spacing: 14) {
            Color.clear.frame(width: 10)
            Text("AGENT").frame(width: 170, alignment: .leading)
            Text("NOW").frame(maxWidth: .infinity, alignment: .leading)
            Text("WOKEN BY").frame(width: 130, alignment: .leading)
            Text("MODEL").frame(width: 110, alignment: .leading)
            Text("TODAY").frame(width: 96, alignment: .trailing)
            Color.clear.frame(width: 150)
        }
        .font(.caps11)
        .tracking(0.45)
        .foregroundStyle(Palette.tx3)
        .padding(.horizontal, 14)
        .padding(.vertical, 7)
    }

    private func cells(_ row: AgentRow) -> some View {
        HStack(spacing: 14) {
            Dot(state: row.state).frame(width: 10)
            HStack(spacing: 6) {
                Text(row.name).fontWeight(.semibold).lineLimit(1).truncationMode(.tail)
                if row.isChair {
                    Text("chair").font(.system(size: 11)).foregroundStyle(Palette.tx3)
                }
            }
            .frame(width: 170, alignment: .leading)
            Text(row.now)
                .foregroundStyle(Palette.tx2)
                .lineLimit(1)
                .truncationMode(.tail)
                .frame(maxWidth: .infinity, alignment: .leading)
            Text(row.wokenBy.isEmpty ? "only when messaged" : row.wokenBy.joined(separator: " · "))
                .font(.small12)
                .foregroundStyle(Palette.tx2)
                .lineLimit(1)
                .frame(width: 130, alignment: .leading)
            Text(store.modelTitle(row.model))
                .font(.small12)
                .foregroundStyle(Palette.tx2)
                .lineLimit(1)
                .frame(width: 110, alignment: .leading)
            VStack(alignment: .trailing, spacing: 0) {
                if row.today.cost != nil || row.today.wakes > 0 {
                    Text(Format.money(row.today.cost)).font(.small12).foregroundStyle(Palette.tx2)
                    Text(row.today.calls.map { Format.count($0, "call", "calls") } ?? Format.count(row.today.wakes, "wake", "wakes"))
                        .font(.system(size: 11))
                        .foregroundStyle(Palette.tx3)
                }
            }
            .lineLimit(1)
            .frame(width: 96, alignment: .trailing)
            actions(row)
                .frame(width: 150, alignment: .trailing)
                .opacity(hover ? 1 : 0)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .contentShape(Rectangle())
    }

    private func actions(_ row: AgentRow) -> some View {
        HStack(spacing: 0) {
            if store.can(.control) {
                if row.state == "held" {
                    Button { control(row, "resume") } label: { Image(systemName: "play.circle") }
                        .buttonStyle(.icon)
                        .help("Resume: it wakes by its rules again")
                } else {
                    Button { control(row, "hold") } label: { Image(systemName: "pause") }
                        .buttonStyle(.icon)
                        .help("Pause: nothing wakes it until you resume. Mail waits.")
                }
                if row.state != "running" {
                    Button { control(row, "wake") } label: { Image(systemName: "play.fill") }
                        .buttonStyle(.icon)
                        .help("Run now: it runs if mail or a queued task is waiting")
                }
            }
            Button("Open") { store.visit(.agent(row.id, .activity)) }.buttonStyle(.accent)
        }
    }

    private func control(_ row: AgentRow, _ action: String) {
        Task { await store.control(row.id, action) }
    }
}
