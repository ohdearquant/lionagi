import SwiftUI

/// "What wakes it": the rules with what each did today, the sources a rule can be added from, and the
/// limits that sit beside the rules.
struct WakeRules: View {
    @Environment(Store.self) private var store
    @Binding var wake: WakePlan
    var showCounts: Bool
    var area: String
    var editable: (String) -> Bool
    @State private var editing: String?

    /// The sources this app knows how to set up, in the order they are offered.
    static let sources: [(String, String)] = [
        ("mail", "Mail"), ("schedule", "Schedule"), ("call", "Call"), ("repo", "Repo event"),
        ("file", "File change"), ("agent", "Another agent finished"),
    ]

    var body: some View {
        FormSection("What wakes it",
                    note: "a rule is a source, an optional free check, and what to do; an agent with no rule runs only when you message it") {
            VStack(spacing: 0) {
                header
                ForEach($wake.rules) { $rule in
                    RuleLine(rule: $rule, showCounts: showCounts, editable: editable("wake.rules"), editing: $editing,
                             agents: store.allRows) {
                        wake.rules.removeAll { $0.id == rule.id }
                    }
                }
                if wake.rules.isEmpty {
                    Text("No rule: it runs only when you message it.")
                        .foregroundStyle(Palette.tx3)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 9)
                }
            }
            .boxed()
            if editable("wake.rules") {
                HStack(spacing: 6) {
                    Text("＋ Add a rule:").foregroundStyle(Palette.tx3)
                    ForEach(Self.sources.indices, id: \.self) { index in
                        Button { add(Self.sources[index].0) } label: { Chip(text: Self.sources[index].1) }
                            .buttonStyle(.plain)
                    }
                }
                .padding(.top, 8)
            }
            limits.padding(.top, 12)
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            Color.clear.frame(width: 30, height: 1)
            Text("SOURCE").frame(width: 80, alignment: .leading)
            Text("WHEN").frame(maxWidth: .infinity, alignment: .leading)
            Text("THEN").frame(maxWidth: .infinity, alignment: .leading)
            Text(showCounts ? "TODAY" : "").frame(width: 170, alignment: .trailing)
        }
        .font(.caps11)
        .tracking(0.45)
        .foregroundStyle(Palette.tx3)
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .hairline()
    }

    private var limits: some View {
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
            GridRow {
                FieldName(text: "At most")
                NumberField(value: $wake.atMostPerHour.real, show: { "\(Int($0)) wakes an hour" })
                    .disabled(!editable("wake.at_most_per_hour"))
                FieldName(text: "Quiet hours")
                HStack(spacing: 6) {
                    TextField("", text: quietFrom, prompt: Text("23:00")).textFieldStyle(.roundedBorder)
                    Text("to").foregroundStyle(Palette.tx2)
                    TextField("", text: quietTo, prompt: Text("07:00")).textFieldStyle(.roundedBorder)
                    Text("mail waits").font(.small12).foregroundStyle(Palette.tx3).lineLimit(1)
                }
                .disabled(!editable("wake.quiet"))
            }
            GridRow {
                FieldName(text: "At a cap")
                MenuPicker(selection: $wake.atCap, options: [("hold_tell_chair", "hold and tell the chair"),
                                                             ("hold", "hold silently"),
                                                             ("continue_tell_me", "keep going, tell me")])
                    .disabled(!editable("wake.at_cap"))
                FieldName(text: "Each wake is")
                MenuPicker(selection: $wake.run, options: [("fresh", "a fresh run, memory in notes"),
                                                           ("continuous", "one continuous conversation")])
                    .disabled(!editable("wake.run"))
            }
        }
    }

    /// Quiet hours are a pair; both empty is none.
    private var quietFrom: Binding<String> {
        Binding(get: { wake.quiet?.from ?? "" }, set: { setQuiet(from: $0, to: wake.quiet?.to ?? "") })
    }

    private var quietTo: Binding<String> {
        Binding(get: { wake.quiet?.to ?? "" }, set: { setQuiet(from: wake.quiet?.from ?? "", to: $0) })
    }

    private func setQuiet(from: String, to: String) {
        wake.quiet = from.isEmpty && to.isEmpty ? nil : QuietHours(from: from, to: to)
    }

    private func add(_ kind: String) {
        var number = wake.rules.count + 1
        while wake.rules.contains(where: { $0.id == "r\(number)" }) {
            number += 1
        }
        var source: [String: JSONValue] = ["kind": .string(kind)]
        if kind == "schedule" {
            source["every"] = .string("1 h")
        }
        var rule = Rule(id: "r\(number)", on: true, source: source, check: nil, deliver: Delivery(kind: "now", items: nil, minutes: nil),
                        phrase: Phrase(when: "", then: ""), today: nil)
        rule.phrase = rule.draftPhrase
        wake.rules.append(rule)
        editing = rule.id
    }
}

/// One rule in the table: its switch, its source, when and then in words, and what it did today.
/// Clicked, it opens its editor.
private struct RuleLine: View {
    @Binding var rule: Rule
    var showCounts: Bool
    var editable: Bool
    @Binding var editing: String?
    var agents: [AgentRow]
    var remove: () -> Void
    @State private var hover = false

