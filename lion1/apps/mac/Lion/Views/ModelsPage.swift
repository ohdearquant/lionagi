import SwiftUI

enum ModelPick: Hashable {
    case model(String)
    case adding
}

/// Settings › Models: the models, grouped by their provider, and one model's settings and limits.
struct ModelsPage: View {
    @Environment(Store.self) private var store
    @Binding var selection: ModelPick?

    var body: some View {
        HStack(spacing: 0) {
            ListPane {
                ForEach(store.providers) { provider in
                    let models = store.models.filter { $0.through == provider.name }
                    if !models.isEmpty {
                        ListGroup(title: provider.title)
                        ForEach(models) { model in
                            ListItem(title: model.shownAs, sub: line(model), tone: Dot.tone(for: model.state),
                                     selected: current == .model(model.id)) {
                                selection = .model(model.id)
                            }
                        }
                    }
                }
                HStack(spacing: 4) {
                    Text("Keys and sign-ins are under")
                    Button("Providers") { store.visit(.settings(.providers)) }
                        .buttonStyle(.plain)
                        .foregroundStyle(Palette.acc)
                }
                .font(.system(size: 11.5))
                .foregroundStyle(Palette.tx3)
                .padding(.horizontal, 8)
                .padding(.top, 12)
            }
            ScrollView {
                Group {
                    switch current {
                    case let .model(id)?:
                        if let model = store.models.first(where: { $0.id == id }) {
                            ModelDetail(model: model).id(model.id)
                        }
                    case .adding?:
                        AddModelForm { added in
                            selection = added.map { .model($0) }
                        }
                    case nil:
                        Hint(text: "No model yet. Add one with ＋ Add.", color: Palette.tx3)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 22)
                .padding(.vertical, 18)
            }
        }
    }

    /// The chosen entry, or the first model when nothing is chosen yet.
    private var current: ModelPick? {
        if let selection {
            if case let .model(id) = selection, !store.models.contains(where: { $0.id == id }) {
                return store.models.first.map { .model($0.id) }
            }
            return selection
        }
        return store.models.first.map { .model($0.id) }
    }

    /// "default for new agents", "4 at once · $3 a day", "not tested yet"
    private func line(_ model: Model) -> String {
        if model.defaultForNew {
            return "default for new agents"
        }
        if model.state == "warn" {
            return "not tested yet"
        }
        if model.state == "off" {
            return "off"
        }
        var parts: [String] = []
        if let atOnce = model.limits?.atOnce {
            parts.append(Format.count(atOnce, "call at once", "calls at once"))
        }
        if let daily = model.limits?.daily {
            parts.append(model.onPlan ? "\(Int(daily)) calls a day" : Format.moneyShort(daily) + " a day")
        }
        if parts.isEmpty, let reasoning = model.reasoning {
            parts.append("effort " + reasoning)
        }
        if parts.isEmpty {
            parts.append(model.onPlan ? "on the plan" : (model.price.map { "$\($0.input) / $\($0.output)" } ?? "no price"))
        }
        return parts.joined(separator: " · ")
    }
}

/// One model's settings. Price and context are the provider's when it publishes them, and read-only.
private struct ModelDetail: View {
    @Environment(Store.self) private var store
    let model: Model
    @State private var draft: Model
    @State private var working = false
    @State private var failure: String?
    @State private var outcome: String?
    @State private var confirmRemove = false

