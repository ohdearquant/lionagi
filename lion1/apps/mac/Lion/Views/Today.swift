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
            GeometryReader { page in
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        totals
                        SectionTitle("For you", note: forYouNote)
                        // as tall as its rows, up to half the page
                        forYouList(limit: page.size.height * 0.5)
                        SectionTitle("Areas")
                        // as many columns as the page has room for
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 340), spacing: 12)], spacing: 12) {
                            ForEach(store.today?.areas ?? []) { area in
                                AreaCard(area: area) { store.visit(.area(area.name)) }
                            }
                        }
                    }
                    .padding(18)
                }
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
        let wakes = figures?.wakes
        return HStack(alignment: .top, spacing: 12) {
            Figure(label: "Running now", value: "\(figures?.running ?? 0)",
                   note: running.isEmpty ? "nothing is running" : running.joined(separator: " · "))
            Figure(label: "Wakes today", value: wakes.map { Format.whole($0) } ?? "—",
                   note: wakes == nil ? "an agent's notes do not read"
                       : figures?.acted.map { "\(Format.whole(max(0, (wakes ?? 0) - $0))) did nothing" }
                       ?? "what they did is not known")
            Figure(label: "Spend today", value: Format.money(figures?.cost), note: capNote)
            Figure(label: "For you", value: "\(store.today?.forYou.open ?? 0)", note: openKinds)
        }
        .fixedSize(horizontal: false, vertical: true)
    }

    private var capNote: String {
        guard let cap = store.today?.totals.cap else { return "no daily cap" }
        guard let cost = store.today?.totals.cost, cap > 0 else { return "cap \(Format.moneyShort(cap))" }
        return "cap \(Format.moneyShort(cap)) · \(Int((cost / cap * 100).rounded()))%"
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
        if parts.isEmpty {
            return (store.today?.forYou.open ?? 0) > 0 ? "not read yet" : "nothing waits on you"
        }
        return parts.joined(separator: " · ")
    }

    private var forYouNote: String {
        guard let open = store.today?.forYou.open else { return "not known" }
        return "\(open) open"
    }

    private func forYouList(limit: CGFloat) -> some View {
        FitScroll(limit: limit) {
            VStack(spacing: 0) {
                if store.forYou.isEmpty {
                    Text("Nothing waits on you.")
                        .foregroundStyle(Palette.tx3)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 22)
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
                        Text("\(row.name) · \(row.state) · \(Format.wakes(row.today.wakes))")
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