    var body: some View {
        HStack(spacing: 12) {
            Toggle("", isOn: $rule.on)
                .toggleStyle(.switch)
                .controlSize(.mini)
                .labelsHidden()
                .tint(Palette.ok)
                .frame(width: 30)
                .disabled(!editable)
            Text(WakeRules.sources.first { $0.0 == rule.sourceKind }?.1 ?? rule.sourceKind.capitalized)
                .fontWeight(.semibold)
                .lineLimit(1)
                .frame(width: 80, alignment: .leading)
            Text(rule.phrase.when)
                .foregroundStyle(Palette.tx2)
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
            Text(rule.phrase.then)
                .foregroundStyle(Palette.tx2)
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
            figures.frame(width: 170, alignment: .trailing)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .hoverWash(hover && editable)
        .contentShape(Rectangle())
        .onHover { hover = $0 }
        .onTapGesture {
            if editable {
                editing = rule.id
            }
        }
        .popover(isPresented: shown, arrowEdge: .bottom) {
            RuleEditor(rule: $rule, agents: agents) {
                editing = nil
                remove()
            }
        }
        .hairline()
    }

    private var shown: Binding<Bool> {
        Binding(get: { editing == rule.id }, set: { if !$0, editing == rule.id { editing = nil } })
    }

    @ViewBuilder private var figures: some View {
        if showCounts {
            VStack(alignment: .trailing, spacing: 0) {
                if !rule.on {
                    Text("off")
                } else if let today = rule.today {
                    Text("\(today.acted) of \(today.fired) acted · \(Format.money(today.cost))")
                    if today.skipped > 0 {
                        Text("\(today.skipped) skipped by the check, free")
                            .font(.system(size: 11))
                            .foregroundStyle(Palette.ok)
                    }
                } else {
                    Text("nothing yet today")
                }
            }
            .font(.small12)
            .foregroundStyle(Palette.tx2)
            .lineLimit(1)
        } else {
            Color.clear.frame(height: 1)
        }
    }
}

/// A rule's parts: where it comes from, the check that may stop it for free, and how it is delivered.
private struct RuleEditor: View {
    @Binding var rule: Rule
    var agents: [AgentRow]
    var remove: () -> Void

    /// Each source's own parameters: key, label, an example.
    private var parameters: [(String, String, String)] {
        switch rule.sourceKind {
        case "mail":
            return [("from", "From", "anything addressed to it")]
        case "schedule":
            return [("every", "Every", "30 m")]
        case "call":
            return [("from", "From", "this Mac only")]
        case "repo":
            return [("repo", "Repository", "owner/name"), ("on", "On", "a pull request opened")]
        case "file":
            return [("path", "Under", "~/notes/")]
        default:
            return []
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(WakeRules.sources.first { $0.0 == rule.sourceKind }?.1 ?? rule.sourceKind.capitalized)
                .fontWeight(.semibold)
            Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 8) {
                ForEach(parameters.indices, id: \.self) { index in
                    GridRow {
                        FieldName(text: parameters[index].1, width: 90)
                        TextField("", text: value(parameters[index].0), prompt: Text(parameters[index].2))
                            .textFieldStyle(.roundedBorder)
                    }
                }
                if rule.sourceKind == "agent" {
                    GridRow {
                        FieldName(text: "Agent", width: 90)
                        MenuPicker(selection: value("agent"),
                                   options: [("", "choose an agent")] + agents.map { ($0.id, "\($0.area) · \($0.name)") })
                    }
                }
                if let about = rule.check?["about"]?.string {
                    GridRow {
                        FieldName(text: "Only if", width: 90)
                        HStack(spacing: 6) {
                            Text(about).foregroundStyle(Palette.tx2).lineLimit(2)
                            Spacer(minLength: 4)
                            Button("Drop the check") {
                                rule.check = nil
                                reword()
                            }
                            .buttonStyle(.ghost)
                        }
                    }
                }
                GridRow {
                    FieldName(text: "Then", width: 90)
                    MenuPicker(selection: deliveryKind, options: [("now", rule.sourceKind == "call" ? "one run per call" : "wake now"),
                                                                  ("collect", "collect, then wake")])
                }
                if rule.deliver.kind == "collect" {
                    GridRow {
                        FieldName(text: "Wake at", width: 90)
                        HStack(spacing: 6) {
                            NumberField(value: $rule.deliver.items.real, show: { "\(Int($0)) items" }, prompt: "10 items")
                            Text("or").foregroundStyle(Palette.tx2)
                            NumberField(value: $rule.deliver.minutes.real, show: { "\(Int($0)) min" }, prompt: "15 min")
                        }
                    }
                }
            }
            Hint(text: "\(rule.draftPhrase.when); then \(rule.draftPhrase.then).", color: Palette.tx3)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Button("Remove this rule", action: remove).buttonStyle(.danger)
                Spacer()
            }
        }
        .padding(14)
        .frame(width: 420)
        .onChange(of: rule.source) { _, _ in reword() }
        .onChange(of: rule.deliver) { _, _ in reword() }
    }

    private func value(_ key: String) -> Binding<String> {
        Binding(get: { rule.source[key]?.display ?? "" },
                set: { rule.source[key] = $0.isEmpty ? nil : .string($0) })
    }

    private var deliveryKind: Binding<String> {
        Binding(
            get: { rule.deliver.kind },
            set: { kind in
                rule.deliver = kind == "collect"
                    ? Delivery(kind: "collect", items: rule.deliver.items ?? 10, minutes: rule.deliver.minutes ?? 15)
                    : Delivery(kind: "now", items: nil, minutes: nil)
            }
        )
    }

    /// The table shows the rule in words; until the daemon words it, the app does.
    private func reword() {
        rule.phrase = rule.draftPhrase
    }
}
