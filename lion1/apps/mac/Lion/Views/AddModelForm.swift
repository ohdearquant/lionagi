import SwiftUI

/// Add a model: the provider first, then one of its models, browsed from the provider's own list
/// where it has one; a check that must pass; then Add.
struct AddModelForm: View {
    @Environment(Store.self) private var store
    /// Called with the new model's id once it is added, or nil on Cancel.
    var done: (String?) -> Void
    @State private var form = AddForm()
    @State private var provider = ""
    @State private var query = ""
    @State private var catalogue: Catalogue?
    @State private var noList = false
    @State private var picked: CatalogueModel?
    @State private var modelId = ""
    @State private var shownAs = ""
    @State private var reasoning = "off"
    @State private var limits = Limits(atOnce: nil, perMinute: nil, daily: nil, total: nil, atLimit: "wait", fallback: nil)

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            DetailHeader("Add a model", sub: "pick where it is reached, then which model")
            FormSection("Provider") {
                Segment(items: store.providers.map(\.name), selection: $provider, title: { name in
                    store.provider(name)?.title ?? name
                }, fill: false)
                if let chosen = store.provider(provider) {
                    Hint(text: note(chosen))
                        .padding(.top, 10)
                        .padding(.bottom, 12)
                    if !noList {
                        browse
                    }
                    fields(chosen)
                }
            }
            if store.can(.limits) {
                FormSection("Limits", note: "optional; empty means no limit") {
                    LimitsFields(limits: $limits, onPlan: store.provider(provider)?.signsIn == true)
                }
            }
            FormSection("Check", note: "nothing is added until this passes") {
                CheckLine(title: "Test", form: form, draft: draft, enabled: !chosenId.isEmpty)
            }
            AddFooter(form: form, draft: draft, note: "an added model is used by no agent until you pick it on an agent's page") {
                let id = store.models.first { $0.through == provider && $0.modelId == chosenId }?.id
                done(id)
            } cancel: {
                form.reset()
                done(nil)
            }
        }
        .frame(maxWidth: 900, alignment: .leading)
        .onAppear {
            if provider.isEmpty {
                provider = store.providers.first?.name ?? ""
            }
        }
        .onChange(of: provider) { _, _ in
            picked = nil
            modelId = ""
            shownAs = ""
            catalogue = nil
            noList = false
            query = ""
            form.reset()
        }
        .task(id: provider + "\u{1F}" + query) {
            await fetch()
        }
    }

    private var chosenId: String {
        (picked?.modelId ?? modelId).trimmingCharacters(in: .whitespaces)
    }

    private var draft: AddDraft {
        .model(NewModelWrite(
            through: provider, modelId: chosenId,
            shownAs: shownAs.isEmpty ? nil : shownAs,
            reasoning: store.can(.modelSettings) && reasoning != "off" ? reasoning : nil,
            context: nil, price: nil,
            limits: store.can(.limits) ? limits : nil,
            dryRun: false, once: ""
        ))
    }

    private func note(_ provider: Provider) -> String {
        if provider.signsIn {
            return "Uses the \(provider.title) sign-in on this Mac; spend counts against that plan."
        }
        if provider.takesKey {
            return "Pick from \(provider.title)'s own list, fetched with your key. Price and context are theirs and stay current."
        }
        return "Pick from the list the address serves, or type the model's id."
    }

    private var browse: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField("", text: $query, prompt: Text(catalogue.map { "search \($0.models.count) models" } ?? "search"))
                .textFieldStyle(.roundedBorder)
            ScrollView {
                LazyVStack(spacing: 0, pinnedViews: [.sectionHeaders]) {
                    Section {
                        ForEach(catalogue?.models ?? []) { entry in
                            CatalogueRow(entry: entry, selected: picked?.modelId == entry.modelId) {
                                // a name the person typed stays; one the app filled follows the pick
                                if shownAs.isEmpty || shownAs == lastPart(picked?.modelId ?? "") {
                                    shownAs = lastPart(entry.modelId)
                                }
                                picked = entry
                            }
                        }
                        if catalogue?.models.isEmpty == true {
                            Hint(text: "Nothing in the list matches.", color: Palette.tx3).padding(10)
                        }
                    } header: {
                        HStack(spacing: 10) {
                            Text("model").frame(maxWidth: .infinity, alignment: .leading)
                            Text("context").frame(width: 80, alignment: .leading)
                            Text("price in / out").frame(width: 130, alignment: .leading)
                            Text("takes").frame(width: 110, alignment: .leading)
                        }
                        .font(.system(size: 11))
                        .foregroundStyle(Palette.tx3)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(Palette.win)
                        .hairline()
                    }
                }
            }
            .frame(height: 168)
            .boxed(8)
        }
        .padding(.bottom, 12)
    }

    @ViewBuilder private func fields(_ chosen: Provider) -> some View {
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
            if noList {
                GridRow {
                    FieldName(text: "Model id")
                    TextField("", text: $modelId, prompt: Text("the id the provider knows it by"))
                        .textFieldStyle(.roundedBorder)
                    Color.clear.frame(height: 1).gridCellColumns(2)
                }
            }
            GridRow {
                FieldName(text: "Shown as")
                TextField("", text: $shownAs, prompt: Text(lastPart(chosenId).isEmpty ? "a short name" : lastPart(chosenId)))
                    .textFieldStyle(.roundedBorder)
                FieldName(text: "Effort")
                MenuPicker(selection: $reasoning, options: [("off", "off"), ("low", "low"), ("medium", "medium"), ("high", "high")])
                    .disabled(!store.can(.modelSettings))
            }
            GridRow {
                FieldName(text: chosen.signsIn ? "Price" : "API key")
                ReadOnly(text: priceOrKey(chosen))
                if let picked, !chosen.signsIn {
                    FieldName(text: "Price in / out")
                    ReadOnly(text: picked.price.map { "$\(String(format: "%.2f", $0.input)) / $\(String(format: "%.2f", $0.output)) per million, \(chosen.title)'s" } ?? "not published")
                } else {
                    Color.clear.frame(height: 1).gridCellColumns(2)
                }
            }
        }
    }

    private func priceOrKey(_ provider: Provider) -> String {
        if provider.signsIn {
            return "on the plan"
        }
        if !provider.takesKey {
            return "whatever the address asks"
        }
        return provider.key?.isSet == true ? "the one saved under Providers" : "no key saved yet: add one under Providers"
    }

    private func lastPart(_ id: String) -> String {
        id.split(separator: "/").last.map(String.init) ?? id
    }

    private func fetch() async {
        guard !provider.isEmpty, !noList else { return }
        try? await Task.sleep(nanoseconds: 250_000_000)
        if Task.isCancelled {
            return
        }
        do {
            catalogue = try await store.catalogue(provider, query: query)
        } catch let error as APIError where error.status == 404 {
            noList = true
        } catch {
            if !Task.isCancelled {
                noList = true
            }
        }
    }
}

private struct CatalogueRow: View {
    let entry: CatalogueModel
    let selected: Bool
    var pick: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: pick) {
            HStack(spacing: 10) {
                Text(entry.modelId).font(.mono12).lineLimit(1).frame(maxWidth: .infinity, alignment: .leading)
                Text(entry.context.map { Format.compact($0) } ?? "—").frame(width: 80, alignment: .leading)
                Text(entry.price.map { "$\(String(format: "%.2f", $0.input)) / $\(String(format: "%.2f", $0.output))" } ?? "—")
                    .frame(width: 130, alignment: .leading)
                Text(entry.takes.joined(separator: ", ")).lineLimit(1).frame(width: 110, alignment: .leading)
            }
            .font(.system(size: 12.5))
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(selected ? Palette.accbg : (hover ? Palette.chip : Color.clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .hairline()
    }
}
