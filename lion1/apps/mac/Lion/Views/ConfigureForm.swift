import SwiftUI

/// The one form for an agent's definition: an agent's Configure view, and the New agent page, which
/// is this form on an agent that does not exist yet.
struct ConfigureForm: View {
    @Environment(Store.self) private var store
    /// nil while the agent does not exist yet.
    let agent: Agent?
    @State private var draft: Definition
    @State private var base: Definition
    @State private var working = false
    @State private var outcome: String?
    @State private var failure: String?
    @State private var changes: [Change]?
    @State private var card: InspectAnswer?

    init(agent: Agent, definition: Definition) {
        self.agent = agent
        _draft = State(initialValue: definition)
        _base = State(initialValue: definition)
    }

    init(new definition: Definition) {
        agent = nil
        _draft = State(initialValue: definition)
        _base = State(initialValue: definition)
    }

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    if agent != nil, !store.can(.definition) {
                        Hint(text: "This daemon does not take changes to a definition, so the form is read-only.",
                             color: Palette.tx3)
                            .padding(.bottom, 12)
                    }
                    basics
                    modelAndLimits
                    instructions
                    WakeRules(wake: $draft.wake, showCounts: agent != nil, area: draft.area, editable: editable)
                    PluginPicks(plugins: $draft.plugins,
                                shellRules: Binding(get: { draft.shellRules ?? "" },
                                                    set: { draft.shellRules = $0.isEmpty ? nil : $0 }),
                                editPlugins: editable("plugins"), editRules: editable("shell_rules"))
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(18)
            }
            footer
        }
    }

    /// A field is changeable when the daemon takes the write and lists the field's path as editable.
    private func editable(_ path: String) -> Bool {
        guard let agent else { return store.can(.addAgent) }
        return store.can(.definition) && agent.editable.contains { path == $0 || path.hasPrefix($0 + ".") }
    }

    // MARK: basics

    private var basics: some View {
        FormSection("Basics") {
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                GridRow {
                    FieldName(text: "Name")
                    TextField("", text: $draft.name, prompt: Text("front desk"))
                        .textFieldStyle(.roundedBorder)
                        .disabled(!editable("name"))
                    FieldName(text: "Area")
                    MenuPicker(selection: $draft.area, options: (store.today?.areas ?? []).map { ($0.name, $0.name) })
                        .disabled(!editable("area"))
                }
                GridRow {
                    FieldName(text: "Profile")
                    MenuPicker(selection: builtFrom, options: profileOptions)
                        .disabled(!editable("built_from"))
                    FieldName(text: "Runs in")
                    MenuPicker(selection: runsIn, options: [("local", "this Mac"), ("daytona", "a daytona box"),
                                                            ("runta", "a runta box")])
                        .disabled(!editable("runs_in"))
                }
                if draft.builtFrom.kind == "file" {
                    GridRow {
                        FieldName(text: "File")
                        HStack(spacing: 8) {
                            TextField("", text: filePath, prompt: Text("~/agents/my_agent.py"))
                                .textFieldStyle(.roundedBorder)
                                .disabled(!editable("built_from"))
                            if store.can(.inspect) {
                                Button("Read it") { inspect() }
                                    .buttonStyle(.boxed)
                                    .disabled(working || (draft.builtFrom.ref ?? "").isEmpty)
                            }
                        }
                        .gridCellColumns(3)
                    }
                }
            }
            if let card, draft.builtFrom.kind == "file" {
                cardView(card)
            }
        }
    }

    private var profileOptions: [(String, String)] {
        store.profiles.map { ("profile:" + $0.name, $0.name) } + [("file", "from a Python file…"), ("blank", "new, empty")]
    }

    private var builtFrom: Binding<String> {
        Binding(
            get: {
                switch draft.builtFrom.kind {
                case "profile":
                    return "profile:" + (draft.builtFrom.ref ?? "")
                case "file":
                    return "file"
                default:
                    return "blank"
                }
            },
            set: { key in
                if key.hasPrefix("profile:") {
                    draft.builtFrom = BuiltFrom(kind: "profile", ref: String(key.dropFirst("profile:".count)))
                } else if key == "file" {
                    draft.builtFrom = BuiltFrom(kind: "file", ref: draft.builtFrom.kind == "file" ? draft.builtFrom.ref : nil)
                } else {
                    draft.builtFrom = BuiltFrom(kind: "blank", ref: nil)
                }
                card = nil
            }
        )
    }

    private var filePath: Binding<String> {
        Binding(get: { draft.builtFrom.ref ?? "" }, set: { draft.builtFrom.ref = $0.isEmpty ? nil : $0 })
    }

    private var runsIn: Binding<String> {
        Binding(get: { draft.runsIn.box ?? "local" },
                set: { draft.runsIn = RunsIn(box: $0 == "local" ? nil : $0) })
    }

    /// What the file's agent says about itself, from running it once on the side.
    private func cardView(_ answer: InspectAnswer) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Hint(text: "The file's agent says it takes these commands"
                + (answer.params.isEmpty ? "; the file lets the app change nothing." : "; the app may change: "
                    + answer.params.joined(separator: ", ") + "."))
            ForEach(answer.card.commands, id: \.name) { command in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(command.name).font(.mono12)
                    Text(command.about).font(.small12).foregroundStyle(Palette.tx2)
                    Chip(text: command.effect)
                }
            }
        }
        .padding(.top, 10)
    }

    private func inspect() {
        guard let file = draft.builtFrom.ref, !file.isEmpty else { return }
        run { card = try await store.inspect(file: file) }
    }

    // MARK: model and limits

    private var modelAndLimits: some View {
        FormSection("Model and limits") {
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                GridRow {
                    FieldName(text: "Provider")
                    MenuPicker(selection: provider, options: store.providers.map { ($0.name, providerTitle($0)) })
                        .disabled(!editable("model"))
                    FieldName(text: "Model")
                    MenuPicker(selection: $draft.model,
                               options: store.models.filter { $0.through == provider.wrappedValue }.map { ($0.ref, $0.shownAs) })
                        .disabled(!editable("model"))
                }
                GridRow {
                    FieldName(text: "Cost cap")
                    NumberField(value: $draft.limits.costPerDay, show: { Format.money($0) + " a day" }, prompt: "no cap")
                        .disabled(!editable("limits.cost_per_day"))
                    FieldName(text: "Turns per wake")
                    NumberField(value: $draft.limits.turnsPerWake.real, show: { Format.whole(Int($0)) })
                        .disabled(!editable("limits.turns_per_wake"))
                }
                GridRow {
                    FieldName(text: "Time per wake")
                    NumberField(value: timePerWake, show: Format.span, read: Format.seconds)
                        .disabled(!editable("limits.time_per_wake_s"))
                    FieldName(text: "Fold context at")
                    NumberField(value: $draft.limits.foldAtTokens.real, show: { Format.whole(Int($0)) + " tokens" })
                        .disabled(!editable("limits.fold_at_tokens"))
                }
                GridRow {
                    FieldName(text: "Escalates to")
                    MenuPicker(selection: escalation, options: escalationOptions)
                        .disabled(!editable("limits.escalates_to"))
                    Color.clear.frame(height: 1).gridCellColumns(2)
                }
            }
        }
    }

    private func providerTitle(_ provider: Provider) -> String {
        if provider.signsIn {
            return provider.title + " (your login)"
        }
        return provider.title + (provider.key?.isSet == true ? " (key)" : "")
    }

    /// The provider is the part of the model's name before its first slash; picking another one
    /// picks that provider's first model.
    private var provider: Binding<String> {
        Binding(
            get: { draft.model.split(separator: "/", maxSplits: 1).first.map(String.init) ?? "" },
            set: { name in
                draft.model = store.models.first { $0.through == name }?.ref ?? name + "/"
            }
        )
    }

    private var timePerWake: Binding<Double?> {
        $draft.limits.timePerWakeS.real
    }

    private var escalation: Binding<String> {
        Binding(get: { draft.limits.escalatesTo ?? "" },
                set: { draft.limits.escalatesTo = $0.isEmpty ? nil : $0 })
    }

    private var escalationOptions: [(String, String)] {
        let others = (store.areas[draft.area]?.agents ?? store.today?.areas.first { $0.name == draft.area }?.agents ?? [])
            .filter { $0.id != agent?.row.id }
            .map { ($0.id, $0.name) }
        return [("chair", "the area's chair"), ("", "no one")] + others
    }

    // MARK: instructions

    private var instructions: some View {
        FormSection("Instructions", note: draft.instructions.fromRole.isEmpty ? nil : "the profile's, then this agent's own") {
            VStack(alignment: .leading, spacing: 8) {
                if !draft.instructions.fromRole.isEmpty {
                    Text(draft.instructions.fromRole)
                        .font(.mono12)
                        .foregroundStyle(Palette.tx3)
                        .lineSpacing(3)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 12)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Palette.chip.opacity(0.6)))
                }
                TextEditor(text: $draft.instructions.extra)
                    .font(.mono12)
                    .lineSpacing(3)
                    .scrollContentBackground(.hidden)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 8)
                    .frame(minHeight: 150)
                    .background(RoundedRectangle(cornerRadius: 8).fill(Palette.chip))
                    .overlay(alignment: .topLeading) {
                        if draft.instructions.extra.isEmpty {
                            Text("From the profile. Write here to add to it for this agent only.")
                                .font(.mono12)
                                .foregroundStyle(Palette.tx3)
                                .padding(.horizontal, 15)
                                .padding(.vertical, 8)
                                .allowsHitTesting(false)
                        }
                    }
                    .disabled(!editable("instructions.extra"))
            }
        }
    }

    // MARK: footer

    private var footer: some View {
        HStack(spacing: 8) {
            if agent != nil {
                if store.can(.definition) {
                    Button("Save") { save(restart: false) }
                        .buttonStyle(.primary)
                        .disabled(working || draft == base)
                        .keyboardShortcut("s")
                    if store.can(.control) {
                        Button("Save and restart") { save(restart: true) }
                            .buttonStyle(.boxed)
                            .disabled(working || draft == base)
                    }
                    Button("Show what changes") { showChanges() }
                        .buttonStyle(.ghost)
                        .disabled(working || draft == base)
                        .popover(isPresented: changesShown, arrowEdge: .top) { changeList }
                    Button("Revert") { revert() }
                        .buttonStyle(.ghost)
                        .disabled(working || draft == base)
                }
            } else if store.can(.addAgent) {
                Button("Create and start") { create(start: true) }
                    .buttonStyle(.primary)
                    .disabled(working || draft.name.trimmingCharacters(in: .whitespaces).isEmpty)
                Button("Create held") { create(start: false) }
                    .buttonStyle(.boxed)
                    .disabled(working || draft.name.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            status
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 10)
        .background(Palette.win)
        .overlay(alignment: .top) { Rectangle().fill(Palette.line).frame(height: 1) }
    }

    @ViewBuilder private var status: some View {
        if let failure {
            Hint(text: failure, color: Palette.bad).lineLimit(2).padding(.leading, 6)
        } else if let outcome {
            Hint(text: outcome).lineLimit(2).padding(.leading, 6)
        } else if agent != nil, store.can(.definition) {
            Hint(text: "a saved change applies on the next wake; a model change needs the restart").lineLimit(1).padding(.leading, 6)
        }
    }

    private var changesShown: Binding<Bool> {
        Binding(get: { changes != nil }, set: { if !$0 { changes = nil } })
    }

    private var changeList: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("What changes").fontWeight(.semibold)
            if changes?.isEmpty ?? true {
                Hint(text: "Nothing: the daemon sees no difference.")
            }
            ForEach(changes ?? [], id: \.path) { change in
                VStack(alignment: .leading, spacing: 1) {
                    Text(change.path).font(.mono12)
                    Text("\(change.old?.display ?? "nothing") → \(change.new?.display ?? "nothing")")
                        .font(.small12)
                        .foregroundStyle(Palette.tx2)
                        .lineLimit(3)
                }
            }
        }
        .padding(14)
        .frame(width: 420, alignment: .leading)
    }

    private func save(restart: Bool) {
        guard let id = agent?.row.id else { return }
        run {
            let answer = try await store.save(id, draft, restart: restart)
            let saved = store.agents[id]?.definition ?? draft
            base = saved
            draft = saved
            var line = "Saved: " + Format.count(answer.changes.count, "change", "changes") + "."
            if restart {
                line += " Restarted."
            } else if answer.needsRestart {
                line += " A model or box change applies after a restart."
            }
            outcome = line
        }
    }

    private func showChanges() {
        guard let id = agent?.row.id else { return }
        run { changes = try await store.changes(id, draft).changes }
    }

    private func revert() {
        draft = base
        outcome = nil
        failure = nil
    }

    private func create(start: Bool) {
        var definition = draft
        definition.name = definition.name.trimmingCharacters(in: .whitespaces)
        run { try await store.create(definition, start: start) }
    }

    private func run(_ work: @escaping () async throws -> Void) {
        working = true
        failure = nil
        outcome = nil
        Task {
            do {
                try await work()
            } catch {
                failure = error.localizedDescription
            }
            working = false
        }
    }
}
