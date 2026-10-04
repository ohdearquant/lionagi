import SwiftUI

/// The day at a glance: the totals, what waits on the person, and the areas.
struct TodayView: View {
    @Environment(Store.self) private var store
    @State private var openItem: String?

    var body: some View {
        VStack(spacing: 0) {
            ScreenBar {
                BarTitle(title: "Today", sub: sub)
            } trailing: {
                if store.can(.addAgent) {
                    Button("＋ Add an agent") { store.visit(.newAgent(area: nil, profile: nil)) }
                        .buttonStyle(.accent)
                }
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    totals
                    SectionTitle("For you", note: forYouNote)
                    forYouList
                    SectionTitle("Areas")
                    LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible())], spacing: 12) {
                        ForEach(store.today?.areas ?? []) { area in
                            AreaCard(area: area) { store.visit(.area(area.name)) }
                        }
                    }
                }
                .padding(18)
            }
        }
    }

    private var sub: String? {
        guard let today = store.today else { return nil }
        let agents = today.areas.reduce(0) { $0 + $1.agents.count }
        return [Format.weekday(today.date), Format.count(today.areas.count, "area", "areas"),
                Format.count(agents, "agent", "agents")].joined(separator: " · ")
    }

    private var totals: some View {
        let figures = store.today?.totals
        let running = store.allRows.filter { $0.state == "running" }.map { "\($0.area) \($0.name)" }
        let wakes = figures?.wakes ?? 0
        return HStack(alignment: .top, spacing: 12) {
            Figure(label: "Running now", value: "\(figures?.running ?? 0)",
                   note: running.isEmpty ? "nothing is running" : running.joined(separator: " · "))
            Figure(label: "Wakes today", value: Format.whole(wakes),
                   note: "\(Format.whole(max(0, wakes - (figures?.acted ?? 0)))) did nothing")
            Figure(label: "Spend today", value: Format.money(figures?.cost), note: capNote)
            Figure(label: "For you", value: "\(store.today?.forYou.open ?? 0)", note: openKinds)
        }
        .fixedSize(horizontal: false, vertical: true)
    }

    private var capNote: String {
        guard let cap = store.today?.totals.cap else { return "no daily cap" }
        let cost = store.today?.totals.cost ?? 0
        return "cap \(Format.moneyShort(cap)) · \(cap > 0 ? Int((cost / cap * 100).rounded()) : 0)%"
    }

    /// "2 questions · 1 reply"
    private var openKinds: String {
        let words: [(String, String, String)] = [
            ("approval", "approval", "approvals"), ("decision", "decision", "decisions"),
            ("question", "question", "questions"), ("reply", "reply", "replies"), ("note", "note", "notes"),
        ]
        let open = store.forYou.filter(\.open)
        let parts = words.compactMap { word -> String? in
            let n = open.filter { $0.kind == word.0 }.count
            return n == 0 ? nil : Format.count(n, word.1, word.2)
        }
        return parts.isEmpty ? "nothing waits on you" : parts.joined(separator: " · ")
    }

    private var forYouNote: String {
        guard let open = store.today?.forYou.open else { return "not known" }
        return "\(open) open"
    }

    private var forYouList: some View {
        ScrollView {
            LazyVStack(spacing: 0) {
                if store.forYou.isEmpty {
                    Text("Nothing waits on you.")
                        .foregroundStyle(Palette.tx3)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 70)
                }
                ForEach(store.forYou) { item in
                    ForYouRow(item: item, openID: $openItem)
                        .overlay(alignment: .bottom) {
                            if item.id != store.forYou.last?.id {
                                Rectangle().fill(Palette.line).frame(height: 1)
                            }
                        }
                }
            }
        }
        .frame(height: 248)
        .boxed()
    }

    private struct Figure: View {
        var label: String
        var value: String
        var note: String

        var body: some View {
            VStack(alignment: .leading, spacing: 2) {
                Caps(text: label)
                Text(value).font(.system(size: 22, weight: .semibold)).lineLimit(1)
                Hint(text: note).lineLimit(2)
            }
            .card()
        }
    }
}

/// An area on Today: its chair, its spend, a line per agent. Opens the area.
private struct AreaCard: View {
    let area: AreaSummary
    var open: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: open) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    Dot(state: area.state)
                    Text(area.name).fontWeight(.semibold).lineLimit(1)
                    if let chair = area.chair {
                        Chip(text: chair.name == "chair" ? "chair" : "chair " + chair.name)
                    }
                    Spacer(minLength: 4)
                    Hint(text: "\(Format.money(area.today.cost)) today")
                }
                VStack(alignment: .leading, spacing: 1) {
                    if area.agents.isEmpty {
                        Text("no agents yet")
                    }
                    ForEach(area.agents) { row in
                        Text("\(row.name) · \(row.state) · \(Format.count(row.today.wakes, "wake", "wakes"))")
                            .lineLimit(1)
                    }
                }
                .font(.small12)
                .foregroundStyle(Palette.tx2)
            }
            .card()
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(hover ? Palette.tx3 : Color.clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
    }
}
