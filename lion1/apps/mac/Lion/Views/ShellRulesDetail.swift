import SwiftUI

/// Settings › Plugins › Shell rules: the named rule sets that say what bash may do, one open at a
/// time, and a dry try of a command against the saved set.
struct ShellRulesDetail: View {
    @Environment(Store.self) private var store
    @State private var chosen = ""
    @State private var fields = RuleFields()
    @State private var naming: Naming?
    @State private var newName = ""
    @State private var command = ""
    @State private var verdict: ShellVerdict?
    @State private var tryFailure: String?
    @State private var failure: String?
    @State private var working = false

    private enum Naming {
        case fresh, copy
    }

    private var names: [String] { (store.shelf?.shellRules ?? []).map(\.name) }
    private var saved: ShellRules? { store.ruleSets[chosen] }
    private var editable: Bool { store.can(.shellRules) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            DetailHeader("Shell rules", sub: "what bash may do, saved as named rule sets and given to agents") {
                if editable {
                    Button("Duplicate") { start(.copy) }
                        .buttonStyle(.ghost)
                        .disabled(saved == nil)
                    Button("New rule set") { start(.fresh) }
                        .buttonStyle(.ghost)
                }
            }
            if let naming {
                namingRow(naming)
            }
            if names.isEmpty {
                Hint(text: "No rule set yet.", color: Palette.tx3)
            } else {
                Segment(items: names, selection: $chosen, title: { $0 }, fill: false)
                    .padding(.bottom, 16)
                if let saved {
                    editor(saved)
                } else {
                    ProgressView().controlSize(.small)
                }
            }
        }
        .onAppear(perform: pickFirst)
        .onChange(of: names) { _, _ in pickFirst() }
        .task(id: chosen) {
            guard !chosen.isEmpty else { return }
            verdict = nil
            tryFailure = nil
            failure = nil
            if let saved {
                fields = RuleFields(saved)
            }
            await store.loadRules(chosen)
            if let saved {
                fields = RuleFields(saved)
            }
        }
    }

    private func pickFirst() {
        if !names.contains(chosen) {
            chosen = names.first ?? ""
        }
    }

    @ViewBuilder private func editor(_ saved: ShellRules) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(saved.name).font(.system(size: 13, weight: .semibold))
                Hint(text: usedBy(saved)).lineLimit(1)
            }
            .padding(.bottom, 12)
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 14, verticalSpacing: 10) {
                textRow("Programs", text: $fields.programs, prompt: "git gh uv cargo")
                textRow("Never", text: $fields.never, prompt: "rm -rf · git push --force")
                textRow("May write under", text: $fields.writeUnder, prompt: "~/code · /tmp")
                GridRow {
                    FieldName(text: "May read")
                    MenuPicker(selection: $fields.read,
                               options: [("anywhere", "anywhere the user can"), ("write_roots", "only where it may write")])
                        .frame(width: 240, alignment: .leading)
                }
                GridRow {
                    FieldName(text: "Network")
                    HStack(spacing: 10) {
                        MenuPicker(selection: $fields.network,
                                   options: [("off", "off"), ("hosts", "only these hosts…"), ("on", "on")])
                            .frame(width: 180, alignment: .leading)
                        if fields.network == "hosts" {
                            TextField("", text: $fields.hosts, prompt: Text("pypi.org · github.com"))
                                .textFieldStyle(.roundedBorder)
                        }
                    }
                }
                textRow("Time per command", text: $fields.time, prompt: "2 min", width: 120)
                textRow("Ask me first", text: $fields.askFirst, prompt: "git push · gh pr merge")
                GridRow {
                    FieldName(text: "Otherwise")
                    MenuPicker(selection: $fields.otherwise,
                               options: [("refuse", "refuse and say why"), ("ask", "ask me first")])
                        .frame(width: 240, alignment: .leading)
                }
            }
            .disabled(!editable)
            .padding(.bottom, 18)

            FormHeading(title: "Try a command", note: "see what this rule set would do, without running it")
            HStack(spacing: 8) {
                TextField("", text: $command, prompt: Text("git push origin main"))
                    .textFieldStyle(.roundedBorder)
                    .font(.mono12)
                    .frame(width: 320)
                    .onSubmit { tryIt() }
                Button("Check") { tryIt() }
                    .buttonStyle(.boxed)
                    .disabled(!editable || command.trimmingCharacters(in: .whitespaces).isEmpty)
                if let verdict {
                    Text("→ " + describe(verdict))
                        .font(.mono12)
                        .foregroundStyle(verdict.verdict == "refused" ? Palette.bad : Palette.tx2)
                        .lineLimit(2)
                } else if let tryFailure {
                    Hint(text: "→ " + tryFailure, color: Palette.bad).lineLimit(2)
                }
            }
            if dirty(saved) {
                Hint(text: "a try reads the saved rule set; save to try your changes", color: Palette.warn)
                    .padding(.top, 6)
            }

            HStack(spacing: 10) {
                Button("Save") { save(saved) }
                    .buttonStyle(.primary)
                    .disabled(!editable || !dirty(saved) || working)
                Button("Revert") {
                    fields = RuleFields(saved)
                    failure = nil
                }
                .buttonStyle(.ghost)
                .disabled(!dirty(saved))
                Hint(text: "an agent picks one rule set on its Configure page; a refusal is on its record with the rule that refused")
                    .lineLimit(2)
            }
            .padding(.top, 22)
            if let failure {
                Hint(text: failure, color: Palette.bad).padding(.top, 8)
            }
        }
    }

    private func textRow(_ name: String, text: Binding<String>, prompt: String, width: CGFloat? = nil) -> some View {
        GridRow {
            FieldName(text: name)
            TextField("", text: text, prompt: Text(prompt))
                .textFieldStyle(.roundedBorder)
                .font(.mono12)
                .frame(width: width)
                .frame(maxWidth: width == nil ? .infinity : nil, alignment: .leading)
        }
    }

    @ViewBuilder private func namingRow(_ naming: Naming) -> some View {
        HStack(spacing: 8) {
            Text(naming == .copy ? "A copy of \(chosen), named" : "A new rule set, named")
                .font(.small12)
                .foregroundStyle(Palette.tx2)
            TextField("", text: $newName, prompt: Text("name"))
                .textFieldStyle(.roundedBorder)
                .frame(width: 200)
                .onSubmit { create(naming) }
            Button(naming == .copy ? "Duplicate" : "Create") { create(naming) }
                .buttonStyle(.boxed)
                .disabled(!nameFree || working)
            Button("Cancel") { self.naming = nil }
                .buttonStyle(.ghost)
            if !trimmedName.isEmpty, !nameFree {
                Hint(text: "a rule set already has that name", color: Palette.warn)
            }
        }
        .padding(.bottom, 14)
    }

    private var trimmedName: String { newName.trimmingCharacters(in: .whitespaces) }
    private var nameFree: Bool { !trimmedName.isEmpty && !names.contains(trimmedName) }

    private func usedBy(_ rules: ShellRules) -> String {
        rules.usedBy.isEmpty ? "used by no agent yet" : "used by " + rules.usedBy.map { store.label(ofAgent: $0) }.joined(separator: ", ")
    }

    private func dirty(_ saved: ShellRules) -> Bool {
        fields != RuleFields(saved)
    }

    private func describe(_ verdict: ShellVerdict) -> String {
        let word: String
        switch verdict.verdict {
        case "runs": word = "runs"
        case "asks": word = "asks you first"
        case "refused": word = "refused"
        default: word = verdict.verdict
        }
        return verdict.rule.isEmpty ? word : "\(word) (\(verdict.rule))"
    }

    private func start(_ kind: Naming) {
        newName = ""
        failure = nil
        naming = kind
    }

    /// A new set starts closed (no programs, no network, refuse otherwise); a copy starts as the
    /// fields now read. Either is saved at once, so it is in the list and any agent can pick it.
    private func create(_ kind: Naming) {
        guard nameFree else { return }
        let base = kind == .copy ? fields : RuleFields()
        guard let rules = base.rules(named: trimmedName, usedBy: []) else {
            failure = RuleFields.timeHelp
            return
        }
        Task {
            working = true
            do {
                try await store.saveRules(rules)
                naming = nil
                newName = ""
                chosen = rules.name
            } catch {
                failure = error.localizedDescription
            }
            working = false
        }
    }

    private func save(_ saved: ShellRules) {
        guard let rules = fields.rules(named: saved.name, usedBy: saved.usedBy) else {
            failure = RuleFields.timeHelp
            return
        }
        Task {
            working = true
            do {
                try await store.saveRules(rules)
                failure = nil
            } catch {
                failure = error.localizedDescription
            }
            working = false
        }
    }

    private func tryIt() {
        let text = command.trimmingCharacters(in: .whitespaces)
        guard editable, !text.isEmpty, !chosen.isEmpty else { return }
        let name = chosen
        Task {
            do {
                verdict = try await store.tryCommand(name, command: text)
                tryFailure = nil
            } catch {
                verdict = nil
                tryFailure = error.localizedDescription
            }
        }
    }
}