    init(model: Model) {
        self.model = model
        _draft = State(initialValue: model)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            DetailHeader(model.shownAs, sub: "through " + providerTitle) {
                if store.can(.testModel) {
                    Button("Test") { test() }.buttonStyle(.ghost).disabled(working)
                }
                if store.can(.models) {
                    Button("Remove") { confirmRemove = true }.buttonStyle(.ghost).disabled(working)
                }
            }
            .confirmationDialog("Remove \(model.shownAs)?", isPresented: $confirmRemove) {
                Button("Remove the model", role: .destructive) { remove() }
            } message: {
                Text("The daemon refuses while an agent uses it, and names the agents.")
            }
            FormSection("Model") {
                Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                    GridRow {
                        FieldName(text: "Shown as")
                        TextField("", text: $draft.shownAs).textFieldStyle(.roundedBorder)
                        FieldName(text: "Provider")
                        ReadOnly(text: providerTitle)
                    }
                    GridRow {
                        FieldName(text: "Model id")
                        ReadOnly(text: model.modelId, mono: true)
                        FieldName(text: "Context")
                        ReadOnly(text: context)
                    }
                    GridRow {
                        FieldName(text: "Price in / out")
                        ReadOnly(text: price)
                        FieldName(text: "Longest answer")
                        NumberField(value: $draft.maxOutput.real, show: { Format.whole(Int($0)) + " tokens" },
                                    prompt: "the model's own")
                            .disabled(!store.can(.modelSettings))
                    }
                    GridRow {
                        FieldName(text: "Reasoning")
                        MenuPicker(selection: reasoning, options: [("off", "off"), ("low", "low"), ("medium", "medium"),
                                                                   ("high", "high")])
                            .disabled(!store.can(.modelSettings))
                        FieldName(text: "Images")
                        MenuPicker(selection: Binding(get: { draft.images ?? false }, set: { draft.images = $0 }),
                                   options: [(false, "not sent"), (true, "sent")])
                            .disabled(!store.can(.modelSettings))
                    }
                    GridRow {
                        FieldName(text: "New agents")
                        MenuPicker(selection: $draft.defaultForNew, options: [(false, "do not use by default"),
                                                                              (true, "use by default")])
                            .disabled(!store.can(.defaultModel))
                        FieldName(text: "If a call fails")
                        MenuPicker(selection: $draft.onFailure, options: failureOptions)
                            .disabled(!store.can(.modelSettings))
                    }
                }
                .disabled(!store.can(.models))
            }
            if store.can(.limits) {
                FormSection("Limits", note: "for this model, across every agent that uses it; empty means no limit") {
                    LimitsFields(limits: Binding(get: { draft.limits ?? Limits(atOnce: nil, perMinute: nil, daily: nil, total: nil, atLimit: "wait", fallback: nil) },
                                                 set: { draft.limits = $0 }), onPlan: model.onPlan, fallbacks: fallbacks)
                        .disabled(!store.can(.models))
                    Hint(text: "the provider's limits apply first", color: Palette.tx3).padding(.top, 8)
                }
            }
            HStack(spacing: 10) {
                if store.can(.models) {
                    Button("Save") { save() }.buttonStyle(.primary).disabled(working || draft == model)
                    Button("Revert") { draft = model }.buttonStyle(.ghost).disabled(working || draft == model)
                }
                if let failure {
                    Hint(text: failure, color: Palette.bad).lineLimit(2)
                } else if let outcome {
                    Hint(text: outcome).lineLimit(2)
                } else {
                    Hint(text: "a change applies to each agent on its next wake")
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .onChange(of: model) { _, fresh in draft = fresh }
    }

    private var provider: Provider? { store.provider(model.through) }
    private var providerTitle: String { provider?.title ?? model.through }

    private var context: String {
        guard let context = model.context else { return "not published" }
        return Format.whole(context) + " tokens" + (provider?.takesKey == true ? " · from \(providerTitle)" : "")
    }

    private var price: String {
        if model.onPlan {
            return "on the plan"
        }
        guard let price = model.price else { return "not published" }
        return "$\(trim(price.input)) / $\(trim(price.output)) per million · from \(providerTitle)"
    }

    private func trim(_ value: Double) -> String {
        String(format: "%.2f", value)
    }

    private var reasoning: Binding<String> {
        Binding(get: { draft.reasoning ?? "off" }, set: { draft.reasoning = $0 == "off" ? nil : $0 })
    }

    private var others: [Model] {
        store.models.filter { $0.id != model.id }
    }

    private var failureOptions: [(String, String)] {
        [("retry", "try again twice, then stop"), ("stop", "stop at once")]
            + others.map { ("fallback:" + $0.id, "fall back to " + $0.shownAs) }
    }

    private var fallbacks: [(String, String)] {
        others.map { ($0.id, $0.shownAs) }
    }

    private func save() {
        var changed: Set<String> = []
        if draft.shownAs != model.shownAs { changed.insert("shown_as") }
        if draft.maxOutput != model.maxOutput { changed.insert("max_output") }
        if draft.reasoning != model.reasoning { changed.insert("reasoning") }
        if draft.images != model.images { changed.insert("images") }
        if draft.defaultForNew != model.defaultForNew { changed.insert("default_for_new") }
        if draft.onFailure != model.onFailure { changed.insert("on_failure") }
        if store.can(.limits), draft.limits != model.limits { changed.insert("limits") }
        let patch = ModelPatch(
            shownAs: draft.shownAs, maxOutput: draft.maxOutput, reasoning: draft.reasoning, images: draft.images,
            defaultForNew: draft.defaultForNew, onFailure: draft.onFailure, limits: draft.limits,
            changed: changed, once: Once.make()
        )
        run("Saved.") { try await store.saveModel(model.id, patch) }
    }

    private func test() {
        run(nil) {
            let answer = try await store.testModel(model.id)
            outcome = "→ " + Store.describe(ModelCheck(ok: answer.ok, seconds: answer.seconds, reply: answer.reply))
        }
    }

    private func remove() {
        run(nil) { try await store.removeModel(model.id) }
    }

    private func run(_ done: String?, _ work: @escaping () async throws -> Void) {
        working = true
        failure = nil
        outcome = nil
        Task {
            do {
                try await work()
                if let done {
                    outcome = done
                }
            } catch {
                failure = error.localizedDescription
            }
            working = false
        }
    }
}