/// A rule set as the form holds it: lists as the text typed, the time as it is said.
struct RuleFields: Equatable {
    var programs = ""
    var never = ""
    var writeUnder = ""
    var read = "write_roots"
    var network = "off"
    var hosts = ""
    var time = "2 min"
    var askFirst = ""
    var otherwise = "refuse"

    static let timeHelp = "Time per command: say it as \"90 s\", \"2 min\" or \"1 h\"."

    init() {}

    init(_ rules: ShellRules) {
        programs = rules.programs.joined(separator: " ")
        never = rules.never.joined(separator: " · ")
        writeUnder = rules.writeUnder.joined(separator: " · ")
        read = rules.read
        switch rules.network {
        case .off:
            network = "off"
        case .on:
            network = "on"
        case let .hosts(list):
            network = "hosts"
            hosts = list.joined(separator: " · ")
        }
        time = Format.span(Double(rules.secondsPerCommand))
        askFirst = rules.askFirst.joined(separator: " · ")
        otherwise = rules.otherwise
    }

    /// The rule set to save; nil when the time cannot be read.
    func rules(named name: String, usedBy: [String]) -> ShellRules? {
        guard let seconds = Format.seconds(time), seconds >= 1 else { return nil }
        let net: Network
        switch network {
        case "on": net = .on
        case "hosts": net = .hosts(Self.items(hosts, spaces: true))
        default: net = .off
        }
        return ShellRules(name: name,
                          programs: Self.items(programs, spaces: true),
                          never: Self.items(never),
                          writeUnder: Self.items(writeUnder),
                          read: read,
                          network: net,
                          secondsPerCommand: Int(seconds.rounded()),
                          askFirst: Self.items(askFirst),
                          otherwise: otherwise,
                          usedBy: usedBy)
    }

    /// Items typed apart by "·" or commas; programs and hosts may also be apart by spaces, since
    /// neither holds one, while a command pattern such as "git push" does.
    static func items(_ text: String, spaces: Bool = false) -> [String] {
        text.split(whereSeparator: { $0 == "·" || $0 == "," || $0 == "\n" || (spaces && $0 == " ") })
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }
}
